"""Location-first, abstention-aware lookup over pre-computed outputs.

GET /lookup?lat=..&lon=..&accuracy_m=..
  -> nearest lake, zone of the point, distance to the extent line, and — first —
     whether we are allowed to answer at all.

The abstention rule: if the device's reported 95 % GPS radius is larger than the
distance from the point to the nearest boundary line, the answer would be a
coin-flip, so we refuse and say why. Every response carries the run_id and code
commit that produced the boundary it was judged against."""
from __future__ import annotations

import csv
import html
import io
import json
import math
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, Query, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from shapely.geometry import shape, Point

from .geo import utm_crs_for, to_utm, to_wgs, m2_to_acres
from .provenance import _git_commit, _hash_files
from .validate import lake_validations

_API_DESCRIPTION = """
Satellite-derived lake boundary **screening** for Hyderabad, with a published abstention rule.

* The red line is the **maximum post-monsoon water extent observed by Sentinel-2 since 2019**: one witness
  to the Full Tank Level (FTL), **not** the notified FTL and **not** a legal determination.
* `/lookup` **refuses to answer** when the device's GPS uncertainty is larger than its distance to the line.
* Every answer carries the `run_id`, `code_commit` and `params_hash` that produced it.
* Only lakes a pipeline run has produced are served. Nothing is invented.

Web app: [`/`](/) · Service health: [`/health`](/health)
"""
_TAGS = [
    {"name": "check", "description": "Check a point against the observed extent: distances, zones, and the abstention rule."},
    {"name": "lakes", "description": "Processed lakes and their geometry. Parcel geometry only, never owner identity."},
    {"name": "service", "description": "Health and provenance of what this server is serving."},
]
_SWAGGER = {"defaultModelsExpandDepth": -1, "docExpansion": "list", "tryItOutEnabled": True}
_FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' "
            "rx='8' fill='%230e1b1d'/%3E%3Cpath d='M5 19c3.5-7.5 8.5-10.5 13.5-10.5 5 0 8.5 3.5 8.5 8.3 0 6-6 8.7-12 8.7-6 0-10-2.3-10-6.5z' "
            "fill='none' stroke='%23ff6a8d' stroke-width='2.6'/%3E%3C/svg%3E")
app = FastAPI(title="lakeaudit API", version="0.3", description=_API_DESCRIPTION, openapi_tags=_TAGS, docs_url=None)
_LAT = {"description": "WGS84 latitude", "examples": [17.430068]}
_LON = {"description": "WGS84 longitude", "examples": [78.486859]}
_ROOT = Path(__file__).resolve().parents[1]
_STATIC = Path(__file__).resolve().parent / "static"
_STATE: dict = {"lakes": {}, "cfg": None, "register": {}}

# The official line is a separate witness we do not have yet. Say so; never draw a stand-in.
_OFFICIAL_FTL = {"available": False, "reason": "not yet extracted from the HMDA notification sheet"}
_BOUNDARY_IS = "one satellite-derived witness to the FTL — NOT the notified FTL and NOT a legal determination"


def _load():
    cfg = yaml.safe_load((_ROOT / "configs" / "thresholds.yaml").read_text())
    _STATE["cfg"] = cfg
    reg = yaml.safe_load((_ROOT / "configs" / "lakes.yaml").read_text())
    _STATE["register"] = {row["id"]: row for row in reg.get("lakes", [])}
    _STATE["server"] = {"code_commit": _git_commit(_ROOT),
                        "params_hash": _hash_files([_ROOT / "configs" / "lakes.yaml", _ROOT / "configs" / "thresholds.yaml"])}
    _STATE["lakes"] = {}
    for d in sorted((_ROOT / "outputs").glob("*")):
        # a lake appears only if a run produced it: never invent geometry
        if not ((d / "boundary.geojson").exists() and (d / "run.json").exists()):
            continue
        run = json.loads((d / "run.json").read_text())
        feats = json.loads((d / "boundary.geojson").read_text())["features"]
        ext = next((shape(f["geometry"]) for f in feats if f["properties"].get("layer") == "max_observed_extent"), None)
        ring = next((shape(f["geometry"]) for f in feats if f["properties"].get("layer") == "buffer_ring"), None)
        bw = next((f["properties"].get("buffer_width_m") for f in feats if f["properties"].get("layer") == "buffer_ring"), None)
        if ext is None:
            continue
        v = {"run": run, "extent": ext, "ring": ring, "buffer_width_m": bw, "dir": d}
        v["card"] = _card(d.name, v)
        _STATE["lakes"][d.name] = v


