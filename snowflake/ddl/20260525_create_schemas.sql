-- ============================================================================
--  20260525_create_schemas.sql
--  Schemas the data platform owns in CAFC_DB.
--
--  Idempotent: every statement is CREATE … IF NOT EXISTS. Safe to re-run.
--
--  Existing schemas left untouched (already present in CAFC_DB):
--    - IMPECT_RAW       provider raw landing zone (IMPECT API)
--    - MANUAL           CSV / sheets landing zone
--    - CORE             canonical layer (identities, dimensions, facts,
--                       plus platform tables owned by the app)
--    - APP_COMPAT       legacy-shape views for the recruitment-platform app
--    - MIGRATION        historical one-shot translation tables (read-only)
--
--  This file only creates schemas that are new in this plan.
-- ============================================================================

USE DATABASE CAFC_DB;

-- GPS / sport-science raw landing zone.
-- Future GPS pipeline (Catapult / STATSports) will COPY INTO tables under
-- this schema from an external stage via a Snowflake Task (see
-- snowflake/tasks/gps_<vendor>.sql when a vendor is chosen).
CREATE SCHEMA IF NOT EXISTS CAFC_DB.GPS_RAW
  COMMENT = 'Raw GPS / sport-science exports. Owned by data platform; written by Snowflake Tasks via COPY INTO from external stages.';

-- Reserved provider schemas — uncomment when the provider is onboarded.
-- Keeping them as comments documents intent without creating empty schemas
-- that show up in INFORMATION_SCHEMA before they have a real owner.
--
-- CREATE SCHEMA IF NOT EXISTS CAFC_DB.WYSCOUT_RAW
--   COMMENT = 'Reserved for Wyscout raw landing zone.';
--
-- CREATE SCHEMA IF NOT EXISTS CAFC_DB.STATSBOMB_RAW
--   COMMENT = 'Reserved for Statsbomb raw landing zone.';
