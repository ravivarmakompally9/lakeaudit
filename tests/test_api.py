"""API contract for the map app: only lakes a run produced appear (never invent geometry),
/nearby and /lookup share UTM distance + ST_Intersects semantics, lake_id scopes /lookup,
and the abstention rule is untouched. Runs on synthetic --offline outputs in a temp tree."""
import json
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


def test_lake_cards_carry_current_validation_only(client, tmp_path):
    from lakeaudit.validate import load_reference, validate_lake
    assert _card(client, "alwal_cheruvu")["validation"] == []
    ref = tmp_path / "ref.geojson"
    ext = next(f for f in client.get("/lake/nalla_cheruvu_kukatpally").json()["boundary"]["features"]
               if f["properties"]["layer"] == "max_observed_extent")
    ref.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": ext["geometry"]}))   # identical line
    out = api._ROOT / "outputs" / "nalla_cheruvu_kukatpally"
    validate_lake(api._ROOT, "nalla_cheruvu_kukatpally", load_reference(ref, "osm_shoreline"))
    stale = json.loads((out / "validation_osm_shoreline.json").read_text())
    stale["ours"]["run_id"] = "run_earlier"; stale["reference"]["kind"] = "field_gps"
    (out / "validation_field_gps.json").write_text(json.dumps(stale))      # measured a line that no longer exists
    rows = _card(client, "nalla_cheruvu_kukatpally")["validation"]        # re-read per request: no restart needed
    assert [r["kind"] for r in rows] == ["osm_shoreline"]
    assert rows[0]["iou"] == 1.0 and rows[0]["counted"] is False and rows[0]["synthetic"] is True
    assert rows[0]["ref_unmatched_parts"] == 0 and rows[0]["no_part_overlaps"] is False
    assert client.get("/lake/nalla_cheruvu_kukatpally").json()["card"]["validation"] == rows


def test_health_reports_what_is_served(client):
    h = client.get("/health").json()
    assert h["status"] == "ok"
    assert h["lakes"] == 0 and h["synthetic_lakes"] == 2           # this temp tree holds two --offline runs
    assert h["outputs_match_params"] is True                        # runs used the same configs the server reads
    assert h["server_commit"] and h["params_hash"] and h["outputs_commits"]
    assert "NOT the notified FTL" in h["boundary_is"]


def test_openapi_documents_every_endpoint(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "lakeaudit API"
    assert {t["name"] for t in spec["tags"]} == {"check", "lakes", "service"}
    for path in ["/lakes", "/lake/{lake_id}", "/nearby", "/lookup", "/health"]:
        op = spec["paths"][path]["get"]
        assert op.get("summary") and op.get("tags"), path
    params = {p["name"]: p for p in spec["paths"]["/lookup"]["get"]["parameters"]}
    assert params["lat"]["schema"].get("examples") or params["lat"].get("examples")
    assert "/" not in spec["paths"] and "/classic" not in spec["paths"]   # the app pages are not API
    assert client.get("/docs").status_code == 200


def test_cards_list_every_year_and_the_next_window(client):
    import yaml
    from datetime import date
    years = yaml.safe_load((ROOT / "configs" / "thresholds.yaml").read_text())["imagery"]["years"]
    for c in client.get("/lakes").json():
        assert [r["year"] for r in c["area_by_year"]] == years                     # every configured year, none dropped
        assert all(r["status"] in ("observed", "no_open_water", "insufficient") for r in c["area_by_year"])
        nw = c["next_window"]
        assert nw["year"] == max(years) + 1 and nw["state"] in ("not_open", "open", "closed")
    y = max(years) + 1
    assert api._next_window(years, date(y, 9, 19))["state"] == "not_open"
    assert api._next_window(years, date(y, 11, 1))["state"] == "open"
    assert api._next_window(years, date(y + 1, 1, 5))["state"] == "closed"
    assert api._next_window([]) is None


def test_connect_gives_a_phone_address_and_its_qr(client, monkeypatch):
    monkeypatch.setattr(api, "_lan_ip", lambda: "192.168.1.50")
    c = client.get("/connect").json()
    assert c["lan_url"].startswith("http://192.168.1.50:") and "https" in c["phone_gps"]
    r = client.get("/connect/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b"<svg" in r.content
    monkeypatch.setattr(api, "_lan_ip", lambda: None)                       # laptop not on any network
    assert client.get("/connect").json()["lan_url"] is None
    assert client.get("/connect/qr.svg").status_code == 503
