# Duplicate-player handling — implementation plan

Status: **PROPOSED — nothing here has been applied.** Drafted 2026-09-30 from the
Arley Kay merge (IMPECT 228530 → 365441, CAFC 1320490 → 1487376).
Companion to `phase-5-cutover.md` (which retired the app-side `dedupe_players.py`
in favour of `merge.py` + `candidates.py`) and ADR 0001.

## Policy (default for every duplicate, any provider)

Duplicates are normal (transliteration, middle/legal names, free-agent
signings, name changes). What matters is how we resolve them:

1. **Check each entity for data unique to it** (attributes, identities, app
   rows, facts). Diff before merging.
2. **Pick the keeper.** Any id is fine if the rest is done properly; prefer the
   record with the correct attributes.
3. **Merge the data into the keeper** — attributes, identities, app-owned rows.
4. **Retire, never delete.** Duplicate keeps its row, gets `IS_ACTIVE=FALSE`
   plus a pointer to the keeper (`MERGED_INTO_CAFC_PLAYER_ID`) and a reason.
5. **Make it visible.** Every merge writes a row to a merge log that
   downstream consumers can read.

Hard rule: **no deletes.** Someone is relying on the old id existing.

## What was verified (read-only, 2026-09-30)

- `APP_COMPAT.PLAYERS_BASE` last built **2026-09-18**, before the merge. It is a
  `table`; the `IS_ACTIVE` filter (commit `50a6e92`) is already in
  `players_base.sql` / `players.sql`, so a rebuild drops 1320490. The row is
  stale, not mis-modelled.
- `CORE_PLAYER_ID_RESOLUTIONS` already maps both 228530 and 365441 → 1487376
  (`IDENTITY`); no overrides exist for either id. Rebuilding the facts
  reproduces the hand repoint (`source_player_id` stays raw).
