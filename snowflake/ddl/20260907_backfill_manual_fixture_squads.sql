-- ============================================================================
-- Backfill squad names + repair squad ids for MANUAL fixtures in the canonical
-- layer, from legacy RECRUITMENT_TEST.PUBLIC.MATCHES.
--
-- WHEN: after 20260907_fixture_identities_add_source_squad_names.sql and after
-- `python -m python.identity.mint_legacy_manual_fixtures --apply`.
--
-- WHY: the June 2026 build minted CORE.FIXTURES rows for ~203 manual fixtures
-- but (a) dropped the team names entirely and (b) stored fabricated
-- 9000000-range HOME_SQUAD_ID / AWAY_SQUAD_ID that don't resolve in
-- CORE_SQUADS -- so APP_COMPAT.MATCHES renders them nameless. Legacy MATCHES
-- actually carries real IMPECT squad ids for most of them (132/225 resolve on
-- both sides) plus denormalised names for all.
--
-- Strategy, MANUAL identities only, keyed by
-- TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID) = lm.CAFC_MATCH_ID:
--   1. Repair CORE.FIXTURES.HOME_SQUAD_ID / AWAY_SQUAD_ID from legacy where
--      the current id does NOT resolve in CORE_SQUADS and the legacy id DOES.
--   2. Copy legacy HOMESQUADNAME / AWAYSQUADNAME into
--      FIXTURE_IDENTITIES.SOURCE_HOME_SQUAD_NAME / SOURCE_AWAY_SQUAD_NAME
--      (the APP_COMPAT.MATCHES name fallback for the ~34 free-text-only ones).
--   3. Refresh FIXTURE_IDENTITIES.SOURCE_*_SQUAD_ID to the real legacy ids
--      (provenance; not read by any view).
--
-- Safe to re-run. The mint script already sets names/ids for the 62 it
-- creates; the guards below just no-op on those.
-- Run as the CORE owner (DEV_ROLE).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- One row per MANUAL fixture: its legacy match, and whether the current vs
-- legacy squad ids resolve in CORE_SQUADS.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE _manual_fixture_legacy AS
SELECT
    fi.CAFC_FIXTURE_ID,
    lm.CAFC_MATCH_ID                        AS legacy_match_id,
    lm.HOMESQUADID                          AS legacy_home_squad_id,
    lm.AWAYSQUADID                          AS legacy_away_squad_id,
    lm.HOMESQUADNAME                        AS legacy_home_name,
    lm.AWAYSQUADNAME                        AS legacy_away_name,
    (lh.CAFC_SQUAD_ID  IS NOT NULL)         AS legacy_home_resolves,
    (la.CAFC_SQUAD_ID  IS NOT NULL)         AS legacy_away_resolves,
    (ch.CAFC_SQUAD_ID  IS NOT NULL)         AS current_home_resolves,
    (ca.CAFC_SQUAD_ID  IS NOT NULL)         AS current_away_resolves
FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
JOIN CAFC_DB.CORE.FIXTURES f
       ON f.CAFC_FIXTURE_ID = fi.CAFC_FIXTURE_ID
JOIN RECRUITMENT_TEST.PUBLIC.MATCHES lm
       ON TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID) = lm.CAFC_MATCH_ID
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS lh ON lh.CAFC_SQUAD_ID = lm.HOMESQUADID
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS la ON la.CAFC_SQUAD_ID = lm.AWAYSQUADID
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS ch ON ch.CAFC_SQUAD_ID = f.HOME_SQUAD_ID
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS ca ON ca.CAFC_SQUAD_ID = f.AWAY_SQUAD_ID
WHERE fi.SOURCE_SYSTEM = 'MANUAL';

-- ---------------------------------------------------------------------------
-- DRY RUN
-- ---------------------------------------------------------------------------
SELECT 'manual fixtures joined to legacy'   AS metric, COUNT(*) AS n FROM _manual_fixture_legacy
UNION ALL
SELECT 'HOME_SQUAD_ID would be repaired',
       COUNT_IF(legacy_home_resolves AND NOT current_home_resolves) FROM _manual_fixture_legacy