def _float(v, nd: int):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, nd)


def _area_by_year(d: Path, summary: dict) -> list[dict]:
    """One row per requested year, from water_by_year.csv. `status` says why a year has no area:
    'insufficient' = no usable scene (acres null, insufficient true); 'no_open_water' = clear scene,
    nothing at the seed reads as water (acres 0 — a measurement, not a gap). An insufficient year keeps
    the scene it rejected and that scene's cloud fraction over the lake, when there was one."""
    rows = {}
    if (d / "water_by_year.csv").exists():
        with (d / "water_by_year.csv").open() as fh:
            rows = {int(r["year"]): r for r in csv.DictReader(fh)}
    with_data = set(summary.get("years_with_data") or [])
    no_water = set(summary.get("years_no_open_water") or [])
    years = summary.get("years_requested") or (_STATE["cfg"] or {}).get("imagery", {}).get("years") or sorted(rows)
    out = []
    for y in sorted(years):
        r = rows.get(y) or {}
        status = "observed" if y in with_data else ("no_open_water" if y in no_water else "insufficient")
        acres = None if status == "insufficient" else _float(r.get("area_acres"), 2)
        if status == "no_open_water" and acres is None:
            acres = 0.0
        out.append({"year": y, "status": status, "insufficient": status == "insufficient", "acres": acres,
                    "scene_id": r.get("scene_id") or None, "acquired": r.get("acquired") or None,
                    "cloud_fraction": _float(r.get("roi_cloud_fraction"), 4)})
    return out


def _card(lake_id: str, v: dict) -> dict:
    run, ext = v["run"], v["extent"]
    reg = _STATE["register"].get(lake_id, {})
    summary = run.get("summary") or {}
    b = summary.get("boundary") or {}
    lon0, lat0 = ext.centroid.coords[0]
    utm = utm_crs_for(lon0, lat0)
    c = to_wgs(to_utm(ext, utm).centroid, utm)
    minx, miny, maxx, maxy = ext.bounds
    return {
        "lake_id": lake_id,
        "name": reg.get("name") or run["lake"]["name"],
        "district": reg.get("district"),
        "notes": reg.get("notes") or "",
        "notification_status": reg.get("notification_status", run["lake"].get("notification_status", "unknown")),
        "dispute": bool(reg.get("dispute", run["lake"].get("dispute", False))),
        "seed_verified": bool(reg.get("seed_verified", run["lake"].get("seed_verified", False))),
        "seed_source": run["lake"].get("seed_source"),
        "data_source": run["data_source"],
        "synthetic": run["data_source"] == "sample",
        "centroid": {"lat": round(c.y, 6), "lon": round(c.x, 6)},
        "bbox": [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)],   # west, south, east, north
        "extent_acres": b.get("extent_area_acres", round(m2_to_acres(b.get("extent_area_m2", 0.0)), 2)),
        "area_class": b.get("area_class"),
        "buffer_width_m": v["buffer_width_m"] if v["buffer_width_m"] is not None else b.get("buffer_width_m"),
        "years_with_data": summary.get("years_with_data", []),
        "years_insufficient": summary.get("years_insufficient", []),
        "years_no_open_water": summary.get("years_no_open_water", []),
        "area_by_year": _area_by_year(v["dir"], summary),
        "structures": (summary.get("structures") or {}).get("counts"),
        "boundary_is": _BOUNDARY_IS,
        "provenance": {"run_id": run["run_id"], "code_commit": run["code_commit"], "params_hash": run["params_hash"],
                       "scene_ids": b.get("scene_ids", []), "years_used": b.get("years_used", [])},
        "official_ftl": dict(_OFFICIAL_FTL),
    }


