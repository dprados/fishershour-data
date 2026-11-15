"""Fishing score — data-schema.md §3, implemented literally (pipeline only; the app does not recompute it).

sc = calibrated(Π factors) on 0..100 (§3.5), per hour and modality. All functions are vectorised over
numpy arrays (NaN = missing). A missing input makes its factor 1, except the core inputs of each family
(`hs` and wind for sea, wind for freshwater), whose absence makes the hour null.
"""
from __future__ import annotations

import numpy as np

SEA_MODES = ("surf", "spin", "rock", "eging", "kayak", "spear")
FRESH_MODES = ("fspin", "fly", "bottom")

S_TRAPEZOID = {
    "surf": (0.3, 0.7, 1.8, 3.0),
    "spin": (0.2, 0.5, 1.6, 2.6),
    "rock": (0.1, 0.3, 1.4, 2.2),
    "eging": (0.0, 0.0, 0.8, 1.5),
    "kayak": (0.0, 0.0, 0.6, 1.2),
    "spear": (0.0, 0.0, 0.5, 1.0),
}
WT_TRAPEZOID = {"fly": (5, 9, 15, 19), "fspin": (8, 16, 24, 29), "bottom": (10, 17, 26, 31)}

# Calibration (§3.5, 2026-11-15): raw product → 0..100 with a per-(family, mode) linear stretch, so that on
# days where the modality is possible the daily best is «Picada alta» on ~25 % of
# the days where the modality is possible (HI = 75th percentile of the non-zero daily best / 0.75; LO = 0). Fitted on 7 days × ~3,000 real spots (scratch script, see README «Calibración»).
# raw ≤ LO → 0, raw ≥ HI → 100. Unsafe hours (raw 0) stay 0.
CALIB: dict[tuple[str, str], tuple[float, float]] = {
    ("sea", "surf"): (0.0, 1.192),
    ("sea", "spin"): (0.0, 1.211),
    ("sea", "rock"): (0.0, 1.220),
    ("sea", "eging"): (0.0, 1.368),
    ("sea", "kayak"): (0.0, 1.490),
    ("sea", "spear"): (0.0, 1.338),
    ("river", "fspin"): (0.0, 0.747),
    ("river", "fly"): (0.0, 1.208),
    ("river", "bottom"): (0.0, 0.667),
    ("reservoir", "fspin"): (0.0, 1.590),
    ("reservoir", "fly"): (0.0, 1.610),
    ("reservoir", "bottom"): (0.0, 1.426),
}
KMH = 3.6


