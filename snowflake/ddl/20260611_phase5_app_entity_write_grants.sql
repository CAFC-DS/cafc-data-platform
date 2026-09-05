-- ============================================================================
-- Phase 5: grants for app-minted players and matches (run at cutover, after
-- the Phase 3 clone script, with a role that owns CAFC_DB.CORE — DEV_ROLE
-- today).
--
-- From the full-cutover state the recruitment backend mints manual players
-- and matches straight into the canonical entity tables (mirroring
-- python/identity/mint.py): sequence NEXTVAL + entity row + a MANUAL identity
-- row. INSERT only — the app never UPDATEs or deletes canonical entities;
-- corrections go through merge.py / identity overrides.
-- ============================================================================
--
-- Found by the 2026-09-05 full-cutover rehearsal: table-level grants alone
-- are not enough — without schema-level USAGE, APP_ROLE's session can
-- CONNECT with database=CAFC_DB/schema=CORE but CURRENT_SCHEMA() silently
-- comes back NULL and every unqualified query fails ("Snowflake connection
-- error"). Neither this script nor 20260610_phase3 / 20260611_phase4
-- granted schema-level USAGE before this. Idempotent — safe to re-run.
-- ============================================================================

GRANT USAGE ON SCHEMA CAFC_DB.CORE        TO ROLE APP_ROLE;
GRANT USAGE ON SCHEMA CAFC_DB.APP_COMPAT  TO ROLE APP_ROLE;

-- Schema USAGE doesn't grant SELECT on the objects inside it -- the app
-- reads exclusively through APP_COMPAT (read_table() -> CAFC_DB.APP_COMPAT.x),
-- so APP_ROLE needs SELECT on every object there, including ones dbt
-- recreates on future builds (CREATE OR REPLACE drops+recreates the grant
-- target, so ALL TABLES/VIEWS alone isn't enough -- FUTURE is required too).
GRANT SELECT ON ALL TABLES     IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;
GRANT SELECT ON ALL VIEWS      IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;
GRANT SELECT ON FUTURE TABLES  IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;
GRANT SELECT ON FUTURE VIEWS   IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;

GRANT SELECT, INSERT ON CAFC_DB.CORE.PLAYERS            TO ROLE APP_ROLE;
GRANT SELECT, INSERT ON CAFC_DB.CORE.PLAYER_IDENTITIES  TO ROLE APP_ROLE;
GRANT SELECT, INSERT ON CAFC_DB.CORE.FIXTURES           TO ROLE APP_ROLE;
GRANT SELECT, INSERT ON CAFC_DB.CORE.FIXTURE_IDENTITIES TO ROLE APP_ROLE;

GRANT USAGE ON SEQUENCE CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ  TO ROLE APP_ROLE;
GRANT USAGE ON SEQUENCE CAFC_DB.CORE.CAFC_FIXTURE_ID_SEQ TO ROLE APP_ROLE;
