"""Extract fishing-spot candidates from Geofabrik .osm.pbf extracts (pyosmium, ODbL).

Writes CACHE/osm/{extract}.json with
  feats : [{"t": "n|w|r", "id", "lat", "lon", "tags", "km2"?, "len_km"?}]
          leisure=fishing, man_made=pier|breakwater|groyne, natural=beach|cape|bay,
          harbours/marinas, water=reservoir|lake (areas, with km2), landuse=reservoir
  rivers: [{"id", "name", "tags", "nodes": [first, last], "c": [[lat, lon], ...]}]
          named waterway=river|canal? (rivers only) ways, geometry simplified
  places: [{"lat", "lon", "name", "place", "pop", "ele"}]

Usage: python -I spots_build/extract_osm.py PBF_DIR [spain portugal france morocco]
"""
import json
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

import osmium  # noqa: E402

KEYS = ("boundary", "leisure", "man_made", "natural", "water", "waterway", "place", "harbour", "landuse",
        "seamark:type")
PLACES = {"city", "town", "village", "hamlet", "suburb", "locality"}
KEEP = ("name", "name:es", "name:fr", "name:pt", "name:en", "name:gl", "name:eu", "name:ca", "name:co",
        "name:br", "name:oc", "alt_name", "loc_name", "official_name", "natural", "water", "waterway", "leisure",
        "man_made", "harbour", "landuse", "place", "population", "ele", "wikidata", "fishing", "sport",
        "seamark:type", "surface", "intermittent", "access", "reservoir_type", "salt", "tidal")


def slim(tags):
    return {k: v for k, v in tags if k in KEEP}


def area_km2_ring(pts):
    """Shoelace area (km2) of a lat/lon ring, equirectangular at its mean latitude."""
    if len(pts) < 3:
        return 0.0
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx = 111.32 * math.cos(math.radians(lat0))
    s = 0.0
    for (a, b), (c, d) in zip(pts, pts[1:] + pts[:1]):
        s += (b * kx) * (c * 110.54) - (d * kx) * (a * 110.54)
    return abs(s) / 2.0


def kind_of(t):
    """Which feature family a tag set belongs to (None = not wanted)."""
    if t.get("leisure") == "fishing":
        return "fishing"
    if t.get("man_made") in ("pier", "breakwater", "groyne"):
        return "pier"
    nat = t.get("natural")
    if nat == "beach" and "name" in t:
        return "beach"
    if nat in ("cape", "bay") and "name" in t:
        return nat
    if (t.get("harbour") == "yes" or t.get("leisure") == "marina" or t.get("landuse") == "harbour"
            or t.get("seamark:type") == "harbour") and "name" in t:
        return "harbour"
    if t.get("water") in ("reservoir", "lake", "lagoon") or t.get("landuse") == "reservoir" \
            or (nat == "water" and "name" in t and "water" not in t):
        return "water"
    if t.get("place") in PLACES and "name" in t:
        return "place"
    if t.get("waterway") == "river" and "name" in t:
        return "river"
    return None


def in_scope(lat, lon):
    for _cc, (s, w, n, e) in config.SCOPE_BOXES:
        if s <= lat <= n and w <= lon <= e:
            return True
    return False


