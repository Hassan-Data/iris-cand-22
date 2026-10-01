-- 002: candidate screening views, one per vertical. Refreshed by the pipeline.
-- Every band comes with a plain-language reason, so the screening is explainable.

CREATE MATERIALIZED VIEW mart.bess_candidates AS
WITH nearest AS (
    SELECT b.*,
           s.substation_id, s.name AS substation_name, s.voltage_kv,
           ST_Distance(b.geom, s.geom) AS dist_m,
           ST_Area(b.geom)             AS area
    FROM core.bess_sites b
    LEFT JOIN LATERAL (
        SELECT * FROM core.grid_substations s
        WHERE s.country_code = b.country_code          -- country-scoped join
        ORDER BY s.geom <-> b.geom
        LIMIT 1
    ) s ON true
)
SELECT country_code,
       source_id,
       name,
       round(area)::bigint                 AS area_m2,
       substation_id                        AS nearest_substation_id,
       substation_name                      AS nearest_substation_name,
       voltage_kv                           AS nearest_substation_kv,
       round(dist_m)::integer               AS substation_distance_m,
       CASE WHEN substation_id IS NULL                   THEN 'unscreened'
            WHEN dist_m <= 2000 AND area >= 20000        THEN 'A'
            WHEN dist_m <= 5000                          THEN 'B'
            ELSE 'C' END                                 AS screening_band,
       CASE WHEN substation_id IS NULL
                THEN 'No substation on record for this country; distance not assessed'
            WHEN dist_m <= 2000 AND area >= 20000
                THEN 'Substation within 2 km and parcel at least 2 ha'
            WHEN dist_m <= 5000
                THEN 'Substation within 5 km'
            ELSE 'Nearest substation more than 5 km away' END AS screening_reason,
       source_name,
       source_date,
       uncertainty,
       geom
FROM nearest;

CREATE UNIQUE INDEX bess_candidates_pk ON mart.bess_candidates (country_code, source_id);
CREATE MATERIALIZED VIEW mart.peatland_candidates AS
WITH base AS (
    SELECT p.*,
           ST_Area(p.geom) AS area,
           f.value         AS eco_points_per_m2,
           f.basis         AS eco_points_basis
    FROM core.peatland_sites p
    LEFT JOIN core.commercial_assumptions f
           ON f.country_code = p.country_code           -- country-scoped join
          AND f.assumption_key = 'eco_points_per_m2'
)
SELECT country_code,
       source_id,
       name,
       round(area)::bigint                                AS area_m2,
       peat_depth_m,
       eco_points_per_m2,
       eco_points_basis,
       round(area * eco_points_per_m2)::bigint            AS eco_points_indicative,  -- NULL if no factor
       CASE WHEN peat_depth_m IS NULL                     THEN 'unscreened'
            WHEN peat_depth_m >= 1.0 AND area >= 50000    THEN 'A'
            WHEN peat_depth_m >= 0.3                      THEN 'B'
            ELSE 'C' END                                  AS screening_band,
       CASE WHEN peat_depth_m IS NULL
                THEN 'Peat depth unknown; not screened'
            WHEN peat_depth_m >= 1.0 AND area >= 50000
                THEN 'Peat depth at least 1.0 m and area at least 5 ha'
            WHEN peat_depth_m >= 0.3
                THEN 'Peat depth at least 0.3 m'
            ELSE 'Peat layer thinner than 0.3 m' END      AS screening_reason,
       source_name,
       source_date,
       uncertainty,
       geom
FROM base;

CREATE UNIQUE INDEX peatland_candidates_pk ON mart.peatland_candidates (country_code, source_id);