-- ============================================================================
-- Add SOURCE_HOME_SQUAD_NAME / SOURCE_AWAY_SQUAD_NAME to CORE.FIXTURE_IDENTITIES
--
-- WHY: manually-added ("internal") fixtures came from the legacy app, which
-- stores denormalised team names on RECRUITMENT_TEST.PUBLIC.MATCHES
-- (HOMESQUADNAME / AWAYSQUADNAME). The canonical model keeps only squad *ids*
-- on CORE.FIXTURES and resolves names via CORE_SQUADS at read time. For
-- manual fixtures that squad id is often a fabricated placeholder (or NULL),
-- so APP_COMPAT.MATCHES renders them nameless and every scout report tied to
-- one shows Fixture = "N/A" (worked on legacy, broke at the 2026-09-06
-- canonical cutover).
--
-- FIXTURE_IDENTITIES is the right home: it already carries the source
-- system's view of a fixture (SOURCE_FIXTURE_ID, SOURCE_HOME_SQUAD_ID, ...).
-- These two columns hold what the source system *called* the squads, for the
-- residual where no real CAFC_SQUAD_ID resolves. APP_COMPAT.MATCHES then
-- coalesces CORE_SQUADS.SQUAD_NAME with these.
--
-- Run as the CORE owner (DEV_ROLE). Idempotent.
-- Followed by:
--   1. python -m python.identity.mint_legacy_manual_fixtures --apply
--   2. 20260907_backfill_manual_fixture_squads.sql
--   3. 20260907_remap_legacy_manual_fixture_ids.sql
--   4. dbt build --select app_compat.matches --target prod
-- ============================================================================

ALTER TABLE CAFC_DB.CORE.FIXTURE_IDENTITIES
    ADD COLUMN IF NOT EXISTS SOURCE_HOME_SQUAD_NAME VARCHAR;

ALTER TABLE CAFC_DB.CORE.FIXTURE_IDENTITIES
    ADD COLUMN IF NOT EXISTS SOURCE_AWAY_SQUAD_NAME VARCHAR;

COMMENT ON COLUMN CAFC_DB.CORE.FIXTURE_IDENTITIES.SOURCE_HOME_SQUAD_NAME IS
    'Home team name as recorded by the source system, for MANUAL fixtures (from legacy MATCHES.HOMESQUADNAME). APP_COMPAT.MATCHES name fallback when HOME_SQUAD_ID does not resolve in CORE_SQUADS.';

COMMENT ON COLUMN CAFC_DB.CORE.FIXTURE_IDENTITIES.SOURCE_AWAY_SQUAD_NAME IS
    'Away team name as recorded by the source system. See SOURCE_HOME_SQUAD_NAME.';
