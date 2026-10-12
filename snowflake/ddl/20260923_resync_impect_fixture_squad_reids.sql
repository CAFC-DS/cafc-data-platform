-- ============================================================================
-- Point CORE.FIXTURES at IMPECT's current squad ids where IMPECT re-IDed them.
--
-- WHY: IMPECT sometimes merges duplicate club records (e.g. 16812 AEP Iraklis
-- -> 749 Iraklis Thessaloniki, 11598 -> 11607 Dibba SCC) or corrects the
-- opponent on a fixture after it was minted. python/identity/fixture_matcher.py
-- never revisited linked fixtures, so CORE.FIXTURES kept the old ids. (Pure
-- home/away flips were fixed separately by 20260922_resync_impect_fixture_
-- home_away.sql.)
--
-- GUARD: only when BOTH current IMPECT squad ids resolve in CORE_SQUADS as
-- MALE. As of 2026-10-12 that is 61 fixtures; 48 are intentionally left alone:
-- 47 women's fixtures (squads 4655->16674 RB Leipzig Frauen, 6080->6069 Irland
-- Frauen -- women's are excluded platform-wide and these should be reviewed
-- for removal, not re-pointed) and 1 whose new squad (9268) isn't in
-- CORE_SQUADS. Dates are never touched. MANUAL / merged fixtures are skipped.
--
-- Safe to re-run. Run as the CORE owner (DEV_ROLE). Review the DRY RUN output,
-- then run the UPDATEs inside the transaction and COMMIT.
-- ============================================================================

CREATE OR REPLACE TEMPORARY TABLE _impect_fixture_reid AS
SELECT
    f.CAFC_FIXTURE_ID,
    fi.FIXTURE_IDENTITY_ID,
    f.HOME_SQUAD_ID   AS old_home_squad_id,
    f.AWAY_SQUAD_ID   AS old_away_squad_id,
    m.HOMESQUADID     AS new_home_squad_id,
    m.AWAYSQUADID     AS new_away_squad_id
FROM CAFC_DB.CORE.FIXTURES f
JOIN CAFC_DB.CORE.FIXTURE_IDENTITIES fi
       ON fi.CAFC_FIXTURE_ID = f.CAFC_FIXTURE_ID
      AND fi.SOURCE_SYSTEM = 'IMPECT'
      AND fi.IS_PRIMARY
JOIN CAFC_DB.IMPECT_RAW.MATCHES m
       ON TO_VARCHAR(m.ID) = fi.SOURCE_FIXTURE_ID
JOIN CAFC_DB.CORE.CORE_SQUADS hs  ON hs.CAFC_SQUAD_ID  = m.HOMESQUADID AND hs.GENDER  = 'MALE'
JOIN CAFC_DB.CORE.CORE_SQUADS aws ON aws.CAFC_SQUAD_ID = m.AWAYSQUADID AND aws.GENDER = 'MALE'
WHERE f.CREATED_FROM_SOURCE = 'IMPECT'
  AND m.HOMESQUADID <> m.AWAYSQUADID
  AND (f.HOME_SQUAD_ID <> m.HOMESQUADID OR f.AWAY_SQUAD_ID <> m.AWAYSQUADID)
  AND NOT (f.HOME_SQUAD_ID = m.AWAYSQUADID AND f.AWAY_SQUAD_ID = m.HOMESQUADID);

-- DRY RUN: expect 61, with names so the re-IDs can be eyeballed.
SELECT COUNT(*) AS to_reid FROM _impect_fixture_reid;

SELECT r.CAFC_FIXTURE_ID, f.FIXTURE_DATE,
       oh.SQUAD_NAME AS old_home, nh.SQUAD_NAME AS new_home,
       oa.SQUAD_NAME AS old_away, na.SQUAD_NAME AS new_away
FROM _impect_fixture_reid r
JOIN CAFC_DB.CORE.FIXTURES f USING (CAFC_FIXTURE_ID)
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS oh ON oh.CAFC_SQUAD_ID = r.old_home_squad_id
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS nh ON nh.CAFC_SQUAD_ID = r.new_home_squad_id
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS oa ON oa.CAFC_SQUAD_ID = r.old_away_squad_id
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS na ON na.CAFC_SQUAD_ID = r.new_away_squad_id
ORDER BY f.FIXTURE_DATE;

-- APPLY
BEGIN;

UPDATE CAFC_DB.CORE.FIXTURES f
   SET HOME_SQUAD_ID = d.new_home_squad_id,
       AWAY_SQUAD_ID = d.new_away_squad_id,
       UPDATED_AT    = CURRENT_TIMESTAMP()
  FROM _impect_fixture_reid d
 WHERE f.CAFC_FIXTURE_ID = d.CAFC_FIXTURE_ID;

UPDATE CAFC_DB.CORE.FIXTURE_IDENTITIES fi
   SET SOURCE_HOME_SQUAD_ID = TO_VARCHAR(d.new_home_squad_id),
       SOURCE_AWAY_SQUAD_ID = TO_VARCHAR(d.new_away_squad_id)
  FROM _impect_fixture_reid d
 WHERE fi.FIXTURE_IDENTITY_ID = d.FIXTURE_IDENTITY_ID;

-- VERIFY: expect 0.
SELECT COUNT(*) AS still_to_reid
FROM CAFC_DB.CORE.FIXTURES f
JOIN CAFC_DB.CORE.FIXTURE_IDENTITIES fi
       ON fi.CAFC_FIXTURE_ID = f.CAFC_FIXTURE_ID AND fi.SOURCE_SYSTEM = 'IMPECT' AND fi.IS_PRIMARY
JOIN _impect_fixture_reid d ON d.CAFC_FIXTURE_ID = f.CAFC_FIXTURE_ID
JOIN CAFC_DB.IMPECT_RAW.MATCHES m ON TO_VARCHAR(m.ID) = fi.SOURCE_FIXTURE_ID
WHERE f.HOME_SQUAD_ID <> m.HOMESQUADID OR f.AWAY_SQUAD_ID <> m.AWAYSQUADID;

COMMIT;
