"""Sky, weather code (`wx`), storm flags (`storm`) and surface currents (`cs`/`cd`): data-schema §2.

`wx` is a WMO-style code derived per hour from the blended series (vectorised over spots × hours):

  precipitation pr ≥ PR_MIN (0.1 mm/h):
    95/96 thunderstorm  convective precipitation cpr ≥ 0.5 mm/h, pr ≥ 0.5 mm/h and CAPE ≥ 400 J/kg
                        (ECMWF MUCAPE) or ≥ 300 J/kg (ICON-EU CAPE_ML); 96 when gusts ≥ 16.7 m/s (60 km/h)
    71 snow             t2 ≤ 1 °C
    80 showers          cpr / pr ≥ 0.6
    51 drizzle          pr < 0.3 mm/h
    61 / 63 / 65 rain   pr < 2.5 / < 7.6 / ≥ 7.6 mm/h (light / moderate / heavy, AMS rates)
  no precipitation:
    45 fog              t2 − td2 ≤ 1 °C, wind < 5 m/s and cloud cover ≥ 80 %
    0 / 1 / 2 / 3       cloud cover < 12.5 / < 37.5 / < 87.5 / ≥ 87.5 % (≤1, ≤3, ≤6, ≥7 oktas)
"""
from __future__ import annotations

import numpy as np

PR_MIN = 0.1            # mm/h: below this the hour is dry
DRIZZLE_MAX = 0.3       # mm/h
RAIN_MODERATE = 2.5     # mm/h
RAIN_HEAVY = 7.6        # mm/h
SNOW_T2 = 1.0           # °C
SHOWER_FRAC = 0.6       # convective share of the precipitation
THUNDER_CPR = 0.5       # mm/h convective precipitation
THUNDER_PR = 0.5        # mm/h total precipitation
THUNDER_CAPE = 400.0    # J/kg ECMWF most-unstable CAPE
THUNDER_CAPE_ML = 300.0  # J/kg ICON-EU mixed-layer CAPE
STRONG_GUST = 60 / 3.6  # m/s: 96 = thunderstorm with gusts ≥ 60 km/h
FOG_SPREAD = 1.0        # °C t2 − dew point
FOG_WIND = 5.0          # m/s
FOG_CC = 80.0           # %
CC_OKTAS = (12.5, 37.5, 87.5)

# Storm flags (next 72 h of `h`): gusts in m/s, as in `wg`.
STORM_HOURS = 72
GALE_SEA = 60 / 3.6     # 16.7 m/s
GALE_INLAND = 70 / 3.6  # 19.4 m/s

WX_CODES = (0, 1, 2, 3, 45, 51, 61, 63, 65, 71, 80, 95, 96)


def _nan(shape):
    return np.full(shape, np.nan, dtype=np.float32)


def wx_code(cc, pr, cpr=None, cape=None, cape_ml=None, t2=None, td2=None, ws=None, wg=None) -> np.ndarray:
    """WMO-style weather code per element (float array, NaN when neither cloud nor precipitation is known)."""
    cc = np.asarray(cc, dtype=float)
    pr = np.asarray(pr, dtype=float)
    shape = np.broadcast(cc, pr).shape

    def arr(x):
        return np.full(shape, np.nan) if x is None else np.broadcast_to(np.asarray(x, dtype=float), shape)

    cpr, cape, cape_ml, t2, td2, ws, wg = map(arr, (cpr, cape, cape_ml, t2, td2, ws, wg))
    out = np.full(shape, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        # sky
        sky = np.select([cc < CC_OKTAS[0], cc < CC_OKTAS[1], cc < CC_OKTAS[2]], [0, 1, 2], 3).astype(float)
        out = np.where(np.isfinite(cc), sky, out)
        fog = (np.isfinite(cc) & (cc >= FOG_CC) & ((t2 - td2) <= FOG_SPREAD) & (ws < FOG_WIND))
        out = np.where(fog, 45, out)
        # precipitation overrides the sky
        wet = pr >= PR_MIN
        rain = np.select([pr < DRIZZLE_MAX, pr < RAIN_MODERATE, pr < RAIN_HEAVY], [51, 61, 63], 65).astype(float)
        conv = np.nan_to_num(cpr) / np.where(pr > 0, pr, np.nan) >= SHOWER_FRAC
        code = np.where(conv, 80, rain)
        code = np.where(t2 <= SNOW_T2, 71, code)
        unstable = (cape >= THUNDER_CAPE) | (cape_ml >= THUNDER_CAPE_ML)
        thunder = (cpr >= THUNDER_CPR) & (pr >= THUNDER_PR) & unstable
        code = np.where(thunder, np.where(wg >= STRONG_GUST, 96, 95), code)
        out = np.where(wet, code, out)
        # dry hour with unknown cloud cover but known (zero) precipitation stays unknown
    return out.astype(np.float32)


def storms(times: np.ndarray, wx: list, wg: list, kind: str, now_ts: int, hours: int = STORM_HOURS) -> list[dict]:
    """[{"from", "to", "kind", "max"}] for the next `hours` hours from the hour containing `now_ts`.

    `from`/`to` are epoch seconds: start of the first flagged hour and end (exclusive) of the last one.
    Contiguous flagged hours of the same kind merge into one episode. `max` = highest gust (m/s, 1 decimal)
    in the episode, or null when unknown. thunder: wx ≥ 95; gale: wg ≥ 16.7 m/s (sea) / 19.4 m/s (inland).
    """
    t0 = now_ts - now_ts % 3600
    gale_thr = GALE_SEA if kind == "sea" else GALE_INLAND
    sel = [k for k, t in enumerate(times) if t0 <= int(t) < t0 + hours * 3600]
    out: list[dict] = []
    for name in ("thunder", "gale"):
        cur = None
        for k in sel:
            w, g = wx[k], wg[k]
            hit = (w is not None and w >= 95) if name == "thunder" else (g is not None and g >= gale_thr)
            if hit:
                t = int(times[k])
                if cur is not None and cur["to"] == t:
                    cur["to"] = t + 3600
                    if g is not None:
                        cur["max"] = g if cur["max"] is None else max(cur["max"], g)
                else:
                    cur = {"from": t, "to": t + 3600, "kind": name, "max": g}
                    out.append(cur)
            else:
                cur = None
    for e in out:
        if e["max"] is not None:
            e["max"] = round(float(e["max"]), 1)
    out.sort(key=lambda e: (e["from"], e["kind"]))
    return out


def current_speed_dir(u: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Surface current (eastward u, northward v, m/s) -> speed (m/s), direction it flows TO (deg, 0 = N)."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    speed = np.hypot(u, v)
    to = (np.degrees(np.arctan2(u, v)) + 360.0) % 360.0
    return speed.astype(np.float32), to.astype(np.float32)


def total_cloud(lcc, mcc, hcc) -> np.ndarray:
    """AROME has low/medium/high cloud only: total = mean of maximum and random overlap (%)."""
    l, m, h = (np.clip(np.asarray(x, dtype=float), 0, 100) / 100.0 for x in (lcc, mcc, hcc))
    mx = np.maximum(np.maximum(l, m), h)
    rnd = 1.0 - (1.0 - l) * (1.0 - m) * (1.0 - h)
    return (50.0 * (mx + rnd)).astype(np.float32)