def _next_window(years: list[int], today: date | None = None) -> dict | None:
    """The first post-monsoon window after the last processed year, and whether it has opened yet,
    so a reader can see why that year is not in the record (not missing: not observable yet)."""
    if not years:
        return None
    im = (_STATE["cfg"] or {}).get("imagery", {})
    y = max(years) + 1
    opens = date.fromisoformat(f"{y}-{im.get('post_monsoon_start_mmdd', '10-15')}")
    closes = date.fromisoformat(f"{y}-{im.get('post_monsoon_end_mmdd', '12-31')}")
    today = today or date.today()
    state = "not_open" if today < opens else ("open" if today <= closes else "closed")
    return {"year": y, "opens": opens.isoformat(), "closes": closes.isoformat(), "state": state}


def _card_now(v: dict) -> dict:
    """The card plus validation records, re-read on every request so a new `validate` shows
    without a restart. Only records made against the run being served appear."""
    card = v["card"]
    return {**card, "validation": lake_validations(v["dir"], v["run"]["run_id"]),
            "next_window": _next_window([r["year"] for r in card["area_by_year"]])}


def _geojson(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {"type": "FeatureCollection", "features": []}


@app.on_event("startup")
def _startup():
    _load()


@app.get("/lakes", tags=["lakes"], summary="List processed lakes")
def lakes():
    """One card per processed lake. Lakes in the register with no run do not appear."""
    return [_card_now(v) for v in _STATE["lakes"].values()]


@app.get("/lake/{lake_id}", tags=["lakes"], summary="One lake with its geometry",
         description="The lake's card plus its run manifest and GeoJSON: observed extent and buffer ring, water by year, and buildings by zone.")
def lake(lake_id: str):
    v = _STATE["lakes"].get(lake_id)
    if not v:
        return JSONResponse({"error": f"unknown lake '{lake_id}'", "known": list(_STATE["lakes"])}, status_code=404)
    return {"card": _card_now(v), "run": v["run"],
            "boundary": _geojson(v["dir"] / "boundary.geojson"),
            "water_by_year": _geojson(v["dir"] / "water_by_year.geojson"),
            "structures": _geojson(v["dir"] / "structures.geojson")}


@app.get("/nearby", tags=["check"], summary="Distance from a point to every lake's edge")
def nearby(lat: float = Query(..., **_LAT), lon: float = Query(..., **_LON)):
    """Every processed lake, nearest observed-extent edge first. Distances in metres, computed in UTM."""
    utm = utm_crs_for(lon, lat)
    pt_u = to_utm(Point(lon, lat), utm)
    rows = []
    for lid, v in _STATE["lakes"].items():
        ext_u = to_utm(v["extent"], utm)
        inside = ext_u.intersects(pt_u)                                   # ST_Intersects semantics
        in_ring = (not inside) and v["ring"] is not None and to_utm(v["ring"], utm).intersects(pt_u)
        card = v["card"]
        rows.append({"lake_id": lid, "name": card["name"], "data_source": card["data_source"], "synthetic": card["synthetic"],
                     "distance_to_extent_line_m": round(float(pt_u.distance(ext_u.boundary)), 1),
                     "point_zone": "inside_extent" if inside else ("in_buffer" if in_ring else "outside"),
                     "buffer_width_m": card["buffer_width_m"], "extent_acres": card["extent_acres"]})
    rows.sort(key=lambda r: r["distance_to_extent_line_m"])
    return {"lat": lat, "lon": lon, "boundary_is": _BOUNDARY_IS,
            "distance_is": "metres from the point to the edge of each lake's maximum observed water extent", "lakes": rows}


@app.get("/lookup", tags=["check"], summary="Check a point (abstains when the GPS is too uncertain)",
         description="Zone of the point relative to the nearest lake's observed extent, or a refusal with the reason "
                     "when the reported accuracy is larger than the distance to the line.")
def lookup(lat: float = Query(..., **_LAT), lon: float = Query(..., **_LON),
           accuracy_m: float = Query(..., description="device 95% GPS radius, metres", examples=[5]),
           lake_id: str | None = Query(None, description="judge against this lake instead of the nearest one", examples=["hussain_sagar"])):
    cfg = _STATE["cfg"]["abstention"]
    pt = Point(lon, lat)
    if not _STATE["lakes"]:
        return {"abstain": True, "reason": "no lakes processed yet"}
    if lake_id is not None and lake_id not in _STATE["lakes"]:
        return JSONResponse({"error": f"unknown lake '{lake_id}'", "known": list(_STATE["lakes"])}, status_code=404)
    # nearest lake by distance to extent, computed in UTM
    best = None
    for lid, v in (_STATE["lakes"].items() if lake_id is None else [(lake_id, _STATE["lakes"][lake_id])]):
        utm = utm_crs_for(lon, lat)
        ext_u, pt_u = to_utm(v["extent"], utm), to_utm(pt, utm)
        d_edge = float(pt_u.distance(ext_u.boundary))
        inside_ext = ext_u.intersects(pt_u)                    # ST_Intersects semantics
        in_ring = (not inside_ext) and v["ring"] is not None and to_utm(v["ring"], utm).intersects(pt_u)
        cand = {"lake_id": lid, "d_edge": d_edge, "inside_ext": inside_ext, "in_ring": in_ring, "v": v}
        if best is None or d_edge < best["d_edge"]:
            best = cand
    v = best["v"]
    run = v["run"]
    zone = "inside_extent" if best["inside_ext"] else ("in_buffer" if best["in_ring"] else "outside")
    base = {
        "lake_id": best["lake_id"], "lake_name": run["lake"]["name"],
        "boundary_method": "max_observed_extent",
        "boundary_is": "one satellite-derived witness to the FTL — NOT the notified FTL and NOT a legal determination",
        "distance_to_extent_line_m": round(best["d_edge"], 1),
        "buffer_width_m": v["buffer_width_m"],
        "reported_gps_accuracy_m": accuracy_m,
        "provenance": {"run_id": run["run_id"], "code_commit": run["code_commit"], "params_hash": run["params_hash"],
                       "data_source": run["data_source"], "years_with_data": run["summary"]["years_with_data"]},
        "disclaimer": "Indicative screening only. Generates a shortlist for human inspection. Not authority to act.",
    }
    # ---- abstention, evaluated BEFORE the verdict
    if accuracy_m > cfg["hard_max_accuracy_m"]:
        return {**base, "abstain": True, "verdict": None,
                "reason": f"GPS accuracy {accuracy_m:.0f} m exceeds the hard limit of {cfg['hard_max_accuracy_m']} m"}
    if accuracy_m > best["d_edge"]:
        return {**base, "abstain": True, "verdict": None,
                "reason": f"Your position is uncertain to ±{accuracy_m:.0f} m but only {best['d_edge']:.0f} m from the boundary line. "
                          f"We cannot tell which side you are on, so we will not guess."}
    plain = {"inside_extent": "Inside the maximum observed water extent.",
             "in_buffer": f"Outside the extent but within the {v['buffer_width_m']:.0f} m buffer ring.",
             "outside": "Outside both the extent and the buffer ring."}[zone]
    if run["data_source"] == "sample":  # --offline output: stamped, never presented as a result
        base["synthetic"] = True
        plain = "[SYNTHETIC SAMPLE DATA — not a result] " + plain
    return {**base, "abstain": False, "verdict": zone, "plain": plain}


_ZONE = {"inside_extent": "Inside the observed extent", "in_buffer": "In the buffer ring",
         "outside": "Outside the extent and buffer"}
_LINE_IS = ("The line is the maximum post-monsoon water extent observed by Sentinel-2 since 2019. It is one witness "
            "to the FTL — NOT the notified FTL and NOT a legal determination.")
_IST = timezone(timedelta(hours=5, minutes=30))
_REPORT_CSS = """
:root{color-scheme:light;--ink:#0e1b1d;--muted:#5a6a6c;--line:#dde3e1;--red:#a8123a;--ochre:#956426;--green:#2f6b4f}
*{box-sizing:border-box}
html,body{margin:0;background:#fff;color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:180mm;margin:0 auto;padding:24px 16px 32px}
h1{font-size:20px;line-height:1.25;margin:0 0 2px}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:18px 0 6px}
.sub{color:var(--muted);margin:0}
.syn{margin:12px 0 0;padding:8px 12px;border-radius:6px;background:var(--red);color:#fff;font-weight:600}
.answer{margin-top:14px;padding:12px 14px;border:1px solid var(--line);border-left:5px solid var(--c);border-radius:8px}
.answer b{display:block;font-size:17px;color:var(--c)}
.answer p{margin:2px 0 0}
.c-outside{--c:var(--green)} .c-in_buffer{--c:var(--ochre)} .c-inside_extent,.c-refused{--c:var(--red)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}
dt{color:var(--muted)} dd{margin:0;overflow-wrap:anywhere}
.mono,td{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
table{border-collapse:collapse;width:100%}
th{text-align:left;font-weight:600;font-size:12px;color:var(--muted)}
th,td{padding:4px 8px 4px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.note{color:var(--muted);font-size:12.5px;margin:6px 0 0}
.fine{margin-top:18px;padding-top:10px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
.fine p{margin:0 0 4px}
.print{min-height:44px;padding:0 16px;margin-top:18px;border:1px solid var(--line);border-radius:8px;background:#fff;font:inherit;font-weight:500;cursor:pointer}
@page{size:A4;margin:14mm}
@media print{main{padding:0;max-width:none}.print{display:none}
  .answer,.syn{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def _report_html(lk: dict, lat: float, lon: float, accuracy_m: float) -> str:
    e = html.escape
    now = datetime.now(timezone.utc)
    card = _STATE["lakes"][lk["lake_id"]]["card"] if lk.get("lake_id") in _STATE["lakes"] else None
    name = card["name"] if card else lk.get("lake_name")
    synthetic = bool(lk.get("synthetic")) or bool(card and card["synthetic"])
    if lk["abstain"]:
        answer = f'<div class="answer c-refused"><b>Cannot answer</b><p>{e(lk["reason"])}</p></div>'
    else:
        answer = (f'<div class="answer c-{e(lk["verdict"])}"><b>{e(_ZONE[lk["verdict"]])}</b>'
                  f'<p>{e(lk["plain"])}</p></div>')
    where = [("Lake", name or "—"), ("District", (card or {}).get("district") or "—"),
             ("Point", f"{lat:.6f}, {lon:.6f} (WGS84)"), ("GPS accuracy", f"±{accuracy_m:g} m (95 % radius)")]
    if "distance_to_extent_line_m" in lk:
        where += [("Distance to the line", f"{lk['distance_to_extent_line_m']:g} m"),
                  ("Buffer width", "—" if lk.get("buffer_width_m") is None else f"{lk['buffer_width_m']:g} m")]
    rows, scenes = "", ""
    if card:
        used = set(card["provenance"].get("years_used") or [])
        seen = [r for r in card["area_by_year"] if r["year"] in used and r.get("scene_id")]
        rows = "".join(f"<tr><td>{r['year']}</td><td>{e(r.get('acquired') or '—')}</td><td>{e(r['scene_id'])}</td></tr>"
                       for r in seen)
        listed = {r["scene_id"] for r in seen}
        rows += "".join(f"<tr><td>—</td><td>—</td><td>{e(s)}</td></tr>"
                        for s in card["provenance"].get("scene_ids") or [] if s not in listed)
        scenes = (f'<h2>Satellite scenes behind the line</h2><table><thead><tr><th>Year</th><th>Acquired</th>'
                  f'<th>Scene id</th></tr></thead><tbody>{rows}</tbody></table>'
                  f'<p class="note">{"Synthetic sample scenes." if synthetic else "Sentinel-2 L2A, one clearest post-monsoon scene per year."}'
                  f' Years with no usable scene or no open water are not part of the line.</p>')
    p = lk.get("provenance") or {}
    prov = [("Run", p.get("run_id")), ("Code commit", p.get("code_commit")), ("Params hash", p.get("params_hash")),
            ("Data source", "SYNTHETIC sample" if synthetic else "Sentinel-2 L2A (earth-search STAC)")]
    dl = lambda pairs, mono=False: "<dl>" + "".join(
        f'<dt>{e(k)}</dt><dd{" class=mono" if mono else ""}>{e(str(v if v is not None else "—"))}</dd>' for k, v in pairs) + "</dl>"
    title = f"Screening report · {name or 'no lake'} · {lat:.5f}, {lon:.5f}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light">
<title>{e(title)}</title><link rel="icon" href="{_FAVICON}"><style>{_REPORT_CSS}</style></head><body><main>
<h1>Lake boundary screening report</h1>
<p class="sub">lakeaudit · generated {now.astimezone(_IST):%d %b %Y, %H:%M} IST ({now:%Y-%m-%d %H:%M} UTC)</p>
{'<p class="syn">SYNTHETIC SAMPLE DATA — not a result</p>' if synthetic else ''}
{answer}
<h2>Where</h2>{dl(where)}
{scenes}
<h2>Provenance</h2>{dl(prov, mono=True)}
<div class="fine"><p><b>What the line is.</b> {e(_LINE_IS)}</p>
<p>{e(lk.get("disclaimer") or "Indicative screening only. Generates a shortlist for human inspection. Not authority to act.")}</p>
<p>Parcel geometry only: no owner identity is recorded or shown.</p></div>
<button type="button" class="print" onclick="print()">Print or save as PDF</button>
</main></body></html>"""


@app.get("/report", tags=["check"], summary="Printable one-page screening report for a point",
         response_class=HTMLResponse,
         description="The /lookup answer for one point — or its refusal and the reason — with the satellite scenes and "
                     "provenance behind the line, as a printable A4 page. It calls /lookup, so the abstention rule is the same.")
def report(lat: float = Query(..., **_LAT), lon: float = Query(..., **_LON),
           accuracy_m: float = Query(..., description="device 95% GPS radius, metres", examples=[5]),
           lake_id: str | None = Query(None, description="judge against this lake instead of the nearest one", examples=["hussain_sagar"])):
    lk = lookup(lat=lat, lon=lon, accuracy_m=accuracy_m, lake_id=lake_id)
    if isinstance(lk, JSONResponse):                 # unknown lake_id: same 404 as /lookup
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=color-scheme content=light>"
                            f"<title>Unknown lake</title><p>Unknown lake '{html.escape(lake_id or '')}'.</p>", status_code=404)
    return HTMLResponse(_report_html(lk, lat, lon, accuracy_m))


