"""python -m lakeaudit <command>

  run       --lake ID [--offline] [--years 2021 2022]   one lake, end to end
  run-all   [--offline]                                   every lake in the register
  gates     --csv vertices.csv [--perimeter M]            run the 3 validity gates on a vertex table
  pdf       --pdf sheet.pdf                               extract a vertex table from an HMDA PDF, then run gates
  validate  --lake ID --kind KIND [--reference PATH]      measure our line against a reference polygon (IoU, gap in metres)
  validation-summary                                      every validation on disk + the published error rate (so far)
  serve     [--port 8000]                                 start the lookup API + demo page
  load-db   --lake ID --dsn postgresql://...              ingest a run into PostGIS
  doctor                                                  check that the public data sources are reachable
  debug     --lake ID --year YYYY                         draw the raw green / swir / MNDWI / SCL for one scene
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _log():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def cmd_run(a):
    from .pipeline import run_lake
    out = run_lake(ROOT, a.lake, offline=a.offline, years=a.years)
    run = json.loads((out / "run.json").read_text())
    print(json.dumps(run["summary"], indent=2))
    print(f"\nopen: {out / 'map.html'}\n      {out / 'preview.png'}\n      {out / 'area_by_year.png'}")


def cmd_run_all(a):
    from .register import load_register
    from .pipeline import run_lake
    lakes, _ = load_register(ROOT / "configs" / "lakes.yaml")
    for l in lakes:
        try:
            run_lake(ROOT, l.id, offline=a.offline, years=a.years)
        except Exception as e:
            logging.getLogger("lakeaudit").error("lake %s failed: %s", l.id, e)


def cmd_gates(a):
    from .ftl_extract.gates import run_gates, vertices_from_rows
    rows = list(csv.DictReader(open(a.csv)))
    rep = run_gates(vertices_from_rows(rows), a.perimeter)
    print(json.dumps(rep.as_dict(), indent=2))
    print("\nRESULT:", "PASS — admitted as a boundary candidate" if rep.passed else "FAIL — routed to manual entry")
    sys.exit(0 if rep.passed else 2)


def cmd_pdf(a):
    from .ftl_extract.pdf_table import extract_vertex_rows
    from .ftl_extract.gates import run_gates, vertices_from_rows
    rows, unparsed, meta = extract_vertex_rows(Path(a.pdf))
    print(json.dumps({"meta": meta, "rows_parsed": len(rows), "rows_unparsed": len(unparsed)}, indent=2))
    if not rows:
        print("no coordinate table recognised — send this sheet to manual entry"); sys.exit(2)
    out = Path(a.pdf).with_suffix(".vertices.csv")
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["n", "lat", "lon", "dist_next_m"]); w.writeheader(); w.writerows(rows)
    print("vertices →", out)
    rep = run_gates(vertices_from_rows(rows), a.perimeter or meta.get("declared_perimeter_m"))
    print(json.dumps(rep.as_dict(), indent=2))
    print("\nRESULT:", "PASS" if rep.passed else "FAIL — routed to manual entry")


def cmd_validate(a):
    from .validate import RefusedReference, load_reference, validate_lake
    path = Path(a.reference) if a.reference else None
    if path is None:
        if a.kind != "osm_shoreline":
            print(f"--reference is required for kind {a.kind}"); sys.exit(2)
        path = ROOT / "cache" / "seeds" / f"{a.lake}.geojson"
        if not path.exists():
            print(f"no cached OSM seed for {a.lake} at {path}"); sys.exit(2)
    try:
        ref = load_reference(path, a.kind, perimeter_m=a.perimeter, date=a.date)
    except RefusedReference as e:
        if e.gates:
            print(json.dumps(e.gates, indent=2))
        print("\nREFUSED — not compared:\n  " + "\n  ".join(e.reasons))
        sys.exit(2)
    rec = validate_lake(ROOT, a.lake, ref, step_m=a.step)
    m, g = rec["metrics"], rec["metrics"]["gap_ref_to_ours_m"]
    print(json.dumps(m, indent=2))
    print(f"\n{rec['lake_name']}: ours vs {a.kind}")
    print(f"  IoU {m['iou']:.3f} · area {m['area_ours_acres']:.1f} ac vs {m['area_ref_acres']:.1f} ac ({m['area_error_pct']:+.1f} %)")
    print(f"  reference line -> our line: median {g['median']:.0f} m · p95 {g['p95']:.0f} m · max {g['max']:.0f} m"
          f" · {100 * m['ref_line_within_m']['30']:.0f} % within 30 m · {100 * m['ref_line_outside_ours']:.0f} % lies outside our extent")
    if m["no_part_overlaps"]:
        print("  NO piece of the reference overlaps our line — wrong lake, or no water seen; gaps cover everything")
    for u in m["ref_parts_unmatched"] if not m["no_part_overlaps"] else []:
        print(f"  reference piece NOT in our line: {u['acres']:.1f} ac at ({u['lat']}, {u['lon']}), "
              f"{u['distance_to_other_line_m']:.0f} m from it — listed, not counted as an edge gap")
    for u in m["ours_parts_unmatched"]:
        print(f"  our piece NOT in the reference: {u['acres']:.1f} ac at ({u['lat']}, {u['lon']}), "
              f"{u['distance_to_other_line_m']:.0f} m from it")
    print("  " + ("COUNTS toward the published error rate" if rec["counts_toward_published_error_rate"]
                  else f"does NOT count toward the published error rate: {rec['why_not_counted']}"))
    out = ROOT / "outputs" / a.lake
    print(f"\nwrote {out / f'validation_{a.kind}.json'}\n      {out / f'validation_{a.kind}.png'}")


def cmd_validation_summary(a):
    from .validate import summarize
    s = summarize(ROOT)
    print(f"{'lake':28s} {'reference':14s} {'IoU':>6s} {'area%':>7s} {'med m':>6s} {'p95 m':>6s} {'max m':>6s} {'<=30m':>6s} {'missed':>9s}  counted")
    for r in s["rows"]:
        flag = "yes" if r["counted"] else "no"
        flag += "  (STALE: lake re-run since)" if r["stale"] else ""
        flag += "  [SYNTHETIC]" if r["synthetic"] else ""
        missed = f"{r['ref_unmatched_acres']:.1f} ac" if r["ref_unmatched_parts"] else "–"
        print(f"{r['lake_id']:28s} {r['kind']:14s} {r['iou']:6.3f} {r['area_error_pct']:+7.1f} {r['gap_median_m']:6.0f} "
              f"{r['gap_p95_m']:6.0f} {r['gap_max_m']:6.0f} {100 * r['within_30m']:5.0f}% {missed:>9s}  {flag}")
    print("(missed = reference pieces with no water in our line; not in the gap columns)")
    print("\nby reference kind (current runs only):")
    print(json.dumps(s["by_kind"], indent=2))
    print("\npublished error rate (gated notified FTL, real data only):")
    print(json.dumps(s["published_error_rate"], indent=2, ensure_ascii=False))


def cmd_doctor(a):
    import requests, yaml
    cfg = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())
    checks = [
        ("STAC catalogue (earth-search)", cfg["imagery"]["stac_url"], "get"),
        ("Sentinel-2 COG bucket", "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/44/Q/LE/2024/11/", "get"),
        ("Overpass (OSM)", "https://overpass-api.de/api/status", "get"),
        ("OSM tiles (map background)", "https://tile.openstreetmap.org/12/2942/1868.png", "get"),
    ]
    ok = True
    for name, url, _ in checks:
        try:
            r = requests.get(url, timeout=20)
            good = r.status_code < 500
            print(f"  {'OK  ' if good else 'FAIL'}  {name:34s} HTTP {r.status_code}")
            ok &= good
        except Exception as e:
            ok = False
            print(f"  FAIL  {name:34s} {type(e).__name__}: {e}")
    try:
        from pystac_client import Client
        c = Client.open(cfg["imagery"]["stac_url"])
        n = len(list(c.search(collections=[cfg["imagery"]["collection"]], bbox=[78.39, 17.48, 78.41, 17.50],
                              datetime="2024-10-15/2024-12-31", max_items=3).items()))
        print(f"  {'OK  ' if n else 'FAIL'}  STAC search over Kukatpally, Oct-Dec 2024: {n} scenes")
        ok &= n > 0
    except Exception as e:
        ok = False
        print(f"  FAIL  STAC search: {type(e).__name__}: {e}")
    print("\nverdict:", "all sources reachable — run with real data" if ok else "something is blocked — use --offline for the demo and fix network after")
    sys.exit(0 if ok else 1)


def cmd_debug(a):
    import numpy as np, yaml
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    from .register import load_register, resolve_seed
    from .imagery import make_grid, search_scenes, read_scene
    from .water import rasterize_roi, mndwi
    from .pipeline import _roi_bbox
    lakes, _ = load_register(ROOT / "configs" / "lakes.yaml")
    cfg = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())
    lake = next(l for l in lakes if l.id == a.lake)
    lake = resolve_seed(lake, cfg["structures"]["overpass_url"])
    bounds, roi = _roi_bbox(lake, cfg["imagery"]["roi_pad_m"])
    grid = make_grid(bounds, cfg["imagery"]["work_resolution_m"])
    items = search_scenes(bounds, a.year, cfg["imagery"])
    print(f"seed={lake.seed_source} ids={lake.seed_osm_ids}  grid {grid.width}x{grid.height} @ {grid.res_m} m EPSG:{grid.crs.to_epsg()}")
    print(f"{len(items)} candidate scenes; using clearest: {items[0].id if items else None}")
    if not items:
        sys.exit(2)
    sc = read_scene(items[0], grid, cfg["imagery"], a.year)
    roi_mask = rasterize_roi(roi, grid); seed_mask = rasterize_roi(lake.seed_polygon, grid)
    idx = mndwi(sc.green, sc.swir)
    print("green href :", sc.extra["green_href"]); print("raster:bands:", sc.extra["green_raster_bands"])
    print("offset     :", sc.extra.get("offset_handling"))
    for name, arr in (("green", sc.green), ("swir", sc.swir), ("mndwi", idx)):
        v = arr[roi_mask & np.isfinite(arr)]; sv = arr[seed_mask & np.isfinite(arr)]
        print(f"{name:6s} ROI n={v.size:6d} p5={np.percentile(v,5) if v.size else float('nan'):.4f} p50={np.median(v) if v.size else float('nan'):.4f} p95={np.percentile(v,95) if v.size else float('nan'):.4f}"
              f"   SEED n={sv.size:5d} p50={np.median(sv) if sv.size else float('nan'):.4f}")
    if sc.scl is not None:
        u, c = np.unique(sc.scl[roi_mask], return_counts=True); print("SCL classes in ROI:", dict(zip(u.tolist(), c.tolist())))
    print(f"roi_mask px={int(roi_mask.sum())}  seed_mask px={int(seed_mask.sum())}  frac(mndwi>0 in ROI)={(idx[roi_mask]>0).mean():.3f}")
    fig, axs = plt.subplots(1, 4, figsize=(16, 4.2))
    for ax, (name, arr, kw) in zip(axs, (("green", sc.green, {}), ("swir16", sc.swir, {}), ("MNDWI", idx, dict(vmin=-1, vmax=1, cmap="RdBu")), ("SCL", sc.scl, dict(cmap="tab20", vmin=0, vmax=11)))):
        if arr is None: continue
        im = ax.imshow(arr, **kw); ax.set_title(name); ax.contour(seed_mask, levels=[0.5], colors="k", linewidths=0.8); plt.colorbar(im, ax=ax, fraction=0.04)
    fig.suptitle(f"{lake.name} — {sc.scene_id} — black outline = seed polygon"); fig.tight_layout()
    out = ROOT / "outputs" / lake.id; out.mkdir(parents=True, exist_ok=True)
    p = out / f"debug_{a.year}.png"; fig.savefig(p, dpi=110); print("wrote", p)


def cmd_serve(a):
    import uvicorn
    uvicorn.run("lakeaudit.api:app", host="0.0.0.0", port=a.port, reload=False)


def cmd_load_db(a):
    from .store import load_postgis
    load_postgis(ROOT / "outputs" / a.lake, a.dsn)
    print("loaded", a.lake)


def main(argv=None):
    _log()
    p = argparse.ArgumentParser(prog="lakeaudit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    r = sp.add_parser("run"); r.add_argument("--lake", required=True); r.add_argument("--offline", action="store_true")
    r.add_argument("--years", nargs="*", type=int); r.set_defaults(fn=cmd_run)
    ra = sp.add_parser("run-all"); ra.add_argument("--offline", action="store_true"); ra.add_argument("--years", nargs="*", type=int); ra.set_defaults(fn=cmd_run_all)
    g = sp.add_parser("gates"); g.add_argument("--csv", required=True); g.add_argument("--perimeter", type=float); g.set_defaults(fn=cmd_gates)
    pd = sp.add_parser("pdf"); pd.add_argument("--pdf", required=True); pd.add_argument("--perimeter", type=float); pd.set_defaults(fn=cmd_pdf)
    v = sp.add_parser("validate"); v.add_argument("--lake", required=True)
    v.add_argument("--kind", required=True, choices=["notified_ftl", "osm_shoreline", "field_gps", "survey_1971", "synthetic"])
    v.add_argument("--reference", help="vertex table (.csv, gated) or polygon (.geojson); default for osm_shoreline: the cached seed")
    v.add_argument("--perimeter", type=float, help="perimeter printed on the sheet, metres (gate 3)")
    v.add_argument("--date", help="the reference's own date (notification / survey / walk)")
    v.add_argument("--step", type=float, default=5.0, help="sample spacing along the reference line, metres")
    v.set_defaults(fn=cmd_validate)
    sp.add_parser("validation-summary").set_defaults(fn=cmd_validation_summary)
    s = sp.add_parser("serve"); s.add_argument("--port", type=int, default=8000); s.set_defaults(fn=cmd_serve)
    sp.add_parser("doctor").set_defaults(fn=cmd_doctor)
    dbg = sp.add_parser("debug"); dbg.add_argument("--lake", required=True); dbg.add_argument("--year", type=int, default=2023); dbg.set_defaults(fn=cmd_debug)
    d = sp.add_parser("load-db"); d.add_argument("--lake", required=True); d.add_argument("--dsn", required=True); d.set_defaults(fn=cmd_load_db)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
