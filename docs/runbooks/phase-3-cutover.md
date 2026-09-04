# Runbook — Phase 3 cutover: scout reports + lists move to `CAFC_DB.CORE`

The first phase where data **physically moves homes**. Seven tables leave
`RECRUITMENT_TEST.PUBLIC` and become canonical in `CAFC_DB.CORE`:
`SCOUT_REPORTS`, `SCOUT_REPORT_ATTRIBUTE_SCORES`, `SCOUT_REPORT_VIEWS`,
`PLAYER_LISTS`, `PLAYER_LIST_ITEMS`, `POSITION_ATTRIBUTES`, `SHARED_REPORT_LINKS`.

Two halves, already built:
- **App half** (recruitment repo, branch `feature/canonical-cutover-phase-3-scout-lists`):
  all reads/writes for these tables templated via `read_table()`/`write_table()`;
  `WRITE_DB` env var added. No-op under defaults — verified 35/35.
- **Platform half** (this repo, branch `feature/phase-3-core-move`):
  clone script `snowflake/ddl/20260610_phase3_move_scout_lists_to_core.sql`,
  compat views repointed `recruitment_legacy` → `core_app`.

## Why a write freeze is required

Live users write scout reports continuously. The clone is a snapshot: any row
written to legacy *after* the clone but *before* the app flips is lost from
CORE. So the final clone and the app flip must happen inside one short window
(minutes) when nobody is writing — out of hours.

## States

| State | App env | Reads | Writes |
|---|---|---|---|
| A. Today (prod) | none of the seam vars set | legacy | legacy |
| B. Soak (optional, local/staging) | `CANONICAL_DB=CAFC_DB PLATFORM_DB_SCHEMA=APP_COMPAT WRITE_DB=RECRUITMENT_TEST` | APP_COMPAT (views → legacy for unmoved tables) | legacy |
| C. Cut over | `CANONICAL_DB=CAFC_DB PLATFORM_DB_SCHEMA=APP_COMPAT CORE_DB_SCHEMA=CORE` (no `WRITE_DB`) | APP_COMPAT (views → CORE for moved tables) | CORE |

## Preconditions (all must be true)

- [ ] Recruitment-repo phases 0 + 1+2 + 3 merged to `main` and **deployed** to
      Railway with NO seam env vars set (state A — deploy is a no-op).
- [ ] `cutover_compare` clean against the deployed build (default mode).
- [ ] This repo's `feature/phase-3-core-move` merged to `main`.
- [ ] A maintenance window agreed (out of hours; ~15 minutes).
- [ ] Whoever runs this has DEV_ROLE (owns `CAFC_DB.CORE`) and Railway access.

## Cutover steps (inside the window)

1. **Announce / verify quiet.** No active writers (check
   `QUERY_HISTORY` for INSERT/UPDATE on the 7 tables in the last few minutes).
2. **Run the move script** (`snowflake/ddl/20260610_phase3_move_scout_lists_to_core.sql`),
   top to bottom. It: snapshots all of `RECRUITMENT_TEST` (free clone) → clones
   the 7 tables into `CORE` → applies grants → prints the row-count parity
   check. **All seven counts must match exactly** — if not, a write slipped in:
   re-run section 2 of the script.
3. **Repoint the compat views**: from this repo,
   `dbt run --select app_compat --target prod`
   (builds the 7 repointed views + the rest of app_compat; views over CORE now).
4. **Flip the app** (Railway env): set
   `CANONICAL_DB=CAFC_DB`, `PLATFORM_DB_SCHEMA=APP_COMPAT`, `CORE_DB_SCHEMA=CORE`
   (ensure `WRITE_DB` is **unset**), redeploy/restart. Startup log must read:
   `READ_PREFIX=CAFC_DB.APP_COMPAT  WRITE_PREFIX=CAFC_DB.CORE`.
5. **Verify, in order:**
   - `cutover_compare` capture against prod → diff vs a pre-window capture:
     only freshness-class diffs allowed.
   - Write test: create a scout report as a test user → confirm the row lands
     in `CAFC_DB.CORE.SCOUT_REPORTS` (`SELECT MAX(ID), MAX(CREATED_AT)`), is
     visible in the app, and `RECRUITMENT_TEST.PUBLIC.SCOUT_REPORTS` did NOT
     grow.
   - Lists test: add/remove a player from a shortlist; same checks on
     `CORE.PLAYER_LIST_ITEMS`.
   - Role spot-check: scout sees own reports only; loan manager sees own +
     loan reports (CLAUDE.md matrix).
6. **Close the window.** Leave the legacy 7 tables in place, frozen — they are
   the rollback target and retire later via the quiet-period process.

## Rollback

**Before any post-flip write** (steps 4–5 failed cleanly):
unset the three env vars on Railway, restart. App reads/writes legacy again.
Re-run `dbt run --select app_compat --target prod` from `main`~ (pre-merge) if
the views must also revert — or simply leave them; nothing reads APP_COMPAT in
state A.

**After post-flip writes landed in CORE** (found a problem later):
1. Unset env vars, restart (app back on legacy).
2. Copy the stranded rows back, e.g. for scout reports:
   ```sql
   INSERT INTO RECRUITMENT_TEST.PUBLIC.SCOUT_REPORTS
   SELECT * FROM CAFC_DB.CORE.SCOUT_REPORTS c
   WHERE c.ID > (SELECT MAX(ID) FROM RECRUITMENT_TEST.PUBLIC.SCOUT_REPORTS);
   ```
   (IDs are safe: the CORE clone's autoincrement continued above legacy's max —
   verified in rehearsal 2026-06-10. Same pattern per affected table; for
   UPDATEs to pre-existing rows compare `UPDATED_AT` if present.)
3. Diagnose, fix, pick a new window. Disaster fallback:
   `RECRUITMENT_TEST_PRE_PHASE3` (full database clone from step 2) has the
   complete pre-cutover state.

## After the dust settles

- Watch for a few days: app error logs, `QUERY_HISTORY` for any unexpected
  writes still hitting the legacy 7 tables (would indicate an untemplated
  write path — none known; bootstrap DDL intentionally still points at the
  connection default).
- Add the 7 tables to the legacy quiet-period watchlist (master plan §3.4).
- Phase 4 (recommendations) repeats this pattern with
  `PLAYER_RECOMMENDATIONS` (+ agent tables); Phase 5 (admin) finishes with
  `USERS` + the rest.
