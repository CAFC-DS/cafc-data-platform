# Part 3 — Recruitment-platform cutover to the canonical layer

**Status:** Data-platform prerequisites COMPLETE (2026-06-01) · App-side work NOT
STARTED. The canonical `CAFC_DB.CORE` layer and the full `CAFC_DB.APP_COMPAT`
bridge are live in prod, dbt-managed and validated. The remaining work is all in
the **recruitment-platform repo** (FastAPI + React — a separate codebase).

This moves the live app off `RECRUITMENT_TEST.PUBLIC.*` onto the canonical
platform: **reads from `CAFC_DB.APP_COMPAT`, writes to `CAFC_DB.CORE`** — one
feature at a time, each independently reversible.

---

## ⚠️ Handoff — READ THIS FIRST if you're starting in the recruitment-platform repo

This document is a **roadmap, not a turnkey script.** The data side is done; the
app side needs a real planning pass *in that repo* before any code changes. Do
not paste-and-run it. Specifically:

**What is already done for you (data-platform side — no action needed):**
- `CAFC_DB.CORE`: canonical players (`CAFC_PLAYER_ID`), fixtures
  (`CAFC_FIXTURE_ID`), dimensions, and ~455M rows of KPI/score facts. Live,
  gender-filtered, identity-resolved, `dbt test` green.
- `CAFC_DB.APP_COMPAT`: **all 20 legacy-shape views exist in prod**, dbt-managed,
  each validated against the legacy table's columns/grain. `players`/`matches`
  carry the canonical surrogate IDs + legacy IDs + `DATA_SOURCE` (`external`/
  `internal`); `scout_reports`, `player_list_items`, `player_notes`,
  `player_stage_history`, `player_information` carry both `PLAYER_ID` and
  `CAFC_PLAYER_ID`.

**What is NOT done / NOT verified (your job in the app repo):**
1. **The line numbers in this doc are UNVERIFIED.** `backend/main.py:485-520`,
   `:90-107`, `:113-150` come from the original master plan's reading of the app
   and may be stale or wrong. **Find the real code; do not trust these.**
2. **How the app references tables is unknown.** The central step — "replace
   unqualified table refs with the templated form" — depends entirely on the
   app's pattern (raw SQL strings? an ORM? a query builder? a central db
   helper?). **Step 1 in the app repo is to find that seam**, not to change code.
3. **Write-cutover is under-specified.** Read-only features just repoint reads
   (easy). Read-write features (scout reports, lists, notes, recommendations)
   need the table's *home* moved into `CORE` first — create the table in `CORE`,
   migrate data, repoint the app's write path — which this doc only gestures at.
4. **Verification needs a running app.** The 5-role checks (§3) are hands-on; you
   need to run the app against a fixed user per role and diff results.

**Carry-over facts from Part 2 that affect the app:**
- **Squad IDs are NOT canonicalised** — `HOMESQUADID`/`AWAYSQUADID`/squad refs in
  `APP_COMPAT` are still IMPECT squad IDs (a squad matcher is future work). Same
  behaviour as legacy, so no app change needed, but don't expect a `CAFC_SQUAD_ID`.
- **`APP_COMPAT.PLAYERS` is broader than legacy** — ~117k canonical players vs the
  legacy ~91k (canonical includes players the legacy list didn't). Confirm the
  app's player search/list is happy showing the larger set, or add a filter.
- **`APP_COMPAT.*` is inert until you point the app at it** — promoting it changed
  nothing the app sees yet; the app still reads `RECRUITMENT_TEST.PUBLIC`.

**Day-1 checklist in the recruitment-platform repo (do in order):**
1. Grep the codebase for table references (`RECRUITMENT_TEST`, `PUBLIC.`, bare
   table names, ORM model `__tablename__`s / schema config). Write down *how*
   and *where* tables are referenced — this determines all downstream effort.
2. Confirm/replace the stale line numbers above with the real locations of the
   DB-connection/query layer and the dual-ID filters.
3. Build the **env-var seam** (§2 Phase 0): add the 3 env vars, route refs
   through them, **defaulting to `RECRUITMENT_TEST.PUBLIC`** so the deploy is a
   no-op. Ship and confirm nothing changed.
4. Cut over the **first read-only feature** (search/profile), verify all 5 roles
   (§3), flip the env var, watch, keep rollback ready.
5. Repeat per feature in the §2 order. Tackle write-cutover (table-home move to
   `CORE`) only when you reach those features.

---

## 0. The core mechanic

The app currently hard-codes / unqualifies table refs that resolve to
`RECRUITMENT_TEST.PUBLIC.*`. Introduce three env vars (master plan §3):

| env var | default | meaning |
| --- | --- | --- |
| `CANONICAL_DB` | `CAFC_DB` | the canonical database |
| `PLATFORM_DB_SCHEMA` | `APP_COMPAT` | where the app **reads** |
| `CORE_DB_SCHEMA` | `CORE` | where the app **writes** |

Reads become `{CANONICAL_DB}.{PLATFORM_DB_SCHEMA}.x`; writes become
`{CANONICAL_DB}.{CORE_DB_SCHEMA}.x`. **Rollback for any feature = point that
feature's refs back at `RECRUITMENT_TEST.PUBLIC`** (config flip, no redeploy if
the var is read at runtime).

