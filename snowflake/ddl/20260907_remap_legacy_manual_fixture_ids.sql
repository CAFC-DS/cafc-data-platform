-- ============================================================================
-- Remap stale legacy manual-fixture ids on SCOUT_REPORTS to canonical ids.
--
-- WHEN: cutover follow-up, AFTER
--   1. 20260907_fixture_identities_add_source_squad_names.sql
--   2. python -m python.identity.mint_legacy_manual_fixtures --apply
--   3. 20260907_backfill_manual_fixture_squads.sql
--
-- WHY: SCOUT_REPORTS.MATCH_ID references manually-added ("internal") matches
-- by their LEGACY RECRUITMENT_TEST.PUBLIC.MATCHES.CAFC_MATCH_ID (small ints,
-- 1901..19001). The canonical layer re-minted those fixtures with new
-- CAFC_FIXTURE_IDs (mapping in CORE.FIXTURE_IDENTITIES, SOURCE_SYSTEM='MANUAL',
-- SOURCE_FIXTURE_ID = the legacy id as text). Since the 2026-09-06 cutover the
-- app reads APP_COMPAT.MATCHES (built from CORE.FIXTURES), so these reports
-- join to nothing and show Fixture / Fixture Date = "N/A".
--
-- SCOUT_REPORTS.MATCH_ID is the ONLY app column that references matches
-- (verified against CAFC_DB.INFORMATION_SCHEMA.COLUMNS, 2026-09-07).
--
-- Dedupe note: the June 2026 build minted some legacy matches more than once
-- (e.g. legacy id 6712 -> 3 CAFC_FIXTURE_IDs). The remap map picks ONE
-- canonical id per legacy id, deterministically: prefer the fixture whose
-- squad ids are populated, tiebreak on the lowest CAFC_FIXTURE_ID. The
-- surplus identity rows are harmless (nothing points at them post-remap) and
-- are left for a later cleanup.
--
-- IDs don't collide: CAFC_FIXTURE_ID space is 2.5M+, legacy internal ids are
-- <= 19001. The NOT EXISTS guard below is belt-and-braces on top of that.
-- Run as the CORE owner (DEV_ROLE). Run the DRY RUN block first.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- One canonical fixture id per legacy manual-match id.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE _fixture_id_remap AS
WITH manual AS (
    SELECT
        fi.CAFC_FIXTURE_ID,
        TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID) AS legacy_id,
        ROW_NUMBER() OVER (
            PARTITION BY TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID)
            ORDER BY CASE WHEN f.HOME_SQUAD_ID IS NOT NULL
                           AND f.AWAY_SQUAD_ID IS NOT NULL THEN 0 ELSE 1 END,
                     fi.CAFC_FIXTURE_ID
        ) AS rn
    FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
    JOIN CAFC_DB.CORE.FIXTURES f ON f.CAFC_FIXTURE_ID = fi.CAFC_FIXTURE_ID
    WHERE fi.SOURCE_SYSTEM = 'MANUAL'
      AND TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID) IS NOT NULL
)
SELECT legacy_id, CAFC_FIXTURE_ID AS canonical_fixture_id
FROM manual
WHERE rn = 1;

-- ---------------------------------------------------------------------------
-- Backup the (id, old MATCH_ID) pairs we are about to touch. Reversible in
-- one statement:  UPDATE ... SET MATCH_ID = b.MATCH_ID FROM ...backup b ...
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAFC_DB.CORE._scout_reports_match_id_backup_20260907 AS
SELECT sr.ID, sr.MATCH_ID, CURRENT_TIMESTAMP() AS backed_up_at
FROM CAFC_DB.CORE.SCOUT_REPORTS sr
JOIN _fixture_id_remap r ON sr.MATCH_ID = r.legacy_id;

-- ---------------------------------------------------------------------------
-- DRY RUN
-- ---------------------------------------------------------------------------
SELECT 'distinct legacy ids in remap map'      AS metric, COUNT(*) AS n FROM _fixture_id_remap
UNION ALL
SELECT 'scout_reports rows that would remap',
       COUNT(*)
FROM CAFC_DB.CORE.SCOUT_REPORTS t
JOIN _fixture_id_remap r ON t.MATCH_ID = r.legacy_id
WHERE NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.FIXTURES f WHERE f.CAFC_FIXTURE_ID = t.MATCH_ID)
UNION ALL
SELECT 'scout_reports still orphaned after remap (broken on legacy too)',
       COUNT(*)
FROM CAFC_DB.CORE.SCOUT_REPORTS t
WHERE t.MATCH_ID IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.FIXTURES f WHERE f.CAFC_FIXTURE_ID = t.MATCH_ID)
  AND NOT EXISTS (SELECT 1 FROM _fixture_id_remap r WHERE r.legacy_id = t.MATCH_ID)
  AND NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
                  WHERE fi.SOURCE_SYSTEM = 'IMPECT'
                    AND TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID) = t.MATCH_ID);

-- ---------------------------------------------------------------------------
-- APPLY
-- ---------------------------------------------------------------------------
UPDATE CAFC_DB.CORE.SCOUT_REPORTS t
SET MATCH_ID = r.canonical_fixture_id
FROM _fixture_id_remap r
WHERE t.MATCH_ID = r.legacy_id
  AND NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.FIXTURES f WHERE f.CAFC_FIXTURE_ID = t.MATCH_ID);

-- ---------------------------------------------------------------------------
-- POST-CHECK  (resolves_core should jump by ~the "would remap" count)
-- ---------------------------------------------------------------------------
SELECT
    COUNT_IF(sr.MATCH_ID IS NOT NULL)              AS with_match_id,
    COUNT_IF(f.CAFC_FIXTURE_ID IS NOT NULL)        AS resolves_core_fixtures,
    COUNT_IF(sr.MATCH_ID IS NOT NULL AND f.CAFC_FIXTURE_ID IS NULL) AS still_unresolved
FROM CAFC_DB.CORE.SCOUT_REPORTS sr
LEFT JOIN CAFC_DB.CORE.FIXTURES f ON f.CAFC_FIXTURE_ID = sr.MATCH_ID;