- Survivor counts today: KPIS 1128, SCORES 245, FIXTURE_PARTICIPATION 2,
  POSITION_PARTICIPATION 3, ITERATION_PARTICIPATION 2 (each includes the
  survivor's own rows). 0 rows left under 1320490. No grain duplicates.
- **Risk:** all three IMPECT identity rows on 1487376 are `IS_PRIMARY=TRUE`,
  confidence 100; lowest `PLAYER_IDENTITY_ID` (96502) is source id 228530, so
  a rebuild emits the survivor as **PLAYERID 228530**.
- `CORE_PLAYER_CURRENT_CONTEXT` / `CORE_PLAYER_RECENT_POSITION` still hold a
  1320490 row (tables; cleared on rebuild).
- No references to 228530/365441/1320490/1487376 in `PLAYER_LIST_ITEMS`,
  `PLAYER_NOTES`, `PLAYER_INFORMATION`, `PLAYER_STAGE_HISTORY`. `SCOUT_REPORTS`
  has 2 rows on PLAYER_ID 365441 (CAFC_PLAYER_ID NULL — true of 11,570 / 11,674
  rows; pre-existing, not merge-related).
- Retired record holds nothing the survivor lacks except `COUNTRY_IDS='[]'`,
  `GENDER='MALE'`.

## Phases

Gates marked **[APPROVAL]** write to prod CORE / APP_COMPAT and need explicit
sign-off. Every CORE write is preceded by a zero-copy clone and logs rowcounts.

### Phase 0 — Fix the Arley Kay case

| # | Step | Writes? |
|---|---|---|
| 0.1 | Re-run the read-only checks above (identities, overrides, resolutions, fact counts) — abort if anything differs | no |
| 0.2 | `CREATE TABLE CORE.<T>_BAK_20260930 CLONE CORE.<T>` for `PLAYERS`, `PLAYER_IDENTITIES`, `PLAYER_IDENTITY_OVERRIDES`, `SCOUT_REPORTS` | **[APPROVAL]** |
| 0.3 | Insert override `('IMPECT','228530') → 1487376`, reason `merged from 1320490 …` (what merge.py step 3 would have done). Expect 1 row | **[APPROVAL]** |
| 0.4 | `UPDATE PLAYER_IDENTITIES SET IS_PRIMARY=FALSE, UPDATED_AT=now()` for ids 96502, 228709. Expect 2 rows | **[APPROVAL]** |
| 0.5 | After Phase 1: set `MERGED_INTO_CAFC_PLAYER_ID=1487376`, reason, `RETIRED_AT` on 1320490; insert merge-log row (with the 2026-09-29 hand-merge counts as the record). Expect 1 + 1 rows | **[APPROVAL]** |
| 0.6 | After Phase 2 is merged: `dbt build --select core_player_id_resolutions+ players_base+` (or narrower — see Rollout) | **[APPROVAL]** |
| 0.7 | Verify (acceptance below) | no |

### Phase 1 — Schema (`snowflake/ddl/20260930_player_retirement_and_merge_log.sql`)

- `ALTER TABLE CORE.PLAYERS ADD COLUMN MERGED_INTO_CAFC_PLAYER_ID NUMBER(38,0),
  RETIRED_AT TIMESTAMP_NTZ, RETIRED_REASON VARCHAR` (idempotent `IF NOT EXISTS`).
- `CREATE TABLE IF NOT EXISTS CORE.PLAYER_MERGE_LOG`: `MERGE_ID` autoincrement,
  `LOSER_CAFC_PLAYER_ID`, `WINNER_CAFC_PLAYER_ID`, `REASON`, `ACTOR`,
  `MERGED_AT`, `DRY_RUN`, `TABLE_COUNTS` (VARIANT: table → rows moved),
  `ATTRIBUTE_DIFF` (VARIANT), `BACKUP_SUFFIX`. Append-only.
- Grants consistent with `20260525_grants.sql`; expose read access for a
  changes feed.

### Phase 2 — dbt (`dbt/`)

- `players_base.sql` `impect_identity`: rank merged-in ids (override reason
  `merged from %` pointing at this CAFC id) **last**, then `is_primary`,
  `match_confidence`, `player_identity_id`. Keeps PLAYERID = survivor's own
  IMPECT id independent of tie-breaks.
- Expose the retirement pointer: add a `core_player_merges` view (or extend
  resolutions) over `PLAYERS` where `MERGED_INTO_CAFC_PLAYER_ID is not null`.
- Tests (singular, error severity): no inactive `CAFC_PLAYER_ID` in
  `players_base`; `players_base` PLAYERID unique per CAFC_PLAYER_ID; no
  `IS_ACTIVE=FALSE` player without a pointer; no fact row under a retired
  CAFC id; `APP_COMPAT.PLAYERS.PLAYERID` uniqueness (warn now — 5 legacy
  duplicates exist — error after cleanup).
- Docs: update `players_base.sql` header and `_models.yml`.

### Phase 3 — `python/identity/merge.py` (extend, don't fork)

Add, in one transaction, keeping the existing confirmation gate (no `--yes`):

1. `--dry-run` (default): attribute diff loser↔winner (flag fields only the
   loser has), per-table rowcounts, PLAYERID-conflict warning. Writes nothing.
2. Backup: `CLONE` the touched CORE tables with a dated suffix.
3. Repoint identities **and demote loser identities to `IS_PRIMARY=FALSE`**.
4. Repoint app tables carrying player ids (`SCOUT_REPORTS`, `PLAYER_LIST_ITEMS`;
   discover the list from `INFORMATION_SCHEMA.COLUMNS`, fail loudly on unknown
   tables). Set `UPDATED_AT` on every row touched.
5. Analytics facts: do **not** update by hand — they rebuild through
   resolutions. Instead print the exact `dbt build --select` command and
   assert overrides exist so the rebuild routes correctly.
6. Retire: `IS_ACTIVE=FALSE`, `MERGED_INTO_CAFC_PLAYER_ID`, `RETIRED_AT`,
   `RETIRED_REASON`. Never delete.
7. Insert override rows (existing behaviour) and a `PLAYER_MERGE_LOG` row.
8. Post-check: identities under loser = 0; overrides = moved identities.
- Unit tests in `tests/test_identity_merge.py` with a fake cursor (as
  `test_identity_matcher.py` does): dry-run writes nothing, no DELETE issued,
  rowcounts logged, refuses inactive winner / self-merge.

### Phase 4 — `python/identity/matcher.py`

- **Bug:** `fetch_existing_players` reads all of `CORE.PLAYERS`. Add
  `WHERE COALESCE(IS_ACTIVE, TRUE)` so a new IMPECT id can never
  `LINK_EXISTING` to a retired player (e.g. a profile matching 1320490's
  name + DOB 2007-11-28). Add a test.
- **Near-duplicate rule (new):** after exact name+DOB (unchanged auto-link),
  run a second pass for active players with the same normalized name and
  either a matching current squad (from `core_player_current_context`) with
  DOB missing/within tolerance, or a matching name with DOB differing by a
  small window. Result: **queue to `PLAYER_IDENTITY_CANDIDATES`** with
  `MATCH_REASON='name+club near-match'` and do not mint or link. This keeps
  invariant 3 (ambiguity is a non-result); a human resolves via `merge.py` /
  an override. Do not auto-link on name+club alone.
- A dbt/orchestrator report listing PENDING near-match candidates.

### Phase 5 — Visibility and policy

- ADR `docs/decisions/0002-duplicate-players-retire-not-delete.md` recording
  the five-step policy and the never-delete rule.
- Changes feed: `APP_COMPAT`/`CORE` view over `PLAYER_MERGE_LOG` (merged-from,
  merged-into, when, why) for the app and analysts; mention in
  `system-handbook.md` §merge and this runbook in the Phase-5 doc.
- Back-fill the 2026-09-29 Kay merge as the first log row; scan for other
  `IS_ACTIVE=FALSE` players without a log row / pointer (read-only report,
  fix separately).

## Rollout order

1. Phase 1 DDL (approval) → 2. Phase 0.2–0.4 (approval) → 3. Merge Phase 2 dbt
change → 4. Phase 0.5–0.6 rebuild (approval) → 5. Phases 3 and 4 code + tests
(PR) → 6. Phase 5. Phase 0 must finish before Phase 3 ships so the tool isn't
tested on an unresolved case.

Rebuild scope options: full `core_player_id_resolutions+ players_base+`
(includes the 329M-row KPI fact) **or** narrower: resolutions →
`core_player_current_context`, `core_player_recent_position` → `players_base`,
leaving facts as-is (they already resolve correctly). Decide at gate 0.6.

## Acceptance (Phase 0)

- `APP_COMPAT.PLAYERS`: exactly one Arley Kay — PLAYERID **365441**, CAFC
  **1487376**, BIRTHDATE **2008-02-15**; none for 228530 / 1320490.
- KPIS 1128, SCORES 245 (and participation counts above) still under 1487376.
- `PLAYERS_BASE` has no `IS_ACTIVE=FALSE` CAFC id.
- `SCOUT_REPORTS` (365441) still 2 rows; nothing under either old id in the
  other app tables.
- Merge-log row exists; clones exist; rowcounts recorded.

## Rollback

Each CORE write has a `_BAK_20260930` clone (restore with `CLONE` swap or
`INSERT … SELECT`); Time Travel is the second net. dbt tables rebuild from
CORE, so reverting the model and rebuilding restores prior output.
`players_base` can be rebuilt independently.

## Open questions

- Which rebuild scope at 0.6?
- Near-match DOB tolerance and squad source for Phase 4.
- Whether the app should read the merge log (redirect stale 228530 bookmarks
  to 365441) or only analysts.
