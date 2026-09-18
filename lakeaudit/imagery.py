"""Sentinel-2 L2A from the public AWS archive via STAC. No account, no GEE.

For each year we pick ONE post-monsoon scene (lake fullest, sky clearest), sorted
by scene cloud cover, and read only the ROI window of three bands straight from
the Cloud-Optimised GeoTIFFs: green (B03), swir16 (B11) and the scene
classification (SCL). Everything is resampled onto one 20 m UTM grid, because
MNDWI on Sentinel-2 is honestly a 20 m product (B11 is 20 m)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.transform import from_origin
from pyproj import CRS, Transformer

from .geo import utm_crs_for

os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")

import logging as _logging
for _n in ("rasterio", "rasterio._env", "rasterio.session", "botocore", "boto3", "urllib3"):
    _logging.getLogger(_n).setLevel(_logging.WARNING)


@dataclass
class Grid:
    crs: CRS
    transform: rasterio.Affine
    width: int
    height: int

    @property
    def res_m(self) -> float:
        return abs(self.transform.a)


@dataclass
class Scene:
    scene_id: str
    acquired: str            # ISO date
    cloud_cover_pct: float   # scene-level, from STAC
    year: int
    green: np.ndarray        # float32 reflectance
    swir: np.ndarray
    scl: np.ndarray | None   # uint8 or None
    grid: Grid
    source: str = "stac"     # "stac" | "sample"
    extra: dict = field(default_factory=dict)


def make_grid(bbox_wgs: tuple[float, float, float, float], res_m: float) -> Grid:
    """A 20 m UTM grid covering a WGS84 bbox."""
    minx, miny, maxx, maxy = bbox_wgs
    utm = utm_crs_for((minx + maxx) / 2, (miny + maxy) / 2)
    t = Transformer.from_crs(4326, utm, always_xy=True)
    xs, ys = zip(t.transform(minx, miny), t.transform(maxx, miny), t.transform(maxx, maxy), t.transform(minx, maxy))
    x0, y1 = min(xs), max(ys)
    x1, y0 = max(xs), min(ys)
    # snap to grid
    x0 = np.floor(x0 / res_m) * res_m
    y1 = np.ceil(y1 / res_m) * res_m
    width = int(np.ceil((x1 - x0) / res_m))
    height = int(np.ceil((y1 - y0) / res_m))
    return Grid(crs=utm, transform=from_origin(x0, y1, res_m, res_m), width=width, height=height)


# ------------------------------------------------------------------ STAC search

def search_scenes(bbox_wgs, year: int, cfg: dict):
    """Return STAC items for one post-monsoon window, cloudiest last."""
    from pystac_client import Client
    start = f"{year}-{cfg['post_monsoon_start_mmdd']}"
    end = f"{year}-{cfg['post_monsoon_end_mmdd']}"
    client = Client.open(cfg["stac_url"])
    search = client.search(
        collections=[cfg["collection"]],
        bbox=list(bbox_wgs),
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lt": cfg["max_cloud_cover_pct"]}},
        max_items=40,
    )
    items = list(search.items())
    items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100.0))
    return items


def _read_on_grid(href: str, grid: Grid, resampling: Resampling) -> np.ndarray:
    with rasterio.open(href) as src:
        with WarpedVRT(
            src,
            crs=grid.crs,
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=resampling,
        ) as vrt:
            return vrt.read(1)


def read_scene(item, grid: Grid, cfg: dict, year: int) -> Scene:
    """Window-read the three bands we need onto the shared 20 m grid."""
    assets = item.assets
    g_href = assets[cfg["green_band"]].href
    s_href = assets[cfg["swir_band"]].href
    green = _read_on_grid(g_href, grid, Resampling.bilinear).astype("float32")
    swir = _read_on_grid(s_href, grid, Resampling.bilinear).astype("float32")
    scl = None
    if "scl" in assets:
        scl = _read_on_grid(assets["scl"].href, grid, Resampling.nearest).astype("uint8")

    # earth-search L2A COGs are scaled reflectance (0..10000) with an offset for
    # processing baseline >= 04.00. The STAC raster:bands metadata carries it.
    offset_note = {}

    def _scale(arr, asset, name):
        """DN -> reflectance. The earth-search COGs on sentinel-cogs already have
        the BOA_ADD_OFFSET removed, while the STAC raster:bands metadata still
        declares offset=-0.1. Applying it again pushes water reflectance negative
        and flips MNDWI. So: apply the metadata offset ONLY if doing so leaves the
        image physically plausible (reflectance is never meaningfully negative)."""
        rb = (asset.extra_fields or {}).get("raster:bands", [{}])
        scale = rb[0].get("scale", 0.0001) if rb else 0.0001
        offset = rb[0].get("offset", 0.0) if rb else 0.0
        valid = arr != 0
        raw = arr * scale
        if offset:
            cand = raw + offset
            neg = float((cand[valid] < -0.005).mean()) if valid.any() else 0.0
            if neg > 0.05:
                offset_note[name] = f"metadata offset {offset} NOT applied ({neg:.0%} px would go negative — COG already offset-corrected)"
                out = raw
            else:
                offset_note[name] = f"metadata offset {offset} applied"
                out = cand
        else:
            out = raw
        out[~valid] = np.nan  # nodata
        return out

    green = _scale(green, assets[cfg["green_band"]], "green")
    swir = _scale(swir, assets[cfg["swir_band"]], "swir")

    def _stats(a):
        v = a[np.isfinite(a)]
        return {"n_finite": int(v.size), "p5": float(np.percentile(v, 5)) if v.size else None,
                "p50": float(np.median(v)) if v.size else None, "p95": float(np.percentile(v, 95)) if v.size else None}

    return Scene(
        scene_id=item.id,
        acquired=str(item.datetime.date()) if item.datetime else item.properties.get("datetime", "")[:10],
        cloud_cover_pct=float(item.properties.get("eo:cloud_cover", np.nan)),
        year=year,
        green=green, swir=swir, scl=scl, grid=grid,
        source="stac",
        extra={"platform": item.properties.get("platform"), "mgrs": item.properties.get("s2:mgrs_tile") or item.properties.get("grid:code"),
               "green_href": g_href, "swir_href": s_href,
               "green_raster_bands": (assets[cfg["green_band"]].extra_fields or {}).get("raster:bands"),
               "green_stats": _stats(green), "swir_stats": _stats(swir), "offset_handling": offset_note},
    )


def best_scene_for_year(bbox_wgs, grid: Grid, year: int, cfg: dict, roi_mask: np.ndarray,
                        max_roi_cloud_fraction: float, scl_cloud_classes: list[int],
                        tries: int = 5):
    """Walk the candidate list (clearest first) and return the first scene whose
    ROI is actually clear. Returns (scene, rejected_list)."""
    rejected = []
    items = search_scenes(bbox_wgs, year, cfg)
    for it in items[:tries]:
        sc = read_scene(it, grid, cfg, year)
        frac = roi_cloud_fraction(sc, roi_mask, scl_cloud_classes)
        sc.extra["roi_cloud_fraction"] = round(float(frac), 4)
        if frac <= max_roi_cloud_fraction:
            return sc, rejected
        rejected.append({"scene_id": sc.scene_id, "acquired": sc.acquired, "roi_cloud_fraction": round(float(frac), 4)})
    return None, rejected


def roi_cloud_fraction(scene: Scene, roi_mask: np.ndarray, cloud_classes: list[int]) -> float:
    if scene.scl is None:
        nan = np.isnan(scene.green) | np.isnan(scene.swir)
        return float(nan[roi_mask].mean()) if roi_mask.any() else 1.0
    cloudy = np.isin(scene.scl, cloud_classes) | (scene.scl == 0)
    return float(cloudy[roi_mask].mean()) if roi_mask.any() else 1.0
