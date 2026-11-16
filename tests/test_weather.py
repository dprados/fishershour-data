"""Sky (cc, wx), storm flags, currents, depth and ramps (data-schema §2, added 16-11-2026)."""
import numpy as np
import pytest

from fishershour_pipeline import blend, products, ramps, weather
from fishershour_pipeline.sources.base import SourceData
from fishershour_pipeline.validate import check_block, check_depth, check_ramp_tile, check_storm
from spots_build import depth as depth_build


def wx(**kw):
    base = dict(cc=50.0, pr=0.0, cpr=0.0, cape=0.0, cape_ml=np.nan, t2=12.0, td2=6.0, ws=6.0, wg=9.0)
    base.update(kw)
    return int(weather.wx_code(**{k: np.array([v]) for k, v in base.items()})[0])


@pytest.mark.parametrize("cc,code", [(0, 0), (12, 0), (20, 1), (50, 2), (87, 2), (88, 3), (100, 3)])
def test_wx_sky_codes(cc, code):
    assert wx(cc=cc) == code


def test_wx_fog_needs_saturation_calm_and_cloud():
    assert wx(cc=95, t2=10.0, td2=9.5, ws=2.0) == 45
    assert wx(cc=95, t2=10.0, td2=9.5, ws=7.0) == 3        # windy: no fog
    assert wx(cc=40, t2=10.0, td2=9.5, ws=2.0) == 2        # clear-ish sky: not fog
    assert wx(cc=95, t2=10.0, td2=7.0, ws=2.0) == 3        # dry air


@pytest.mark.parametrize("pr,code", [(0.05, 3), (0.1, 51), (0.29, 51), (0.3, 61), (2.4, 61), (2.5, 63), (7.6, 65)])
def test_wx_rain_intensity(pr, code):
    assert wx(cc=100, pr=pr) == code


def test_wx_snow_showers_thunder():
    assert wx(cc=100, pr=1.0, t2=0.5) == 71
    assert wx(cc=60, pr=1.0, cpr=0.7) == 80                                   # convective share 0.7
    assert wx(cc=60, pr=1.0, cpr=0.7, cape=500) == 95
    assert wx(cc=60, pr=1.0, cpr=0.7, cape=100, cape_ml=350) == 95             # ICON-EU CAPE_ML
    assert wx(cc=60, pr=1.0, cpr=0.7, cape=500, wg=17.0) == 96                 # gusts ≥ 60 km/h
    assert wx(cc=60, pr=1.0, cpr=0.4, cape=2000) == 61                        # mostly stratiform rain
    assert wx(cc=60, pr=0.4, cpr=0.4, cape=2000) == 80                        # too little rain
    assert wx(cc=60, pr=1.0, cpr=0.7, cape=500, t2=0.0) == 95                 # thunder beats snow


def test_wx_unknown_without_cloud_and_rain():
    out = weather.wx_code(np.array([np.nan, np.nan]), np.array([np.nan, 2.0]))
    assert np.isnan(out[0]) and out[1] == 61


def test_storms_merge_and_thresholds():
    t0 = 1_794_700_800                       # on the hour
    times = t0 + 3600 * np.arange(100)
    wxl = [3] * 100
    wg = [5.0] * 100
    wxl[2] = wxl[3] = 95
    wxl[4] = 96
    wg[4] = 18.0
    wg[10] = 17.0                            # gale at sea, not inland
    wg[11] = 20.0                            # gale everywhere
    wg[90] = 30.0                            # beyond 72 h
    sea = weather.storms(times, wxl, wg, "sea", t0 + 1800)
    assert sea == [{"from": t0 + 2 * 3600, "to": t0 + 5 * 3600, "kind": "thunder", "max": 18.0},
                   {"from": t0 + 4 * 3600, "to": t0 + 5 * 3600, "kind": "gale", "max": 18.0},
                   {"from": t0 + 10 * 3600, "to": t0 + 12 * 3600, "kind": "gale", "max": 20.0}]
    inland = weather.storms(times, wxl, wg, "river", t0)
    assert [e["kind"] for e in inland] == ["thunder", "gale"]
    assert inland[1] == {"from": t0 + 11 * 3600, "to": t0 + 12 * 3600, "kind": "gale", "max": 20.0}
    later = weather.storms(times, wxl, wg, "sea", t0 + 12 * 3600)
    assert later == []                       # past episodes drop out; hour 90 is beyond 72 h
    assert weather.storms(times, [None] * 100, [None] * 100, "sea", t0) == []
    assert check_storm(sea) == [] and check_storm(inland) == []


def test_check_storm_rejects_bad_entries():
    assert check_storm({"from": 1}) == ["storm must be a list"]
    assert check_storm([{"from": 7200, "to": 3600, "kind": "gale", "max": 20}])
    assert check_storm([{"from": 3600, "to": 7200, "kind": "hail", "max": 20}])
    assert check_storm([{"from": 3600, "to": 7200, "kind": "gale"}])


def test_current_speed_and_direction_to():
    s, d = weather.current_speed_dir(np.array([0.0, 0.3, 0.0, -0.4]), np.array([0.5, 0.0, -0.2, 0.0]))
    assert np.allclose(s, [0.5, 0.3, 0.2, 0.4])
    assert np.allclose(d, [0, 90, 180, 270])     # flowing TO north, east, south, west


