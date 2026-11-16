"""Static depth per sea spot -> data/spots/depth.json (run once locally, and again after catalogue changes).

    .venv/bin/python -m spots_build.depth [--only ID ...] [--workers 6]

Source: EMODnet Bathymetry DTM (`emodnet__mean`, 1/16 arc-minute ≈ 115 m, CC BY 4.0) through its public WCS
(`text/plain` grid of a ~4 km window per spot, a few KB). Where EMODnet has no value, GEBCO 2020 (15 arc-second,
public domain with attribution) through NOAA CoastWatch ERDDAP. Nothing big is downloaded: ~9.5k small requests.

Per spot:
- anchor = the spot if it is on a water cell (elevation < 0), else the nearest water cell within 1.5 km;
- depth   = median depth (m, positive, integer, ≥ 1) of the water cells within 300 m of the anchor, on the
  seaward side (bearing within ±90° of `info.facing` when known; all directions if that leaves < 3 cells);
- depth10 = distance (m, rounded to 10) from the anchor to the nearest cell ≥ 10 m deep inside the window
  (0 if the anchor already is), null if there is none within ~4 km.
Values are referred to the DTM's vertical datum (LAT where the survey data allow it): approximate, not for
navigation. Existing entries whose spot did not move are kept unless --refresh.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEA = os.path.join(ROOT, "data", "spots", "sea.json")
OUT = os.path.join(ROOT, "data", "spots", "depth.json")
WCS = "https://ows.emodnet-bathymetry.eu/wcs"
GEBCO = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/GEBCO_2020.csv"
UA = "fishershour-data-pipeline/1 (+https://github.com/dprados/fishershour-data)"
HALF_KM = 4.0          # window half-size
ANCHOR_KM = 1.5
SAMPLE_M = 300.0
DEEP_M = 10.0
KM_DEG = 111.195


def _get(url: str, timeout: float = 60.0) -> str:
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - retry any network error
            last = e
            time.sleep(2 + 4 * attempt)
    raise RuntimeError(f"{url[:120]}: {last}")


def window(lat: float, lon: float) -> tuple[float, float, float, float]:
    dlat = HALF_KM / KM_DEG
    dlon = HALF_KM / (KM_DEG * max(math.cos(math.radians(lat)), 0.2))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def parse_wcs_text(text: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """GeoServer WCS text/plain -> (z (ny, nx) with NaN, cell-centre lats (ny,), lons (nx,))."""
    def param(name):
        k = text.index(f'"{name}"')
        return float(text[k:].split(",", 1)[1].split("]", 1)[0])

    dx, x0 = param("elt_0_0"), param("elt_0_2")
    dy, y0 = param("elt_1_1"), param("elt_1_2")
    body = text.split("Band 0:", 1)[1].strip().splitlines()
    rows = [[float(v) for v in line.split()] for line in body if line.strip()]
    nx = min(len(r) for r in rows)
    z = np.array([r[:nx] for r in rows], dtype=float)
    z[~np.isfinite(z) | (np.abs(z) > 12000)] = np.nan
    lats = y0 + dy * np.arange(z.shape[0])
    lons = x0 + dx * np.arange(nx)
    return z, lats, lons


def fetch_emodnet(lat: float, lon: float):
    a, b, c, d = window(lat, lon)
    q = urllib.parse.urlencode([("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
                                ("coverageId", "emodnet__mean"), ("format", "text/plain"),
                                ("subset", f"Lat({a:.5f},{b:.5f})"), ("subset", f"Long({c:.5f},{d:.5f})")])
    return parse_wcs_text(_get(f"{WCS}?{q}"))


def fetch_gebco(lat: float, lon: float):
    a, b, c, d = window(lat, lon)
    q = f"elevation[({a:.4f}):1:({b:.4f})][({c:.4f}):1:({d:.4f})]"
    text = _get(f"{GEBCO}?{urllib.parse.quote(q, safe='[]():')}")
    lines = text.strip().splitlines()[2:]          # header + units
    pts = [tuple(float(x) for x in ln.split(",")) for ln in lines if ln]
    lats = np.unique([p[0] for p in pts])
    lons = np.unique([p[1] for p in pts])
    z = np.full((len(lats), len(lons)), np.nan)
    li = {v: k for k, v in enumerate(lats)}
    lj = {v: k for k, v in enumerate(lons)}
    for la, lo, el in pts:
        z[li[la], lj[lo]] = el
    return z, lats, lons


def depth_at(z: np.ndarray, lats: np.ndarray, lons: np.ndarray, lat: float, lon: float,
             facing: float | None) -> tuple[int | None, int | None]:
    """(depth m, depth10 m) from an elevation window (negative = water). See module doc."""
    k = math.cos(math.radians(lat))
    dy = np.broadcast_to((lats - lat)[:, None] * KM_DEG * 1000.0, z.shape)
    dx = np.broadcast_to((lons - lon)[None, :] * KM_DEG * 1000.0 * k, z.shape)
    dist = np.hypot(dy, dx)
    water = np.isfinite(z) & (z < 0)
    if not water.any():
        return None, None
    i0, j0 = np.unravel_index(np.argmin(dist), dist.shape)
    if water[i0, j0]:
        ai, aj = i0, j0
    else:
        d = np.where(water, dist, np.inf)
        ai, aj = np.unravel_index(np.argmin(d), d.shape)
        if d[ai, aj] > ANCHOR_KM * 1000.0:
            return None, None
    ry, rx = dy - dy[ai, aj], dx - dx[ai, aj]
    rd = np.hypot(ry, rx)
    near = water & (rd <= SAMPLE_M)
    sel = near
    if facing is not None:
        bearing = (np.degrees(np.arctan2(rx, ry)) + 360.0) % 360.0
        diff = np.abs((bearing - facing + 180.0) % 360.0 - 180.0)
        seaward = near & ((diff <= 90.0) | (rd < 1.0))
        if seaward.sum() >= 3:
            sel = seaward
    depth = max(1, int(round(float(np.median(-z[sel]))))) if sel.any() else None
    deep = np.isfinite(z) & (z <= -DEEP_M)
    d10 = None
    if deep.any():
        d10 = int(round(float(np.min(np.where(deep, rd, np.inf))) / 10.0) * 10)
    return depth, d10


def one(spot: dict) -> tuple[str, list]:
    lat, lon = spot["lat"], spot["lon"]
    facing = (spot.get("info") or {}).get("facing")
    src = "emodnet"
    try:
        z, la, lo = fetch_emodnet(lat, lon)
        depth, d10 = depth_at(z, la, lo, lat, lon, facing)
    except Exception as e:  # noqa: BLE001
        print(f"emodnet {spot['id']}: {e}", file=sys.stderr)
        depth = d10 = None
    if depth is None:
        src = "gebco"
        try:
            z, la, lo = fetch_gebco(lat, lon)
            depth, d10 = depth_at(z, la, lo, lat, lon, facing)
        except Exception as e:  # noqa: BLE001
            print(f"gebco {spot['id']}: {e}", file=sys.stderr)
    if depth is None:
        src, d10 = None, None
    return spot["id"], [lat, lon, depth, d10, src]


def save(rows: dict, path: str) -> None:
    blob = {"v": 1, "source": "EMODnet Bathymetry DTM (CC BY 4.0); GEBCO 2020 fallback",
            "cols": ["lat", "lon", "depth", "depth10", "src"], "spots": dict(sorted(rows.items()))}
    txt = json.dumps(blob, separators=(",", ":"), ensure_ascii=False).replace('],"', '],\n"')
    with open(path, "w") as f:
        f.write(txt + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    spots = json.load(open(SEA))["spots"]
    rows = {}
    if os.path.exists(args.out):
        rows = json.load(open(args.out)).get("spots", {})
    ids = {s["id"] for s in spots}
    rows = {k: v for k, v in rows.items() if k in ids}
    todo = [s for s in spots if (not args.only or s["id"] in args.only) and
            (args.refresh or args.only or s["id"] not in rows or
             abs(rows[s["id"]][0] - s["lat"]) > 1e-6 or abs(rows[s["id"]][1] - s["lon"]) > 1e-6)]
    print(f"{len(spots)} sea spots, {len(todo)} to compute")
    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(one, s) for s in todo]
        for n, f in enumerate(as_completed(futs), 1):
            sid, row = f.result()
            rows[sid] = row
            if n % 500 == 0:
                save(rows, args.out)
                print(f"{n}/{len(todo)} {time.time() - t0:.0f}s", flush=True)
    save(rows, args.out)
    vals = [r for r in rows.values()]
    print(f"done in {time.time() - t0:.0f}s: {sum(r[2] is not None for r in vals)} with depth "
          f"({sum(r[4] == 'emodnet' for r in vals)} EMODnet, {sum(r[4] == 'gebco' for r in vals)} GEBCO), "
          f"{sum(r[3] is not None for r in vals)} with depth10")
    return 0


if __name__ == "__main__":
    sys.exit(main())
