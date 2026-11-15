import json

from fishershour_pipeline import ids
from fishershour_pipeline.config import SPOT_FILES
from fishershour_pipeline.spots import load_spots


def test_resolve_rules():
    aliases, orphans = ids.resolve({"a", "b", "c"}, {"a", "d"}, {}, {"b": "d", "x": "a"})
    assert aliases == {"b": "d", "x": "a"}
    assert orphans == ["c"]


def test_alias_chains():
    aliases, orphans = ids.resolve({"old"}, {"new"}, {}, {"old": "mid", "mid": "new"})
    assert aliases["old"] == "new" and not orphans


def test_catalogue_ids_unique_and_permanent():
    spots = load_spots(SPOT_FILES)
    assert len(spots) > 1000
    assert len({s["id"] for s in spots}) == len(spots)
    aliases, orphans = ids.catalogue_check(SPOT_FILES)
    assert not orphans, f"published ids without spot or alias: {orphans[:10]}"


def test_save_roundtrip(tmp_path):
    ids.save({"b", "a"}, {"x": "a"}, str(tmp_path / "p.txt"), str(tmp_path / "a.json"))
    assert ids.load_published(str(tmp_path / "p.txt")) == {"a", "b"}
    assert json.load(open(tmp_path / "a.json"))["aliases"] == {"x": "a"}
