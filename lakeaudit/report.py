"""Two things a reviewer can look at without a server: a static PNG of the lake
(water by year, extent, buffer, buildings by zone) and the year-by-year area
chart. Plus an interactive Leaflet map (Leaflet inlined — opens offline; only the
basemap tiles need internet)."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

from .geo import m2_to_acres
from .water import NO_SCENE_METHODS


def _plot_geom(ax, geom, **kw):
    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    for p in polys:
        x, y = p.exterior.xy
        ax.fill(x, y, **kw)
        for ring in p.interiors:
            xi, yi = ring.xy
            ax.fill(xi, yi, color="white", alpha=1.0, lw=0)


def render_preview(out_dir: Path, title: str, source: str) -> Path:
    from shapely.geometry import shape
    b = json.loads((out_dir / "boundary.geojson").read_text())["features"]
    w = json.loads((out_dir / "water_by_year.geojson").read_text())["features"]
    s = json.loads((out_dir / "structures.geojson").read_text())["features"]

    fig, ax = plt.subplots(figsize=(11.5, 8), dpi=110)
    for f in b:
        if f["properties"].get("layer") == "buffer_ring":
            _plot_geom(ax, shape(f["geometry"]), color="#e0b96a", alpha=0.55, lw=0)
    for f in b:
        if f["properties"].get("layer") == "max_observed_extent":
            g = shape(f["geometry"])
            polys = [g] if g.geom_type == "Polygon" else list(g.geoms)
            for p in polys:
                x, y = p.exterior.xy
                ax.plot(x, y, color="#a8123a", lw=2.2, zorder=5)
    years = sorted({f["properties"]["year"] for f in w})
    cmap = plt.get_cmap("Blues")
    for i, yr in enumerate(years):
        for f in w:
            if f["properties"]["year"] == yr:
                _plot_geom(ax, shape(f["geometry"]), color=cmap(0.35 + 0.6 * i / max(1, len(years) - 1)), alpha=0.5, lw=0)
    zcol = {"inside_extent": "#a8123a", "in_buffer": "#c9791c", "outside": "#43565a"}
    for f in s:
        _plot_geom(ax, shape(f["geometry"]), color=zcol[f["properties"]["zone"]], alpha=0.95, lw=0)
    import math
    lat0 = sum(ax.get_ylim()) / 2 if ax.get_ylim()[0] else 17.4
    ax.set_aspect(1 / math.cos(math.radians(lat0)))
    ax.set_title(f"{title}\n{'SYNTHETIC SAMPLE DATA — not a result' if source == 'sample' else 'Sentinel-2 L2A, post-monsoon, MNDWI/Otsu'}",
                 fontsize=11, loc="left")
    ax.set_xlabel("lon"); ax.set_ylabel("lat")
    ax.ticklabel_format(useOffset=False, style="plain")  # plain degrees, not "0.004 + 1.75e1"
    handles = [Line2D([0], [0], color="#a8123a", lw=2.2, label="max observed extent (one witness, not the FTL)"),
               Patch(color="#e0b96a", alpha=0.55, label="statutory buffer ring"),
               Patch(color=cmap(0.4), alpha=0.5, label=f"water, {years[0] if years else '?'}"),
               Patch(color=cmap(0.95), alpha=0.5, label=f"water, {years[-1] if years else '?'}"),
               Patch(color=zcol["inside_extent"], label="building inside extent"),
               Patch(color=zcol["in_buffer"], label="building in buffer"),
               Patch(color=zcol["outside"], label="building outside")]
    # outside the axes: inside, it covered Hussain Sagar's southern tip and red line
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.02, 1), borderaxespad=0, fontsize=8, frameon=True)
    ax.grid(alpha=0.2)
    p = out_dir / "preview.png"
    fig.savefig(p, bbox_inches="tight"); plt.close(fig)
    return p


def render_area_chart(out_dir: Path, title: str, source: str) -> Path:
    import csv
    all_rows = list(csv.DictReader((out_dir / "water_by_year.csv").open()))
    rows = [r for r in all_rows if r["area_m2"] not in ("", "0", "0.0")]
    missing = [r for r in all_rows if r["area_m2"] in ("", "0", "0.0")]
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=110)
    for r in missing:
        if r["threshold_method"] in NO_SCENE_METHODS:   # no usable scene: we can't say anything about this year
            col, ls, label = "#a8123a", "--", f"{r['year']}: insufficient data\n({r['threshold_method']})"
        else:                                           # clear scene, nothing at the seed reads as water
            col, ls, label = "#6b5a3e", ":", f"{r['year']}: no open water detected\n(clear scene, lakebed reads as land)"
        ax.axvline(int(r["year"]), color=col, lw=1, ls=ls, alpha=.6)
        ax.annotate(label, (int(r["year"]), 0.5), xycoords=("data", "axes fraction"),
                    ha="center", fontsize=7.5, color=col, rotation=90, va="center")
    if rows:
        ys = [int(r["year"]) for r in rows]
        ac = [m2_to_acres(float(r["area_m2"])) for r in rows]
        ax.plot(ys, ac, marker="o", color="#0a6f7d", lw=2)
        for y, a, r in zip(ys, ac, rows):
            ax.annotate(f"{a:.1f} ac", (y, a), textcoords="offset points", xytext=(0, 7), ha="center", fontsize=8)
            if r["threshold_method"] != "otsu_roi":
                ax.annotate("fallback thr.", (y, a), textcoords="offset points", xytext=(0, -14), ha="center", fontsize=7, color="#a8123a")
        ax.set_xticks(sorted(set(ys + [int(r["year"]) for r in missing])))
    ax.set_ylabel("post-monsoon water area (acres)")
    ax.set_title(f"{title} — observed water extent by year" + ("  [SYNTHETIC SAMPLE]" if source == "sample" else ""), fontsize=10, loc="left")
    ax.grid(alpha=0.25)
    p = out_dir / "area_by_year.png"
    fig.tight_layout(); fig.savefig(p); plt.close(fig)
    return p


_LEAFLET = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{leaflet_css}</style>
<script>{leaflet_js}</script>
<style>html,body,#m{{height:100%;margin:0}} .lg{{background:#fff;padding:8px 10px;font:12px system-ui;line-height:1.6;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.3)}} .sw{{display:inline-block;width:14px;height:10px;margin-right:6px;vertical-align:middle}} .warn{{background:#a8123a;color:#fff;padding:6px 10px;font:13px system-ui;border-radius:4px}}</style>
</head><body><div id="m"></div>
<script>
const B={boundary}; const W={water}; const S={structures}; const SRC="{source}";
const m=L.map('m');
// No real basemap under synthetic shapes: a fake lake over real homes must never be rendered.
if(SRC!=='sample') L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap'}}).addTo(m);
else document.getElementById('m').style.background='#f4f1ea';
const ring=L.geoJSON(B,{{filter:f=>f.properties.layer==='buffer_ring',style:{{color:'#956426',weight:1,fillColor:'#e0b96a',fillOpacity:.45,dashArray:'6 4'}}}}).addTo(m);
const ext=L.geoJSON(B,{{filter:f=>f.properties.layer==='max_observed_extent',style:{{color:'#a8123a',weight:3,fill:false}}}}).addTo(m);
const seed=L.geoJSON(B,{{filter:f=>f.properties.layer==='seed_polygon',style:{{color:'#6d7d7f',weight:1,fill:false,dashArray:'2 4'}}}}).addTo(m);
const yrs=[...new Set(W.features.map(f=>f.properties.year))].sort();
const wl={{}};
yrs.forEach((y,i)=>{{const t=yrs.length>1?i/(yrs.length-1):1; wl['water '+y]=L.geoJSON(W,{{filter:f=>f.properties.year===y,style:{{color:'#0a6f7d',weight:1,fillColor:'#0a6f7d',fillOpacity:.15+.35*t}}}});}});
if(yrs.length) wl['water '+yrs[yrs.length-1]].addTo(m);
const zc={{inside_extent:'#a8123a',in_buffer:'#c9791c',outside:'#43565a'}};
// A building's zone means nothing without the boundary it was measured against: say which, from what, when.
const EP=(B.features.find(f=>f.properties.layer==='max_observed_extent')||{{properties:{{}}}}).properties;
const BSRC=(SRC==='sample'?'SYNTHETIC sample data — not a result':'Sentinel-2 L2A, post-monsoon')+', '+(EP.years_used||[]).join(', ')+' ('+(EP.scene_ids||[]).length+' scenes)';
const pop=p=>'<b>'+p.structure_id+'</b> <small>('+p.footprint_source+' footprint)</small><br>zone: '+p.zone+'<br>distance to extent line: '+p.dist_to_extent_m+' m'
 +'<hr style="margin:4px 0"><small>measured against: max observed water extent — one witness, not the FTL<br>'+BSRC
 +'<br>buffer: '+EP.buffer_width_m+' m ('+EP.area_class+' lake, by observed extent)'
 +'<details><summary>scene ids</summary>'+(EP.scene_ids||[]).join('<br>')+'</details>'
 +'run: '+p.run_id+'<br>commit: '+p.code_commit+' · params: '+p.params_hash+'</small>';
const st=L.geoJSON(S,{{style:f=>({{color:zc[f.properties.zone],weight:1,fillColor:zc[f.properties.zone],fillOpacity:.85}}),
 onEachFeature:(f,l)=>l.bindPopup(pop(f.properties),{{maxWidth:320}})}}).addTo(m);
L.control.layers(null,Object.assign({{'buffer ring':ring,'max observed extent':ext,'OSM seed polygon':seed,'buildings':st}},wl),{{collapsed:false}}).addTo(m);
const lg=L.control({{position:'bottomleft'}}); lg.onAdd=()=>{{const d=L.DomUtil.create('div','lg'); d.innerHTML='<span class="sw" style="border:2px solid #a8123a"></span>max observed extent — one witness, not the FTL<br><span class="sw" style="background:#e0b96a"></span>statutory buffer ring<br><span class="sw" style="background:#0a6f7d;opacity:.5"></span>water by year (toggle)<br><span class="sw" style="background:#a8123a"></span>building inside &nbsp;<span class="sw" style="background:#c9791c"></span>in buffer &nbsp;<span class="sw" style="background:#43565a"></span>outside'; return d;}}; lg.addTo(m);
if(SRC==='sample'){{const w=L.control({{position:'topright'}}); w.onAdd=()=>{{const d=L.DomUtil.create('div','warn'); d.textContent='SYNTHETIC SAMPLE DATA — not a result'; return d;}}; w.addTo(m);}}
m.fitBounds(ext.getBounds().isValid()?ext.getBounds().pad(.4):seed.getBounds().pad(.4));
</script></body></html>"""


# Leaflet 1.9.4 (BSD-2, see vendor/leaflet-1.9.4/LICENSE), inlined so map.html opens with no network.
# SRI-verified against leafletjs.com: js sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=,
# css sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=. Only the basemap tiles still need a network.
_LEAFLET_DIR = Path(__file__).resolve().parent / "vendor" / "leaflet-1.9.4"


def render_map(out_dir: Path, title: str, source: str) -> Path:
    b = (out_dir / "boundary.geojson").read_text()
    w = (out_dir / "water_by_year.geojson").read_text()
    s = (out_dir / "structures.geojson").read_text()
    p = out_dir / "map.html"
    p.write_text(_LEAFLET.format(title=title, boundary=b, water=w, structures=s, source=source,
                                 leaflet_css=(_LEAFLET_DIR / "leaflet.css").read_text(),
                                 leaflet_js=(_LEAFLET_DIR / "leaflet.js").read_text()))
    return p
