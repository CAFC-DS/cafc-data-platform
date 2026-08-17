-- Operational state for the scheduled IMPECT event sync.
--
-- One row per IMPECT match.  This is deliberately separate from EVENTS:
-- a match can be known but not data-ready, permanently lack event coverage,
-- or require a retry after a transient provider failure.

USE DATABASE CAFC_DB;

CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.IMPECT_EVENT_SYNC_STATE (
  MATCH_ID                    NUMBER(38,0) NOT NULL,
  ITERATION_ID                NUMBER(38,0) NOT NULL,
  COMPETITION_NAME            VARCHAR,
  SEASON                      VARCHAR,
  SCHEDULED_AT                TIMESTAMP_NTZ,
  MATCH_AVAILABLE             BOOLEAN,
  SOURCE_LAST_CALCULATION_AT  VARCHAR,
  SOURCE_UPDATED_AT           TIMESTAMP_NTZ,
  STATUS                      VARCHAR NOT NULL,
  EVENT_COUNT                 NUMBER(38,0),
  LAST_ATTEMPTED_AT           TIMESTAMP_NTZ,
  LAST_SUCCEEDED_AT           TIMESTAMP_NTZ,
  NEXT_RETRY_AT               TIMESTAMP_NTZ,
  LAST_ERROR                  VARCHAR,
  INGESTION_RUN_ID            NUMBER(38,0),
  CREATED_AT                  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
  UPDATED_AT                  TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT IMPECT_EVENT_SYNC_STATE_PK PRIMARY KEY (MATCH_ID)
)
COMMENT = 'Control ledger for scheduled IMPECT match-event ingestion. STATUS is SUCCESS, NO_EVENT_DATA, FAILED, or DELETED.';

ALTER TABLE CAFC_DB.CORE.IMPECT_EVENT_SYNC_STATE
  ADD COLUMN IF NOT EXISTS SOURCE_UPDATED_AT TIMESTAMP_NTZ;

CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.IMPECT_FEED_CURSORS (
  FEED_NAME             VARCHAR NOT NULL,
  LAST_SUCCESSFUL_SINCE TIMESTAMP_NTZ NOT NULL,
  UPDATED_AT            TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT IMPECT_FEED_CURSORS_PK PRIMARY KEY (FEED_NAME)
)
COMMENT = 'High-water timestamps for IMPECT timestamp-based update/delete feeds. Each poll overlaps the stored value to avoid boundary misses.';
