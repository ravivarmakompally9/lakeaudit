"""Every button and control in the web app, clicked in a real browser (Playwright + Chromium).

The tests build a throwaway outputs/ tree with the --offline pipeline, serve it with the real
FastAPI app on a free port, and answer map-tile and web-font requests locally, so no network
is needed. Every test also fails if the page logs a console error.

One-time setup:  pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import base64
import json
import math
import shutil
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from lakeaudit import api  # noqa: E402
from lakeaudit.pipeline import run_lake  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
DESKTOP = {"viewport": {"width": 1400, "height": 900}}
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
SIZES = pytest.mark.parametrize("size", [DESKTOP, PHONE], ids=["desktop", "phone"])
TARGETS = "button:visible, summary:visible, label.opt:visible, .field input:visible, .leaflet-control-zoom a:visible"


# ---------------------------------------------------------------- fixtures

def _tree(tmp: Path, lakes: list[str], real: tuple[str, ...] = ()) -> Path:
    """configs + --offline runs. Lakes in `real` get data_source='stac' so the UI's real-data
    path can be exercised; that relabelling only ever happens inside this temp directory."""
    shutil.copytree(ROOT / "configs", tmp / "configs")
    for lid in lakes:
        run_lake(tmp, lid, offline=True)
    for lid in real:
        p = tmp / "outputs" / lid / "run.json"
        run = json.loads(p.read_text())
        run["data_source"] = "stac"
        p.write_text(json.dumps(run))
    return tmp


def _validate(root: Path, lid: str, grow_m: float, pond: bool = False, kind: str = "osm_shoreline",
              shift_m: float = 0.0) -> None:
    """A reference `grow_m` outside the run's extent, measured with the real validate code.
    `pond` adds a detached 40 m-radius piece 150 m off the east shore that our line never covers;
    `shift_m` moves the whole reference east (3 km: a different lake, nothing overlaps)."""
    from shapely.affinity import translate
    from shapely.geometry import Point, mapping, shape
    from shapely.ops import unary_union
    from lakeaudit.geo import to_utm, to_wgs, utm_crs_for
    from lakeaudit.validate import load_reference, validate_lake
    out = root / "outputs" / lid
    ext = next(shape(f["geometry"]) for f in json.loads((out / "boundary.geojson").read_text())["features"]
               if f["properties"]["layer"] == "max_observed_extent")
    utm = utm_crs_for(ext.centroid.x, ext.centroid.y)
    ref = out / "ref.geojson"
    ref_u = to_utm(ext, utm).buffer(grow_m)
    if pond:
        minx, miny, maxx, maxy = ref_u.bounds
        ref_u = unary_union([ref_u, Point(maxx + 190, (miny + maxy) / 2).buffer(40)])
    ref_u = translate(ref_u, xoff=shift_m)
    ref.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": mapping(to_wgs(ref_u, utm))}))
    validate_lake(root, lid, load_reference(ref, kind))


@pytest.fixture(scope="module")
def trees(tmp_path_factory):
    real = _tree(tmp_path_factory.mktemp("real"), ["alwal_cheruvu", "hussain_sagar", "nalla_cheruvu_kukatpally"],
                 real=("hussain_sagar", "nalla_cheruvu_kukatpally"))
    _validate(real, "hussain_sagar", 12, pond=True)
    _validate(real, "nalla_cheruvu_kukatpally", 12)          # then made stale: must not show
    p = real / "outputs" / "nalla_cheruvu_kukatpally" / "validation_osm_shoreline.json"
    rec = json.loads(p.read_text()); rec["ours"]["run_id"] = "run_earlier"; p.write_text(json.dumps(rec))
    _validate(real, "nalla_cheruvu_kukatpally", 0, kind="field_gps", shift_m=3000)   # a walk around another lake
    return {"real": real, "synth": _tree(tmp_path_factory.mktemp("synth"), ["alwal_cheruvu"])}


@pytest.fixture(scope="module")
def server(trees):
    old = api._ROOT
    api._ROOT = trees["real"]
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "test server did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)
    api._ROOT = old


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary not downloaded
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield b
        b.close()


def _offline(route):
    url = route.request.url
    if url.startswith("http://127.0.0.1"):
        return route.continue_()
    if "fonts.googleapis.com" in url:
        return route.fulfill(status=200, content_type="text/css", body="")
    return route.fulfill(status=200, content_type="image/png", body=PNG_1PX)   # map tiles


@pytest.fixture
def app(browser, server):
    opened = []

    def open_app(size=DESKTOP, geo=None, geo_denied=False, insecure=False):
        ctx = browser.new_context(**size, geolocation=geo, permissions=["geolocation"] if geo else [])
        if insecure:                                    # as a phone sees the laptop over http://<LAN IP>
            ctx.add_init_script("Object.defineProperty(window, 'isSecureContext', {get: () => false});")
        if geo_denied:
            ctx.add_init_script("navigator.geolocation.getCurrentPosition = (ok, err) => "
                                "setTimeout(() => err({code: 1, message: 'denied'}), 10);")
        ctx.route("**/*", _offline)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server)
        page.wait_for_selector('body[data-ready="1"]')
        opened.append((ctx, errors))
        return page

    yield open_app
    problems = [e for _, errs in opened for e in errs]
    for ctx, _ in opened:
        ctx.close()
    assert not problems, f"browser console errors: {problems}"


# ---------------------------------------------------------------- helpers

def card(server, lid):
    return next(c for c in httpx.get(f"{server}/lakes").json() if c["lake_id"] == lid)


def north_of(c, metres):
    return c["bbox"][3] + metres / 111_320, c["centroid"]["lon"]


def nearby(server, lat, lon, lid):
    rows = httpx.get(f"{server}/nearby", params={"lat": lat, "lon": lon}).json()["lakes"]
    return next(r for r in rows if r["lake_id"] == lid)


def settle(page, ms=1200):
    page.wait_for_timeout(ms)          # map fly animations


def click_latlng(page, lat, lon):
    x, y = page.evaluate(f"(() => {{ const p = lakeaudit.map.latLngToContainerPoint([{lat}, {lon}]); return [p.x, p.y]; }})()")
    page.mouse.click(x, y)


def has_layer(page, expr):
    return page.evaluate(f"lakeaudit.map.hasLayer({expr})")


def enter(page, lat, lon, acc):
    if page.get_attribute("#manual", "open") is None:
        page.click("#manual summary")
    page.fill("#inLat", f"{lat:.6f}" if isinstance(lat, float) else str(lat))
    page.fill("#inLon", f"{lon:.6f}" if isinstance(lon, float) else str(lon))
    page.fill("#inAcc", str(acc))
    page.click("#btnCheck")


# ---------------------------------------------------------------- home

@SIZES
def test_home_lists_real_lakes_and_hides_synthetic(app, size):
    page = app(size)
    assert page.locator("#lakeList .lake .nm").all_inner_texts() == ["Hussain Sagar", "Nalla Cheruvu (Kukatpally)"]
    assert page.inner_text("#meta") == "2 lakes · Sentinel-2"
    assert page.evaluate("lakeaudit.groups.markers.getLayers().length") == 2
    assert page.is_hidden("#synBanner")
    assert has_layer(page, "lakeaudit.base.streets")
    assert "not the notified FTL" in page.inner_text(".note")
    labels = page.eval_on_selector_all(".tip-lake", "els => els.map(e => e.getBoundingClientRect().height)")
    assert len(labels) == 2 and all(h < 30 for h in labels)                   # single-line map labels


@SIZES
def test_loading_placeholder_goes_away_and_map_labels_never_overlap(app, size):
    page = app(size)
    assert page.is_hidden("#lakeSkel") and page.is_visible("#lakeList")

    def labels():
        return page.eval_on_selector_all(".tip-lake", """els => els.map(e => {const r = e.getBoundingClientRect();
            return {off: e.classList.contains('tip-off'), l: r.left, r: r.right, t: r.top, b: r.bottom};})""")

    def overlaps(ls):
        on = [x for x in ls if not x["off"]]
        return [(a, b) for i, a in enumerate(on) for b in on[i + 1:]
                if a["l"] < b["r"] and a["r"] > b["l"] and a["t"] < b["b"] and a["b"] > b["t"]]
    assert not overlaps(labels()) and not any(x["off"] for x in labels())      # fitted view: every label shows
    page.evaluate("lakeaudit.map.setView([17.45, 78.45], 8, {animate: false})")  # zoom out until labels collide
    page.wait_for_timeout(300)
    ls = labels()
    assert any(x["off"] for x in ls) and not overlaps(ls)                       # one is hidden, none stack
    page.evaluate("lakeaudit.map.setView([17.45, 78.45], 12, {animate: false})")
    page.wait_for_timeout(300)
    assert not any(x["off"] for x in labels())                                  # room again: all come back


def test_lake_buttons_open_each_lake_and_back_returns_home(app):
    page = app()
    for i, name in enumerate(["Hussain Sagar", "Nalla Cheruvu (Kukatpally)"]):
        page.locator("#lakeList .lake").nth(i).click()
        assert page.is_visible("#vLake") and page.inner_text("#lake h1") == name
        assert page.locator("#lake .stat span").all_inner_texts() == [
            "Observed extent", "Buffer ring", "Years with water", "Buildings inside / in buffer"]
        page.click("#vLake [data-back]")
        assert page.is_visible("#vHome") and page.is_hidden("#vLake")


# ---------------------------------------------------------------- lake page

@SIZES
def test_lake_screen_shows_every_year_and_the_next_window(app, server, size):
    page = app(size)
    for c in [x for x in httpx.get(f"{server}/lakes").json() if not x["synthetic"]]:
        page.locator("#lakeList .lake", has_text=c["name"]).click()
        cells = page.locator("#lake .years .yr")
        got = [(int(cells.nth(i).get_attribute("data-year")), cells.nth(i).get_attribute("data-status")) for i in range(cells.count())]
        want = [(r["year"], r["status"]) for r in c["area_by_year"]] + [(c["next_window"]["year"], c["next_window"]["state"])]
        assert got == want, c["name"]
        assert all(str(y) in cells.nth(i).get_attribute("aria-label") for i, (y, _) in enumerate(got))
        assert str(c["next_window"]["year"]) in page.inner_text("#lake .years + .fine")
        page.click("#vLake [data-back]")


def test_zoom_to_lake_button(app, server):
    page = app()
    page.locator("#lakeList .lake", has_text="Hussain Sagar").click()
    page.evaluate("lakeaudit.map.setView([17.0, 78.0], 9, {animate: false})")
    page.click('#lake [data-act="zoom"]')
    settle(page)
    w, s, e, n = card(server, "hussain_sagar")["bbox"]
    view = page.evaluate("(() => { const b = lakeaudit.map.getBounds(); return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]; })()")
    assert view[0] <= w and view[1] <= s and view[2] >= e and view[3] >= n


@SIZES
def test_lake_shows_how_wrong_the_line_can_be(app, size):
    page = app(size)
    page.locator("#lakeList .lake", has_text="Hussain Sagar").click()
    vals = page.locator("#lake .val")
    assert [vals.nth(i).get_attribute("data-kind") for i in range(vals.count())] == ["notified_ftl", "osm_shoreline"]
    assert "Not yet measured" in vals.nth(0).inner_text()                         # the official witness comes first
    osm = vals.nth(1).inner_text()
    assert "not the ftl" in osm.lower() and "Overlap (IoU)" in osm and "12 m" in osm and "Not counted" in osm
    assert "Missed: a separate piece of this outline (1.2 ac)" in osm and "Not included in the gaps above" in osm
    page.click("#vLake [data-back]")
    page.locator("#lakeList .lake", has_text="Nalla Cheruvu").click()             # OSM record stale, GPS walk elsewhere
    vals = page.locator("#lake .val")
    assert [vals.nth(i).get_attribute("data-kind") for i in range(vals.count())] == ["notified_ftl", "field_gps"]
    gps = vals.nth(1).inner_text()
    assert "No part of this outline overlaps our line" in gps and "Missed" not in gps


def test_lake_data_source_opens(app):
    page = app()
    page.locator("#lakeList .lake").first.click()
    assert page.is_hidden("#lake details.prov dl")
    page.click("#lake details.prov summary")
    text = page.inner_text("#lake details.prov dl")
    assert "Run" in text and "Commit" in text and "Params" in text and "run_" in text


def test_check_a_location_here_judges_against_that_lake(app, server):
    lat, lon = north_of(card(server, "nalla_cheruvu_kukatpally"), 500)      # nearer Nalla than Hussain
    page = app(geo={"latitude": lat, "longitude": lon, "accuracy": 20})
    page.locator("#lakeList .lake", has_text="Hussain Sagar").click()
    page.click('#lake [data-act="check-here"]')                                # no location yet: asks the GPS
    page.wait_for_selector("#answer")
    assert page.text_content("#result .eyebrow") == "Checked against"
    assert page.inner_text("#result h1") == "Hussain Sagar"
    assert page.get_attribute("#answer", "data-state") == "outside"
    page.click('#result [data-act="open-lake"]')                               # second time: reuses the location
    page.click('#lake [data-act="check-here"]')
    page.wait_for_selector("#answer")
    assert page.inner_text("#result h1") == "Hussain Sagar"


# ---------------------------------------------------------------- use my location

@SIZES
def test_use_my_location_gives_a_verdict(app, server, size):
    lat, lon = north_of(card(server, "nalla_cheruvu_kukatpally"), 1000)
    page = app(size, geo={"latitude": lat, "longitude": lon, "accuracy": 35})
    page.click("#btnLocate")
    page.wait_for_selector("#answer")
    assert page.get_attribute("#answer", "data-state") == "outside"
    assert page.text_content("#result .eyebrow") == "Nearest lake"
    assert page.inner_text("#result h1") == "Nalla Cheruvu (Kukatpally)"
    assert "±35 m" in page.inner_text("#result .stats") and "GPS accuracy" in page.inner_text("#result .stats")
    assert page.evaluate("lakeaudit.groups.me.getLayers().length") == 2      # accuracy circle + dot
    page.click("#result details.prov summary")
    assert "Commit" in page.inner_text("#result details.prov dl")
    page.click('#result [data-act="open-lake"]')                              # "View lake"
    assert page.is_visible("#vLake") and page.inner_text("#lake h1") == "Nalla Cheruvu (Kukatpally)"


def test_use_my_location_refuses_then_try_again_and_pick_on_map(app, server):
    c = card(server, "nalla_cheruvu_kukatpally")
    lat, lon = north_of(c, 15)
    d = nearby(server, lat, lon, "nalla_cheruvu_kukatpally")["distance_to_extent_line_m"]
    page = app(geo={"latitude": lat, "longitude": lon, "accuracy": d + 20})
    page.click("#btnLocate")
    page.wait_for_selector('#answer[data-state="refused"]')
    assert "will not guess" in page.inner_text("#answer")
    page.click('#answer [data-act="retry"]')                                  # "Try again" asks the GPS again
    page.wait_for_selector('#answer[data-state="refused"]')
    page.click('#answer [data-act="pick"]')                                   # "Pick on map"
    assert page.is_visible("#vHome") and page.get_attribute("#manual", "open") is not None
    assert page.get_attribute("#btnPick", "aria-pressed") == "true" and page.is_visible("#toast")


def test_use_my_location_when_permission_is_denied(app):
    page = app(geo_denied=True)
    page.click("#btnLocate")
    page.wait_for_selector('#answer[data-state="error"]')
    assert "denied" in page.inner_text("#answer")
    assert page.locator('#answer [data-act="retry"]').count() == 1
    assert page.locator('#answer [data-act="pick"]').count() == 1


def test_use_my_location_explains_https_on_a_phone_over_plain_http(app):
    page = app(PHONE, geo={"latitude": 17.49, "longitude": 78.40, "accuracy": 10}, insecure=True)
    page.click("#btnLocate")
    page.wait_for_selector('#answer[data-state="error"]')
    assert "secure (https)" in page.inner_text("#answer") and "Pick on map" in page.inner_text("#answer")
    page.click('#answer [data-act="pick"]')                                  # the phone path still works
    assert page.get_attribute("#btnPick", "aria-pressed") == "true"


# ---------------------------------------------------------------- "watch it refuse": the accuracy slider

@SIZES
def test_accuracy_slider_flips_the_answer_at_the_line(app, server, size):
    lat, lon = north_of(card(server, "hussain_sagar"), 15)
    row = nearby(server, lat, lon, "hussain_sagar")
    d = row["distance_to_extent_line_m"]
    page = app(size)
    enter(page, lat, lon, max(1, round(d / 3)))
    page.wait_for_selector("#answer")
    assert page.get_attribute("#answer", "data-state") == row["point_zone"]
    assert page.is_visible(".wi-line") and "line" in page.inner_text(".wi-scale")
    wider = math.ceil(d) + 5                                                 # past the line: must refuse
    page.locator("#accRange").fill(str(wider))
    page.wait_for_selector('#answer[data-state="refused"]')
    assert page.inner_text("#accOut") == f"±{wider} m" and f"±{wider} m" in page.inner_text("#result .stats")
    assert page.evaluate("lakeaudit.groups.me.getLayers()[0].getRadius()") == wider      # the circle follows
    narrower = max(1, math.floor(d) - 2)                                     # back inside: answers again
    page.locator("#accRange").fill(str(narrower))
    page.wait_for_selector(f'#answer[data-state="{row["point_zone"]}"]')


def test_accuracy_slider_marks_the_100_m_limit_far_from_the_line(app, server):
    lat, lon = north_of(card(server, "nalla_cheruvu_kukatpally"), 1000)
    page = app(geo={"latitude": lat, "longitude": lon, "accuracy": 35})
    page.click("#btnLocate")
    page.wait_for_selector('#answer[data-state="outside"]')
    assert "100 m limit" in page.inner_text(".wi-scale")
    page.locator("#accRange").fill("120")
    page.wait_for_selector('#answer[data-state="refused"]')
    assert "hard limit of 100 m" in page.inner_text("#answer")


# ---------------------------------------------------------------- open on a phone

def test_open_on_phone_shows_a_qr_of_the_lan_address(app):
    page = app()
    assert not page.evaluate("document.getElementById('phoneDlg').open")
    page.click("#btnPhone")
    page.wait_for_function("document.getElementById('phoneDlg').open")
    page.wait_for_function("!document.getElementById('phoneUrl').textContent.startsWith('Finding')")
    url = page.inner_text("#phoneUrl")
    if url.startswith("http://"):                                           # laptop is on a network
        page.wait_for_function("document.getElementById('qrImg').complete && document.getElementById('qrImg').naturalWidth > 0")
        assert page.is_visible("#qrImg") and url.rsplit(":", 1)[-1].isdigit()
    else:
        assert "not on a network" in url
    assert "https" in page.inner_text("#phoneNote")
    page.click("#btnPhoneClose")
    assert not page.evaluate("document.getElementById('phoneDlg').open")
    page.click("#btnPhone")
    page.wait_for_function("document.getElementById('phoneDlg').open")
    page.keyboard.press("Escape")
    assert not page.evaluate("document.getElementById('phoneDlg').open")


def test_open_on_phone_button_is_desktop_only(app):
    assert app(PHONE).is_hidden("#btnPhone")


# ---------------------------------------------------------------- enter coordinates

def test_enter_coordinates_opens_and_rejects_bad_input(app):
    page = app()
    assert page.get_attribute("#manual", "open") is None and page.is_hidden("#inLat")
    page.click("#manual summary")
    assert page.is_visible("#inLat") and page.is_visible("#inLon") and page.is_visible("#inAcc")
    for lat, lon, acc, word in [("", "78.4", "10", "latitude"), ("95", "78.4", "10", "latitude"),
                                ("17.4", "abc", "10", "longitude"), ("17.4", "200", "10", "longitude"),
                                ("17.4", "78.4", "0", "accuracy"), ("17.4", "78.4", "-5", "accuracy")]:
        enter(page, lat, lon, acc)
        assert page.is_visible("#formErr") and word in page.inner_text("#formErr")
        assert page.is_visible("#vHome")
    page.click("#manual summary")
    assert page.get_attribute("#manual", "open") is None                    # closes again


def test_check_button_answers_near_the_line_and_refuses_when_too_uncertain(app, server):
    lat, lon = north_of(card(server, "hussain_sagar"), 15)
    row = nearby(server, lat, lon, "hussain_sagar")
    d = row["distance_to_extent_line_m"]
    page = app()
    enter(page, lat, lon, max(1, round(d / 3)))
    page.wait_for_selector("#answer")
    assert page.get_attribute("#answer", "data-state") == row["point_zone"]
    assert "Accuracy entered" in page.inner_text("#result .stats")
    settle(page)
    assert page.evaluate("lakeaudit.map.getZoom()") >= 16                    # zoomed in to the line
    page.click("#vResult [data-back]")
    enter(page, lat, lon, round(d + 10))
    page.wait_for_selector('#answer[data-state="refused"]')
    page.click('#answer [data-act="retry"]')                                  # manual point: back to the form
    assert page.is_visible("#vHome") and page.get_attribute("#manual", "open") is not None


@SIZES
def test_pick_on_map_fills_the_form_and_can_be_cancelled(app, size):
    page = app(size)
    page.evaluate("lakeaudit.map.setView([17.2, 78.2], 13, {animate: false})")   # an empty patch of map
    page.click("#manual summary")
    page.click("#btnPick")
    assert page.get_attribute("#btnPick", "aria-pressed") == "true" and page.is_visible("#toast")
    x, y = (900, 450) if size is DESKTOP else (195, 230)
    page.mouse.click(x, y)
    want = page.evaluate(f"(() => {{ const ll = lakeaudit.map.containerPointToLatLng([{x}, {y}]); return [ll.lat, ll.lng]; }})()")
    assert abs(float(page.input_value("#inLat")) - want[0]) < 1e-5
    assert abs(float(page.input_value("#inLon")) - want[1]) < 1e-5
    assert page.get_attribute("#btnPick", "aria-pressed") == "false" and page.is_hidden("#toast")
    page.click("#btnPick")
    page.click("#btnPick")                                                    # second press turns it off
    assert page.get_attribute("#btnPick", "aria-pressed") == "false"
    page.click("#btnPick")
    page.keyboard.press("Escape")                                             # Escape turns it off
    assert page.get_attribute("#btnPick", "aria-pressed") == "false" and page.is_hidden("#toast")


# ---------------------------------------------------------------- map

def test_clicking_a_lake_marker_opens_the_lake(app):
    page = app()
    x, y, name = page.evaluate("""(() => { const m = lakeaudit.groups.markers.getLayers()[0];
        const p = lakeaudit.map.latLngToContainerPoint(m.getLatLng()); return [p.x, p.y, m.getTooltip().getContent()]; })()""")
    page.mouse.click(x, y)
    assert page.is_visible("#vLake") and page.inner_text("#lake h1") == name


def test_clicking_inside_a_lake_opens_the_lake(app, server):
    c = card(server, "hussain_sagar")
    w, s, e, n = c["bbox"]
    page = app()
    page.evaluate(f"lakeaudit.map.fitBounds([[{s}, {w}], [{n}, {e}]], {{animate: false, paddingTopLeft: [416, 40], paddingBottomRight: [256, 70]}})")
    click_latlng(page, c["centroid"]["lat"], c["centroid"]["lon"] - (e - w) * 0.25)   # inside, clear of marker and buildings
    assert page.is_visible("#vLake") and page.inner_text("#lake h1") == "Hussain Sagar"


def test_clicking_a_building_shows_zone_run_and_commit(app):
    page = app()
    b = page.evaluate("""(() => {
        const f = lakeaudit.state.lay['hussain_sagar'].buildingsFC.features.find(f => f.properties.zone === 'inside_extent');
        const ring = f.geometry.type === 'Polygon' ? f.geometry.coordinates[0] : f.geometry.coordinates[0][0];
        const lat = ring.reduce((a, p) => a + p[1], 0) / ring.length, lon = ring.reduce((a, p) => a + p[0], 0) / ring.length;
        lakeaudit.map.setView([lat, lon], 19, {animate: false});
        return {lat, lon, run: f.properties.run_id, commit: f.properties.code_commit}; })()""")
    settle(page, 400)
    click_latlng(page, b["lat"], b["lon"])
    page.wait_for_selector(".leaflet-popup-content .pop")
    text = page.inner_text(".leaflet-popup-content")
    assert "Inside the observed extent" in text and b["run"] in text and b["commit"] in text


def test_desktop_zoom_buttons(app):
    page = app()
    z0 = page.evaluate("lakeaudit.map.getZoom()")
    page.click(".leaflet-control-zoom-in")
    settle(page, 600)
    assert page.evaluate("lakeaudit.map.getZoom()") == z0 + 1
    page.click(".leaflet-control-zoom-out")
    settle(page, 600)
    assert page.evaluate("lakeaudit.map.getZoom()") == z0


# ---------------------------------------------------------------- layers

@pytest.mark.parametrize("box, group", [("#lyExtent", "extent"), ("#lyBuffer", "buffer"), ("#lyBuildings", "buildings")])
def test_layer_checkboxes(app, box, group):
    page = app()
    assert has_layer(page, f"lakeaudit.groups.{group}")
    page.uncheck(box)
    assert not has_layer(page, f"lakeaudit.groups.{group}")
    page.check(box)
    assert has_layer(page, f"lakeaudit.groups.{group}")


def test_basemap_buttons(app):
    page = app()
    page.check('input[name="base"][value="satellite"]')
    assert has_layer(page, "lakeaudit.base.satellite") and not has_layer(page, "lakeaudit.base.streets")
    page.check('input[name="base"][value="streets"]')
    assert has_layer(page, "lakeaudit.base.streets") and not has_layer(page, "lakeaudit.base.satellite")


def test_phone_layers_button_and_no_zoom_buttons(app):
    page = app(PHONE)
    assert page.locator(".leaflet-control-zoom").count() == 0
    assert page.is_hidden("#layersBody") and page.get_attribute("#layersToggle", "aria-expanded") == "false"
    page.click("#layersToggle")
    assert page.is_visible("#layersBody") and page.get_attribute("#layersToggle", "aria-expanded") == "true"
    page.uncheck("#lyBuildings")
    assert not has_layer(page, "lakeaudit.groups.buildings")
    page.click("#layersToggle")
    assert page.is_hidden("#layersBody")
    page.click("#layersToggle")
    page.keyboard.press("Escape")
    assert page.is_hidden("#layersBody") and page.get_attribute("#layersToggle", "aria-expanded") == "false"


def test_desktop_shows_layers_without_a_button(app):
    page = app()
    assert page.is_hidden("#layersToggle") and page.is_visible("#layersBody")


# ---------------------------------------------------------------- synthetic-only data

def test_synthetic_only_data_is_stamped_and_has_no_real_basemap(app, server, trees):
    api._ROOT = trees["synth"]
    api._load()
    try:
        page = app()
        assert page.is_visible("#synBanner") and "SYNTHETIC" in page.inner_text("#synBanner").upper()
        assert page.inner_text("#meta") == "1 lake · SYNTHETIC sample"
        assert "SYNTHETIC" in page.inner_text("#lakeList")
        assert not has_layer(page, "lakeaudit.base.streets") and not has_layer(page, "lakeaudit.base.satellite")
        page.check('input[name="base"][value="satellite"]')                   # still no real imagery underneath
        assert not has_layer(page, "lakeaudit.base.satellite")
        c = card(server, "alwal_cheruvu")
        enter(page, c["centroid"]["lat"], c["centroid"]["lon"], 5)
        page.wait_for_selector("#answer")
        assert "[SYNTHETIC SAMPLE DATA" in page.inner_text("#answer")
    finally:
        api._ROOT = trees["real"]
        api._load()


# ---------------------------------------------------------------- every screen: touch targets, no sideways scroll

@SIZES
def test_every_control_is_at_least_44px_and_nothing_scrolls_sideways(app, server, size):
    lat, lon = north_of(card(server, "nalla_cheruvu_kukatpally"), 1000)
    page = app(size, geo={"latitude": lat, "longitude": lon, "accuracy": 35})

    def check(where):
        small = page.eval_on_selector_all(TARGETS, """els => els.map(e => { const r = e.getBoundingClientRect();
            return [r.width, r.height, (e.id || e.textContent || '').trim().slice(0, 30)]; })
            .filter(([w, h]) => w > 0 && h < 43.5)""")
        assert not small, f"{where}: controls under 44 px: {small}"
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0, f"{where}: sideways scroll"

    if size is PHONE:
        page.click("#layersToggle")
    page.click("#manual summary")
    check("home")
    if size is PHONE:
        page.click("#layersToggle")
    page.locator("#lakeList .lake").first.click()
    page.click("#lake details.prov summary")
    check("lake")
    page.click("#vLake [data-back]")
    page.click("#btnLocate")
    page.wait_for_selector("#answer")
    page.click("#result details.prov summary")
    check("result")


def test_map_fly_survives_a_zero_size_map(app):
    """A resize or phone rotation can catch the map at 0x0; flyTo's first frame then throws
    "Invalid LatLng (NaN, NaN)", which used to surface as "Could not reach the service"."""
    page = app()
    bounds = "L.latLngBounds([17.42, 78.46], [17.44, 78.48])"
    page.evaluate("() => { const m = document.getElementById('map'); m.style.width = m.style.height = '0px'; lakeaudit.map.invalidateSize(); }")
    assert "Invalid LatLng" in page.evaluate(f"""() => {{ try {{ lakeaudit.map.flyToBounds({bounds}, {{duration: .1}});
        return 'no error'; }} catch (e) {{ return String(e); }} }}""")                  # the bug, unguarded
    page.evaluate("lakeaudit.map.stop()")
    page.evaluate(f"lakeaudit.fly({bounds})")                                          # guarded: must not throw
    c, z = page.evaluate("[lakeaudit.map.getCenter(), lakeaudit.map.getZoom()]")
    assert all(math.isfinite(v) for v in (c["lat"], c["lng"], z))
