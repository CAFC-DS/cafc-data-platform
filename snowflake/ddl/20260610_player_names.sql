-- 20260610_player_names.sql
-- Store all three provider name forms on CORE.PLAYERS:
--   COMMON_NAME  (IMPECT commonname — what legacy PLAYERNAME always was)
--   FIRST_NAME / LAST_NAME (already existed, but mint.py never populated them;
--   ~11k post-migration minted rows were NULL)
-- DISPLAY_NAME stays the club-facing default and is refreshed to the latest
-- provider commonname for provider-linked players. Verified 2026-06-10: legacy
-- PLAYERNAME = commonname for 99.8% of rows; earlier mismatches ("A. Bytyqi"
-- vs "Armir Bytyqi") were data vintage, not a different name policy.
-- Re-runnable. mint.py now writes all three on new mints; this backfills.

ALTER TABLE CAFC_DB.CORE.PLAYERS
  ADD COLUMN IF NOT EXISTS COMMON_NAME VARCHAR(255)
  COMMENT 'Provider common/display name (IMPECT commonname). Matching key for identity resolution, refreshed from latest provider data.';

-- Safety: zero-copy snapshot before the bulk UPDATE (free until divergence).
CREATE TABLE IF NOT EXISTS CAFC_DB.CORE.PLAYERS_BACKUP_20260610
  CLONE CAFC_DB.CORE.PLAYERS;

-- Backfill from each player's best IMPECT identity, taking the most recent
-- iteration's name fields. Identity ranked like app_compat/players.sql:
-- primary first, then match confidence, then identity id.
MERGE INTO CAFC_DB.CORE.PLAYERS p
USING (
    SELECT
        i.CAFC_PLAYER_ID,
        s.first_name,
        s.last_name,
        s.common_name
    FROM (
        SELECT CAFC_PLAYER_ID, SOURCE_PLAYER_ID
        FROM CAFC_DB.CORE.PLAYER_IDENTITIES
        WHERE SOURCE_SYSTEM = 'IMPECT'
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY CAFC_PLAYER_ID
            ORDER BY CASE WHEN IS_PRIMARY THEN 0 ELSE 1 END,
                     MATCH_CONFIDENCE DESC NULLS LAST,
                     PLAYER_IDENTITY_ID
        ) = 1
    ) i
    JOIN (
        SELECT ID, FIRSTNAME AS first_name, LASTNAME AS last_name,
               COMMONNAME AS common_name
        FROM CAFC_DB.IMPECT_RAW.PLAYERS
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY ID ORDER BY ITERATION_ID DESC NULLS LAST
        ) = 1
    ) s ON s.ID::VARCHAR = i.SOURCE_PLAYER_ID
) u
ON p.CAFC_PLAYER_ID = u.CAFC_PLAYER_ID
WHEN MATCHED THEN UPDATE SET
    COMMON_NAME  = u.common_name,
    FIRST_NAME   = COALESCE(u.first_name, p.FIRST_NAME),
    LAST_NAME    = COALESCE(u.last_name,  p.LAST_NAME),
    DISPLAY_NAME = COALESCE(u.common_name, p.DISPLAY_NAME),
    UPDATED_AT   = CURRENT_TIMESTAMP();

-- Internal/manual players (no IMPECT identity) keep their DISPLAY_NAME and
-- get no COMMON_NAME — there is no provider name for them.
