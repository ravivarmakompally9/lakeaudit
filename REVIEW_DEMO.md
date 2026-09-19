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

   **Lake identity, checked 18 Sep.** Three independent checks agree that the water body at **17.4884, 78.4184** (OSM way 129532522) is the Nalla Cheruvu HYDRAA surveyed in September 2024 — see the Q&A "Are you sure it's the same Nalla Cheruvu". The fourth, HYDRAA's survey nos. 66–69 against the cadastral map, is pending, so the register still says `seed_verified: false`; changing that is for whoever owns the next lake run.

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
   python -m lakeaudit validate --lake gurram_cheruvu --kind osm_shoreline
   python -m lakeaudit validation-summary
   python -m lakeaudit validate --lake hussain_sagar --kind notified_ftl --reference sample_data/ftl_vertices_ocr_dropped_digit.csv --perimeter 1858.4
   ```
   Expected: IoU 0.96 / 0.86 / 0.84; gap median 7 / 10 / 8 m, p95 36 / 24 / 20 m; summary ends with
   `published error rate … n_lakes: 0 — not yet measured`. The last command prints **REFUSED — not compared**.
   Gurram prints gap median 9 m, p95 30 m, plus `reference piece NOT in our line: 4.6 ac … 30 m from it`,
   and its summary row shows `4.6 ac` under **missed**.
   Open `outputs/hussain_sagar/validation_osm_shoreline.png` and `outputs/gurram_cheruvu/validation_osm_shoreline.png`.

6. **Commit.** `git add -A && git commit -m "second review"`. The commit hash is now in every output row — show that.

## Tomorrow — the 8-minute flow

**Slide: the problem (1 min).** Fig. 1 and Fig. 2 from the figure set. One sentence: *enforcement runs on satellite evidence whose accuracy nobody has published, against boundaries that mostly don't exist and otherwise keep moving.*

**Slide: what we build (1 min).** Fig. 3. Point at the gate in the middle: *everything downstream is blocked until we've measured ourselves against the boundaries the government has already fixed.*

**Live: the register (30 s).** Open `configs/lakes.yaml`. *Adding a lake is a row. There is no lake in the code.*

**Live: Hussain Sagar (45 s).** `preview.png` then `map.html`. Toggle the water layers by year. *Sentinel-2, post-monsoon window, one scene per year, chosen by cloud cover over the lake — not over the whole tile. The index is MNDWI; the threshold is Otsu run inside the lake's own neighbourhood, because on a whole Hyderabad tile water is under 1% of pixels and Otsu's assumption breaks.*

**Live: Nalla Cheruvu (2 min).** The honest-comparison lake. Identity: three checks agree, the cadastral check is pending (Q&A below) — say so if asked.

1. *Nothing on screen.* *HYDRAA went to this lake in September 2024 and said 27 acres total, 14 encroached — so about 13 left. Here is what one independent witness saw, using none of their data.* (27 − 14 is our arithmetic; HYDRAA did not publish a "remaining" figure.)
2. `area_by_year.png`. *Seven post-monsoon years. Open water in three of them: 9.1 acres in 2022, 10.0 in 2024, 12.7 in 2025. In the other four the sky was clear but the lakebed reads as land — dry or weed-covered; this index cannot tell which. We record "no open water". We don't guess.* (The y-axis starts at 9 acres, so the rise looks steeper than it is.)
3. `preview.png`, then `map.html`. *The red line is the union of those three years: 13.3 acres. It is the maximum water extent Sentinel-2 has observed since 2019 — one witness to the FTL, not the FTL.*
4. The comparison. *13.3 acres. HYDRAA's own figure for the lake's original water spread is 14 acres 32 guntas — about 14.8. OpenStreetMap maps 15.2. Our pixels are 20 metres: on this lake's 1.15 km shoreline, half a pixel of edge moves the area anywhere from 10.6 to 16.2 acres, and all three numbers sit inside that band. So this is a sanity check, not a validation. And the 27? Even a full pixel of error only reaches 19 acres. The gap between the water we see and the lake's 27 acres is real — not a resolution artefact.*
5. The zero, and what happened next. *OpenStreetMap footprints fetched on 18 September: 645 buildings around this lake, zero inside our line, zero in our buffer. That is not "no encroachment". Our line is the water's edge, and every building stands on land. HYDRAA counted more than 50 houses in the buffer of the 27-acre lake; our 9-metre ring, drawn from the water instead, contains none of the buildings OpenStreetMap maps. Then the lake changed. HYDRAA demolished the sheds in September 2024, drained and dug out the lake in spring 2025 — Sentinel-2 shows it empty on 28 March 2025 (S2B_43QHV_20250328_0_L2A) — and reopened it at 27 acres on 9 March 2026; its own target for the water was 22 acres. Our latest post-monsoon scene, 13 December 2025 (S2B_43QHV_20251213_1_L2A), falls between the dig-out and the reopening: 12.7 acres of water. Whether the lake was full then, we can't say; our next look is the October–December 2026 window. What we can say: a water witness counts water, not lake land — built on, landscaped, or not yet full. That is why the design is a set of witnesses: the notified polygon from the PDFs, the 1971–72 Survey of India sheet, Landsat back to 1988. Where they disagree is the signal.* (Slide: `docs/figures/nalla_restoration_2024_2026.png` — six Sentinel-2 scenes, April 2024 → April 2026, scene id on each panel. If asked about the pale ring around the lake: it appears between the December 2024 and December 2025 panels and hugs roughly the OSM outline (15.2 ac); we read it as new works around the lake, but the figure does not measure it, and HYDRAA's 27.14 ac is the lake's full area, not what this witness counts as open water. If `map.html` shows no buildings, the last run's OSM fetch failed — rerun the lake before presenting. "1988" is deliberate: the plan's "1984→" is the global TM record, but the first Landsat Level-2 scene over this lake is LT05_L2SP_144048_19880119_02_T1.)
6. The buffer. *Today the code sets the buffer from the observed extent: 5.4 hectares, "small", 9 metres. On HYDRAA's 27 acres it would be 30 metres. Set the buffer from what is left of a lake and encroachment shrinks its own protection — so the area class will come from the largest credible witness. And the whole 9-metre ring here is 2.6 acres: smaller than the roughly ±2.8 acres that half a pixel of edge moves this lake. At 20 m, this system cannot adjudicate a 9 m buffer on a lake this size. Saying that, with a measured number, is what this project is for.*
7. *One lake, not blind yet, one witness. But it gave a number we could check against an official one — and it told us exactly what it cannot see.*

Short on time: drop the first half of 6 (the area-class point); keep the 2.6 vs ±2.8 line. Where the numbers come from: MNDWI uses Sentinel-2's 20 m SWIR band; the edge figures are the `boundary.geojson` extent buffered ±10 m / ±20 m in UTM 44N (10.6–16.2 ac / 8.3–19.0 ac); the ring is the `buffer_ring` feature (2.58 ac).

**Live: provenance (30 s).** Click any building on the map → popup shows zone, distance, run id, **commit hash**. Open `run.json`. *Every row can be regenerated. If someone challenges a line in month five, we rerun this exact commit with this exact params hash.*

**Live: the gates (45 s).** Run the clean CSV, then the dropped-digit one. *The official boundary lives in scanned PDFs. OCR lies. A dropped digit moves a vertex 7,000 km. Three checks, and a sheet that fails any one goes to a human — it never becomes a boundary.*

**Live: how wrong can the line be (45 s).** In the app, open Hussain Sagar and scroll to *How wrong can this line be?*: the notified FTL box says *Not yet measured*, the OSM box shows IoU 0.96 · 7 m · 36 m. Then `validation_osm_shoreline.png` and `validation-summary`. (Restart `serve` after running `validate` the first time. After that, new records show without a restart.)
*This is the machinery for the number nobody publishes: IoU and the gap in metres, point by point along the line. The darker the dot, the further the reference line is from ours. Today the reference is OSM's shoreline, and that is not the FTL, so the tool itself prints "does not count". The bottom line reads n = 0: the published error rate starts when the first gated HMDA sheet goes in. A sheet that fails the gates is refused, never compared.* Point at the worst gap: *a channel narrower than one 20 m pixel. That is exactly the kind of limit we publish.*

**Live: two witnesses disagree about what the lake is (30 s).** In the app, open Gurram Cheruvu and scroll to the same box. It reads IoU 0.74 · 9 m · 31 m, then *Missed: a separate piece of this outline (4.6 ac) with no water in our line.* Then its `validation_osm_shoreline.png`: the pond is hatched, just west of the lake.
*OpenStreetMap draws Gurram Cheruvu as two pieces: the lake, and a 4.6-acre pond 30 metres west of it. In no post-monsoon image since 2019 did Sentinel-2 see open water in that pond. Along the lake itself the two lines agree to a median of 9 metres. We don't average the pond into that edge number, and we don't hide it either: it is listed on its own. Whether the pond is dry, weed-covered, built on, or was never part of the lake, this witness cannot say. That is what the notified polygon and a field visit are for. Where witnesses disagree is the signal.*

**Live: abstention (1 min).** The lookup page. 40 m accuracy → refuses, with the reason in plain words. 5 m → verdict. *This is the rule that makes the system safe to exist. Every other tool in this space answers anyway.*

**Slide: what's next (30 s).** The table in README under "Honest status". Say the percentage out loud: *roughly a third of the build; the parts that are done are the parts everything else stands on.*

## Questions you will get, and the answers

**"Why not Google Earth Engine?"** GEE is in the plan for the 1984→ Landsat record and is applied for. Sentinel-2 from 2019 is enough to build and test every stage, and it runs with no account from any laptop — which is why you can see it working today.

**"Is the red line the FTL?"** No. It is the maximum water extent Sentinel-2 has observed since 2019 — one independent witness. The notified FTL is another. The 1971–72 survey sheet is a third. The paper measures the gap between them. Saying "this is the FTL" is exactly the mistake the project exists to stop.

**"The government says half of Nalla Cheruvu was encroached and you found zero buildings?"** Our line is the water's edge; every building stands on land, so zero inside it is expected. HYDRAA counts 50-plus houses in the buffer of the 27-acre line; our 9 m ring is drawn from the 13-acre water edge and contains no building that OpenStreetMap maps (18 Sep fetch; OSM coverage is partial). Zero means "not visible to this witness", not "clean" — which is why no single witness decides, and why the envelope needs the notified polygon, the 1971–72 sheet and Landsat.

**"Was Nalla Cheruvu dry in 2019–2021?"** We don't know. The scenes were clear and the lakebed read as land (median MNDWI −0.24 to −0.36 inside the OSM outline). A floating weed mat also reads as land to this index. A field visit settles it; until then we record "no open water", not "dry".

**"Isn't 13.3 vs HYDRAA's figure just luck?"** Partly. HYDRAA's figure for the original water spread is 14.8 acres, and half a pixel of edge on this lake is about ±2.8 acres, which is why we call it a sanity check. The accuracy number comes from blind validation against notified polygons, not from one lake. What is not luck: 27 acres is outside even a full pixel of error (19 ac max).

**"Why is 2025 bigger than 2022?"** It coincides with HYDRAA's restoration: the lake was drained and desilted in spring 2025 (empty in the 28 March 2025 scene, S2B_43QHV_20250328_0_L2A) and refilled that monsoon. The timing matches, but imagery alone doesn't prove the cause, so we report the area, not a reason.

**"Are you sure it's the same Nalla Cheruvu HYDRAA surveyed?"** Three independent checks agree. OSM way 129532522 is the only water body of that name within 8 km. Google Maps' "Kukatpally Lake" pin is 45 m from our seed, and Deccan Chronicle calls it "Nalla Cheruvu, also known as Kukatpally lake". And Sentinel-2 shows our outline drained on 28 March 2025 (S2B_43QHV_20250328_0_L2A) and ringed by new works by 13 December 2025 (S2B_43QHV_20251213_1_L2A; no ring on 18 December 2024, S2B_43QHV_20241218_0_L2A), matching HYDRAA's restoration. Pending: HYDRAA places the lake at survey nos. 66–69 of Kukatpally village; we have not yet checked those against the cadastral map.

**"Where do the buildings come from?"** OpenStreetMap today, because it needs no setup. Google Open Buildings V3 is the production source and drops in as a config switch. OSM coverage is partial and Open Buildings' South Asia recall is about 77%, so counts are reported with that caveat — never as complete.

**"How accurate is it?"** Against the notified FTL we don't know yet, and that is the honest answer this review should hear. Measuring it (blind, against the 765 notified polygons) is Slice 3, and the measuring tool is already built and tested: it recovers a known 20 m offset as 20 m. What we can say today: against OSM's mapped shoreline, which is not the FTL, our line is within a median 7–10 m (p95 20–36 m) on three lakes. On a synthetic lake with known area the pipeline recovers it within 3% (`tests/test_water.py`), and cloud-covered years are rejected rather than silently shrinking the lake.

**"Gurram's gap is only 9 m, so why is its IoU just 0.74?"** IoU counts area, and the gap counts edge. Our line holds 29.0 acres and OSM's 38.7. Of the 9.7-acre difference, 4.6 is the detached pond we never saw water in, listed separately as *missed*; the rest is along the main lake's edge. Reporting the two separately is the point: "the edge is off by 9 m" and "a whole piece of the lake is missing" are different findings.

**"Mir Alam's gap is 521 m. Isn't your line just wrong there?"** It may be — that is what the number is for. OSM draws Mir Alam Tank at 350 acres; the most water Sentinel-2 has seen there across seven post-monsoon years, combined, is 278. The 72 acres OSM includes and we never saw as open water are not spread evenly: 91% of OSM's shoreline lies outside our line, a tenth of it more than 400 m away, worst at 17.3415 N, 78.4274 E (621 m). Half of OSM's shoreline is within 21 m of ours (the median gap). Even in the best year only two-thirds of the OSM outline read as water, and in 2024 and 2025 most of it read as land (median MNDWI −0.35 and −0.31, SWIR 0.14), though about 111 and 117 acres of open water were still seen. A floating weed mat reads as land to this index, exactly as at Nalla; whether that is what is here, this witness cannot say. And the disagreement sits in one place: a ~30-acre vegetated arm running south-west from the western lobe, which OSM draws as lake and where no open water was detected in any of seven post-monsoon scenes (median MNDWI −0.40 to −0.49, the same as the dry land beside it). Set that arm aside and 95% of OSM's shoreline is within 67 m of ours (median 15 m; 80% within 30 m). So 521 m means "two witnesses disagree by a lot here, and the water witness cannot see through weed" — not encroachment, and not that OSM is right. We publish the gap rather than tune it away. A notified polygon or a field visit decides.

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
