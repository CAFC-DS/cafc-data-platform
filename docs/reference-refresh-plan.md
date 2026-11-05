# Plan: weekly reference-data refresh (players, teams, competitions, matches)

_Status: proposed, 2026-11-05. Nothing in this plan is built yet._

## 1. Problem

Players' names, their teams and competitions, and matches go stale in the
recruitment app. The app reads `CAFC_DB.APP_COMPAT.players` / `matches`
(`read_table()` in the recruitment backend, 66 + 37 call sites), which are dbt
models over `CORE` and `IMPECT_RAW`. Those only change when someone runs the
loaders, the identity steps and dbt by hand.

### What the data looked like on 2026-11-05

| Object | Last changed | Note |
|---|---|---|
| `IMPECT_RAW.ITERATIONS` | 2026-08-17 | Competitions/seasons created since mid-August are missing |
| `IMPECT_RAW.PLAYERS`, `SQUADS` | 2026-09-16 | 7 weeks old: transfers, new players, name fixes missing |
| `IMPECT_RAW.MATCHES` | 2026-10-29 | Refreshed by hand |
| `IMPECT_RAW.COUNTRIES` | 2026-02-19 | Rarely changes |
| `CORE.CORE_SQUADS` / `CORE_COMPETITIONS` / `CORE_SEASONS` | 2026-09-04 | dbt tables, last built by hand |
| `CORE.CORE_PLAYER_CURRENT_CONTEXT` | 2026-10-16 | Drives SQUADNAME / COMPETITIONNAME / SEASON |
| `CORE.FIXTURES` | 2026-11-04 | Only 2 men's IMPECT matches unlinked: the fixture linker was run by hand recently |
| `CORE.PLAYER_IDENTITY_CANDIDATES` | empty | No ambiguity backlog to clear first |

Player attributes in `CORE.PLAYERS` versus IMPECT's latest record, for the
114,888 IMPECT-created players: **1,042 display names, 955 birth dates and
1,498 first/last names differ.** `mint.py` writes these once and nothing
updates them.

### Root causes

1. **Nothing runs on a schedule.** The Railway crons (`impect-backfill` Sun
   06:00 UTC, `scout-refresh` Sun 09:00 UTC) and GitHub `championship-sync`
   only handle event data and scoring.
2. **The orchestrator never links fixtures.** `python/orchestrator.py` runs the
   player matcher but not `python/identity/fixture_matcher.py`, so new IMPECT
   matches only reach `CORE.FIXTURES` (and therefore `app_compat.matches`)
   when someone runs the linker by hand.
3. **Player attributes are frozen at mint time.** Nothing copies IMPECT name
   or birth-date corrections onto `CORE.PLAYERS`.
4. **The full-table loaders are unsafe to schedule.** `load_players.py` /
   `load_squads.py` / `load_matches.py` loop over every iteration, swallow
   per-iteration API errors, and then replace the whole table. One failed call
   silently deletes that iteration's rows.
5. **`refresh_iteration_metadata.py` (the per-iteration alternative) isn't
   atomic.** It runs `DELETE` and then `write_pandas` on an autocommit
   connection, and `write_pandas` issues DDL (a temporary stage). If the load
   fails, the iteration's slice is left empty. Any fetch error also aborts the
   whole run.

## 2. Target design

One new Railway cron service, **`reference-refresh`**, in project
`cheerful-healing`, built from this repo. It runs a new orchestrator command,
`python -m python.orchestrator refresh-reference`, **every Sunday at 03:00
UTC**. That is ahead of `impect-backfill` (06:00) and `scout-refresh` (09:00),
so Sunday's runs go reference → events → scoring.

```
reference-refresh (Sun 03:00 UTC, run-to-completion, ~20-40 min)
 1  preflight          env, Snowflake login, IMPECT login, no overlapping run
 2  open run           CORE.INGESTION_RUNS (TRIGGERED_BY='reference-refresh')
 3  small dims         ITERATIONS, COUNTRIES: full replace, with a shrink guard
 4  scope              current-season men's iterations (~126 today)
 5  raw slices         MATCHES / SQUADS / PLAYERS for those iterations: atomic swap
 6  dbt staging        tag:staging (views; cheap)
 7  player matcher     link / mint / queue-ambiguous             (exists)
 8  attribute sync     CORE.PLAYERS name/DOB/etc. from IMPECT    (new)
 9  dbt squads         core_squads core_competitions core_seasons
10  fixture matcher    mint CORE.FIXTURES for new IMPECT matches (exists, now wired in)
11  dbt reference      selector `reference_refresh` (context, players_base, players, matches, ...)
12  dbt test           same selector, plus the new freshness/linkage tests
13  close run          SUCCESS + JSON counts in NOTES; exit 1 on failure
```

