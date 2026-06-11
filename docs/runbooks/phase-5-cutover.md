# Phase 5 — app-minted players & matches (canonical entity writes)

Companion to `phase-3-cutover.md`. Phase 5 is the last app change before full
cutover: the seven player/match write sites in the recruitment backend either
mint into CORE or are retired. App side lives on
`feature/canonical-cutover-phase-5-player-match-writes` in the app repo.

## What the app does post-cutover

Gated by the app's `WRITES_TO_CORE` flag (true when `WRITE_DB=CAFC_DB` and
`CORE_DB_SCHEMA=CORE`):

- **Add Player** (staff): `CAFC_PLAYER_ID_SEQ.NEXTVAL` → INSERT
  `CORE.PLAYERS` (DISPLAY/COMMON/FIRST/LAST name, BIRTH_DATE,
  CREATED_FROM_SOURCE='MANUAL') → INSERT `CORE.PLAYER_IDENTITIES`
  (SOURCE_SYSTEM='MANUAL', SOURCE_PLAYER_ID='app_<cafc_id>',
  SOURCE_CONTEXT='recruitment-app add-player', MATCH_CONFIDENCE=100,
  IS_PRIMARY=TRUE). Surfaces as `internal` / PLAYERID NULL via app_compat.
- **Add Match** (staff): same pattern with `CAFC_FIXTURE_ID_SEQ` /
  `CORE.FIXTURES` / `CORE.FIXTURE_IDENTITIES`. Squads are referenced **by id
  only** (must exist in CORE_SQUADS); free-text team names are rejected —
  names render from the squads dimension at read time.
- **Agent intake**: can no longer create players at all. Submissions must
  link an existing player; edits of pre-policy manual entries keep their
  stored link.
- Retired: the legacy `/admin/setup-cafc-player-ids` migration endpoint
  (410 Gone) and the app-repo `dedupe_players.py` (use `merge.py` +
  `candidates.py` here instead).

## Platform changes in this phase

1. **`app_compat.players` split** (`players_base.sql` + `players.sql`):
   the snapshot table keeps the expensive iteration-KPI context build;
   the app now reads a view = base UNION live tail of CORE.PLAYERS rows
   minted since the last dbt build. Without this, an app-minted player is
   invisible until the next dbt run. `app_compat.matches` was already a live
   view — no change.
2. **Grants**: `snowflake/ddl/20260611_phase5_app_entity_write_grants.sql`
   (INSERT on the four entity/identity tables + USAGE on both sequences to
   APP_ROLE). Run at cutover alongside the Phase 3 grants.

## Cutover-day order (delta to phase-3 runbook)

0. **Fresh IMPECT refresh first** (orchestrator) — soak testing 2026-06-11
   found canonical fixtures lag legacy (~400/mo missing Apr–May, max date
   2026-12-05 vs 2026-12-29) because refreshes are manual; 790/8443 scout
   reports lose their match link on stale data. Refresh, then re-check.
1. Run the Phase 3 clone script (its runbook, states A→C).
2. Run `20260611_phase5_app_entity_write_grants.sql`.
3. Run `20260611_phase5_remap_legacy_player_ids.sql` (dry-run blocks first)
   — repoints cloned app rows that reference legacy manual CAFC ids /
   superseded IMPECT ids to canonical ids. Found 2026-06-11: 13 list items
   + 69 scout reports unresolved canonically; 8 list items map via MANUAL
   identities, 5 are broken in legacy too (deleted players).
4. `dbt build --select app_compat` so `players_base` materialises and the
   `players` view is created before the app flips.
5. Flip the app env: drop `WRITE_DB`, set `CORE_DB_SCHEMA=CORE`
   (`CANONICAL_DB=CAFC_DB`, `PLATFORM_DB_SCHEMA=APP_COMPAT` as in Phase 3).

## Verification

- Add Player via API → row in CORE.PLAYERS + PLAYER_IDENTITIES (MANUAL),
  player immediately searchable in the app (live tail), universal id
  `internal_<cafc_id>`.
- Add Match via API with two squad ids → row in CORE.FIXTURES +
  FIXTURE_IDENTITIES, fixture immediately visible with proper team names.
- Add Match with a free-text team → 400.
- Agent recommendation without a typeahead selection → 400, no PLAYERS row.
- `dbt build` afterwards: new manual player migrates from tail to base with
  identical shape (run the app's cutover_compare before/after a build).

## Known gaps (logged in app REFACTOR_BACKLOG.md)

- Manual players' POSITION/SQUADNAME have no canonical home — NULL in
  app_compat until manual attributes or squad identity work lands.
- Legacy 9000xxx manual-squad ids on migrated MANUAL fixtures don't resolve
  in CORE_SQUADS → NULL team names on those historical fixtures.