_DEMO ="""<!doctype html><meta charset=utf-8><title>lakeaudit lookup</title>
<meta name=color-scheme content=light><style>:root{color-scheme:light}body{font:15px system-ui;max-width:640px;margin:40px auto;padding:0 16px;background:#fff;color:#111}input{width:100%;padding:8px;margin:4px 0 12px;font-size:15px}button{padding:10px 16px;font-size:15px}pre{background:#f3f5f4;padding:12px;overflow:auto;border-radius:4px}.ab{color:#a8123a;font-weight:600}</style>
<h2>Where am I relative to the nearest lake boundary?</h2>
<label>Latitude<input id=lat value="17.4239"></label><label>Longitude<input id=lon value="78.4738"></label>
<label>GPS accuracy (m) — the browser's 95% radius<input id=acc value="12"></label>
<button onclick="go()">Look up</button> <button onclick="useGps()">Use my phone's GPS</button>
<p id=msg></p><pre id=out></pre>
<script>
async function go(){const q=new URLSearchParams({lat:lat.value,lon:lon.value,accuracy_m:acc.value});const r=await fetch('/lookup?'+q);const j=await r.json();out.textContent=JSON.stringify(j,null,2);msg.innerHTML=j.abstain?'<span class=ab>ABSTAINED — '+j.reason+'</span>':'<b>'+j.verdict+'</b> — '+(j.plain||'');}
function useGps(){navigator.geolocation.getCurrentPosition(p=>{lat.value=p.coords.latitude.toFixed(6);lon.value=p.coords.longitude.toFixed(6);acc.value=Math.round(p.coords.accuracy);go();},e=>msg.textContent='GPS error: '+e.message,{enableHighAccuracy:true,timeout:15000});}
</script>"""


