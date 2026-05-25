-- ============================================================================
--  20260525_grants.sql
--  Two roles that bound what each repo can do inside CAFC_DB.
--
--  DATA_PLATFORM_ROLE — used by this repo (orchestrator, dbt, identity loader)
--    Writes:  IMPECT_RAW.*, MANUAL.*, GPS_RAW.*, APP_COMPAT.*, the dbt-managed
--             canonical tables in CORE (dimensions, facts, identities)
--    Reads:   CORE.* (incl. app-owned platform tables), MIGRATION.* (frozen)
--    Cannot:  write to app-owned platform tables in CORE
--
--  APP_ROLE — used by the recruitment-platform backend
--    Writes:  only the app-owned platform tables in CORE (see allow-list below)
--    Reads:   CORE.*, APP_COMPAT.*
--    Cannot:  touch raw landing zones, MIGRATION, or canonical dbt tables
--
--  Idempotent: CREATE ROLE IF NOT EXISTS + GRANT (re-applying GRANT is a no-op).
--
--  -------------------------------------------------------------------------
--  Resolved against live CAFC_DB on 2026-05-25:
--    - Warehouse DEVELOPMENT_WH (from python/extract/impect/.env).
--    - CORE today contains only FIXTURES, FIXTURE_IDENTITIES, PLAYERS,
--      PLAYER_IDENTITIES, USERS. The first four are canonical (data platform
--      writes them); only USERS is app-owned right now. Scout reports / notes
--      / shortlists / recommendations are still in RECRUITMENT_TEST.PUBLIC
--      and will be moved into CORE by the recruitment-platform cutover (plan
--      Part 3) — at that point this file gets new INSERT/UPDATE/DELETE grants
--      for each table as it lands.
--
--  STILL TO DO BEFORE APPLYING:
--    1. Decide who gets DATA_PLATFORM_ROLE / APP_ROLE granted to them — at
--       minimum, the user behind SNOWFLAKE_USER in .env (HUMARJI) needs
--       DATA_PLATFORM_ROLE, and whichever service account the FastAPI backend
--       uses needs APP_ROLE. Those GRANT TO USER … statements are deliberately
--       NOT in this file so per-environment assignments don't get committed —
--       do them in a worksheet.
--    2. If you keep using DEV_ROLE (the current role per .env) for local
--       development, decide whether to grant DATA_PLATFORM_ROLE to DEV_ROLE
--       or to switch .env over to SNOWFLAKE_ROLE=DATA_PLATFORM_ROLE once the
--       new role is provisioned.
-- ============================================================================

USE DATABASE CAFC_DB;

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
CREATE ROLE IF NOT EXISTS DATA_PLATFORM_ROLE
  COMMENT = 'Owned by the cafc-data-platform repo. Writes raw landing zones, dbt-managed canonical tables, and app_compat views. Cannot write app-owned platform tables in CORE.';

CREATE ROLE IF NOT EXISTS APP_ROLE
  COMMENT = 'Owned by the recruitment-platform repo. Reads CORE + APP_COMPAT; writes only the app-owned platform tables in CORE.';

-- Both roles roll up to SYSADMIN so an admin can still manage them.
GRANT ROLE DATA_PLATFORM_ROLE TO ROLE SYSADMIN;
GRANT ROLE APP_ROLE          TO ROLE SYSADMIN;

-- ---------------------------------------------------------------------------
-- Warehouse access
-- ---------------------------------------------------------------------------
GRANT USAGE ON WAREHOUSE DEVELOPMENT_WH TO ROLE DATA_PLATFORM_ROLE;
GRANT USAGE ON WAREHOUSE DEVELOPMENT_WH TO ROLE APP_ROLE;

-- ---------------------------------------------------------------------------
-- Database access
-- ---------------------------------------------------------------------------
GRANT USAGE ON DATABASE CAFC_DB TO ROLE DATA_PLATFORM_ROLE;
GRANT USAGE ON DATABASE CAFC_DB TO ROLE APP_ROLE;

-- ============================================================================
--  DATA_PLATFORM_ROLE
-- ============================================================================

