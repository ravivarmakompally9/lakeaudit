"""How wrong can the line be? Measure the max observed extent against a reference polygon.

A reference is another WITNESS to where the lake's edge is: the HMDA notified FTL, a
field GPS walk, the 1971–72 survey sheet, or OSM's mapped shoreline. It is read only
here, after a run. It is never an input to the pipeline, so nothing is tuned on it.

Only a notified FTL that passed all three validity gates, compared against a real
(non-synthetic) run, counts toward the published error rate. Every other comparison is
kept and labelled as what it is.

Metrics. IoU and overlap are computed in the lake's local UTM zone; the two areas are
geodesic, like everywhere else in the pipeline.
  iou                     area(ours ∩ ref) / area(ours ∪ ref)
  area_error_pct          (ours − ref) / ref
  gap_ref_to_ours_m       at points every `step_m` along the reference line, the distance to
                          our line. Its p95 answers "how far can the reference line be from ours".
                          Gaps are measured FROM each outer shoreline (islands dropped: an FTL is an
                          outer line, and Hussain Sagar's statue island, below the 20 m pixel, would
                          read as a 420 m gap) TO any edge of the other line, islands included: a
                          land tongue whose neck is under one pixel wide is a peninsula in OSM but an
                          island in our mask (Durgam Cheruvu's south basin) — we did see that land.
                          Only pieces that overlap the other line are measured this way. A piece
                          with no overlap at all (OSM's Gurram Cheruvu has a detached 4.6 ac pond
                          where Sentinel-2 never saw open water) is not an edge error. It is listed
                          under ref_parts_unmatched with its acres and distance instead of turning
                          every point on it into a 200 m "gap". If no piece overlaps (wrong lake?),
                          every piece is measured and no_part_overlaps is set.
  gap_ours_to_ref_m       the same, measured the other way (our unmatched pieces: ours_parts_unmatched)
  hausdorff_m             the larger of the two maxima (sampled)
  ref_line_outside_ours   fraction of the reference line lying outside our extent — where the
                          reference reaches further than any water Sentinel-2 has seen
  ref_line_within_m       fraction of the reference line within 9 / 20 / 30 / 60 m of ours
                          (9 and 30 are the buffer widths; 20 is one Sentinel-2 pixel)
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiLineString, Polygon, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity

from .geo import area_m2, m2_to_acres, to_utm, to_wgs, utm_crs_for
from .provenance import _git_commit

KINDS = {
    "notified_ftl": "HMDA notified FTL polygon, from the sheet's vertex table after the three validity gates",
    "osm_shoreline": "OpenStreetMap community-mapped shoreline. Not the FTL. It is also the seed that picks "
                     "which water body is the lake, so it is not fully independent of our line",
    "field_gps": "shoreline walked with a phone GPS",
    "survey_1971": "Survey of India 1971–72 toposheet, georeferenced by hand",
    "synthetic": "synthetic reference for tests — not a result",
}
COUNTS_TOWARD_PUBLISHED_ERROR = {"notified_ftl"}
OURS_IS = "maximum post-monsoon water extent observed by Sentinel-2 since 2019 — one witness to the FTL, not the FTL"
WITHIN_M = (9, 20, 30, 60)


class RefusedReference(Exception):
    """The reference cannot be compared: it failed a gate or is not usable as a polygon."""

    def __init__(self, reasons: list[str], gates: dict | None = None):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.gates = gates


@dataclass
class Reference:
    kind: str
    polygon_wgs: object
    path: str
    sha256: str
    date: str | None          # the reference's own date: notification, survey, walk, or fetch date
    gates: dict | None        # GateReport of a vertex table; None for a polygon file


def load_reference(path: Path, kind: str, *, perimeter_m: float | None = None, date: str | None = None) -> Reference:
    """Read a reference polygon. A vertex table (.csv: n, lat, lon[, dist_next_m]) must pass
    the three gates first — a table that fails any gate never becomes a polygon. A notified
    FTL is only accepted through that route."""
    if kind not in KINDS:
        raise RefusedReference([f"unknown reference kind '{kind}'. Known: {sorted(KINDS)}"])
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    gates = None
    if path.suffix.lower() == ".csv":
        from .ftl_extract.gates import run_gates, vertices_from_rows
        verts = vertices_from_rows(list(csv.DictReader(path.open())))
        rep = run_gates(verts, perimeter_m)
        gates = rep.as_dict()
        if not rep.passed:
            raise RefusedReference(["vertex table failed the validity gates — routed to manual entry, not compared"]
                                   + rep.reasons, gates)
        poly = Polygon([(v.lon, v.lat) for v in verts])
    else:
        if kind == "notified_ftl":
            raise RefusedReference(["a notified FTL is accepted only as a vertex table (.csv) that passes the three gates"])
        d = json.loads(path.read_text())
        feats = d["features"] if d.get("type") == "FeatureCollection" else [d]
        geoms = [shape(f["geometry"]) for f in feats if f.get("geometry") and f["geometry"]["type"] in ("Polygon", "MultiPolygon")]
        if not geoms:
            raise RefusedReference([f"{path.name} has no Polygon or MultiPolygon"])
        poly = unary_union(geoms)
        if date is None:
            date = next((f.get("properties", {}).get("fetched_utc") for f in feats if f.get("properties", {}).get("fetched_utc")), None)
    if not poly.is_valid or poly.is_empty:
        raise RefusedReference([f"reference polygon is not valid: {explain_validity(poly)}"], gates)
    return Reference(kind, poly, str(path), sha, date, gates)


def _polys(g):
    return [g] if g.geom_type == "Polygon" else list(g.geoms)


def _shore(poly_utm) -> MultiLineString:
    """The outer shoreline(s): exterior rings only, islands dropped."""
    return MultiLineString([list(p.exterior.coords) for p in _polys(poly_utm)])


def _samples(poly_utm, step_m: float) -> np.ndarray:
    """Points every `step_m` along the outer shoreline(s)."""
    out = []
    for line in _shore(poly_utm).geoms:
        n = max(int(math.ceil(line.length / step_m)), 1)
        out.append(shapely.line_interpolate_point(line, np.linspace(0.0, line.length, n, endpoint=False)))
    return np.concatenate(out)


def _stats(d: np.ndarray) -> dict:
    return {"mean": round(float(d.mean()), 1), "median": round(float(np.median(d)), 1),
            "p90": round(float(np.percentile(d, 90)), 1), "p95": round(float(np.percentile(d, 95)), 1),
            "max": round(float(d.max()), 1), "n_samples": int(d.size)}


def _overlapping(g_utm, other_utm) -> tuple[list, list]:
    """Split a line's pieces into those that overlap the other line and those that do not."""
    hit, miss = [], []
    for q in _polys(g_utm):
        (hit if q.intersection(other_utm).area > 1.0 else miss).append(q)   # > 1 m²: numerically nonzero
    return hit, miss


