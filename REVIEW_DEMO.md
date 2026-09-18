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
   Open each `outputs/<lake>/preview.png` and `map.html`. **Look at Hussain Sagar first** — it is big and unambiguous, so if the water outline is wrong there, something is off. Then Nalla Cheruvu: the log prints an acreage per year; HYDRAA's own number for this lake is **27 acres total, 14 encroached**. Whatever our max-extent acreage is, write it next to that. That comparison is the demo.

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

6. **Commit.** `git add -A && git commit -m "second review"`. The commit hash is now in every output row — show that.

## Tomorrow — the 8-minute flow

**Slide: the problem (1 min).** Fig. 1 and Fig. 2 from the figure set. One sentence: *enforcement runs on satellite evidence whose accuracy nobody has published, against boundaries that mostly don't exist and otherwise keep moving.*

**Slide: what we build (1 min).** Fig. 3. Point at the gate in the middle: *everything downstream is blocked until we've measured ourselves against the boundaries the government has already fixed.*

**Live: the register (30 s).** Open `configs/lakes.yaml`. *Adding a lake is a row. There is no lake in the code.*

**Live: Hussain Sagar (1 min).** `preview.png` then `map.html`. Toggle the water layers by year. *Sentinel-2, post-monsoon window, one scene per year, chosen by cloud cover over the lake — not over the whole tile. The index is MNDWI; the threshold is Otsu run inside the lake's own neighbourhood, because on a whole Hyderabad tile water is under 1% of pixels and Otsu's assumption breaks.*

**Live: Nalla Cheruvu (1.5 min).** `area_by_year.png` — the lake's life story. Then the map: the red line is the max observed extent; buildings inside it are red. Say the acreage, then say HYDRAA's 27/14. *That's our first validation point. It's one lake and it's not blind yet — but the pipeline gives a number that can be checked against an official one, and that is the whole project.*

**Live: provenance (30 s).** Click any building on the map → popup shows zone, distance, run id, **commit hash**. Open `run.json`. *Every row can be regenerated. If someone challenges a line in month five, we rerun this exact commit with this exact params hash.*

**Live: the gates (1 min).** Run the clean CSV, then the dropped-digit one. *The official boundary lives in scanned PDFs. OCR lies. A dropped digit moves a vertex 7,000 km. Three checks, and a sheet that fails any one goes to a human — it never becomes a boundary.*

**Live: abstention (1 min).** The lookup page. 40 m accuracy → refuses, with the reason in plain words. 5 m → verdict. *This is the rule that makes the system safe to exist. Every other tool in this space answers anyway.*

**Slide: what's next (30 s).** The table in README under "Honest status". Say the percentage out loud: *roughly a third of the build; the parts that are done are the parts everything else stands on.*

## Questions you will get, and the answers

**"Why not Google Earth Engine?"** GEE is in the plan for the 1984→ Landsat record and is applied for. Sentinel-2 from 2019 is enough to build and test every stage, and it runs with no account from any laptop — which is why you can see it working today.

**"Is the red line the FTL?"** No. It is the maximum water extent Sentinel-2 has observed since 2019 — one independent witness. The notified FTL is another. The 1971–72 survey sheet is a third. The paper measures the gap between them. Saying "this is the FTL" is exactly the mistake the project exists to stop.

**"Where do the buildings come from?"** OpenStreetMap today, because it needs no setup. Google Open Buildings V3 is the production source and drops in as a config switch. OSM coverage is partial and Open Buildings' South Asia recall is about 77%, so counts are reported with that caveat — never as complete.

**"How accurate is it?"** We don't know yet, and that is the honest answer this review should hear. Measuring it — blind, against the 765 notified polygons — is Slice 3. What we can say today: on a synthetic lake with known area the pipeline recovers it within 3% (`tests/test_water.py`), and cloud-covered years are rejected rather than silently shrinking the lake.

**"What if OCR of the PDF fails?"** Then the gates fail and the sheet goes to manual entry. The system is designed so that a bad extraction cannot become a boundary. The PDF reader is best-effort until we have real sheets to tune it on.

**"What about pre-2016 buildings / 1979?"** Cannot be dated with any free dataset; the abstract now says so. Water extent goes back to 1984 once GEE is live; MSS 1972–83 is context only, with error bars.

**"Is this legal to publish?"** Parcel geometry only, no owner identity, no assertion of illegality — only the measured relationship to a boundary, with that boundary's source and date, and an abstention rule. Ethics framing goes to the guide for sign-off this week.

## What's next (the remaining ~65%)

| slice | weeks | deliverable |
|---|---|---|
| Extract official polygons from HMDA sheets for 15 lakes | 2 | the first georeferenced FTL vector dataset |
| GEE pipeline for Landsat 1984→ | 2 | water extent back to 1984, MSS as labelled context |
| **Blind validation against notified polygons** | 3 | IoU, mean/worst gap in metres, area error — *the number* |
| Building dating 2016–2023 with cloud-gap bounds | 2 | first-appearance year per structure |
| Adversarial eval + conformal threshold | 3 | the published abstention threshold |
| Field visits (all three), review queue, public site | ongoing | ground truth; nothing public without `confirmed` |
