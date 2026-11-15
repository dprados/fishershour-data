"""Spot catalogue (built by spots_build/, committed in data/spots/) and regulation regions."""
from __future__ import annotations

import json
import math
import os

from . import config

REGIONS_RULES = os.path.join("data", "regions", "rules.json")
ZONES = os.path.join("data", "spots", "zones.json")


def load_spots(paths=None) -> list[dict]:
    out = []
    for p in paths or config.SPOT_FILES:
        if os.path.exists(p):
            with open(p) as f:
                out.extend(json.load(f)["spots"])
    seen = set()
    uniq = []
    for s in out:
        if s["id"] in seen:
            raise ValueError(f"duplicate spot id {s['id']}")
        seen.add(s["id"])
        uniq.append(s)
    return uniq


def tile_of(lat: float, lon: float) -> str:
    return f"t_{math.floor(lat)}_{math.floor(lon)}"


def load_zone_names(path: str = ZONES) -> dict:
    if not os.path.exists(path):
        return {"zones": {}, "regions": {}}
    with open(path) as f:
        return json.load(f)


FIXED_NAMES = {
    "pt": {"es": "Portugal continental", "en": "Mainland Portugal", "fr": "Portugal continental",
           "pt": "Portugal continental"},
    "pt-cont": {"es": "Portugal (aguas interiores)", "en": "Portugal (inland waters)",
                "fr": "Portugal (eaux intérieures)", "pt": "Portugal (águas interiores)"},
    "pt-ac": {"es": "Azores", "en": "Azores", "fr": "Açores", "pt": "Açores"},
    "pt-md": {"es": "Madeira", "en": "Madeira", "fr": "Madère", "pt": "Madeira"},
    "ma": {"es": "Marruecos", "en": "Morocco", "fr": "Maroc", "pt": "Marrocos"},
    "es-cn": {"es": "Canarias", "en": "Canary Islands", "fr": "Canaries", "pt": "Canárias"},
}
LANGS = ("es", "en", "fr", "pt", "ca", "gl", "eu")


def _names(rec: dict | None) -> dict:
    if not rec:
        return {}
    base = rec["name"] or ""
    if " / " in base:
        base = base.split(" / ")[0]
    names = rec.get("names") or {}
    out = {}
    for lang in LANGS:
        out[lang] = names.get(f"name:{lang}") or base
    out["es"] = names.get("name:es") or base
    return out


def regions_meta(used: set[str], rules_path: str = REGIONS_RULES, zones_path: str = ZONES) -> list[dict]:
    """index/meta.json `regions`: id, name (per language), kind, rules (official URL), declare."""
    with open(rules_path) as f:
        rules = json.load(f)["rules"]
    zinfo = load_zone_names(zones_path).get("regions", {})
    out = []
    for rid in sorted(used):
        z = zinfo.get(rid, {})
        sea = z.get("sea", not (rid.endswith("-cont") or rid.startswith("fr-d-")))
        if rid in FIXED_NAMES:
            name = dict(FIXED_NAMES[rid])
        elif rid.startswith("fr-d-"):
            name = _names(z.get("l6"))
        else:
            name = _names(z.get("l4"))
        if not name:
            name = {"es": rid, "en": rid}
        cc = rid[:2]
        declare = None
        if sea:
            declare = {"es": "pescarec", "fr": "recfishing", "pt": "pt"}.get(cc)
        rule = rules.get(rid) or {}
        out.append({"id": rid, "name": name, "kind": "sea" if sea else "fresh", "rules": rule.get("url"),
                    "declare": declare})
    return out
