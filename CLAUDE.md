# lakeaudit — project context for Claude Code

Read this fully before doing anything. It is the handoff from the design/build session.

## What this is

B.Tech major project (Batch 23MPCS-H8, Vardhaman College of Engineering, guide Dr. Ruqqaiya Begum).
Title: *Design, Implementation and Reliability Evaluation of a Satellite-Derived Full Tank Level
Reconstruction System with a Published Abstention Threshold for Lake Encroachment Screening.*

Hyderabad lakes are being built over. Legality depends on the FTL (Full Tank Level) line — the water's
extent when the lake is full — plus a buffer ring (30 m if the lake is ≥10 ha, else 9 m). The government
has notified FTLs for only ~1 in 5 lakes, the notified ones get re-drawn under litigation, and **nobody in
this space — government, commercial, academic — publishes an error rate**. Our contribution is the error
rate and an abstention threshold, built on a reproducible reconstruction pipeline.

## Non-negotiable framing (say this exactly in any text you write)

- The red line this code produces is the **maximum post-monsoon water extent observed by Sentinel-2
  since 2019**. It is ONE WITNESS to the FTL. It is **never** called "the FTL" and is **never** a legal
  determination. Outputs are a shortlist for human inspection.
- Parcel geometry only. Never join to owner identity. Never assert a building is "illegal" — only its
  measured geometric relationship to a boundary, with that boundary's source and date.
- Every output row carries `run_id`, `code_commit`, `params_hash`, scene ids. Never remove that.
- The API abstains when GPS accuracy > distance to boundary. Never remove that.
- Synthetic (`--offline`) output is stamped SYNTHETIC and is never presented as a result (API `/lookup`
  adds `synthetic: true` and prefixes the plain-language verdict).

## Second review is TOMORROW (Sept 19, 2026). Goal: demo ~35% working.

What must work on the laptop tonight, in order:
1. `python -m lakeaudit run --lake hussain_sagar` → ~1,010–1,065 ac/year, max extent
   ≈1,081 ac (437 ha); agrees with OSM's mapped shoreline (454 ha) within 4 %. Red outline
   traces the lake in `outputs/hussain_sagar/preview.png` and `map.html`.
2. `python -m lakeaudit run --lake nalla_cheruvu_kukatpally` — HYDRAA's official figure for this lake
   is **27 acres total, 14 encroached**; our max-extent acreage is compared against that in the demo.
   Verified: max observed extent **13.3 ac** (2022 9.1 · 2024 10.0 · 2025 12.7; 2019–21, 2023 no open
   water at MNDWI>0). ≈ the 13 ac HYDRAA says remain; S2-since-2019 cannot witness land built over
   earlier — that is the case for Landsat 1984→. Area class from observed extent = small (9 m); on
   HYDRAA's 27 ac (10.9 ha) it would be 30 m.
3. `python -m lakeaudit run --lake durgam_cheruvu` — verified: 68–78 ac/yr (2023: 54), max extent **81 ac**,
   both basins (cable bridge at ~17.4315 splits them; needs the OSM seed, way 28131043, to keep both).
   Record has 65 / 83 / 160+ ac candidates; ours is one witness, it does not pick between them.
4. `python -m lakeaudit serve` → http://localhost:8000 — show abstention (accuracy 40 → refuses; 5 → verdict).
5. `python -m lakeaudit gates --csv sample_data/ftl_vertices_clean.csv --perimeter 1858.4` (PASS) and
   `..._ocr_dropped_digit.csv` (FAIL gate 1).
6. `python -m pytest -q tests` → 10 passed (pytest now in requirements.txt).
7. `python -m lakeaudit run --lake alwal_cheruvu --offline` as the no-WiFi backup.
Full script + Q&A: `REVIEW_DEMO.md`. Status table: `README.md`.

## Environment on this Mac

- Python is `python3`; the venv with all packages lives in `~/Downloads/lakeaudit/.venv` (the FIRST
  unzip). Activate with `source ~/Downloads/lakeaudit/.venv/bin/activate` — then `python` works.
  If working from a different folder copy, still activate that same venv (or `pip install -r requirements.txt`
  into a new one).
- No GEE account yet (applied for). Everything runs off public Sentinel-2 COGs on AWS via STAC
  (earth-search v1) and OSM Overpass. `python -m lakeaudit doctor` checks reachability.
- No Docker/PostGIS needed for the demo; schema is in `db/schema.sql`, loader in `store.load_postgis`.

## Bugs already found and fixed on real data (don't re-introduce)

1. Overpass returned 406 to a bare `requests` call → now sends a User-Agent and falls back to mirrors
   (`register.UA`, `register.OVERPASS_MIRRORS`). Seed for Hussain Sagar resolves to OSM relation 2833155.
