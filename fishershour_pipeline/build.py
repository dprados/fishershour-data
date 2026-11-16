"""Forecast build: fetch sources -> blend -> score -> write the static v1 site.

    python -m fishershour_pipeline.build --out site/v1 --cache cache --work work

Exit 0 when at least one spot got fresh data, 2 otherwise (the workflow then skips the deploy and the
previous site stays online), 3 when a published id vanished without alias.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import config, ids, products, ramps
from .blend import Sources, blend
from .sources import arome, copernicus, ecmwf, glofas, hubeau, icon_eu, miteco
from .sources.base import SourceStatus
from .spots import load_depth, load_spots, load_zone_names, regions_meta, tile_of
from .state import LastGood

log = logging.getLogger("fishershour")
ALL_SOURCES = ("waves", "currents", "sst", "kd", "ecmwf", "icon-eu", "arome", "hubeau", "miteco", "glofas")


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def write(path: str, obj) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = dumps(obj)
    with open(path, "w") as f:
        f.write(raw)
    return raw


def load_tides(path: str) -> dict:
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f).get("spots", {})
    return {}


def fetch_all(spots, now, h_start, work, cache, skip: set[str]) -> tuple[Sources, list[SourceStatus]]:
    src = Sources()
    statuses: list[SourceStatus] = []

    def guarded(name, fn):
        if name in skip:
            return None
        t0 = time.time()
        try:
            return fn()
        except Exception as e:  # a source must never take the run down
            log.exception("source %s crashed", name)
            return ("crash", SourceStatus(name, False, time.time() - t0, f"crash: {e}"))

    jobs = {
        "waves": lambda: copernicus.fetch_waves(spots, h_start, os.path.join(work, "cmems"), cache),
        "currents": lambda: copernicus.fetch_currents(spots, h_start, os.path.join(work, "cmems"), cache),
        "sst": lambda: copernicus.fetch_sst(spots, h_start, os.path.join(work, "cmems"), cache),
        "kd": lambda: copernicus.fetch_kd(spots, h_start, os.path.join(work, "cmems"), cache),
        "ecmwf": lambda: ecmwf.fetch(spots, now, cache),
        "icon-eu": lambda: icon_eu.fetch(spots, now, cache),
        "arome": lambda: arome.fetch(spots, now, cache),
        "hubeau": lambda: hubeau.fetch(spots, h_start, cache),
        "miteco": lambda: miteco.fetch(cache),
        "glofas": lambda: glofas.fetch(spots, now, os.path.join(work, "glofas")),
    }
    with ThreadPoolExecutor(len(jobs)) as ex:
        futs = {name: ex.submit(guarded, name, fn) for name, fn in jobs.items()}
        results = {name: f.result() for name, f in futs.items()}

    for name, res in results.items():
        if res is None:
            continue
        if isinstance(res, tuple) and res and res[0] == "crash":
            statuses.append(res[1])
            continue
        if name == "ecmwf":
            src.ecmwf_wave, src.ecmwf_wind, sts = res
            statuses.extend(sts)
            continue
        data, st = res
        statuses.extend(st if isinstance(st, list) else [st])
        if name == "waves":
            src.waves = data
        elif name == "currents":
            src.currents = data
        elif name == "sst":
            src.sst = data
        elif name == "kd":
            src.kd = data
        elif name == "icon-eu":
            src.icon = data
        elif name == "arome":
            src.arome = data
        elif name == "hubeau":
            src.hubeau = data
        elif name == "miteco":
            src.miteco = data or {}
        elif name == "glofas":
            src.glofas = data or {}
    return src, statuses


def meta_status(statuses: list[SourceStatus]) -> dict:
    """Contract `status`: short per-source word (ok | failed | nokey | skipped)."""
    by = {s.name: s for s in statuses}

    def word(*names):
        found = [by[n] for n in names if n in by]
        if not found:
            return "skipped"
        if any("nokey" in s.detail for s in found):
            return "nokey"
        return "ok" if all(s.ok for s in found) else "failed"

    return {"ibi": word("ibi"), "medsea": word("medsea"), "sst": word("cmems-sst"), "clarity": word("cmems-kd"),
            "currents": word("cmems-cur"),
            "ecmwf": word("ecmwf-wave", "ecmwf-wind"), "icon": word("icon-eu"), "arome": word("arome"),
            "glofas": word("glofas"), "hubeau": word("hubeau"), "miteco": word("miteco")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/v1")
    ap.add_argument("--spots", nargs="+", default=config.SPOT_FILES)
    ap.add_argument("--tides", default="data/tides/eot20_constituents.json")
    ap.add_argument("--depth", default=config.DEPTH_FILE)
    ap.add_argument("--ramps", default=config.RAMPS_FILE)
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--work", default="work")
    ap.add_argument("--skip", nargs="*", default=[], help=f"sources to skip: {ALL_SOURCES}")
    ap.add_argument("--limit", type=int, default=0, help="only the first N spots (debug)")
    ap.add_argument("--only", nargs="*", default=[], help="only these spot ids (debug)")
    ap.add_argument("--base-url", default=config.BASE_URL, help="published site, for last-good fallback")
    ap.add_argument("--report", default="")
    ap.add_argument("--published", default=ids.PUBLISHED)
    ap.add_argument("--aliases", default=ids.ALIASES)
    ap.add_argument("--update-ids", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    t_start = time.time()
    timings = {}
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    generated = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    h_start = now.replace(hour=0, minute=0, second=0)

    spots = load_spots(args.spots)
    if args.only:
        spots = [s for s in spots if s["id"] in set(args.only)]
    if args.limit:
        spots = spots[: args.limit]
    if not spots:
        log.error("no spots")
        return 2
    tides = load_tides(args.tides)
    log.info("%d spots, %d with tide constituents", len(spots), sum(1 for s in spots if s["id"] in tides))

    t = time.time()
    src, statuses = fetch_all(spots, now, h_start, args.work, args.cache, set(args.skip))
    timings["fetch"] = time.time() - t
    for st in statuses:
        log.info("source %-11s ok=%s %.0fs %.0f MB %s", st.name, st.ok, st.seconds, st.bytes / 1e6, st.detail)

    t = time.time()
    blended = blend(src, spots, h_start)
    timings["blend"] = time.time() - t

    t = time.time()
    builder = products.SpotBuilder(blended, h_start, now, generated, src.hubeau.meta if src.hubeau else None,
                                   src.miteco, src.glofas, load_depth(args.depth))
    last_good = LastGood(args.cache, args.base_url)
    out = args.out
    tiles: dict[str, list] = defaultdict(list)
    zone_cands: dict[str, list] = defaultdict(list)
    zone_bbox: dict[str, list[float]] = {}
    regions_used: set[str] = set()
    fresh = stale = dropped = 0
    stale_ids = []
    for i, spot in enumerate(spots):
        doc = builder.build(i, spot, tides.get(spot["id"]))
        if products.doc_is_complete(doc):
            raw = write(os.path.join(out, "spots", f"{spot['id']}.json"), doc)
            last_good.save(doc, raw)
            fresh += 1
        else:
            prev = last_good.load(spot["id"]) if args.base_url or args.cache else None
            if prev is not None and prev.get("v") == config.SCHEMA_VERSION:
                doc = prev
                write(os.path.join(out, "spots", f"{spot['id']}.json"), doc)
                stale += 1
                stale_ids.append(spot["id"])
            elif any(x is not None for x in doc["h"]["ws"]):
                write(os.path.join(out, "spots", f"{spot['id']}.json"), doc)
                stale += 1
                stale_ids.append(spot["id"])
            else:
                dropped += 1
                log.warning("spot %s dropped: no data and no last-good", spot["id"])
                continue
        tiles[tile_of(spot["lat"], spot["lon"])].append(products.index_entry(doc, now))
        z = spot["zone"]
        bb = zone_bbox.setdefault(z, [spot["lat"], spot["lon"], spot["lat"], spot["lon"]])
        bb[0], bb[1] = min(bb[0], spot["lat"]), min(bb[1], spot["lon"])
        bb[2], bb[3] = max(bb[2], spot["lat"]), max(bb[3], spot["lon"])
        zone_cands[z].append(products.zone_candidates(doc, now))
        regions_used.add(spot["region"])
    timings["spots"] = time.time() - t

    # Permanent ids
    published_now = {s["id"] for s in spots if os.path.exists(os.path.join(out, "spots", f"{s['id']}.json"))}
    known = ids.load_published(args.published)
    aliases, orphans = ids.resolve(known, published_now, {}, ids.load_aliases(args.aliases))
    if args.only or args.limit:
        orphans = []
    for old, new in aliases.items():
        src_p = os.path.join(out, "spots", f"{new}.json")
        if os.path.exists(src_p):
            shutil.copyfile(src_p, os.path.join(out, "spots", f"{old}.json"))
    write(os.path.join(out, "index", "aliases.json"), {"v": config.SCHEMA_VERSION, "aliases": aliases})
    if orphans:
        log.error("%d published ids vanished without alias: %s", len(orphans), orphans[:20])
    if args.update_ids and not (args.only or args.limit):
        ids.save(known | published_now, aliases, args.published, args.aliases)

    t = time.time()
    for tile, entries in tiles.items():
        entries.sort(key=lambda e: e["id"])
        write(os.path.join(out, "index", f"{tile}.json"), {"v": config.SCHEMA_VERSION, "spots": entries})
    for z, cands in zone_cands.items():
        write(os.path.join(out, "zones", f"{z}.json"), products.zone_doc(z, cands, generated))
    ramp_tiles = ramps.tiles(ramps.load(args.ramps))
    for tile, doc in ramp_tiles.items():
        write(os.path.join(out, "ramps", f"{tile}.json"), doc)
    znames = load_zone_names().get("zones", {})
    zone_list = [{"id": z, "name": znames.get(z, {}), "bbox": [round(v, 3) for v in zone_bbox[z]]}
                 for z in sorted(zone_bbox)]
    run = {
        "sources": {st.name: st.as_json() for st in statuses},
        "spots": {"total": len(spots), "fresh": fresh, "stale": stale, "dropped": dropped},
        "stale": stale_ids[:50],
        "ids": {"aliases": len(aliases), "orphans": orphans[:50]},
    }
    meta = {"v": config.SCHEMA_VERSION, "generated": generated, "tiles": sorted(tiles), "zones": zone_list,
            "regions": regions_meta(regions_used), "status": meta_status(statuses),
            "attribution": config.ATTRIBUTION, "ramps": sorted(ramp_tiles), "run": run}
    write(os.path.join(out, "index", "meta.json"), meta)
    timings["index"] = time.time() - t
    timings["total"] = time.time() - t_start
    log.info("published %d fresh, %d stale, %d dropped; timings %s", fresh, stale, dropped,
             {k: round(v, 1) for k, v in timings.items()})
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w") as f:
            json.dump({"generated": generated, "timings": timings, **run}, f, indent=1)
    if fresh == 0:
        return 2
    return 3 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
