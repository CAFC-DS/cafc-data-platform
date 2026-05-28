-- ============================================================================
--  20260525_identity_overrides.sql
--  Operational tables that the identity matcher (plan §2.6) and orchestrator
--  (plan §2.7) depend on. Not dbt-managed — dbt produces transformations,
--  this is operational state with primary keys and explicit DML.
--
--    CORE.PLAYER_IDENTITY_OVERRIDES     highest-precedence manual mappings
--    CORE.PLAYER_IDENTITY_CANDIDATES    ambiguous-match queue for human review
--    CORE.INGESTION_RUNS                transaction envelope for each refresh
--    CORE.SQUAD_NAME_OVERRIDES          (player tables don't address squads;
--                                         legacy MIGRATION.SQUAD_NAME_CORRECTIONS
--                                         lands here)
--
--  Idempotent. Every CREATE is IF NOT EXISTS. Re-running has no effect on
--  existing objects or rows (the migration INSERTs use NOT EXISTS guards).
--
--  -------------------------------------------------------------------------
--  Resolved against live CAFC_DB on 2026-05-25:
--    - MAX(CAFC_PLAYER_ID) = 1,483,342 across CORE.PLAYERS and CORE.PLAYER_IDENTITIES.
--    - CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ already exists (it's the DEFAULT on
--      CORE.PLAYERS.CAFC_PLAYER_ID). The matcher in plan §2.6 should call
--      that existing sequence; no new sequence is created here.
--    - CORE.PLAYER_IDENTITIES uses column SOURCE_PLAYER_ID (not EXTERNAL_ID),
--      so PLAYER_IDENTITY_OVERRIDES does the same — keeps the matcher's
--      lookup logic uniform.
--    - MIGRATION.SCOUT_REPORT_PLAYER_ID_OVERRIDES is (WRONG_LEGACY_PLAYER_ID,
--      CORRECT_IMPECT_PLAYER_ID, NOTES). Despite the column name, the
--      WRONG_LEGACY_PLAYER_ID values are actually IMPECT player IDs that
--      were incorrectly attached to a player; the correction redirects them
--      to the CAFC_PLAYER_ID held by IMPECT player CORRECT_IMPECT_PLAYER_ID.
--      So the override rows here land with SOURCE_SYSTEM='IMPECT', not 'LEGACY'.
--    - MIGRATION.SQUAD_NAME_CORRECTIONS is (CURRENT_MANUAL_SQUAD_ID,
--      WRONG_SQUAD_NAME, CORRECT_IMPECT_SQUAD_ID, CORRECT_MANUAL_SQUAD_ID,
--      NOTES). Squad model isn't designed yet — we land the rows verbatim
--      in SQUAD_NAME_OVERRIDES preserving the source shape; canonicalisation
--      happens once squad dimensions are modeled (plan §2.5+).
--
--  -------------------------------------------------------------------------
--  STILL TO REVIEW BEFORE APPLYING:
--    The migration INSERTs at the bottom of this file move 6 + 28 rows out
--    of MIGRATION.* into the new tables. They are idempotent (NOT EXISTS
--    guards) but they DO mutate CORE. Read them, run them once in a
--    worksheet, then verify row counts match (6, 28).
-- ============================================================================

USE DATABASE CAFC_DB;
USE SCHEMA   CORE;

