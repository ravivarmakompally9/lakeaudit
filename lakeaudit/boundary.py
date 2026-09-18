"""Maximum observed extent + statutory buffer.

This is ONE witness to the FTL, not the FTL. The official notified polygon (when
one exists) and the historical survey sheet are the other witnesses; the
disagreement between them is reported, never hidden."""
from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

from .geo import utm_crs_for, to_utm, to_wgs, area_m2, m2_to_ha, m2_to_acres
from .register import BufferRule
from .water import WaterResult


@dataclass
class Boundary:
    method: str
    years_used: list[int]
    scene_ids: list[str]
    extent_wgs: Polygon | MultiPolygon        # max observed extent
    buffer_ring_wgs: Polygon | MultiPolygon   # ring OUTSIDE the extent
    buffer_outer_wgs: Polygon | MultiPolygon  # extent + ring
    area_m2: float
    area_class: str                           # "large" | "small"
    buffer_width_m: float

    def summary(self) -> dict:
        return {
            "method": self.method,
            "years_used": self.years_used,
            "scene_ids": self.scene_ids,
            "extent_area_m2": round(self.area_m2, 1),
            "extent_area_ha": round(m2_to_ha(self.area_m2), 3),
            "extent_area_acres": round(m2_to_acres(self.area_m2), 2),
            "area_class": self.area_class,
            "buffer_width_m": self.buffer_width_m,
        }


def max_observed_extent(results: list[WaterResult], rule: BufferRule, simplify_m: float = 5.0) -> Boundary | None:
    good = [r for r in results if r.polygon_wgs is not None and not r.polygon_wgs.is_empty]
    if not good:
        return None
    polys = [r.polygon_wgs for r in good]
    c = polys[0].centroid
    utm = utm_crs_for(c.x, c.y)
    union_utm = unary_union([to_utm(p, utm) for p in polys])
    # fill pinholes left by speckle removal; keep outer shape
    union_utm = union_utm.buffer(simplify_m).buffer(-simplify_m)
    if simplify_m > 0:
        union_utm = union_utm.simplify(simplify_m, preserve_topology=True)
    extent_wgs = to_wgs(union_utm, utm)
    a = area_m2(extent_wgs)
    ha = m2_to_ha(a)
    width = rule.width_for_area_ha(ha)
    outer_utm = union_utm.buffer(width)
    ring_utm = outer_utm.difference(union_utm)
    return Boundary(
        method="max_observed_extent",
        years_used=sorted({r.year for r in good}),
        scene_ids=[r.scene_id for r in good],
        extent_wgs=extent_wgs,
        buffer_ring_wgs=to_wgs(ring_utm, utm),
        buffer_outer_wgs=to_wgs(outer_utm, utm),
        area_m2=a,
        area_class="large" if ha >= rule.large_threshold_ha else "small",
        buffer_width_m=width,
    )
