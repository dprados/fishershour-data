"""Blend sources into per-spot series on the `h` (hourly, 7 days) and `h3` (3-hourly, days 7–14) axes.

Waves  h : Copernicus IBI (Atlantic) / MEDSEA (Mediterranean) hourly -> per-hour fallback ECMWF WAM.
Waves h3 : ECMWF WAM × persistent coastal factor (Copernicus/ECMWF Hs over the last 48 h of h, 0.3–1.5).
Wind   h : AROME (FR + ES, 0–48 h) > ICON-EU (0–120 h) > ECMWF, 6 h cross-fades (as Wavespot).
Wind  h3 : ECMWF. t2, p (msl), pr (from accumulated tp): ECMWF.
SST, cl  : Copernicus daily, linear between daily means, held up to 3 days past the last day.
q        : Hub'Eau hourly observations (past hours only).
cc     h : AROME (lcc/mcc/hcc -> total) > ICON-EU CLCT > ECMWF tcc, same 6 h cross-fades as the wind.
cc    h3 : ECMWF. wx: weather.wx_code from cc, pr, ECMWF cp (convective rate), MUCAPE, ICON-EU CAPE_ML (h),
           t2, dew point, wind and gusts.
cs, cd   : Copernicus IBI / MEDSEA hourly surface currents (sea spots, ~9 days: all of h, part of h3).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from . import config
from .sources.base import SourceData
from .sources.copernicus import clarity
from .timeseries import interp_direction, interp_scalar, wind_from_uv
from .weather import current_speed_dir, wx_code

DIR_FIELDS = {"dir", "wd"}


@dataclass
class Sources:
    waves: SourceData | None = None
    ecmwf_wave: SourceData | None = None
    ecmwf_wind: SourceData | None = None
    icon: SourceData | None = None
    arome: SourceData | None = None
    sst: SourceData | None = None
    kd: SourceData | None = None
    hubeau: SourceData | None = None
    currents: SourceData | None = None
    miteco: dict = field(default_factory=dict)
    glofas: dict = field(default_factory=dict)


@dataclass
class Blended:
    th: np.ndarray
    th3: np.ndarray
    h: dict[str, np.ndarray]
    h3: dict[str, np.ndarray]
    rain_acc: tuple[np.ndarray, np.ndarray] | None = None   # (times, accumulated mm) per spot (ECMWF)
    q_hist: tuple[np.ndarray, np.ndarray] | None = None     # Hub'Eau times, q incl. the 48 h before h


def target_times(h_start: dt.datetime) -> tuple[np.ndarray, np.ndarray]:
    t0 = int(h_start.timestamp())
    th = t0 + 3600 * np.arange(config.H_HOURS, dtype=np.int64)
    th3 = t0 + 3600 * (config.H_HOURS + config.H3_STEP * np.arange(config.H3_N, dtype=np.int64))
    return th, th3


def _on(src: SourceData | None, name: str, target: np.ndarray, nspots: int, max_gap_h: float) -> np.ndarray:
    if src is None or name not in src.fields:
        return np.full((nspots, len(target)), np.nan, dtype=np.float32)
    fn = interp_direction if name in DIR_FIELDS else interp_scalar
    return fn(src.times, src.fields[name], target, max_gap_h)


def _daily_on(src: SourceData | None, name: str, target: np.ndarray, nspots: int, hold_h: float = 72.0):
    """Daily means (stamped at noon) -> linear in time, held flat up to hold_h beyond the ends."""
    out = np.full((nspots, len(target)), np.nan, dtype=np.float32)
    if src is None:
        return out
    vals = src.fields[name]
    t = src.times.astype(float)
    for i in np.nonzero(src.covered)[0]:
        ok = np.isfinite(vals[i])
        if not ok.any():
            continue
        ts, vs = t[ok], vals[i, ok]
        y = np.interp(target.astype(float), ts, vs)
        y[(target < ts[0] - hold_h * 3600) | (target > ts[-1] + hold_h * 3600)] = np.nan
        out[i] = y
    return out


def _fade(ok: np.ndarray) -> np.ndarray:
    """Weight of a regional source on its valid hours, ramping over 6 h at both ends (as Wavespot)."""
    n = len(ok)
    first = int(np.nonzero(ok)[0].min())
    last = int(np.nonzero(ok)[0].max())
    w = ok.astype(float)
    if first > 0:
        fade_in = np.arange(first, min(first + 7, last + 1))
        w[fade_in] = np.minimum(w[fade_in], (fade_in - first) / 6.0)
    if last < n - 1:
        fade = np.arange(max(0, last - 6), last + 1)
        w[fade] = np.minimum(w[fade], (last - fade) / 6.0)
    return w


def overlay_scalar(base: np.ndarray, regional: np.ndarray) -> np.ndarray:
    """Cross-fade a regional scalar (NaN outside its range) over a base series, per spot."""
    out = base.copy()
    for i in range(base.shape[0]):
        ok = np.isfinite(regional[i])
        if not ok.any():
            continue
        w = np.where(np.isfinite(base[i]), _fade(ok), ok.astype(float))
        out[i] = np.where(ok, w * regional[i] + (1 - w) * np.nan_to_num(base[i]), base[i])
    return out.astype(np.float32)


def _overlay(base_u, base_v, base_g, src: SourceData, target, nspots, max_gap_h):
    iu = _on(src, "u10", target, nspots, max_gap_h)
    iv = _on(src, "v10", target, nspots, max_gap_h)
    ig = _on(src, "gust", target, nspots, max_gap_h)
    u, v, g = base_u.copy(), base_v.copy(), base_g.copy()
    for i in range(nspots):
        ok = np.isfinite(iu[i]) & np.isfinite(iv[i])
        if not ok.any():
            continue
        w = _fade(ok)
        has_base = np.isfinite(base_u[i]) & np.isfinite(base_v[i])
        w = np.where(has_base, w, ok.astype(float))
        u[i] = np.where(ok, w * iu[i] + (1 - w) * np.nan_to_num(base_u[i]), base_u[i])
        v[i] = np.where(ok, w * iv[i] + (1 - w) * np.nan_to_num(base_v[i]), base_v[i])
        gi = np.where(np.isfinite(ig[i]), ig[i], base_g[i])
        g[i] = np.where(ok & np.isfinite(base_g[i]), w * gi + (1 - w) * base_g[i], np.where(ok, gi, base_g[i]))
    return u, v, g


def _wind(src: Sources, target: np.ndarray, nspots: int, regional: bool):
    u = _on(src.ecmwf_wind, "u10", target, nspots, 6.0)
    v = _on(src.ecmwf_wind, "v10", target, nspots, 6.0)
    g = _on(src.ecmwf_wind, "gust", target, nspots, 6.0)
    if regional:
        for s, gap in ((src.icon, 3.5), (src.arome, 1.5)):
            if s is not None:
                u, v, g = _overlay(u, v, g, s, target, nspots, gap)
    ws, wd = wind_from_uv(u, v)
    for i in range(nspots):            # gust undefined at step 0 -> back-fill up to 3 h
        row = g[i]
        nz = np.nonzero(np.isfinite(row))[0]
        if len(nz) and 0 < nz[0] <= 3:
            row[: nz[0]] = row[nz[0]]
    g = np.where(np.isfinite(g), np.maximum(g, ws), g)
    return {"ws": ws, "wg": g.astype(np.float32), "wd": wd}


def accum_rate(src: SourceData | None, target: np.ndarray, nspots: int, name: str = "tp") -> np.ndarray:
    """Precipitation rate (mm/h) on target from an accumulated field (`tp`, `cp`: mm since run start)."""
    out = np.full((nspots, len(target)), np.nan, dtype=np.float32)
    if src is None or name not in src.fields:
        return out
    t = src.times
    acc = src.fields[name]
    k = np.searchsorted(t, target, side="left")      # interval (t[k-1], t[k]]
    valid = (target >= t[0]) & (target <= t[-1])
    k = np.clip(k, 1, len(t) - 1)
    dt_h = (t[k] - t[k - 1]) / 3600.0
    rate = (acc[:, k] - acc[:, k - 1]) / np.where(dt_h > 0, dt_h, np.nan)
    rate = np.where(valid[None, :], np.maximum(rate, 0.0), np.nan)
    # step 0 (run start): hour before the first interval -> use the first interval's rate
    return rate.astype(np.float32)


def _waves(src: Sources, th: np.ndarray, th3: np.ndarray, nspots: int):
    h = {f: np.full((nspots, len(th)), np.nan, dtype=np.float32) for f in ("hs", "tp", "dir")}
    if src.waves is not None:
        for f in h:
            h[f] = src.waves.fields[f].copy()
    ec = {f: _on(src.ecmwf_wave, f, th, nspots, 6.0) for f in ("hs", "tp", "dir")}
    take = ~np.isfinite(h["hs"]) & np.isfinite(ec["hs"])
    for f in h:
        h[f] = np.where(take, ec[f], h[f])
    h3 = {f: _on(src.ecmwf_wave, f, th3, nspots, 6.0) for f in ("hs", "tp", "dir")}
    # persistent coastal factor from the last 48 h of h where Copernicus was used
    if src.waves is not None and src.ecmwf_wave is not None:
        cm = src.waves.fields["hs"][:, -48:]
        e = ec["hs"][:, -48:]
        both = np.isfinite(cm) & np.isfinite(e)
        sa = np.where(both, cm, 0).sum(axis=1)
        sb = np.where(both, e, 0).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            k = np.where((both.sum(axis=1) >= 12) & (sb > 0.05), sa / sb, 1.0)
        k = np.clip(np.nan_to_num(k, nan=1.0), 0.3, 1.5)
        h3["hs"] = h3["hs"] * k[:, None]
    return h, h3


def _sky(src: Sources, h: dict, h3: dict, th, th3, nspots: int) -> None:
    """cc (blended like the wind) and wx on both axes."""
    cc = _on(src.ecmwf_wind, "cc", th, nspots, 6.0)
    for s, gap in ((src.icon, 3.5), (src.arome, 1.5)):
        if s is not None and "cc" in s.fields:
            cc = overlay_scalar(cc, _on(s, "cc", th, nspots, gap))
    h["cc"] = np.clip(cc, 0, 100).astype(np.float32)
    h3["cc"] = np.clip(_on(src.ecmwf_wind, "cc", th3, nspots, 6.0), 0, 100).astype(np.float32)
    for blk, t, regional in ((h, th, True), (h3, th3, False)):
        cape_ml = _on(src.icon, "cape", t, nspots, 3.5) if regional else None
        # thresholds on pr as published (1 decimal), so `wx` never contradicts the `pr` the app shows
        blk["wx"] = wx_code(blk["cc"], np.round(blk["pr"], 1), cpr=accum_rate(src.ecmwf_wind, t, nspots, "cp"),
                            cape=_on(src.ecmwf_wind, "cape", t, nspots, 6.0), cape_ml=cape_ml, t2=blk["t2"],
                            td2=_on(src.ecmwf_wind, "td2", t, nspots, 6.0), ws=blk["ws"], wg=blk["wg"])


def _currents(src: Sources, h: dict, h3: dict, th, th3, nspots: int) -> None:
    """cs (m/s) and cd (deg, flowing to) from hourly u/v; NaN beyond the Copernicus forecast."""
    for blk, t in ((h, th), (h3, th3)):
        u = _on(src.currents, "u", t, nspots, 1.5)
        v = _on(src.currents, "v", t, nspots, 1.5)
        cs, cd = current_speed_dir(u, v)
        bad = ~(np.isfinite(u) & np.isfinite(v))
        blk["cs"] = np.where(bad, np.nan, cs).astype(np.float32)
        blk["cd"] = np.where(bad, np.nan, cd).astype(np.float32)


def blend(src: Sources, spots: list[dict], h_start: dt.datetime) -> Blended:
    nspots = len(spots)
    th, th3 = target_times(h_start)
    wh, w3 = _waves(src, th, th3, nspots)
    windh = _wind(src, th, nspots, regional=True)
    wind3 = _wind(src, th3, nspots, regional=False)
    h = {**wh, **windh}
    h3 = {**w3, **wind3}
    for blk, t in ((h, th), (h3, th3)):
        blk["t2"] = _on(src.ecmwf_wind, "t2", t, nspots, 6.0)
        blk["p"] = _on(src.ecmwf_wind, "msl", t, nspots, 6.0)
        blk["pr"] = accum_rate(src.ecmwf_wind, t, nspots)
        blk["sst"] = _daily_on(src.sst, "sst", t, nspots)
        kd = _daily_on(src.kd, "kd", t, nspots)
        blk["cl"] = np.where(np.isfinite(kd), clarity(kd), np.nan).astype(np.float32)
        blk["q"] = np.full((nspots, len(t)), np.nan, dtype=np.float32)
    _sky(src, h, h3, th, th3, nspots)
    _currents(src, h, h3, th, th3, nspots)
    q_hist = None
    if src.hubeau is not None:
        qt, qv = src.hubeau.times, src.hubeau.fields["q"]
        pos = np.searchsorted(qt, th)
        ok = (pos < len(qt)) & (qt[np.clip(pos, 0, len(qt) - 1)] == th)
        h["q"][:, ok] = qv[:, pos[ok]]
        q_hist = (qt, qv)
    rain_acc = None
    if src.ecmwf_wind is not None and "tp" in src.ecmwf_wind.fields:
        rain_acc = (src.ecmwf_wind.times, src.ecmwf_wind.fields["tp"])
    # sea-only and fresh-only fields
    for i, s in enumerate(spots):
        if s["kind"] == "sea":
            for blk in (h, h3):
                blk["q"][i] = np.nan
        else:
            for blk in (h, h3):
                for f in ("hs", "tp", "dir", "sst", "cl", "cs", "cd"):
                    blk[f][i] = np.nan
    return Blended(th, th3, h, h3, rain_acc, q_hist)
