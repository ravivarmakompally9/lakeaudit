"""The lake register. Loads configs/lakes.yaml and resolves each lake's seed polygon
from OpenStreetMap. The OSM polygon is only a SEED for the region of interest —
it is never used as a boundary."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from shapely.geometry import LineString, Point, Polygon, MultiPolygon, mapping, shape
from shapely.ops import linemerge, polygonize, unary_union

from .geo import utm_crs_for, to_utm, to_wgs, area_m2

log = logging.getLogger("lakeaudit")
UA = {"User-Agent": "lakeaudit/0.2 (academic research; Vardhaman College of Engineering; contact via repo)"}
# One OSM fetch per lake, reused after that: Overpass 504s for minutes at a time.
# Keyed on the register row, so editing seed_lat/lon or the regex refetches.
SEED_CACHE = Path(__file__).resolve().parent.parent / "cache" / "seeds"
OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]


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
            # A multipolygon's outer ring is often split across several OPEN ways.
            # Closing each way on its own draws a chord and truncates the lake
            # (Hussain Sagar: 242 ha instead of 454 ha). Stitch ways into rings first.
            try:
                poly = _rings(el["members"], "outer")
                if poly is not None and not poly.is_empty:
                    inner = _rings(el["members"], "inner")
                    if inner is not None and not inner.is_empty:
                        poly = poly.difference(inner)
                    out.append((el["id"], name, poly))
            except Exception:
                pass
    return out


def _rings(members: list[dict], role: str):
    lines = [LineString([(g["lon"], g["lat"]) for g in m["geometry"]])
             for m in members if m.get("role") == role and len(m.get("geometry", [])) >= 2]
    if not lines:
        return None
    polys = [p for p in polygonize(linemerge(lines)) if p.is_valid and p.area > 0]
    return unary_union(polys) if polys else None


def _seed_key(lake: Lake) -> dict:
    return {"seed_lat": lake.seed_lat, "seed_lon": lake.seed_lon,
            "osm_name_regex": lake.osm_name_regex, "search_radius_m": lake.search_radius_m}


def resolve_seed(lake: Lake, overpass_url: str, timeout: int = 90) -> Lake:
    """Snap the lake to the nearest matching OSM water polygon. Falls back to a
    circle if OSM is unreachable or nothing matches — and says so."""
    cache = SEED_CACHE / f"{lake.id}.geojson"
    if cache.exists():
        c = json.loads(cache.read_text())
        pr = c["properties"]
        if all(pr.get(k) == v for k, v in _seed_key(lake).items()):
            lake.seed_polygon = shape(c["geometry"])
            lake.seed_source = f"{pr['seed_source']}_cached_{pr['fetched_utc'][:10]}"
            lake.seed_osm_ids = pr["osm_ids"]
            return lake
    q = _OVERPASS_Q.format(r=lake.search_radius_m, lat=lake.seed_lat, lon=lake.seed_lon)
    polys = []
    urls = [overpass_url] + [u for u in OVERPASS_MIRRORS if u != overpass_url]
    for url in urls:
        try:
            r = requests.post(url, data={"data": q}, headers=UA, timeout=timeout)
            if r.status_code != 200:
                log.warning("Overpass %s -> HTTP %s: %s", url, r.status_code, r.text[:120].replace("\n", " "))
                continue
            polys = _osm_elements_to_polys(r.json().get("elements", []))
            log.info("Overpass %s -> %d water polygons within %d m", url, len(polys), lake.search_radius_m)
            break
        except Exception as e:  # network, rate limit, parse
            log.warning("Overpass %s failed: %s: %s", url, type(e).__name__, str(e)[:120])
    if not polys:
        lake.notes = (lake.notes + " | OSM lookup returned nothing — using fallback circle").strip(" |")

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
        SEED_CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"type": "Feature", "geometry": mapping(poly), "properties": {
            **_seed_key(lake), "seed_source": lake.seed_source, "osm_ids": lake.seed_osm_ids, "osm_name": name,
            "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}}))
        return lake

    # fallback: 400 m circle — clearly labelled
    utm = utm_crs_for(lake.seed_lon, lake.seed_lat)
    circ = to_wgs(to_utm(seed_pt, utm).buffer(400), utm)
    lake.seed_polygon = circ
    lake.seed_source = "fallback_circle"
    return lake


def seed_area_ha(lake: Lake) -> float:
    return area_m2(lake.seed_polygon) / 10_000.0 if lake.seed_polygon else 0.0
