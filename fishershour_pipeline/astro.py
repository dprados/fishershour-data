"""Sunrise/sunset (NOAA simplified, as Wavespot's sun.py) and moon phase, vectorised over hourly times."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

SUNRISE_ZENITH = 90.833
SYNODIC = 29.530588853
NEW_MOON_JD = 2451550.1          # 2000-01-06 14:24 UTC (mean new moon reference)
GOLDEN_MIN = 90                  # "alba y ocaso": ±90 min of sunrise/sunset (data-schema §3.2)


def _solar_params(jd: float) -> tuple[float, float]:
    t = (jd - 2451545.0) / 36525.0
    l0 = math.fmod(280.46646 + t * (36000.76983 + t * 0.0003032), 360.0)
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    omega = 125.04 - 1934.136 * t
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam))))
    y = math.tan(math.radians(eps) / 2.0) ** 2
    l0r = math.radians(l0)
    eot = 4.0 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                             - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    return decl, eot


def sun_times(lat: float, lon: float, day: dt.date) -> tuple[float, float]:
    """Sunrise and sunset of a UTC date in minutes after 00:00 UTC of that date."""
    jd = day.toordinal() + 1721424.5 + 0.5 - lon / 360.0
    decl, eot = _solar_params(jd)
    latr, declr = math.radians(lat), math.radians(decl)
    cos_h = (math.cos(math.radians(SUNRISE_ZENITH)) - math.sin(latr) * math.sin(declr)) / (math.cos(latr) * math.cos(declr))
    noon = 720.0 - 4.0 * lon - eot
    if cos_h <= -1.0:
        return 0.0, 1440.0
    if cos_h >= 1.0:
        return noon, noon
    ha = math.degrees(math.acos(cos_h))
    return noon - 4.0 * ha, noon + 4.0 * ha


def light_phase(lat: float, lon: float, times: np.ndarray) -> np.ndarray:
    """Per UNIX time: 2 = dawn/dusk (±90 min of sunrise or sunset), 1 = day, 0 = night."""
    times = np.asarray(times, dtype=np.int64)
    if len(times) == 0:
        return np.zeros(0, np.int8)
    d0 = dt.datetime.fromtimestamp(int(times.min()), dt.timezone.utc).date() - dt.timedelta(days=1)
    d1 = dt.datetime.fromtimestamp(int(times.max()), dt.timezone.utc).date() + dt.timedelta(days=1)
    rises, sets = [], []
    d = d0
    while d <= d1:
        r, s = sun_times(lat, lon, d)
        base = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()
        rises.append(base + r * 60)
        sets.append(base + s * 60)
        d += dt.timedelta(days=1)
    rises = np.array(rises)
    sets = np.array(sets)
    ev = np.concatenate([rises, sets])
    near = np.min(np.abs(times[:, None] - ev[None, :]), axis=1) <= GOLDEN_MIN * 60
    day = ((times[:, None] >= rises[None, :]) & (times[:, None] <= sets[None, :])).any(axis=1)
    out = np.where(near, 2, np.where(day, 1, 0)).astype(np.int8)
    return out


def moon_age(times: np.ndarray) -> np.ndarray:
    """Days since the last mean new moon."""
    jd = np.asarray(times, dtype=float) / 86400.0 + 2440587.5
    return np.mod(jd - NEW_MOON_JD, SYNODIC)


def moon_factor(times: np.ndarray) -> np.ndarray:
    """M: 1.03 within ±2 days of new or full moon, else 1."""
    age = moon_age(times)
    near_new = (age < 2.0) | (age > SYNODIC - 2.0)
    near_full = np.abs(age - SYNODIC / 2.0) < 2.0
    return np.where(near_new | near_full, 1.03, 1.0)
