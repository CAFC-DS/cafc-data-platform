# CAFC Data & Recruitment Platform — System Handbook

> The "read this in six months and understand everything" document.
> Evergreen description of the system. For the dated architecture critique and
> roadmap recommendations, see `architecture-review-2026-06.md`.
> Last verified against the live estate: 2026-06-10.

---

## 1. What this system is

Charlton Athletic FC runs a **recruitment platform** (a web app used by ~50
scouts, managers and analysts) on top of a **football data platform** (pipelines
that ingest provider data into Snowflake and model it into canonical, club-owned
tables). Two codebases, one Snowflake account:

| Repo | Location | What it is |
|---|---|---|
| `NewRecruitmentPlatform` | `~/Desktop/CAFC/2025-26/Recruitment/Coding/NewRecruitmentPlatform` | The live app: FastAPI backend (`backend/main.py`, deployed on Railway) + React/TypeScript frontend. Scouts write reports, intel, lists, recommendations here. |
| `cafc-data-platform` | `~/Projects/cafc-data-platform` | The data pipeline: Python extractors (IMPECT API → Snowflake), an identity-resolution layer, and a dbt project that builds the canonical `CAFC_DB` layers. |

The boundary between them is **the Snowflake schema, not a code import**. The
app reads/writes tables; the platform builds tables. Neither imports the other.

**We are mid-migration** (the "canonical cutover"): the app historically used a
single mixed database, `RECRUITMENT_TEST.PUBLIC`. The data platform has built a
properly modelled home, `CAFC_DB`, and the app is being repointed feature by
feature. Section 7 describes the end state; `docs/part-3-recruitment-cutover.md`
and the recruitment repo's cutover branches track the live progress.

---

## 2. The Snowflake estate, database by database

### `CAFC_DB` — the canonical platform (the future; partially live)

| Schema | Built by | Purpose |
|---|---|---|
| `IMPECT_RAW` | Python extractors (`python/extract/impect/`) | Provider data landed verbatim from the IMPECT API. ~477M rows. Append-oriented; the replayable source of truth for everything IMPECT. |
| `IMPECT_RAW_STAGING` | dbt (`models/staging/impect/`) | Thin typed **views** over `IMPECT_RAW` — renames, casts, light cleanup. One view per raw table (`stg_impect__players`, `stg_impect__iteration_player_kpis`, …). |
| `CORE` | dbt (`models/canonical/`) + identity Python (`python/identity/`) | The canonical club-owned model. Dimensions (`core_squads`, `core_competitions`, `core_seasons`), ID-resolution bridges (`core_player_id_resolutions`, `core_fixture_id_resolutions`), KPI facts (`core_player_iteration_kpis` — the 329M-row workhorse — plus fixture/season/squad variants), and the identity tables (`PLAYERS`, `PLAYER_IDENTITIES`, `IDENTITY_OVERRIDES`, `PLAYER_IDENTITY_CANDIDATES`, `INGESTION_RUNS`). ~457M rows. |
| `APP_COMPAT` | dbt (`models/app_compat/`) | **Temporary** bridge views that re-present canonical data in the legacy table shapes the app expects (same columns as `RECRUITMENT_TEST.PUBLIC.*`, plus `CAFC_*` dual IDs and `DATA_SOURCE`). One model per legacy table. App-owned tables (scout reports, notes, lists…) are *live passthroughs* to `RECRUITMENT_TEST.PUBLIC` until their writes move in cutover Phase 3+. IMPECT-derived tables (`players`, `matches`) are rebuilt from `CORE`. Scaffolding only — dropped at end state (see ADR 0001). |
| `MANUAL` | hand loads | Small manually-maintained inputs (~474 rows). |
| `MIGRATION` | one-shot scripts | Historical legacy→canonical ID maps from the original migration. Read-only reference; no runtime callers at end state. |
| `*_DEV_HUMARJI`, `DBT_TEST__AUDIT*` | dbt dev/test targets | Developer working copies and test-failure audit tables. Safe to rebuild/drop; never read by the app. |

### `RECRUITMENT_TEST` — the legacy home (despite the name, this is PRODUCTION today)

One schema, `PUBLIC`, 20 tables, two distinct families:

- **App-owned transactional data** (the live system of record, written by the
  app every day): `SCOUT_REPORTS` (~8.5k), `SCOUT_REPORT_ATTRIBUTE_SCORES`
  (~45k), `SCOUT_REPORT_VIEWS`, `PLAYER_INFORMATION` (intel), `PLAYER_NOTES`,
  `PLAYER_LISTS`/`PLAYER_LIST_ITEMS`, `PLAYER_RECOMMENDATIONS`,
  `PLAYER_STAGE_HISTORY`, `STATUS_HISTORY`, `AGENT_PROFILES`,
  `SHARED_REPORT_LINKS`, `SCOUT_ASSIGNMENT*`, `NOTIFICATIONS`, `USERS` (~173
  app users + auth).
