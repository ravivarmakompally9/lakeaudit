"""Water extent from one scene: MNDWI -> Otsu inside the ROI -> mask -> polygon.

Why Otsu is windowed: it assumes a bimodal histogram. On a whole Hyderabad tile
water is < 1% of pixels and the assumption fails. Inside a padded ROI around one
lake it usually holds — and when it doesn't we say so and fall back."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from rasterio import features
from shapely.geometry import shape, Polygon, MultiPolygon
from shapely.ops import unary_union
from skimage.filters import threshold_otsu
from skimage.measure import label
from skimage.morphology import remove_small_objects, remove_small_holes

from .imagery import Scene, Grid
from .geo import to_wgs, area_m2


@dataclass
class WaterResult:
    year: int
    scene_id: str
    acquired: str
    threshold: float
    threshold_method: str          # "otsu_roi" | "fallback_fixed"
    polygon_wgs: Polygon | MultiPolygon | None
    area_m2: float
    roi_cloud_fraction: float
    n_valid_px: int
    notes: str = ""


def mndwi(green: np.ndarray, swir: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (green - swir) / (green + swir)
    return out.astype("float32")


def rasterize_roi(poly_wgs, grid: Grid) -> np.ndarray:
    """ROI polygon (WGS84) -> boolean mask on the grid."""
    from .geo import to_utm
    poly_utm = to_utm(poly_wgs, grid.crs)
    return features.rasterize(
        [(poly_utm, 1)], out_shape=(grid.height, grid.width), transform=grid.transform, fill=0, dtype="uint8"
    ).astype(bool)


def choose_threshold(idx: np.ndarray, roi_mask: np.ndarray, fallback: float) -> tuple[float, str, str]:
    vals = idx[roi_mask & np.isfinite(idx)]
    if vals.size < 50:
        return fallback, "fallback_fixed", "too few valid pixels in ROI"
    try:
        t = float(threshold_otsu(vals))
    except Exception as e:
        return fallback, "fallback_fixed", f"otsu failed: {e}"
    # Sanity: Otsu on a unimodal (all-land or all-water) ROI returns a threshold
    # that puts almost everything on one side. Detect and fall back.
    frac = float((vals > t).mean())
    if frac < 0.005 or frac > 0.995:
        return fallback, "fallback_fixed", f"otsu degenerate (water frac {frac:.3f})"
    return t, "otsu_roi", ""


def _drop_small(fn, m, px):
    """skimage renamed min_size/area_threshold -> max_size in 0.26; support both."""
    import inspect
    params = inspect.signature(fn).parameters
    if "max_size" in params:
        return fn(m, max_size=px - 1)
    key = "min_size" if "min_size" in params else "area_threshold"
    return fn(m, **{key: px})


def water_mask(idx: np.ndarray, threshold: float, roi_mask: np.ndarray, scl: np.ndarray | None,
               cloud_classes: list[int], min_patch_px: int, seed_mask: np.ndarray | None,
               keep_largest_only: bool) -> np.ndarray:
    m = (idx > threshold) & roi_mask & np.isfinite(idx)
    if scl is not None:
        m &= ~np.isin(scl, cloud_classes)
    if min_patch_px > 1:
        m = _drop_small(remove_small_objects, m, min_patch_px)
        m = _drop_small(remove_small_holes, m, min_patch_px)
    if keep_largest_only and m.any():
        lab = label(m, connectivity=2)
        if seed_mask is not None and (lab[seed_mask] > 0).any():
            # keep every component that touches the seed polygon (a lake can be
            # split by a bund or bridge), not just the single largest blob
            keep_ids = np.unique(lab[seed_mask])
            keep_ids = keep_ids[keep_ids > 0]
            m = np.isin(lab, keep_ids)
        else:
            ids, counts = np.unique(lab[lab > 0], return_counts=True)
            m = lab == ids[np.argmax(counts)]
    return m


def mask_to_polygon(mask: np.ndarray, grid: Grid, simplify_m: float = 0.0):
    shapes = features.shapes(mask.astype("uint8"), mask=mask, transform=grid.transform)
    polys = [shape(g) for g, v in shapes if v == 1]
    if not polys:
        return None
    u = unary_union(polys)
    if simplify_m > 0:
        u = u.simplify(simplify_m, preserve_topology=True)
    return to_wgs(u, grid.crs)


def extract_water(scene: Scene, roi_mask: np.ndarray, seed_mask: np.ndarray | None, cfg: dict,
                  simplify_m: float = 0.0) -> WaterResult:
    idx = mndwi(scene.green, scene.swir)
    t, method, note = choose_threshold(idx, roi_mask, cfg["otsu_fallback_threshold"])
    m = water_mask(
        idx, t, roi_mask, scene.scl, cfg["scl_cloud_classes"], cfg["min_water_patch_px"],
        seed_mask, cfg["keep_largest_component_only"],
    )
    poly = mask_to_polygon(m, scene.grid, simplify_m)
    return WaterResult(
        year=scene.year, scene_id=scene.scene_id, acquired=scene.acquired,
        threshold=round(t, 4), threshold_method=method, polygon_wgs=poly,
        area_m2=area_m2(poly) if poly is not None else 0.0,
        roi_cloud_fraction=float(scene.extra.get("roi_cloud_fraction", np.nan)),
        n_valid_px=int((roi_mask & np.isfinite(idx)).sum()),
        notes=note,
    )
