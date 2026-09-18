"""API contract for the map app: only lakes a run produced appear (never invent geometry),
/nearby and /lookup share UTM distance + ST_Intersects semantics, lake_id scopes /lookup,
and the abstention rule is untouched. Runs on synthetic --offline outputs in a temp tree."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lakeaudit import api
from lakeaudit.pipeline import run_lake

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    shutil.copytree(ROOT / "configs", root / "configs")
    for lid in ("alwal_cheruvu", "nalla_cheruvu_kukatpally"):
        run_lake(root, lid, offline=True)
    ghost = root / "outputs" / "ghost_lake"          # a run folder that produced no boundary
    ghost.mkdir()
    (ghost / "run.json").write_text("{}")
    old = api._ROOT
    api._ROOT = root
    try:
        with TestClient(api.app) as c:               # startup -> _load() against the temp tree
            yield c
    finally:
        api._ROOT = old


def _card(client, lid):
    return next(c for c in client.get("/lakes").json() if c["lake_id"] == lid)


def test_lakes_lists_only_what_a_run_produced(client):
    cards = client.get("/lakes").json()
    assert sorted(c["lake_id"] for c in cards) == ["alwal_cheruvu", "nalla_cheruvu_kukatpally"]
    c = _card(client, "alwal_cheruvu")
    assert c["official_ftl"] == {"available": False, "reason": "not yet extracted from the HMDA notification sheet"}
    assert c["synthetic"] is True and c["data_source"] == "sample"
    assert {"run_id", "code_commit", "params_hash"} <= set(c["provenance"])
    w, s, e, n = c["bbox"]
    assert w < c["centroid"]["lon"] < e and s < c["centroid"]["lat"] < n
    assert c["district"] and c["buffer_width_m"] in (9, 30)
    assert {r["status"] for r in c["area_by_year"]} <= {"observed", "insufficient", "no_open_water"}


def test_lake_detail_and_unknown(client):
    d = client.get("/lake/alwal_cheruvu").json()
    assert {"card", "run", "boundary", "water_by_year", "structures"} <= set(d)
    assert any(f["properties"]["layer"] == "max_observed_extent" for f in d["boundary"]["features"])
    assert client.get("/lake/ghost_lake").status_code == 404
    assert client.get("/lake/nope").status_code == 404


def test_nearby_sorted_with_zones(client):
    c = _card(client, "nalla_cheruvu_kukatpally")
    r = client.get("/nearby", params={"lat": c["centroid"]["lat"], "lon": c["centroid"]["lon"]}).json()
    rows = r["lakes"]
    assert [x["lake_id"] for x in rows][0] == "nalla_cheruvu_kukatpally"
    assert rows[0]["point_zone"] == "inside_extent"
    assert all(a["distance_to_extent_line_m"] <= b["distance_to_extent_line_m"] for a, b in zip(rows, rows[1:]))
    far = client.get("/nearby", params={"lat": 17.30, "lon": 78.30}).json()["lakes"]
    assert all(x["point_zone"] == "outside" and x["distance_to_extent_line_m"] > 1000 for x in far)


def test_lookup_lake_id_scopes_the_answer(client):
    c = _card(client, "nalla_cheruvu_kukatpally")
    q = {"lat": c["centroid"]["lat"], "lon": c["centroid"]["lon"], "accuracy_m": 1}
    assert client.get("/lookup", params=q).json()["lake_id"] == "nalla_cheruvu_kukatpally"
    other = client.get("/lookup", params={**q, "lake_id": "alwal_cheruvu"}).json()
    assert other["lake_id"] == "alwal_cheruvu" and other["verdict"] == "outside"
    assert client.get("/lookup", params={**q, "lake_id": "nope"}).status_code == 404


def test_lookup_still_abstains_and_stamps_synthetic(client):
    c = _card(client, "alwal_cheruvu")
    q = {"lat": c["centroid"]["lat"], "lon": c["centroid"]["lon"], "lake_id": "alwal_cheruvu"}
    d = client.get("/nearby", params={"lat": q["lat"], "lon": q["lon"]}).json()["lakes"]
    d_edge = next(x for x in d if x["lake_id"] == "alwal_cheruvu")["distance_to_extent_line_m"]
    refused = client.get("/lookup", params={**q, "accuracy_m": d_edge + 10}).json()
    assert refused["abstain"] is True and refused["verdict"] is None
    ok = client.get("/lookup", params={**q, "accuracy_m": min(d_edge / 2, 50)}).json()   # under the 100 m hard limit too
    assert ok["abstain"] is False and ok["verdict"] == "inside_extent"
    assert ok["synthetic"] is True and ok["plain"].startswith("[SYNTHETIC SAMPLE DATA")


def test_app_and_static_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Use my location" in page.text and "/static/leaflet/leaflet.js" in page.text
    assert client.get("/static/leaflet/leaflet.js").status_code == 200
    assert client.get("/static/leaflet/images/layers.png").status_code == 200
    assert client.get("/classic").status_code == 200
