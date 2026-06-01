# Part 3 — Recruitment-platform cutover to the canonical layer

**Status:** Draft · **Date:** 2026-06-01 · **Prereq:** Part 2 complete (canonical
layer live in prod `CAFC_DB.CORE`, identity matcher applied, `dbt test` green).

This is the *only* set of changes to the **recruitment-platform repo** (FastAPI +
React — a separate repo). It moves the live app off `RECRUITMENT_TEST.PUBLIC.*`
and onto the canonical platform: **reads from `CAFC_DB.APP_COMPAT`, writes to
`CAFC_DB.CORE`** — one feature at a time, each independently reversible.

It spans two repos:
- **cafc-data-platform** (this repo): finish the `APP_COMPAT` layer + reconcile
  the divergent views (the "Prerequisites" section).
- **recruitment-platform**: the env-var seam + per-feature reference switch (the
  "App changes" section).

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

## 1. Prerequisites (in cafc-data-platform — do BEFORE touching the app)

The app can't read `APP_COMPAT` for a feature until that feature's tables have
correct, complete views. As of 2026-06-01:

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

---

## 2. App changes (recruitment-platform repo)

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