### The read/write asymmetry (the crux of Part 3)

`APP_COMPAT` objects are **views** — you cannot `INSERT/UPDATE` through them. So
features split into two kinds, and they are NOT equal effort:

- **Read-only features** (search, profile, analytics, chatbot): just repoint
  reads to `APP_COMPAT`. Easy, low-risk, fully reversible. **Do these first.**
- **Read-write features** (scout reports, lists, notes, recommendations,
  assignments): the app writes these tables. Cutting them over means
  **migrating the table's home into `CORE`** (so writes have a real table to
  land in) *and* repointing reads to the `APP_COMPAT` view over it. This is the
  larger, later work.

A pragmatic intermediate for read-write tables: keep writing to
`RECRUITMENT_TEST.PUBLIC` while adding the `APP_COMPAT` read view, until the
table is ready to move to `CORE`. That decouples "read cutover" from "write
cutover" per table.

---

## 1. Prerequisites (in cafc-data-platform) — ✅ COMPLETE 2026-06-01

All done and promoted to prod. Kept below for the record / context; **no action
required here.**
- **§1b done** — `players` (table, 117,135) and `matches` (view, 145,710) rebuilt
  to the legacy contract: one row per player/fixture, `DATA_SOURCE`
  `external`/`internal` exclusive, full legacy column set, context from the
  most-recent iteration.
- **§1a done** — all 13 missing views built; every one matches its legacy row
  count; the 3 `PLAYER_ID`-only tables enriched with `CAFC_PLAYER_ID`.
- **`USERS` done** — repointed to the live `RECRUITMENT_TEST.PUBLIC.USERS` (160).
- **§1c done** — `dbt build --select tag:app_compat --target prod`: prod
  `APP_COMPAT` now has all 20 objects, dbt-managed.

<details><summary>Original prerequisite detail (for reference)</summary>

### 1a. Build the 13 missing `APP_COMPAT` views
Only 7 of 20 app tables have a view. Missing (with row counts / id columns):

| table | rows | player-id situation |
| --- | --- | --- |
| `PLAYER_LISTS` | 11 | user-scoped |
| `PLAYER_LIST_ITEMS` | 2,017 | has `PLAYER_ID` + `CAFC_PLAYER_ID` ✓ |
| `PLAYER_NOTES` | 1 | `PLAYER_ID` only → resolve canonical |
| `PLAYER_RECOMMENDATIONS` | 268 | no direct player col — check join shape |
| `PLAYER_STAGE_HISTORY` | 4,403 | `PLAYER_ID` only → resolve canonical |
| `PLAYER_INFORMATION` | 214 | `PLAYER_ID` only → resolve canonical |
| `AGENT_PROFILES` | 123 | user-scoped |
| `NOTIFICATIONS` | 0 | user-scoped |
| `STATUS_HISTORY` | 274 | — |
| `SHARED_REPORT_LINKS` | 18 | — |
| `SCOUT_ASSIGNMENTS` / `_AUDIT` / `_PLAYERS` | 0 / 0 / 0 | empty today |

Each becomes a dbt model in `dbt/models/app_compat/` (passthrough where the
source already has the needed shape; **identity-enriched** where it has only
legacy `PLAYER_ID` — add `CAFC_PLAYER_ID` via a join through
`core_player_id_resolutions` on the legacy source system). Add `DATA_SOURCE`
where the app's dual-ID filters expect it.

### 1b. Reconcile the divergent canonical views
These already exist twice and disagree — pick the authoritative definition
*before* the app reads them:
- `players`: dbt model = 273,273 rows vs prod hand-built = 234,230. Different
  population + the dbt version adds 6 columns. Decide the canonical row grain
  and column contract; make the dbt model authoritative; delete the hand-built one.
