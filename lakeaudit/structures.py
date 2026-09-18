"""Building footprints -> zone classification -> distance to the boundary line.

Footprint source is a config switch. OSM (Overpass) is the zero-setup default so
the pipeline runs anywhere tonight; Google Open Buildings V3 is the intended
production source and drops in as a different loader with the same output shape.

We report parcel geometry only. No owner, no survey number, no identity."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from shapely.geometry import Polygon, MultiPolygon, box, mapping, shape
from shapely.ops import unary_union

from .geo import utm_crs_for, to_utm, area_m2
from .register import UA, OVERPASS_MIRRORS

log = logging.getLogger("lakeaudit")

# Footprints fetched once per lake and reused: Overpass goes down for hours, and a failed
# fetch used to overwrite a lake's buildings with none. Each file records the bbox it covers
# and when it was fetched; delete a file to refetch.
BUILDING_CACHE = Path(__file__).resolve().parent.parent / "cache" / "buildings"

_OVERPASS_BLDG = """
[out:json][timeout:90];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]({s},{w},{n},{e});
);
out body geom;
"""


@dataclass
class Structure:
    structure_id: str
    source: str
    footprint_wgs: Polygon | MultiPolygon
    zone: str                    # inside_extent | in_buffer | outside
    dist_to_extent_m: float      # 0 if intersects extent; else distance to extent edge
    footprint_area_m2: float
    tags: dict


def fetch_osm_buildings(bbox_wgs, overpass_url: str, timeout: int = 120) -> list[tuple[str, Polygon, dict]]:
    minx, miny, maxx, maxy = bbox_wgs
    q = _OVERPASS_BLDG.format(s=miny, w=minx, n=maxy, e=maxx)
    last = None
    for url in [overpass_url] + [u for u in OVERPASS_MIRRORS if u != overpass_url]:
        try:
            r = requests.post(url, data={"data": q}, headers=UA, timeout=timeout)
            if r.status_code == 200:
                break
            last = RuntimeError(f"{url} -> HTTP {r.status_code}")
        except Exception as e:
            last = e
    else:
        raise last or RuntimeError("all Overpass mirrors failed")
    out = []
    for el in r.json().get("elements", []):
        tags = el.get("tags") or {}
        if el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            if len(pts) >= 4:
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                p = Polygon(pts)
                if p.is_valid and p.area > 0:
                    out.append((f"osm_way_{el['id']}", p, tags))
        elif el["type"] == "relation" and "members" in el:
            outers = [Polygon([(g["lon"], g["lat"]) for g in m["geometry"]])
                      for m in el["members"] if m.get("role") == "outer" and "geometry" in m and len(m["geometry"]) >= 3]
            outers = [o for o in outers if o.is_valid]
            if outers:
                out.append((f"osm_rel_{el['id']}", unary_union(outers), tags))
    return out


def save_building_cache(lake_id: str, bbox_wgs, footprints: list[tuple[str, Polygon, dict]],
                        fetched_utc: str, note: str = "") -> Path:
    BUILDING_CACHE.mkdir(parents=True, exist_ok=True)
    p = BUILDING_CACHE / f"{lake_id}.geojson"
    p.write_text(json.dumps({
        "type": "FeatureCollection",
        "lakeaudit": {"lake_id": lake_id, "bbox": [round(v, 7) for v in bbox_wgs], "fetched_utc": fetched_utc,
                      "source": "osm_overpass", "note": note},
        "features": [{"type": "Feature", "geometry": mapping(g), "properties": {"id": i, "tags": t}} for i, g, t in footprints],
    }))
    return p


def buildings_for_lake(lake_id: str, bbox_wgs, overpass_url: str, timeout: int = 120):
    """Footprints for one lake -> (footprints, where_from). Uses cache/buildings/<lake>.geojson when
    its bbox covers the one asked for (to ~1 m); otherwise fetches from Overpass and saves the
    result. A failed fetch raises and never touches the cache."""
    want = box(*bbox_wgs)
    p = BUILDING_CACHE / f"{lake_id}.geojson"
    if p.exists():
        fc = json.loads(p.read_text())
        meta = fc.get("lakeaudit", {})
        if box(*meta["bbox"]).buffer(1e-5).covers(want):
            out = [(f["properties"]["id"], shape(f["geometry"]), f["properties"].get("tags") or {}) for f in fc["features"]]
            return [t for t in out if t[1].intersects(want)], f"cache/buildings, OSM fetched {meta['fetched_utc']}"
        log.info("building cache for %s does not cover the requested bbox — refetching", lake_id)
    fp = fetch_osm_buildings(bbox_wgs, overpass_url, timeout)
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    save_building_cache(lake_id, bbox_wgs, fp, fetched)
    return fp, f"Overpass live, fetched {fetched}"


def classify(footprints: list[tuple[str, Polygon, dict]], extent_wgs, ring_wgs, source: str) -> list[Structure]:
    c = extent_wgs.centroid
    utm = utm_crs_for(c.x, c.y)
    extent_utm = to_utm(extent_wgs, utm)
    ring_utm = to_utm(ring_wgs, utm)
    edge = extent_utm.boundary
    out = []
    for sid, poly, tags in footprints:
        p_utm = to_utm(poly, utm)
        if p_utm.intersects(extent_utm):
            zone = "inside_extent"
            dist = 0.0
        elif p_utm.intersects(ring_utm):
            zone = "in_buffer"
            dist = float(p_utm.distance(extent_utm))
        else:
            zone = "outside"
            dist = float(p_utm.distance(extent_utm))
        out.append(Structure(
            structure_id=sid, source=source, footprint_wgs=poly, zone=zone,
            dist_to_extent_m=round(dist, 1), footprint_area_m2=round(area_m2(poly), 1),
            tags={k: v for k, v in tags.items() if k in ("building", "building:levels", "height")},
        ))
    return out


def zone_summary(structs: list[Structure]) -> dict:
    s = {"inside_extent": 0, "in_buffer": 0, "outside": 0}
    a = {"inside_extent": 0.0, "in_buffer": 0.0, "outside": 0.0}
    for st in structs:
        s[st.zone] += 1
        a[st.zone] += st.footprint_area_m2
    return {
        "counts": s,
        "footprint_area_m2": {k: round(v, 1) for k, v in a.items()},
        "total": len(structs),
        "caveat": "counts are as detected by the footprint source; OSM coverage is partial and Open Buildings recall in South Asia is ~77%",
    }
