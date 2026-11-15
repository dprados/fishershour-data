"""GloFAS river discharge forecast (Copernicus Emergency Management Service, CEMS-FLOODS licence).

Optional: runs only when the `EWDS_API_KEY` secret is set (personal access token of an
ewds.climate.copernicus.eu account that accepted the CEMS-FLOODS licence for `cems-glofas-forecast`).
Without it the source reports `nokey` and `daily.q` stays from Hub'Eau (or null).

Request: control forecast, river_discharge_in_the_last_24_hours, leadtime 24..720 h, area GLOFAS_AREA,
grib2. Each river spot takes, within ±2 grid cells (0.05°), the cell with the largest mean discharge
(snaps the point onto the modelled channel).
Attribution: "Generated using Copernicus Emergency Management Service information [year]".
"""
from __future__ import annotations

import datetime as dt
import os
import time

import numpy as np

from .. import config
from .base import SourceStatus, log, split_grib, decode_grib


def fetch(spots: list[dict], now: dt.datetime, work_dir: str):
    """Returns ({spot index: (start date, [m³/s per day])}, SourceStatus)."""
    t0 = time.time()
    key = os.environ.get("EWDS_API_KEY", "").strip()
    if not key:
        return {}, SourceStatus("glofas", True, 0.0, "nokey: set the EWDS_API_KEY secret to enable GloFAS")
    rivers = [i for i, s in enumerate(spots) if s.get("kind") == "river"]
    if not rivers:
        return {}, SourceStatus("glofas", True, 0.0, "no river spots")
    try:
        import cdsapi
    except ImportError:
        return {}, SourceStatus("glofas", False, 0.0, "cdsapi not installed")
    os.makedirs(work_dir, exist_ok=True)
    target = os.path.join(work_dir, "glofas.grib2")
    last_err = None
    for back in range(3):            # today's run, else the previous days
        run = (now - dt.timedelta(days=back)).date()
        try:
            c = cdsapi.Client(url=config.EWDS_URL, key=key, quiet=True, progress=False)
            c.retrieve("cems-glofas-forecast", {
                "system_version": ["operational"], "hydrological_model": ["lisflood"],
                "product_type": ["control_forecast"], "variable": "river_discharge_in_the_last_24_hours",
                "year": [f"{run.year}"], "month": [f"{run.month:02d}"], "day": [f"{run.day:02d}"],
                "leadtime_hour": [str(h) for h in range(24, 24 * config.GLOFAS_DAYS + 1, 24)],
                "area": config.GLOFAS_AREA, "data_format": "grib2", "download_format": "unarchived",
            }, target)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("GloFAS %s failed: %s", run, e)
    else:
        return {}, SourceStatus("glofas", False, time.time() - t0, f"retrieve failed: {last_err}")
    with open(target, "rb") as f:
        raw = f.read()
    fields = [decode_grib(m) for m in split_grib(raw)]
    fields.sort(key=lambda g: g.valid_time)
    if not fields:
        return {}, SourceStatus("glofas", False, time.time() - t0, "empty grib")
    grid = fields[0].grid
    stack = np.stack([g.values for g in fields])          # (nt, nlat, nlon)
    mean = np.nanmean(stack, axis=0)
    out = {}
    for i in rivers:
        s = spots[i]
        fi, fj = grid.fractional_index(s["lat"], s["lon"])
        ci, cj = int(round(fi)), int(round(fj))
        best = None
        for a in range(ci - 2, ci + 3):
            for b in range(cj - 2, cj + 3):
                if 0 <= a < grid.nlat and 0 <= b < grid.nlon and np.isfinite(mean[a, b]):
                    if best is None or mean[a, b] > best[0]:
                        best = (mean[a, b], a, b)
        if best is None:
            continue
        series = [None if not np.isfinite(v) else round(float(v), 2) for v in stack[:, best[1], best[2]]]
        # discharge "in the last 24 h" valid at run+24h*k -> the day before the valid time
        start = (dt.datetime.fromtimestamp(fields[0].valid_time, dt.timezone.utc) - dt.timedelta(days=1)).date()
        out[i] = (start, series)
    return out, SourceStatus("glofas", True, time.time() - t0, f"{len(out)} river spots", len(raw))
