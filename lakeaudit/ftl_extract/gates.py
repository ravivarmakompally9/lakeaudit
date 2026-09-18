"""Three validity gates for an extracted FTL vertex table.

  Gate 1  PLAUSIBLE   every vertex lies inside the study region, and no hop between
                      consecutive vertices is absurdly long (a dropped digit in a
                      coordinate sends a point tens of km away).
  Gate 2  CLOSED      the vertices form a valid, simple, closed polygon.
  Gate 3  CONSISTENT  the perimeter computed from the vertices agrees with the
                      perimeter the sheet itself states; and, when the sheet lists
                      per-segment distances, each computed hop agrees with the
                      declared hop.

A table that fails ANY gate is routed to manual entry. It never becomes a boundary."""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Polygon
from shapely.validation import explain_validity

from ..geo import geodesic_m, area_m2, m2_to_acres


@dataclass
class Vertex:
    n: int
    lat: float
    lon: float
    declared_next_m: float | None = None   # distance to next vertex as printed on the sheet


@dataclass
class GateReport:
    passed: bool
    gate1_plausible: bool
    gate2_closed: bool
    gate3_consistent: bool
    computed_perimeter_m: float
    declared_perimeter_m: float | None
    perimeter_rel_error: float | None
    computed_area_acres: float | None
    bad_vertices: list[int] = field(default_factory=list)   # vertex numbers implicated
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# Telangana, generously. A vertex outside this is an OCR error, not a lake.
DEFAULT_BBOX = {"min_lat": 15.5, "max_lat": 20.0, "min_lon": 76.5, "max_lon": 81.5}


def run_gates(
    vertices: list[Vertex],
    declared_perimeter_m: float | None,
    *,
    bbox: dict = DEFAULT_BBOX,
    max_hop_m: float = 2000.0,
    perimeter_tol: float = 0.02,      # 2 %
    hop_tol_m: float = 5.0,           # per-segment absolute tolerance
    hop_tol_rel: float = 0.05,        # or 5 %, whichever is larger
) -> GateReport:
    reasons: list[str] = []
    bad: set[int] = set()

    if len(vertices) < 3:
        return GateReport(False, False, False, False, 0.0, declared_perimeter_m, None, None, [], ["fewer than 3 vertices"])

    # ---------------- Gate 1: plausibility
    g1 = True
    for v in vertices:
        if not (bbox["min_lat"] <= v.lat <= bbox["max_lat"] and bbox["min_lon"] <= v.lon <= bbox["max_lon"]):
            g1 = False
            bad.add(v.n)
            reasons.append(f"vertex {v.n} outside study region ({v.lat:.5f}, {v.lon:.5f})")
    hops = []
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        d = geodesic_m(a.lat, a.lon, b.lat, b.lon)
        hops.append(d)
        if d > max_hop_m:
            g1 = False
            bad.update({a.n, b.n})
            reasons.append(f"hop {a.n}->{b.n} is {d:.0f} m (> {max_hop_m:.0f} m)")

    # ---------------- Gate 2: closure / validity
    poly = Polygon([(v.lon, v.lat) for v in vertices])
    g2 = poly.is_valid and poly.area > 0
    if not g2:
        reasons.append(f"polygon invalid: {explain_validity(poly)}")

    # ---------------- Gate 3: perimeter consistency
    g3 = True
    computed_perim = float(sum(hops))
    rel_err = None
    if declared_perimeter_m is not None and declared_perimeter_m > 0:
        rel_err = abs(computed_perim - declared_perimeter_m) / declared_perimeter_m
        if rel_err > perimeter_tol:
            g3 = False
            reasons.append(f"computed perimeter {computed_perim:.0f} m vs declared {declared_perimeter_m:.0f} m ({rel_err*100:.1f} % off)")
    declared_hops = [v.declared_next_m for v in vertices]
    if any(h is not None for h in declared_hops):
        for v, d_comp, d_decl in zip(vertices, hops, declared_hops):
            if d_decl is None:
                continue
            tol = max(hop_tol_m, hop_tol_rel * d_decl)
            if abs(d_comp - d_decl) > tol:
                g3 = False
                bad.add(v.n)
                reasons.append(f"segment from vertex {v.n}: computed {d_comp:.1f} m vs sheet {d_decl:.1f} m")

    area_ac = m2_to_acres(area_m2(poly)) if g2 else None
    return GateReport(
        passed=g1 and g2 and g3,
        gate1_plausible=g1, gate2_closed=g2, gate3_consistent=g3,
        computed_perimeter_m=round(computed_perim, 1),
        declared_perimeter_m=declared_perimeter_m,
        perimeter_rel_error=None if rel_err is None else round(rel_err, 4),
        computed_area_acres=None if area_ac is None else round(area_ac, 2),
        bad_vertices=sorted(bad),
        reasons=reasons,
    )


def vertices_from_rows(rows: list[dict]) -> list[Vertex]:
    """rows: dicts with keys n, lat, lon and optionally dist_next_m."""
    out = []
    for r in rows:
        out.append(Vertex(
            n=int(r["n"]), lat=float(r["lat"]), lon=float(r["lon"]),
            declared_next_m=None if r.get("dist_next_m") in (None, "", "nan") else float(r["dist_next_m"]),
        ))
    out.sort(key=lambda v: v.n)
    return out
