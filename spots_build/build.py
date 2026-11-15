"""Build the Fishers Hour spot catalogue: data/spots/sea.json, data/spots/fresh.json, data/spots/zones.json.

Inputs (CACHE = $FISHERSHOUR_SPOTS_CACHE, default ~/.cache/ardora):
  osm/*.json            extract_osm.py output (Geofabrik ES, Canarias, PT, FR, MA; ODbL)
  land.pkl              land.py output (OSM land polygons; ODbL)
  ext/miteco_embalses.geojson   MITECO Boletín Hidrológico reservoirs (ArcGIS Embalses_Mapa)
  ext/cyl_tramos.geojson        Junta de Castilla y León fishing stretches (IDECyL WFS)
  ext/hsurf.grib2.bz2           DWD ICON-EU model orography (salmonid heuristic)

Usage: python -I spots_build/build.py
"""
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import heuristics as H  # noqa: E402
from geo import (EXTRACT_CC, Admin, Elevation, Places, display, excluded, km, load_extracts,  # noqa: E402
                 tz_of)
from land import LAND_PKL, Land  # noqa: E402
from naming import is_latin, latin_prefix, slugify, title_case  # noqa: E402

LANG_PREF = {
    "ES": ["name:es", "name", "name:gl", "name:eu", "name:ca", "name:en"],
    "PT": ["name", "name:pt", "name:en"],
    "FR": ["name", "name:fr", "name:co", "name:br", "name:oc", "name:en"],
    "MA": ["name:fr", "name:es", "name:en", "name"],
}
# ES: regional official names first where they are the local usage
LOCAL_FIRST = {"ES-GA": "name:gl", "ES-PV": "name:eu", "ES-CT": "name:ca", "ES-IB": "name:ca", "ES-VC": "name"}

PIER_WORDS = re.compile(r"\b(espig[oó]n|muelle|dique|escollera|malec[oó]n|pantal[aá]n|moll|esculler|"
                        r"jet[ée]e|digue|[ée]pi|ponton|wharf|pier|molhe|pared[aã]o|cais|ponte-cais)\b", re.I)
BEACH_WORDS = re.compile(r"\b(playa|platja|praia|plage|hondartza|arenal|cala)\b", re.I)
BAD_WATER = re.compile(r"\b(salin|salina|marais|bassin|balsa|depuradora|decantaci|lagune|lagoa costeira|"
                       r"golf|piscine|evaporaci|stockage|r[ée]serve incendie|bac|gravi[eè]re priv)\w*", re.I)


def pick_name(tags, cc, iso=None):
    pref = list(LANG_PREF[cc])
    if iso in LOCAL_FIRST:
        pref.insert(0, LOCAL_FIRST[iso])
    names = []
    for k in pref + ["alt_name", "loc_name", "official_name"]:
        v = (tags.get(k) or "").strip()
        if v and is_latin(v) and v not in names:
            names.append(v)
    if not names:
        lp = latin_prefix(tags.get("name") or "")
        if lp and len(lp) >= 3:
            return lp, []
        return None, []
    n = names[0]
    if n.isupper() and len(n) > 4:
        n = title_case(n)
    return n, [a for a in names[1:4] if a != n]


