# fishershour-data

Open-data pipeline of **Fishers Hour** (fishing forecast for sea, rivers and reservoirs in Spain, Portugal,
France and Atlantic Morocco). It builds a static JSON site, published with GitHub Pages at
`https://dprados.github.io/fishershour-data/v1/`. The contract with the iOS app is
`apps/fishershour/docs/data-schema.md` in the app repo (moving to `apps/fishershour/`): change it there first.

The fishing score (0–100 per hour and modality, §3 of the contract) is computed **only here**; the app
shows it, groups it into windows and calibrates it. Tides are published as EOT20 constants and the app
computes the curve.

## Layout

| Path | What |
|---|---|
| `fishershour_pipeline/build.py` | Forecast run: fetch all sources in parallel → blend → score → `site/v1` |
| `fishershour_pipeline/sources/` | `copernicus` (IBI + MEDSEA waves, SST, light attenuation), `ecmwf` (IFS 0.1°: WAM waves, wind, gusts, t2, msl, precipitation), `icon_eu` (DWD wind), `arome` (Météo-France wind, FR + ES), `hubeau` (French real-time flow + monthly normals), `miteco` (Spanish reservoirs, weekly), `glofas` (optional, needs `EWDS_API_KEY`) |
| `fishershour_pipeline/blend.py` | Per-spot series on `h` (hourly, 7 days) and `h3` (3-hourly, days 7–14) |
| `fishershour_pipeline/score.py` | Contract §3, literal and vectorised; `tests/test_score.py` has one test per factor and safety rule |
| `fishershour_pipeline/products.py` | Spot documents, index tiles, zone tops; tide speed/coefficient, light phase, flow ratio |
| `fishershour_pipeline/astro.py`, `wtemp.py`, `tides.py` | Sunrise/sunset ±90 min and moon phase; air→water temperature (Mohseni); harmonic tides (copied from Wavespot) |
| `fishershour_pipeline/validate.py`, `monitor.py`, `links.py`, `ids.py`, `tides_extract.py` | Site validation, daily health check, weekly regulation-link check, permanent ids, EOT20 extraction |
| `spots_build/` | Spot catalogue builder (run locally, see below) |
| `data/spots/sea.json`, `fresh.json`, `zones.json` | The catalogue (ODbL derivative) and zone/region names |
| `data/regions/rules.json` | Official regulation URL per region (curated, checked weekly) |
| `data/tides/eot20_constituents.json` | EOT20 constants per sea spot (`tides.yml`) |
| `data/fresh/hubeau_normals.json` | Monthly median flow per Hub'Eau station (refreshed every 120 days by the forecast run) |
| `data/published_ids.txt`, `data/aliases.json` | Ids are permanent: every id ever published, and retired id → current id |
| `data/inputs/wavespot_layer1.json` | Wavespot layer-1 coastal points reused as low-priority sea candidates |

## Sources and licences

| Source | Used for | Licence / attribution |
|---|---|---|
| Copernicus Marine IBI (`cmems_mod_ibi_wav_anfc_0.027deg_PT1H-i`, `…phy…3D_P1D-m`, `…bgc-optics…P1D-m` `kd`) | Atlantic waves, SST, clarity | Copernicus Marine licence (free, commercial use, attribution) |
| Copernicus Marine MEDSEA (`cmems_mod_med_wav_anfc_4.2km_PT1H-i`, `…phy-tem…P1D-m`, `…bgc-optics…P1D-m` `kd490`) | Mediterranean waves, SST, clarity | idem |
| ECMWF open data IFS 0.1° | waves for `h3`/fallback, wind, gusts, t2, msl, precipitation | CC BY 4.0 |
| DWD ICON-EU, Météo-France AROME | wind 0–120 h / 0–48 h | DWD open data; Licence Ouverte 2.0 |
| Hub'Eau hydrométrie (Eaufrance) | French flow `q` and normals `qn` | Licence Ouverte 2.0 |
| MITECO Boletín Hidrológico (ArcGIS layer `Embalses_Mapa` of the official dashboard) | Spanish reservoir % and weekly change | reuse allowed citing MITECO (aviso legal, Ley 37/2007); provisional data |
| GloFAS (CEMS, via EWDS) | 30-day river discharge forecast | CEMS-FLOODS licence, attribution; **disabled until `EWDS_API_KEY` exists** |
| EOT20 (DGFI-TUM) | tide constants | CC BY 4.0 |
| OpenStreetMap (Geofabrik extracts, land polygons) | spots, names, orientation | ODbL — `data/spots/*.json` is a derivative database under ODbL |
| Junta de Castilla y León, IDECyL WFS `pesca:pesca_cyl_tramos_v` | cotos, escenarios and AREC/ARE river stretches | LICENCIA-IGCYL-C: credit «©Junta de Castilla y León», data free at www.jcyl.es |
| DWD ICON-EU HSURF | model altitude for the salmonid (`fly`) heuristic | DWD open data |

