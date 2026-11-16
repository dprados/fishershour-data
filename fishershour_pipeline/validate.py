"""Validate a built v1 site against data-schema.md (structure, ranges, cross-references, size budget).

    python -m fishershour_pipeline.validate site/v1 [--sample N]
Exit 1 on any error. Prints a short report.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

from . import config

KINDS = {"sea", "river", "reservoir", "lake"}
TYPES = {"beach", "rock", "pier", "harbour", "rivermouth", "estuary", "river", "reservoir", "lake"}
SEA_MODES = {"surf", "spin", "rock", "eging", "kayak", "spear"}
FRESH_MODES = {"fspin", "fly", "bottom"}
ID_RE = re.compile(r"^[a-z]{2}-[a-z0-9]+(-[a-z0-9]+)*$")
FIELDS = ["hs", "tp", "dir", "ws", "wg", "wd", "t2", "p", "pr", "sst", "cl", "q", "wt"]
RANGES = {"hs": (0, 25), "tp": (0, 30), "dir": (0, 359), "ws": (0, 70), "wg": (0, 90), "wd": (0, 359),
          "t2": (-40, 50), "p": (900, 1080), "pr": (0, 300), "sst": (-2, 35), "cl": (0, 1), "q": (0, 50000),
          "wt": (0, 40)}
# Optional series (added 16-11-2026): validated when present. cs/cd only on sea spots.
OPT_FIELDS = ["cc", "wx", "cs", "cd"]
OPT_RANGES = {"cc": (0, 100), "wx": (0, 99), "cs": (0, 5), "cd": (0, 359)}
INT_FIELDS = {"cc", "wx", "cd"}
WX_CODES = {0, 1, 2, 3, 45, 51, 61, 63, 65, 71, 80, 95, 96}
STORM_KINDS = {"thunder", "gale"}
MAX_SITE_MB = 800


def check_block(doc, name, n_expected, step, errors):
    b = doc.get(name)
    if not isinstance(b, dict):
        errors.append(f"{doc['id']}: missing {name}")
        return
    if b.get("step") != step or b.get("n") != n_expected:
        errors.append(f"{doc['id']}: {name} step/n {b.get('step')}/{b.get('n')}")
    for f in FIELDS:
        arr = b.get(f)
        if not isinstance(arr, list) or len(arr) != b.get("n"):
            errors.append(f"{doc['id']}: {name}.{f} length")
            continue
        lo, hi = RANGES[f]
        bad = [v for v in arr if v is not None and not (lo <= v <= hi)]
        if bad:
            errors.append(f"{doc['id']}: {name}.{f} out of range {bad[:3]}")
    for f in OPT_FIELDS:
        if f not in b:
            continue
        if f in ("cs", "cd") and doc.get("kind") != "sea":
            errors.append(f"{doc['id']}: {name}.{f} on a freshwater spot")
        arr = b[f]
        if not isinstance(arr, list) or len(arr) != b.get("n"):
            errors.append(f"{doc['id']}: {name}.{f} length")
            continue
        lo, hi = OPT_RANGES[f]
        bad = [v for v in arr if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool)
                                                   or not (lo <= v <= hi) or (f in INT_FIELDS and not _is_int(v))
                                                   or (f == "wx" and v not in WX_CODES))]
        if bad:
            errors.append(f"{doc['id']}: {name}.{f} invalid {bad[:3]}")
    sc = b.get("sc") or {}
    if set(sc) != set(doc["modes"]):
        errors.append(f"{doc['id']}: {name}.sc modes {sorted(sc)} != {doc['modes']}")
    for m, arr in sc.items():
        if len(arr) != b.get("n") or any(v is not None and not (isinstance(v, int) and 0 <= v <= 100) for v in arr):
            errors.append(f"{doc['id']}: {name}.sc.{m} invalid")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_tile_hours(e: dict) -> list[str]:
    """Optional h0/hs of a tile entry (older files lack them; the pipeline always writes both)."""
    if "h0" not in e and "hs" not in e:
        return []
    if ("h0" in e) != ("hs" in e):
        return ["h0 and hs must come together"]
    out = []
    h0, hs = e["h0"], e["hs"]
    if not _is_int(h0) or h0 <= 0 or h0 % 3600:
        out.append(f"bad h0 {h0!r}")
    if not isinstance(hs, list) or len(hs) != config.TILE_HOURS:
        out.append(f"hs must be a list of {config.TILE_HOURS}")
    elif any(v is not None and not (_is_int(v) and 0 <= v <= 100) for v in hs):
        out.append("hs values must be ints 0-100 or null")
    return out


def check_storm(storm) -> list[str]:
    """Optional doc-level `storm`: [{from, to, kind, max}] (epoch seconds on the hour, from < to)."""
    if not isinstance(storm, list):
        return ["storm must be a list"]
    out = []
    for e in storm:
        if not isinstance(e, dict) or set(e) != {"from", "to", "kind", "max"}:
            out.append(f"bad storm entry {e!r}"[:120])
            continue
        if not (_is_int(e["from"]) and _is_int(e["to"]) and e["from"] < e["to"]
                and e["from"] % 3600 == 0 and e["to"] % 3600 == 0):
            out.append(f"bad storm interval {e['from']}–{e['to']}")
        if e["kind"] not in STORM_KINDS:
            out.append(f"bad storm kind {e['kind']!r}")
        m = e["max"]
        if m is not None and (isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= 90):
            out.append(f"bad storm max {m!r}")
    return out


def check_depth(doc) -> list[str]:
    info = doc.get("info") or {}
    out = []
    d, d10 = info.get("depth"), info.get("depth10")
    if d is not None:
        if doc.get("kind") != "sea":
            out.append("depth on a freshwater spot")
        elif isinstance(d, bool) or not isinstance(d, (int, float)) or not 0 < d <= 11000:
            out.append(f"bad depth {d!r}")
    if d10 is not None and (not _is_int(d10) or d10 < 0 or doc.get("kind") != "sea"):
        out.append(f"bad depth10 {d10!r}")
    return out


def check_ramp_tile(tile: str, doc) -> list[str]:
    """`ramps/{tile}.json`: {"v": 1, "r": [{"lat", "lon", "n"}]} with every point inside the 1° tile."""
    if not isinstance(doc, dict) or doc.get("v") != config.SCHEMA_VERSION or not isinstance(doc.get("r"), list):
        return [f"ramps {tile}: bad structure"]
    try:
        _, la, lo = tile.split("_")
        la, lo = int(la), int(lo)
    except ValueError:
        return [f"ramps {tile}: bad tile name"]
    out = []
    for r in doc["r"]:
        if not isinstance(r, dict) or set(r) != {"lat", "lon", "n"}:
            out.append(f"ramps {tile}: bad entry {r!r}"[:120])
            continue
        if not (la <= r["lat"] < la + 1 and lo <= r["lon"] < lo + 1):
            out.append(f"ramps {tile}: point outside tile {r['lat']},{r['lon']}")
        if r["n"] is not None and not isinstance(r["n"], str):
            out.append(f"ramps {tile}: bad name")
    return out


def check_spot(doc, errors, warnings):
    sid = doc.get("id", "?")
    for k in ("v", "id", "name", "aliases", "lat", "lon", "country", "zone", "region", "tz", "kind", "type", "modes",
              "info", "generated", "h", "h3", "daily", "tide"):
        if k not in doc:
            errors.append(f"{sid}: missing {k}")
            return
    if not ID_RE.match(sid):
        errors.append(f"{sid}: bad id")
    if doc["kind"] not in KINDS or doc["type"] not in TYPES:
        errors.append(f"{sid}: bad kind/type {doc['kind']}/{doc['type']}")
    allowed = SEA_MODES if doc["kind"] == "sea" else FRESH_MODES
    if not doc["modes"] or not set(doc["modes"]) <= allowed:
        errors.append(f"{sid}: bad modes {doc['modes']}")
    check_block(doc, "h", config.H_HOURS, 1, errors)
    check_block(doc, "h3", config.H3_N, config.H3_STEP, errors)
    info = doc["info"]
    for k in ("facing", "depth", "bottom", "species", "sources"):
        if k not in info:
            errors.append(f"{sid}: info.{k} missing")
    if doc["kind"] != "sea" and info.get("facing") is not None:
        errors.append(f"{sid}: freshwater with facing")
    if doc["kind"] != "sea" and not info.get("wtEstimated"):
        warnings.append(f"{sid}: wt not flagged as estimate")
    d = doc["daily"]
    for k in ("start", "q", "qn", "lvl", "lvlDate", "lvlTrend"):
        if k not in d:
            errors.append(f"{sid}: daily.{k} missing")
    if doc["tide"] is not None:
        if doc["kind"] != "sea" or "cons" not in doc["tide"]:
            errors.append(f"{sid}: bad tide")
    if doc["kind"] == "sea" and all(v is None for v in doc["h"]["hs"]):
        warnings.append(f"{sid}: no waves")
    if all(v is None for v in doc["h"]["ws"]):
        errors.append(f"{sid}: no wind")
    if "storm" in doc:
        errors.extend(f"{sid}: {m}" for m in check_storm(doc["storm"]))
    errors.extend(f"{sid}: {m}" for m in check_depth(doc))
    if "cc" in doc["h"] and all(v is None for v in doc["h"]["cc"]):
        warnings.append(f"{sid}: no cloud cover")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--sample", type=int, default=0, help="validate only N random spot files (0 = all)")
    args = ap.parse_args(argv)
    root = args.site.rstrip("/")
    errors, warnings = [], []
    meta = json.load(open(os.path.join(root, "index", "meta.json")))
    for k in ("v", "generated", "tiles", "zones", "regions", "status", "attribution"):
        if k not in meta:
            errors.append(f"meta: missing {k}")
    region_ids = {r["id"] for r in meta.get("regions", [])}
    for r in meta.get("regions", []):
        if not r.get("rules", "").startswith("http"):
            errors.append(f"region {r['id']}: no rules URL")
        if r.get("declare") not in (None, "pescarec", "recfishing", "pt"):
            errors.append(f"region {r['id']}: bad declare")
    zone_ids = {z["id"] for z in meta.get("zones", [])}
    index_ids = set()
    for t in meta["tiles"]:
        tile = json.load(open(os.path.join(root, "index", f"{t}.json")))
        for e in tile["spots"]:
            index_ids.add(e["id"])
            for k in ("id", "n", "lat", "lon", "k", "t", "z", "g", "m", "b", "bm"):
                if k not in e:
                    errors.append(f"tile {t}/{e.get('id')}: missing {k}")
            if e.get("z") not in zone_ids:
                errors.append(f"tile {t}/{e['id']}: unknown zone {e.get('z')}")
            if e.get("g") not in region_ids:
                errors.append(f"tile {t}/{e['id']}: unknown region {e.get('g')}")
            if not (0 <= e.get("b", -1) <= 100):
                errors.append(f"tile {t}/{e['id']}: bad b")
            errors.extend(f"tile {t}/{e['id']}: {msg}" for msg in check_tile_hours(e))
            if not os.path.exists(os.path.join(root, "spots", f"{e['id']}.json")):
                errors.append(f"tile {t}/{e['id']}: spot file missing")
    for z in zone_ids:
        if not os.path.exists(os.path.join(root, "zones", f"{z}.json")):
            errors.append(f"zone file missing: {z}")
    ramp_tiles = meta.get("ramps") or []
    nramps = 0
    for t in ramp_tiles:
        path = os.path.join(root, "ramps", f"{t}.json")
        if not os.path.exists(path):
            errors.append(f"ramps tile missing: {t}")
            continue
        doc = json.load(open(path))
        errors.extend(check_ramp_tile(t, doc))
        nramps += len(doc.get("r") or [])
    aliases = json.load(open(os.path.join(root, "index", "aliases.json"))).get("aliases", {})
    for old, new in aliases.items():
        if new not in index_ids:
            errors.append(f"alias {old} -> {new}: target not indexed")
    ids = sorted(index_ids)
    if args.sample:
        random.seed(0)
        ids = random.sample(ids, min(args.sample, len(ids)))
    for sid in ids:
        with open(os.path.join(root, "spots", f"{sid}.json")) as f:
            doc = json.load(f)
        check_spot(doc, errors, warnings)
    size = 0
    for dp, _dn, fns in os.walk(os.path.dirname(root) if os.path.basename(root) == "v1" else root):
        size += sum(os.path.getsize(os.path.join(dp, fn)) for fn in fns)
    mb = size / 1e6
    if mb > MAX_SITE_MB:
        errors.append(f"site is {mb:.0f} MB > {MAX_SITE_MB} MB")
    print(f"validated {len(ids)} spots, {len(index_ids)} indexed, {len(meta['tiles'])} tiles, "
          f"{len(zone_ids)} zones, {len(region_ids)} regions, {nramps} ramps in {len(ramp_tiles)} tiles, {mb:.0f} MB; "
          f"{len(errors)} errors, {len(warnings)} warnings")
    for e in errors[:40]:
        print("ERROR", e)
    for w in warnings[:15]:
        print("warn ", w)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
