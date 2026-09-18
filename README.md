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

### How wrong can the line be? (validation)

```bash
python -m lakeaudit validate --lake hussain_sagar --kind osm_shoreline          # vs OSM's mapped shoreline
python -m lakeaudit validate --lake X --kind notified_ftl --reference sheet.vertices.csv --perimeter M
python -m lakeaudit validation-summary                                           # every comparison + the published error rate
```

Measures our max observed extent against a reference polygon: IoU, area error, and the gap in metres from
the reference line to ours (median, p95, max, share within 9 / 20 / 30 / 60 m). Writes
`outputs/<lake>/validation_<kind>.json` and `.png`. The reference is read only after a run and is never an
input to the pipeline. A notified FTL is accepted only as a vertex table that passes the three gates; a table
that fails is refused, never compared. **Only gated notified FTLs on real (non-synthetic) runs count toward
the published error rate**, and today that count is zero. The OSM comparison is a check on the method, not
the number: OSM is not the FTL, and it is also our seed.

Against OSM (runs of 18 Sep 2026): Hussain Sagar IoU 0.96, gap median 7 m / p95 36 m · Durgam Cheruvu 0.86,
10 / 24 m · Nalla Cheruvu (Kukatpally) 0.84, 8 / 20 m. The worst gaps are channels and inlets narrower than
one 20 m Sentinel-2 pixel.

The app's Lake screen shows these under *How wrong can this line be?*, with the notified-FTL box first. It stays
*Not yet measured* until a gated sheet is compared. `/lakes` and `/lake/{id}` carry them as `validation` (current run only).

### The web app

`python -m lakeaudit serve`, then open http://localhost:8000. One panel, three screens:

- **Home** — *Use my location*, or *Enter coordinates* (latitude, longitude, accuracy; *Pick on map*; *Check*), and the list of lakes.
- **Result** — the nearest lake, one verdict (inside the extent / in the buffer ring / outside) or **Cannot answer** when the
  position is less certain than the distance to the line (*Try again*, *Pick on map*), the two numbers behind it, *View lake*.
- **Lake** — observed extent, buffer width, years with water, buildings inside / in buffer; *Check a location here*, *Zoom to lake*.

Layers: Streets / Satellite, Observed extent, Buffer ring, Buildings (click one for its zone, run and commit). Every screen has
a collapsed *Data source* (run, commit, params). Synthetic lakes are hidden when real ones exist. The old form is at `/classic`.

| endpoint | returns |
|---|---|
| `GET /lakes` | one card per lake a run produced (never invented): centroid/bbox, extent, buffer, per-year status, structures, provenance, `official_ftl: {available:false}` |
| `GET /lake/{id}` | the card + `run.json` + boundary, water-by-year and structures GeoJSON |
| `GET /nearby?lat&lon` | every lake by distance (m, UTM) from the point to its observed-extent edge, with `point_zone` |
| `GET /lookup?lat&lon&accuracy_m[&lake_id]` | verdict or abstention; `lake_id` judges against that lake instead of the nearest |

Screenshots for the deck (server running): `python scripts/screenshots.py` → `docs/screenshots/`.

### Tests

```bash
pip install playwright && python -m playwright install chromium    # once, for the browser tests
python -m pytest -q tests               # 51: gates, water extraction, validation, API, and every button in the web app
```

`tests/test_ui.py` clicks every button and control in a real browser (desktop and phone sizes) against a throwaway
`--offline` data tree, with no network, and fails on any console error, any control under 44 px, or sideways scrolling.

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
| Map-first web app (`/lakes`, `/lake/{id}`, `/nearby`, `/lookup?lake_id`), desktop + phone | done; every button covered by `tests/test_ui.py` |
| Official notified-FTL layer in the app | not started — needs extracted polygons (`official_ftl.available: false`) |
| Multi-year MSS/Landsat (1984→) via GEE | not started — awaiting GEE approval |
| Validation machinery (IoU, gap in metres, gated references, stale-run check) | done, tested; run against OSM on 3 lakes |
| Blind validation against notified polygons — the published error rate | not started — needs extracted official polygons (n = 0 today) |
| Building dating 2016–2023 (Open Buildings 2.5D Temporal) | not started |
| Adversarial eval + conformal threshold | not started |
| Review queue UI | schema only |

## What the boundary is, and is not

The red line is the **maximum post-monsoon water extent observed by Sentinel-2 since 2019**. It is one independent witness to where the lake reaches when full. It is **not** the notified FTL, and nothing this code produces is a legal determination. The point of the project is to measure how far this witness sits from the official line — and to publish that number.