Order constraints that matter:

- **Step 9 before step 10.** `fixture_matcher.fetch_unlinked` inner-joins
  `CORE.CORE_SQUADS` and filters to `GENDER='MALE'`. A match involving a squad
  that is new this week stays unlinked until `core_squads` is rebuilt.
- **Steps 7 and 8 before step 11.** `players_base` reads `CORE.PLAYERS` and
  `PLAYER_IDENTITIES`.
- **The heavy facts are out of scope.** Do not build `tag:facts` here:
  `core_player_iteration_kpis` alone is ~329M rows. `core_player_recent_position`
  reads all of `EVENTS` and belongs with the event pipeline. The reference
  refresh builds only the dimensions and app_compat models that carry names,
  teams, competitions and matches.

### Scope: which iterations count as current

Pick automatically from today's date, overridable with `REFRESH_SEASONS`:

| Always | June–August only | From November on |
|---|---|---|
| current split season (`26/27`) + current calendar year (`2026`) | previous split season (`25/26`), so late results, play-offs and summer transfers land | next calendar year (`2027`), which IMPECT creates early for spring leagues |

Women's iterations are excluded (the platform-wide rule, same as the
matchers). On 2026-11-05 that is 84 + 42 = 126 iterations, about 380 API calls,
paced by the existing `impect_api` rate limiter.

## 3. Work items

### PR 1: safe slice refresh (`python/extract/impect/`)

Rework `refresh_iteration_metadata.py` into the step-5 engine (or add
`refresh_reference_slices.py` and keep the old CLI working).

- **Fetch** each `(table, iteration)` pair, catching errors per pair. A
  failure keeps that iteration's existing rows and is recorded.
- **Shrink guard per iteration.** If an iteration that had more than 20 rows
  comes back with under 50% of them, skip it and warn. That pattern is an API
  hiccup, not real data.
- **Fail threshold.** If more than 10% of pairs failed or were skipped, finish
  the swap for the good ones, then mark the run FAILED so someone looks.
- **Atomic swap per table:**
  1. `write_pandas` everything fetched into a transient
     `IMPECT_RAW.<TABLE>__REFRESH` table (`overwrite=True`).
  2. In one explicit transaction: `BEGIN; DELETE FROM <TABLE> WHERE <iter_col>
     IN (<refreshed ids>); INSERT INTO <TABLE> (<common cols>) SELECT <common
     cols> FROM <TABLE>__REFRESH; COMMIT;`
  3. Drop the transient table.

  The iteration column is `ITERATIONID` for MATCHES and `ITERATION_ID` for
  SQUADS and PLAYERS.
- **Column drift.** Insert only the columns the target already has. Log any
  new API columns, but don't add them.
- **`--dry-run`.** Fetch and report per-table and per-iteration old→new row
  counts, without writing.
- **Small dims (step 3).** `load_iterations.py` / `load_countries.py` get the
  same shrink guard: refuse to replace if the new count is under 95% of the
  current one.
- **Season-scope helper** (`reference_scope.py`) implementing the table in §2.
- **One connection per run**, instead of the current one per iteration × table.
- **Tests** (`tests/test_refresh_reference_slices.py`, monkeypatched API and
  cursor, in the style of `tests/test_load_match_info.py`):
  - scope helper across the year boundaries;
  - a failed fetch keeps the old slice;
  - the shrink guard;
  - the fail threshold;
  - the SQL issued for the swap, with the right iteration column per table.

### PR 2: player attribute sync (`python/identity/sync_attributes.py`)

- For each active `CORE.PLAYERS` row with `CREATED_FROM_SOURCE = 'IMPECT'`,
  take its primary IMPECT identity (same `row_number()` rule as
  `players_base.sql`). Join to that player's latest `IMPECT_RAW.PLAYERS` row
  (highest `ITERATION_ID`). One `MERGE` updates `DISPLAY_NAME`, `COMMON_NAME`
  (from `COMMONNAME`), `FIRST_NAME`, `LAST_NAME`, `BIRTH_DATE`, `BIRTH_PLACE`
  and `STRONG_FOOT`.
  - Only overwrite with **non-blank** source values; never blank a field.
  - Only touch rows that actually differ (`IS DISTINCT FROM`), and set
    `UPDATED_AT = CURRENT_TIMESTAMP()` on them.