def _a(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def trapezoid(x, a, b, c, d) -> np.ndarray:
    """0 outside (a, d), 1 on [b, c], linear ramps. a == b (or c == d) means a vertical edge."""
    x = _a(x)
    up = np.where(x >= b, 1.0, np.where(x <= a, 0.0, (x - a) / max(b - a, 1e-9)))
    if a == b:
        up = np.where(x >= a, 1.0, 0.0)
    down = np.where(x <= c, 1.0, np.where(x >= d, 0.0, (d - x) / max(d - c, 1e-9)))
    return np.minimum(up, down)


def ang_diff(a, b) -> np.ndarray:
    d = np.abs(np.mod(_a(a) - _a(b), 360.0))
    return np.where(d > 180.0, 360.0 - d, d)


def exposure(direction, facing) -> np.ndarray:
    """1 within ±75° of facing, linear down to 0.35 at 150°, 0.35 beyond. Unknown facing/dir -> 1."""
    direction = _a(direction)
    if facing is None:
        return np.ones_like(direction)
    d = ang_diff(direction, facing)
    e = np.where(d <= 75, 1.0, np.where(d >= 150, 0.35, 1.0 - 0.65 * (d - 75) / 75.0))
    return np.where(np.isfinite(direction), e, 1.0)


# ------------------------------------------------------------------ sea
def wind_sector(wd, facing) -> np.ndarray:
    """0 = from land (offshore), 1 = cross, 2 = from the sea (onshore). δ = angle to the offshore direction."""
    wd = _a(wd)
    if facing is None:
        return np.ones_like(wd)
    delta = ang_diff(wd, (facing + 180.0) % 360.0)
    return np.where(delta > 110, 2, np.where(delta > 70, 1, 0))


def factor_tide(mode: str, v, coef) -> np.ndarray:
    """T = 0.65 + 0.35·v (v = |dh/dt| / day max), × coefficient band. NaN v (no tide) -> 1."""
    v = _a(v)
    coef = _a(coef)
    t = 0.65 + 0.35 * np.clip(v, 0, 1)
    if mode in ("surf", "spin", "rock"):
        k = np.where((coef >= 55) & (coef <= 95), 1.0, 0.85)
    elif mode in ("eging", "spear"):
        k = np.where(coef < 80, 1.0, 0.85)
    else:
        k = np.ones_like(t)
    k = np.where(np.isfinite(coef), k, 1.0)
    return np.where(np.isfinite(v), t * k, 1.0)


def factor_sea_state(mode: str, hs, direction, facing) -> np.ndarray:
    hs_eff = _a(hs) * exposure(direction, facing)
    return trapezoid(hs_eff, *S_TRAPEZOID[mode])


def factor_sea_wind(mode: str, ws, wd, facing) -> np.ndarray:
    v = _a(ws) * KMH
    sector = wind_sector(wd, facing)
    onshore = np.clip(1 - np.maximum(0, v - 15) * 0.025, 0.2, 1)
    cross = np.clip(1 - np.maximum(0, v - 20) * 0.02, 0.3, 1)
    offshore = np.clip(1 - np.maximum(0, v - 25) * 0.02, 0.4, 1)
    w = np.where(sector == 2, onshore, np.where(sector == 1, cross, offshore))
    if mode == "kayak":
        w = np.where(((sector == 0) & (v > 15)) | (v > 25), 0.0, w)
    return w


def factor_clarity(mode: str, cl) -> np.ndarray:
    cl = _a(cl)
    if mode in ("surf", "spin", "rock"):
        c = 0.75 + 0.25 * (1 - np.abs(cl - 0.55) / 0.55)
    elif mode in ("eging", "spear"):
        c = 0.4 + 0.6 * cl
    else:
        c = np.ones_like(cl)
    return np.where(np.isfinite(cl), c, 1.0)


def factor_sst(sst, sst_48h_ago) -> np.ndarray:
    drop = _a(sst_48h_ago) - _a(sst)
    return np.where(np.isfinite(drop) & (drop > 1.5), 0.85, 1.0)


def factor_light_sea(mode: str, phase, cl) -> np.ndarray:
    """phase: 2 dawn/dusk, 1 day, 0 night."""
    phase = np.asarray(phase)
    cl = _a(cl)
    night = {"surf": 1.0, "eging": 1.0, "rock": 0.8, "spin": 0.6, "kayak": 0.0, "spear": 0.0}[mode]
    if mode in ("spin", "spear"):
        day = np.where(np.isfinite(cl) & (cl > 0.7), 0.85, 0.9)
    else:
        day = np.full(phase.shape, 0.9)
    return np.where(phase == 2, 1.15, np.where(phase == 1, day, night))


def factor_pressure(p, p_12h_ago) -> np.ndarray:
    d = _a(p) - _a(p_12h_ago)
    out = np.where((d <= -2) & (d >= -6), 1.05, np.where(d > 4, 0.95, 1.0))
    return np.where(np.isfinite(d), out, 1.0)


def safety_sea(mode: str, hs, wg, ws, wd, facing, cl) -> np.ndarray:
    """§3.3: 0 where the modality is unsafe, else 1 (raw hs, as the app's warnings)."""
    hs = _a(hs)
    ok = np.ones_like(hs)
    if mode == "rock":
        ok = np.where((hs > 2.5) | (_a(wg) * KMH > 50), 0.0, ok)
    elif mode == "kayak":
        v = _a(ws) * KMH
        sector = wind_sector(wd, facing)
        ok = np.where(((sector == 0) & (v > 15)) | (v > 25) | (hs > 1.2), 0.0, ok)
    elif mode == "spear":
        cl = _a(cl)
        ok = np.where((hs > 1.0) | (np.isfinite(cl) & (cl < 0.3)), 0.0, ok)
    return ok


def sea_scores(mode: str, s: dict, facing) -> np.ndarray:
    """s: dict of aligned arrays hs, dir, ws, wg, wd, cl, sst, sst48, p, p12, phase, tide_v, coef, moon."""
    f = (factor_tide(mode, s["tide_v"], s["coef"])
         * factor_sea_state(mode, s["hs"], s["dir"], facing)
         * factor_sea_wind(mode, s["ws"], s["wd"], facing)
         * factor_clarity(mode, s["cl"])
         * factor_sst(s["sst"], s["sst48"])
         * factor_light_sea(mode, s["phase"], s["cl"])
         * factor_pressure(s["p"], s["p12"])
         * s["moon"]
         * safety_sea(mode, s["hs"], s["wg"], s["ws"], s["wd"], facing, s["cl"]))
    missing = ~np.isfinite(_a(s["hs"])) | ~np.isfinite(_a(s["ws"]))
    return finish(f, missing, ("sea", mode))


# ------------------------------------------------------------------ freshwater
def factor_flow(mode: str, r, rise24) -> np.ndarray:
    """Q on r = q/qn; rise24 = q / q(t-24h) - 1. NaN r -> 1."""
    r = _a(r)
    if mode in ("fly", "fspin"):
        q = np.where(r > 2.2, 0.3, np.where(r > 1.4, 0.7, np.where(r < 0.4, 0.75, 1.0)))
    else:
        q = np.where(r > 3, 0.5, np.where(r > 1.8, 0.8, 1.0))
    q = np.where(np.isfinite(r), q, 1.0)
    rise24 = _a(rise24)
    if mode in ("fly", "fspin"):
        q = q * np.where(np.isfinite(rise24) & (rise24 > 0.5), 0.7, 1.0)
    return q


def factor_level(trend) -> float:
    if trend is None or not np.isfinite(trend):
        return 1.0
    return 1.05 if trend > 1 else 0.85 if trend < -2 else 1.0


def factor_water_temp(mode: str, wt, wt_72h_ago) -> np.ndarray:
    wt = _a(wt)
    th = np.where(np.isfinite(wt), trapezoid(wt, *WT_TRAPEZOID[mode]), 1.0)
    drop = _a(wt_72h_ago) - wt
    return th * np.where(np.isfinite(drop) & (drop > 3), 0.85, 1.0)


def factor_rain(kind: str, rain24) -> np.ndarray:
    rain24 = _a(rain24)
    if kind != "river":
        return np.ones_like(rain24)
    return np.where(np.isfinite(rain24) & (rain24 > 15), 0.75, 1.0)


def factor_fresh_wind(kind: str, ws) -> np.ndarray:
    v = _a(ws) * KMH
    if kind in ("reservoir", "lake"):
        w = np.where(v > 45, 0.2, np.where(v > 30, 0.6, np.where((v >= 5) & (v <= 20), 1.05, 1.0)))
    else:
        w = np.where(v > 35, 0.7, 1.0)
    return np.where(np.isfinite(v), w, 1.0)


def factor_light_fresh(mode: str, phase) -> np.ndarray:
    phase = np.asarray(phase)
    night = 0.9 if mode == "bottom" else 0.3
    return np.where(phase == 2, 1.15, np.where(phase == 1, 0.9, night))


def fresh_scores(mode: str, kind: str, s: dict) -> np.ndarray:
    """s: ws, wt, wt72, r, rise24, rain24, p, p12, phase, moon, lvl_trend (scalar)."""
    f = (factor_flow(mode, s["r"], s["rise24"]) if kind == "river" else 1.0)
    f = (f * (factor_level(s.get("lvl_trend")) if kind == "reservoir" else 1.0)
         * factor_water_temp(mode, s["wt"], s["wt72"])
         * factor_rain(kind, s["rain24"])
         * factor_fresh_wind(kind, s["ws"])
         * factor_light_fresh(mode, s["phase"])
         * factor_pressure(s["p"], s["p12"])
         * s["moon"])
    missing = ~np.isfinite(_a(s["ws"]))
    return finish(f, missing, (family(kind), mode))


def family(kind: str) -> str:
    return "sea" if kind == "sea" else "river" if kind == "river" else "reservoir"


def finish(f, missing, key: tuple[str, str] | None = None) -> np.ndarray:
    f = _a(f)
    if key in CALIB:
        lo, hi = CALIB[key]
        f = np.where(f > 0, (f - lo) / (hi - lo), 0.0)
    sc = np.floor(np.clip(100.0 * f, 0, 100) + 0.5 + 1e-9)
    return np.where(missing, np.nan, sc)


def level(sc: float) -> int:
    """App levels (§3): 4 ≥ 75, 3 ≥ 55, 2 ≥ 35, 1 ≥ 18, else 0."""
    return 4 if sc >= 75 else 3 if sc >= 55 else 2 if sc >= 35 else 1 if sc >= 18 else 0
