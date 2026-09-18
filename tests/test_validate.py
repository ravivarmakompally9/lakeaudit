"""The error-rate machinery recovers known answers: a line shrunk by 20 m reads as a 20 m gap,
a 30 m shift reads as a 30 m worst gap, a detached piece with no water in it is listed rather
than read as a 200 m gap, a vertex table that fails a gate is never compared, and nothing
synthetic or non-official counts toward the published error rate."""
import math
import csv
import json
import shutil
from pathlib import Path

import pytest
from shapely.affinity import translate
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from lakeaudit.geo import m2_to_acres, to_utm, to_wgs, utm_crs_for
from lakeaudit.pipeline import run_lake
from lakeaudit.validate import RefusedReference, compare, load_reference, summarize, validate_lake

ROOT = Path(__file__).resolve().parents[1]
LON, LAT = 78.47, 17.42
UTM = utm_crs_for(LON, LAT)
C = to_utm(Point(LON, LAT), UTM)


def _circle(r_m, dx_m=0.0):
    return to_wgs(translate(C.buffer(r_m, quad_segs=128), xoff=dx_m), UTM)


def test_identical_lines_have_no_gap():
    m, _ = compare(_circle(300), _circle(300))
    assert m["iou"] == pytest.approx(1.0, abs=1e-4)
    assert m["gap_ref_to_ours_m"]["max"] < 0.5 and m["area_error_pct"] == pytest.approx(0.0, abs=0.01)


def test_line_20m_inside_reads_as_20m_gap():
    m, _ = compare(_circle(280), _circle(300))          # ours falls 20 m short all round
    g = m["gap_ref_to_ours_m"]
    assert g["median"] == pytest.approx(20.0, abs=0.5) and g["max"] == pytest.approx(20.0, abs=0.5)
    assert m["ref_line_outside_ours"] == 1.0
    assert m["ref_line_within_m"]["9"] == 0.0 and m["ref_line_within_m"]["30"] == 1.0
    assert m["area_error_pct"] < 0 and m["iou"] == pytest.approx((280 / 300) ** 2, abs=0.002)


def test_30m_shift_reads_as_30m_worst_gap():
    m, _ = compare(_circle(300, dx_m=30), _circle(300))
    assert m["gap_ref_to_ours_m"]["max"] == pytest.approx(30.0, abs=0.5)
    assert 0 < m["ref_line_outside_ours"] < 1 and m["iou"] < 0.95


def test_detached_reference_piece_is_listed_not_blended_into_gaps():
    # Gurram's shape: the reference has the lake plus a pond 240 m off its shore; we saw water only in the lake
    m, arrays = compare(_circle(300), unary_union([_circle(300), _circle(60, dx_m=600)]))
    g = m["gap_ref_to_ours_m"]
    assert g["max"] < 0.5 and m["ref_line_within_m"]["9"] == 1.0          # the lake's edge agrees exactly
    [u] = m["ref_parts_unmatched"]
    assert u["distance_to_other_line_m"] == pytest.approx(240.0, abs=1.0)
    assert u["acres"] == pytest.approx(m2_to_acres(math.pi * 60 ** 2), rel=0.01)
    assert m["ref_unmatched_acres"] == u["acres"] and m["ref_parts"] == 2 and not m["no_part_overlaps"]
    assert m["iou"] < 1.0 and m["area_error_pct"] < 0                   # the missed pond still costs IoU and area
    assert m["ours_parts_unmatched"] == [] and len(arrays["ref_unmatched_wgs"]) == 1


def test_our_extra_piece_is_listed_and_no_overlap_falls_back_and_flags():
    m, _ = compare(unary_union([_circle(300), _circle(50, dx_m=-500)]), _circle(300))
    assert len(m["ours_parts_unmatched"]) == 1 and m["gap_ours_to_ref_m"]["max"] < 0.5
    assert m["ref_parts_unmatched"] == []
    far, _ = compare(_circle(100, dx_m=2000), _circle(100))              # wrong lake: nothing overlaps
    assert far["no_part_overlaps"] and far["iou"] == 0.0
    assert far["gap_ref_to_ours_m"]["median"] > 1700                     # measured anyway, not hidden
    assert len(far["ref_parts_unmatched"]) == 1


def test_gate_failure_is_refused_never_compared():
    with pytest.raises(RefusedReference) as e:
        load_reference(ROOT / "sample_data" / "ftl_vertices_ocr_dropped_digit.csv", "notified_ftl", perimeter_m=1858.4)
    assert e.value.gates["gate1_plausible"] is False
    with pytest.raises(RefusedReference):                # notified FTL only through the gated vertex table
        load_reference(ROOT / "cache" / "seeds" / "hussain_sagar.geojson", "notified_ftl")
    ok = load_reference(ROOT / "sample_data" / "ftl_vertices_clean.csv", "notified_ftl", perimeter_m=1858.4)
    assert ok.gates["passed"] and ok.polygon_wgs.is_valid


def test_synthetic_run_never_counts_and_stale_runs_drop_out(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "configs", root / "configs")
    run_lake(root, "alwal_cheruvu", offline=True)
    out = root / "outputs" / "alwal_cheruvu"
    ext = next(f for f in json.loads((out / "boundary.geojson").read_text())["features"]
               if f["properties"]["layer"] == "max_observed_extent")
    ours = shape(ext["geometry"])
    utm = utm_crs_for(ours.centroid.x, ours.centroid.y)
    ref = to_wgs(to_utm(ours, utm).buffer(15).simplify(2), utm)      # a "sheet" 15 m outside our line
    csv_p = tmp_path / "sheet.vertices.csv"
    with csv_p.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["n", "lat", "lon"])
        for i, (x, y) in enumerate(list(ref.exterior.coords)[:-1], 1):
            w.writerow([i, round(y, 7), round(x, 7)])

    rec = validate_lake(root, "alwal_cheruvu", load_reference(csv_p, "notified_ftl"))
    assert rec["reference"]["gates"]["passed"]
    assert rec["metrics"]["gap_ref_to_ours_m"]["median"] == pytest.approx(15.0, abs=2.0)
    assert rec["synthetic"] and rec["counts_toward_published_error_rate"] is False
    assert rec["why_not_counted"] == "synthetic data"
    assert {"run_id", "code_commit", "params_hash", "scene_ids"} <= set(rec["ours"])
    assert (out / "validation_notified_ftl.png").exists()

    s = summarize(root)
    assert s["published_error_rate"]["n_lakes"] == 0 and not s["rows"][0]["stale"]
    run = json.loads((out / "run.json").read_text()); run["run_id"] = "run_newer"
    (out / "run.json").write_text(json.dumps(run))
    s = summarize(root)
    assert s["rows"][0]["stale"] and s["by_kind"] == {}
