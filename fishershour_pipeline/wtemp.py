"""Water temperature estimate from air temperature (Mohseni et al. 1998 logistic model).

    wt = mu + (alpha - mu) / (1 + exp(gamma · (beta - Ta)))

Ta = trailing mean of the 2 m air temperature: 3 days for rivers, 7 days for reservoirs/lakes (thermal
inertia). Generic parameters (no station calibration yet); salmonid/mountain rivers use a colder alpha.
Published with info.wtEstimated = true. The trailing mean uses the samples available from the start of
the series (the first days average over a shorter window).
"""
from __future__ import annotations

import numpy as np

PARAMS = {
    "river": (0.5, 24.0, 13.5, 0.20),      # mu, alpha, beta, gamma
    "river_cold": (0.5, 19.0, 13.0, 0.20),
    "lake": (5.0, 27.0, 14.0, 0.17),
}
WINDOW_H = {"river": 72, "river_cold": 72, "lake": 168}


def mohseni(ta, mu, alpha, beta, gamma):
    ta = np.asarray(ta, dtype=float)
    return mu + (alpha - mu) / (1.0 + np.exp(gamma * (beta - ta)))


def trailing_mean(values: np.ndarray, times: np.ndarray, window_h: float) -> np.ndarray:
    """Time-weighted trailing mean over `window_h` hours on an irregular axis (NaN-safe)."""
    v = np.asarray(values, dtype=float)
    t = np.asarray(times, dtype=float)
    ok = np.isfinite(v)
    if ok.sum() < 2:
        return np.full(len(t), np.nan)
    grid = np.arange(t[ok][0], t[ok][-1] + 1, 3600.0)
    g = np.interp(grid, t[ok], v[ok])
    c = np.concatenate([[0.0], np.cumsum(g)])
    k = np.searchsorted(grid, t, side="right")       # samples <= t
    w = int(window_h)
    lo = np.maximum(k - w, 0)
    n = k - lo
    out = np.where(n > 0, (c[k] - c[lo]) / np.maximum(n, 1), np.nan)
    out[(t < t[ok][0]) | (t > t[ok][-1])] = np.nan
    return out


def estimate(t2: np.ndarray, times: np.ndarray, kind: str, cold: bool) -> np.ndarray:
    key = "lake" if kind in ("reservoir", "lake") else ("river_cold" if cold else "river")
    ta = trailing_mean(t2, times, WINDOW_H[key])
    wt = mohseni(ta, *PARAMS[key])
    return np.maximum(wt, 0.0)
