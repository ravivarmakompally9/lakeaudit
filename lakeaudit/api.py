"""Location-first, abstention-aware lookup over pre-computed outputs.

GET /lookup?lat=..&lon=..&accuracy_m=..
  -> nearest lake, zone of the point, distance to the extent line, and — first —
     whether we are allowed to answer at all.

The abstention rule: if the device's reported 95 % GPS radius is larger than the
distance from the point to the nearest boundary line, the answer would be a
coin-flip, so we refuse and say why. Every response carries the run_id and code
commit that produced the boundary it was judged against."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from shapely.geometry import shape, Point

from .geo import utm_crs_for, to_utm, m2_to_acres

app = FastAPI(title="lakeaudit lookup", version="0.2")
_ROOT = Path(__file__).resolve().parents[1]
_STATE: dict = {"lakes": {}, "cfg": None}


def _load():
    cfg = yaml.safe_load((_ROOT / "configs" / "thresholds.yaml").read_text())
    _STATE["cfg"] = cfg
    for d in (_ROOT / "outputs").glob("*"):
        if not (d / "boundary.geojson").exists():
            continue
        run = json.loads((d / "run.json").read_text())
        feats = json.loads((d / "boundary.geojson").read_text())["features"]
        ext = next((shape(f["geometry"]) for f in feats if f["properties"].get("layer") == "max_observed_extent"), None)
        ring = next((shape(f["geometry"]) for f in feats if f["properties"].get("layer") == "buffer_ring"), None)
        bw = next((f["properties"].get("buffer_width_m") for f in feats if f["properties"].get("layer") == "buffer_ring"), None)
        if ext is None:
            continue
        _STATE["lakes"][d.name] = {"run": run, "extent": ext, "ring": ring, "buffer_width_m": bw, "dir": d}


@app.on_event("startup")
def _startup():
    _load()


@app.get("/lakes")
def lakes():
    return [{"lake_id": k, "name": v["run"]["lake"]["name"], "data_source": v["run"]["data_source"],
             "run_id": v["run"]["run_id"], "extent_acres": round(m2_to_acres(v["run"]["summary"]["boundary"]["extent_area_m2"]), 2)}
            for k, v in _STATE["lakes"].items()]


@app.get("/lake/{lake_id}")
def lake(lake_id: str):
    v = _STATE["lakes"].get(lake_id)
    if not v:
        return JSONResponse({"error": "unknown lake"}, status_code=404)
    return {"run": v["run"], "boundary": json.loads((v["dir"] / "boundary.geojson").read_text())}


@app.get("/lookup")
def lookup(lat: float = Query(...), lon: float = Query(...), accuracy_m: float = Query(..., description="device 95% GPS radius, metres")):
    cfg = _STATE["cfg"]["abstention"]
    pt = Point(lon, lat)
    if not _STATE["lakes"]:
        return {"abstain": True, "reason": "no lakes processed yet"}
    # nearest lake by distance to extent, computed in UTM
    best = None
    for lid, v in _STATE["lakes"].items():
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
    zone = "inside_ftl" if best["inside_ext"] else ("in_buffer" if best["in_ring"] else "outside")
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
    return {**base, "abstain": False, "verdict": zone,
            "plain": {"inside_ftl": "Inside the maximum observed water extent.",
                      "in_buffer": f"Outside the extent but within the {v['buffer_width_m']:.0f} m buffer ring.",
                      "outside": "Outside both the extent and the buffer ring."}[zone]}


_DEMO = """<!doctype html><meta charset=utf-8><title>lakeaudit lookup</title>
<style>body{font:15px system-ui;max-width:640px;margin:40px auto;padding:0 16px}input{width:100%;padding:8px;margin:4px 0 12px;font-size:15px}button{padding:10px 16px;font-size:15px}pre{background:#f3f5f4;padding:12px;overflow:auto;border-radius:4px}.ab{color:#a8123a;font-weight:600}</style>
<h2>Where am I relative to the nearest lake boundary?</h2>
<label>Latitude<input id=lat value="17.4239"></label><label>Longitude<input id=lon value="78.4738"></label>
<label>GPS accuracy (m) — the browser's 95% radius<input id=acc value="12"></label>
<button onclick="go()">Look up</button> <button onclick="useGps()">Use my phone's GPS</button>
<p id=msg></p><pre id=out></pre>
<script>
async function go(){const q=new URLSearchParams({lat:lat.value,lon:lon.value,accuracy_m:acc.value});const r=await fetch('/lookup?'+q);const j=await r.json();out.textContent=JSON.stringify(j,null,2);msg.innerHTML=j.abstain?'<span class=ab>ABSTAINED — '+j.reason+'</span>':'<b>'+j.verdict+'</b> — '+(j.plain||'');}
function useGps(){navigator.geolocation.getCurrentPosition(p=>{lat.value=p.coords.latitude.toFixed(6);lon.value=p.coords.longitude.toFixed(6);acc.value=Math.round(p.coords.accuracy);go();},e=>msg.textContent='GPS error: '+e.message,{enableHighAccuracy:true,timeout:15000});}
</script>"""


@app.get("/", response_class=HTMLResponse)
def demo():
    return _DEMO
