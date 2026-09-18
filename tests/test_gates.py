"""The three validity gates must admit a clean sheet and reject each kind of OCR damage."""
import copy
import math

from lakeaudit.ftl_extract.gates import Vertex, run_gates
from lakeaudit.geo import geodesic_m


def _ring(n=12, lat0=17.4935, lon0=78.4005, r_m=300.0):
    """A clean 12-vertex ring around a point, with declared per-segment distances."""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        dlat = (r_m * math.cos(a)) / 111_320.0
        dlon = (r_m * math.sin(a)) / (111_320.0 * math.cos(math.radians(lat0)))
        pts.append((lat0 + dlat, lon0 + dlon))
    vs = []
    for i, (la, lo) in enumerate(pts):
        la2, lo2 = pts[(i + 1) % n]
        vs.append(Vertex(n=i + 1, lat=la, lon=lo, declared_next_m=round(geodesic_m(la, lo, la2, lo2), 1)))
    perim = sum(v.declared_next_m for v in vs)
    return vs, perim


def test_clean_sheet_passes():
    vs, perim = _ring()
    rep = run_gates(vs, perim)
    assert rep.passed, rep.reasons
    assert rep.gate1_plausible and rep.gate2_closed and rep.gate3_consistent
    assert abs(rep.computed_perimeter_m - perim) < 1.0


def test_dropped_digit_fails_gate1():
    vs, perim = _ring()
    bad = copy.deepcopy(vs)
    bad[4].lon = 7.84005          # "78.4005" -> "7.84005": one dropped digit
    rep = run_gates(bad, perim)
    assert not rep.passed
    assert not rep.gate1_plausible
    assert 5 in rep.bad_vertices


def test_swapped_digits_fails_gate3_and_names_the_vertex():
    vs, perim = _ring()
    bad = copy.deepcopy(vs)
    bad[7].lat = bad[7].lat + 0.0009   # ~100 m shift: still plausible, still closed, but the sheet's distances no longer match
    rep = run_gates(bad, perim)
    assert rep.gate1_plausible
    assert not rep.gate3_consistent
    assert not rep.passed
    assert 8 in rep.bad_vertices or 7 in rep.bad_vertices


def test_wrong_declared_perimeter_fails_gate3():
    vs, perim = _ring()
    rep = run_gates([Vertex(v.n, v.lat, v.lon, None) for v in vs], perim * 1.10)  # sheet says 10 % more
    assert not rep.gate3_consistent
    assert rep.perimeter_rel_error is not None and rep.perimeter_rel_error > 0.02


def test_self_intersecting_fails_gate2():
    vs, perim = _ring()
    bad = copy.deepcopy(vs)
    bad[2], bad[8] = bad[8], bad[2]     # bow-tie
    for i, v in enumerate(bad):
        v.n = i + 1
        v.declared_next_m = None
    rep = run_gates(bad, None)
    assert not rep.gate2_closed
    assert not rep.passed


def test_too_few_vertices():
    rep = run_gates([Vertex(1, 17.49, 78.40), Vertex(2, 17.491, 78.401)], None)
    assert not rep.passed and "fewer than 3" in rep.reasons[0]
