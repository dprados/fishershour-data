"""MITECO Boletín Hidrológico: weekly reservoir storage for Spain (embalses > 5 hm³).

Source: MITECO's public ArcGIS FeatureServer behind the official "Dashboard Boletín Hidrológico"
(layer Embalses_Mapa: EMBALSE_ID, embalse_nombre, agua_actual/agua_total hm³, Porcentaje_Reserva,
Variacion_Porcentaje (points vs previous week), Fecha_str DDMMYYYY). Reuse allowed citing the source
(MITECO aviso legal, Ley 37/2007). Values are provisional.

The last good table is kept in the cache (state) so a failed week keeps the previous values.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

from .. import config
from .base import SourceStatus, http_get, log


def parse(geojson: dict) -> dict[str, dict]:
    out = {}
    for f in geojson.get("features") or []:
        p = f.get("properties") or {}
        mid = p.get("EMBALSE_ID")
        if mid is None or p.get("Porcentaje_Reserva") is None:
            continue
        ds = str(p.get("Fecha_str") or "")
        date = None
        if len(ds) == 8:
            date = f"{ds[4:8]}-{ds[2:4]}-{ds[0:2]}"
        elif p.get("fecha"):
            date = dt.datetime.fromtimestamp(p["fecha"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        trend = p.get("Variacion_Porcentaje")
        if trend is None and p.get("Variacion_Reserva") is not None and p.get("agua_total"):
            trend = 100.0 * p["Variacion_Reserva"] / p["agua_total"]
        out[str(int(mid))] = {"lvl": round(float(p["Porcentaje_Reserva"]), 1), "lvlDate": date,
                              "lvlTrend": None if trend is None else round(float(trend), 1),
                              "name": p.get("embalse_nombre")}
    return out


def fetch(cache_dir: str | None):
    t0 = time.time()
    path = os.path.join(cache_dir, "miteco_last.json") if cache_dir else None
    try:
        data = parse(json.loads(http_get(config.MITECO_LAYER, timeout=60)))
        if len(data) < 100:
            raise RuntimeError(f"only {len(data)} reservoirs")
        if path:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f)
        dates = sorted({v["lvlDate"] for v in data.values() if v["lvlDate"]})
        return data, SourceStatus("miteco", True, time.time() - t0, f"{len(data)} reservoirs, {dates[-1:]}")
    except Exception as e:  # noqa: BLE001
        log.warning("MITECO failed: %s", e)
        if path and os.path.exists(path):
            with open(path) as f:
                return json.load(f), SourceStatus("miteco", False, time.time() - t0, f"last good used: {e}")
        return {}, SourceStatus("miteco", False, time.time() - t0, str(e))
