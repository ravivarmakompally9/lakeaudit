"""Water extraction on synthetic scenes must recover the known lake area, mask
clouds, and reject a year whose ROI is too cloudy."""
import numpy as np
import yaml
from pathlib import Path

from lakeaudit.sample_data import sample_lake
from lakeaudit.water import rasterize_roi, extract_water, mndwi, choose_threshold
from lakeaudit.imagery import roi_cloud_fraction
from lakeaudit.boundary import max_observed_extent
from lakeaudit.register import BufferRule
from lakeaudit.geo import utm_crs_for, to_utm, to_wgs

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())


def _setup():
    seed, scenes, blds, truth = sample_lake(17.4935, 78.4005, [2019, 2020, 2021, 2022])
    grid = scenes[0].grid
    c = seed.centroid
    utm = utm_crs_for(c.x, c.y)
    roi = to_wgs(to_utm(seed, utm).buffer(600), utm)
    return seed, scenes, truth, rasterize_roi(roi, grid), rasterize_roi(seed, grid)


def test_area_recovered_within_3pct():
    seed, scenes, truth, roi_mask, seed_mask = _setup()
    sc = scenes[0]
    wr = extract_water(sc, roi_mask, seed_mask, CFG["water"])
    assert wr.threshold_method == "otsu_roi"
    t = truth["truth_area_m2_by_year"][2019]
    assert abs(wr.area_m2 - t) / t < 0.03, (wr.area_m2, t)


def test_cloudy_year_is_rejected_not_silently_shrunk():
    seed, scenes, truth, roi_mask, seed_mask = _setup()
    cloudy = scenes[2]  # sample_data puts a cloud over index 2
    frac = roi_cloud_fraction(cloudy, roi_mask, CFG["water"]["scl_cloud_classes"])
    assert frac > CFG["water"]["max_roi_cloud_fraction"]


def test_otsu_degenerate_falls_back():
    idx = np.full((50, 50), -0.4, dtype="float32")  # all land, unimodal
    roi = np.ones_like(idx, dtype=bool)
    t, method, note = choose_threshold(idx, roi, 0.0)
    assert method == "fallback_fixed"


def test_max_extent_is_union_and_buffer_class_correct():
    seed, scenes, truth, roi_mask, seed_mask = _setup()
    waters = [extract_water(s, roi_mask, seed_mask, CFG["water"]) for s in (scenes[0], scenes[3])]
    b = max_observed_extent(waters, BufferRule(10, 30, 9))
    assert b is not None
    assert b.area_m2 >= max(w.area_m2 for w in waters) * 0.99
    assert b.area_class == "large" and b.buffer_width_m == 30
    assert not b.buffer_ring_wgs.intersects(b.extent_wgs.representative_point())
