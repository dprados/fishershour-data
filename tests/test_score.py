"""Every factor and safety rule of data-schema §3 (one test per rule)."""
import numpy as np
import pytest

from fishershour_pipeline import score as S


def one(x):
    return float(np.asarray(x).ravel()[0])


# ------------------------------------------------------------------ helpers
def test_trapezoid_edges():
    assert one(S.trapezoid([0.3], 0.3, 0.7, 1.8, 3.0)) == 0
    assert one(S.trapezoid([0.5], 0.3, 0.7, 1.8, 3.0)) == pytest.approx(0.5)
    assert one(S.trapezoid([1.0], 0.3, 0.7, 1.8, 3.0)) == 1
    assert one(S.trapezoid([2.4], 0.3, 0.7, 1.8, 3.0)) == pytest.approx(0.5)
    assert one(S.trapezoid([3.0], 0.3, 0.7, 1.8, 3.0)) == 0
    # a == b: flat water is perfect for eging
    assert one(S.trapezoid([0.0], 0, 0, 0.8, 1.5)) == 1


def test_exposure():
    assert one(S.exposure([300], 300)) == 1
    assert one(S.exposure([300 + 75], 300)) == 1
    assert one(S.exposure([300 + 150], 300)) == pytest.approx(0.35)
    assert one(S.exposure([300 + 112.5], 300)) == pytest.approx(0.675)
    assert one(S.exposure([120], 300)) == pytest.approx(0.35)
    assert one(S.exposure([10], None)) == 1


def test_wind_sector():
    # facing 270 (west): offshore wind comes from the east (90)
    assert one(S.wind_sector([90], 270)) == 0
    assert one(S.wind_sector([180], 270)) == 1
    assert one(S.wind_sector([270], 270)) == 2


# ------------------------------------------------------------------ sea factors
def test_tide_movement_and_coefficient():
    assert one(S.factor_tide("spin", [1.0], [70])) == pytest.approx(1.0)
    assert one(S.factor_tide("spin", [0.0], [70])) == pytest.approx(0.65)
    assert one(S.factor_tide("surf", [1.0], [40])) == pytest.approx(0.85)
    assert one(S.factor_tide("rock", [1.0], [100])) == pytest.approx(0.85)
    assert one(S.factor_tide("eging", [1.0], [79])) == pytest.approx(1.0)
    assert one(S.factor_tide("spear", [1.0], [80])) == pytest.approx(0.85)
    assert one(S.factor_tide("kayak", [1.0], [110])) == pytest.approx(1.0)
    assert one(S.factor_tide("spin", [np.nan], [np.nan])) == 1  # no tide (Mediterranean)


@pytest.mark.parametrize("mode,hs,expected", [
    ("surf", 1.0, 1), ("surf", 0.2, 0), ("spin", 0.35, 0.5), ("rock", 1.8, 0.5),
    ("eging", 0.0, 1), ("eging", 1.5, 0), ("kayak", 0.9, 0.5), ("spear", 0.75, 0.5),
])
def test_sea_state_trapezoids(mode, hs, expected):
    assert one(S.factor_sea_state(mode, [hs], [270], 270)) == pytest.approx(expected)


def test_sea_state_uses_exposure():
    # 2.0 m from behind the coast: effective 0.7 m -> surf 1.0 instead of 0.83
    assert one(S.factor_sea_state("surf", [2.0], [90], 270)) == pytest.approx(1.0)
    assert one(S.factor_sea_state("surf", [2.0], [270], 270)) == pytest.approx(1 - 0.2 / 1.2)


def test_wind_onshore_cross_offshore():
    ms = lambda kmh: [kmh / 3.6]  # noqa: E731
    assert one(S.factor_sea_wind("spin", ms(35), [270], 270)) == pytest.approx(0.5)     # onshore
    assert one(S.factor_sea_wind("spin", ms(60), [270], 270)) == pytest.approx(0.2)     # onshore floor
    assert one(S.factor_sea_wind("spin", ms(40), [180], 270)) == pytest.approx(0.6)     # cross
    assert one(S.factor_sea_wind("spin", ms(45), [90], 270)) == pytest.approx(0.6)      # offshore
    assert one(S.factor_sea_wind("spin", ms(10), [90], 270)) == 1


def test_kayak_wind_safety():
    ms = lambda kmh: [kmh / 3.6]  # noqa: E731
    assert one(S.factor_sea_wind("kayak", ms(16), [90], 270)) == 0   # offshore > 15
    assert one(S.factor_sea_wind("kayak", ms(14), [90], 270)) == 1
    assert one(S.factor_sea_wind("kayak", ms(26), [270], 270)) == 0  # any > 25


