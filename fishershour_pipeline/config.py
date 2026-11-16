"""Static configuration: datasets, horizons, attribution. Boxes are (lat_min, lat_max, lon_min, lon_max)."""
from __future__ import annotations

import os

SCHEMA_VERSION = 1
BASE_URL = os.environ.get("FISHERSHOUR_BASE_URL", "https://dprados.github.io/fishershour-data/v1/")

# Horizons (data-schema §2): `h` = today 00Z .. +7 days hourly; `h3` = day 7 .. day 14 every 3 h.
H_HOURS = 168
TILE_HOURS = 36          # hourly scores of the best mode carried by each tile entry (h0/hs)
H3_STEP = 3
H3_N = 56
DAILY_DAYS = 14          # daily.q / daily.qn length without GloFAS (GloFAS: up to 30)
GLOFAS_DAYS = 30

# ---------------------------------------------------------------- Copernicus Marine
IBI_DOMAIN = (26.0, 56.0, -19.0, 5.0)
MED_DOMAIN = (30.2, 45.98, -6.0, 36.3)
IBI_WAV = "cmems_mod_ibi_wav_anfc_0.027deg_PT1H-i"
MED_WAV = "cmems_mod_med_wav_anfc_4.2km_PT1H-i"
WAV_VARS = ["VHM0", "VTPK", "VMDR"]
IBI_SST = ("cmems_mod_ibi_phy_anfc_0.027deg-3D_P1D-m", "thetao")
MED_SST = ("cmems_mod_med_phy-tem_anfc_4.2km_P1D-m", "thetao")
IBI_KD = ("cmems_mod_ibi_bgc-optics_anfc_0.027deg_P1D-m", "kd")
MED_KD = ("cmems_mod_med_bgc-optics_anfc_4.2km_P1D-m", "kd490")
# Surface currents, hourly (IBI includes tides). Forecast ~9–10 days: all of `h`, the start of `h3`.
IBI_CUR = "cmems_mod_ibi_phy_anfc_0.027deg-2D_PT1H-m"
MED_CUR = "cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m"
CUR_VARS = ["uo", "vo"]
CUR_DAYS = 14            # requested span; the service clips it to the forecast it has
WAVE_TILE_DEG = 2.0      # spots are grouped in 2°x2° tiles, one subset request per tile and basin
WAVE_TILE_MARGIN = 0.12

# Clarity mapping (README "Clarity"): cl = 1 at kd <= KD_CLEAR, 0 at kd >= KD_TURBID, log-linear between.
KD_CLEAR = 0.04
KD_TURBID = 0.8

# ------------------------------------------------------------------- ECMWF
ECMWF_MIRRORS = [
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "https://data.ecmwf.int/forecasts",
]
ECMWF_RESOL = "0p1"
ECMWF_WAVE_PARAMS = ["swh", "mwd", "pp1d"]
ECMWF_OPER_PARAMS = ["10u", "10v", "10fg", "2t", "msl", "tp", "tcc", "cp", "mucape", "2d"]
ECMWF_MAX_STEP = {"2d": 180}   # dew point only for fog on `h` (7 days): saves ~30 GRIB fields per run
ECMWF_STEPS = list(range(0, 145, 3)) + list(range(150, 361, 6))

# ------------------------------------------------------------------ DWD ICON-EU
ICON_EU_BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
ICON_EU_VARS = {"u_10m": "U_10M", "v_10m": "V_10M", "vmax_10m": "VMAX_10M", "clct": "CLCT", "cape_ml": "CAPE_ML"}
ICON_EU_STEPS = list(range(0, 79)) + list(range(81, 121, 3))
ICON_EU_DOMAIN = (29.5, 70.5, -23.5, 62.5)

# ------------------------------------------------------------------ Météo-France AROME
AROME_BASE = "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net"
AROME_PACKAGES = ["SP1", "SP2"]   # SP1: wind, gusts; SP2: low/medium/high cloud
AROME_BLOCKS = ["00H06H", "07H12H", "13H18H", "19H24H", "25H30H", "31H36H", "37H42H", "43H48H"]
AROME_DOMAIN = (37.5, 55.4, -12.0, 16.0)
AROME_EDGE_MARGIN = 0.5
AROME_ES_MIN_LAT = 38.0

# ------------------------------------------------------------------ Freshwater
HUBEAU = "https://hubeau.eaufrance.fr/api"
HUBEAU_MAX_KM_SAME_RIVER = 30.0
HUBEAU_MAX_KM_ANY = 3.0
MITECO_LAYER = ("https://services-eu1.arcgis.com/RvnYk1PBUJ9rrAuT/arcgis/rest/services/Embalses_Mapa/"
                "FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326&f=geojson")
EWDS_URL = "https://ewds.climate.copernicus.eu/api"
GLOFAS_AREA = [51.5, -10.0, 35.8, 10.0]   # N, W, S, E

# ------------------------------------------------------------------ EOT20
EOT20_URL = "https://www.seanoe.org/data/00683/79489/data/85762.zip"
TIDE_MIN_RANGE_M = 0.2   # spring range below this -> tide: null (Mediterranean)

ATTRIBUTION = [
    "Copernicus Marine Service",
    "Copernicus Emergency Management Service (GloFAS)",
    "ECMWF (CC BY 4.0)",
    "DWD",
    "Météo-France",
    "Hub'Eau / Eaufrance (Licence Ouverte)",
    "MITECO",
    "©Junta de Castilla y León (datos gratuitos en www.jcyl.es)",
    "EOT20 DGFI-TUM (CC BY 4.0)",
    "EMODnet Bathymetry Consortium, EMODnet Digital Bathymetry (DTM) (CC BY 4.0)",
    "GEBCO Compilation Group, GEBCO 2020 Grid",
    "© OpenStreetMap contributors (ODbL)",
]

DEPTH_FILE = "data/spots/depth.json"     # static, from spots_build/depth.py (EMODnet DTM, GEBCO fallback)
RAMPS_FILE = "data/ramps/ramps.json"     # weekly, from fishershour_pipeline.ramps (OSM via Overpass)

SPOT_FILES = ["data/spots/sea.json", "data/spots/fresh.json"]
