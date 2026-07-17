-- ============================================================================
--  20260716_create_dvms_raw_schema.sql
--  Raw landing zone for the Premier League DVMS portal (dvms.premierleague.com,
--  a Hudl-built system), onboarded per the provider-integration framework in
--  docs/architecture-review-2026-06.md §6.
--
--  Originally built as OPTA_RAW, renamed to DVMS_RAW before any data landed:
--  DVMS bundles multiple providers under one fixture record, confirmed via
--  its documented API (dvms.premierleague.com/dvms/api-docs) —
--    - Opta: F7 (team sheet) + F24 (event data), XML
--    - Second Spectrum (branded GeniusIQ in the UI): tracking/physical data,
--      in DAT/JSON/JSONL/XML/CSV depending on sub-feed
--    - Video (broadcast/tactical), plus panoramic
--  "OPTA_RAW" would have been a misnomer for a schema holding all of that.
--
--  Scope: raw ingestion only. Two tables mirroring the real API's two-level
--  structure (fixture -> assets), not the flat single-table guess from the
--  first pass, which assumed a JSON folder listing that turned out not to
--  exist — the actual API returns each fixture's full asset list inline.
--  Linking DVMS rows to CORE.PLAYERS / CORE.FIXTURES (identity resolution)
--  is a separate, not-yet-built follow-up: python/identity/matcher.py is
--  currently hardcoded to IMPECT_RAW.PLAYERS and needs generalising first.
--
--  Idempotent: every statement is CREATE ... IF NOT EXISTS. Safe to re-run.
--
--  No grants to DATA_PLATFORM_ROLE: that role doesn't exist in this account
--  (see [[data-platform-role-not-provisioned]] memory / SHOW GRANTS ON
--  SCHEMA CAFC_DB.IMPECT_RAW for confirmation) — DEV_ROLE (the role that
--  runs this file) already has full rights on what it creates by ownership.
-- ============================================================================

USE DATABASE CAFC_DB;

DROP SCHEMA IF EXISTS CAFC_DB.OPTA_RAW;

CREATE SCHEMA IF NOT EXISTS CAFC_DB.DVMS_RAW
  COMMENT = 'Raw data landed verbatim from the Premier League DVMS portal (dvms.premierleague.com) by python/extract/dvms/. Bundles Opta event data, Second Spectrum tracking data, and video metadata. Owned by data platform.';

-- ---------------------------------------------------------------------------
-- DVMS_RAW.FIXTURES
--   One row per fixture returned by POST /dvms/{competitionId}/fixtures/{season}.
--   RAW_PAYLOAD keeps the full verbatim fixture JSON (including its nested
--   assets array) as the replay buffer; the typed columns are pulled out for
--   convenient querying. DVMS_RAW.ASSETS below is the normalized one-row-
--   per-asset breakout of the same nested array — both exist so neither the
--   full source shape nor query convenience is sacrificed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.DVMS_RAW.FIXTURES (
  FIXTURE_ID           VARCHAR(100)   NOT NULL  COMMENT 'Hudl fixture id (Mongo-style ObjectId string).',
  COMPETITION_ID        VARCHAR(100)  NOT NULL  COMMENT 'Hudl competition id, e.g. the EFL Championship id used in the API path.',
  SEASON                VARCHAR(20)   NOT NULL  COMMENT 'Season key used in the API path, e.g. "2025" for the 2025-26 season.',
  OPTA_MATCH_ID          VARCHAR(50)            COMMENT 'Opta match id, e.g. "g2634377". The join key once identity resolution exists.',
  OPTA_HOME_TEAM_ID       VARCHAR(50)           COMMENT 'Opta team id for the home side, e.g. "t20".',
  OPTA_AWAY_TEAM_ID       VARCHAR(50)           COMMENT 'Opta team id for the away side.',
  HOME_TEAM_NAME          VARCHAR(200)          COMMENT 'Home team display name as DVMS has it.',
  AWAY_TEAM_NAME          VARCHAR(200)          COMMENT 'Away team display name as DVMS has it.',
  MATCH_DATE               TIMESTAMP_NTZ        COMMENT 'Kickoff date/time (UTC per the API).',
  ROUND                     VARCHAR(20)         COMMENT 'Round/matchday number as a string per the source.',
  HOME_SCORE                 VARCHAR(20)        COMMENT 'Home score as a string per the source (may be null pre-match).',
  AWAY_SCORE                  VARCHAR(20)       COMMENT 'Away score as a string per the source.',
  VENUE_ID                     VARCHAR(50)      COMMENT 'DVMS venue id.',
  VENUE_NAME                    VARCHAR(200)    COMMENT 'Venue display name.',
  NUM_ASSETS_AVAILABLE            NUMBER(38,0)  COMMENT 'Count of assets DVMS reports as available for this fixture at load time.',
  RAW_PAYLOAD                      VARCHAR      COMMENT 'Verbatim fixture JSON from the API response, including its nested assets array.',
  LOADED_AT                         TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP() COMMENT 'When this row was landed.',
  INGESTION_RUN_ID                   NUMBER(38,0) COMMENT 'FK to CORE.INGESTION_RUNS for the extractor run that landed this row.'
)
COMMENT = 'Raw DVMS fixtures, one row per fixture per extractor run (append-only replay buffer — not deduped on FIXTURE_ID). RAW_PAYLOAD is the verbatim source JSON.';

