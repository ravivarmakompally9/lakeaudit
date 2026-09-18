"""Building-footprint cache: a covered bbox never touches the network; an uncovered one fetches
and saves; a failed fetch raises and leaves the cache alone."""
import json

import pytest
from shapely.geometry import box

from lakeaudit import structures

BBOX = (78.40, 17.40, 78.41, 17.41)
FOOTPRINTS = [("osm_way_1", box(78.402, 17.402, 78.4021, 17.4021), {"building": "yes"}),
              ("osm_way_2", box(78.408, 17.408, 78.4081, 17.4081), {"building": "house"})]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(structures, "BUILDING_CACHE", tmp_path)
    return tmp_path


def _no_network(*a, **k):
    raise AssertionError("Overpass must not be called when the cache covers the bbox")


def test_cached_footprints_are_used_without_network(cache, monkeypatch):
    structures.save_building_cache("lake_a", BBOX, FOOTPRINTS, "2026-09-18T16:25Z")
    monkeypatch.setattr(structures, "fetch_osm_buildings", _no_network)
    fp, where = structures.buildings_for_lake("lake_a", (78.401, 17.401, 78.409, 17.409), "http://x")
    assert [i for i, _, _ in fp] == ["osm_way_1", "osm_way_2"]
    assert fp[1][2] == {"building": "house"}
    assert where == "cache/buildings, OSM fetched 2026-09-18T16:25Z"
    fp, _ = structures.buildings_for_lake("lake_a", (78.4015, 17.4015, 78.405, 17.405), "http://x")
    assert [i for i, _, _ in fp] == ["osm_way_1"]                     # only footprints inside the asked bbox


def test_uncovered_bbox_fetches_and_saves(cache, monkeypatch):
    structures.save_building_cache("lake_a", (78.40, 17.40, 78.405, 17.405), FOOTPRINTS[:1], "2026-09-18T16:25Z")
    monkeypatch.setattr(structures, "fetch_osm_buildings", lambda *a, **k: FOOTPRINTS)
    fp, where = structures.buildings_for_lake("lake_a", BBOX, "http://x")
    assert len(fp) == 2 and where.startswith("Overpass live")
    saved = json.loads((cache / "lake_a.geojson").read_text())
    assert saved["lakeaudit"]["bbox"] == list(BBOX) and len(saved["features"]) == 2


def test_failed_fetch_raises_and_keeps_the_cache(cache, monkeypatch):
    structures.save_building_cache("lake_a", (78.40, 17.40, 78.405, 17.405), FOOTPRINTS[:1], "2026-09-18T16:25Z")
    before = (cache / "lake_a.geojson").read_text()

    def down(*a, **k):
        raise RuntimeError("all Overpass mirrors failed")
    monkeypatch.setattr(structures, "fetch_osm_buildings", down)
    with pytest.raises(RuntimeError):
        structures.buildings_for_lake("lake_a", BBOX, "http://x")
    with pytest.raises(RuntimeError):
        structures.buildings_for_lake("lake_b", BBOX, "http://x")       # no cache at all
    assert (cache / "lake_a.geojson").read_text() == before
    assert not (cache / "lake_b.geojson").exists()
