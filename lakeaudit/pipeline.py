"""One lake, one run, end to end. This is the unit of work the orchestrator loops over."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import yaml
from shapely.geometry import shape

from .provenance import new_run
from .register import load_register, resolve_seed, Lake
from .imagery import make_grid, best_scene_for_year
from .water import rasterize_roi, extract_water, WaterResult, NO_SCENE_METHODS
from .boundary import max_observed_extent
from .structures import fetch_osm_buildings, classify, zone_summary
from .store import write_outputs
from .report import render_preview, render_area_chart, render_map
from .geo import bbox_pad_deg, area_m2, m2_to_acres, utm_crs_for, to_utm, to_wgs

log = logging.getLogger("lakeaudit")


def _roi_bbox(lake: Lake, pad_m: float):
    minx, miny, maxx, maxy = lake.seed_polygon.bounds
    c = lake.seed_polygon.centroid
    utm = utm_crs_for(c.x, c.y)
    b_utm = to_utm(lake.seed_polygon, utm).buffer(pad_m)
    return to_wgs(b_utm, utm).bounds, to_wgs(b_utm, utm)


def run_lake(repo_root: Path, lake_id: str, *, offline: bool = False, years: list[int] | None = None) -> Path:
    cfg_paths = [repo_root / "configs" / "lakes.yaml", repo_root / "configs" / "thresholds.yaml"]
    lakes, rule = load_register(cfg_paths[0])
    cfg = yaml.safe_load(cfg_paths[1].read_text())
    lake = next((l for l in lakes if l.id == lake_id), None)
    if lake is None:
        raise SystemExit(f"unknown lake id '{lake_id}'. Known: {[l.id for l in lakes]}")
    years = years or cfg["imagery"]["years"]
    prov = new_run(repo_root, cfg_paths)
    out_dir = repo_root / "outputs" / lake.id
    t0 = time.time()
    source = "sample" if offline else "stac"
    log.info("run %s  lake=%s  source=%s  commit=%s  params=%s", prov.run_id, lake.id, source, prov.code_commit, prov.params_hash)
    log.info("register: seed (%.5f, %.5f) verified=%s  years=%s", lake.seed_lat, lake.seed_lon, lake.seed_verified, years)

    # ---------------------------------------------------------------- 1. seed
    footprints = []
    truth = {}
    if offline:
        from .sample_data import sample_lake
        lake.seed_polygon, scenes, footprints, truth = sample_lake(lake.seed_lat, lake.seed_lon, years)
        lake.seed_source = "sample"
        grid = scenes[0].grid
        roi_poly_bounds, roi_poly = _roi_bbox(lake, cfg["imagery"]["roi_pad_m"])
    else:
        lake = resolve_seed(lake, cfg["structures"]["overpass_url"])
        log.info("seed: %s (%s) area≈%.1f ha", lake.seed_source, lake.seed_osm_ids, area_m2(lake.seed_polygon) / 1e4)
        roi_poly_bounds, roi_poly = _roi_bbox(lake, cfg["imagery"]["roi_pad_m"])
        grid = make_grid(roi_poly_bounds, cfg["imagery"]["work_resolution_m"])
    roi_mask = rasterize_roi(roi_poly, grid)
    seed_mask = rasterize_roi(lake.seed_polygon, grid)

    # ---------------------------------------------------------------- 2. water by year
    waters: list[WaterResult] = []
    rejected: dict = {}
    if offline:
        for sc in scenes:
            from .imagery import roi_cloud_fraction
            frac = roi_cloud_fraction(sc, roi_mask, cfg["water"]["scl_cloud_classes"])
            sc.extra["roi_cloud_fraction"] = round(float(frac), 4)
            if frac > cfg["water"]["max_roi_cloud_fraction"]:
                rejected[str(sc.year)] = [{"scene_id": sc.scene_id, "roi_cloud_fraction": round(float(frac), 4)}]
                waters.append(WaterResult(sc.year, sc.scene_id, sc.acquired, float("nan"), "rejected_cloud", None, 0.0, float(frac), 0, "ROI too cloudy — year recorded as insufficient data"))
                continue
            waters.append(extract_water(sc, roi_mask, seed_mask, cfg["water"], cfg["boundary"]["simplify_tolerance_m"]))
    else:
        for yr in years:
            try:
                sc, rej = best_scene_for_year(roi_poly_bounds, grid, yr, cfg["imagery"], roi_mask,
                                              cfg["water"]["max_roi_cloud_fraction"], cfg["water"]["scl_cloud_classes"])
            except Exception as e:
                log.warning("year %s: imagery search/read failed: %s", yr, e)
                sc, rej = None, [{"error": f"{type(e).__name__}: {e}"}]
            if rej:
                rejected[str(yr)] = rej
            if sc is None:
                waters.append(WaterResult(yr, "", "", float("nan"), "no_clear_scene", None, 0.0, float("nan"), 0,
                                          "no scene with a clear ROI in the post-monsoon window — insufficient data"))
                log.warning("year %s: no clear scene (%d rejected)", yr, len(rej))
                continue
            gs, ss = sc.extra.get("green_stats", {}), sc.extra.get("swir_stats", {})
            log.info("year %s  bands: green p50=%s  swir p50=%s  | %s", yr,
                     None if gs.get("p50") is None else round(gs["p50"], 4),
                     None if ss.get("p50") is None else round(ss["p50"], 4),
                     (sc.extra.get("offset_handling") or {}).get("green", "no offset in metadata"))
            wr = extract_water(sc, roi_mask, seed_mask, cfg["water"], cfg["boundary"]["simplify_tolerance_m"])
            waters.append(wr)
            log.info("year %s  %s  %s  area=%.2f ac  thr=%.3f (%s)  cloud=%.2f", yr, sc.acquired, sc.scene_id,
                     m2_to_acres(wr.area_m2), wr.threshold, wr.threshold_method, wr.roi_cloud_fraction)

    # ---------------------------------------------------------------- 3. boundary
    boundary = max_observed_extent(waters, rule, cfg["boundary"]["simplify_tolerance_m"])
    if boundary is None:
        log.error("no usable water extent in any year — cannot derive a boundary")

    # ---------------------------------------------------------------- 4. structures
    structs = []
    if boundary is not None:
        if not offline:
            try:
                bb = boundary.buffer_outer_wgs.buffer(0.002).bounds  # ~200 m beyond the ring
                footprints = fetch_osm_buildings(bb, cfg["structures"]["overpass_url"])
            except Exception as e:
                log.warning("Overpass failed: %s — structures skipped this run", e)
                footprints = []
        structs = classify(footprints, boundary.extent_wgs, boundary.buffer_ring_wgs, cfg["structures"]["source"] if not offline else "sample")

    # ---------------------------------------------------------------- 5. store + report
    good_years = [w.year for w in waters if w.polygon_wgs is not None]
    # Two different absences: no usable scene (cloud) vs a clear scene where nothing
    # touching the seed reads as water (lakebed reads as land). Don't conflate them.
    no_scene = [w.year for w in waters if w.polygon_wgs is None and w.threshold_method in NO_SCENE_METHODS]
    summary = {
        "years_requested": years,
        "years_with_data": good_years,
        "years_insufficient": no_scene,
        "years_no_open_water": [w.year for w in waters if w.polygon_wgs is None and w.year not in no_scene],
        "boundary": boundary.summary() if boundary else None,
        "structures": zone_summary(structs) if structs else None,
        "elapsed_s": round(time.time() - t0, 1),
        **({"sample_truth": truth} if truth else {}),
    }
    write_outputs(out_dir, prov, lake, waters, boundary, structs, rejected, summary, source)
    render_preview(out_dir, lake.name, source)
    render_area_chart(out_dir, lake.name, source)
    render_map(out_dir, lake.name, source)
    log.info("done in %.1fs → %s", time.time() - t0, out_dir)
    return out_dir