def run(path):
    feats, rivers, places, admin = [], [], [], []
    fp = (osmium.FileProcessor(path)
          .with_locations()
          .with_areas(osmium.filter.TagFilter(("water", "reservoir"), ("water", "lake"), ("water", "lagoon"),
                                              ("landuse", "reservoir"), ("natural", "water"),
                                              ("leisure", "fishing"), ("natural", "beach"),
                                              ("boundary", "administrative")))
          .with_filter(osmium.filter.KeyFilter(*KEYS)))
    for o in fp:
        t = o.tags
        if o.is_area() and t.get("boundary") == "administrative" and t.get("admin_level") in ("2", "4", "6"):
            a = _admin(o, t)
            if a:
                admin.append(a)
            continue
        k = kind_of(t)
        if k is None:
            continue
        if o.is_node():
            if k == "water" or k == "river":
                continue
            la, lo = o.location.lat, o.location.lon
            if not in_scope(la, lo):
                continue
            if k == "place":
                try:
                    pop = int(str(t.get("population", "0")).replace(" ", "").replace(".", "").replace(",", ""))
                except ValueError:
                    pop = 0
                try:
                    ele = float(str(t.get("ele", "")).replace(",", ".").split()[0]) if t.get("ele") else None
                except ValueError:
                    ele = None
                places.append({"lat": round(la, 5), "lon": round(lo, 5), "name": t.get("name"),
                               "place": t.get("place"), "pop": pop, "ele": ele,
                               "names": {kk: v for kk, v in t if kk.startswith("name:")
                                         and kk[5:] in ("es", "fr", "pt", "gl", "eu", "ca")}})
                continue
            feats.append({"t": "n", "id": o.id, "lat": round(la, 6), "lon": round(lo, 6), "k": k,
                          "tags": slim(t)})
        elif o.is_way():
            pts = [(nd.location.lat, nd.location.lon) for nd in o.nodes if nd.location.valid()]
            if not pts:
                continue
            closed = len(pts) > 2 and pts[0] == pts[-1]
            if k == "river":
                la, lo = pts[len(pts) // 2]
                if not in_scope(la, lo) and not in_scope(*pts[0]):
                    continue
                step = max(1, len(pts) // 60)
                coords = pts[::step]
                if coords[-1] != pts[-1]:
                    coords.append(pts[-1])
                ln = sum(_km(a, b) for a, b in zip(pts, pts[1:]))
                rivers.append({"id": o.id, "name": t.get("name"), "tags": slim(t),
                               "nodes": [o.nodes[0].ref, o.nodes[-1].ref], "len_km": round(ln, 3),
                               "c": [[round(a, 5), round(b, 5)] for a, b in coords]})
                continue
            if k in ("place", "water"):
                continue
            if closed:
                ring = pts[:-1]
                la = sum(p[0] for p in ring) / len(ring)
                lo = sum(p[1] for p in ring) / len(ring)
            else:
                # piers/breakwaters: keep the seaward end later; store both ends + middle
                la, lo = pts[len(pts) // 2]
            if not in_scope(la, lo):
                continue
            d = {"t": "w", "id": o.id, "lat": round(la, 6), "lon": round(lo, 6), "k": k, "tags": slim(t)}
            if k == "pier":
                d["ends"] = [[round(pts[0][0], 6), round(pts[0][1], 6)], [round(pts[-1][0], 6), round(pts[-1][1], 6)]]
                d["len_km"] = round(sum(_km(a, b) for a, b in zip(pts, pts[1:])), 3)
            if k == "water" and closed:
                # area handled by the area pass (from_way) to avoid double counting
                continue
            feats.append(d)
        elif o.is_area():
            if k not in ("water", "fishing", "beach"):
                continue
            if o.from_way() and k != "water":
                continue
            rings = [[(n.lat, n.lon) for n in ring] for ring in o.outer_rings()]
            rings = [r for r in rings if len(r) >= 3]
            if not rings:
                continue
            km2 = sum(area_km2_ring(r) for r in rings)
            big = max(rings, key=len)
            la = sum(p[0] for p in big) / len(big)
            lo = sum(p[1] for p in big) / len(big)
            if not in_scope(la, lo):
                continue
            d = {"t": "w" if o.from_way() else "r", "id": o.orig_id(), "lat": round(la, 6), "lon": round(lo, 6),
                 "k": k, "tags": slim(t), "km2": round(km2, 4)}
            if k == "water" and km2 >= 0.3:
                # outline for representative point / shore sampling (simplified)
                step = max(1, len(big) // 200)
                d["ring"] = [[round(a, 5), round(b, 5)] for a, b in big[::step]]
            feats.append(d)
    return path, feats, rivers, places, admin


def _admin(o, t):
    import shapely
    from shapely.geometry import Polygon, MultiPolygon
    polys = []
    try:
        for outer in o.outer_rings():
            pts = [(n.lon, n.lat) for n in outer]
            if len(pts) >= 4:
                polys.append(Polygon(pts))
    except Exception:
        return None
    if not polys:
        return None
    g = shapely.make_valid(MultiPolygon(polys) if len(polys) > 1 else polys[0])
    g = shapely.simplify(g, 0.003)
    c = g.representative_point()
    if not in_scope(c.y, c.x) and t.get("admin_level") != "2":
        return None
    return {"level": int(t.get("admin_level")), "name": t.get("name"), "ref": t.get("ref"),
            "iso": t.get("ISO3166-2") or t.get("ISO3166-1"), "code": t.get("ref:INSEE") or t.get("ref:nuts"),
            "names": {k: v for k, v in t if k in ("name:es", "name:fr", "name:pt", "name:en", "name:ca", "name:gl",
                                                  "name:eu")},
            "wkt": g.wkt}


def _km(a, b):
    kx = 111.32 * math.cos(math.radians(a[0]))
    return math.hypot((b[0] - a[0]) * 110.54, (b[1] - a[1]) * kx)


def main():
    pbf_dir = sys.argv[1]
    names = sys.argv[2:] or ["spain", "portugal", "france", "morocco"]
    out_dir = os.path.join(config.CACHE, "osm")
    os.makedirs(out_dir, exist_ok=True)
    paths = [os.path.join(pbf_dir, f"{n}-latest.osm.pbf") for n in names]
    with ProcessPoolExecutor(len(paths)) as ex:
        for path, feats, rivers, places, admin in ex.map(run, paths):
            name = os.path.basename(path).split("-latest")[0]
            with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
                json.dump({"feats": feats, "rivers": rivers, "places": places, "admin": admin}, f,
                          ensure_ascii=False)
            print(name, len(feats), len(rivers), len(places), len(admin), flush=True)


if __name__ == "__main__":
    main()