def _piece(q_utm, other_utm, utm) -> dict:
    pt = to_wgs(q_utm.representative_point(), utm)
    return {"acres": round(m2_to_acres(area_m2(to_wgs(q_utm, utm))), 2), "lat": round(pt.y, 6), "lon": round(pt.x, 6),
            "distance_to_other_line_m": round(float(q_utm.distance(other_utm)), 1)}


def compare(ours_wgs, ref_wgs, step_m: float = 5.0) -> tuple[dict, dict]:
    """Returns (metrics, arrays). `arrays` holds the reference-line samples and their gaps
    (UTM + WGS84) for plotting; it is not written to the record."""
    c = ref_wgs.centroid
    utm = utm_crs_for(c.x, c.y)
    o, r = to_utm(ours_wgs, utm), to_utm(ref_wgs, utm)
    shapely.prepare(o)
    inter, union = o.intersection(r).area, o.union(r).area

    # Pair the pieces: a piece with no overlap with the other line is reported, not gap-measured.
    r_hit, r_miss = _overlapping(r, o)
    o_hit, o_miss = _overlapping(o, r)
    no_overlap = not r_hit
    r_meas = unary_union(r_hit) if r_hit else r       # nothing overlaps: measure everything, flag it
    o_meas = unary_union(o_hit) if o_hit else o

    pr, po = _samples(r_meas, step_m), _samples(o_meas, step_m)
    d_r = shapely.distance(pr, o.boundary)            # reference outer line -> any edge of ours
    d_o = shapely.distance(po, r.boundary)            # our outer line -> any edge of the reference
    outside = ~shapely.covers(o, pr)                  # reference reaches beyond our extent here

    a_o, a_r = area_m2(ours_wgs), area_m2(ref_wgs)
    i = int(np.argmax(d_r))
    worst = to_wgs(pr[i], utm)
    metrics = {
        "iou": round(inter / union, 4) if union > 0 else 0.0,
        "area_ours_acres": round(m2_to_acres(a_o), 2),
        "area_ref_acres": round(m2_to_acres(a_r), 2),
        "area_error_pct": round(100.0 * (a_o - a_r) / a_r, 2) if a_r > 0 else None,
        "overlap_acres": round(m2_to_acres(inter), 2),
        "ours_only_acres": round(m2_to_acres(o.area - inter), 2),
        "ref_only_acres": round(m2_to_acres(r.area - inter), 2),
        "gap_ref_to_ours_m": _stats(d_r),
        "gap_ours_to_ref_m": _stats(d_o),
        "hausdorff_m": round(float(max(d_r.max(), d_o.max())), 1),
        "ref_line_outside_ours": round(float(outside.mean()), 4),
        "ref_line_within_m": {str(t): round(float((d_r <= t).mean()), 4) for t in WITHIN_M},
        "worst_gap": {"lat": round(worst.y, 6), "lon": round(worst.x, 6), "gap_m": round(float(d_r[i]), 1),
                      "ref_outside_ours": bool(outside[i])},
        "step_m": step_m,
        "gap_measured_on": ("NO piece of the reference overlaps our line (wrong lake, or no water seen) — every piece measured"
                            if no_overlap else
                            "from the outer shoreline (islands dropped) of each piece that overlaps the other line, "
                            "to any edge of the other line; pieces with no overlap are listed separately"),
        "no_part_overlaps": no_overlap,
        "ref_parts": len(_polys(r)),
        "ref_parts_unmatched": [_piece(q, o, utm) for q in r_miss],
        "ref_unmatched_acres": round(sum(m2_to_acres(area_m2(to_wgs(q, utm))) for q in r_miss), 2),
        "ours_parts": len(_polys(o)),
        "ours_parts_unmatched": [_piece(q, r, utm) for q in o_miss],
        "inner_rings": {"ours": sum(len(p.interiors) for p in _polys(o)), "ref": sum(len(p.interiors) for p in _polys(r))},
    }
    xy = shapely.get_coordinates(to_wgs(shapely.multipoints(pr), utm))
    return metrics, {"ref_lonlat": xy, "gap_m": d_r, "outside": outside,
                     "ref_unmatched_wgs": [to_wgs(q, utm) for q in r_miss]}