def test_clarity():
    assert one(S.factor_clarity("spin", [0.55])) == pytest.approx(1.0)
    assert one(S.factor_clarity("surf", [0.0])) == pytest.approx(0.75)
    assert one(S.factor_clarity("eging", [1.0])) == pytest.approx(1.0)
    assert one(S.factor_clarity("spear", [0.0])) == pytest.approx(0.4)
    assert one(S.factor_clarity("rock", [np.nan])) == 1


def test_sst_drop():
    assert one(S.factor_sst([15.0], [16.6])) == 0.85
    assert one(S.factor_sst([15.0], [16.4])) == 1
    assert one(S.factor_sst([15.0], [np.nan])) == 1


def test_light_sea():
    assert one(S.factor_light_sea("spin", [2], [0.5])) == 1.15
    assert one(S.factor_light_sea("surf", [0], [0.5])) == 1.0
    assert one(S.factor_light_sea("eging", [0], [0.5])) == 1.0
    assert one(S.factor_light_sea("rock", [0], [0.5])) == 0.8
    assert one(S.factor_light_sea("spin", [0], [0.5])) == 0.6
    assert one(S.factor_light_sea("kayak", [0], [0.5])) == 0
    assert one(S.factor_light_sea("spear", [0], [0.5])) == 0
    assert one(S.factor_light_sea("spin", [1], [0.8])) == 0.85
    assert one(S.factor_light_sea("spear", [1], [0.8])) == 0.85
    assert one(S.factor_light_sea("spin", [1], [0.5])) == 0.9
    assert one(S.factor_light_sea("rock", [1], [0.9])) == 0.9


def test_pressure():
    assert one(S.factor_pressure([1010], [1013])) == 1.05
    assert one(S.factor_pressure([1006], [1013])) == 1.0   # fall of 7 hPa: storm, no bonus
    assert one(S.factor_pressure([1018], [1013])) == 0.95
    assert one(S.factor_pressure([1014], [1013])) == 1.0


def test_safety_rock():
    assert one(S.safety_sea("rock", [2.6], [5], [5], [0], 0, [0.5])) == 0
    assert one(S.safety_sea("rock", [1.0], [14.0], [5], [0], 0, [0.5])) == 0    # gusts 50.4 km/h
    assert one(S.safety_sea("rock", [1.0], [13.0], [5], [0], 0, [0.5])) == 1


def test_safety_kayak():
    assert one(S.safety_sea("kayak", [1.3], [2], [2], [270], 270, [0.5])) == 0
    assert one(S.safety_sea("kayak", [0.5], [5], [4.5], [90], 270, [0.5])) == 0   # 16 km/h offshore
    assert one(S.safety_sea("kayak", [0.5], [8], [7.3], [270], 270, [0.5])) == 0  # 26 km/h onshore
    assert one(S.safety_sea("kayak", [0.5], [3], [3], [270], 270, [0.5])) == 1


def test_safety_spear():
    assert one(S.safety_sea("spear", [1.1], [2], [2], [0], 0, [0.8])) == 0
    assert one(S.safety_sea("spear", [0.5], [2], [2], [0], 0, [0.2])) == 0
    assert one(S.safety_sea("spear", [0.5], [2], [2], [0], 0, [0.6])) == 1


def _sea_inputs(**kw):
    base = dict(hs=[1.0], dir=[270], ws=[2.0], wg=[3.0], wd=[90], cl=[0.55], sst=[16], sst48=[16], p=[1013],
                p12=[1013], phase=[1], tide_v=[1.0], coef=[70], moon=np.array([1.0]))
    base.update(kw)
    return {k: np.asarray(v, dtype=float) for k, v in base.items()}


def test_sea_score_product_and_rounding(monkeypatch):
    monkeypatch.setattr(S, "CALIB", {})   # raw product; calibration tested separately
    # spin: S=1, W=1, C=1, L=0.9 -> 90
    assert one(S.sea_scores("spin", _sea_inputs(), 270)) == 90
    # dawn bonus and full moon can exceed 100 -> clamped
    assert one(S.sea_scores("spin", _sea_inputs(phase=[2], moon=np.array([1.03]), p=[1010]), 270)) == 100


def test_sea_score_missing_core_is_null():
    assert np.isnan(one(S.sea_scores("spin", _sea_inputs(hs=[np.nan]), 270)))


# ------------------------------------------------------------------ freshwater factors
def test_flow_bands_fly_fspin():
    assert one(S.factor_flow("fly", [1.0], [0])) == 1
    assert one(S.factor_flow("fly", [1.8], [0])) == 0.7
    assert one(S.factor_flow("fspin", [2.5], [0])) == 0.3
    assert one(S.factor_flow("fspin", [0.3], [0])) == 0.75
    assert one(S.factor_flow("fly", [1.0], [0.6])) == pytest.approx(0.7)   # rise > 50 % in 24 h
    assert one(S.factor_flow("fly", [np.nan], [np.nan])) == 1


