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
    sc = b.get("sc") or {}
    if set(sc) != set(doc["modes"]):
        errors.append(f"{doc['id']}: {name}.sc modes {sorted(sc)} != {doc['modes']}")
    for m, arr in sc.items():
        if len(arr) != b.get("n") or any(v is not None and not (isinstance(v, int) and 0 <= v <= 100) for v in arr):
            errors.append(f"{doc['id']}: {name}.sc.{m} invalid")


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
            if not os.path.exists(os.path.join(root, "spots", f"{e['id']}.json")):
                errors.append(f"tile {t}/{e['id']}: spot file missing")
    for z in zone_ids:
        if not os.path.exists(os.path.join(root, "zones", f"{z}.json")):
            errors.append(f"zone file missing: {z}")
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
          f"{len(zone_ids)} zones, {len(region_ids)} regions, {mb:.0f} MB; "
          f"{len(errors)} errors, {len(warnings)} warnings")
    for e in errors[:40]:
        print("ERROR", e)
    for w in warnings[:15]:
        print("warn ", w)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
