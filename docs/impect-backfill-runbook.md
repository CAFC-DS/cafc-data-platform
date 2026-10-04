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
CREATE WAREHOUSE IF NOT EXISTS BACKFILL_WH
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
  STATEMENT_TIMEOUT_IN_SECONDS = 1800
  COMMENT = 'IMPECT historical backfill only. Nothing else runs here.';

CREATE RESOURCE MONITOR IMPECT_INGEST_MONITOR WITH CREDIT_QUOTA = <approved credits>
  FREQUENCY = MONTHLY START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 50 PERCENT DO NOTIFY
           ON 75 PERCENT DO NOTIFY
           ON 90 PERCENT DO SUSPEND
           ON 100 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE BACKFILL_WH SET RESOURCE_MONITOR = IMPECT_INGEST_MONITOR;
```

### Role and service user (admin, once)

The connector logs in as a **user** (with an RSA key) and uses a **role** for its permissions, so
create both: a least-privilege role, and a service user that only ever holds that role. Use a
new key pair for it rather than a person's key.

```sql
-- as SECURITYADMIN (or a role that can create roles/users)
CREATE ROLE IF NOT EXISTS BACKFILL_ROLE COMMENT = 'IMPECT historical backfill, least privilege';
CREATE USER IF NOT EXISTS BACKFILL_USER TYPE = SERVICE  -- drop TYPE if your account lacks it
  DEFAULT_ROLE = BACKFILL_ROLE DEFAULT_WAREHOUSE = BACKFILL_WH
  COMMENT = 'Service user for the IMPECT backfill (key-pair auth only)';
GRANT ROLE BACKFILL_ROLE TO USER BACKFILL_USER;
ALTER USER BACKFILL_USER SET RSA_PUBLIC_KEY = '<public key, without the BEGIN/END lines>';

-- as ACCOUNTADMIN (or the owner of these objects)
GRANT USAGE ON WAREHOUSE BACKFILL_WH TO ROLE BACKFILL_ROLE;
GRANT USAGE ON DATABASE CAFC_DB TO ROLE BACKFILL_ROLE;
GRANT USAGE ON SCHEMA CAFC_DB.IMPECT_RAW TO ROLE BACKFILL_ROLE;
GRANT USAGE ON SCHEMA CAFC_DB.CORE TO ROLE BACKFILL_ROLE;
-- write_pandas and the staging step create TEMPORARY objects in IMPECT_RAW
GRANT CREATE TABLE, CREATE STAGE, CREATE FILE FORMAT ON SCHEMA CAFC_DB.IMPECT_RAW TO ROLE BACKFILL_ROLE;
GRANT SELECT, INSERT, DELETE ON TABLE CAFC_DB.IMPECT_RAW.EVENTS TO ROLE BACKFILL_ROLE;      -- DELETE only for re-pulls
GRANT SELECT, INSERT, UPDATE ON TABLE CAFC_DB.IMPECT_RAW.MATCH_INFO TO ROLE BACKFILL_ROLE;  -- MERGE
GRANT SELECT, UPDATE ON TABLE CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE TO ROLE BACKFILL_ROLE;
GRANT SELECT, INSERT, UPDATE ON TABLE CAFC_DB.CORE.INGESTION_RUNS TO ROLE BACKFILL_ROLE;
```

Those grants cover **processing** (`--process` and the Railway supervisor). Discovery
(`--discover`) also writes the discovery table and iteration metadata and creates temp objects in
`CORE`, so run it as a person's role, not as `BACKFILL_ROLE`.

Create the key pair locally, keep the private key out of the repo, and put its PEM text in
`SNOWFLAKE_PRIVATE_KEY`:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -nocrypt -out backfill_rsa_key.p8
openssl rsa -in backfill_rsa_key.p8 -pubout -out backfill_rsa_key.pub
```

Backfill variables: `SNOWFLAKE_USER=BACKFILL_USER`, `SNOWFLAKE_ROLE=BACKFILL_ROLE`,
`SNOWFLAKE_WAREHOUSE=BACKFILL_WH`, `SNOWFLAKE_PRIVATE_KEY=<new key>`. Once the service runs
on these, remove any broader key or role (for example an ACCOUNTADMIN login) from the Railway
service. Run nothing else on `BACKFILL_WH`.

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
WHERE WAREHOUSE_NAME = 'BACKFILL_WH' AND START_TIME >= '<run start>' AND END_TIME <= '<run end>';
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

### Staged plan: season blocks, newest first, by geographic group

`python/extract/impect/backfill_plans/five_seasons_men.json` splits the queue into **5 season
blocks** (`26/27+2026`, `25/26+2025`, `24/25+2024`, `23/24+2023`, `22/23+2022`; each pairs a
split-year season with its calendar-year twin), and each block into **11 geographic groups**
(Nordics & Baltics, British Isles, Germany/Austria/Switzerland, Western Europe, Southern Europe,
Central & Eastern Europe/Balkans/Turkey, North & Central America, South America, Asia/Middle East/Oceania,
Africa, International & continental). The group definitions live in `backfill_plan.py`.

| Variable | Meaning |
|---|---|
| `BACKFILL_PLAN` | `backfill_plans/five_seasons_men.json` (relative to `python/extract/impect/`) |
| `BACKFILL_BLOCKS` | Only these blocks, e.g. `26/27+2026`; unset runs every block in order |
| `BACKFILL_DRY_RUN=1` | Print each stage's live counts and exit without starting workers |

Stages run in order. A finished stage costs only a count query, an unfinished one resumes from the
queue, and a stalled stage is reported but does not stop later stages (the run then exits 1).
Every Snowflake session carries `...;block=<block>;group=<group>`, so credits can be reported per stage:

```sql
SELECT QUERY_TAG, COUNT(*) AS statements, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS credits
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
WHERE START_TIME >= DATEADD(day, -3, CURRENT_TIMESTAMP()) AND QUERY_TAG LIKE 'project=cafc-data-platform;job=impect-backfill;block=%'
GROUP BY 1 ORDER BY 1;
```

Regenerate the plan with `python backfill_plan.py --write backfill_plans/five_seasons_men.json`
(read-only; it adds iterations that appeared since). The plan lists iteration ids only; what is left to do
is always read from the queue.

**Before running a season that is still being played**, add its newly completed matches (and any new
iterations) to the queue. This is queue-only and does not touch `MATCHES`/`SQUADS`/`PLAYERS`; run it as a
developer role, not `BACKFILL_ROLE`:

```bash
python refresh_backfill_queue.py --seasons 26/27,2026            # dry run
python refresh_backfill_queue.py --seasons 26/27,2026 --apply
```

Run one block per deployment, check the block's logs and credits, then move `BACKFILL_BLOCKS` to the next.

Operational notes:

- The Railway service rebuilds on pushes that touch `Dockerfile.backfill` or
  `python/extract/impect/**`. A push redeploys, and so starts, the service with whatever
  variables are currently set. Update the variables (or stop the service) before pushing.
- A redeploy now hands claims back (SIGTERM) instead of stranding them.
- The supervisor logs in to IMPECT once before starting workers, and the login is single-flight and retried,
  so workers starting together no longer race for a token.
- Matches whose events endpoint returns 404 are marked `FAILED` with a 12 h cooldown and
  stop after 5 attempts; they are reported but never block the exit.
- The queue is the source of truth. `python backfill_historical_match_events.py --status`
  prints counts by status.
