# CAFC_DB — the plain-English guide

**Read this first.** It explains what `CAFC_DB` is, how it is organised, and where
the migration stands, without the jargon. The other docs in this folder go deeper.

_Status as of 2026-09-30, checked against the `main` branch of the recruitment app
and this repo. Anything that could only be confirmed against live Snowflake or
Railway values is marked **(unverified)**._

---

## 1. What CAFC_DB is

`CAFC_DB` is Charlton's football database in Snowflake. It is not "the recruitment
app's database". It is the club's database, and the recruitment app is one client
of it, alongside Tableau, analysts and future data providers (GPS, Wyscout).

Its main job is to give every player and every match **one Charlton ID**
(`CAFC_PLAYER_ID`, `CAFC_FIXTURE_ID`). Provider IDs (IMPECT's `PLAYERID` and so on)
are just attributes of that identity. This lets a scout report and a player's
technical data join on the same key, which was impossible before.

## 2. How it is organised

Data moves left to right through four kinds of schema:

| Schema | What is in it | Written by | Read by |
|---|---|---|---|
| `IMPECT_RAW` (and later `DVMS_RAW`, `SKILLCORNER_RAW`, ...) | Provider data exactly as received. Never edited. | Python extractors | dbt only |
| `IMPECT_RAW_STAGING` | Typed, renamed views over the raw tables | dbt | dbt only |
| **`CORE`** | The real database. Canonical players, fixtures, squads, competitions, seasons, the ~455M-row KPI tables, and the recruitment app's own tables (scout reports, lists, notes, users, ...) | dbt + identity code (football data); the app (its own tables) | The app, analysts |
| `APP_COMPAT` | **Temporary** views that show `CORE` data in the old app table format. Being removed. | dbt | The recruitment app (for `players` and `matches` only, see section 4) |

Planned but not built: an `ANALYTICS` schema of clean, pre-joined tables for Tableau
and reporting.

Two rules keep it tidy:

1. **Raw data is never edited.** Everything downstream can be rebuilt from it.
2. **dbt builds football data; the app owns user data.** Scout reports, lists and
   notes are written by users, so dbt never rebuilds or overwrites them.

## 3. How the recruitment app connects

The app is a FastAPI backend (deployed on Railway, service
`cafc-recruitment-platform`) plus a React frontend (Vercel). All its SQL is in
`backend/main.py`, and it reaches tables through small helper functions:

| Helper | Points at | Used for |
|---|---|---|
| `core_table('x')` | `CAFC_DB.CORE.x` | The app's own tables (reads **and** writes) |
| `read_table('x')` | `CAFC_DB.APP_COMPAT.x` | `players` and `matches` only |
| `write_table('x')` | `CAFC_DB.CORE.x` | Writes |

The target database and schema come from three environment variables
(`CANONICAL_DB`, `PLATFORM_DB_SCHEMA`, `CORE_DB_SCHEMA`). If they are unset, the
code falls back to the **old** database, `RECRUITMENT_TEST.PUBLIC`.

> **Which Railway service is which** (checked 2026-09-30, project `cheerful-healing`):
>
> | Service | Deploys from | Seam vars set? | Meaning |
> |---|---|---|---|
> | `dependable-adventure` | branch `cutover/full` | Yes (`CANONICAL_DB`, `PLATFORM_DB_SCHEMA`, `CORE_DB_SCHEMA`; no `WRITE_DB`) | The app running on `CAFC_DB` |
> | `cafc-recruitment-platform` | branch `main` | No | The old app on `RECRUITMENT_TEST.PUBLIC` |
>
> Railway hides variable values, so the exact values are **(unverified)**; the
> startup log line "Canonical seam: READ_PREFIX=... WRITE_PREFIX=..." shows them.
> `cutover/full` is `main` plus a handful of feature fixes, so the code state in
> section 4 is the same on both.

## 4. Where the migration stands

The migration moves the app from the old `RECRUITMENT_TEST.PUBLIC` database onto
`CAFC_DB`. Status by piece:

| Piece | Status |
|---|---|
| `CORE` built: identities, dimensions, KPI facts | Done, dbt-tested |
| App-owned tables (18) read and write `CAFC_DB.CORE` directly through `core_table()` | Done in the app code |
| **`players`** (66 call sites) still read via `APP_COMPAT` | **Left to do** |
| **`matches`** (37 call sites) still read via `APP_COMPAT` | **Left to do** (the `core_fixture_details` model, its CORE-native replacement, is built here) |
| Drop `APP_COMPAT` | Blocked on the two rows above |
| Retire `RECRUITMENT_TEST` | After two 30-day quiet periods with zero reads |

The old docs described "phases 0 to 6". You can ignore the phase numbers. The
remaining work is: **switch the app's `players` and `matches` reads to `CORE`, then
delete `APP_COMPAT`.**

## 5. What "done" looks like

- The app reads and writes `CAFC_DB.CORE` and nothing else.
- `APP_COMPAT` is deleted.
- `RECRUITMENT_TEST` is renamed `..._LEGACY`, then dropped after the quiet periods.
- Tableau reads from `ANALYTICS`, not from `CORE` directly.
- Adding a new provider means a new `<PROVIDER>_RAW` schema, some staging models and
  identity rows. `CORE`'s shape does not change.

## 6. Where to look next

| Question | Read |
|---|---|
| Why a bridge and not a big-bang rebuild? | `decisions/0001-app-compat-strangler-fig.md` |
| Full description of every schema and flow | `system-handbook.md` |
| Identity rules (why two of the same player) | `system-handbook.md` section 4 |
| Cutover-day steps that were used | `runbooks/phase-3-cutover.md`, `runbooks/phase-5-cutover.md` |
| Using the raw event data | `raw-event-data-usage-guide.md` |
