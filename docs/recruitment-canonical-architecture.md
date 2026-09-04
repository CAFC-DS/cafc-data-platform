# Recruitment Platform — Target Data Architecture

**Status:** design of record · **Date:** 2026-09-03 · supersedes the ad-hoc
"lift-and-shift `CAFC_DB.APP`" as the *end state* (that clone is now Phase 1's
fallback only — see §7).

Related: `docs/decisions/0001-app-compat-strangler-fig.md` (why strangler, not
big-bang), `docs/part-3-recruitment-cutover.md` (original per-feature plan),
`docs/runbooks/phase-3-cutover.md`, `docs/runbooks/phase-5-cutover.md`.

---

## 1. What's wrong with `RECRUITMENT_TEST.PUBLIC` today

The live app reads and writes 25 tables in `RECRUITMENT_TEST.PUBLIC`. The
problems the new database is meant to fix:

| Problem | Detail |
|---|---|
| **No canonical entity IDs** | Players carry IMPECT ids *or* app-minted ids in the same `PLAYERID` column; no single surrogate key. ~84 real players resolve to more than one id. Manual and external players collide. |
| **Can't link to technical data** | Scout reports / notes / lists key off legacy `PLAYER_ID`. The canonical KPI, score and event facts (~455M rows) key off `CAFC_PLAYER_ID`. There is no reliable join between the app's data and a player's analytical profile. |
| **App owns drifting copies** | `PLAYERS` (84k) and `MATCHES` (158k) are app-local snapshots that diverge from the IMPECT source and from each other. |
| **Team / squad ids inconsistent** | Squad refs are raw IMPECT squad ids mixed with `9000xxx` manual-squad ids; no canonical squad key. *(Out of scope this round — needs a squad matcher — see §8.)* |
| **Denormalised** | Competition / season / squad / position context is copied onto `PLAYERS` rows rather than derived from dimensions. |

## 2. Target: a three-layer model in `CAFC_DB`

```
                IMPECT_RAW  ─┐
                DVMS / OPTA ─┼─►  CORE           canonical entities + facts
                             │   (dbt + matcher)  CAFC_PLAYER_ID / CAFC_FIXTURE_ID
                             │        │
   app-owned transactional   │        ▼
   tables (scout reports,    │   APP_COMPAT       legacy-shape bridge (dbt views)
   notes, lists, users, …) ──┴──►    │            dual IDs, exact legacy columns
                                     ▼
                                THE APP            reads APP_COMPAT, writes CORE
```

### 2.1 `CAFC_DB.CORE` — canonical entities & facts

Owned by `python/identity/matcher.py` (entities) + dbt (`dbt/models/canonical/`,
facts). **The app never writes here until Phase 3+** (§6).

| Object | Grain | Source |
|---|---|---|
| `CORE.PLAYERS` | one row per canonical player, `CAFC_PLAYER_ID` from `CAFC_PLAYER_ID_SEQ` | matcher |
| `CORE.PLAYER_IDENTITIES` | `(SOURCE_SYSTEM, SOURCE_PLAYER_ID) → CAFC_PLAYER_ID` | matcher — `IMPECT`, `MANUAL` today; `OPTA`/`DVMS` later |
| `CORE.PLAYER_IDENTITY_OVERRIDES` | manual correction, highest precedence | human, via `20260525_identity_overrides.sql` |
| `CORE.PLAYER_IDENTITY_CANDIDATES` | ambiguous-match review queue | matcher |
| `CORE.FIXTURES` / `FIXTURE_IDENTITIES` | canonical fixtures, `CAFC_FIXTURE_ID` | matcher — **built, not app-facing this round** |
| `CORE.SQUADS` / `COMPETITIONS` / `SEASONS` | dimensions | dbt from IMPECT staging |
| `canonical.core_player_*` facts | player × fixture / iteration / season KPIs, scores, participation | dbt from `IMPECT_RAW` |
| `core_player_id_resolutions` | resolution *view* — overrides beat identities, collapsed to one link per pair | dbt |

Properties: gender-filtered (platform-wide), identity-resolved, `dbt test`
green, every `CAFC_PLAYER_ID` `unique` + `not_null`.

### 2.2 `CAFC_DB.APP_COMPAT` — the legacy-shape bridge