- **Out of scope:** manual players (`CREATED_FROM_SOURCE='MANUAL'`), which
  scouts typed in, and retired players (`IS_ACTIVE=FALSE`). The recruitment
  app has no endpoint that edits player names, so nothing the app writes is
  overwritten.
- **Change log.** Before the MERGE, insert old/new values into a new
  `CORE.PLAYER_ATTRIBUTE_CHANGES (RUN_ID, CAFC_PLAYER_ID, COLUMN_NAME,
  OLD_VALUE, NEW_VALUE, CHANGED_AT)`, in the same transaction. This is the
  audit trail and the rollback.
- **Bulk guard.** Abort if one run would change more than 5% of players
  (~6k), unless `--allow-bulk`. The first run is ~1.5k, so it passes.
- **Matcher side effect (intended).** The player matcher matches on
  `DISPLAY_NAME` + `BIRTH_DATE`, so corrected values make future links more
  accurate. Note this in the module docstring.
- **Optional, same PR.** Sync `CORE.FIXTURES.FIXTURE_DATE` for IMPECT-created
  fixtures whose IMPECT `SCHEDULEDDATE` moved (postponements). `app_compat.matches`
  already prefers the live IMPECT date, but `core_fixture_details` and the
  Phase-6 reads use `FIXTURES`. Measure the drift first; only build this if it
  is non-trivial.
- **DDL:** `snowflake/ddl/2026MMDD_player_attribute_changes.sql`.
- **Tests:** unit test of the generated SQL and the guard; `--dry-run`
  reports counts per column.

### PR 3: orchestrator command, fixture-matcher fix, dbt wiring

- **`python/orchestrator.py`:** add a `refresh-reference` subcommand that runs
  steps 1–13 above.
  - Reuse `open_run` / `close_run`.
  - Write per-step counts into `NOTES` as JSON (iterations refreshed,
    rows per table, failed pairs, players linked / minted / ambiguous,
    attributes changed, fixtures minted).
  - Add `--dry-run`: every Python step in its dry-run mode, plus dbt against
    the `dev` target.
- **dbt binary:** fall back to `shutil.which("dbt")` when `.venv/bin/dbt`
  doesn't exist (the container has no venv). Honour `DBT_PROFILES_DIR`.
- **`fixture_matcher.run`:** when a `run_id` is passed in, it never calls
  `conn.commit()`. That only works because the connector autocommits. Make it
  commit explicitly in both paths, and add a test.
- **Overlap guard:** refuse to start if another `reference-refresh` row is
  `RUNNING` and less than 3 hours old. Older ones count as dead and are marked
  `ABANDONED`.
- **`dbt/selectors.yml`:** add selector `reference_refresh`:
  - `core_squads`, `core_competitions`, `core_seasons`
  - `core_player_current_context`, `core_fixture_details`
  - `players_base`, `players`, `matches`
- **`dbt_project.yml`:** set `+copy_grants: true` on canonical dimensions and
  app_compat. Otherwise `CREATE OR REPLACE` on `players_base` /
  `core_player_current_context` can drop `APP_ROLE`'s SELECT, because there is
  no future-grant for APP_ROLE on CORE tables.
- **New singular dbt tests:**
  - `reference_fixtures_linked.sql`: men's IMPECT matches in scoped iterations
    with valid squads/date but no `FIXTURE_IDENTITIES` row (expect 0; warn
    severity at first).
  - `reference_players_base_complete.sql`: active `CORE.PLAYERS` missing from
    `players_base` (expect 0).
  - `reference_names_in_sync.sql`: IMPECT-created players whose name still
    differs from IMPECT after the sync (expect 0).
- **Tests:** orchestrator step order with all steps mocked; a failure in any
  step closes the run FAILED and returns 1; dry-run writes nothing.

### PR 4: container and Railway packaging

- **`Dockerfile.refresh`:** `python:3.11-slim`; copy `pyproject.toml`,
  `python/` and `dbt/`; `pip install .` (dbt-snowflake is already a
  dependency); `pip install -r python/extract/impect/requirements.txt`;
  `CMD ["bash", "deploy/refresh_entrypoint.sh"]`.