Not used (licence or reliability): SNIRH, EFAS real time, embalses.net, BD CARTHAGE, Open-Meteo, WDPA,
FishBase, Copernicus Land LSWT (only ~13 Iberian/French lakes and needs a CDSE account; not worth it now),
Hub'Eau temperature (not real time: last values a year old).

### Key decisions
- **Waves**: Copernicus hourly by 2°×2° tiles (IBI for Atlantic spots, MEDSEA for Mediterranean ones,
  `x.med` in the catalogue); per-hour fallback to ECMWF WAM; `h3` = ECMWF × a coastal factor (Copernicus/
  ECMWF Hs over the last 48 h, clamped 0.3–1.5). Azores and the Sahara coast south of 26°N use ECMWF only.
- **Clarity `cl`**: from the daily diffuse attenuation coefficient (IBI `kd`, MED `kd490`):
  `cl = clip((ln 0.8 − ln kd) / (ln 0.8 − ln 0.04), 0, 1)` — kd ≤ 0.04 m⁻¹ (Secchi ≈ 40 m) → 1,
  kd 0.18 (Secchi ≈ 9 m) → 0.5, kd ≥ 0.8 (Secchi ≈ 2 m, «agua tomada») → 0. Held 72 h past the last day.
- **Tide**: `tide: null` when the spring range 2·(M2+S2) < 0.2 m (most of the Mediterranean).
  Coefficient = day range / spring range × 100.
- **Water temperature `wt`**: always estimated from air temperature (Mohseni logistic on a 3-day mean
  for rivers, 7-day for reservoirs/lakes; colder curve for salmonid rivers), `info.wtEstimated: true`.
- **Flow**: Hub'Eau station on the same river within 30 km (or any within 3 km) that currently publishes Q.
  `q` = observations only; the score holds the last observation 72 h, then GloFAS daily if available.
- **Reservoir level**: MITECO weekly %, `lvlTrend` = `Variacion_Porcentaje` (points per week); the last
  good table is kept in the state cache.

## Spot catalogue (`spots_build/`)

Built locally (the Geofabrik extracts are ~7 GB), committed as `data/spots/*.json`:

```sh
export FISHERSHOUR_SPOTS_CACHE=~/.cache/ardora      # big scratch, never inside the repo
# downloads: Geofabrik spain, canary-islands, portugal, france, morocco (.osm.pbf) into $CACHE/pbf,
#            land-polygons-split-4326 into $CACHE/land, MITECO layer, CyL WFS GeoJSON and ICON-EU HSURF into $CACHE/ext
PY="python -I"
$PY spots_build/extract_osm.py $CACHE/pbf spain canary-islands portugal france morocco
$PY spots_build/land.py $CACHE/land/land-polygons-split-4326/land_polygons.shp
$PY spots_build/build.py
```