UNION ALL
SELECT 'AWAY_SQUAD_ID would be repaired',
       COUNT_IF(legacy_away_resolves AND NOT current_away_resolves) FROM _manual_fixture_legacy
UNION ALL
SELECT 'both sides resolve after repair',
       COUNT_IF( (current_home_resolves OR legacy_home_resolves)
             AND (current_away_resolves OR legacy_away_resolves) ) FROM _manual_fixture_legacy
UNION ALL
SELECT 'free-text-only residual (name fallback needed)',
       COUNT_IF( NOT (current_home_resolves OR legacy_home_resolves)
              OR NOT (current_away_resolves OR legacy_away_resolves) ) FROM _manual_fixture_legacy;

-- ---------------------------------------------------------------------------
-- APPLY
-- ---------------------------------------------------------------------------
UPDATE CAFC_DB.CORE.FIXTURES f
SET HOME_SQUAD_ID = r.legacy_home_squad_id,
    UPDATED_AT    = CURRENT_TIMESTAMP()
FROM _manual_fixture_legacy r
WHERE f.CAFC_FIXTURE_ID = r.CAFC_FIXTURE_ID
  AND r.legacy_home_resolves
  AND NOT r.current_home_resolves;

UPDATE CAFC_DB.CORE.FIXTURES f
SET AWAY_SQUAD_ID = r.legacy_away_squad_id,
    UPDATED_AT    = CURRENT_TIMESTAMP()
FROM _manual_fixture_legacy r
WHERE f.CAFC_FIXTURE_ID = r.CAFC_FIXTURE_ID
  AND r.legacy_away_resolves
  AND NOT r.current_away_resolves;

UPDATE CAFC_DB.CORE.FIXTURE_IDENTITIES fi
SET SOURCE_HOME_SQUAD_NAME = COALESCE(fi.SOURCE_HOME_SQUAD_NAME, r.legacy_home_name),
    SOURCE_AWAY_SQUAD_NAME = COALESCE(fi.SOURCE_AWAY_SQUAD_NAME, r.legacy_away_name),
    SOURCE_HOME_SQUAD_ID   = COALESCE(TO_VARCHAR(r.legacy_home_squad_id), fi.SOURCE_HOME_SQUAD_ID),
    SOURCE_AWAY_SQUAD_ID   = COALESCE(TO_VARCHAR(r.legacy_away_squad_id), fi.SOURCE_AWAY_SQUAD_ID),
    UPDATED_AT             = CURRENT_TIMESTAMP()
FROM _manual_fixture_legacy r
WHERE fi.CAFC_FIXTURE_ID = r.CAFC_FIXTURE_ID
  AND fi.SOURCE_SYSTEM = 'MANUAL';

-- ---------------------------------------------------------------------------
-- POST-CHECK
-- ---------------------------------------------------------------------------
SELECT
    COUNT(*)                                                          AS manual_fixtures,
    COUNT_IF(hs.CAFC_SQUAD_ID IS NOT NULL AND aws.CAFC_SQUAD_ID IS NOT NULL) AS both_ids_resolve,
    COUNT_IF(fi.SOURCE_HOME_SQUAD_NAME IS NOT NULL)                   AS have_home_name
FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
JOIN CAFC_DB.CORE.FIXTURES f       ON f.CAFC_FIXTURE_ID = fi.CAFC_FIXTURE_ID
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS hs  ON hs.CAFC_SQUAD_ID  = f.HOME_SQUAD_ID
LEFT JOIN CAFC_DB.CORE.CORE_SQUADS aws ON aws.CAFC_SQUAD_ID = f.AWAY_SQUAD_ID
WHERE fi.SOURCE_SYSTEM = 'MANUAL';