def _extent(out_dir: Path):
    feats = json.loads((out_dir / "boundary.geojson").read_text())["features"]
    f = next((f for f in feats if f["properties"].get("layer") == "max_observed_extent"), None)
    return None if f is None else (shape(f["geometry"]), f["properties"])


def validate_lake(root: Path, lake_id: str, ref: Reference, step_m: float = 5.0) -> dict:
    out_dir = root / "outputs" / lake_id
    if not (out_dir / "run.json").exists():
        raise SystemExit(f"no run for '{lake_id}' — run `python -m lakeaudit run --lake {lake_id}` first")
    run = json.loads((out_dir / "run.json").read_text())
    ext = _extent(out_dir)
    if ext is None:
        raise SystemExit(f"run {run['run_id']} produced no max observed extent — nothing to validate")
    ours, props = ext
    metrics, arrays = compare(ours, ref.polygon_wgs, step_m)

    synthetic = run["data_source"] == "sample" or ref.kind == "synthetic"
    if ref.kind not in COUNTS_TOWARD_PUBLISHED_ERROR:
        why = f"reference is {ref.kind}, not a gated notified FTL"
    elif synthetic:
        why = "synthetic data"
    else:
        why = None
    try:
        rel = str(Path(ref.path).resolve().relative_to(root.resolve()))
    except ValueError:
        rel = ref.path
    rec = {
        "lake_id": lake_id,
        "lake_name": run["lake"]["name"],
        "ours": {"layer": "max_observed_extent", "is": OURS_IS, "run_id": run["run_id"], "code_commit": run["code_commit"],
                 "params_hash": run["params_hash"], "data_source": run["data_source"],
                 "scene_ids": props.get("scene_ids") or (run["summary"].get("boundary") or {}).get("scene_ids", []),
                 "years_used": (run["summary"].get("boundary") or {}).get("years_used", [])},
        "reference": {"kind": ref.kind, "is": KINDS[ref.kind], "path": rel, "sha256": ref.sha256, "date": ref.date,
                      "gates": ref.gates},
        "metrics": metrics,
        "synthetic": synthetic,
        "counts_toward_published_error_rate": why is None,
        "why_not_counted": why,
        "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validator_commit": _git_commit(root),
    }
    (out_dir / f"validation_{ref.kind}.json").write_text(json.dumps(rec, indent=2))
    render_validation(out_dir, rec, ours, ref.polygon_wgs, arrays)
    return rec