def test_flow_bands_bottom():
    assert one(S.factor_flow("bottom", [1.0], [0])) == 1
    assert one(S.factor_flow("bottom", [2.5], [0])) == 0.8
    assert one(S.factor_flow("bottom", [3.5], [0])) == 0.5
    assert one(S.factor_flow("bottom", [1.0], [0.9])) == 1   # the rise rule only applies to fly/fspin


def test_level_trend():
    assert S.factor_level(1.5) == 1.05
    assert S.factor_level(-2.5) == 0.85
    assert S.factor_level(0) == 1
    assert S.factor_level(None) == 1


def test_water_temperature():
    assert one(S.factor_water_temp("fly", [12], [12])) == 1
    assert one(S.factor_water_temp("fly", [17], [17])) == pytest.approx(0.5)
    assert one(S.factor_water_temp("fspin", [12], [12])) == pytest.approx(0.5)
    assert one(S.factor_water_temp("bottom", [20], [24])) == pytest.approx(0.85)   # drop > 3 °C in 72 h
    assert one(S.factor_water_temp("bottom", [np.nan], [np.nan])) == 1


def test_rain_rivers_only():
    assert one(S.factor_rain("river", [16])) == 0.75
    assert one(S.factor_rain("river", [10])) == 1
    assert one(S.factor_rain("reservoir", [40])) == 1


def test_fresh_wind():
    ms = lambda kmh: [kmh / 3.6]  # noqa: E731
    assert one(S.factor_fresh_wind("reservoir", ms(10))) == 1.05
    assert one(S.factor_fresh_wind("lake", ms(35))) == 0.6
    assert one(S.factor_fresh_wind("reservoir", ms(50))) == 0.2
    assert one(S.factor_fresh_wind("reservoir", ms(2))) == 1
    assert one(S.factor_fresh_wind("river", ms(40))) == 0.7
    assert one(S.factor_fresh_wind("river", ms(30))) == 1


def test_light_fresh():
    assert one(S.factor_light_fresh("fly", [2])) == 1.15
    assert one(S.factor_light_fresh("bottom", [0])) == 0.9
    assert one(S.factor_light_fresh("fspin", [0])) == 0.3
    assert one(S.factor_light_fresh("fly", [1])) == 0.9


def _fresh_inputs(**kw):
    base = dict(ws=[2.0], wt=[12], wt72=[12], r=[1.0], rise24=[0.0], rain24=[0.0], p=[1013], p12=[1013],
                phase=[1], moon=np.array([1.0]))
    base.update(kw)
    out = {k: np.asarray(v, dtype=float) for k, v in base.items()}
    out["lvl_trend"] = kw.get("lvl_trend")
    return out


def test_fresh_score_river_flood_and_reservoir_level(monkeypatch):
    monkeypatch.setattr(S, "CALIB", {})
    assert one(S.fresh_scores("fly", "river", _fresh_inputs())) == 90
    assert one(S.fresh_scores("fly", "river", _fresh_inputs(r=[3.5]))) == 27      # flood: 0.3
    # reservoir: no flow factor, rising level 1.05, light wind 1.05
    v = one(S.fresh_scores("fspin", "reservoir", _fresh_inputs(ws=[3.0], lvl_trend=2.0, r=[5.0], wt=[18], wt72=[18])))
    assert v == round(100 * 0.9 * 1.05 * 1.05)


def test_calibration_stretch():
    lo, hi = S.CALIB[("sea", "spin")]
    assert lo == 0.0 and hi > 1
    # a plain daytime hour (raw 0.9) is «buena», not «alta»; dawn + moving tide + moon reaches «alta»
    assert S.level(one(S.sea_scores("spin", _sea_inputs(), 270))) == 3
    assert S.level(one(S.sea_scores("spin", _sea_inputs(phase=[2], moon=np.array([1.03]), p=[1010]), 270))) == 4
    # unsafe stays 0 and the scale never exceeds 100
    assert one(S.finish(np.array([0.0]), np.array([False]), ("sea", "spin"))) == 0
    assert one(S.finish(np.array([9.0]), np.array([False]), ("sea", "spin"))) == 100
    # every family × mode has a calibration
    assert {m for f, m in S.CALIB} == set(S.SEA_MODES) | set(S.FRESH_MODES)


def test_levels():
    assert [S.level(x) for x in (80, 60, 40, 20, 5)] == [4, 3, 2, 1, 0]
