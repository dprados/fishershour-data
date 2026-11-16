"""Slipways and boat ramps from OpenStreetMap (ODbL) for Spain, Portugal and France, islands included.

    python -m fishershour_pipeline.ramps [--out data/ramps/ramps.json] [--from-file overpass.json]

Weekly (`ramps.yml`): one polite Overpass query (`leisure=slipway` + `man_made=slipway`, nodes and the centre
of ways/relations), deduplicated within 25 m, committed to `data/ramps/ramps.json`. The forecast build
publishes it as `v1/ramps/{tile}.json` (same 1° tiles as `index/`). If the new list is much shorter than the
committed one (partial Overpass answer) the file is left untouched.
Attribution: «© OpenStreetMap contributors (ODbL)».
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from collections import defaultdict

from . import config
from .spots import tile_of

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
QUERY = """[out:json][timeout:600];
(area["ISO3166-1"="ES"][admin_level=2];area["ISO3166-1"="PT"][admin_level=2];area["ISO3166-1"="FR"][admin_level=2];)->.a;
(nwr["leisure"="slipway"](area.a);nwr["man_made"="slipway"](area.a););
out tags center qt;"""
# Europe + Macaronesia (drops French overseas territories): lat_min, lat_max, lon_min, lon_max
BOX = (27.0, 52.0, -32.0, 10.0)
DEDUPE_M = 25.0
MIN_KEEP = 0.8          # refuse an update that loses more than 20 % of the ramps
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
USER_AGENT = "fishershour-data-pipeline/1 (+https://github.com/dprados/fishershour-data)"


def fetch(timeout: int = 900) -> list[dict]:
    import requests

    last = None
    for url in OVERPASS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": QUERY}, timeout=timeout, headers={"User-Agent": USER_AGENT})
                if r.status_code == 200:
                    blob = r.json()
                    if blob.get("remark") and "error" in blob["remark"].lower():
                        raise RuntimeError(blob["remark"][:200])
                    return blob.get("elements", [])
                last = f"{url}: HTTP {r.status_code}"
            except Exception as e:  # network, timeout, bad JSON
                last = f"{url}: {e}"
            time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"Overpass failed: {last}")


def parse(elements: list[dict]) -> list[list]:
    """Overpass elements -> sorted [[lat, lon, name|None]], inside BOX, deduplicated within DEDUPE_M."""
    pts = []
    for e in elements:
        if "lat" in e and "lon" in e:
            lat, lon = e["lat"], e["lon"]
        elif "center" in e:
            lat, lon = e["center"]["lat"], e["center"]["lon"]
        else:
            continue
        if not (BOX[0] <= lat <= BOX[1] and BOX[2] <= lon <= BOX[3]):
            continue
        name = (e.get("tags") or {}).get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        pts.append((round(lat, 5), round(lon, 5), name))
    # named first, then ways (centre) before nodes is not knowable here: keep the first by sort order
    pts.sort(key=lambda p: (p[2] is None, p[0], p[1]))
    cell = DEDUPE_M / 111_195.0
    grid: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    kept = []
    for lat, lon, name in pts:
        ci, cj = int(math.floor(lat / cell)), int(math.floor(lon / cell))
        k = math.cos(math.radians(lat))
        dup = False
        for di in (-1, 0, 1):
            for dj in (-2, -1, 0, 1, 2):
                for la, lo in grid.get((ci + di, cj + dj), ()):
                    if math.hypot((la - lat) * 111_195.0, (lo - lon) * 111_195.0 * k) < DEDUPE_M:
                        dup = True
                        break
                if dup:
                    break
            if dup:
                break
        if dup:
            continue
        grid[(ci, cj)].append((lat, lon))
        kept.append([lat, lon, name])
    kept.sort(key=lambda p: (p[0], p[1]))
    return kept


def load(path: str = config.RAMPS_FILE) -> list[list]:
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("r", [])


def save(ramps: list[list], path: str = config.RAMPS_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = {"v": 1, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "OpenStreetMap leisure=slipway, man_made=slipway (Overpass API)", "attribution": ATTRIBUTION,
            "n": len(ramps), "r": ramps}
    with open(path, "w") as f:
        f.write(json.dumps(blob, separators=(",", ":"), ensure_ascii=False).replace('],[', '],\n['))
        f.write("\n")


def tiles(ramps: list[list]) -> dict[str, dict]:
    """{tile: {"v": 1, "r": [{"lat", "lon", "n"}]}} with the index tile naming (t_{floor lat}_{floor lon})."""
    out: dict[str, dict] = {}
    for lat, lon, name in ramps:
        out.setdefault(tile_of(lat, lon), {"v": config.SCHEMA_VERSION, "r": []})["r"].append(
            {"lat": lat, "lon": lon, "n": name})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=config.RAMPS_FILE)
    ap.add_argument("--from-file", default="", help="parse a saved Overpass JSON answer instead of querying")
    ap.add_argument("--force", action="store_true", help="write even if much shorter than the current file")
    args = ap.parse_args(argv)
    if args.from_file:
        with open(args.from_file) as f:
            elements = json.load(f).get("elements", [])
    else:
        elements = fetch()
    ramps = parse(elements)
    old = load(args.out)
    print(f"{len(elements)} OSM elements -> {len(ramps)} ramps ({sum(1 for r in ramps if r[2])} named); "
          f"previous {len(old)}")
    if old and len(ramps) < MIN_KEEP * len(old) and not args.force:
        print("refusing to replace: the new list is too short (partial Overpass answer?)")
        return 1
    if [list(r) for r in old] == ramps:
        print("unchanged")
        return 0
    save(ramps, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
