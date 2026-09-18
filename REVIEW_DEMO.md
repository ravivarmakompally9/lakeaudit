# Second review — what to run tonight, what to show tomorrow

## Tonight (≈ 45 minutes, in this order)

1. **Install and check the network**
   ```bash
   pip install -r requirements.txt
   python -m lakeaudit doctor
   ```
   All four lines should say `OK`. If any says `FAIL`, you can still demo — see step 4.

2. **Run three real lakes** (each 1–3 minutes; Sentinel-2 is read straight from the public AWS archive)
   ```bash
   python -m lakeaudit run --lake hussain_sagar
   python -m lakeaudit run --lake nalla_cheruvu_kukatpally
   python -m lakeaudit run --lake durgam_cheruvu
   ```
   Open each `outputs/<lake>/preview.png` and `map.html`. **Look at Hussain Sagar first** — it is big and unambiguous, so if the water outline is wrong there, something is off. Then Nalla Cheruvu. HYDRAA's own number for this lake is **27 acres total, 14 encroached**.

   **First, confirm it is the right lake.** The register has `seed_verified: false`: it was matched by name to OSM way 129532522, and "Nalla Cheruvu" is a common name. Check that the water body at **17.4884, 78.4184** is the one HYDRAA surveyed in September 2024 (a news photo or HYDRAA's press note against Google Maps). If it is, set `seed_verified: true` and commit. If it isn't, drop the HYDRAA comparison from the demo and say why — the comparison is worthless against the wrong lake.

   Expected output: open water 2022 **9.1 ac** · 2024 **10.0** · 2025 **12.7**; 2019–21 and 2023 are clear scenes (0 % ROI cloud) with no open water — the lakebed reads as land; max extent **13.3 ac** (5.4 ha → small class → 9 m ring); structures **0 inside · 0 in buffer · 645 outside**. The zero is expected, not a bug — the script below explains why, and it is the most important thing this lake shows.

   If the OSM seed lands on the wrong water body (the `seed_verified: false` lakes), edit the seed coordinates in `configs/lakes.yaml` and rerun. That is a one-line change, and saying so in the review is itself a point.

3. **Start the API and try the abstention rule**
   ```bash
   python -m lakeaudit serve        # then open http://localhost:8000
   ```
   Type any point near a lake edge with GPS accuracy 40 m → it refuses. Same point with accuracy 5 m → verdict. Screenshot both.
   Tested point: **lat 17.430068, lon 78.486859** (Hussain Sagar east shore, 20 m outside the extent) →
   40 m: ABSTAINED · 5 m: `in_buffer` (30 m ring). The page's default (lake centre) is 518 m from the edge, so it won't abstain.

4. **Run the offline fallback once, so it exists**
   ```bash
   python -m lakeaudit run --lake nalla_cheruvu_kukatpally --offline
   ```
   Wait — this overwrites the real outputs for that lake. So do it as `--lake alwal_cheruvu --offline` instead, and keep it as the backup. If review-room WiFi dies, the synthetic run demonstrates every stage of the mechanism with a **SYNTHETIC SAMPLE** banner on it. Never present it as a result.

5. **Run the gates and the tests, screenshot the terminal**
   ```bash
   python -m lakeaudit gates --csv sample_data/ftl_vertices_clean.csv --perimeter 1858.4
   python -m lakeaudit gates --csv sample_data/ftl_vertices_ocr_dropped_digit.csv --perimeter 1858.4
   python -m pytest -q tests
   ```

5b. **Measure the three real lakes against OSM, then show the refusal path**
   ```bash
   python -m lakeaudit validate --lake hussain_sagar --kind osm_shoreline
   python -m lakeaudit validate --lake durgam_cheruvu --kind osm_shoreline
   python -m lakeaudit validate --lake nalla_cheruvu_kukatpally --kind osm_shoreline
   python -m lakeaudit validation-summary
   python -m lakeaudit validate --lake hussain_sagar --kind notified_ftl --reference sample_data/ftl_vertices_ocr_dropped_digit.csv --perimeter 1858.4
   ```
   Expected: IoU 0.96 / 0.86 / 0.84; gap median 7 / 10 / 8 m, p95 36 / 24 / 20 m; summary ends with
   `published error rate … n_lakes: 0 — not yet measured`. The last command prints **REFUSED — not compared**.
   Open `outputs/hussain_sagar/validation_osm_shoreline.png`.

6. **Commit.** `git add -A && git commit -m "second review"`. The commit hash is now in every output row — show that.

## Tomorrow — the 8-minute flow

**Slide: the problem (1 min).** Fig. 1 and Fig. 2 from the figure set. One sentence: *enforcement runs on satellite evidence whose accuracy nobody has published, against boundaries that mostly don't exist and otherwise keep moving.*

**Slide: what we build (1 min).** Fig. 3. Point at the gate in the middle: *everything downstream is blocked until we've measured ourselves against the boundaries the government has already fixed.*

**Live: the register (30 s).** Open `configs/lakes.yaml`. *Adding a lake is a row. There is no lake in the code.*

**Live: Hussain Sagar (45 s).** `preview.png` then `map.html`. Toggle the water layers by year. *Sentinel-2, post-monsoon window, one scene per year, chosen by cloud cover over the lake — not over the whole tile. The index is MNDWI; the threshold is Otsu run inside the lake's own neighbourhood, because on a whole Hyderabad tile water is under 1% of pixels and Otsu's assumption breaks.*

**Live: Nalla Cheruvu (2 min).** The honest-comparison lake. Only run this beat if tonight's lake-identity check passed.

1. *Nothing on screen.* *HYDRAA went to this lake in September 2024 and said 27 acres total, 14 encroached — so about 13 left. Here is what one independent witness saw, using none of their data.* (27 − 14 is our arithmetic; HYDRAA did not publish a "remaining" figure.)
2. `area_by_year.png`. *Seven post-monsoon years. Open water in three of them: 9.1 acres in 2022, 10.0 in 2024, 12.7 in 2025. In the other four the sky was clear but the lakebed reads as land — dry or weed-covered; this index cannot tell which. We record "no open water". We don't guess.* (If the chart still labels those years "insufficient data", correct it out loud: the scenes were clear. The y-axis starts at 9 acres, so the rise looks steeper than it is.)
3. `preview.png`, then `map.html`. *The red line is the union of those three years: 13.3 acres. It is the maximum water extent Sentinel-2 has observed since 2019 — one witness to the FTL, not the FTL.*
4. The comparison. *13.3 against about 13 — consistent. But our pixels are 20 metres. On this lake's 1.15 km shoreline, moving the edge by half a pixel moves the area anywhere from 10.6 to 16.2 acres. HYDRAA's 13 and OpenStreetMap's mapped 15.2 both sit inside that band. So this is a sanity check, not a validation. And the 27? Even a full pixel of error only reaches 19 acres. The 14 lost acres are not a resolution artefact. They are real — and this witness cannot see them.*
5. The zero. *645 buildings around this lake. Zero inside our line, zero in the buffer. That is not "no encroachment". It is exactly what a witness that only sees water since 2019 should report: whatever stands on those 14 acres was there before our record starts. That is why the design is a set of witnesses, not one detector — the notified polygon from the PDFs, the 1971–72 Survey of India sheet, Landsat from 1984. Where they disagree is the signal.*
6. The buffer. *Today the code sets the buffer from the observed extent: 5.4 hectares, "small", 9 metres. On HYDRAA's 27 acres it would be 30 metres. Set the buffer from what is left of a lake and encroachment shrinks its own protection — so the area class will come from the largest credible witness. And the whole 9-metre ring here is 2.6 acres: smaller than the roughly ±2.8 acres that half a pixel of edge moves this lake. At 20 m, this system cannot adjudicate a 9 m buffer on a lake this size. Saying that, with a measured number, is what this project is for.*
7. *One lake, not blind yet, one witness. But it gave a number we could check against an official one — and it told us exactly what it cannot see.*

Short on time: drop the first half of 6 (the area-class point); keep the 2.6 vs ±2.8 line. Where the numbers come from: MNDWI uses Sentinel-2's 20 m SWIR band; the edge figures are the `boundary.geojson` extent buffered ±10 m / ±20 m in UTM 44N (10.6–16.2 ac / 8.3–19.0 ac); the ring is the `buffer_ring` feature (2.58 ac).

**Live: provenance (30 s).** Click any building on the map → popup shows zone, distance, run id, **commit hash**. Open `run.json`. *Every row can be regenerated. If someone challenges a line in month five, we rerun this exact commit with this exact params hash.*

**Live: the gates (45 s).** Run the clean CSV, then the dropped-digit one. *The official boundary lives in scanned PDFs. OCR lies. A dropped digit moves a vertex 7,000 km. Three checks, and a sheet that fails any one goes to a human — it never becomes a boundary.*

**Live: how wrong can the line be (45 s).** In the app, open Hussain Sagar and scroll to *How wrong can this line be?*: the notified FTL box says *Not yet measured*, the OSM box shows IoU 0.96 · 7 m · 36 m. Then `validation_osm_shoreline.png` and `validation-summary`. (Restart `serve` after running `validate` the first time. After that, new records show without a restart.)
*This is the machinery for the number nobody publishes: IoU and the gap in metres, point by point along the line. The darker the dot, the further the reference line is from ours. Today the reference is OSM's shoreline, and that is not the FTL, so the tool itself prints "does not count". The bottom line reads n = 0: the published error rate starts when the first gated HMDA sheet goes in. A sheet that fails the gates is refused, never compared.* Point at the worst gap: *a channel narrower than one 20 m pixel. That is exactly the kind of limit we publish.*

**Live: abstention (1 min).** The lookup page. 40 m accuracy → refuses, with the reason in plain words. 5 m → verdict. *This is the rule that makes the system safe to exist. Every other tool in this space answers anyway.*

**Slide: what's next (30 s).** The table in README under "Honest status". Say the percentage out loud: *roughly a third of the build; the parts that are done are the parts everything else stands on.*

## Questions you will get, and the answers

**"Why not Google Earth Engine?"** GEE is in the plan for the 1984→ Landsat record and is applied for. Sentinel-2 from 2019 is enough to build and test every stage, and it runs with no account from any laptop — which is why you can see it working today.

**"Is the red line the FTL?"** No. It is the maximum water extent Sentinel-2 has observed since 2019 — one independent witness. The notified FTL is another. The 1971–72 survey sheet is a third. The paper measures the gap between them. Saying "this is the FTL" is exactly the mistake the project exists to stop.

**"The government says half of Nalla Cheruvu is encroached and you found zero buildings?"** This witness sees water from 2019 on; the land was taken before that. Zero means "not visible to this witness", not "clean". That is exactly why no single witness gets to decide, and why the envelope needs the notified polygon, the 1971–72 sheet and Landsat 1984→.

**"Was Nalla Cheruvu dry in 2019–2021?"** We don't know. The scenes were clear and the lakebed read as land (median MNDWI −0.24 to −0.36 inside the OSM outline). A floating weed mat also reads as land to this index. A field visit settles it; until then we record "no open water", not "dry".

**"Isn't 13.3 vs 13 just luck?"** Partly. Half a pixel of edge on this lake is about ±2.8 acres, which is why we call it a sanity check. The accuracy number comes from blind validation against notified polygons, not from one lake. What is not luck: 27 acres is outside even a full pixel of error (19 ac max), so the loss is real.

**"Why is 2025 bigger than 2022?"** Rainfall, weed clearance and restoration are all candidates, and imagery alone cannot separate them. We report the area; we don't claim a cause.

**"Are you sure it's the same Nalla Cheruvu HYDRAA surveyed?"** Matched to OSM way 129532522 by name, then checked against _(fill in tonight: the source you used)_. If that check fails, say so — don't present the comparison.

**"Where do the buildings come from?"** OpenStreetMap today, because it needs no setup. Google Open Buildings V3 is the production source and drops in as a config switch. OSM coverage is partial and Open Buildings' South Asia recall is about 77%, so counts are reported with that caveat — never as complete.

**"How accurate is it?"** Against the notified FTL we don't know yet, and that is the honest answer this review should hear. Measuring it (blind, against the 765 notified polygons) is Slice 3, and the measuring tool is already built and tested: it recovers a known 20 m offset as 20 m. What we can say today: against OSM's mapped shoreline, which is not the FTL, our line is within a median 7–10 m (p95 20–36 m) on three lakes. On a synthetic lake with known area the pipeline recovers it within 3% (`tests/test_water.py`), and cloud-covered years are rejected rather than silently shrinking the lake.

**"What if OCR of the PDF fails?"** Then the gates fail and the sheet goes to manual entry. The system is designed so that a bad extraction cannot become a boundary. The PDF reader is best-effort until we have real sheets to tune it on.

**"What about pre-2016 buildings / 1979?"** Cannot be dated with any free dataset; the abstract now says so. Water extent goes back to 1984 once GEE is live; MSS 1972–83 is context only, with error bars.

**"Is this legal to publish?"** Parcel geometry only, no owner identity, no assertion of illegality — only the measured relationship to a boundary, with that boundary's source and date, and an abstention rule. Ethics framing goes to the guide for sign-off this week.

## What's next (the remaining ~65%)

| slice | weeks | deliverable |
|---|---|---|
| Extract official polygons from HMDA sheets for 15 lakes | 2 | the first georeferenced FTL vector dataset |
| GEE pipeline for Landsat 1984→ | 2 | water extent back to 1984, MSS as labelled context |
| **Blind validation against notified polygons** (tool built; needs the sheets) | 3 | IoU, mean/worst gap in metres, area error — *the number* |
| Building dating 2016–2023 with cloud-gap bounds | 2 | first-appearance year per structure |
| Adversarial eval + conformal threshold | 3 | the published abstention threshold |
| Field visits (all three), review queue, public site | ongoing | ground truth; nothing public without `confirmed` |