def test_total_cloud_overlap():
    assert weather.total_cloud(0, 0, 0) == 0
    assert weather.total_cloud(100, 0, 0) == 100
    v = float(weather.total_cloud(50, 50, 0))
    assert 50 < v < 75                           # between maximum (50) and random (75) overlap


def test_overlay_scalar_fades_in_regional_source():
    base = np.full((1, 30), 20.0, dtype=np.float32)
    reg = np.full((1, 30), np.nan, dtype=np.float32)
    reg[0, :20] = 80.0
    out = blend.overlay_scalar(base, reg)
    assert out[0, 0] == 80.0 and out[0, 25] == 20.0
    assert 20.0 < out[0, 17] < 80.0              # 6 h fade-out before the regional source ends


def test_convective_rate_from_accumulation():
    t = np.array([0, 3 * 3600, 6 * 3600], dtype=np.int64)
    src = SourceData("ecmwf", t, {"cp": np.array([[0.0, 3.0, 3.0]], dtype=np.float32)}, np.array([True]))
    rate = blend.accum_rate(src, np.array([3600, 4 * 3600]), 1, "cp")
    assert np.allclose(rate, [[1.0, 0.0]])


def test_round_list_new_fields():
    assert products.round_list([12.6, np.nan], "cc") == [13, None]
    assert products.round_list([95.0], "wx") == [95]
    assert products.round_list([359.7], "cd") == [0]
    assert products.round_list([0.123], "cs") == [0.12]


def test_check_block_optional_fields():
    doc = {"id": "es-x", "kind": "river", "modes": ["fspin"]}
    blk = {f: [None] * 2 for f in ("hs", "tp", "dir", "ws", "wg", "wd", "t2", "p", "pr", "sst", "cl", "q", "wt")}
    blk.update({"step": 1, "n": 2, "sc": {"fspin": [1, 2]}, "cc": [0, 100], "wx": [3, 95]})
    errors = []
    check_block(dict(doc, h=blk), "h", 2, 1, errors)
    assert errors == []
    blk["wx"] = [4, 95]
    blk["cs"] = [0.1, 0.2]
    check_block(dict(doc, h=blk), "h", 2, 1, errors)
    assert any("wx invalid" in e for e in errors) and any("freshwater" in e for e in errors)


def test_check_depth():
    assert check_depth({"kind": "sea", "info": {"depth": 6, "depth10": 120}}) == []
    assert check_depth({"kind": "sea", "info": {"depth": None}}) == []
    assert check_depth({"kind": "reservoir", "info": {"depth": 6}})
    assert check_depth({"kind": "sea", "info": {"depth": -3}})


WCS_TEXT = """Grid bounds: ...
Grid to world: PARAM_MT["Affine",
  PARAMETER["num_row", 3],
  PARAMETER["elt_0_0", 0.001],
  PARAMETER["elt_0_2", -3.0005],
  PARAMETER["elt_1_1", -0.001],
  PARAMETER["elt_1_2", 43.0025]]
Contents:
Band 0:
-20.0 -15.0 -12.0 -11.0 -9.0
-15.0 -11.0 -8.0 -6.0 -4.0
-9.0 -6.0 -4.0 -2.0 5.0
-5.0 -3.0 -1.0 4.0 12.0
3.0 6.0 9.0 15.0 30.0
"""


def test_depth_from_wcs_window():
    z, lats, lons = depth_build.parse_wcs_text(WCS_TEXT)
    assert z.shape == (5, 5) and lats[0] == pytest.approx(43.0025) and lons[1] == pytest.approx(-2.9995)
    # spot on land in the south-east corner, facing north-west (315°)
    d, d10 = depth_build.depth_at(z, lats, lons, 42.9985, -2.9965, 315.0)
    assert d is not None and 1 <= d <= 10
    assert d10 is not None and 100 <= d10 <= 400
    # no water at all -> nothing
    assert depth_build.depth_at(np.abs(z) + 1, lats, lons, 43.0, -3.0, None) == (None, None)


def test_ramps_parse_dedupe_box_and_tiles():
    els = [
        {"type": "node", "lat": 43.4612345, "lon": -3.8, "tags": {"leisure": "slipway"}},
        {"type": "way", "center": {"lat": 43.46125, "lon": -3.80005}, "tags": {"leisure": "slipway", "name": "Rampa"}},
        {"type": "node", "lat": 43.47, "lon": -3.8, "tags": {"man_made": "slipway", "name": " "}},
        {"type": "node", "lat": 16.2, "lon": -61.5, "tags": {"leisure": "slipway"}},          # Guadeloupe: out
        {"type": "relation", "tags": {"leisure": "slipway"}},                                   # no position
    ]
    r = ramps.parse(els)
    assert r == [[43.46125, -3.80005, "Rampa"], [43.47, -3.8, None]]
    t = ramps.tiles(r)
    assert list(t) == ["t_43_-4"]
    assert t["t_43_-4"] == {"v": 1, "r": [{"lat": 43.46125, "lon": -3.80005, "n": "Rampa"},
                                          {"lat": 43.47, "lon": -3.8, "n": None}]}
    assert check_ramp_tile("t_43_-4", t["t_43_-4"]) == []
    assert check_ramp_tile("t_42_-4", t["t_43_-4"])           # points outside the tile