- **`deploy/refresh_entrypoint.sh`:**
  1. Write `$SNOWFLAKE_PRIVATE_KEY` to `/tmp/sf_key.pem` (chmod 600) and export
     `SNOWFLAKE_PRIVATE_KEY_PATH`. Both `_snowflake.py` and the IMPECT loader
     read a key **path**, while Railway stores the PEM as a variable, as in
     `impect-backfill`.
  2. Export `DBT_PROFILES_DIR=/app/deploy`.
  3. `exec python -u -m python.orchestrator refresh-reference --target
     "${DBT_TARGET:-prod}" --triggered-by railway-cron ${REFRESH_DRY_RUN:+--dry-run}`.
- **`deploy/profiles.yml`:** a copy of `dbt/profiles.yml.example`'s `prod`
  output. It reads env vars only, so no secrets are committed. Query tag
  `dbt:prod:reference-refresh`.
- **Docs:** add a "reference refresh" section to `docs/system-handbook.md`
  and a runbook `docs/runbooks/reference-refresh.md` covering what it does,
  how to run by hand, how to read `NOTES`, rollback, and the Monday check.

## 4. Snowflake prerequisites (one-off, admin)

1. **Service user** `SVC_REFERENCE_REFRESH` with RSA key-pair auth.
2. **Role.** Every prod `CORE` / `APP_COMPAT` / `IMPECT_RAW` object is owned
   by `DEV_ROLE` today, and dbt's `CREATE OR REPLACE` needs ownership.
   - **Now:** grant `DEV_ROLE` to the service user. This matches how prod is
     built today, and it is the only option that works without moving
     ownership.
   - **Later (cleanup):** move ownership of the dbt-built objects and the raw
     tables to `DATA_PLATFORM_ROLE` (`GRANT OWNERSHIP ... COPY CURRENT GRANTS`)
     and switch the service to it.
3. **Warehouse:** an XS warehouse with `AUTO_SUSPEND = 60`. Tag sessions
   `project=cafc-data-platform;job=reference-refresh` for cost reporting
   (same pattern as the backfill).
4. **Check the app's grants.** Find the role the recruitment backend connects
   as. Confirm it has SELECT on `APP_COMPAT.PLAYERS` / `PLAYERS_BASE` /
   `MATCHES` and on `CORE.CORE_PLAYER_CURRENT_CONTEXT`, `CORE_SQUADS` etc.
   (`copy_grants` keeps these across rebuilds).
5. **Clear stale run rows.** `INGESTION_RUNS` has **3,713 rows stuck in
   `RUNNING`** since 2026-06-01 (crashed extractor runs that never closed).
   Mark rows more than a day old as `ABANDONED`, so "is a run in progress?"
   and the weekly check give real answers. Fixing the extractors that leave
   them is a separate task.

## 5. Railway setup

New service in `cheerful-healing` / `production`:

| Setting | Value |
|---|---|
| Name | `reference-refresh` |
| Source | `CAFC-DS/cafc-data-platform`, branch `main` |
| Builder | Dockerfile, path `Dockerfile.refresh` |
| Watch paths | `Dockerfile.refresh`, `deploy/**`, `python/**`, `dbt/**` |
| Cron | `0 3 * * 0` (Sundays 03:00 UTC) |
| Restart policy | `NEVER` |
| Resources | 1 vCPU / 2 GB is plenty (biggest frame is ~60k player rows) |

Variables:

- **Snowflake:** `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`
  (=`SVC_REFERENCE_REFRESH`), `SNOWFLAKE_PRIVATE_KEY` (PEM), `SNOWFLAKE_ROLE`,
  `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE=CAFC_DB`.
- **IMPECT:** `IMPECT_USERNAME`, `IMPECT_PASSWORD` (same values as
  `impect-backfill`).
- **Optional:** `REFRESH_SEASONS` (override the automatic scope),
  `REFRESH_DRY_RUN=1`, `DBT_TARGET`.

**Deploys may start a run.** The backfill runbook notes that a push redeploys
and starts a cron service. That is safe here because every step is
idempotent; a mid-week run is just an early refresh. Keep `REFRESH_DRY_RUN=1`
set until the first live run (§6.4), so pushes before go-live can't write
anything.

## 6. Rollout

1. **Build PRs 1–4**, each with its tests (`pytest`, `ruff`). dbt changes are
   checked with `dbt build --select reference_refresh --target dev`.