def render_validation(out_dir: Path, rec: dict, ours_wgs, ref_wgs, arrays: dict) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    m, kind = rec["metrics"], rec["reference"]["kind"]
    g = m["gap_ref_to_ours_m"]
    fig, ax = plt.subplots(figsize=(11.5, 8), dpi=110)
    for p in _polys(ours_wgs):
        ax.plot(*p.exterior.xy, color="#a8123a", lw=2.0, zorder=4)
    for p in _polys(ref_wgs):
        ax.plot(*p.exterior.xy, color="#6b7780", lw=0.8, zorder=3)
    for q in arrays["ref_unmatched_wgs"]:
        ax.fill(*q.exterior.xy, facecolor="none", edgecolor="#43565a", hatch="////", lw=0.9, zorder=3)
    xy, gap = arrays["ref_lonlat"], arrays["gap_m"]
    vmax = max(30.0, float(np.percentile(gap, 99)))
    sc = ax.scatter(xy[:, 0], xy[:, 1], c=gap, cmap="Purples", vmin=0, vmax=vmax, s=7, zorder=5, linewidths=0)
    cb = fig.colorbar(sc, ax=ax, fraction=0.035, pad=0.02)
    cb.set_label("reference line: distance to our line (m)", fontsize=8)
    w = m["worst_gap"]
    ax.scatter([w["lon"]], [w["lat"]], s=90, facecolor="none", edgecolor="#1b1b1b", lw=1.4, zorder=6)
    ax.annotate(f"worst {w['gap_m']:.0f} m", (w["lon"], w["lat"]), xytext=(8, 8), textcoords="offset points", fontsize=8)

    lat0 = sum(ax.get_ylim()) / 2
    ax.set_aspect(1 / math.cos(math.radians(lat0)))
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.set_xlabel("lon"); ax.set_ylabel("lat"); ax.grid(alpha=0.2)
    head = f"{rec['lake_name']} — our line vs {kind}" + ("   [SYNTHETIC — not a result]" if rec["synthetic"] else "")
    line2 = (f"IoU {m['iou']:.2f} · area {m['area_ours_acres']:.1f} ac vs {m['area_ref_acres']:.1f} ac ({m['area_error_pct']:+.1f} %)\n"
             f"gap median {g['median']:.0f} m, p95 {g['p95']:.0f} m, max {g['max']:.0f} m · "
             f"{100 * m['ref_line_within_m']['30']:.0f} % of the reference line within 30 m")
    miss = m["ref_parts_unmatched"]
    if m["no_part_overlaps"]:
        line2 += "\nNO piece of the reference overlaps our line — wrong lake, or no water seen. Gaps cover everything."
    elif miss:
        line2 = line2.replace("gap median", "gap (pieces that overlap) median")
        line2 += (f"\nnot in our line: {len(miss)} reference piece{'s' if len(miss) > 1 else ''}, {m['ref_unmatched_acres']:.1f} ac, "
                  f"{min(u['distance_to_other_line_m'] for u in miss):.0f} m away (hatched)")
    ax.set_title(f"{head}\n{line2}", fontsize=10, loc="left")
    handles = [Line2D([0], [0], color="#a8123a", lw=2.0, label="ours: max observed extent\n(one witness, not the FTL)"),
               Line2D([0], [0], color="#6b7780", lw=0.8, marker="o", markersize=4, markerfacecolor="#7a64a8",
                      markeredgewidth=0, label=f"reference: {kind}\n(dots coloured by gap)")]
    if arrays["ref_unmatched_wgs"] and not m["no_part_overlaps"]:
        from matplotlib.patches import Patch
        handles.append(Patch(facecolor="none", edgecolor="#43565a", hatch="////",
                             label="reference piece with no water\nin our line (not gap-measured)"))
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.18, 1), borderaxespad=0, fontsize=8, frameon=True)
    counted = ("Counts toward the published error rate." if rec["counts_toward_published_error_rate"]
               else f"Does NOT count toward the published error rate: {rec['why_not_counted']}.")
    o = rec["ours"]
    fig.text(0.01, 0.005, f"{counted}  Gaps measured from outer shorelines (islands dropped).\n"
                          f"Reference: {rec['reference']['is']}.\n"
                          f"run {o['run_id']} · commit {o['code_commit']} · params {o['params_hash']} · reference sha256 {rec['reference']['sha256'][:12]}",
             fontsize=7, color="#43565a", va="bottom")
    p = out_dir / f"validation_{kind}.png"
    fig.savefig(p, bbox_inches="tight"); plt.close(fig)
    return p