@app.get("/health", tags=["service"], summary="Service health and provenance")
def health():
    """What this server is serving, and whether it matches the code and config it is running."""
    runs = [v["run"] for v in _STATE["lakes"].values()]
    real = [r for r in runs if r["data_source"] != "sample"]
    srv = _STATE.get("server") or {}
    return {"status": "ok" if runs else "no_lakes",
            "lakes": len(real), "synthetic_lakes": len(runs) - len(real),
            "server_commit": srv.get("code_commit"), "params_hash": srv.get("params_hash"),
            "outputs_commits": sorted({r["code_commit"] for r in runs}),
            "outputs_params_hashes": sorted({r["params_hash"] for r in runs}),
            "outputs_match_params": bool(runs) and all(r["params_hash"] == srv.get("params_hash") for r in runs),
            "boundary_is": _BOUNDARY_IS}


_PHONE_GPS = ("Phone browsers share 'Use my location' only over a secure (https) connection. Over this address, "
              "use Enter coordinates or Pick on map instead; everything else works.")


def _lan_ip() -> str | None:
    """This laptop's address on the local network (opens no connection, sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def _phone_url(request: Request) -> str | None:
    ip = _lan_ip()
    return f"http://{ip}:{request.url.port or 8000}" if ip else None


@app.get("/connect", tags=["service"], summary="Address for phones on the same WiFi")
def connect(request: Request):
    return {"lan_url": _phone_url(request), "same_network_only": True, "phone_gps": _PHONE_GPS}


@app.get("/connect/qr.svg", tags=["service"], summary="QR code of the phone address", response_class=Response)
def connect_qr(request: Request):
    url = _phone_url(request)
    if not url:
        return JSONResponse({"error": "this computer is not on a network"}, status_code=503)
    try:                                          # optional: the server runs without it, only the QR is missing
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return JSONResponse({"error": "QR codes need the qrcode package: pip install qrcode"}, status_code=501)
    buf = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.get("/docs", include_in_schema=False)
def docs():
    """Swagger UI pinned to a light scheme: on a dark-mode laptop the default renders grey on black."""
    page = get_swagger_ui_html(openapi_url=app.openapi_url, title="lakeaudit API", swagger_favicon_url=_FAVICON,
                               swagger_ui_parameters=_SWAGGER)
    light = '<meta name="color-scheme" content="light"><style>:root{color-scheme:light}html,body{background:#fff}</style>'
    return HTMLResponse(page.body.decode().replace("<head>", "<head>" + light, 1))


app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    """The map is the interface: static/app.html, Leaflet from /static/leaflet/."""
    return FileResponse(_STATIC / "app.html", headers={"Cache-Control": "no-cache"})


@app.get("/classic", response_class=HTMLResponse, include_in_schema=False)
def demo():
    """The original single-form lookup page, kept as a fallback."""
    return _DEMO
