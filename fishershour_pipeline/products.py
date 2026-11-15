"""Published documents: spots/{id}.json, index tiles entries, zones/{zone}.json (data-schema §1, §2, §5)."""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import numpy as np

from . import astro, config, score, tides, wtemp

DEC2 = ("hs", "ws", "wg", "cl")
DEC1 = ("tp", "t2", "p", "pr", "sst", "wt")
INT = ("dir", "wd")
SERIES = ["hs", "tp", "dir", "ws", "wg", "wd", "t2", "p", "pr", "sst", "cl", "q", "wt"]


def round_list(arr, field: str) -> list:
    a = np.asarray(arr, dtype=float)
    ok = np.isfinite(a)
    z = np.where(ok, a, 0.0)
    if field in INT:
        vals = (np.rint(z).astype(np.int64) % 360).tolist()
    elif field == "q":
        vals = [round(x, 2) if abs(x) < 10 else round(x, 1) for x in z.tolist()]
    elif field == "sc":
        vals = np.rint(z).astype(np.int64).tolist()
    else:
        vals = np.round(z, 1 if field in DEC1 else 2).tolist()
    return [v if k else None for v, k in zip(vals, ok.tolist())]


def lag(values: np.ndarray, times: np.ndarray, hours: float) -> np.ndarray:
    """Value at t - hours (linear on the finite samples), NaN when outside the data."""
    v = np.asarray(values, dtype=float)
    ok = np.isfinite(v)
    if ok.sum() < 2:
        return np.full(len(times), np.nan)
    t = times.astype(float)
    tt = t - hours * 3600
    out = np.interp(tt, t[ok], v[ok])
    out[(tt < t[ok][0]) | (tt > t[ok][-1])] = np.nan
    return out


def spring_range(cons: dict) -> float:
    return 2.0 * (cons.get("M2", [0, 0])[0] + cons.get("S2", [0, 0])[0])


def tide_doc(tc: dict | None) -> dict | None:
    """Constants for the app, or None where the tide is negligible (spring range < 0.2 m)."""
    if not tc or not tc.get("cons"):
        return None
    if spring_range(tc["cons"]) < config.TIDE_MIN_RANGE_M:
        return None
    return {"z0": tc.get("z0", 0.0), "cons": tc["cons"]}


def tide_factors(tide: dict | None, times: np.ndarray, tz: str) -> tuple[np.ndarray, np.ndarray]:
    """(v, coef) per time: v = |dh/dt| / max of the local day; coef = day range / spring range × 100."""
    n = len(times)
    if tide is None:
        return np.full(n, np.nan), np.full(n, np.nan)
    cons, z0 = tide["cons"], tide.get("z0", 0.0)
    zone = ZoneInfo(tz)
    dates = [dt.datetime.fromtimestamp(int(t), zone).date() for t in times]
    dh = (tides.predict(cons, times + 300, z0) - tides.predict(cons, times - 300, z0)) / 600.0
    v = np.full(n, np.nan)
    coef = np.full(n, np.nan)
    sr = max(spring_range(cons), 1e-6)
    for d in sorted(set(dates)):
        t0 = dt.datetime.combine(d, dt.time(0), zone).timestamp()
        fine_t = np.arange(t0, t0 + 86400 + 1, 600.0)
        h = tides.predict(cons, fine_t, z0)
        rate = np.abs(np.diff(h)) / 600.0
        idx = [k for k, dd in enumerate(dates) if dd == d]
        mx = rate.max() if len(rate) else 0
        v[idx] = np.abs(dh[idx]) / mx if mx > 0 else 0.0
        coef[idx] = round((h.max() - h.min()) / sr * 100.0)
    return np.clip(v, 0, 1), coef


def local_today(tz: str, now: dt.datetime) -> dt.date:
    return now.astimezone(ZoneInfo(tz)).date()