- **IMPECT-derived copies** from the pre-platform era: `PLAYERS` (~91.5k),
  `MATCHES` (~122k), `POSITION_ATTRIBUTES`. These are superseded by
  `CAFC_DB.CORE`/`APP_COMPAT` equivalents and go away at end state.

This database is retired at the end of the cutover (Section 7).

### `CAFC_TEST_ANALYSIS` — ad-hoc analysis workspace

14 tables (~2.7M rows) of IMPECT event/match data (`IMPECT_EVENTS_STAGING`,
`IMPECT_PASS_NETWORK`, per-90 aggregates, …) loaded by analysis scripts outside
the dbt project. Currently ungoverned — a parallel mini-pipeline. The
architecture review recommends folding anything load-bearing into the dbt
project and treating the rest as scratch.

### Warehouse

A single warehouse, `DEVELOPMENT_WH`, currently serves everything (app queries,
dbt builds, ad-hoc analysis). The review recommends splitting this.

---

## 3. How data flows, ingestion → reporting

```
IMPECT API
   │  python/extract/impect/load_*.py        (HTTP, retry, pagination)
   ▼
CAFC_DB.IMPECT_RAW                            (verbatim landing, replayable)
   │  dbt: models/staging/impect/             (typed views, renames)
   ▼
CAFC_DB.IMPECT_RAW_STAGING
   │  python/identity/matcher.py              (link/mint CAFC ids; ambiguity → queue)
   │  dbt: models/canonical/                  (dims, facts, resolutions)
   ▼
CAFC_DB.CORE                                  (canonical, club-owned, provider-agnostic)
   │  dbt: models/app_compat/                 (legacy-shape reshape, dual IDs)
   ▼
CAFC_DB.APP_COMPAT ──────────────► Recruitment app reads (per cutover phase)
                                            │
RECRUITMENT_TEST.PUBLIC ◄───────── Recruitment app writes (until Phase 3+ flips
   │        ▲                                 writes into CORE)
   │        └── APP_COMPAT passthrough views read app-owned tables live from here
   ▼
(Tableau / ad-hoc analysis — to be formalised into an ANALYTICS layer; see review)
```

The refresh is orchestrated by `python/orchestrator.py`:
open `INGESTION_RUNS` row → extract → `dbt build` staging → identity matcher →
`dbt build` dims/facts/app_compat → `dbt test` → mark run SUCCESS/FAILED.

---

## 4. The identity model (the heart of provider-agnosticism)

Every real-world person gets one club-owned **`CAFC_PLAYER_ID`** (Snowflake
sequence; append-only, never re-pointed). Provider ids are *attributes* of that
identity, never the key:

- `CORE.PLAYER_IDENTITIES (SOURCE_SYSTEM, EXTERNAL_ID → CAFC_PLAYER_ID)` — one
  row per provider identity. IMPECT today; Wyscout/StatsBomb/GPS later are just
  more rows.
- `CORE.IDENTITY_OVERRIDES` — human decisions that always win over the matcher.
- `CORE.PLAYER_IDENTITY_CANDIDATES` — the ambiguity queue. The matcher never
  guesses; uncertain matches wait for a human.
- Same pattern for fixtures/squads via the `*_id_resolutions` bridges.

The legacy `PLAYERID` column the app still uses is simply the IMPECT identity.
During transition the app runs **dual-ID** logic (`universal_id` =
`"internal_{cafc_id}"` / `"external_{playerid}"` + `DATA_SOURCE`), which the
`APP_COMPAT` views feed. At end state the app keys on `CAFC_PLAYER_ID` alone.

Three invariants (enforced in `python/identity/`):
1. `CAFC_PLAYER_ID` is append-only — merging duplicates is an explicit human
   operation (`merge.py`), never automatic.
2. The override table always wins.
3. Ambiguity is a non-result — queue it, don't guess.

---

## 5. The app and the cutover seam

`backend/main.py` (~16.5k lines) holds all SQL (~345 `cursor.execute` sites).
Role-based filtering happens in the backend (5 roles: Admin / Senior Manager /
Manager / Loan Manager / Scout — see the repo's `CLAUDE.md` for the exact
visibility rules).

The cutover seam (Phase 0) is three env vars read at startup:

```
CANONICAL_DB        default RECRUITMENT_TEST
PLATFORM_DB_SCHEMA  default PUBLIC          → READ_PREFIX  = db.schema for reads
CORE_DB_SCHEMA      default PUBLIC          → WRITE_PREFIX = db.schema for writes
```

