"""Static configuration for the spot build (scope, densities, thresholds).

Boxes are (south, west, north, east) in WGS84 degrees.
"""
import os

CACHE = os.environ.get("FISHERSHOUR_SPOTS_CACHE", os.path.expanduser("~/.cache/ardora"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "data", "spots")

# Coarse scope (anything outside is ignored at extraction time).
SCOPE_BOXES = [
    ("ES", (27.4, -18.4, 29.6, -13.3)),      # Canary Islands
    ("PT", (32.3, -17.4, 33.2, -16.1)),      # Madeira, Porto Santo, Desertas
    ("PT", (36.8, -31.6, 39.9, -24.7)),      # Azores
    ("MA", (27.6, -13.3, 35.95, -5.9)),      # Atlantic Morocco (Tangier .. Tarfaya)
    ("XX", (35.9, -9.7, 51.2, 9.7)),         # Iberia + France + Corsica (country from admin polygons)
]

# Sea-spot coastal land model: land polygons are loaded inside these boxes.
LAND_BOXES = [
    (27.3, -18.5, 29.7, -13.2),
    (32.2, -17.5, 33.3, -16.0),
    (36.7, -31.7, 40.0, -24.6),
    (27.5, -13.5, 36.0, -5.6),
    (35.7, -10.0, 44.0, 4.6),
    (41.2, -5.3, 51.2, 9.7),
]

# Morocco: Atlantic only (west of Cap Spartel / the Strait).
MA_MAX_LON = -5.92

# Excluded enclaves/territories (s, w, n, e).
EXCLUDE_BOXES = [
    (36.10, -5.37, 36.16, -5.33),   # Gibraltar
    (35.86, -5.40, 35.93, -5.27),   # Ceuta (Mediterranean Africa, out of scope)
    (35.26, -2.97, 35.32, -2.92),   # Melilla
    (43.72, 7.40, 43.76, 7.44),     # Monaco
]

# Sea spots
SEA_DEDUPE_M = 500
SEA_MAX_COAST_M = {"fishing": 400, "pier": 300, "beach": 1500, "cape": 600, "harbour": 800,
                   "rivermouth": 2500, "bay": 1500}
UNNAMED_PIER_MIN_KM = 0.12      # unnamed piers/breakwaters shorter than this are ignored
UNNAMED_PIER_SPACING_M = 3000   # at most one unnamed pier spot per this radius

# Freshwater spots
RESERVOIR_MIN_KM2 = 0.5
LAKE_MIN_KM2 = 1.0
RIVER_MIN_KM = 50.0
RIVER_SAMPLE_KM = 20.0
FRESH_DEDUPE_M = 1500
SNAP_PLACE_KM = 4.0

# Orientation (exposure-window method, as Wavespot)
RAY_LEN_M = 20_000
RAY_STEP_DEG = 5
CAND_RADIUS_M = 600
CAND_OFFSET_M = 120
MAX_CANDS = 12
NORMAL_RADIUS_M = 600
NORMAL_MARGIN_DEG = 30
NORMAL_WEIGHT = 0.5
MIN_ISLAND_KM2 = 0.05