2. **Rehearse locally.** Run `python -m python.orchestrator refresh-reference
   --dry-run` with prod credentials. All Python steps report, dbt builds into
   the dev schemas. Check:
   - expected iteration count;
   - per-table row deltas;
   - players to link and mint;
   - ~1.5k attribute changes;
   - fixtures to mint.

   The Python steps write to hard-coded `CAFC_DB` tables, so dry-run is the
   only rehearsal. There is no point running live against a clone.
3. **Snapshot:** `CREATE DATABASE CAFC_DB_SNAPSHOT_<date> CLONE CAFC_DB;`
   (free until data diverges; keep 30 days).
4. **First live run, by hand on Railway.** Deploy with `REFRESH_DRY_RUN=1`,
   check the logs, then unset it and trigger one run. Afterwards check:
   - `INGESTION_RUNS` row is `SUCCESS` and its `NOTES` counts look right;
   - dbt tests pass;
   - in the app (`dependable-adventure`): search a player whose name changed,
     a player who moved club since September, and a match played this week.
5. **Turn on the cron.** Watch the first two Sunday runs.
6. **Weekly check** (Monday, ~5 min):

   ```sql
   SELECT RUN_ID, STATUS, STARTED_AT, FINISHED_AT, NOTES
   FROM CAFC_DB.CORE.INGESTION_RUNS
   WHERE TRIGGERED_BY = 'railway-cron' AND SOURCE_SYSTEM = 'IMPECT'
   ORDER BY RUN_ID DESC LIMIT 3;

   SELECT COUNT(DISTINCT SOURCE_PLAYER_ID) FROM CAFC_DB.CORE.PLAYER_IDENTITY_CANDIDATES;
   ```

   Railway also marks a failed cron run (exit 1).
7. **Later:** if midweek fixtures need to appear sooner, add a Wednesday
   03:00 UTC run (same service, cron `0 3 * * 0,3`).

## 7. Rollback

| What went wrong | Undo |
|---|---|
| Bad raw slice in `IMPECT_RAW.*` | Time Travel: `CREATE OR REPLACE TABLE IMPECT_RAW.PLAYERS CLONE IMPECT_RAW.PLAYERS AT (TIMESTAMP => '<run start>')` (default 1-day retention; otherwise the snapshot clone) |
| Bad attribute changes | Re-apply `OLD_VALUE` from `CORE.PLAYER_ATTRIBUTE_CHANGES WHERE RUN_ID = X` |
| Bad player / fixture mints | `CORE.PLAYERS` / `PLAYER_IDENTITIES` / `FIXTURES` / `FIXTURE_IDENTITIES` have no `RUN_ID` column (checked 2026-11-05), so select by `CREATED_AT` between the run's `STARTED_AT` and `FINISHED_AT`, plus `CREATED_FROM_SOURCE='IMPECT'` / `SOURCE_CONTEXT`. Deleting mints is only safe before any scout report references them, so prefer `merge.py` after the first day |
| Bad dbt build | Fix and re-run `dbt build --select reference_refresh --target prod` |
| Stop everything | Remove the cron schedule on the Railway service |

## 8. Out of scope / known limits

- **Transfers IMPECT hasn't recorded yet.** Team and competition come from
  the player's highest-ID IMPECT iteration. If IMPECT hasn't added the player
  to their new club's iteration yet, no refresh can know about the move.
- **Manual players and matches** keep what scouts entered, on purpose.
- **Recruitment-platform code: no changes needed.** It already reads
  `APP_COMPAT.players/matches`. The backend's in-process `_data_cache` should
  expire well within a week; confirm its TTL during the first live run.
- **Squad identity** is still `cafc_squad_id = impect_squad_id` (see
  `core_squads.sql`). Not changed here.
- **Snowflake cleanup** (backup tables, `_TEST` / `_DEBUG`, ownership moves,
  retiring `RECRUITMENT_TEST`) runs separately and doesn't block this, apart
  from the §4 prerequisites.

## 9. Effort

| Item | Estimate |
|---|---|
| PR 1 slice refresh | 0.5–1 day |
| PR 2 attribute sync | 0.5 day |
| PR 3 orchestrator + dbt | 0.5–1 day |
| PR 4 container + docs | 0.5 day |
| §4 Snowflake prerequisites | 1 hour (admin) |
| Rollout §6.2–6.5 | 1 hour active, then two Sundays of watching |
