"""Spot catalogue sanity (data/spots/*.json, built by spots_build/)."""
import json
import os
import re
import sys

from fishershour_pipeline.config import SPOT_FILES
from fishershour_pipeline.spots import REGIONS_RULES, ZONES, load_spots, regions_meta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spots_build"))
import heuristics  # noqa: E402

SEA_MODES = {"surf", "spin", "rock", "eging", "kayak", "spear"}
FRESH_MODES = {"fspin", "fly", "bottom"}
SPOTS = load_spots(SPOT_FILES)


def test_counts_in_target():
    sea = [s for s in SPOTS if s["kind"] == "sea"]
    fresh = [s for s in SPOTS if s["kind"] != "sea"]
    assert 6000 <= len(sea) <= 12000
    assert 3000 <= len(fresh) <= 8000


def test_fields_and_modes():
    for s in SPOTS:
        assert re.match(r"^[a-z]{2}-[a-z0-9]+(-[a-z0-9]+)*$", s["id"]), s["id"]
        assert s["id"].startswith(s["country"].lower() + "-")
        assert s["country"] in ("ES", "PT", "FR", "MA")
        allowed = SEA_MODES if s["kind"] == "sea" else FRESH_MODES
        assert s["modes"] and set(s["modes"]) <= allowed, s["id"]
        assert set(s["info"]["species"]) <= set(heuristics.ALL_SPECIES), s["id"]
        if s["kind"] != "sea":
            assert s["country"] != "MA" and s["info"]["facing"] is None


def test_every_region_has_an_official_link():
    used = {s["region"] for s in SPOTS}
    regions = regions_meta(used, REGIONS_RULES, ZONES)
    missing = [r["id"] for r in regions if not (r["rules"] or "").startswith("http")]
    assert not missing, missing
    assert {r["declare"] for r in regions} <= {None, "pescarec", "recfishing", "pt"}


def test_every_zone_has_names():
    zones = json.load(open(ZONES))["zones"]
    for s in SPOTS:
        assert s["zone"] in zones and zones[s["zone"]].get("es"), s["zone"]