- `matches`: dbt (29 cols) vs hand-built (8). Same exercise.
- `users`: `APP_COMPAT.USERS`→`CORE.USERS` (51) vs live
  `RECRUITMENT_TEST.PUBLIC.USERS` (159). Decide the users' home (almost
  certainly the live 159 is authoritative → repoint, like scout_reports was).

### 1c. Promote the completed `APP_COMPAT` layer to prod
Once 1a/1b are done and dev-validated, build `tag:app_compat` to prod (drop the
`--skip-app-compat` flag). This is the point the deferred app_compat promotion
finally happens — coherently, all at once, with the divergences resolved.

</details>

---

## 2. App changes (recruitment-platform repo) — START HERE

> This is where the remaining work lives. Do the **Day-1 checklist** in the
> Handoff section above before changing any code.

### Phase 0 — the seam (one PR, zero behavior change)
- Add the 3 env vars (master plan cites `backend/main.py:485-520`).
- Replace unqualified table refs with the templated form, **defaulting to
  `RECRUITMENT_TEST.PUBLIC`** so the deploy is a no-op. This is pure
  refactoring: prove the seam works before flipping anything.
- Confirm the dual-ID filters (master plan cites `backend/main.py:90-107,
  113-150`) still key off `DATA_SOURCE` + the canonical/legacy id pair.

### Phases 1..N — per-feature cutover, lowest blast radius first
Order (from master plan §3), each its own PR:
1. **Read-only**: search / profile / analytics / chatbot → reads to `APP_COMPAT`.
2. **Notes / intel**.
3. **Scout reports + lists**.
4. **Recommendations**.
5. **Admin**.

For each feature:
1. Point its **reads** at `{PLATFORM_DB_SCHEMA}` (`APP_COMPAT`).
2. If it writes: either keep writes on legacy for now, or (when the table has
   moved to `CORE`) point **writes** at `{CORE_DB_SCHEMA}` (`CORE`).
3. **Verify against all 5 roles** (CLAUDE.md: Admin, Senior Manager, Manager,
   Loan Manager, Scout) — see §3.
4. Ship behind the env default; flip the var to cut over; **rollback = flip back**.

---

## 3. Role-verification matrix (every feature, before flip)

The app filters data by role and by the dual-ID (`DATA_SOURCE` + canonical/legacy
id). For each cutover feature, confirm identical results legacy-vs-`APP_COMPAT`
for each role:

| role | what to check |
| --- | --- |
| Admin | sees all; row counts match legacy exactly |
| Senior Manager | scope unchanged |
| Manager | scope unchanged |
| Loan Manager | scope unchanged |
| Scout | own-assignments scope unchanged; dual-ID filter still matches |

Method: run the same app query against legacy and `APP_COMPAT` for a fixed user
of each role; diff the result sets. Any delta blocks the flip.

---

## 4. Decommission `RECRUITMENT_TEST.PUBLIC` (master plan §3.4)

After a feature is cut over and stable:
1. Watch `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` for reads on
   `RECRUITMENT_TEST.PUBLIC` for **30 days**.
2. Zero reads → rename `RECRUITMENT_TEST.PUBLIC` → `RECRUITMENT_TEST.LEGACY`,
   revoke writes.
3. **Another 30 days** clean → drop. (Snapshot first; it's reversible until then.)

---

## 5. Risks / open decisions surfaced in Part 2

- **players/matches/users divergence** (§1b) — must be resolved before those
  feeds are trusted. This is the single biggest correctness risk.
- **Write cutover needs table-home migration to `CORE`** — bigger than the read
  cutover; sequence read-only features first to bank low-risk wins.
- **Identity enrichment for `PLAYER_ID`-only tables** (notes, stage_history,
  player_information) — depends on the legacy player-id → canonical resolution
  being correct (legacy source_system in `PLAYER_IDENTITIES` / `MIGRATION.*`).
- **Empty tables** (assignments, notifications) — trivial views now, but confirm
  the app's write path when they start being used.

---

## 6. Definition of done
- Every app feature reads `APP_COMPAT` (or `CORE`) — zero refs to
  `RECRUITMENT_TEST.PUBLIC` in the app.
- All 5 roles verified per feature.
- `RECRUITMENT_TEST.PUBLIC` dropped after the two 30-day quiet periods.
- `APP_COMPAT` is entirely dbt-managed; the strangler bridge can then be
  thinned (the app could read `CORE` natively where it no longer needs the
  legacy shape).
