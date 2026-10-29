-- One-off repair of CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE (2026-10-29).
--
-- 1. SCHEDULED_AT was written through write_pandas from timezone-aware datetimes
--    without Parquet logical types, which stored garbage (years like 978309430;
--    some values are so far out that Snowflake cannot even render them).
--    Fixed in backfill_historical_match_events._stage_and_merge. Restore the true
--    kick-off time from IMPECT_RAW.MATCHES (one SCHEDULEDDATE per ID, all ISO-8601
--    UTC) and NULL the rest; the next queue refresh refills this season's NULLs from
--    the IMPECT API.
-- 2. The 2026-10-29 run marked recent 26/27 / 2026 matches NO_EVENT_DATA (terminal)
--    because IMPECT had not published their events yet. Put those back to PENDING so
--    the next backfill run retries them.
--
-- Run as a role that can UPDATE the queue and read IMPECT_RAW.MATCHES (not BACKFILL_ROLE).

BEGIN;

UPDATE CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE q
   SET SCHEDULED_AT = CONVERT_TIMEZONE('UTC', TRY_TO_TIMESTAMP_TZ(m.SCHEDULEDDATE))::TIMESTAMP_NTZ,
       UPDATED_AT = CURRENT_TIMESTAMP()
  FROM (SELECT ID, ANY_VALUE(SCHEDULEDDATE) AS SCHEDULEDDATE
          FROM CAFC_DB.IMPECT_RAW.MATCHES GROUP BY ID) m
 WHERE m.ID = q.MATCH_ID;

-- Rows with no MATCHES entry keep their garbage value: clear it.
UPDATE CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE
   SET SCHEDULED_AT = NULL, UPDATED_AT = CURRENT_TIMESTAMP()
 WHERE MATCH_ID NOT IN (SELECT ID FROM CAFC_DB.IMPECT_RAW.MATCHES WHERE ID IS NOT NULL);

-- Retry this season's NO_EVENT_DATA matches from the last 30 days (or with no known date).
UPDATE CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE
   SET STATUS = 'PENDING', ATTEMPT_COUNT = 0, NEXT_ATTEMPT_AT = NULL, COMPLETED_AT = NULL,
       EVENT_COUNT = NULL, LAST_ERROR = NULL, UPDATED_AT = CURRENT_TIMESTAMP()
 WHERE STATUS = 'NO_EVENT_DATA'
   AND SEASON IN ('26/27', '2026')
   AND (SCHEDULED_AT IS NULL
        OR SCHEDULED_AT >= DATEADD(day, -30, CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ));

COMMIT;

-- Check: expect 0 rows outside 2000-2035, and ~1,165 PENDING for this season.
SELECT COUNT_IF(SCHEDULED_AT IS NOT NULL AND YEAR(SCHEDULED_AT) NOT BETWEEN 2000 AND 2035) AS bad_dates,
       COUNT_IF(SCHEDULED_AT IS NULL) AS null_dates,
       COUNT_IF(STATUS = 'PENDING' AND SEASON IN ('26/27', '2026')) AS pending_this_season
  FROM CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE;
