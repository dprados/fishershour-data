"""Copernicus Marine Service: IBI (Atlantic) and MEDSEA (Mediterranean) waves, SST and light attenuation.

- Waves (VHM0, VTPK, VMDR) hourly: IBI 0.027° / MED 4.2 km, one `copernicusmarine subset` request per
  2°x2° tile of sea spots and basin.
- SST: daily surface `thetao` (IBI PHY / MED PHY-TEM), one request per basin over the spots' bbox.
- Clarity: daily diffuse attenuation (IBI BGC optics `kd`, MED BGC optics `kd490`), mapped to cl in 0..1.
- Currents: hourly surface uo/vo (IBI PHY 2D, with tides; MED PHY-CUR 2D), same tiles as waves.
Each spot uses the nearest valid sea cell, preferring cells whose 8 neighbours are sea (cached).
Attribution: "Generated using E.U. Copernicus Marine Service Information".
"""
from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from .. import config
from ..cells import map_cells
from ..grids import RegularGrid, erode_valid
from .base import IO_LOCK, SourceData, SourceStatus, log


def basin_of(spot: dict) -> str | None:
    """'med' | 'ibi' | None (sea spots outside both domains -> ECMWF only)."""
    if spot.get("kind") != "sea":
        return None
    lat, lon = spot["lat"], spot["lon"]
    if spot.get("x", {}).get("med"):
        a, b, c, d = config.MED_DOMAIN
        if a <= lat <= b and c <= lon <= d:
            return "med"
    a, b, c, d = config.IBI_DOMAIN
    if a <= lat <= b and c <= lon <= d:
        return "ibi"
    return None


def _cli() -> list[str]:
    exe = os.path.join(os.path.dirname(sys.executable), "copernicusmarine")
    if os.path.exists(exe):
        return [exe]
    found = shutil.which("copernicusmarine")
    if found:
        return [found]
    return [sys.executable, "-c", "from copernicusmarine.command_line_interface.copernicus_marine import "
            "command_line_interface as c; c()"]


def subset(dataset: str, variables: list[str], box, start: dt.datetime, end: dt.datetime, out_dir: str,
           name: str, depth: tuple[float, float] | None = None, timeout: int = 900) -> str:
    """`copernicusmarine subset` in a subprocess (credentials from COPERNICUSMARINE_SERVICE_* env)."""
    cmd = _cli() + [
        "subset", "--dataset-id", dataset,
        "--minimum-latitude", f"{box[0]:.3f}", "--maximum-latitude", f"{box[1]:.3f}",
        "--minimum-longitude", f"{box[2]:.3f}", "--maximum-longitude", f"{box[3]:.3f}",
        "--start-datetime", start.strftime("%Y-%m-%dT%H:%M:%S"),
        "--end-datetime", end.strftime("%Y-%m-%dT%H:%M:%S"),
        "--output-directory", out_dir, "--output-filename", name,
        "--overwrite", "--disable-progress-bar", "--log-level", "WARN",
    ]
    for v in variables:
        cmd += ["--variable", v]
    if depth is not None:
        cmd += ["--minimum-depth", str(depth[0]), "--maximum-depth", str(depth[1])]
    last = ""
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            path = os.path.join(out_dir, name)
            if r.returncode == 0 and os.path.exists(path):
                return path
            last = (r.stderr or r.stdout)[-400:]
        except subprocess.TimeoutExpired:
            last = "timeout"
        time.sleep(5 + 10 * attempt)
    raise RuntimeError(f"subset {name} failed: {last}")


def tiles(spots: list[dict]) -> dict[str, list[int]]:
    """{'{basin}_{ti}_{tj}': [spot indices]} for sea spots in IBI/MED."""
    out: dict[str, list[int]] = {}
    for i, s in enumerate(spots):
        b = basin_of(s)
        if b is None:
            continue
        ti = math.floor(s["lat"] / config.WAVE_TILE_DEG)
        tj = math.floor(s["lon"] / config.WAVE_TILE_DEG)
        out.setdefault(f"{b}_{ti}_{tj}".replace("-", "m"), []).append(i)
    return out


def _box(spots, idx, margin, domain):
    lats = [spots[i]["lat"] for i in idx]
    lons = [spots[i]["lon"] for i in idx]
    return (max(min(lats) - margin, domain[0]), min(max(lats) + margin, domain[1]),
            max(min(lons) - margin, domain[2]), min(max(lons) + margin, domain[3]))


def _grid_of(ds) -> RegularGrid:
    return RegularGrid.from_coords(ds["latitude"].values, ds["longitude"].values)


