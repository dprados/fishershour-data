"""Geographic lookups for the spot build: admin areas (country/region/zone), places, elevation."""
import json
import math
import os
import re
import sys

import numpy as np
import shapely
from shapely import wkt as shapely_wkt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from naming import slugify  # noqa: E402

EXTRACTS = ["spain", "canary-islands", "portugal", "france", "morocco"]
EXTRACT_CC = {"spain": "ES", "canary-islands": "ES", "portugal": "PT", "france": "FR", "morocco": "MA"}


def km(lat1, lon1, lat2, lon2):
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * 110.54, (lon2 - lon1) * kx)


def bearing(lat1, lon1, lat2, lon2):
    kx = math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2((lon2 - lon1) * kx, lat2 - lat1)) % 360


def load_extracts():
    out = {}
    for name in EXTRACTS:
        p = os.path.join(config.CACHE, "osm", f"{name}.json")
        if os.path.exists(p):
            with open(p) as f:
                out[name] = json.load(f)
    return out


def display(name, names, lang="es"):
    """'Alacant / Alicante' -> name:es when present, else the first part."""
    if names.get(f"name:{lang}"):
        return names[f"name:{lang}"]
    if name and " / " in name:
        return name.split(" / ")[0]
    return name


class Admin:
    """Point -> (country, ES comunidad / FR région ISO, province/département/distrito)."""

    def __init__(self, extracts):
        self.l4, self.l6 = [], []
        seen = set()
        for ex, d in extracts.items():
            for a in d.get("admin", []):
                iso = a.get("iso") or ""
                lvl = a["level"]
                cc = iso[:2]
                if lvl == 2 and iso == "PT":
                    lvl = 4  # mainland + islands as one country-level polygon
                    iso = "PT"
                if not iso or cc not in ("ES", "FR", "PT", "MA"):
                    continue
                if lvl == 6 and cc == "MA":
                    continue
                key = (lvl, iso)
                if key in seen:
                    continue
                seen.add(key)
                g = shapely_wkt.loads(a["wkt"])
                rec = {"iso": iso, "name": a["name"], "names": a.get("names", {}), "ref": a.get("ref"),
                       "code": a.get("code"), "g": g, "cc": cc}
                (self.l4 if lvl == 4 else self.l6).append(rec)
        self.t4 = shapely.STRtree([r["g"] for r in self.l4])
        self.t6 = shapely.STRtree([r["g"] for r in self.l6])

    def _find(self, tree, recs, lat, lon, max_deg=0.25):
        p = shapely.Point(lon, lat)
        hits = tree.query(p, predicate="intersects")
        if len(hits):
            return recs[int(hits[0])]
        idx = tree.query_nearest(p, max_distance=max_deg)
        if len(idx):
            return recs[int(idx[0])]
        return None

    def locate(self, lat, lon, hint_cc=None):
        """(country, l4 record, l6 record)."""
        if lat < 30.0 and lon < -13.3 and lon > -18.5:
            cc = "ES"
        elif lon < -24:
            cc = "PT"
        elif 32.2 < lat < 33.3 and lon < -16:
            cc = "PT"
        elif lat < 35.95 and lon < -5.0 and lat > 27.0 and not (lat > 35.7 and lon > -5.95):
            cc = "MA"
        else:
            cc = None
        r4 = self._find(self.t4, self.l4, lat, lon)
        r6 = self._find(self.t6, self.l6, lat, lon)
        if cc is None:
            cc = r4["cc"] if r4 else hint_cc
        if r4 and r4["cc"] != cc:
            r4 = None
        if r6 and r6["cc"] != cc:
            r6 = None
        return cc, r4, r6


PLACE_W = {"city": 3.0, "town": 2.0, "village": 1.0, "suburb": 0.7, "hamlet": 0.5, "locality": 0.35}


class Places:
    def __init__(self, extracts):
        self.grid = {}
        seen = set()
        for d in extracts.values():
            for p in d["places"]:
                key = (p["name"], round(p["lat"], 3), round(p["lon"], 3))
                if key in seen or not p.get("name"):
                    continue
                seen.add(key)
                self.grid.setdefault((int(math.floor(p["lat"] * 10)), int(math.floor(p["lon"] * 10))), []).append(p)

    def near(self, lat, lon, radius_km, kinds=None):
        r = int(math.ceil(radius_km / 11.0)) + 1
        ci, cj = int(math.floor(lat * 10)), int(math.floor(lon * 10))
        out = []
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for p in self.grid.get((i, j), ()):
                    if kinds and p["place"] not in kinds:
                        continue
                    d = km(lat, lon, p["lat"], p["lon"])
                    if d <= radius_km:
                        out.append((d, p))
        return out

    def best(self, lat, lon, radius_km=8.0, kinds=("city", "town", "village", "suburb", "hamlet")):
        """Nearest settlement weighted by size."""
        cands = self.near(lat, lon, radius_km, kinds)
        if not cands:
            return None, None
        d, p = min(cands, key=lambda c: (c[0] + 0.3) / PLACE_W.get(c[1]["place"], 0.3))
        return p, d


class Elevation:
    """ICON-EU HSURF (DWD, 0.0625°) — coarse model orography, enough for the salmonid heuristic."""

    def __init__(self, path=None):
        path = path or os.path.join(config.CACHE, "ext", "hsurf.grib2.bz2")
        self.ok = False
        if not os.path.exists(path):
            return
        import bz2
        import eccodes
        h = eccodes.codes_new_from_message(bz2.decompress(open(path, "rb").read()))
        try:
            g = lambda k: eccodes.codes_get(h, k)  # noqa: E731
            self.ni, self.nj = g("Ni"), g("Nj")
            self.lat0 = g("latitudeOfFirstGridPointInDegrees")
            self.lon0 = g("longitudeOfFirstGridPointInDegrees")
            self.di = g("iDirectionIncrementInDegrees")
            self.dj = g("jDirectionIncrementInDegrees")
            self.vals = eccodes.codes_get_values(h).reshape(self.nj, self.ni)
            self.ok = True
        finally:
            eccodes.codes_release(h)

    def at(self, lat, lon):
        if not self.ok:
            return None
        i = int(round((lat - self.lat0) / self.dj))
        j = int(round(((lon - self.lon0) % 360.0) / self.di))
        if 0 <= i < self.nj and 0 <= j < self.ni:
            return float(self.vals[i, j])
        return None


def excluded(lat, lon):
    for s, w, n, e in config.EXCLUDE_BOXES:
        if s <= lat <= n and w <= lon <= e:
            return True
    return False


_ART = re.compile(r"^(le|la|les|l'|el|o|a)\s*", re.I)


def tz_of(cc, lat, lon):
    if cc == "ES":
        return "Atlantic/Canary" if lat < 30 else "Europe/Madrid"
    if cc == "PT":
        if lon < -24:
            return "Atlantic/Azores"
        if lon < -15:
            return "Atlantic/Madeira"
        return "Europe/Lisbon"
    if cc == "FR":
        return "Europe/Paris"
    return "Africa/Casablanca"


def zone_slug(name):
    return slugify(name or "x")