def _row(rec: dict) -> dict:
    m, g = rec["metrics"], rec["metrics"]["gap_ref_to_ours_m"]
    return {"lake_id": rec["lake_id"], "kind": rec["reference"]["kind"], "reference_date": rec["reference"].get("date"),
            "iou": m["iou"], "area_error_pct": m["area_error_pct"], "gap_median_m": g["median"], "gap_p95_m": g["p95"],
            "gap_max_m": g["max"], "within_30m": m["ref_line_within_m"]["30"],
            "ref_unmatched_parts": len(m.get("ref_parts_unmatched", [])), "ref_unmatched_acres": m.get("ref_unmatched_acres", 0.0),
            "no_part_overlaps": bool(m.get("no_part_overlaps", False)),
            "counted": rec["counts_toward_published_error_rate"], "why_not_counted": rec["why_not_counted"],
            "synthetic": rec["synthetic"], "run_id": rec["ours"]["run_id"], "validated_utc": rec["validated_utc"]}


def lake_validations(out_dir: Path, run_id: str) -> list[dict]:
    """Validation rows for the line this run drew. A record made against an earlier run of
    the lake measured a line that no longer exists, so it is left out."""
    rows = []
    for p in sorted(out_dir.glob("validation_*.json")):
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("ours", {}).get("run_id") == run_id:
            rows.append(_row(rec))
    return rows


def summarize(root: Path) -> dict:
    """Every validation record on disk. A record is stale when its lake has been re-run
    since — the line it measured no longer exists — and is left out of the aggregate."""
    rows = []
    for p in sorted((root / "outputs").glob("*/validation_*.json")):
        rec = json.loads(p.read_text())
        run_p = p.parent / "run.json"
        current = json.loads(run_p.read_text()).get("run_id") if run_p.exists() else None
        rows.append({**_row(rec), "stale": rec["ours"]["run_id"] != current})
    live = [r for r in rows if not r["stale"]]
    by_kind = {}
    for k in sorted({r["kind"] for r in live}):
        ks = [r for r in live if r["kind"] == k]
        by_kind[k] = {"n_lakes": len(ks), "median_iou": round(float(np.median([r["iou"] for r in ks])), 4),
                      "median_gap_p95_m": round(float(np.median([r["gap_p95_m"] for r in ks])), 1),
                      "worst_gap_m": max(r["gap_max_m"] for r in ks)}
    counted = [r for r in live if r["counted"]]
    published = ({"n_lakes": len(counted), "median_iou": round(float(np.median([r["iou"] for r in counted])), 4),
                  "median_gap_p95_m": round(float(np.median([r["gap_p95_m"] for r in counted])), 1),
                  "worst_gap_m": max(r["gap_max_m"] for r in counted)}
                 if counted else {"n_lakes": 0, "status": "not yet measured — no gated notified FTL has been compared"})
    return {"rows": rows, "by_kind": by_kind, "published_error_rate": published}