def _surface(da):
    for d in ("depth", "deptht", "lev"):
        if d in da.dims:
            da = da.isel({d: 0})
    return da


def _fetch_hourly(spots, h_start, end, work_dir, cache_dir, kind: str, datasets: dict[str, str],
                  variables: list[str], names: list[str], workers: int):
    """One hourly subset per 2°x2° tile and basin -> (target times, {name: (nspots, nt)}, covered, failed, bytes).

    `names[k]` is the output name of `variables[k]`. Each spot takes the nearest valid sea cell (eroded first).
    """
    groups = tiles(spots)
    nt = int((end - h_start).total_seconds() // 3600) + 1
    target = int(h_start.timestamp()) + 3600 * np.arange(nt, dtype=np.int64)
    nspots = len(spots)
    fields = {n: np.full((nspots, nt), np.nan, dtype=np.float32) for n in names}
    covered = np.zeros(nspots, bool)
    failed: dict[str, list[str]] = {"ibi": [], "med": []}
    nbytes = 0
    os.makedirs(work_dir, exist_ok=True)

    def work(key):
        basin = key.split("_")[0]
        domain = config.MED_DOMAIN if basin == "med" else config.IBI_DOMAIN
        box = _box(spots, groups[key], config.WAVE_TILE_MARGIN, domain)
        return subset(datasets[basin], variables, box, h_start, end, work_dir, f"{kind}_{key}.nc")

    import xarray as xr

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(work, k): k for k in groups}
        for f in as_completed(futs):
            key = futs[f]
            idx = groups[key]
            try:
                path = f.result()
                nbytes += os.path.getsize(path)
                with IO_LOCK, xr.open_dataset(path) as ds:
                    grid = _grid_of(ds)
                    arrs = [_surface(ds[v]).values for v in variables]
                    times = ds["time"].values.astype("datetime64[s]").astype(np.int64)
                os.remove(path)
            except Exception as e:
                failed[key.split("_")[0]].append(key)
                log.warning("%s tile %s failed: %s", kind, key, e)
                continue
            valid = np.isfinite(arrs[0]).any(axis=0)
            sub = [spots[i] for i in idx]
            cells, _ = map_cells(f"{kind}_{key}", grid, lambda: [erode_valid(valid), valid], sub, 12.0, cache_dir)
            ok = cells[:, 0] >= 0
            pos = np.searchsorted(target, times)
            inside = (pos < len(target)) & (target[np.clip(pos, 0, len(target) - 1)] == times)
            gi = np.array(idx)
            for name, arr in zip(names, arrs):
                vals = arr[:, cells[ok, 0], cells[ok, 1]].T  # (n_ok, nt)
                blk = fields[name][gi[ok]]
                blk[:, pos[inside]] = vals[:, inside]
                fields[name][gi[ok]] = blk
            covered[gi[ok]] = True
    return groups, target, fields, covered, failed, nbytes


def fetch_waves(spots: list[dict], h_start: dt.datetime, work_dir: str, cache_dir: str | None, workers: int = 6):
    t0 = time.time()
    groups = tiles(spots)
    if not groups:
        return None, SourceStatus("waves-cmems", False, 0.0, "no sea spot in IBI/MED")
    end = h_start + dt.timedelta(hours=config.H_HOURS - 1)
    groups, target, fields, covered, failed, nbytes = _fetch_hourly(
        spots, h_start, end, work_dir, cache_dir, "wav", {"ibi": config.IBI_WAV, "med": config.MED_WAV},
        config.WAV_VARS, ["hs", "tp", "dir"], workers)
    elapsed = time.time() - t0
    n_ibi = sum(1 for k in groups if k.startswith("ibi"))
    n_med = len(groups) - n_ibi
    st_ibi = SourceStatus("ibi", n_ibi > 0 and len(failed["ibi"]) < max(1, n_ibi) * 0.2, elapsed,
                          f"{n_ibi - len(failed['ibi'])}/{n_ibi} tiles", nbytes)
    st_med = SourceStatus("medsea", n_med > 0 and len(failed["med"]) < max(1, n_med) * 0.2, elapsed,
                          f"{n_med - len(failed['med'])}/{n_med} tiles")
    if not covered.any():
        return None, [st_ibi, st_med]
    return SourceData("cmems-waves", target, fields, covered), [st_ibi, st_med]


