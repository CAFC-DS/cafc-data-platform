-- ============================================================================
-- Swap CORE.FIXTURES home/away back to IMPECT_RAW.MATCHES where they are reversed.
--
-- WHY: python/identity/fixture_matcher.py only mints fixtures for IMPECT
-- matches it hasn't linked yet and never revisits them. When IMPECT corrects a
-- match's HOMESQUADID/AWAYSQUADID after the mint, CORE.FIXTURES keeps the old
-- orientation. Found 2026-10: 32 fixtures reversed, e.g. Wigan Athletic v
-- MK Dons (IMPECT match 270221, CAFC_FIXTURE_ID 2864025, 2026-09-02) showing
-- MK Dons at home, and its return fixture (270516 / 2864320) likewise.
--
-- Scope: fixtures created from IMPECT whose primary IMPECT identity is linked
-- and whose two squads are the exact reverse of IMPECT_RAW.MATCHES today.
-- Deliberately NOT synced: squad re-IDs (IMPECT swapped in a different squad
-- id, often not in CORE_SQUADS) and rescheduled dates. MANUAL and merged
-- multi-source fixtures are left alone.
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
    m.HOMESQUADID                                AS new_home_squad_id,
    m.AWAYSQUADID                                AS new_away_squad_id
FROM CAFC_DB.CORE.FIXTURES f
JOIN CAFC_DB.CORE.FIXTURE_IDENTITIES fi
       ON fi.CAFC_FIXTURE_ID = f.CAFC_FIXTURE_ID
      AND fi.SOURCE_SYSTEM = 'IMPECT'
      AND fi.IS_PRIMARY
JOIN CAFC_DB.IMPECT_RAW.MATCHES m
       ON TO_VARCHAR(m.ID) = fi.SOURCE_FIXTURE_ID
WHERE f.CREATED_FROM_SOURCE = 'IMPECT'
  AND f.HOME_SQUAD_ID = m.AWAYSQUADID
  AND f.AWAY_SQUAD_ID = m.HOMESQUADID
  AND f.HOME_SQUAD_ID <> f.AWAY_SQUAD_ID;

-- ---------------------------------------------------------------------------
-- DRY RUN: what would change. Expect the 32 reversed fixtures (incl. 2864025
-- and 2864320) plus any date-only drift.
-- ---------------------------------------------------------------------------
SELECT COUNT(*) AS reversed FROM _impect_fixture_drift;

SELECT d.*, f.FIXTURE_DATE
FROM _impect_fixture_drift d
JOIN CAFC_DB.CORE.FIXTURES f USING (CAFC_FIXTURE_ID)
ORDER BY f.FIXTURE_DATE;

-- ---------------------------------------------------------------------------
-- APPLY
-- ---------------------------------------------------------------------------
BEGIN;

UPDATE CAFC_DB.CORE.FIXTURES f
   SET HOME_SQUAD_ID = d.new_home_squad_id,
       AWAY_SQUAD_ID = d.new_away_squad_id,
       UPDATED_AT    = CURRENT_TIMESTAMP()
  FROM _impect_fixture_drift d
 WHERE f.CAFC_FIXTURE_ID = d.CAFC_FIXTURE_ID;

-- Provenance columns on the identity row (not read by any view) follow suit.
UPDATE CAFC_DB.CORE.FIXTURE_IDENTITIES fi
   SET SOURCE_HOME_SQUAD_ID = TO_VARCHAR(d.new_home_squad_id),
       SOURCE_AWAY_SQUAD_ID = TO_VARCHAR(d.new_away_squad_id)
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
  AND f.HOME_SQUAD_ID = m.AWAYSQUADID AND f.AWAY_SQUAD_ID = m.HOMESQUADID
  AND f.HOME_SQUAD_ID <> f.AWAY_SQUAD_ID;

SELECT CAFC_FIXTURE_ID, HOME_SQUAD_ID, AWAY_SQUAD_ID, FIXTURE_DATE
FROM CAFC_DB.CORE.FIXTURES
WHERE CAFC_FIXTURE_ID IN (2864025, 2864320);

COMMIT;
