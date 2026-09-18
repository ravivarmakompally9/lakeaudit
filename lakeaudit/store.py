"""Results, not images. Every run writes small, queryable files under outputs/<lake>/:

  run.json            provenance + summary
  water_by_year.csv   one row per year: area, threshold, scene id, cloud fraction
  water_by_year.geojson
  boundary.geojson    extent + buffer ring (+ seed polygon for reference)
  structures.geojson  every footprint with zone + distance, provenance-stamped
  rejected_scenes.json

PostGIS is optional: db/schema.sql + load_postgis() ingest exactly these files."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from shapely.geometry import mapping

from .provenance import Provenance, write_run_manifest
from .water import WaterResult
from .boundary import Boundary
from .structures import Structure


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def write_outputs(out_dir: Path, prov: Provenance, lake, waters: list[WaterResult], boundary: Boundary | None,
                  structs: list[Structure], rejected: dict, summary: dict, source: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    # water by year
    with (out_dir / "water_by_year.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "acquired", "scene_id", "area_m2", "area_acres", "threshold", "threshold_method",
                    "roi_cloud_fraction", "n_valid_px", "notes", "run_id", "code_commit", "params_hash", "source"])
        for r in waters:
            w.writerow([r.year, r.acquired, r.scene_id, round(r.area_m2, 1), round(r.area_m2 / 4046.8564, 2),
                        r.threshold, r.threshold_method, r.roi_cloud_fraction, r.n_valid_px, r.notes,
                        prov.run_id, prov.code_commit, prov.params_hash, source])
    feats = []
    for r in waters:
        if r.polygon_wgs is None:
            continue
        feats.append({"type": "Feature", "geometry": mapping(r.polygon_wgs),
                      "properties": prov.stamp({"year": r.year, "acquired": r.acquired, "scene_id": r.scene_id,
                                                "area_m2": round(r.area_m2, 1), "threshold": r.threshold,
                                                "threshold_method": r.threshold_method, "source": source})})
    (out_dir / "water_by_year.geojson").write_text(json.dumps(_fc(feats)))

    # boundary
    bfeats = []
    if lake.seed_polygon is not None:
        bfeats.append({"type": "Feature", "geometry": mapping(lake.seed_polygon),
                       "properties": {"layer": "seed_polygon", "seed_source": lake.seed_source, "osm_ids": lake.seed_osm_ids}})
    if boundary is not None:
        bfeats.append({"type": "Feature", "geometry": mapping(boundary.extent_wgs),
                       "properties": prov.stamp({"layer": "max_observed_extent", **boundary.summary(), "source": source})})
        bfeats.append({"type": "Feature", "geometry": mapping(boundary.buffer_ring_wgs),
                       "properties": prov.stamp({"layer": "buffer_ring", "buffer_width_m": boundary.buffer_width_m, "source": source})})
    (out_dir / "boundary.geojson").write_text(json.dumps(_fc(bfeats)))

    # structures
    sfeats = [{"type": "Feature", "geometry": mapping(s.footprint_wgs),
               "properties": prov.stamp({"structure_id": s.structure_id, "footprint_source": s.source, "zone": s.zone,
                                         "dist_to_extent_m": s.dist_to_extent_m, "footprint_area_m2": s.footprint_area_m2,
                                         **{f"tag_{k}": v for k, v in s.tags.items()}, "source": source})}
              for s in structs]
    (out_dir / "structures.geojson").write_text(json.dumps(_fc(sfeats)))

    (out_dir / "rejected_scenes.json").write_text(json.dumps(rejected, indent=2))
    manifest = write_run_manifest(prov, out_dir, extra={"lake": {"id": lake.id, "name": lake.name, "seed_source": lake.seed_source,
                                                                  "seed_verified": lake.seed_verified,
                                                                  "notification_status": lake.notification_status,
                                                                  "dispute": lake.dispute},
                                                         "data_source": source, "summary": summary})
    return {"run_json": str(manifest)}


# ----------------------------------------------------------------- PostGIS (optional)

def load_postgis(out_dir: Path, dsn: str) -> None:
    """Ingest a run directory into PostGIS. Requires psycopg2 and db/schema.sql applied."""
    import psycopg2  # local import: optional dependency
    run = json.loads((out_dir / "run.json").read_text())
    lake = run["lake"]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("""INSERT INTO pipeline_runs(run_id, code_commit, params_hash, started_utc, data_source, lake_id)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                (run["run_id"], run["code_commit"], run["params_hash"], run["started_utc"], run["data_source"], lake["id"]))
    cur.execute("""INSERT INTO lakes(lake_id, name, seed_source, notification_status, dispute)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (lake_id) DO UPDATE SET seed_source=EXCLUDED.seed_source""",
                (lake["id"], lake["name"], lake["seed_source"], lake["notification_status"], lake["dispute"]))
    for f in json.loads((out_dir / "water_by_year.geojson").read_text())["features"]:
        p = f["properties"]
        cur.execute("""INSERT INTO water_extents(lake_id, year, scene_id, acquired, area_m2, threshold, threshold_method, run_id, geom)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s, ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                       ON CONFLICT (lake_id, year, run_id) DO UPDATE SET geom=EXCLUDED.geom, area_m2=EXCLUDED.area_m2""",
                    (lake["id"], p["year"], p["scene_id"], p["acquired"], p["area_m2"], p["threshold"], p["threshold_method"],
                     p["run_id"], json.dumps(f["geometry"])))
    for f in json.loads((out_dir / "boundary.geojson").read_text())["features"]:
        p = f["properties"]
        if p.get("layer") == "max_observed_extent":
            cur.execute("""INSERT INTO boundaries(lake_id, method, area_m2, buffer_width_m, run_id, geom)
                           VALUES (%s,%s,%s,%s,%s, ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                           ON CONFLICT (lake_id, method, run_id) DO UPDATE SET geom=EXCLUDED.geom""",
                        (lake["id"], p["method"], p["extent_area_m2"], p["buffer_width_m"], p["run_id"], json.dumps(f["geometry"])))
    for f in json.loads((out_dir / "structures.geojson").read_text())["features"]:
        p = f["properties"]
        cur.execute("""INSERT INTO structures(structure_id, lake_id, footprint_source, zone, dist_to_extent_m, footprint_area_m2, run_id, geom)
                       VALUES (%s,%s,%s,%s,%s,%s,%s, ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                       ON CONFLICT (structure_id, run_id) DO NOTHING""",
                    (p["structure_id"], lake["id"], p["footprint_source"], p["zone"], p["dist_to_extent_m"],
                     p["footprint_area_m2"], p["run_id"], json.dumps(f["geometry"])))
    conn.commit()
    cur.close(); conn.close()