def fetch_currents(spots: list[dict], h_start: dt.datetime, work_dir: str, cache_dir: str | None,
                   workers: int = 6):
    """Hourly surface currents uo/vo (m/s) from IBI / MEDSEA PHY 2D, as far as the forecast goes (~9 days)."""
    t0 = time.time()
    groups = tiles(spots)
    if not groups:
        return None, SourceStatus("cmems-cur", False, 0.0, "no sea spot in IBI/MED")
    end = h_start + dt.timedelta(days=config.CUR_DAYS) - dt.timedelta(hours=1)
    groups, target, fields, covered, failed, nbytes = _fetch_hourly(
        spots, h_start, end, work_dir, cache_dir, "cur", {"ibi": config.IBI_CUR, "med": config.MED_CUR},
        config.CUR_VARS, ["u", "v"], workers)
    nfail = len(failed["ibi"]) + len(failed["med"])
    st = SourceStatus("cmems-cur", bool(covered.any()) and nfail < max(1, len(groups)) * 0.2, time.time() - t0,
                      f"{len(groups) - nfail}/{len(groups)} tiles; {int(covered.sum())} spots", nbytes)
    if not covered.any():
        return None, st
    return SourceData("cmems-cur", target, fields, covered), st


def _daily(spots, h_start, work_dir, cache_dir, which: str, days: int = 10):
    """Daily field per basin -> SourceData on noon-centred daily times."""
    t0 = time.time()
    import xarray as xr

    nspots = len(spots)
    out = None
    times_all = None
    nbytes = 0
    problems = []
    start = h_start - dt.timedelta(days=3)          # a few analysis days for the 48 h SST drop rule
    end = h_start + dt.timedelta(days=days)
    for basin, (dataset, var), domain in (
            ("ibi", config.IBI_SST if which == "sst" else config.IBI_KD, config.IBI_DOMAIN),
            ("med", config.MED_SST if which == "sst" else config.MED_KD, config.MED_DOMAIN)):
        idx = [i for i, s in enumerate(spots) if basin_of(s) == basin]
        if not idx:
            continue
        box = _box(spots, idx, 0.2, domain)
        try:
            path = subset(dataset, [var], box, start, end, work_dir, f"{which}_{basin}.nc",
                          depth=(0.0, 1.6))
        except Exception as e:
            problems.append(f"{basin}: {e}"[:160])
            continue
        nbytes += os.path.getsize(path)
        with IO_LOCK, xr.open_dataset(path) as ds:
            da = _surface(ds[var])
            grid = _grid_of(ds)
            vals = da.values
            times = ds["time"].values.astype("datetime64[s]").astype(np.int64) + 12 * 3600
        os.remove(path)
        valid = np.isfinite(vals).any(axis=0)
        sub = [spots[i] for i in idx]
        cells, _ = map_cells(f"{which}_{basin}_{grid.signature[:40].replace('.', '')}", grid,
                             lambda: [erode_valid(valid), valid], sub, 15.0, cache_dir)
        if times_all is None:
            times_all = times
            out = np.full((nspots, len(times)), np.nan, dtype=np.float32)
        common, ia, ib = np.intersect1d(times_all, times, return_indices=True)
        ok = cells[:, 0] >= 0
        gi = np.array(idx)
        v = vals[:, cells[ok, 0], cells[ok, 1]].T
        blk = out[gi[ok]]
        blk[:, ia] = v[:, ib]
        out[gi[ok]] = blk
    name = "sst" if which == "sst" else "kd"
    if out is None:
        return None, SourceStatus(f"cmems-{name}", False, time.time() - t0, "; ".join(problems) or "no spots")
    covered = np.isfinite(out).any(axis=1)
    st = SourceStatus(f"cmems-{name}", not problems, time.time() - t0,
                      f"{int(covered.sum())} spots" + (f"; {'; '.join(problems)}" if problems else ""), nbytes)
    return SourceData(f"cmems-{name}", times_all, {name: out}, covered), st


def fetch_sst(spots, h_start, work_dir, cache_dir):
    return _daily(spots, h_start, work_dir, cache_dir, "sst")


def fetch_kd(spots, h_start, work_dir, cache_dir):
    return _daily(spots, h_start, work_dir, cache_dir, "kd")


def clarity(kd: np.ndarray) -> np.ndarray:
    """Light attenuation (m^-1) -> clarity 0..1 (1 = very clear). Log-linear between KD_CLEAR and KD_TURBID.

    Secchi depth ~ 1.7/kd: kd 0.04 ~ 40 m (open ocean blue), 0.18 ~ 9 m (cl 0.5), 0.8 ~ 2 m (cl 0).
    """
    kd = np.asarray(kd, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        x = (math.log(config.KD_TURBID) - np.log(kd)) / (math.log(config.KD_TURBID) - math.log(config.KD_CLEAR))
    return np.clip(x, 0.0, 1.0).astype(np.float32)