20 dbt models (`dbt/models/app_compat/`) that re-present CORE + app-owned data
in the **exact `RECRUITMENT_TEST.PUBLIC` column contract**, so the app sees no
schema change. Explicitly temporary scaffolding (ADR 0001) — retired at Phase 6.

- `players`, `matches`: canonical-backed, emit **dual IDs** — `CAFC_PLAYER_ID`
  *and* legacy `PLAYERID` *and* `DATA_SOURCE` (`external` = has IMPECT identity,
  `internal` = app-minted) — so the app's existing dual-ID filters keep working
  during transition. `players` is materialised as a table + a live-tail view
  union (`players_base.sql` + `players.sql`) so an app-minted player is visible
  before the next dbt run.
- app-owned tables (`scout_reports`, `player_notes`, `player_list_items`,
  `player_stage_history`, `player_information`, `player_recommendations`,
  `player_lists`, `agent_profiles`, `users`, …): thin passthrough **plus a
  `CAFC_PLAYER_ID` identity join** where the base table has only legacy
  `PLAYER_ID`. `scout_reports` and `player_list_items` already carry
  `CAFC_PLAYER_ID` at source, so those are pure passthrough.

### 2.3 App-owned transactional tables — home of record

The ~18 tables the app writes: `scout_reports`, `scout_report_attribute_scores`,
`scout_report_views`, `player_lists`, `player_list_items`, `player_notes`,
`player_stage_history`, `player_recommendations`, `recommendation_notes_history`,
`users`, `agent_profiles`, `shared_report_links`, `password_reset_tokens`,
`status_history`, `player_information`, `player_list_flags`, `position_attributes`
(+ 4 empty `scout_assignment*`).

**These are never rebuilt by dbt** (ADR 0001 — they are user-generated, not
derived). Their lifecycle is: *(a)* gain a `CAFC_PLAYER_ID` column (nullable,
backfilled through `core_player_id_resolutions`); *(b)* migrate home from
`RECRUITMENT_TEST.PUBLIC` into `CAFC_DB.CORE`, table by table, in Phases 3–5.
Until a table's home moves, the app keeps writing it in place and `APP_COMPAT`
reads it there.

## 3. How the app connects (end state)

Three env vars (already implemented on the app's `feature/canonical-cutover-*`
branches — 386 templated call sites, `WRITES_TO_CORE` gate):

| var | end-state value | meaning |
|---|---|---|
| `CANONICAL_DB` | `CAFC_DB` | the canonical database |
| `PLATFORM_DB_SCHEMA` | `APP_COMPAT` | where the app **reads** (`FROM {READ_PREFIX}.x`) |
| `CORE_DB_SCHEMA` | `CORE` | where the app **writes** (`INSERT/UPDATE {WRITE_PREFIX}.x`) |

