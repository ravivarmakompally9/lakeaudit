"""Small geodesy helpers shared by every stage. All areas and distances are computed
in the lake's local UTM zone — never in degrees."""
from __future__ import annotations

import math

from pyproj import CRS, Geod, Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform

GEOD = Geod(ellps="WGS84")
WGS84 = CRS.from_epsg(4326)


def utm_crs_for(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def to_utm(geom, utm: CRS):
    t = Transformer.from_crs(WGS84, utm, always_xy=True).transform
    return shp_transform(t, geom)


def to_wgs(geom, utm: CRS):
    t = Transformer.from_crs(utm, WGS84, always_xy=True).transform
    return shp_transform(t, geom)


def area_m2(geom_wgs) -> float:
    """Geodesic area of a WGS84 polygon, in square metres."""
    if geom_wgs.is_empty:
        return 0.0
    total = 0.0
    polys = [geom_wgs] if geom_wgs.geom_type == "Polygon" else list(geom_wgs.geoms)
    for p in polys:
        a, _ = GEOD.geometry_area_perimeter(p)
        total += abs(a)
    return total


def m2_to_acres(m2: float) -> float:
    return m2 / 4046.8564224


def m2_to_ha(m2: float) -> float:
    return m2 / 10_000.0


def geodesic_m(lat1, lon1, lat2, lon2) -> float:
    _, _, d = GEOD.inv(lon1, lat1, lon2, lat2)
    return d


def bbox_pad_deg(lat: float, lon: float, pad_m: float, half_w_m: float = 0.0, half_h_m: float = 0.0):
    """WGS84 bbox (minx, miny, maxx, maxy) around a point, padded in metres."""
    dlat = (pad_m + half_h_m) / 111_320.0
    dlon = (pad_m + half_w_m) / (111_320.0 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