- **Sea** (9.5k): OSM `leisure=fishing`, named `man_made=pier|breakwater` (unnamed breakwaters ≥ 300 m get
  «Espigón/Digue/Molhe de {pueblo}», one per 3 km), named harbours/marinas, named beaches, named capes
  (→ `rock`), river mouths (end of a named river ≥ 8 km within 2.5 km of the coast; `estuary` when the open
  sea window is < 60°), plus Wavespot layer-1 points. Dedupe 500 m by priority (fishing > pier > beach/mouth
  > harbour > cape > Wavespot). `facing` with Wavespot's exposure-window method on OSM land polygons.
- **Freshwater** (3.8k): OSM named reservoirs ≥ 0.5 km² and lakes ≥ 1 km² (joined to MITECO ids; 53 MITECO
  reservoirs without OSM polygon added at the MITECO point), named public `leisure=fishing` inland,
  Castilla y León cotos/escenarios/AREC/ARE stretches, and named rivers ≥ 50 km sampled every 20 km,
  named after the nearest town («Río Duero en Zamora», «Loire à Blois», «Rio Douro em …»).
- **Modes** by type (beach: surf, spin, kayak; rock: rock, spin, eging, spear; pier: spin, rock, eging;
  harbour: eging, spin, rock; rivermouth: spin, surf, kayak; estuary: spin, eging, kayak; river: fspin,
  bottom (+ fly first if salmonid); reservoir/lake: fspin, bottom (+ fly if high)). Salmonid heuristic:
  CyL `truchera`; north-Atlantic Iberia and Brittany/Normandy rivers; else ICON-EU altitude
  (FR ≥ 350 m, PT north ≥ 300 m, ES ≥ 800 m; lakes FR ≥ 900 m, ES/PT ≥ 1000 m).
- **Species** (`spots_build/heuristics.py`): climate zone (`atl`, `med`, `can` = Canarias/Madeira/Azores)
  × type, south-Atlantic extras (baila, herrera, palometa, sama, denton, anjova); freshwater by country,
  kind, salmonid flag and Ebro/Tajo basin (siluro).
- **Ids**: `{cc}-{slug(name)}` (`-2` on collision). A rebuild keeps the previous id of the same source or
  of a spot ≤ 150 m away of the same kind. Published ids never disappear (aliases).

## Workflows

- `forecast.yml` — 10:00 and 22:00 UTC + manual: tests, build, validate, record ids/normals, deploy Pages.
  State (cell mappings, last-good spots, Hub'Eau station list, MITECO last good) persists in `actions/cache`.
  If no spot gets fresh data the deploy is skipped and the previous site stays online.
- `monitor.yml` — daily 11:40 UTC: opens an issue if data is > 14 h old, a core source failed or > 20 % of
  spots are stale (`glofas: nokey` is not a failure).
- `links.yml` — weekly (Monday 06:15 UTC) and when `rules.json` changes: checks every regulation URL and
  opens/updates an issue when one is broken (bot-protected 403/429 count as «blocked», not broken).
- `spots.yml` — on catalogue changes: unique ids, valid modes/species/regions, no published id lost.
- `tides.yml` — on `sea.json` changes and twice a week (keeps the 2.3 GB EOT20 cache warm).

Secrets: `COPERNICUSMARINE_SERVICE_USERNAME`, `COPERNICUSMARINE_SERVICE_PASSWORD`; optional `EWDS_API_KEY`.

## Local run

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
set -a; source ../../../credentials.env; set +a          # Copernicus Marine account (never commit it)
.venv/bin/python -m fishershour_pipeline.build --out /tmp/fh/v1 --cache /tmp/fh-cache --work /tmp/fh-work --base-url ""
.venv/bin/python -m fishershour_pipeline.validate /tmp/fh/v1
.venv/bin/python -m pytest -q tests
```

Debug a few spots: `--only es-playa-de-el-palmar fr-loire-a-blois`; skip sources: `--skip arome glofas`.

## Size

~17 KB per spot (all series, 7 days hourly + `h3` to day 14) → ~230 MB for 13.3k spots plus index and
zones; the validator fails above 800 MB.