-- Raw landing zones — full ownership-equivalent.
-- USAGE on the schema, plus ALL on existing + future tables/views/stages.
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE STAGE, CREATE FILE FORMAT, CREATE SEQUENCE
  ON SCHEMA CAFC_DB.IMPECT_RAW TO ROLE DATA_PLATFORM_ROLE;
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE STAGE, CREATE FILE FORMAT, CREATE SEQUENCE
  ON SCHEMA CAFC_DB.MANUAL     TO ROLE DATA_PLATFORM_ROLE;
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE STAGE, CREATE FILE FORMAT, CREATE SEQUENCE
  ON SCHEMA CAFC_DB.GPS_RAW    TO ROLE DATA_PLATFORM_ROLE;

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA CAFC_DB.IMPECT_RAW TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA CAFC_DB.MANUAL     TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA CAFC_DB.GPS_RAW    TO ROLE DATA_PLATFORM_ROLE;

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CAFC_DB.IMPECT_RAW TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CAFC_DB.MANUAL     TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CAFC_DB.GPS_RAW    TO ROLE DATA_PLATFORM_ROLE;

-- APP_COMPAT schema — data platform creates and maintains the legacy-shape views.
GRANT USAGE, CREATE VIEW ON SCHEMA CAFC_DB.APP_COMPAT TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON ALL VIEWS    IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE DATA_PLATFORM_ROLE;

-- CORE schema — data platform can create tables (dbt models) and read everything.
-- Write privileges on app-owned tables are intentionally NOT granted; see the
-- block below where APP_ROLE gets exclusive write on the allow-list.
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE ON SCHEMA CAFC_DB.CORE TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON ALL TABLES    IN SCHEMA CAFC_DB.CORE TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON ALL VIEWS     IN SCHEMA CAFC_DB.CORE TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CAFC_DB.CORE TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA CAFC_DB.CORE TO ROLE DATA_PLATFORM_ROLE;

-- Future tables that DATA_PLATFORM_ROLE creates in CORE: it owns them outright,
-- so writes are implicit via ownership. No extra grant needed.

-- MIGRATION schema — frozen historical reference. Read-only, no future writes.
GRANT USAGE ON SCHEMA CAFC_DB.MIGRATION TO ROLE DATA_PLATFORM_ROLE;
GRANT SELECT ON ALL TABLES IN SCHEMA CAFC_DB.MIGRATION TO ROLE DATA_PLATFORM_ROLE;

-- ============================================================================
--  APP_ROLE
-- ============================================================================

-- Read everything the app needs.
GRANT USAGE ON SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;
GRANT SELECT ON ALL VIEWS    IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CAFC_DB.APP_COMPAT TO ROLE APP_ROLE;

GRANT USAGE ON SCHEMA CAFC_DB.CORE TO ROLE APP_ROLE;
GRANT SELECT ON ALL TABLES    IN SCHEMA CAFC_DB.CORE TO ROLE APP_ROLE;
GRANT SELECT ON ALL VIEWS     IN SCHEMA CAFC_DB.CORE TO ROLE APP_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CAFC_DB.CORE TO ROLE APP_ROLE;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA CAFC_DB.CORE TO ROLE APP_ROLE;

-- ---------------------------------------------------------------------------
-- App-owned platform tables in CORE.
-- As of 2026-05-25 the only app-owned table in CORE is USERS (51 rows of
-- application user accounts). Scout reports, notes, shortlists, and
-- recommendations are still in RECRUITMENT_TEST.PUBLIC and will move into
-- CORE during the recruitment-platform cutover (plan Part 3). At that point,
-- add a GRANT line here for each table as it lands.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE CAFC_DB.CORE.USERS TO ROLE APP_ROLE;

-- Pending (uncomment as each table lands in CORE during the platform cutover):
-- GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE CAFC_DB.CORE.SCOUT_REPORTS    TO ROLE APP_ROLE;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE CAFC_DB.CORE.SCOUT_NOTES      TO ROLE APP_ROLE;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE CAFC_DB.CORE.SHORTLISTS       TO ROLE APP_ROLE;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE CAFC_DB.CORE.RECOMMENDATIONS  TO ROLE APP_ROLE;