-- ---------------------------------------------------------------------------
-- DVMS_RAW.ASSETS
--   One row per (fixture, asset) discovered inline in the fixtures response
--   -- normalizes FIXTURES.RAW_PAYLOAD's nested assets array. ASSET_TYPE /
--   ASSET_SUBTYPE are the DVMS API's own enum codes (see the DataType /
--   DataSubType schemas at dvms.premierleague.com/dvms/api-docs) -- kept as
--   raw integers here rather than decoded, since decoding is a staging
--   concern, not a landing concern.
--
--   RAW_PAYLOAD holds the downloaded file content as text regardless of its
--   real format (Opta files are XML; Second Spectrum spans JSON/JSONL/XML/
--   CSV depending on sub-feed) -- format-specific parsing happens in dbt
--   staging per FILE_TYPE, not here.
--
--   Known boundary: unqualified VARCHAR caps at 16MB/row. SIZE_BYTES is
--   populated from the API's own asset metadata *before* attempting a
--   download, specifically so an oversized tracking payload can be skipped
--   (RAW_PAYLOAD left NULL) rather than discovered by a failed insert. The
--   stage + COPY INTO route already earmarked for GPS/tracking (see GPS_RAW
--   in 20260525_create_schemas.sql) is the fallback for anything that blows
--   the ceiling.
--
--   Only assets with READY = TRUE are expected to have RAW_PAYLOAD populated
--   -- DVMS processes data asynchronously after a match, so not-yet-ready
--   assets land as metadata-only rows and get revisited on a later run.
--
--   Append-only, same rationale as FIXTURES.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.DVMS_RAW.ASSETS (
  FIXTURE_ID       VARCHAR(100)  NOT NULL  COMMENT 'FK to DVMS_RAW.FIXTURES.FIXTURE_ID.',
  ASSET_ID         VARCHAR(100)  NOT NULL  COMMENT 'Hudl asset id.',
  ASSET_TYPE       NUMBER(38,0)            COMMENT 'DVMS DataType enum code (0=Video, 2=Opta, 5=SecondSpectrum, etc. -- see API docs).',
  ASSET_SUBTYPE    NUMBER(38,0)            COMMENT 'DVMS DataSubType enum code (20=OptaF24, 21=OptaF7, 38=SecondSpectrumDataJSON(L), etc. -- see API docs).',
  ASSET_KEY        VARCHAR(2000)           COMMENT 'DVMS storage key/path, e.g. "2025/g2634377/Opta/OptaF24/g2634377_Opta_f24.xml" -- encodes season/opta match id/provider/filename.',
  READY            BOOLEAN                 COMMENT 'Whether DVMS reports this asset as downloadable yet.',
  SIZE_BYTES       NUMBER(38,0)            COMMENT 'File size per DVMS asset metadata, checked before download to guard the 16MB VARCHAR ceiling below.',
  FILENAME         VARCHAR(500)            COMMENT 'Original filename per DVMS asset metadata.',
  SOURCE_URL       VARCHAR(4000)           COMMENT 'Signed download URL used for this pull, if downloaded. Expires -- kept for audit, not for re-fetching later.',
  RAW_PAYLOAD      VARCHAR                 COMMENT 'Downloaded file content as text (XML/JSON/JSONL/CSV depending on feed). NULL if not READY or skipped for size.',
  LOADED_AT        TIMESTAMP_NTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP()  COMMENT 'When this row was landed.',
  INGESTION_RUN_ID NUMBER(38,0)            COMMENT 'FK to CORE.INGESTION_RUNS for the extractor run that landed this row.'
)
COMMENT = 'Raw DVMS assets, one row per (fixture, asset) per extractor run. Normalized breakout of FIXTURES.RAW_PAYLOAD''s nested assets array.';
