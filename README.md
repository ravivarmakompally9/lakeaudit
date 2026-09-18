# lakeaudit

Satellite-derived lake boundary reconstruction for Hyderabad, built to measure its own error.

**Batch 23MPCS-H8 · Vardhaman College of Engineering · Major Project 2026-27**
*Design, Implementation and Reliability Evaluation of a Satellite-Derived Full Tank Level Reconstruction System with a Published Abstention Threshold for Lake Encroachment Screening*

## What it does today

```
configs/lakes.yaml ──► register ──► OSM seed polygon
                                        │
   public Sentinel-2 archive (AWS) ──► one post-monsoon scene per year
                                        │  MNDWI → Otsu inside the ROI → water polygon
                                        ▼
                              max observed extent + statutory buffer ring
                                        │
   building footprints (OSM) ──► inside extent / in buffer / outside, distance to line
                                        │
                              GeoJSON + CSV, every row stamped with
                              run_id · code commit · params hash · scene ids
                                        │
                          /lookup API that ABSTAINS when GPS error > distance
                                        │
                          map-first web app at /  (Leaflet, served locally)
```

Separately, `lakeaudit/ftl_extract/` recovers the *official* FTL vertex table from HMDA's scanned notification PDFs and passes it through three validity gates (plausible? closed? perimeter matches the sheet?) before it is allowed to become a polygon.

Everything is driven by two config files. Adding a lake is adding a row to `configs/lakes.yaml`. No GEE account, no API keys.

## Run it

Python 3.10–3.12 recommended (3.11 tested).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m lakeaudit doctor                              # are the public data sources reachable?
python -m lakeaudit run --lake nalla_cheruvu_kukatpally # real Sentinel-2 + OSM, ~1–3 min
python -m lakeaudit run --lake hussain_sagar
python -m lakeaudit serve                               # http://localhost:8000  (map app: lakes, location check that abstains)
```

Outputs land in `outputs/<lake_id>/`:

| file | what |
|---|---|
| `preview.png` | static picture: water by year, extent, buffer, buildings by zone |
| `area_by_year.png` | the lake's post-monsoon area, year by year, with missing years marked |
| `map.html` | interactive Leaflet map, Leaflet inlined — opens offline; only basemap tiles need internet |
| `water_by_year.csv/.geojson` | one row per year: area, threshold, scene id, cloud fraction, provenance |
| `boundary.geojson` | max observed extent + buffer ring + OSM seed |
| `structures.geojson` | every footprint with zone, distance, provenance |
| `rejected_scenes.json` | every scene we looked at and refused, and why |
| `run.json` | provenance manifest + summary |

No network? `python -m lakeaudit run --lake <id> --offline` runs the identical code on a synthetic shrinking lake. Everything it produces is stamped **SYNTHETIC SAMPLE** — it demonstrates the mechanism, never a result.

### The validity gates

```bash
python -m lakeaudit gates --csv sample_data/ftl_vertices_clean.csv --perimeter 1858.4               # PASS
python -m lakeaudit gates --csv sample_data/ftl_vertices_ocr_dropped_digit.csv --perimeter 1858.4   # FAIL gate 1
python -m lakeaudit gates --csv sample_data/ftl_vertices_ocr_shifted_point.csv --perimeter 1858.4   # FAIL gate 3
python -m lakeaudit pdf --pdf path/to/hmda_sheet.pdf                                                # try a real sheet
```

### The web app

`python -m lakeaudit serve`, then open http://localhost:8000. The map is the interface: every processed lake's
observed extent, buffer ring, water by year and structures by zone; "Use my location" (or a typed / clicked point)
runs `/nearby` then `/lookup`, shows **how sure we are** (GPS accuracy vs distance to the line) *before* any verdict,
and refuses when the line runs through the uncertainty circle. The notified-FTL layer is shown but disabled: no
dataset yet, and nothing stands in for it. Synthetic lakes are drawn only with the basemap hidden. The original
single-form page is at `/classic`.

| endpoint | returns |
|---|---|
| `GET /lakes` | one card per lake a run produced: register fields, centroid/bbox, extent acres, area class + buffer, per-year status, structure counts, provenance, `official_ftl: {available:false, …}` |
| `GET /lake/{id}` | the card + `run.json` + boundary, water-by-year and structures GeoJSON |
| `GET /nearby?lat&lon` | every lake by distance (m, UTM) from the point to its observed-extent edge, with `point_zone` |
| `GET /lookup?lat&lon&accuracy_m[&lake_id]` | verdict or abstention; `lake_id` judges against that lake instead of the nearest |

Leaflet 1.9.4 is vendored in `lakeaudit/static/leaflet/` (npm tarball, SRI-verified). UI walkthrough at desktop and
phone sizes with a mocked GPS fix — fails on any console error or a touch target under 44 px:

```bash
pip install playwright && python -m playwright install chromium
python scripts/ui_walkthrough.py        # server must be running; screenshots -> docs/screenshots/
```

### Tests

```bash
python -m pytest -q tests               # 16: gates, water extraction, API contract (needs httpx)
```

### PostGIS (optional, for the production store)

```bash
docker run --name lakeaudit-db -e POSTGRES_PASSWORD=lakeaudit -p 5432:5432 -d postgis/postgis:16-3.4
psql postgresql://postgres:lakeaudit@localhost:5432/postgres -f db/schema.sql
pip install psycopg2-binary
python -m lakeaudit load-db --lake nalla_cheruvu_kukatpally --dsn postgresql://postgres:lakeaudit@localhost:5432/postgres
```

## Honest status

| component | state |
|---|---|
| Lake register, config-driven | done |
| OSM seed resolution with labelled fallback | done |
| Sentinel-2 post-monsoon scene selection, ROI-level cloud rejection | done |
| MNDWI + per-lake Otsu + degenerate-histogram fallback | done |
| Max observed extent + area-class buffer | done |
| Building footprints (OSM) → zone + distance | done (Open Buildings V3 loader: next) |
| Provenance on every row; PostGIS schema + loader | done |
| FTL vertex extraction: three validity gates | done, tested |
| FTL vertex extraction: PDF table reader | best-effort, needs real sheets to tune |
| Lookup API with abstention rule | done |
| Map-first web app (`/lakes`, `/lake/{id}`, `/nearby`, `/lookup?lake_id`), desktop + phone | done; Playwright walkthrough, 16 tests |
| Official notified-FTL layer in the app | not started — layer shown disabled until polygons are extracted |
| Multi-year MSS/Landsat (1984→) via GEE | not started — awaiting GEE approval |
| Blind validation against notified polygons | not started — needs extracted official polygons |
| Building dating 2016–2023 (Open Buildings 2.5D Temporal) | not started |
| Adversarial eval + conformal threshold | not started |
| Review queue UI | schema only |

## What the boundary is, and is not

The red line is the **maximum post-monsoon water extent observed by Sentinel-2 since 2019**. It is one independent witness to where the lake reaches when full. It is **not** the notified FTL, and nothing this code produces is a legal determination. The point of the project is to measure how far this witness sits from the official line — and to publish that number.
