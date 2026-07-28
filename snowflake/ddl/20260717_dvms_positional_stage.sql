-- ============================================================================
--  20260717_dvms_positional_stage.sql
--  Snowflake internal stage for the raw positional tracking feed (Second
--  Spectrum Data JSONL, ASSET_SUBTYPE=38) that DVMS_RAW.ASSETS.RAW_PAYLOAD
--  deliberately never holds — confirmed live at ~420MB/match, ~200GB/season,
--  far past the 16MB unqualified-VARCHAR ceiling. Per user decision
--  (2026-07-17): land it via a Snowflake internal stage (uses existing
--  Snowflake storage, no external S3/Azure account needed) rather than
--  skipping it or standing up external cloud storage.
--
--  Only the JSONL format is staged, not the XML (same data, see
--  python/extract/dvms/load_dvms_positional.py) — no reason to pay for both.
--
--  Idempotent: CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS. Safe to
--  re-run. Same no-DATA_PLATFORM_ROLE-grants rationale as
--  20260716_create_dvms_raw_schema.sql — DEV_ROLE owns what it creates.
-- ============================================================================

USE DATABASE CAFC_DB;

-- STAGED_AT marks an asset as already uploaded to POSITIONAL_STAGE, the same
-- "check what's already done before repeating expensive work" pattern
-- load_dvms_fixtures.py uses for small-asset downloads (_load_known_state) —
-- necessary here even more so, since each file is ~420MB.
ALTER TABLE CAFC_DB.DVMS_RAW.ASSETS
  ADD COLUMN IF NOT EXISTS STAGED_AT TIMESTAMP_NTZ
  COMMENT 'When this asset''s full content was PUT to DVMS_RAW.POSITIONAL_STAGE. NULL if not staged (the normal case for every asset type except the positional feed — see load_dvms_positional.py). The staged file path is @DVMS_RAW.POSITIONAL_STAGE/<ASSET_KEY>, not a separate column, since ASSET_KEY already deterministically encodes it.';

CREATE STAGE IF NOT EXISTS CAFC_DB.DVMS_RAW.POSITIONAL_STAGE
  COMMENT = 'Raw positional tracking files (Second Spectrum Data JSONL, ~420MB/match), PUT here by python/extract/dvms/load_dvms_positional.py using each asset''s ASSET_KEY as the file path. Not inlined in ASSETS.RAW_PAYLOAD — see DVMS_RAW.ASSETS.STAGED_AT.';
