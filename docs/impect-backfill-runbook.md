# IMPECT event backfill: runbook

How to run the historical event backfill without repeating the cost problem in the
1 October 2026 Snowflake cost report (12 `UPDATE ... PARSE_JSON` passes per match,
3-4 connections per match, 4-5 restarting workers on a shared warehouse).

## What changed

| Before | Now |
|---|---|
| `write_pandas` into `EVENTS`, then 12 `UPDATE`s rewriting the new rows | Stage into a temp table, then **one `INSERT ... SELECT`** that parses all 12 VARIANT columns (`load_events_batch`) |
| One match per load | Several matches per load (`--load-matches`, default 10) |
| 3-4 Snowflake connections per match | One connection per batch, reused |
| One `UPDATE` per match for queue state, one `MERGE` per match for match info | One `UPDATE` per batch, one `MERGE` per batch (`upsert_many`) |
| Per-match `DELETE` on every load | `DELETE` only for matches that already have rows, in the same transaction as the insert |
| Fetch, then load, strictly in turn | IMPECT fetching (`--fetch-workers`, default 3) overlaps the previous group's load |
| Killed worker leaves rows `RUNNING` for 12 h and burns an attempt | SIGTERM hands unfinished claims back without counting the attempt; the supervisor also returns claims older than 90 min |
| Supervisor waited forever on rows in cooldown, then crashed | Exits 0 when nothing is claimable; cooldown/exhausted rows are reported, not waited for |
| No tags | Every session carries a `QUERY_TAG` |

No table DDL changed. `EVENTS_LOAD_STAGE` and `MATCH_INFO_LOAD_STAGE` are session-scoped temporary tables.

## 1. Dedicated ingestion warehouse (admin, once)

Run as a role that can create warehouses and monitors. Names and the quota are placeholders.

```sql
CREATE WAREHOUSE IF NOT EXISTS INGEST_WH
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
  STATEMENT_TIMEOUT_IN_SECONDS = 1800
  COMMENT = 'IMPECT historical backfill only. Nothing else runs here.';

CREATE RESOURCE MONITOR IMPECT_INGEST_MONITOR WITH CREDIT_QUOTA = <approved credits>
  FREQUENCY = MONTHLY START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 50 PERCENT DO NOTIFY
           ON 75 PERCENT DO NOTIFY
           ON 90 PERCENT DO SUSPEND
           ON 100 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE INGEST_WH SET RESOURCE_MONITOR = IMPECT_INGEST_MONITOR;
GRANT USAGE ON WAREHOUSE INGEST_WH TO ROLE <role the backfill connects as>;
```

Point the backfill at it with `SNOWFLAKE_WAREHOUSE=INGEST_WH` (and make sure
`SNOWFLAKE_ROLE` is a role with usage on it). Run nothing else on it.

## 2. Benchmark 100 matches before any bulk run

From `python/extract/impect/`, with the usual `SNOWFLAKE_*` / `IMPECT_*` environment:

```bash
python backfill_historical_match_events.py --process --max-matches 100 \
  --batch-size 25 --load-matches 10 --fetch-workers 3 \
  --query-tag "project=cafc-data-platform;job=impect-benchmark"
```

Each batch prints one line, for example
`batch: claimed=25 success=24 no_event_data=1 failed=0 rows=64000 fetch_wait=18.2s load=21.0s total=41.5s (2170 matches/hour)`.
`fetch_wait` is time blocked on IMPECT, `load` is time in Snowflake, so the bottleneck is visible.

Then read the credits (both views lag: attribution up to ~8 h, metering up to ~3 h):

```sql
-- Compute credits attributed to the benchmark's queries (excludes warehouse idle time)
SELECT COUNT(*) AS statements, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS compute_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
WHERE START_TIME >= DATEADD(day, -1, CURRENT_TIMESTAMP())
  AND QUERY_TAG = 'project=cafc-data-platform;job=impect-benchmark';

-- What the warehouse actually billed over the run window (includes idle/resume time)
SELECT SUM(CREDITS_USED) AS credits
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE WAREHOUSE_NAME = 'INGEST_WH' AND START_TIME >= '<run start>' AND END_TIME <= '<run end>';
```

Record **credits per match = billed credits / 100** and **matches per hour**, then forecast:
`remaining matches x credits per match`. Get that forecast approved before section 3.
The old design measured roughly 0.011 credits per match for the JSON updates alone
(328.64 credits / ~29,500 matches), so anything near that means the change did not work.

## 3. Full run on Railway, one season at a time

Service variables (all names; secrets stay in Railway):

| Variable | Meaning |
|---|---|
| `BACKFILL_SEASONS` | Queue `SEASON` labels, e.g. `25/26` then `24/25`; calendar-year leagues use `2026`, `2025`. `BACKFILL_ITERATION_IDS` also accepted |
| `BACKFILL_MAX_MATCHES` | Credit budget expressed in matches (approved credits / measured credits per match) |
| `BACKFILL_MAX_MINUTES` | Optional wall-clock cap |
| `BACKFILL_WORKERS` (2), `BACKFILL_FETCH_WORKERS` (3), `BACKFILL_LOAD_MATCHES` (10), `BACKFILL_BATCH_SIZE` (25) | Throughput knobs; raise only if the benchmark shows headroom and IMPECT is not answering 429 |
| `BACKFILL_QUERY_TAG` | e.g. `project=cafc-data-platform;job=impect-backfill;season=25/26` |

The service exits 0 when the scope is drained or a budget is reached (restart policy
`ON_FAILURE` leaves it stopped), and 1 if it cannot make progress for
`BACKFILL_MAX_STALLED_MINUTES` (default 120).

Operational notes:

- The Railway service rebuilds on pushes that touch `Dockerfile.backfill` or
  `python/extract/impect/**`. A push redeploys, and so starts, the service with whatever
  variables are currently set. Update the variables (or stop the service) before pushing.
- A redeploy now hands claims back (SIGTERM) instead of stranding them.
- Matches whose events endpoint returns 404 are marked `FAILED` with a 12 h cooldown and
  stop after 5 attempts; they are reported but never block the exit.
- The queue is the source of truth. `python backfill_historical_match_events.py --status`
  prints counts by status.
