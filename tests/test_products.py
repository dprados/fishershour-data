import datetime as dt
import json

import numpy as np
import pytest

from fishershour_pipeline import astro, products, wtemp
from fishershour_pipeline.sources import copernicus, hubeau, miteco

MED_CONS = {"M2": [0.06, 100.0], "S2": [0.02, 120.0], "K1": [0.03, 200.0]}
ATL_CONS = {"M2": [1.3, 95.0], "S2": [0.45, 125.0], "N2": [0.27, 75.0], "K1": [0.07, 60.0], "O1": [0.06, 320.0]}


def test_tide_null_in_the_mediterranean():
    assert products.tide_doc({"z0": 0, "cons": MED_CONS}) is None
    assert products.tide_doc({"z0": 0, "cons": ATL_CONS})["cons"] == ATL_CONS
    assert products.tide_doc(None) is None


def test_tide_factors_ranges():
    t0 = int(dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc).timestamp())
    times = t0 + 3600 * np.arange(48)
    v, coef = products.tide_factors({"z0": 0, "cons": ATL_CONS}, times, "Europe/Madrid")
    assert np.nanmax(v) <= 1 and np.nanmin(v) >= 0 and np.nanmax(v) > 0.9
    assert 20 < np.nanmin(coef) <= np.nanmax(coef) < 130
    v, coef = products.tide_factors(None, times, "Europe/Madrid")
    assert np.isnan(v).all()


def test_light_phase_madrid_november():
    # Sunrise in Madrid on 15 Nov ~ 07:10 UTC, sunset ~ 17:00 UTC
    t0 = int(dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc).timestamp())
    times = t0 + 3600 * np.arange(24)
    ph = astro.light_phase(40.4, -3.7, times)
    assert ph[2] == 0 and ph[7] == 2 and ph[12] == 1 and ph[17] == 2 and ph[22] == 0


def test_moon_factor():
    # 2026-11-24 ~ full moon (new moon 2026-11-09)
    t = np.array([dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc).timestamp(),
                  dt.datetime(2026, 11, 16, 12, tzinfo=dt.timezone.utc).timestamp()])
    m = astro.moon_factor(t)
    assert m[0] == 1.03 and m[1] == 1.0


def test_water_temperature_estimate():
    t = 3600 * np.arange(240)
    wt = wtemp.estimate(np.full(240, 25.0), t, "river", False)
    assert 18 < wt[-1] < 24
    cold = wtemp.estimate(np.full(240, 25.0), t, "river", True)
    assert cold[-1] < wt[-1]
    lake = wtemp.estimate(np.full(240, 2.0), t, "reservoir", False)
    assert lake[-1] >= 5.0


def test_clarity_mapping():
    cl = copernicus.clarity(np.array([0.03, 0.04, 0.18, 0.8, 2.0, np.nan]))
    assert cl[0] == 1 and cl[1] == pytest.approx(1)
    assert cl[2] == pytest.approx(0.5, abs=0.02)
    assert cl[3] == pytest.approx(0) and cl[4] == 0 and np.isnan(cl[5])  # NaN handled by caller


def test_hubeau_river_names():
    assert hubeau.norm_river("La Loire") == hubeau.norm_river("la loire") == "loire"
    assert hubeau.norm_river("L'Allier") == "allier"


def test_hubeau_assign_same_river_first():
    spots = [{"id": "fr-x", "country": "FR", "kind": "river", "lat": 47.5, "lon": 1.3, "name": "Loire à X",
              "x": {"river": "La Loire"}}]
    st = [{"code_station": "A", "latitude_station": 47.501, "longitude_station": 1.301, "libelle_cours_eau": "le Cosson"},
          {"code_station": "B", "latitude_station": 47.58, "longitude_station": 1.33, "libelle_cours_eau": "la Loire"}]
    assert hubeau.assign(spots, st) == [["B", "A"]]


def test_miteco_parse():
    gj = {"features": [{"properties": {"EMBALSE_ID": 20, "embalse_nombre": "Almendra", "Porcentaje_Reserva": 42.733,
                                       "Variacion_Porcentaje": -3.96, "Fecha_str": "10112026", "agua_total": 2649}}]}
    assert miteco.parse(gj)["20"] == {"lvl": 42.7, "lvlDate": "2026-11-10", "lvlTrend": -4.0, "name": "Almendra"}


def test_round_list():
    assert products.round_list(np.array([1.234, np.nan]), "hs") == [1.23, None]
    assert products.round_list(np.array([359.6]), "dir") == [0]
    assert products.round_list(np.array([0.123, 123.45]), "q") == [0.12, 123.5]
    assert json.dumps(products.round_list(np.array([55.0]), "sc")) == "[55]"