- **Reads** → `CAFC_DB.APP_COMPAT.*` views.
- **Writes** → `CAFC_DB.CORE.*` tables (once that table's home has moved).
- **Add Player / Add Match** → mint `CAFC_PLAYER_ID_SEQ.NEXTVAL` → `INSERT
  CORE.PLAYERS` + `INSERT CORE.PLAYER_IDENTITIES` (`SOURCE_SYSTEM='MANUAL'`,
  `SOURCE_PLAYER_ID='app_<id>'`, `IS_PRIMARY=TRUE`). Free-text team names on Add
  Match are rejected — squads referenced by id.
- **Rollback for any feature** = point that feature's refs back at
  `RECRUITMENT_TEST.PUBLIC` (config flip).

## 4. Identity resolution

1. `matcher.py` links IMPECT players ↔ legacy app players ↔ future sources,
   writing `CORE.PLAYER_IDENTITIES`. Override rows in
   `CORE.PLAYER_IDENTITY_OVERRIDES` win at query time via
   `core_player_id_resolutions`.
2. Every app-owned table's `CAFC_PLAYER_ID` is derived by joining its legacy
   `PLAYER_ID` through `core_player_id_resolutions` on
   `source_system = 'RECRUITMENT_APP'` (or `'IMPECT'` where the legacy id is
   itself an IMPECT id).
3. **Unmatched legacy players** get a `CAFC_PLAYER_ID` minted as `MANUAL`-origin
   — no app row is left without a canonical id. The 2026-06-11 rehearsal found
   ~69 scout reports + 13 list items unresolved; 8 map via `MANUAL` identities,
   5 are broken in legacy too (deleted players) and are logged, not blocked.
4. ~84 players legitimately map to >1 canonical id — the resolution view applies
   a deterministic tiebreak (`IS_PRIMARY`, then `MATCH_CONFIDENCE`, then
   `UPDATED_AT`, then lowest id).

## 5. Player universe (decision 2026-09-03)

The app shows the **full canonical player universe** (~117k+), not just players
with prior activity (~84k legacy). Rationale: it's a recruitment tool; scouting
players not yet in the system is the point. The legacy set is offered as a
**saved default filter** ("players with activity") so day-to-day search results
stay familiar.

## 6. Delivery path — strangler, one reversible step at a time

| Phase | Change | App-side | Reversible by |
|---|---|---|---|
| **1** (this weekend) | Build `CORE` + `APP_COMPAT` in prod. App on a single canonical-identity schema: reads canonical shape, **writes still to app-owned tables** which now carry `CAFC_PLAYER_ID`. | env flip only | env flip back to legacy |
| **2** | Templated **read** cutover to `APP_COMPAT` views, per feature (search/profile → notes → scout+lists → recs → admin). | merge `phase-1-2-reads` (rebased) | per-feature env flip |
| **3** | Move `scout_reports` + `scout_report_*` + `player_lists*` + `position_attributes` + `shared_report_links` **home to `CORE`**; writes → `CORE`. | merge `phase-3-scout-lists` | copy stranded rows back (runbook) |
| **4** | Move remaining app tables (`player_notes`, `player_stage_history`, `player_information`, `player_recommendations`, `agent_profiles`, `status_history`, `users`) home to `CORE`. | merge `phase-4-remaining` | as Phase 3 |
| **5** | Add Player / Add Match mint into `CORE.PLAYERS` / `CORE.FIXTURES`; retire the app's `players`/`matches` copies and `/admin/setup-cafc-player-ids`. | merge `phase-5-player-match-writes` | flag `WRITES_TO_CORE=false` |
| **6** | App reads `CORE` **natively** (drop the legacy-shape need); drop `APP_COMPAT`; retire `RECRUITMENT_TEST` after two 30-day quiet periods. | native-shape refactor | — |

Phases 2–5 are built and were rehearsed end-to-end on 2026-06-12; they need
rebasing over ~212 commits of app `main` drift and re-rehearsing before merge.

## 7. Fallback

Phase 1 has a hard go/no-go gate (runbook). If `CORE` build, identity apply, or
app verification is not clean by the gate: **abort to unchanged
`RECRUITMENT_TEST.PUBLIC`** (env vars never set → redeploy is a no-op), bring
the app up on legacy, reschedule. `RECRUITMENT_TEST` is never mutated by any
phase, so this is always clean. The `CAFC_DB.APP` lift-and-shift clone remains
available as a manual same-shape fallback but is **not** the planned end state.

## 8. Out of scope / deferred

- **Fixture canonicalisation in the app** — `CORE.FIXTURES` is built, but the
  app keeps legacy match ids this round (decision 2026-09-03: player identity
  only). Folded in at Phase 5.
- **Squad / team id canonicalisation** — no squad matcher exists yet. Team refs
  stay IMPECT-native (same as legacy). Separate future project.
- **Manual players' POSITION / SQUADNAME** — no canonical home until manual-
  attribute or squad-identity work lands; `NULL` in `APP_COMPAT` meanwhile.
- **Tableau / analyst scripts** reading `RECRUITMENT_TEST` — repoint before the
  Phase 6 retirement.
- **`APP_COMPAT` retirement** — Phase 6, after native-shape reads.

## 9. Cleanup folded into Phase 1 follow-up (decision 2026-09-03)

Not done during the weekend window (keep the change surface minimal); tracked
here as immediate follow-up:

- Drop the 4 empty `SCOUT_ASSIGNMENT*` tables and
  `PLAYER_STAGE_HISTORY_CHANGED_AT_BKP` (stale backup) from the target.
- Normalise denormalised context columns on `players` to dimension joins
  (already the case in `app_compat.players` — verify no app code depends on the
  raw columns).
- Bring the 4 hand-built scout-report `APP_COMPAT` views fully under dbt with
  dual IDs + tests (ADR 0001 discipline item 2).
- Fix any remaining `_TEST`-suffixed object references.
