-- ============================================================================
--  20260727_skillcorner_raw_schema.sql
--  Raw landing zone for SkillCorner physical/tracking data, onboarded per the
--  provider-integration framework in docs/architecture-review-2026-06.md §6.
--  Previously only a placeholder mention in docs -- this is the first real
--  build for this provider.
--
--  Confirmed live before writing this (auth undocumented publicly -- see
--  python/extract/skillcorner/skillcorner_api.py for how it was determined):
--    - The club's account has real match data for only 2 Championship
--      editions: 2024/2025 and 2025/2026 (557 matches each, matching DVMS's
--      count for the same competition/season). Every earlier edition back to
--      2019/2020 is listed in SkillCorner's own /competition_editions/
--      catalogue but returns zero matches for this account -- same "listed
--      but not entitled" pattern seen with Impect's dataVersion boundary,
--      just gated by account access rather than a data-collection start date.
--    - This is physical-summary data (one row per player per match), NOT
--      event-level/positional tracking -- GET /match/{id}/physical/ returns
--      a flat CSV, no nested/conditional structure, so (unlike the Impect
--      events table) there's no need for VARIANT detail columns here.
--
--  PHYSICAL_SUMMARY's table DDL is intentionally NOT hand-written here: at
--  178 columns of uniformly-typed physical metrics (repeated across
--  possession-state x half splits, e.g. "HSR Distance TIP", "Sprint Count 1
--  OTIP"), it matches the shape of this platform's existing wide
--  reference/aggregate tables (IMPECT_RAW.ITERATION_PLAYER_KPIS etc.), which
--  are also loaded via write_pandas(..., auto_create_table=True) rather than
--  hand-authored DDL. See load_physical_summary.py.
--
--  Idempotent: CREATE ... IF NOT EXISTS. Safe to re-run. No grants to
--  DATA_PLATFORM_ROLE: not provisioned in this account yet (see
--  [[data-platform-role-not-provisioned]]) -- DEV_ROLE owns this by creation.
-- ============================================================================

USE DATABASE CAFC_DB;

CREATE SCHEMA IF NOT EXISTS CAFC_DB.SKILLCORNER_RAW
  COMMENT = 'Raw data landed verbatim from the SkillCorner API by python/extract/skillcorner/. Physical/tracking data (not event-level). Owned by data platform.';