-- ---------------------------------------------------------------------------
-- NOTE on the CAFC_PLAYER_ID sequence:
--
--   CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ already exists. It's wired in as the
--   DEFAULT expression on CORE.PLAYERS.CAFC_PLAYER_ID, so the live sequence
--   is by definition >= MAX(CAFC_PLAYER_ID) at all times. The matcher
--   (python/identity/mint.py) should mint new IDs by either:
--     a) SELECT CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ.NEXTVAL — explicit call
--     b) INSERT INTO CORE.PLAYERS (...) without CAFC_PLAYER_ID — let DEFAULT fire
--   Both are safe; option (a) is what plan §2.6 describes.
--
--   No new sequence is created in this file. If you ever need to recreate
--   the sequence (e.g. after a restore that broke the wiring), the safest
--   pattern is:
--     CREATE SEQUENCE CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ
--       START WITH (SELECT MAX(CAFC_PLAYER_ID) + 1 FROM CAFC_DB.CORE.PLAYERS)
--       INCREMENT BY 1 ORDER;
--   (Snowflake doesn't actually allow a subquery in START WITH — compute the
--   number first, paste it in.)
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- CORE.PLAYER_IDENTITY_OVERRIDES
--   Highest-precedence manual mapping of (SOURCE_SYSTEM, SOURCE_PLAYER_ID) →
--   CAFC_PLAYER_ID. If a row exists here, the matcher uses it unconditionally
--   (plan §2.6 invariant #2: "the override table always wins"). Column names
--   match CORE.PLAYER_IDENTITIES so the matcher can reuse identifiers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES (
  SOURCE_SYSTEM     VARCHAR(50)    NOT NULL  COMMENT 'Origin of SOURCE_PLAYER_ID. e.g. "IMPECT", "MANUAL", "LEGACY".',
  SOURCE_PLAYER_ID  VARCHAR(255)   NOT NULL  COMMENT 'The provider-side identifier being mapped. VARCHAR to match CORE.PLAYER_IDENTITIES.SOURCE_PLAYER_ID exactly.',
  CAFC_PLAYER_ID    NUMBER(38, 0)  NOT NULL  COMMENT 'The canonical CAFC_PLAYER_ID this external identity must resolve to.',
  REASON            VARCHAR                  COMMENT 'Free-text justification (e.g. "two players share name+DOB; this one is the U21 striker").',
  CREATED_BY        VARCHAR        NOT NULL  COMMENT 'Snowflake user or service account that inserted the row.',
  CREATED_AT        TIMESTAMP_NTZ  NOT NULL  DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT PK_PLAYER_IDENTITY_OVERRIDES PRIMARY KEY (SOURCE_SYSTEM, SOURCE_PLAYER_ID)
)
COMMENT = 'Manual player-identity overrides. Matcher consults this first; if a (SOURCE_SYSTEM, SOURCE_PLAYER_ID) pair has a row, that CAFC_PLAYER_ID is used unconditionally.';

-- ---------------------------------------------------------------------------
-- CORE.PLAYER_IDENTITY_CANDIDATES
--   Ambiguous-match queue. When the matcher sees multiple plausible
--   CORE.PLAYERS rows for a new external identity it writes one row per
--   candidate here and does NOT link. The dbt test no_orphan_kpis fails the
--   refresh if any fact row references an external identity that's only
--   here, surfacing the queue so a human can resolve it (typically by
--   inserting into PLAYER_IDENTITY_OVERRIDES, then re-running).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.PLAYER_IDENTITY_CANDIDATES (
  SOURCE_SYSTEM            VARCHAR(50)    NOT NULL,
  SOURCE_PLAYER_ID         VARCHAR(255)   NOT NULL,
  CANDIDATE_CAFC_PLAYER_ID NUMBER(38, 0)  NOT NULL  COMMENT 'One existing CORE.PLAYERS.CAFC_PLAYER_ID the matcher could not rule out.',
  SOURCE_NAME              VARCHAR(255)             COMMENT 'Provider-side name at match-attempt time; preserved so reviewers see what the provider was claiming.',
  SOURCE_BIRTH_DATE        DATE                     COMMENT 'Provider-side DOB at match-attempt time.',
  MATCH_REASON             VARCHAR                  COMMENT 'Short tag for why this candidate matched, e.g. "commonname+dob exact" or "commonname exact, dob missing".',
  RUN_ID                   NUMBER(38, 0)  NOT NULL  COMMENT 'INGESTION_RUNS.RUN_ID that first produced this candidate row.',
  STATUS                   VARCHAR(20)    NOT NULL  DEFAULT 'PENDING' COMMENT 'PENDING / RESOLVED / REJECTED. RESOLVED = a human picked this candidate; REJECTED = a human picked a different one.',
  RESOLVED_BY              VARCHAR,
  RESOLVED_AT              TIMESTAMP_NTZ,
  CREATED_AT               TIMESTAMP_NTZ  NOT NULL  DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Ambiguous-match queue. One row per (external_id, candidate) pair; humans resolve by inserting into PLAYER_IDENTITY_OVERRIDES and updating STATUS here.';

-- ---------------------------------------------------------------------------
-- CORE.INGESTION_RUNS
--   Transaction envelope. The orchestrator opens one row at the start of a
--   refresh, captures RUN_ID, stamps it on every downstream insert, and
--   marks SUCCESS / FAILED at the end. Lets you delete partial-failure rows
--   by RUN_ID if a refresh blows up mid-flight.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.INGESTION_RUNS (
  RUN_ID        NUMBER(38, 0)  NOT NULL  AUTOINCREMENT  COMMENT 'Surrogate key; stamped on every row written during this refresh.',
  SOURCE_SYSTEM VARCHAR(50)    NOT NULL  COMMENT 'Which extractor opened the run, e.g. "IMPECT", "MANUAL", "GPS".',
  STARTED_AT    TIMESTAMP_NTZ  NOT NULL  DEFAULT CURRENT_TIMESTAMP(),
  FINISHED_AT   TIMESTAMP_NTZ            COMMENT 'NULL while the run is in flight.',
  STATUS        VARCHAR(20)    NOT NULL  DEFAULT 'RUNNING' COMMENT 'RUNNING / SUCCESS / FAILED.',
  DRY_RUN       BOOLEAN        NOT NULL  DEFAULT FALSE     COMMENT 'TRUE for orchestrator --dry-run invocations against dev schemas.',
  TRIGGERED_BY  VARCHAR                  COMMENT 'GitHub Actions run URL, Snowflake Task name, or human username.',
  NOTES         VARCHAR                  COMMENT 'Free-text notes (failure reasons, manual intervention details).',
  CONSTRAINT PK_INGESTION_RUNS PRIMARY KEY (RUN_ID)
)
COMMENT = 'One row per orchestrator refresh. RUN_ID is the envelope stamped on every downstream insert so partial failures are recoverable.';

-- ---------------------------------------------------------------------------
-- CORE.SQUAD_NAME_OVERRIDES
--   Lands MIGRATION.SQUAD_NAME_CORRECTIONS verbatim, preserving the source
--   shape because the canonical squad model isn't designed yet. Once squad
--   dimensions exist (plan §2.5+), this table becomes the inputs to a dbt
--   model that emits canonical squad identities — at which point the
--   columns might be tightened (CORRECT_MANUAL_SQUAD_ID being VARCHAR in
--   source is a typing oddity worth fixing then).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.SQUAD_NAME_OVERRIDES (
  WRONG_MANUAL_SQUAD_ID    NUMBER(38, 0)            COMMENT 'The manual squad ID that was being incorrectly used (source column: CURRENT_MANUAL_SQUAD_ID).',
  WRONG_SQUAD_NAME         VARCHAR                  COMMENT 'The manually-typed squad name that was wrong.',
  CORRECT_IMPECT_SQUAD_ID  NUMBER(38, 0)            COMMENT 'The IMPECT squad ID that the row actually refers to.',
  CORRECT_MANUAL_SQUAD_ID  VARCHAR                  COMMENT 'Sometimes the correction is to a different manual squad ID rather than to IMPECT. VARCHAR to match the source typing oddity.',
  REASON                   VARCHAR                  COMMENT 'Was NOTES in source.',
  CREATED_BY               VARCHAR        NOT NULL,
  CREATED_AT               TIMESTAMP_NTZ  NOT NULL  DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Squad-name overrides migrated from MIGRATION.SQUAD_NAME_CORRECTIONS. Source-shape preserved; canonicalisation happens once squad dimensions are modeled.';

-- ============================================================================
--  ONE-SHOT MIGRATION INSERTS (idempotent via NOT EXISTS guards).
--  Source row counts at inspection time: 6 player overrides, 28 squad
--  corrections. After running, verify those numbers below.
-- ============================================================================

-- Player overrides: the source column WRONG_LEGACY_PLAYER_ID is actually a
-- *wrong IMPECT player ID* that historically got attached to a player who
-- is really IMPECT player CORRECT_IMPECT_PLAYER_ID. So the override fires
-- on (SOURCE_SYSTEM='IMPECT', SOURCE_PLAYER_ID=<the wrong IMPECT id>) and
-- redirects it to the CAFC_PLAYER_ID currently held by the correct IMPECT id.
--
-- NOTE: this masks the wrong row in CORE.PLAYER_IDENTITIES (which still
-- points the wrong IMPECT id at its own original CAFC_PLAYER_ID). That row
-- should arguably be cleaned up too, but the matcher precedence (override
-- always wins) means runtime behaviour is already correct after this insert.
-- Cleanup of the stale PLAYER_IDENTITIES row is a separate decision.
--
-- The PLAYER_IDENTITIES join goes through a deduped sub-select. PLAYER_IDENTITIES
-- can hold multiple rows per (SOURCE_SYSTEM, SOURCE_PLAYER_ID) — one per
-- squad context — but they all share the same CAFC_PLAYER_ID, so ANY_VALUE
-- collapses the fan-out safely.
INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES
  (SOURCE_SYSTEM, SOURCE_PLAYER_ID, CAFC_PLAYER_ID, REASON, CREATED_BY, CREATED_AT)
SELECT
  'IMPECT'                                                                AS SOURCE_SYSTEM,
  src.WRONG_LEGACY_PLAYER_ID                                              AS SOURCE_PLAYER_ID,
  pi.CAFC_PLAYER_ID                                                       AS CAFC_PLAYER_ID,
  'Migrated from MIGRATION.SCOUT_REPORT_PLAYER_ID_OVERRIDES'
    || ' (wrong_impect_id=' || src.WRONG_LEGACY_PLAYER_ID
    || ' → correct_impect_id=' || src.CORRECT_IMPECT_PLAYER_ID
    || COALESCE('; ' || src.NOTES, '') || ')'                             AS REASON,
  CURRENT_USER()                                                          AS CREATED_BY,
  CURRENT_TIMESTAMP()                                                     AS CREATED_AT
FROM CAFC_DB.MIGRATION.SCOUT_REPORT_PLAYER_ID_OVERRIDES src
JOIN (
    SELECT
        SOURCE_PLAYER_ID,
        ANY_VALUE(CAFC_PLAYER_ID)  AS CAFC_PLAYER_ID
    FROM CAFC_DB.CORE.PLAYER_IDENTITIES
    WHERE SOURCE_SYSTEM = 'IMPECT'
    GROUP BY SOURCE_PLAYER_ID
) pi
  ON pi.SOURCE_PLAYER_ID = src.CORRECT_IMPECT_PLAYER_ID
WHERE NOT EXISTS (
  SELECT 1 FROM CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES o
   WHERE o.SOURCE_SYSTEM    = 'IMPECT'
     AND o.SOURCE_PLAYER_ID = src.WRONG_LEGACY_PLAYER_ID
);

-- Squad overrides: copy verbatim, preserving the source shape.
INSERT INTO CAFC_DB.CORE.SQUAD_NAME_OVERRIDES
  (WRONG_MANUAL_SQUAD_ID, WRONG_SQUAD_NAME, CORRECT_IMPECT_SQUAD_ID, CORRECT_MANUAL_SQUAD_ID, REASON, CREATED_BY, CREATED_AT)
SELECT
  src.CURRENT_MANUAL_SQUAD_ID  AS WRONG_MANUAL_SQUAD_ID,
  src.WRONG_SQUAD_NAME         AS WRONG_SQUAD_NAME,
  src.CORRECT_IMPECT_SQUAD_ID  AS CORRECT_IMPECT_SQUAD_ID,
  src.CORRECT_MANUAL_SQUAD_ID  AS CORRECT_MANUAL_SQUAD_ID,
  src.NOTES                    AS REASON,
  CURRENT_USER()               AS CREATED_BY,
  CURRENT_TIMESTAMP()           AS CREATED_AT
FROM CAFC_DB.MIGRATION.SQUAD_NAME_CORRECTIONS src
WHERE NOT EXISTS (
  -- Match against the natural key. CURRENT_MANUAL_SQUAD_ID can be NULL in source,
  -- so guard for that to avoid double-inserts.
  SELECT 1 FROM CAFC_DB.CORE.SQUAD_NAME_OVERRIDES o
   WHERE o.WRONG_MANUAL_SQUAD_ID IS NOT DISTINCT FROM src.CURRENT_MANUAL_SQUAD_ID
     AND o.WRONG_SQUAD_NAME      IS NOT DISTINCT FROM src.WRONG_SQUAD_NAME
);

-- Verify after running:
--   SELECT COUNT(*) FROM CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES;  -- expect 6
--   SELECT COUNT(*) FROM CAFC_DB.CORE.SQUAD_NAME_OVERRIDES;       -- expect 28