Templated SQL says `FROM {read_table('players')}`. With defaults that resolves
to `RECRUITMENT_TEST.PUBLIC.players` (a no-op); flip the env vars
(`CANONICAL_DB=CAFC_DB PLATFORM_DB_SCHEMA=APP_COMPAT CORE_DB_SCHEMA=CORE`) and
the same code reads the canonical layer. Untemplated SQL keeps resolving via the
connection default — that's what makes per-feature cutover safe. **Rollback is
always: flip the env var back and restart.**

Cutover phases (each = one branch/PR in the recruitment repo):

| Phase | Scope | Branch |
|---|---|---|
| 0 | Seam only (no-op) | `feature/canonical-cutover-seam` ✅ built |
| 1+2 | Read-only: search/profile/analytics + notes/intel | `feature/canonical-cutover-phase-1-2-reads` ✅ built & verified |
| 3 | Scout reports + lists — **first writes into CORE**, app-owned tables physically move | not started |
| 4 | Recommendations | not started |
| 5 | Admin | not started |
| — | 30-day quiet period → retire `RECRUITMENT_TEST` | — |

**Verification harness:** `backend/tools/cutover_compare/` (in the recruitment
repo) logs into the app as all five roles, captures the key read endpoints under
legacy and flipped config, and diffs them. Run it after any compat-view change
and at every phase boundary. See its README.

---

## 6. How a new analyst should navigate this

1. **Read this handbook**, then ADR 0001 (`docs/decisions/0001-app-compat-strangler-fig.md`)
   — it explains *why* the bridge architecture exists and its end date.
2. **Want to know what a number means?** Trace it backwards: app endpoint →
   `backend/main.py` SQL → `APP_COMPAT` view (`dbt/models/app_compat/*.sql`) →
   `CORE` model (`dbt/models/canonical/`) → staging view → `IMPECT_RAW` table.
   `dbt docs generate && dbt docs serve` renders this lineage as a clickable graph.
3. **Want to add/inspect pipeline data?** Work in the dbt project with
   `--target dev` (builds into your own `*_DEV_*` schemas — never prod). The
   profile lives in `~/.dbt/profiles.yml` (see `dbt/profiles.yml.example`).
4. **Want app behaviour?** Run the backend locally (`python backend/main.py`,
   reads `backend/.env`) — but remember: today it points at **live** data;
   treat writes with care until the dev/prod split (see review) exists.
5. **Identity questions** ("why are there two of this player?") →
   `CORE.PLAYER_IDENTITY_CANDIDATES` queue and `python/identity/merge.py`.
6. **Never** key new work on `PLAYERID` — that's IMPECT's id. Use
   `CAFC_PLAYER_ID`.

---

## 7. End-state architecture (when all phases complete)

```
                    ┌─────────────────────────────┐
   providers ─────► │ CAFC_DB                     │
   (IMPECT, +next)  │   <PROVIDER>_RAW   landing  │
                    │   staging views    typing   │
                    │   CORE             canonical dims/facts/identities
                    │                    + app-owned tables (reports, lists…)
                    │   ANALYTICS        marts for Tableau / reporting
                    └────────┬────────────────────┘
                             │
              app reads CORE natively (CAFC ids), writes CORE
              Tableau reads ANALYTICS only
```

- `APP_COMPAT` views: **dropped** (time-boxed scaffolding, per ADR 0001).
- `RECRUITMENT_TEST`: renamed `…LEGACY`, then dropped after the quiet period.
- Legacy `PLAYERID` demoted to one row in `PLAYER_IDENTITIES`.
- New providers plug in as: new `<PROVIDER>_RAW` schema + staging models +
  identity rows. `CORE`'s shape does not change — that is the design's payoff.

---

## 8. Glossary

| Term | Meaning |
|---|---|
| **Canonical / CORE** | Club-owned model keyed on `CAFC_*` ids, independent of any provider. |
| **APP_COMPAT** | Temporary views presenting canonical data in legacy table shapes. |
| **Strangler fig** | Migration pattern: new system grows around the old, feature by feature, until the old is hollow and removed. |
| **Seam** | The env-var dial (`READ_PREFIX`/`WRITE_PREFIX`) in the app that picks which database SQL resolves to. |
| **Dual ID** | Carrying both legacy (`PLAYERID`) and canonical (`CAFC_PLAYER_ID`) on a row during transition. |
| **Iteration** | IMPECT's term for a competition-season data cycle. |
| **Identity resolution** | Linking provider ids to one `CAFC_PLAYER_ID` (match → link; no match → mint; ambiguous → queue). |
| **Quiet period** | 30 days of zero reads on a legacy object (per `QUERY_HISTORY`) before rename/drop. |
