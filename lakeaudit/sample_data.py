"""Offline fallback: a synthetic lake that shrinks year by year while buildings
advance onto the old lakebed. Same Scene objects, same pipeline code — so the
whole chain can be demonstrated with no network. Every output produced from
this is stamped source="sample" and must never be mistaken for a result."""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon, Point, box
from shapely.affinity import scale as shp_scale

from .imagery import Scene, Grid, make_grid
from .geo import utm_crs_for, to_utm, to_wgs, bbox_pad_deg


def _ellipse_utm(cx, cy, rx, ry, bite_m=0.0) -> Polygon:
    e = shp_scale(Point(cx, cy).buffer(1.0, 64), rx, ry)
    if bite_m > 0:
        # encroachment eats the eastern edge inward by bite_m
        cut = box(cx + rx - bite_m, cy - ry, cx + rx + 10, cy + ry)
        e = e.difference(cut)
    return e


def sample_lake(seed_lat: float, seed_lon: float, years: list[int], res_m: float = 20.0, rng_seed: int = 7):
    """Returns (seed_polygon_wgs, scenes, buildings_wgs) for the pipeline."""
    rng = np.random.default_rng(rng_seed)
    utm = utm_crs_for(seed_lon, seed_lat)
    c = to_utm(Point(seed_lon, seed_lat), utm)
    cx, cy = c.x, c.y
    rx0, ry0 = 380.0, 260.0
    bbox = bbox_pad_deg(seed_lat, seed_lon, 600, half_w_m=rx0, half_h_m=ry0)
    grid = make_grid(bbox, res_m)

    # pixel centre coordinates
    xs = grid.transform.c + (np.arange(grid.width) + 0.5) * grid.transform.a
    ys = grid.transform.f + (np.arange(grid.height) + 0.5) * grid.transform.e
    X, Y = np.meshgrid(xs, ys)

    scenes = []
    truth = {}
    for i, yr in enumerate(years):
        bite = 40.0 * i                      # 40 m of lakebed lost per year on the east side
        lake = _ellipse_utm(cx, cy, rx0, ry0, bite)
        inside = np.zeros_like(X, dtype=bool)
        # vectorised point-in-polygon via bounding test + shapely on candidates
        from shapely import contains_xy
        inside = contains_xy(lake, X, Y)
        green = np.where(inside, 0.07, 0.13) + rng.normal(0, 0.008, X.shape)
        swir = np.where(inside, 0.02, 0.24) + rng.normal(0, 0.012, X.shape)
        scl = np.where(inside, 6, 5).astype("uint8")
        if i == 2:  # one year gets a cloud patch over the lake to exercise rejection
            cl = (X > cx - 320) & (X < cx + 320) & (Y > cy - 280) & (Y < cy + 280)
            scl[cl] = 9
            green[cl] = 0.45; swir[cl] = 0.40
        scenes.append(Scene(
            scene_id=f"SAMPLE_{yr}_synthetic", acquired=f"{yr}-11-15", cloud_cover_pct=5.0 if i != 2 else 40.0,
            year=yr, green=green.astype("float32"), swir=swir.astype("float32"), scl=scl, grid=grid, source="sample",
        ))
        truth[yr] = lake.area

    seed_poly = to_wgs(_ellipse_utm(cx, cy, rx0 * 0.8, ry0 * 0.8, bite_m=40.0 * (len(years) - 1)), utm)

    # buildings: a strip marching onto the eaten lakebed, plus ordinary ones outside
    blds = []
    k = 0
    for row in range(3):
        for col in range(6):
            x0 = cx + rx0 - 40.0 * (len(years) - 1) + 12 + col * 26
            y0 = cy - 60 + row * 40
            blds.append((f"sample_bldg_{k}", to_wgs(box(x0, y0, x0 + 18, y0 + 14), utm), {"building": "yes"})); k += 1
    for j in range(10):
        x0 = cx + rx0 + 60 + (j % 5) * 40
        y0 = cy - 200 + (j // 5) * 300
        blds.append((f"sample_bldg_{k}", to_wgs(box(x0, y0, x0 + 20, y0 + 15), utm), {"building": "yes"})); k += 1
    return seed_poly, scenes, blds, {"truth_area_m2_by_year": {y: round(a, 1) for y, a in truth.items()}}
