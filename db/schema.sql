-- lakeaudit PostGIS schema. Every derived row carries run_id -> pipeline_runs,
-- which carries the code commit and the params hash. That is provenance.
--
--   docker run --name lakeaudit-db -e POSTGRES_PASSWORD=lakeaudit -p 5432:5432 -d postgis/postgis:16-3.4
--   psql postgresql://postgres:lakeaudit@localhost:5432/postgres -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id        text PRIMARY KEY,
  code_commit   text NOT NULL,
  params_hash   text NOT NULL,
  started_utc   timestamptz NOT NULL,
  data_source   text NOT NULL CHECK (data_source IN ('stac','sample')),
  lake_id       text NOT NULL
);

CREATE TABLE IF NOT EXISTS lakes (
  lake_id              text PRIMARY KEY,
  name                 text NOT NULL,
  seed_source          text,
  notification_status  text,
  dispute              boolean DEFAULT false
);

-- one row per (lake, year, run). Reruns with the same run_id overwrite; a new
-- run_id is a new row. Nothing is ever silently replaced.
CREATE TABLE IF NOT EXISTS water_extents (
  lake_id          text REFERENCES lakes(lake_id),
  year             int  NOT NULL,
  scene_id         text NOT NULL,
  acquired         date,
  area_m2          double precision,
  threshold        double precision,
  threshold_method text,
  run_id           text REFERENCES pipeline_runs(run_id),
  geom             geometry(MultiPolygon,4326),
  PRIMARY KEY (lake_id, year, run_id)
);

CREATE TABLE IF NOT EXISTS boundaries (
  lake_id         text REFERENCES lakes(lake_id),
  method          text NOT NULL,          -- max_observed_extent | notified | toposheet_1971
  area_m2         double precision,
  buffer_width_m  double precision,
  run_id          text REFERENCES pipeline_runs(run_id),
  geom            geometry(MultiPolygon,4326),
  PRIMARY KEY (lake_id, method, run_id)
);

CREATE TABLE IF NOT EXISTS structures (
  structure_id      text NOT NULL,
  lake_id           text REFERENCES lakes(lake_id),
  footprint_source  text,
  zone              text CHECK (zone IN ('inside_extent','in_buffer','outside')),
  dist_to_extent_m  double precision,
  footprint_area_m2 double precision,
  first_seen_year   int,                  -- filled by the dating stage (2016-2023), NULL until then
  dating_gap_months int,                  -- width of the largest cloud gap bounding the date
  run_id            text REFERENCES pipeline_runs(run_id),
  geom              geometry(MultiPolygon,4326),
  PRIMARY KEY (structure_id, run_id)
);

-- Human-in-the-loop. Nothing reaches the public site unless status = 'confirmed'.
CREATE TABLE IF NOT EXISTS reviews (
  review_id     serial PRIMARY KEY,
  structure_id  text NOT NULL,
  run_id        text NOT NULL,
  status        text NOT NULL CHECK (status IN ('pending','confirmed','rejected','uncertain')),
  reviewer      text,
  notes         text,
  reviewed_utc  timestamptz DEFAULT now(),
  FOREIGN KEY (structure_id, run_id) REFERENCES structures(structure_id, run_id)
);

-- Spatial indexes. USING GIST is not optional.
CREATE INDEX IF NOT EXISTS water_extents_geom_gix ON water_extents USING GIST (geom);
CREATE INDEX IF NOT EXISTS boundaries_geom_gix    ON boundaries    USING GIST (geom);
CREATE INDEX IF NOT EXISTS structures_geom_gix    ON structures    USING GIST (geom);

-- The one query the public site runs. ST_Intersects, not ST_Contains:
-- ST_Contains returns false for a point exactly on the boundary.
--   SELECT lake_id, method,
--          ST_Intersects(geom, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)) AS inside,
--          ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography) AS dist_m
--   FROM boundaries WHERE method='max_observed_extent'
--   ORDER BY dist_m LIMIT 1;
