-- ============================================================================
-- Phase 5: remap stale legacy player ids on app tables to canonical ids.
--
-- WHEN: cutover day, AFTER the Phase 3 clone script (it operates on the
-- CORE clones, never on RECRUITMENT_TEST — legacy keeps its own id space so
-- legacy mode still works if we roll back).
--
-- WHY: app rows (list items, scout reports, ...) reference players by the
-- LEGACY dual-id scheme: PLAYER_ID = IMPECT id, CAFC_PLAYER_ID = legacy
-- manual_player_seq id. The canonical layer re-minted manual players with
-- new CAFC_PLAYER_IDs (mapping kept in CORE.PLAYER_IDENTITIES,
-- SOURCE_SYSTEM='MANUAL', SOURCE_PLAYER_ID = legacy id), and some vintage
-- IMPECT ids were superseded (non-primary identities). Verified 2026-06-11:
-- 13 list items and 69 scout reports resolve in legacy but not canonical;
-- 8/13 list items map cleanly via MANUAL identities (the other 5 are broken
-- in legacy too — they reference deleted players).
--
-- Strategy per table with (PLAYER_ID, CAFC_PLAYER_ID):
--   1. Manual remap: row's CAFC_PLAYER_ID matches a MANUAL identity's
--      SOURCE_PLAYER_ID and not an existing canonical CAFC_PLAYER_ID →
--      set CAFC_PLAYER_ID to the canonical id (and PLAYER_ID to the
--      canonical player's primary IMPECT id when it has one).
--   2. Stale-external remap: row's PLAYER_ID matches a non-primary IMPECT
--      identity → repoint to that player's primary IMPECT id + canonical
--      CAFC id.
--
-- Run the DRY RUN blocks first; counts should match the verified numbers
-- above (plus drift). Each UPDATE logs rowcount; spot-check in the app.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Canonical resolution helper: one row per legacy-referenceable id.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE _player_id_remap AS
WITH primary_impect AS (
    SELECT cafc_player_id, source_player_id,
           ROW_NUMBER() OVER (
               PARTITION BY cafc_player_id
               ORDER BY CASE WHEN is_primary THEN 0 ELSE 1 END,
                        match_confidence DESC NULLS LAST,
                        player_identity_id
           ) AS rn
    FROM CAFC_DB.CORE.PLAYER_IDENTITIES
    WHERE source_system = 'IMPECT'
),
manual_ids AS (
    SELECT TRY_TO_NUMBER(source_player_id) AS legacy_cafc_id, cafc_player_id
    FROM CAFC_DB.CORE.PLAYER_IDENTITIES
    WHERE source_system = 'MANUAL'
)
SELECT
    m.legacy_cafc_id,
    m.cafc_player_id              AS canonical_cafc_id,
    TRY_TO_NUMBER(pi.source_player_id) AS canonical_impect_id
FROM manual_ids m
LEFT JOIN primary_impect pi
       ON pi.cafc_player_id = m.cafc_player_id AND pi.rn = 1
WHERE m.legacy_cafc_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- DRY RUN — rows the manual remap would touch (run per table, eyeball them).
-- A row only remaps when its current CAFC id does NOT resolve canonically
-- (protects new app-minted CORE ids, which live in the same column).
-- ---------------------------------------------------------------------------
SELECT 'PLAYER_LIST_ITEMS' AS tbl, COUNT(*) AS would_remap
FROM CAFC_DB.CORE.PLAYER_LIST_ITEMS t
JOIN _player_id_remap r ON t.CAFC_PLAYER_ID = r.legacy_cafc_id
WHERE NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.PLAYERS p
                  WHERE p.CAFC_PLAYER_ID = t.CAFC_PLAYER_ID)
UNION ALL
SELECT 'SCOUT_REPORTS', COUNT(*)
FROM CAFC_DB.CORE.SCOUT_REPORTS t
JOIN _player_id_remap r ON t.CAFC_PLAYER_ID = r.legacy_cafc_id
WHERE NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.PLAYERS p
                  WHERE p.CAFC_PLAYER_ID = t.CAFC_PLAYER_ID);

-- ---------------------------------------------------------------------------
-- APPLY — manual-id remap. Repeat the UPDATE per app table that carries
-- (PLAYER_ID, CAFC_PLAYER_ID): PLAYER_LIST_ITEMS, SCOUT_REPORTS, and any
-- other cloned table found to reference players (check PLAYER_NOTES /
-- PLAYER_INFORMATION column shapes before adding them here).
-- ---------------------------------------------------------------------------
UPDATE CAFC_DB.CORE.PLAYER_LIST_ITEMS t
SET CAFC_PLAYER_ID = r.canonical_cafc_id,
    PLAYER_ID      = COALESCE(r.canonical_impect_id, t.PLAYER_ID)
FROM _player_id_remap r
WHERE t.CAFC_PLAYER_ID = r.legacy_cafc_id
  AND NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.PLAYERS p
                  WHERE p.CAFC_PLAYER_ID = t.CAFC_PLAYER_ID);

UPDATE CAFC_DB.CORE.SCOUT_REPORTS t
SET CAFC_PLAYER_ID = r.canonical_cafc_id,
    PLAYER_ID      = COALESCE(r.canonical_impect_id, t.PLAYER_ID)
FROM _player_id_remap r
WHERE t.CAFC_PLAYER_ID = r.legacy_cafc_id
  AND NOT EXISTS (SELECT 1 FROM CAFC_DB.CORE.PLAYERS p
                  WHERE p.CAFC_PLAYER_ID = t.CAFC_PLAYER_ID);

-- ---------------------------------------------------------------------------
-- Stale-external remap: PLAYER_ID is a superseded IMPECT id (exists in
-- identities but isn't any player's primary id in app_compat).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE _impect_id_remap AS
WITH ranked AS (
    SELECT cafc_player_id,
           TRY_TO_NUMBER(source_player_id) AS impect_id,
           ROW_NUMBER() OVER (
               PARTITION BY cafc_player_id
               ORDER BY CASE WHEN is_primary THEN 0 ELSE 1 END,
                        match_confidence DESC NULLS LAST,
                        player_identity_id
           ) AS rn
    FROM CAFC_DB.CORE.PLAYER_IDENTITIES
    WHERE source_system = 'IMPECT'
)
SELECT old.impect_id  AS stale_impect_id,
       prim.impect_id AS primary_impect_id,
       prim.cafc_player_id AS canonical_cafc_id
FROM ranked old
JOIN ranked prim ON prim.cafc_player_id = old.cafc_player_id AND prim.rn = 1
WHERE old.rn > 1 AND old.impect_id IS NOT NULL
  AND old.impect_id <> prim.impect_id;

UPDATE CAFC_DB.CORE.PLAYER_LIST_ITEMS t
SET PLAYER_ID = r.primary_impect_id,
    CAFC_PLAYER_ID = COALESCE(t.CAFC_PLAYER_ID, r.canonical_cafc_id)
FROM _impect_id_remap r
WHERE t.PLAYER_ID = r.stale_impect_id;

UPDATE CAFC_DB.CORE.SCOUT_REPORTS t
SET PLAYER_ID = r.primary_impect_id,
    CAFC_PLAYER_ID = COALESCE(t.CAFC_PLAYER_ID, r.canonical_cafc_id)
FROM _impect_id_remap r
WHERE t.PLAYER_ID = r.stale_impect_id;

-- Post-check: re-run the app-repo verification (backend cutover_compare +
-- the list-items resolution query) — unresolved counts should drop to the
-- already-broken-in-legacy residue only.
