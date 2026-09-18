"""The lake register. Loads configs/lakes.yaml and resolves each lake's seed polygon
from OpenStreetMap. The OSM polygon is only a SEED for the region of interest —
it is never used as a boundary."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml
from shapely.geometry import Point, Polygon, MultiPolygon, shape
from shapely.ops import unary_union

from .geo import utm_crs_for, to_utm, to_wgs, area_m2


@dataclass
class Lake:
    id: str
    name: str
    district: str
    seed_lat: float
    seed_lon: float
    seed_verified: bool
    osm_name_regex: str
    search_radius_m: int
    notification_status: str = "unknown"
    dispute: bool = False
    notes: str = ""
    # filled at runtime
    seed_polygon: Polygon | MultiPolygon | None = None
    seed_source: str = ""          # "osm" | "fallback_circle" | "sample"
    seed_osm_ids: list[int] = field(default_factory=list)


@dataclass
class BufferRule:
    large_threshold_ha: float
    large_buffer_m: float
    small_buffer_m: float

    def width_for_area_ha(self, ha: float) -> float:
        return self.large_buffer_m if ha >= self.large_threshold_ha else self.small_buffer_m


def load_register(path: Path) -> tuple[list[Lake], BufferRule]:
    cfg = yaml.safe_load(path.read_text())
    rule = BufferRule(**cfg["buffer_rule"])
    lakes = [Lake(**row) for row in cfg["lakes"]]
    ids = [l.id for l in lakes]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate lake ids in register")
    return lakes, rule


# --------------------------------------------------------------------------- OSM

_OVERPASS_Q = """
[out:json][timeout:60];
(
  way["natural"="water"](around:{r},{lat},{lon});
  relation["natural"="water"](around:{r},{lat},{lon});
  way["water"](around:{r},{lat},{lon});
  relation["water"](around:{r},{lat},{lon});
);
out body geom;
"""


def _osm_elements_to_polys(elements: list[dict]) -> list[tuple[int, str, Polygon]]:
    out = []
    for el in elements:
        name = (el.get("tags") or {}).get("name", "")
        if el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            if len(pts) >= 4 and pts[0] == pts[-1]:
                poly = Polygon(pts)
                if poly.is_valid and poly.area > 0:
                    out.append((el["id"], name, poly))
        elif el["type"] == "relation" and "members" in el:
            outers = []
            for m in el["members"]:
                if m.get("role") == "outer" and "geometry" in m:
                    pts = [(g["lon"], g["lat"]) for g in m["geometry"]]
                    if len(pts) >= 3:
                        outers.append(pts)
            if outers:
                try:
                    poly = unary_union([Polygon(o) for o in outers if Polygon(o).is_valid])
                    if not poly.is_empty:
                        out.append((el["id"], name, poly))
                except Exception:
                    pass
    return out


def resolve_seed(lake: Lake, overpass_url: str, timeout: int = 90) -> Lake:
    """Snap the lake to the nearest matching OSM water polygon. Falls back to a
    circle if OSM is unreachable or nothing matches — and says so."""
    q = _OVERPASS_Q.format(r=lake.search_radius_m, lat=lake.seed_lat, lon=lake.seed_lon)
    try:
        r = requests.post(overpass_url, data={"data": q}, timeout=timeout)
        r.raise_for_status()
        polys = _osm_elements_to_polys(r.json().get("elements", []))
    except Exception as e:  # network, rate limit, parse
        polys = []
        lake.notes = (lake.notes + f" | OSM lookup failed: {type(e).__name__}").strip(" |")

    seed_pt = Point(lake.seed_lon, lake.seed_lat)
    rx = re.compile(lake.osm_name_regex)
    named = [(i, n, p) for i, n, p in polys if n and rx.search(n)]
    candidates = named or polys
    if candidates:
        # prefer name match; among those, the one closest to the seed, then largest
        candidates.sort(key=lambda t: (t[2].distance(seed_pt), -t[2].area))
        osm_id, name, poly = candidates[0]
        lake.seed_polygon = poly
        lake.seed_source = "osm" + ("" if named else "_unnamed_nearest")
        lake.seed_osm_ids = [osm_id]
        return lake

    # fallback: 400 m circle — clearly labelled
    utm = utm_crs_for(lake.seed_lon, lake.seed_lat)
    circ = to_wgs(to_utm(seed_pt, utm).buffer(400), utm)
    lake.seed_polygon = circ
    lake.seed_source = "fallback_circle"
    return lake


def seed_area_ha(lake: Lake) -> float:
    return area_m2(lake.seed_polygon) / 10_000.0 if lake.seed_polygon else 0.0