class SpotBuilder:
    def __init__(self, blended, h_start: dt.datetime, now: dt.datetime, generated: str, hub_meta: dict | None,
                 miteco: dict, glofas: dict):
        self.b = blended
        self.h_start = h_start
        self.now = now
        self.generated = generated
        self.hub = hub_meta or {}
        self.miteco = miteco or {}
        self.glofas = glofas or {}
        self.t_all = np.concatenate([blended.th, blended.th3])
        self.moon = astro.moon_factor(self.t_all)

    def series(self, i: int, f: str) -> np.ndarray:
        return np.concatenate([self.b.h[f][i], self.b.h3[f][i]]).astype(float)

    # ------------------------------------------------------------ freshwater helpers
    def _normal_for(self, i: int, times: np.ndarray) -> np.ndarray:
        codes = self.hub.get("codes") or []
        code = codes[i] if i < len(codes) else None
        normals = (self.hub.get("normals") or {}).get(code) if code else None
        if not normals:
            return np.full(len(times), np.nan)
        months = [dt.datetime.fromtimestamp(int(t), dt.timezone.utc).month for t in times]
        return np.array([normals[m - 1] if normals[m - 1] is not None else np.nan for m in months], dtype=float)

    def _q_for_score(self, i: int) -> np.ndarray:
        """Observed hourly q; persistence of the last observation ≤ 72 h; then GloFAS daily."""
        t = self.t_all
        q = np.full(len(t), np.nan)
        if self.b.q_hist is not None:
            qt, qv = self.b.q_hist
            row = qv[i]
            ok = np.isfinite(row)
            if ok.any():
                last_t = qt[ok][-1]
                last_v = row[ok][-1]
                interp = np.interp(t, qt[ok], row[ok])
                q = np.where(t <= last_t, interp, np.where(t <= last_t + 72 * 3600, last_v, np.nan))
                q[t < qt[ok][0]] = np.nan
        g = self.glofas.get(i)
        if g:
            start, vals = g
            for k, tt in enumerate(t):
                if np.isfinite(q[k]):
                    continue
                d = dt.datetime.fromtimestamp(int(tt), dt.timezone.utc).date()
                j = (d - start).days
                if 0 <= j < len(vals) and vals[j] is not None:
                    q[k] = vals[j]
        return q

    def _q_hist_lag24(self, i: int, q: np.ndarray) -> np.ndarray:
        """q 24 h earlier, using the Hub'Eau 48 h history before the h start when available."""
        t = self.t_all
        if self.b.q_hist is not None:
            qt, qv = self.b.q_hist
            row = qv[i]
            ok = np.isfinite(row)
            if ok.sum() >= 2:
                tt = t - 86400
                prev = np.interp(tt, qt[ok], row[ok])
                prev[(tt < qt[ok][0]) | (tt > qt[ok][-1])] = np.nan
                return np.where(np.isfinite(prev), prev, lag(q, t, 24))
        return lag(q, t, 24)

    def daily(self, i: int, spot: dict) -> dict:
        start = self.h_start.date()
        d = {"start": start.isoformat(), "q": [], "qn": [], "lvl": None, "lvlDate": None, "lvlTrend": None}
        if spot["kind"] == "river":
            n = config.DAILY_DAYS
            g = self.glofas.get(i)
            q = [None] * n
            if g:
                gs, vals = g
                n = max(n, len(vals) + (gs - start).days)
                n = min(n, config.GLOFAS_DAYS)
                q = [None] * n
                for k in range(n):
                    j = (start + dt.timedelta(days=k) - gs).days
                    if 0 <= j < len(vals):
                        q[k] = vals[j]
            if self.b.q_hist is not None:
                row = self.b.h["q"][i]
                for k in range(min(n, config.H_HOURS // 24)):
                    day = row[24 * k: 24 * (k + 1)]
                    if np.isfinite(day).sum() >= 3:
                        q[k] = float(np.nanmean(day))
            day_times = np.array([int(dt.datetime.combine(start + dt.timedelta(days=k), dt.time(12),
                                                          dt.timezone.utc).timestamp()) for k in range(n)])
            qn = self._normal_for(i, day_times)
            d["q"] = round_list(np.array([np.nan if v is None else v for v in q], float), "q")
            d["qn"] = round_list(qn, "q")
        if spot["kind"] == "reservoir":
            mid = spot.get("x", {}).get("miteco")
            rec = self.miteco.get(str(mid)) if mid is not None else None
            if rec:
                d["lvl"], d["lvlDate"], d["lvlTrend"] = rec["lvl"], rec["lvlDate"], rec["lvlTrend"]
        return d

    # ------------------------------------------------------------ document
    def build(self, i: int, spot: dict, tide_raw: dict | None) -> dict:
        t = self.t_all
        nh = len(self.b.th)
        info = dict(spot["info"])
        modes = spot["modes"]
        phase = astro.light_phase(spot["lat"], spot["lon"], t)
        p = self.series(i, "p")
        p12 = lag(p, t, 12)
        ws = self.series(i, "ws")
        series = {f: self.series(i, f) for f in SERIES if f != "wt"}
        tide = None
        if spot["kind"] == "sea":
            tide = tide_doc(tide_raw)
            v, coef = tide_factors(tide, t, spot["tz"])
            s = {"hs": series["hs"], "dir": series["dir"], "ws": ws, "wg": series["wg"], "wd": series["wd"],
                 "cl": series["cl"], "sst": series["sst"], "sst48": lag(series["sst"], t, 48), "p": p, "p12": p12,
                 "phase": phase, "tide_v": v, "coef": coef, "moon": self.moon}
            sc = {m: score.sea_scores(m, s, info.get("facing")) for m in modes}
            wt = np.full(len(t), np.nan)
        else:
            wt = wtemp.estimate(series["t2"], t, spot["kind"], bool(spot.get("x", {}).get("salmonid")))
            info["wtEstimated"] = True
            daily = None
            q = self._q_for_score(i) if spot["kind"] == "river" else np.full(len(t), np.nan)
            qn = self._normal_for(i, t)
            with np.errstate(invalid="ignore", divide="ignore"):
                r = q / qn
                q24 = self._q_hist_lag24(i, q)
                rise = q / q24 - 1.0
            rain24 = self._rain24(i)
            trend = None
            if spot["kind"] == "reservoir":
                mid = spot.get("x", {}).get("miteco")
                rec = self.miteco.get(str(mid)) if mid is not None else None
                trend = rec["lvlTrend"] if rec else None
            s = {"ws": ws, "wt": wt, "wt72": lag(wt, t, 72), "r": r, "rise24": rise, "rain24": rain24,
                 "p": p, "p12": p12, "phase": phase, "moon": self.moon, "lvl_trend": trend}
            sc = {m: score.fresh_scores(m, spot["kind"], s) for m in modes}
            del daily
        series["wt"] = wt
        h, h3 = self._blocks(series, sc, nh)
        return {
            "v": config.SCHEMA_VERSION, "id": spot["id"], "name": spot["name"], "aliases": spot.get("aliases", []),
            "lat": spot["lat"], "lon": spot["lon"], "country": spot["country"], "zone": spot["zone"],
            "region": spot["region"], "tz": spot["tz"], "kind": spot["kind"], "type": spot["type"],
            "modes": modes, "info": info, "generated": self.generated,
            "h": h, "h3": h3, "daily": self.daily(i, spot), "tide": tide,
        }

    def _rain24(self, i: int) -> np.ndarray:
        t = self.t_all
        if self.b.rain_acc is None:
            return np.full(len(t), np.nan)
        rt, acc = self.b.rain_acc
        row = acc[i].astype(float)
        ok = np.isfinite(row)
        if ok.sum() < 2:
            return np.full(len(t), np.nan)
        a_now = np.interp(t, rt[ok], row[ok], left=0.0)
        a_prev = np.interp(t - 86400, rt[ok], row[ok], left=0.0)
        out = a_now - a_prev
        out[t > rt[ok][-1]] = np.nan
        return out

    def _blocks(self, series: dict, sc: dict, nh: int):
        def blk(sl, start, step, n):
            d = {"start": start, "step": step, "n": n}
            for f in SERIES:
                d[f] = round_list(series[f][sl], f)
            d["sc"] = {m: round_list(v[sl], "sc") for m, v in sc.items()}
            return d

        fmt = "%Y-%m-%dT%H:%M:%SZ"
        h_start = dt.datetime.fromtimestamp(int(self.b.th[0]), dt.timezone.utc).strftime(fmt)
        h3_start = dt.datetime.fromtimestamp(int(self.b.th3[0]), dt.timezone.utc).strftime(fmt)
        return (blk(slice(0, nh), h_start, 1, nh),
                blk(slice(nh, None), h3_start, config.H3_STEP, len(self.b.th3)))


def doc_is_complete(doc: dict, min_frac: float = 0.5) -> bool:
    h = doc["h"]
    n = max(h["n"], 1)
    ws_ok = sum(x is not None for x in h["ws"]) / n
    if doc["kind"] == "sea":
        return ws_ok >= min_frac and sum(x is not None for x in h["hs"]) / n >= min_frac
    return ws_ok >= min_frac


def day_hours(doc: dict, d: dt.date) -> list[int]:
    zone = ZoneInfo(doc["tz"])
    start = dt.datetime.strptime(doc["h"]["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return [k for k in range(doc["h"]["n"]) if (start + dt.timedelta(hours=k)).astimezone(zone).date() == d]


def best_of_day(doc: dict, d: dt.date) -> tuple[int, str | None, int | None]:
    """(best score, its modality, hour index) of a local day; (0, None, None) without data."""
    idx = day_hours(doc, d)
    best = (0, None, None)
    for m in doc["modes"]:
        row = doc["h"]["sc"].get(m) or []
        for k in idx:
            v = row[k] if k < len(row) else None
            if v is not None and v > best[0]:
                best = (v, m, k)
    if best[1] is None and doc["modes"]:
        best = (0, doc["modes"][0], None)
    return best


def index_entry(doc: dict, now: dt.datetime) -> dict:
    b, bm, _ = best_of_day(doc, local_today(doc["tz"], now))
    return {"id": doc["id"], "n": doc["name"], "lat": doc["lat"], "lon": doc["lon"], "k": doc["kind"],
            "t": doc["type"], "z": doc["zone"], "g": doc["region"], "m": doc["modes"], "b": b, "bm": bm}


def zone_candidates(doc: dict, now: dt.datetime) -> dict[str, dict]:
    out = {}
    today = local_today(doc["tz"], now)
    start = dt.datetime.strptime(doc["h"]["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    for d in (today, today + dt.timedelta(days=1)):
        b, bm, k = best_of_day(doc, d)
        if bm is None:
            continue
        at = None if k is None else (start + dt.timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out[d.isoformat()] = {"id": doc["id"], "n": doc["name"], "k": doc["kind"], "b": b, "bm": bm, "at": at}
    return out


def zone_doc(zone: str, cands: list[dict[str, dict]], generated: str, top: int = 10) -> dict:
    dates = sorted({d for c in cands for d in c})[:2]
    days = []
    for d in dates:
        rows = [c[d] for c in cands if d in c]
        rows.sort(key=lambda r: (-r["b"], r["id"]))
        days.append({"date": d, "best": rows[:top]})
    return {"v": config.SCHEMA_VERSION, "zone": zone, "generated": generated, "days": days}
