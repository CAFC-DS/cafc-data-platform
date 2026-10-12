-- ============================================================================
-- Re-sync CORE.FIXTURES home/away squads (and date) with IMPECT_RAW.MATCHES.
--
-- WHY: python/identity/fixture_matcher.py only mints fixtures for IMPECT
-- matches it hasn't linked yet and never revisits them. When IMPECT corrects a
-- match's HOMESQUADID/AWAYSQUADID after the mint, CORE.FIXTURES keeps the old
-- orientation. Found 2026-10: 32 fixtures reversed, e.g. Wigan Athletic v
-- MK Dons (IMPECT match 270221, CAFC_FIXTURE_ID 2864025, 2026-09-02) showing
-- MK Dons at home, and its return fixture (270516 / 2864320) likewise.
--
-- Scope: fixtures created from IMPECT whose primary IMPECT identity is linked
-- and whose squads or date differ from IMPECT_RAW.MATCHES today. MANUAL and
-- merged multi-source fixtures are left alone.
--
-- Safe to re-run (a second run finds nothing to change).
-- Run as the CORE owner (DEV_ROLE). Review the DRY RUN output, then run the
-- UPDATEs inside the transaction and COMMIT.
-- ============================================================================

CREATE OR REPLACE TEMPORARY TABLE _impect_fixture_drift AS
SELECT
    f.CAFC_FIXTURE_ID,
    fi.FIXTURE_IDENTITY_ID,
    f.HOME_SQUAD_ID                              AS old_home_squad_id,
    f.AWAY_SQUAD_ID                              AS old_away_squad_id,
    f.FIXTURE_DATE                               AS old_fixture_date,
    m.HOMESQUADID                                AS new_home_squad_id,
    m.AWAYSQUADID                                AS new_away_squad_id,
    TRY_TO_TIMESTAMP_NTZ(m.SCHEDULEDDATE)        AS new_fixture_date
FROM CAFC_DB.CORE.FIXTURES f
JOIN CAFC_DB.CORE.FIXTURE_IDENTITIES fi
       ON fi.CAFC_FIXTURE_ID = f.CAFC_FIXTURE_ID
      AND fi.SOURCE_SYSTEM = 'IMPECT'
      AND fi.IS_PRIMARY
JOIN CAFC_DB.IMPECT_RAW.MATCHES m
       ON TO_VARCHAR(m.ID) = fi.SOURCE_FIXTURE_ID
WHERE f.CREATED_FROM_SOURCE = 'IMPECT'
  AND m.HOMESQUADID IS NOT NULL
  AND m.AWAYSQUADID IS NOT NULL
  AND TRY_TO_TIMESTAMP_NTZ(m.SCHEDULEDDATE) IS NOT NULL
  AND (   f.HOME_SQUAD_ID <> m.HOMESQUADID
       OR f.AWAY_SQUAD_ID <> m.AWAYSQUADID
       OR f.FIXTURE_DATE  <> TRY_TO_TIMESTAMP_NTZ(m.SCHEDULEDDATE));

-- ---------------------------------------------------------------------------
-- DRY RUN: what would change. Expect the 32 reversed fixtures (incl. 2864025
-- and 2864320) plus any date-only drift.
-- ---------------------------------------------------------------------------
SELECT
    COUNT(*)                                                         AS drifted,
    COUNT_IF(old_home_squad_id = new_away_squad_id
         AND old_away_squad_id = new_home_squad_id)                  AS home_away_reversed,
    COUNT_IF(old_fixture_date <> new_fixture_date)                   AS date_changed
FROM _impect_fixture_drift;

SELECT * FROM _impect_fixture_drift ORDER BY old_fixture_date;

-- ---------------------------------------------------------------------------
-- APPLY
-- ---------------------------------------------------------------------------
BEGIN;

UPDATE CAFC_DB.CORE.FIXTURES f
   SET HOME_SQUAD_ID = d.new_home_squad_id,
       AWAY_SQUAD_ID = d.new_away_squad_id,
       FIXTURE_DATE  = d.new_fixture_date,
       UPDATED_AT    = CURRENT_TIMESTAMP()
  FROM _impect_fixture_drift d
 WHERE f.CAFC_FIXTURE_ID = d.CAFC_FIXTURE_ID;

-- Provenance columns on the identity row (not read by any view) follow suit.
UPDATE CAFC_DB.CORE.FIXTURE_IDENTITIES fi
   SET SOURCE_HOME_SQUAD_ID = TO_VARCHAR(d.new_home_squad_id),
       SOURCE_AWAY_SQUAD_ID = TO_VARCHAR(d.new_away_squad_id),
       SOURCE_FIXTURE_DATE  = d.new_fixture_date
  FROM _impect_fixture_drift d
 WHERE fi.FIXTURE_IDENTITY_ID = d.FIXTURE_IDENTITY_ID;

-- ---------------------------------------------------------------------------
-- VERIFY: expect 0 rows, and the two Wigan / MK Dons fixtures with Wigan
-- (877) home on 2864025 and MK Dons (956) home on 2864320.
-- ---------------------------------------------------------------------------
SELECT COUNT(*) AS still_drifted
FROM CAFC_DB.CORE.FIXTURES f
JOIN CAFC_DB.CORE.FIXTURE_IDENTITIES fi
       ON fi.CAFC_FIXTURE_ID = f.CAFC_FIXTURE_ID AND fi.SOURCE_SYSTEM = 'IMPECT' AND fi.IS_PRIMARY
JOIN CAFC_DB.IMPECT_RAW.MATCHES m ON TO_VARCHAR(m.ID) = fi.SOURCE_FIXTURE_ID
WHERE f.CREATED_FROM_SOURCE = 'IMPECT'
  AND (f.HOME_SQUAD_ID <> m.HOMESQUADID OR f.AWAY_SQUAD_ID <> m.AWAYSQUADID);

SELECT CAFC_FIXTURE_ID, HOME_SQUAD_ID, AWAY_SQUAD_ID, FIXTURE_DATE
FROM CAFC_DB.CORE.FIXTURES
WHERE CAFC_FIXTURE_ID IN (2864025, 2864320);

COMMIT;