2. **earth-search COGs are already BOA-offset-corrected, but STAC `raster:bands` still says
   `offset: -0.1`.** Applying it again made water reflectance negative and flipped MNDWI. `imagery.read_scene`
   decides ONCE per scene from the item property `earthsearch:boa_offset_applied` (true on every item
   checked, baselines 05.00–05.11). The earlier per-band pixel heuristic offset SWIR but not green over
   mostly-land ROIs and turned Kukatpally into "water" — don't bring it back. Log line:
   `metadata offset -0.1 NOT applied (earthsearch:boa_offset_applied=true, baseline 05.xx)`.
3. Otsu is run inside a per-lake ROI, never scene-global (bimodality fails when water <1 % of tile);
   degenerate Otsu falls back to threshold 0.0 and says so in `threshold_method`.
4. OSM multipolygon relations with an outer ring split across several OPEN ways were closed way-by-way,
   drawing chords → truncated seed (Hussain Sagar 242 ha instead of 454 ha) → 600 m ROI clipped the
   water mask (930 ac instead of 1,081 ac). `register._rings` now stitches ways (linemerge+polygonize)
   and subtracts inner rings. Check: water extent must stay well inside the ROI edge.
5. Small lakes in mostly-land ROIs: Otsu split land from land (thr −0.24…−0.31) and flooded ~300 ac.
   `water.min_water_threshold: 0.0` clamps it (`otsu_clamped_floor`). If nothing touches the seed, the
   year is recorded as no open water — never the largest blob elsewhere in the ROI.

## Repo map

```
configs/lakes.yaml        15 lakes; seed_lat/lon are SEEDS snapped to OSM; seed_verified:false = unchecked
configs/thresholds.yaml   every tunable; hashed into params_hash
lakeaudit/register.py     load register, resolve OSM seed polygon (fallback circle, labelled)
lakeaudit/imagery.py      STAC search, post-monsoon window, WarpedVRT window reads onto 20 m UTM grid
lakeaudit/water.py        MNDWI, ROI-windowed Otsu, mask -> polygon, per-year WaterResult
lakeaudit/boundary.py     max observed extent + buffer ring by area class
lakeaudit/structures.py   OSM buildings -> inside_ftl / in_buffer / outside, distance to extent
lakeaudit/provenance.py   run_id, git commit, params hash
lakeaudit/store.py        GeoJSON/CSV writer + optional PostGIS loader
lakeaudit/report.py       preview.png, area_by_year.png, map.html (Leaflet)
lakeaudit/api.py          FastAPI /lookup with abstention; demo page at /
lakeaudit/pipeline.py     run_lake(): the unit of work
lakeaudit/cli.py          run | run-all | gates | pdf | serve | doctor | debug | load-db
lakeaudit/ftl_extract/    gates.py (3 validity gates, tested) + pdf_table.py (best-effort HMDA PDF reader)
lakeaudit/sample_data.py  synthetic shrinking lake for --offline
tests/                    10 tests: gates catch OCR damage; water recovers area within 3 %; cloudy year rejected
outputs/<lake_id>/        run.json, water_by_year.csv/.geojson, boundary.geojson, structures.geojson, preview.png, map.html
```

## What is NOT built yet (say so honestly in the review)

Landsat 1984→ via GEE (awaiting approval) · official-polygon extraction from real HMDA PDFs (reader is
best-effort, no real sheets yet) · **blind validation against notified polygons — the paper's number** ·
building dating 2016–2023 (Open Buildings 2.5D Temporal) · adversarial eval + conformal threshold ·
review-queue UI (schema only) · Open Buildings V3 loader (OSM is the placeholder).

## Working style the user wants

Direct, concise, one focused step at a time. Give a single command, wait for the output, then the next.
Don't rebuild things that work. If a lake's outline lands on the wrong water body, the fix is the
`seed_lat/seed_lon` row in `configs/lakes.yaml`, not code. Commit after each working step so
`code_commit` in outputs is real (`git add -A && git commit -m "..."`).

## Known things that may bite tonight

- OSM seeds are cached in `cache/seeds/<lake>.geojson` (committed) — runs no longer need Overpass for
  seeds. Delete a file to refetch. Buildings still come from Overpass live.
- Overpass rate-limits: if `fetch_osm_buildings` fails, the run still completes with zero structures and
  logs a warning — rerun after a minute.
- `seed_verified: false` lakes: OSM name regex may match the wrong water body; check `preview.png`.
- Years with no clear post-monsoon scene are recorded as "insufficient data" (`summary.years_insufficient`)
  — that is correct behaviour, not a bug. A clear scene where nothing at the seed reads as water is a
  different case: "no open water detected" (`summary.years_no_open_water`). The chart marks both, differently.
- Two large `lakeaudit*` folders exist in `~/Downloads` from repeated unzips; work in the newest (v3).
