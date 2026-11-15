"""Hub'Eau hydrométrie (Eaufrance, Licence Ouverte 2.0): real-time flow near French river spots.

- Stations: /v2/hydrometrie/referentiel/stations (en_service). A river spot takes the nearest station on the
  same river (normalised name) within 15 km, or any station within 3 km. Mapping cached.
- Observations: /v2/hydrometrie/observations_tr, grandeur Q (l/s -> m³/s), hourly means.
- Normals: /v2/hydrometrie/obs_elab QmM (monthly mean, l/s) -> per-station median of each calendar month,
  stored in data/fresh/hubeau_normals.json (refreshed when older than 120 days).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .. import config
from .base import SourceData, SourceStatus, log, session

NORMALS = os.path.join("data", "fresh", "hubeau_normals.json")
STOP = {"la", "le", "les", "l", "riviere", "fleuve", "ruisseau", "canal", "de", "du", "des", "d"}


def norm_river(name: str | None) -> str:
    if not name:
        return ""
    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    toks = [x for x in re.split(r"[^a-z0-9]+", t) if x and x not in STOP]
    return " ".join(toks)


def _get(path: str, params: dict, timeout: float = 60) -> dict:
    url = f"{config.HUBEAU}/{path}"
    for attempt in range(4):
        try:
            r = session().get(url, params=params, timeout=timeout)
            if r.status_code in (200, 206):
                return r.json()
            if r.status_code == 400:
                raise ValueError(f"400 {r.text[:200]}")
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("hubeau %s retry: %s", path, e)
        time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"hubeau {path} failed")


def stations(cache_dir: str | None) -> list[dict]:
    p = os.path.join(cache_dir, "hubeau_stations.json") if cache_dir else None
    if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < 7 * 86400:
        with open(p) as f:
            return json.load(f)
    out, page = [], 1
    while True:
        d = _get("v2/hydrometrie/referentiel/stations",
                 {"en_service": "true", "format": "json", "size": 5000, "page": page,
                  "fields": "code_station,libelle_station,longitude_station,latitude_station,libelle_cours_eau"})
        data = d.get("data") or []
        out.extend(x for x in data if x.get("latitude_station") and x.get("longitude_station"))
        if len(data) < 5000 or page > 5:
            break
        page += 1
    if p:
        os.makedirs(cache_dir, exist_ok=True)
        with open(p, "w") as f:
            json.dump(out, f)
    return out


def assign(spots: list[dict], st: list[dict], k: int = 3) -> list[list[str]]:
    """Candidate station codes per spot (French rivers only), best first: same river within 30 km,
    then any station within 3 km. Many in-service stations publish no real-time Q, so the caller keeps
    the first candidate that has data."""
    grid: dict[tuple[int, int], list[dict]] = {}
    for s in st:
        grid.setdefault((int(s["latitude_station"] * 5), int(s["longitude_station"] * 5)), []).append(s)
    out = []
    for sp in spots:
        if sp.get("country") != "FR" or sp.get("kind") != "river":
            out.append([])
            continue
        river = norm_river(sp.get("x", {}).get("river") or sp.get("info", {}).get("river") or sp["name"])
        cands = []
        ci, cj = int(sp["lat"] * 5), int(sp["lon"] * 5)
        kx = 111.32 * math.cos(math.radians(sp["lat"]))
        for i in range(ci - 1, ci + 2):
            for j in range(cj - 1, cj + 2):
                for s in grid.get((i, j), ()):
                    d = math.hypot((s["latitude_station"] - sp["lat"]) * 110.54,
                                   (s["longitude_station"] - sp["lon"]) * kx)
                    same = bool(river) and norm_river(s.get("libelle_cours_eau")) == river
                    if (same and d <= config.HUBEAU_MAX_KM_SAME_RIVER) or d <= config.HUBEAU_MAX_KM_ANY:
                        cands.append((d - (100 if same else 0), s["code_station"]))
        out.append([c for _, c in sorted(cands)[:k]])
    return out


def observations(codes: list[str], since: dt.datetime, workers: int = 6) -> dict[str, dict[int, float]]:
    """{station: {unix_hour: q m³/s}} hourly means of real-time Q."""
    uniq = sorted(set(c for c in codes if c))
    batches = [uniq[i:i + 12] for i in range(0, len(uniq), 12)]

    def one(batch):
        res: dict[str, dict[int, list[float]]] = {}
        d = _get("v2/hydrometrie/observations_tr",
                 {"code_entite": ",".join(batch), "grandeur_hydro": "Q", "size": 20000,
                  "date_debut_obs": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "fields": "code_station,date_obs,resultat_obs"})
        for o in d.get("data") or []:
            if o.get("resultat_obs") is None or o["resultat_obs"] < 0:   # tidal/backwater stations
                continue
            t = dt.datetime.fromisoformat(o["date_obs"].replace("Z", "+00:00"))
            h = int(t.replace(minute=0, second=0, microsecond=0).timestamp())
            res.setdefault(o["code_station"], {}).setdefault(h, []).append(float(o["resultat_obs"]) / 1000.0)
        return res

    out: dict[str, dict[int, float]] = {}
    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(lambda b: _safe(one, b), batches):
            for code, hours in (res or {}).items():
                out[code] = {h: float(np.mean(v)) for h, v in hours.items()}
    return out


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:  # noqa: BLE001
        log.warning("hubeau batch failed: %s", e)
        return None


def load_normals(path: str = NORMALS) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"updated": {}, "q": {}}


def refresh_normals(codes: list[str], path: str = NORMALS, max_age_days: int = 120, budget: int = 2500) -> dict:
    """Monthly median of QmM per station (m³/s, 12 values, None if < 3 years)."""
    store = load_normals(path)
    now = time.time()
    todo = [c for c in sorted(set(c for c in codes if c))
            if now - store["updated"].get(c, 0) > max_age_days * 86400][:budget]

    def one(code):
        try:
            d = _get("v2/hydrometrie/obs_elab", {"code_entite": code, "grandeur_hydro_elab": "QmM", "size": 20000,
                                                 "fields": "date_obs_elab,resultat_obs_elab"})
        except Exception as e:  # noqa: BLE001
            log.info("normals %s: %s", code, e)
            return code, None
        per = {m: [] for m in range(1, 13)}
        for o in d.get("data") or []:
            if o.get("resultat_obs_elab") is None:
                continue
            m = int(o["date_obs_elab"][5:7])
            per[m].append(float(o["resultat_obs_elab"]) / 1000.0)
        return code, [round(float(np.median(v)), 3) if len(v) >= 3 else None for m, v in sorted(per.items())]

    if todo:
        with ThreadPoolExecutor(6) as ex:
            for code, vals in ex.map(one, todo):
                if vals is not None:
                    store["q"][code] = vals
                    store["updated"][code] = int(now)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(store, f, separators=(",", ":"))
    log.info("hubeau normals: %d refreshed, %d known", len(todo), len(store["q"]))
    return store


def fetch(spots: list[dict], h_start: dt.datetime, cache_dir: str | None, normals_path: str = NORMALS):
    """SourceData with field `q` (m³/s, hourly, from h_start - 48 h) + per-spot station/normals meta."""
    t0 = time.time()
    try:
        st = stations(cache_dir)
    except Exception as e:  # noqa: BLE001
        return None, SourceStatus("hubeau", False, time.time() - t0, f"stations: {e}")
    cands = assign(spots, st)
    since = h_start - dt.timedelta(hours=48)
    obs = observations([c for cs in cands for c in cs], since)
    codes = [next((c for c in cs if obs.get(c)), None) for cs in cands]
    n_assigned = sum(1 for cs in cands if cs)
    try:
        normals = refresh_normals([c for c in codes if c], normals_path)
    except Exception as e:  # noqa: BLE001
        log.warning("normals failed: %s", e)
        normals = load_normals(normals_path)
    times = int(since.timestamp()) + 3600 * np.arange(48 + config.H_HOURS, dtype=np.int64)
    q = np.full((len(spots), len(times)), np.nan, dtype=np.float32)
    for i, c in enumerate(codes):
        if not c or c not in obs:
            continue
        for h, v in obs[c].items():
            k = (h - times[0]) // 3600
            if 0 <= k < len(times):
                q[i, k] = v
    covered = np.isfinite(q).any(axis=1)
    meta = {"codes": codes, "normals": {c: normals["q"].get(c) for c in set(codes) if c}}
    detail = f"{n_assigned} river spots near a station, {int(covered.sum())} with real-time flow"
    ok = bool(covered.sum() > 0.25 * max(n_assigned, 1))
    return SourceData("hubeau", times, {"q": q}, covered, meta), SourceStatus("hubeau", ok, time.time() - t0, detail)
