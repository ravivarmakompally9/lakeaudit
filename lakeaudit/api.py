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
import json
from pathlib import Path

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import shape, Point

from .geo import utm_crs_for, to_utm, to_wgs, m2_to_acres

app = FastAPI(title="lakeaudit lookup", version="0.2")
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


def _area_by_year(d: Path, summary: dict) -> list[dict]:
    """One row per requested year, from water_by_year.csv. `status` says why a year has no area:
    'insufficient' = no usable scene; 'no_open_water' = clear scene, nothing at the seed reads as water."""
    rows = {}
    if (d / "water_by_year.csv").exists():
        with (d / "water_by_year.csv").open() as fh:
            rows = {int(r["year"]): r for r in csv.DictReader(fh)}
    with_data = set(summary.get("years_with_data") or [])
    no_water = set(summary.get("years_no_open_water") or [])
    out = []
    for y in summary.get("years_requested") or sorted(rows):
        r = rows.get(y) or {}
        status = "observed" if y in with_data else ("no_open_water" if y in no_water else "insufficient")
        out.append({"year": y, "status": status,
                    "acres": round(float(r["area_acres"]), 2) if status == "observed" and r.get("area_acres") else None,
                    "scene_id": r.get("scene_id") or None, "acquired": r.get("acquired") or None})
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


def _geojson(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {"type": "FeatureCollection", "features": []}


@app.on_event("startup")
def _startup():
    _load()


@app.get("/lakes")
def lakes():
    """One card per processed lake. Lakes in the register with no run do not appear."""
    return [v["card"] for v in _STATE["lakes"].values()]


@app.get("/lake/{lake_id}")
def lake(lake_id: str):
    v = _STATE["lakes"].get(lake_id)
    if not v:
        return JSONResponse({"error": f"unknown lake '{lake_id}'", "known": list(_STATE["lakes"])}, status_code=404)
    return {"card": v["card"], "run": v["run"],
            "boundary": _geojson(v["dir"] / "boundary.geojson"),
            "water_by_year": _geojson(v["dir"] / "water_by_year.geojson"),
            "structures": _geojson(v["dir"] / "structures.geojson")}


@app.get("/nearby")
def nearby(lat: float = Query(...), lon: float = Query(...)):
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


@app.get("/lookup")
def lookup(lat: float = Query(...), lon: float = Query(...), accuracy_m: float = Query(..., description="device 95% GPS radius, metres"),
           lake_id: str | None = Query(None, description="judge against this lake instead of the nearest one")):
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


_DEMO = """<!doctype html><meta charset=utf-8><title>lakeaudit lookup</title>
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


app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    """The map is the interface: static/app.html, Leaflet from /static/leaflet/."""
    return FileResponse(_STATIC / "app.html", headers={"Cache-Control": "no-cache"})


@app.get("/classic", response_class=HTMLResponse, include_in_schema=False)
def demo():
    """The original single-form lookup page, kept as a fallback."""
    return _DEMO
