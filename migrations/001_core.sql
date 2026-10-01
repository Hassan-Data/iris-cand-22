-- 001: schemas, staging, core entities, change tracking.
-- Contract: geometry column is always `geom`, CRS is EPSG:25832 (metres),
-- every business table has a non-null country_code.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

-- When did each core table last change? (filled automatically by a trigger)
CREATE TABLE ops.source_changes (
    table_name  text PRIMARY KEY,
    changed_at  timestamptz NOT NULL
);

-- When was each view / export last built successfully, and from which input state?
CREATE TABLE ops.pipeline_state (
    object_name       text PRIMARY KEY,
    completed_at      timestamptz NOT NULL,
    source_watermark  timestamptz,
    row_count         integer NOT NULL,
    run_id            text NOT NULL
);

-- Trigger function: whenever a row in a core table changes, record the time.
CREATE FUNCTION ops.touch_source_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ops.source_changes (table_name, changed_at)
    VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, clock_timestamp())
    ON CONFLICT (table_name) DO UPDATE SET changed_at = excluded.changed_at;
    RETURN NULL;
END $$;

-- Raw submissions exactly as delivered. geom is NOT validated here on purpose.
CREATE TABLE staging.site_submissions (
    country_code   char(2) NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    source_id      text    NOT NULL,
    vertical       text    NOT NULL CHECK (vertical IN ('bess', 'peatland')),
    name           text    NOT NULL,
    review_status  text    NOT NULL CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    geom           geometry,
    source_name    text    NOT NULL,
    source_date    date    NOT NULL,
    uncertainty    text    NOT NULL CHECK (uncertainty IN ('low', 'medium', 'high')),
    peat_depth_m   numeric,
    PRIMARY KEY (country_code, source_id)
);

CREATE TABLE core.grid_substations (
    country_code   char(2) NOT NULL,
    substation_id  text    NOT NULL,
    name           text    NOT NULL,
    voltage_kv     integer NOT NULL,
    geom           geometry(Point, 25832) NOT NULL,
    source_name    text    NOT NULL,
    source_date    date    NOT NULL,
    PRIMARY KEY (country_code, substation_id)
);
CREATE INDEX grid_substations_geom_idx ON core.grid_substations USING gist (geom);

-- Country-specific business assumptions, e.g. 8 eco-points per m2 applies to DE only.
CREATE TABLE core.commercial_assumptions (
    country_code    char(2) NOT NULL,
    assumption_key  text    NOT NULL,
    value           numeric NOT NULL,
    unit            text    NOT NULL,
    basis           text    NOT NULL,
    PRIMARY KEY (country_code, assumption_key)
);

CREATE TABLE core.bess_sites (
    country_code  char(2) NOT NULL,
    source_id     text    NOT NULL,
    name          text    NOT NULL,
    geom          geometry(MultiPolygon, 25832) NOT NULL,
    source_name   text    NOT NULL,
    source_date   date    NOT NULL,
    uncertainty   text    NOT NULL,
    promoted_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, source_id)
);
CREATE INDEX bess_sites_geom_idx ON core.bess_sites USING gist (geom);

CREATE TABLE core.peatland_sites (
    country_code  char(2) NOT NULL,
    source_id     text    NOT NULL,
    name          text    NOT NULL,
    geom          geometry(MultiPolygon, 25832) NOT NULL,
    peat_depth_m  numeric,              -- NULL = unknown; we never guess it
    source_name   text    NOT NULL,
    source_date   date    NOT NULL,
    uncertainty   text    NOT NULL,
    promoted_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (country_code, source_id)
);
CREATE INDEX peatland_sites_geom_idx ON core.peatland_sites USING gist (geom);

CREATE TRIGGER track_change AFTER INSERT OR UPDATE OR DELETE ON core.grid_substations
    FOR EACH ROW EXECUTE FUNCTION ops.touch_source_change();
CREATE TRIGGER track_change AFTER INSERT OR UPDATE OR DELETE ON core.commercial_assumptions
    FOR EACH ROW EXECUTE FUNCTION ops.touch_source_change();
CREATE TRIGGER track_change AFTER INSERT OR UPDATE OR DELETE ON core.bess_sites
    FOR EACH ROW EXECUTE FUNCTION ops.touch_source_change();
CREATE TRIGGER track_change AFTER INSERT OR UPDATE OR DELETE ON core.peatland_sites
    FOR EACH ROW EXECUTE FUNCTION ops.touch_source_change();