# ------------------------------------------------------------------ helpers
class PointIndex:
    """Grid index over accepted points (lat, lon) for dedupe."""

    def __init__(self, cell_km=2.0):
        self.cell = cell_km / 111.0
        self.g = defaultdict(list)

    def _k(self, lat, lon):
        return int(lat // self.cell), int(lon // self.cell)

    def add(self, lat, lon, obj):
        self.g[self._k(lat, lon)].append((lat, lon, obj))

    def near(self, lat, lon, radius_m):
        ci, cj = self._k(lat, lon)
        r = int(math.ceil(radius_m / 1000 / (self.cell * 111))) + 1
        out = []
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for la, lo, o in self.g.get((i, j), ()):
                    d = km(lat, lon, la, lo) * 1000
                    if d <= radius_m:
                        out.append((d, o))
        return sorted(out, key=lambda x: x[0])


def dedupe(cands, radius_m):
    """Greedy by priority; losers' names become aliases, sources merged."""
    cands = sorted(cands, key=lambda c: (c["prio"], -len(c.get("name") or ""), c["lat"], c["lon"]))
    idx = PointIndex()
    kept = []
    for c in cands:
        near = idx.near(c["lat"], c["lon"], radius_m)
        near = [o for _, o in near if o["kind"] == c["kind"] or radius_m <= 600]
        if near:
            w = near[0]
            if c.get("name") and c["name"] != w["name"] and c["name"] not in w["aliases"] and len(w["aliases"]) < 3 \
                    and not c.get("synthetic"):
                w["aliases"].append(c["name"])
            for s in c["sources"]:
                if s not in w["sources"] and len(w["sources"]) < 4:
                    w["sources"].append(s)
            for k, v in c.get("x", {}).items():
                w["x"].setdefault(k, v)
            continue
        idx.add(c["lat"], c["lon"], c)
        kept.append(c)
    return kept


def src(f):
    return f"osm:{ {'n': 'node', 'w': 'way', 'r': 'relation'}[f['t']] }/{f['id']}"


def of_place(cc, place, kind):
    """'Espigón de X' / 'Digue de X' / 'Molhe de X' with articles handled."""
    words = {"ES": {"pier": "Espigón de", "fishing": "Pesquero de"},
             "PT": {"pier": "Molhe de", "fishing": "Pesqueiro de"},
             "FR": {"pier": "Digue de", "fishing": "Coin de pêche de"},
             "MA": {"pier": "Digue de", "fishing": "Coin de pêche de"}}[cc][kind]
    p = place.strip()
    if cc in ("FR", "MA"):
        for art, rep in (("Le ", "du "), ("Les ", "des "), ("La ", "de la "), ("L'", "de l'")):
            if p.startswith(art):
                return words[:-3] + " " + rep + p[len(art):]
        if p[:1].lower() in "aeiouyéèêâîôûh":
            return words[:-3] + " d'" + p
    if cc == "ES" and p.startswith("El "):
        return words[:-3] + " del " + p[3:]
    if cc == "PT":
        for art, rep in (("O ", "do "), ("A ", "da "), ("Os ", "dos "), ("As ", "das ")):
            if p.startswith(art):
                return words[:-3] + " " + rep + p[len(art):]
    return f"{words} {p}"


def mouth_name(cc, river):
    r = river.strip()
    if cc == "ES":
        m = re.match(r"^(r[ií]o|riu|r[ií]o)\s+(.*)$", r, re.I)
        return f"Desembocadura del río {m.group(2)}" if m else f"Desembocadura de {r}"
    if cc == "PT":
        if r.lower().startswith("rio "):
            rest = r[4:]
            if re.match(r"(?i)(da|do|de|das|dos)\s", rest):
                return "Foz do Rio " + rest
            return "Foz do " + rest
        if r.lower().startswith("ribeira"):
            return "Foz da " + r
        return "Foz de " + r
    for art, rep in (("Le ", "du "), ("Les ", "des "), ("La ", "de la "), ("L'", "de l'")):
        if r.startswith(art):
            return "Embouchure " + rep + r[len(art):]
    if r[:1].lower() in "aeiouyéèêâîôûh":
        return "Embouchure de l'" + r
    return "Embouchure de " + r


def clean_river(cc, river):
    """'Rhin / Rhein' -> 'Rhin'; 'Río Miño / Rio Minho' -> the part in the spot's language."""
    parts = [p.strip() for p in river.split(" / ") if p.strip()]
    if len(parts) > 1:
        pref = {"ES": "río", "PT": "rio"}.get(cc)
        for p in parts:
            if pref and p.lower().startswith(pref + " "):
                return p
        return parts[0]
    return river.strip()


def place_name(p, cc):
    nm = p["name"]
    if cc == "MA":
        nm = (p.get("names") or {}).get("name:fr") or nm
    if not is_latin(nm):
        nm = latin_prefix(nm) or nm
    return nm


def river_at(cc, river, place):
    r = clean_river(cc, river)
    if cc == "ES":
        return f"{r} en {place}"
    if cc == "PT":
        return f"{r} em {place}"
    r = re.sub(r"^(La|Le|Les|L')\s*", "", r) if len(r) > 4 else r
    return f"{r[0].upper() + r[1:]} à {place}"


# ------------------------------------------------------------------ sea candidates
def sea_candidates(ex, land, places):
    t0 = time.time()
    seen = set()
    raw = []
    for name, d in ex.items():
        cc0 = EXTRACT_CC[name]
        for f in d["feats"]:
            k = f["k"]
            if k not in ("fishing", "pier", "beach", "cape", "harbour"):
                continue
            key = (f["t"], f["id"])
            if key in seen:
                continue
            seen.add(key)
            lat, lon = f["lat"], f["lon"]
            if k == "pier" and f.get("ends"):
                raw.append((f, cc0, f["ends"]))
            else:
                raw.append((f, cc0, None))
    # coast distance for all candidate points (piers: both ends)
    pts = []
    for f, _cc, ends in raw:
        pts.extend(ends if ends else [[f["lat"], f["lon"]]])
    lats = np.array([p[0] for p in pts])
    lons = np.array([p[1] for p in pts])
    dist = land.coast_distance_m(lats, lons)
    onl = land.on_land(lats, lons)
    out = []
    i = 0
    for f, cc0, ends in raw:
        if ends:
            d0, d1 = dist[i], dist[i + 1]
            l0, l1 = onl[i], onl[i + 1]
            i += 2
            # seaward end: the one in water, else the one farther from the coast
            j = 1 if (not l1 and l0) or (l0 == l1 and d1 > d0) else 0
            lat, lon = ends[j]
            dc = min(d0, d1)
        else:
            lat, lon, dc = f["lat"], f["lon"], dist[i]
            i += 1
        k = f["k"]
        if dc > config.SEA_MAX_COAST_M[k] or excluded(lat, lon):
            continue
        if cc0 == "MA" and lon > config.MA_MAX_LON:
            continue
        tags = f["tags"]
        name, aliases = pick_name(tags, cc0)
        c = {"lat": round(lat, 5), "lon": round(lon, 5), "kind": "sea", "name": name, "aliases": aliases,
             "sources": [src(f)], "cc0": cc0, "x": {}}
        if k == "fishing":
            if tags.get("access") == "private":
                continue
            typ = "pier" if name and PIER_WORDS.search(name) else "beach" if name and BEACH_WORDS.search(name) \
                else "rock"
            c.update(type=typ, prio=0 if name else 5)
            if not name:
                c["need_name"] = "fishing"
        elif k == "pier":
            ln = f.get("len_km") or 0
            if name and re.fullmatch(r"(?i)(épi|epi|groyne|espigón|espigon)?\s*(n[°o]\s*)?\d+[a-z]?", name):
                name = None
            if name:
                c.update(name=name, type="pier", prio=1)
            else:
                if ln < 0.3 or tags.get("man_made") == "groyne":
                    continue
                c.update(type="pier", prio=6, need_name="pier", length=ln)
        elif k == "beach":
            c.update(type="beach", prio=2)
        elif k == "harbour":
            c.update(type="harbour", prio=3)
        elif k == "cape":
            c.update(type="rock", prio=4)
        out.append(c)
    # name the unnamed ones after the nearest settlement; thin out unnamed piers
    idx = PointIndex()
    final = []
    for c in sorted(out, key=lambda c: (c["prio"], -c.get("length", 0))):
        if c.get("need_name"):
            if c["need_name"] == "pier" and idx.near(c["lat"], c["lon"], config.UNNAMED_PIER_SPACING_M):
                continue
            p, _d = places.best(c["lat"], c["lon"], 6.0, ("city", "town", "village", "suburb"))
            if p is None:
                continue
            c["name"] = of_place(c["cc0"], place_name(p, c["cc0"]), c["need_name"])
            c["synthetic"] = True
        if c["type"] == "pier":
            idx.add(c["lat"], c["lon"], c)
        final.append(c)
    print(f"sea candidates: {len(final)} ({time.time() - t0:.0f}s)", flush=True)
    return final


# ------------------------------------------------------------------ rivers
def wavespot_layer1():
    """Wavespot layer-1 points (OSM/EEA/Wikidata, ODbL) as low-priority sea candidates."""
    p = os.path.join(config.REPO, "data", "inputs", "wavespot_layer1.json")
    if not os.path.exists(p):
        return []
    out = []
    for w in json.load(open(p)):
        kind = w.get("kind")
        if kind == "synthetic" and w["country"] != "MA":
            continue
        typ = {"beach": "beach", "bathing": "beach", "wikidata": "beach", "cape": "rock", "synthetic": "beach",
               "bay": "beach", "locality": "rock"}.get(kind)
        if typ is None or "kite" in (w["info"].get("sports") or []) and w["info"].get("window") is None:
            continue
        out.append({"lat": w["lat"], "lon": w["lon"], "kind": "sea", "type": typ, "name": w["name"],
                    "aliases": [], "sources": list(w.get("sources") or [f"wavespot:{w['id']}"])[:2],
                    "cc0": w["country"], "prio": 7 if kind != "synthetic" else 8, "x": {}})
    print(f"wavespot layer-1 candidates: {len(out)}", flush=True)
    return out


def river_components(ex):
    """Named waterway=river ways grouped by (extract, name) and connectivity."""
    comps = []
    for name, d in ex.items():
        by_name = defaultdict(list)
        for r in d["rivers"]:
            by_name[r["name"]].append(r)
        for rname, ways in by_name.items():
            parent = list(range(len(ways)))

            def find(a):
                while parent[a] != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a

            node_owner = {}
            for i, w in enumerate(ways):
                for nd in w["nodes"]:
                    if nd in node_owner:
                        parent[find(i)] = find(node_owner[nd])
                    else:
                        node_owner[nd] = i
            groups = defaultdict(list)
            for i in range(len(ways)):
                groups[find(i)].append(ways[i])
            for g in groups.values():
                ln = sum(w["len_km"] for w in g)
                deg = Counter(nd for w in g for nd in w["nodes"])
                ends = []
                for w in g:
                    if deg[w["nodes"][0]] == 1:
                        ends.append(w["c"][0])
                    if deg[w["nodes"][-1]] == 1:
                        ends.append(w["c"][-1])
                comps.append({"name": rname, "extract": name, "len_km": ln, "ways": g, "ends": ends,
                              "wikidata": next((w["tags"].get("wikidata") for w in g if w["tags"].get("wikidata")),
                                               None)})
    # merge same-name components across extracts (border rivers) by wikidata
    print(f"river components: {len(comps)}; >= {config.RIVER_MIN_KM} km: "
          f"{sum(1 for c in comps if c['len_km'] >= config.RIVER_MIN_KM)}", flush=True)
    return comps


def rivermouths(comps, land, places):
    out = []
    cands = [(c, e) for c in comps if c["len_km"] >= 8 for e in c["ends"]]
    if not cands:
        return out
    lats = np.array([e[0] for _, e in cands])
    lons = np.array([e[1] for _, e in cands])
    dist = land.coast_distance_m(lats, lons)
    best = {}
    for (c, e), d in zip(cands, dist):
        if d > config.SEA_MAX_COAST_M["rivermouth"]:
            continue
        k = id(c)
        if k not in best or d < best[k][1]:
            best[k] = (c, d, e)
    for c, d, e in best.values():
        cc = EXTRACT_CC[c["extract"]]
        if cc == "MA" and e[1] > config.MA_MAX_LON:
            continue
        rname = c["name"] if is_latin(c["name"]) else latin_prefix(c["name"])
        if not rname:
            continue
        if excluded(e[0], e[1]):
            continue
        out.append({"lat": round(e[0], 5), "lon": round(e[1], 5), "kind": "sea", "type": "rivermouth",
                    "name": mouth_name(cc, clean_river(cc, rname)), "aliases": [], "sources": [f"osm:way/{c['ways'][0]['id']}"],
                    "cc0": cc, "prio": 2, "x": {"river": c["name"], "riverKm": round(c["len_km"])}})
    print(f"river mouths: {len(out)}", flush=True)
    return out


# ------------------------------------------------------------------ orientation (multiprocessing)
_OR = None


def _init_orienter():
    global _OR
    from orientation import Orienter
    _OR = Orienter(Land(LAND_PKL))


def _orient(p):
    try:
        return _OR.orient(p[0], p[1])
    except Exception:
        return None


def orient_all(spots):
    t0 = time.time()
    with Pool(max(2, os.cpu_count() - 1), initializer=_init_orienter) as pool:
        res = pool.map(_orient, [(s["lat"], s["lon"]) for s in spots], chunksize=64)
    for s, r in zip(spots, res):
        s["facing"] = r.get("facing") if r else None
        s["x"]["window"] = r.get("window") if r else None
        s["x"]["width"] = r.get("width") if r else 0
    print(f"orientation: {len(spots)} spots in {time.time() - t0:.0f}s", flush=True)


# ------------------------------------------------------------------ freshwater
def water_bodies(ex, land):
    out = []
    seen = set()
    for name, d in ex.items():
        cc0 = EXTRACT_CC[name]
        if cc0 == "MA" or name == "canary-islands":
            continue
        for f in d["feats"]:
            if f["k"] != "water" or "ring" not in f:
                continue
            t = f["tags"]
            km2 = f.get("km2") or 0
            is_res = t.get("water") == "reservoir" or t.get("landuse") == "reservoir"
            is_lake = t.get("water") in ("lake", None) and t.get("natural") == "water"
            if t.get("water") == "lagoon":
                continue
            if not ((is_res and km2 >= config.RESERVOIR_MIN_KM2) or (is_lake and km2 >= config.LAKE_MIN_KM2)):
                continue
            if t.get("salt") == "yes" or t.get("intermittent") == "yes" or t.get("tidal") == "yes":
                continue
            nm, aliases = pick_name(t, cc0)
            if not nm or BAD_WATER.search(nm):
                continue
            key = (f["t"], f["id"])
            if key in seen:
                continue
            seen.add(key)
            ring = [(lo, la) for la, lo in f["ring"]]
            try:
                poly = shapely.make_valid(Polygon(ring))
                rp = poly.representative_point()
            except Exception:
                continue
            out.append({"lat": round(rp.y, 5), "lon": round(rp.x, 5), "kind": "reservoir" if is_res else "lake",
                        "name": nm, "aliases": aliases, "sources": [src(f)], "cc0": cc0, "prio": 2,
                        "x": {"km2": round(km2, 2)}, "poly": poly})
    if out:
        onl = land.on_land(np.array([w["lat"] for w in out]), np.array([w["lon"] for w in out]))
        dist = land.coast_distance_m(np.array([w["lat"] for w in out]), np.array([w["lon"] for w in out]))
        out = [w for w, ok, dc in zip(out, onl, dist) if ok and dc > 1500]
    print(f"reservoirs/lakes: {Counter(w['kind'] for w in out)}", flush=True)
    return out


def miteco_join(waters):
    p = os.path.join(config.CACHE, "ext", "miteco_embalses.geojson")
    if not os.path.exists(p):
        return []
    feats = json.load(open(p))["features"]
    es = [w for w in waters if w["kind"] == "reservoir"]
    tree = shapely.STRtree([w["poly"] for w in es]) if es else None
    extra = []
    matched = 0
    for f in feats:
        pr = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        mid, mname = pr["EMBALSE_ID"], pr["embalse_nombre"].strip()
        tok = slugify(mname)
        best = None
        if tree is not None:
            pt = Point(lon, lat)
            for i in tree.query(pt.buffer(0.04)):
                w = es[int(i)]
                d = w["poly"].distance(pt) * 111
                nm_ok = tok and (tok in slugify(w["name"]) or any(tok in slugify(a) for a in w["aliases"]))
                score = d - (5 if nm_ok else 0)
                if (nm_ok and d < 4) or d < 0.3:
                    if best is None or score < best[0]:
                        best = (score, w)
        if best:
            w = best[1]
            if "miteco" not in w["x"]:
                w["x"]["miteco"] = mid
                w["sources"].append(f"miteco:{mid}")
                matched += 1
            continue
        extra.append({"lat": round(lat, 5), "lon": round(lon, 5), "kind": "reservoir",
                      "name": f"Embalse de {mname}", "aliases": [], "sources": [f"miteco:{mid}"], "cc0": "ES",
                      "prio": 2, "x": {"miteco": mid, "km2": None}})
    print(f"MITECO reservoirs: {len(feats)}; matched to OSM {matched}; added {len(extra)}", flush=True)
    return extra


def river_samples(comps, land, water_tree, waters):
    out = []
    step = config.RIVER_SAMPLE_KM
    for c in comps:
        if c["len_km"] < config.RIVER_MIN_KM:
            continue
        cc0 = EXTRACT_CC[c["extract"]]
        if cc0 == "MA" or c["extract"] == "canary-islands":
            continue
        acc = 0.0
        nxt = step / 2
        for w in sorted(c["ways"], key=lambda w: (w["c"][0][0], w["c"][0][1])):
            pts = w["c"]
            for a, b in zip(pts, pts[1:]):
                seg = km(a[0], a[1], b[0], b[1])
                while acc + seg >= nxt and seg > 0:
                    t = (nxt - acc) / seg
                    la, lo = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
                    out.append({"lat": round(la, 5), "lon": round(lo, 5), "kind": "river", "name": None,
                                "aliases": [], "sources": [f"osm:way/{w['id']}"], "cc0": cc0, "prio": 4,
                                "x": {"river": c["name"], "riverKm": round(c["len_km"])}})
                    nxt += step
                acc += seg
    if not out:
        return out
    lats = np.array([s["lat"] for s in out])
    lons = np.array([s["lon"] for s in out])
    dist = land.coast_distance_m(lats, lons)
    onl = land.on_land(lats, lons)
    keep = []
    for s, dc, ok in zip(out, dist, onl):
        if not ok or dc < 6000 or excluded(s["lat"], s["lon"]):
            continue
        hits = water_tree.query(Point(s["lon"], s["lat"]).buffer(0.01)) if water_tree is not None else []
        if len(hits):
            continue
        keep.append(s)
    print(f"river samples: {len(out)} -> {len(keep)} after coast/reservoir filter", flush=True)
    return keep


def inland_fishing(ex, land, river_tree):
    out = []
    seen = set()
    for name, d in ex.items():
        cc0 = EXTRACT_CC[name]
        if cc0 == "MA" or name == "canary-islands":
            continue
        for f in d["feats"]:
            if f["k"] != "fishing" or (f["t"], f["id"]) in seen:
                continue
            seen.add((f["t"], f["id"]))
            t = f["tags"]
            if t.get("access") in ("private", "no", "customers", "members"):
                continue
            nm, aliases = pick_name(t, cc0)
            if not nm:
                continue
            out.append({"lat": round(f["lat"], 5), "lon": round(f["lon"], 5), "kind": "lake", "name": nm,
                        "aliases": aliases, "sources": [src(f)], "cc0": cc0, "prio": 1, "x": {},
                        "km2": f.get("km2")})
    if not out:
        return out
    lats = np.array([s["lat"] for s in out])
    lons = np.array([s["lon"] for s in out])
    dist = land.coast_distance_m(lats, lons)
    onl = land.on_land(lats, lons)
    keep = []
    for s, dc, ok in zip(out, dist, onl):
        if not ok or dc < 2500:
            continue
        p = Point(s["lon"], s["lat"])
        near = river_tree["tree"].query(p.buffer(0.004)) if river_tree else []
        if len(near):
            s["kind"] = "river"
            s["x"]["river"] = river_tree["names"][int(near[0])]
        elif (s.pop("km2", None) or 0) < 0.03 and re.search(r"(?i)étang|etang|plan d'eau|lac|lago|laguna|charca",
                                                            s["name"]) is None:
            # small unnamed-type pond/'parcours' far from a mapped river: keep as river stretch only if named so
            if not re.search(r"(?i)parcours|coto|tramo|rivi[eè]re|r[ií]o|ruisseau", s["name"]):
                continue
            s["kind"] = "river"
        keep.append(s)
    print(f"inland fishing: {len(out)} -> {len(keep)}", flush=True)
    return keep


def cyl_tramos():
    p = os.path.join(config.CACHE, "ext", "cyl_tramos.geojson")
    if not os.path.exists(p):
        return []
    feats = json.load(open(p))["features"]
    want = {"Coto de pesca", "Escenario Deportivo Social", "AREC", "ARE"}
    out = []
    for f in feats:
        pr = f["properties"]
        if pr.get("categoria") not in want or not f.get("geometry"):
            continue
        g = shapely.geometry.shape(f["geometry"])
        if g.is_empty:
            continue
        if g.geom_type == "MultiLineString":
            g = max(g.geoms, key=lambda x: x.length)
        mid = g.interpolate(0.5, normalized=True)
        nm = (pr.get("nombr_tram") or "").strip()
        if not nm:
            continue
        nm = title_case(nm) if nm.isupper() else nm
        cat = pr["categoria"]
        if cat == "Coto de pesca" and not nm.lower().startswith("coto"):
            nm = f"Coto {nm}"
        elif cat == "Escenario Deportivo Social" and not nm.lower().startswith("esc"):
            nm = f"Escenario de pesca {nm}"
        trout = str(pr.get("truchera") or "").strip().lower() in ("si", "sí", "s", "1", "true", "trucha")
        out.append({"lat": round(mid.y, 5), "lon": round(mid.x, 5), "kind": "river", "name": nm, "aliases": [],
                    "sources": [f"jcyl:{pr.get('codigo')}"], "cc0": "ES", "prio": 0,
                    "x": {"river": pr.get("rio_mas_a"), "salmonid": trout, "cyl": cat,
                          "species": pr.get("esp_princ")}})
    print(f"CyL stretches: {len(out)}", flush=True)
    return out


# ------------------------------------------------------------------ ids, regions, zones
def load_previous():
    prev = []
    for f in ("sea.json", "fresh.json"):
        p = os.path.join(config.OUT_DIR, f)
        if os.path.exists(p):
            prev.extend(json.load(open(p))["spots"])
    return prev


def assign_ids(spots, prev):
    by_src = {}
    for p in prev:
        for s in p["info"].get("sources", []):
            by_src.setdefault(s, p)
    pidx = PointIndex()
    for p in prev:
        pidx.add(p["lat"], p["lon"], p)
    used = set()
    # 1) keep previous ids (same first source, or <=150 m and same kind)
    for s in spots:
        p = by_src.get(s["sources"][0])
        if p is None or p["kind"] != s["kind"]:
            near = [o for d, o in pidx.near(s["lat"], s["lon"], 150) if o["kind"] == s["kind"]]
            p = near[0] if near else None
        if p is not None and p["id"] not in used and p["id"].startswith(s["country"].lower() + "-"):
            s["id"] = p["id"]
            used.add(p["id"])
    # 2) new ids
    for s in sorted(spots, key=lambda s: (s["prio"], s["lat"], s["lon"])):
        if s.get("id"):
            continue
        base = f"{s['country'].lower()}-{slugify(s['name'])[:60].strip('-')}"
        cid, n = base, 2
        while cid in used:
            cid = f"{base}-{n}"
            n += 1
        s["id"] = cid
        used.add(cid)


CANARY_ISLANDS = [("lanzarote", "Lanzarote", (28.8, -14.0, 29.5, -13.3)),
                  ("fuerteventura", "Fuerteventura", (27.9, -14.6, 28.8, -13.8)),
                  ("gran-canaria", "Gran Canaria", (27.7, -15.9, 28.2, -15.3)),
                  ("tenerife", "Tenerife", (27.9, -16.95, 28.65, -16.05)),
                  ("la-gomera", "La Gomera", (27.95, -17.4, 28.25, -17.05)),
                  ("la-palma", "La Palma", (28.4, -18.05, 28.9, -17.65)),
                  ("el-hierro", "El Hierro", (27.6, -18.2, 27.9, -17.85))]
BALEARES = [("mallorca", "Mallorca", (39.2, 2.2, 40.0, 3.6)), ("menorca", "Menorca", (39.75, 3.75, 40.15, 4.4)),
            ("ibiza-formentera", "Ibiza y Formentera", (38.6, 1.1, 39.15, 1.65))]
MA_ZONES = [("ma-tanger-kenitra", "Tánger y Kenitra", "Tanger et Kénitra", 34.2),
            ("ma-rabat-casablanca", "Rabat y Casablanca", "Rabat et Casablanca", 33.0),
            ("ma-el-jadida-safi", "El Jadida y Safi", "El Jadida et Safi", 31.7),
            ("ma-essaouira-agadir", "Esauira y Agadir", "Essaouira et Agadir", 30.0),
            ("ma-sud", "Sidi Ifni a Tarfaya", "Sidi Ifni à Tarfaya", -90)]


def zone_names(place_es, place_fr, place_pt, place_en, sea):
    if sea:
        return {"es": f"Costa de {place_es}", "en": f"{place_en} coast", "fr": f"Côte : {place_fr}",
                "pt": f"Costa de {place_pt}"}
    return {"es": f"Ríos y embalses de {place_es}", "en": f"{place_en} rivers and lakes",
            "fr": f"Rivières et lacs : {place_fr}", "pt": f"Rios e albufeiras de {place_pt}"}


def locate_all(spots, admin, regions_out, zones_out):
    for s in spots:
        cc, r4, r6 = admin.locate(s["lat"], s["lon"], s["cc0"])
        if cc is None:
            cc = s["cc0"]
        s["country"] = cc
        sea = s["kind"] == "sea"
        iso4 = r4["iso"] if r4 else None
        s["x"]["iso4"] = iso4
        # ---- region (rules)
        if cc == "ES":
            if s["lat"] < 30:
                iso4 = "ES-CN"
            code = (iso4 or "ES-AN")[3:].lower()
            region = f"es-{code}" if sea else f"es-{code}-cont"
        elif cc == "FR":
            if sea:
                region = f"fr-{(iso4 or 'FR-BRE')[3:].lower()}".replace("fr-20r", "fr-cor")
            else:
                ref = (r6 or {}).get("ref") or ((r6 or {}).get("iso") or "FR-xx")[3:]
                ref = re.sub(r"^69[DMdm]$", "69", ref)          # Rhône + Métropole de Lyon: one federation
                region = f"fr-d-{ref.lower()}"
        elif cc == "PT":
            if s["lon"] < -24:
                region = "pt-ac"
            elif s["lon"] < -15:
                region = "pt-md"
            else:
                region = "pt" if sea else "pt-cont"
        else:
            region = "ma"
        s["region"] = region
        regions_out.setdefault(region, {"cc": cc, "sea": sea, "r4": r4, "r6": r6 if cc == "FR" and not sea else None})
        # ---- zone (listing)
        zid, zn = None, None
        if cc == "ES" and s["lat"] < 30:
            for slug_, nm, (a, b, c, d) in CANARY_ISLANDS:
                if a <= s["lat"] <= c and b <= s["lon"] <= d:
                    zid, zn = f"es-{slug_}", (nm, nm, nm, nm)
                    break
            zid = zid or "es-canarias"
            zn = zn or ("Canarias", "Canaries", "Canárias", "Canary Islands")
        elif cc == "ES" and iso4 == "ES-IB":
            for slug_, nm, (a, b, c, d) in BALEARES:
                if a <= s["lat"] <= c and b <= s["lon"] <= d:
                    zid, zn = f"es-{slug_}", (nm, nm, nm, nm)
                    break
            zid = zid or "es-baleares"
            zn = zn or ("Baleares", "Baléares", "Baleares", "Balearic Islands")
        elif cc == "PT" and s["lon"] < -24:
            zid, zn = "pt-acores", ("Azores", "Açores", "Açores", "Azores")
        elif cc == "PT" and s["lon"] < -15:
            zid, zn = "pt-madeira", ("Madeira", "Madère", "Madeira", "Madeira")
        elif cc == "MA":
            for zid_, es_, fr_, lat_min in MA_ZONES:
                if s["lat"] >= lat_min:
                    zid, zn = zid_, (es_, fr_, fr_, fr_)
                    break
        elif r6 is not None:
            nm_es = display(r6["name"], r6["names"], "es")
            nm_fr = display(r6["name"], r6["names"], "fr")
            local = r6["name"] if " / " not in (r6["name"] or "") else nm_es
            zid = f"{cc.lower()}-{slugify(local)}"
            zn = (nm_es, nm_fr, display(r6["name"], r6["names"], "pt"), display(r6["name"], r6["names"], "en"))
        else:
            zid, zn = f"{cc.lower()}-otros", ("otras zonas", "autres", "outras", "other")
        zid = f"{zid}-{'sea' if sea else 'fresh'}"
        s["zone"] = zid
        if zid not in zones_out:
            zones_out[zid] = zone_names(zn[0], zn[1], zn[2], zn[3], sea)
        s["tz"] = tz_of(cc, s["lat"], s["lon"])


def finalize(s, elev):
    cc = s["country"]
    if s["kind"] == "sea":
        typ = s["type"]
        if typ == "rivermouth" and (s["x"].get("width") or 0) < 60:
            typ = "estuary"
        s["type"] = typ
        clim = H.climate(cc, s["lat"], s["lon"])
        modes = list(H.SEA_MODES[typ])
        info = {"facing": s.get("facing"), "depth": None, "bottom": H.SEA_BOTTOM[typ],
                "species": H.sea_species(clim, typ, s["lat"], cc), "sources": s["sources"]}
        x = {"clim": clim, "window": s["x"].get("window"), "med": H.is_med(s["lat"], s["lon"])}
    else:
        e = elev.at(s["lat"], s["lon"])
        sal = s["x"].get("salmonid")
        if sal is None:
            sal = H.salmonid(cc, s["x"].get("iso4"), s["kind"], e, s["lat"])
        s["type"] = s["kind"]
        modes = H.fresh_modes(s["kind"], sal)
        info = {"facing": None, "depth": None, "bottom": None,
                "species": H.fresh_species(cc, s["kind"], sal, s["lat"], s["lon"]), "sources": s["sources"]}
        if s["x"].get("river"):
            info["river"] = s["x"]["river"]
        x = {"elev": None if e is None else round(e), "salmonid": bool(sal)}
        for k in ("miteco", "river", "km2", "cyl"):
            if s["x"].get(k) is not None:
                x[k] = s["x"][k]
    return {"id": s["id"], "name": s["name"], "aliases": s["aliases"][:3], "lat": s["lat"], "lon": s["lon"],
            "country": cc, "zone": s["zone"], "region": s["region"], "tz": s["tz"], "kind": s["kind"],
            "type": s["type"], "modes": modes, "info": info, "x": x}


def main():
    t0 = time.time()
    ex = load_extracts()
    admin = Admin(ex)
    places = Places(ex)
    elev = Elevation()
    land = Land(LAND_PKL)
    print(f"loaded ({time.time() - t0:.0f}s)", flush=True)

    comps = river_components(ex)
    sea = sea_candidates(ex, land, places) + rivermouths(comps, land, places) + wavespot_layer1()
    sea = dedupe(sea, config.SEA_DEDUPE_M)
    print(f"sea after dedupe: {len(sea)} {Counter(s['type'] for s in sea)}", flush=True)

    waters = water_bodies(ex, land)
    extra_res = miteco_join(waters)
    water_tree = shapely.STRtree([w["poly"] for w in waters]) if waters else None
    named_rivers = [(c["name"], w) for c in comps for w in c["ways"]]
    river_tree = {"tree": shapely.STRtree([LineString([(b, a) for a, b in w["c"]]) if len(w["c"]) > 1
                                           else Point(w["c"][0][1], w["c"][0][0]) for _, w in named_rivers]),
                  "names": [n for n, _ in named_rivers]}
    fresh = cyl_tramos() + inland_fishing(ex, land, river_tree) + waters + extra_res + \
        river_samples(comps, land, water_tree, waters)
    for w in fresh:
        w.pop("poly", None)
    # reservoirs/lakes win over river samples nearby; dedupe within kind at FRESH_DEDUPE_M
    fresh = dedupe(fresh, config.FRESH_DEDUPE_M)
    print(f"fresh after dedupe: {len(fresh)} {Counter(s['kind'] for s in fresh)}", flush=True)

    regions, zones = {}, {}
    locate_all(sea, admin, regions, zones)
    locate_all(fresh, admin, regions, zones)
    # drop anything that fell outside the countries in scope
    sea = [s for s in sea if s["country"] in ("ES", "PT", "FR", "MA")]
    fresh = [s for s in fresh if s["country"] in ("ES", "PT", "FR")]
    # river samples: name after the nearest settlement
    for s in fresh:
        if s["name"] is None:
            p, _d = places.best(s["lat"], s["lon"], config.SNAP_PLACE_KM, ("city", "town", "village"))
            if p is None:
                p, _d = places.best(s["lat"], s["lon"], 10.0, ("city", "town", "village", "hamlet", "suburb"))
            s["name"] = river_at(s["country"], s["x"]["river"], place_name(p, s["country"])) if p \
                else clean_river(s["country"], s["x"]["river"])
    sea = [s for s in sea if s.get("name")]
    fresh = [s for s in fresh if s.get("name")]
    orient_all(sea)
    prev = load_previous()
    assign_ids(sea + fresh, prev)
    sea_out = sorted((finalize(s, elev) for s in sea), key=lambda s: s["id"])
    fresh_out = sorted((finalize(s, elev) for s in fresh), key=lambda s: s["id"])
    os.makedirs(config.OUT_DIR, exist_ok=True)
    gen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lic = ("Derived from © OpenStreetMap contributors (ODbL); MITECO (reservoirs); "
           "©Junta de Castilla y León (tramos de pesca, IDECyL, free at www.jcyl.es)")
    for name, data in (("sea.json", sea_out), ("fresh.json", fresh_out)):
        with open(os.path.join(config.OUT_DIR, name), "w") as f:
            json.dump({"generated": gen, "licence": lic, "spots": data}, f, ensure_ascii=False, separators=(",", ":"))
    region_meta = {}
    for rid, r in sorted(regions.items()):
        region_meta[rid] = {"cc": r["cc"], "sea": r["sea"],
                            "l4": None if not r["r4"] else {"iso": r["r4"]["iso"], "name": r["r4"]["name"],
                                                             "names": r["r4"]["names"]},
                            "l6": None if not r["r6"] else {"iso": r["r6"]["iso"], "name": r["r6"]["name"],
                                                             "names": r["r6"]["names"]}}
    with open(os.path.join(config.OUT_DIR, "zones.json"), "w") as f:
        json.dump({"zones": dict(sorted(zones.items())), "regions": region_meta}, f, ensure_ascii=False, indent=0)
    allsp = sea_out + fresh_out
    print("by kind/country:", Counter((s["kind"], s["country"]) for s in allsp))
    print("by type:", Counter(s["type"] for s in allsp))
    print(f"zones {len(zones)}, regions {len(regions)}; total {len(allsp)} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
