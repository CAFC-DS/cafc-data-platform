# Architecture Review — June 2026

> Dated critique and roadmap. Companion to `system-handbook.md` (which describes
> *what exists*; this describes *what to change*). Grounded in the live estate as
> inventoried on 2026-06-10.

---

## 1. Executive summary

The foundations are right and should not be rebuilt: canonical club-owned IDs,
provider data landed raw and modelled via dbt, a strangler-fig cutover with
env-var rollback, and verification tooling that compares legacy vs canonical
responses role-by-role. Phase 1+2 of the app cutover is built and verified.

The real gaps are **operational, not architectural**:

1. **There is no production/development separation where it matters most.** The
   live app's system of record is a database called `RECRUITMENT_TEST`, and a
   developer laptop connects to it with write access. The data platform *does*
   have dev targets (`*_DEV_HUMARJI` schemas) — the app has nothing equivalent.
2. **`cafc-data-platform` has no git remote.** The entire pipeline codebase
   exists on one laptop. This is the single biggest risk in the whole system
   and costs ten minutes to fix.
3. **Reporting is ungoverned.** There is no ANALYTICS layer; Tableau and ad-hoc
   work read whatever they were pointed at historically (including
   `CAFC_TEST_ANALYSIS`, a parallel mini-pipeline outside dbt). This is the
   thing most likely to break silently during the `RECRUITMENT_TEST` retirement.
4. **One warehouse serves everything** — app traffic, dbt builds, ad-hoc
   queries. No isolation, no cost attribution.
5. **Data-content decisions remain before the prod flip:** canonical player
   display names differ from legacy for many players (changes search behaviour),
   and both sides carry small numbers of duplicate `PLAYERID`s.

Recommended posture: **pause new cutover phases for roughly a week of
operational hardening** (remote + CI, ANALYTICS layer + Tableau inventory,
CAFC_DEV database, warehouse split, name-policy decision), then resume Phase 3
on much safer ground. Details and exact steps below.

---

## 2. Architecture review (what's good, what's questionable)

**Keep — these decisions are correct:**

- *Two repos with the schema as the seam.* App and pipeline have different
  lifecycles, permissions, reviewers. Correct split, correctly drawn.
- *Raw → staging → core → compat layering* with dbt owning all SQL transforms,
  Python owning extraction and identity. Industry-standard, cheap, testable.
- *Canonical `CAFC_*` ids minted by the club, provider ids as identity rows.*
  This is the provider-agnostic core and it already exists and works.
- *Strangler-fig cutover with env-var rollback,* verified per phase by the
  `cutover_compare` harness across all five roles. The Phase 1+2 verification
  caught a real type regression (`PLAYERID` string vs number) — proof the
  process works.
- *ADR discipline* (ADR 0001 explicitly time-boxes `APP_COMPAT`). Continue it.

**Question — these need a decision or correction:**

- *`APP_COMPAT.players` materialized as a table* is right for cost, but it means
  canonical reads go stale between dbt runs. Fine — but only once a scheduled
  refresh exists. Today refreshes are manual.
- *`CAFC_TEST_ANALYSIS`* duplicates IMPECT ingestion (events, pass networks,
  per-90s) outside dbt. Either promote what's load-bearing into
  `dbt/models/` or declare it scratch with a deletion date. An ungoverned
  parallel pipeline is how "two versions of the truth" starts.
- *`DBT_TEST__AUDIT*` schemas* (95–99 objects, 13M rows in dev) accumulate
  forever. Set `store_failures` selectively or schedule cleanup.
- *App dev hits production data.* Acceptable historically; not acceptable once
  Phase 3 makes the app write into `CORE`. Fix before Phase 3, not after.

---

## 3. Risks in the current plan (ranked)

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | `cafc-data-platform` has **no remote**; laptop loss = pipeline loss | Critical | `gh repo create cafc-data-platform --private` + push, today |
| 2 | Live app DB (`RECRUITMENT_TEST`) doubles as the dev database; local backends run with write access against production rows | High | CAFC_DEV clone + app dev profile (Section 5); until then, treat local writes as prod writes |
| 3 | Unknown Tableau / downstream readers of `RECRUITMENT_TEST` and `CAFC_TEST_ANALYSIS` will break at retirement | High | Inventory via `QUERY_HISTORY` (Section 7, step 0) before any retirement step |
| 4 | Canonical `display_name` ≠ legacy `PLAYERNAME` for many players ("A. Bytyqi" vs "Armir Bytyqi", inconsistently in both directions) → search results visibly change at flip | Medium-High | Decide name policy before prod flip (Section 11, action 2) |
| 5 | Duplicate `PLAYERID`s: 60 in legacy `PLAYERS`, 83 in `APP_COMPAT.PLAYERS` | Medium | Run the candidates queue + `merge.py`; add a dbt uniqueness test on `APP_COMPAT.players.PLAYERID` (warn-level) |
| 6 | `APP_COMPAT` becomes permanent (the classic compat-layer failure; ADR 0001 already flags it) | Medium | Time-box: schedule the "app reads CORE natively" milestone when Phase 5 merges |
| 7 | Single warehouse — a heavy dbt build can starve live app queries; no cost attribution | Medium | Warehouse split (Section 8) |
| 8 | No CI anywhere (dbt parse/test not enforced; app has no test gate) | Medium | GitHub Actions on the data-platform repo once it has a remote: `dbt build --target ci` on PR |
| 9 | Bus factor of one — no second person can run a refresh | Medium | The handbook (now written) + README runbooks; record a 15-min screen-capture of a refresh |
| 10 | Stale canonical reads (manual dbt runs) once prod reads flip | Low-Med | Nightly scheduled refresh before the prod flip |

---

## 4. Recommended target architecture

No structural rebuild — additions only (new pieces marked ★):

```
                        ┌──────────────────────────────────────────┐
 IMPECT API ──────────► │ CAFC_DB (PROD)                           │
 Wyscout / GPS / … ───► │   IMPECT_RAW, <PROVIDER>_RAW…   landing  │
                        │   staging views                  typing  │
                        │   CORE          canonical + app-owned    │
                        │   APP_COMPAT    (time-boxed bridge)      │
                        │   ANALYTICS ★   marts for BI/Tableau     │
                        └───────┬──────────────────────────────────┘
                                │ zero-copy clone, refreshed on demand ★
                        ┌───────▼──────────────────────────────────┐
                        │ CAFC_DEV ★  (same schema names as prod)  │
                        │   dbt --target dev builds here           │
                        │   app local/dev backend points here      │
                        └──────────────────────────────────────────┘

 Warehouses ★ : APP_WH (app serving) · TRANSFORM_WH (extract+dbt) · ANALYST_WH (Tableau/ad-hoc)
 Roles        : APP_ROLE (app) · DATA_PLATFORM_ROLE (pipeline writes)
                · ANALYST_ROLE ★ (read CORE+ANALYTICS) · TABLEAU_ROLE ★ (read ANALYTICS only)
```

Principles:
- **Prod/dev separation at the *database* level** (`CAFC_DB` vs `CAFC_DEV`),
  not schema suffixes. Same schema names inside both → code is identical, only
  the connection differs. (The current `*_DEV_HUMARJI` suffix approach works but
  pollutes the prod database and makes grants fiddly; migrate the dbt dev target
  to `CAFC_DEV` when convenient.)
- **BI reads ANALYTICS only.** Nothing downstream ever points at staging,
  RAW, or `APP_COMPAT`. `CORE` reads are for the app and analysts, not dashboards.
- **Sensitive future domains (medical, sports science) get their own schemas
  *and roles*** (`MEDICAL_RAW`/`CORE_MEDICAL` readable only by a `MEDICAL_ROLE`).
  Medical data is GDPR special-category; never co-mingle its grants with
  football-performance data.

---

## 5. DEV vs PROD strategy

### What exists in PROD (`CAFC_DB` + the app's Railway deployment)
- All `*_RAW`, staging, `CORE`, `APP_COMPAT`, `ANALYTICS` schemas — built only
  by scheduled/explicitly-promoted runs (`dbt --target prod`, run from `main`).
- The Railway backend with prod env vars (the seam flips happen *here* only).
- Production-ready means: model merged to `main`, dbt tests pass, and (for
  cutover work) `cutover_compare` is clean.

### What exists in DEV
- `CAFC_DEV` — a zero-copy clone of `CAFC_DB`, refreshed on demand
  (`CREATE OR REPLACE DATABASE CAFC_DEV CLONE CAFC_DB`). Free until divergence.
  - dbt `--target dev` builds into it (same schema names as prod).
  - The app's local backend points its *connection default* here, and the seam
    env vars give the same flexibility as prod.
  - Experimental scoring models, scratch tables, analyst sandboxes: schemas
    inside `CAFC_DEV` (e.g. `SANDBOX_<NAME>`), never in `CAFC_DB`.
- Interim reality (until Phase 3 moves app tables into `CORE`): the app-owned
  tables live in `RECRUITMENT_TEST.PUBLIC`, so a cloned `CAFC_DEV` alone can't
  serve full app dev. Clone `RECRUITMENT_TEST` too
  (`CREATE DATABASE RECRUITMENT_DEV CLONE RECRUITMENT_TEST`) and point local
  `SNOWFLAKE_DATABASE` at the clone for any work that writes. After Phase 5 this
  goes away — one clone covers everything.

### Promotion process (DEV → PROD), by change type

| Change | Path |
|---|---|
| New dbt model / table | branch → `dbt build --target dev` → PR (CI: `dbt build` + tests in an ephemeral CI schema) → merge → next prod run builds it |
| Schema change to an existing model | same, plus: check downstream refs (`dbt ls --select model+`), update `_models.yml` tests, note breaking changes in the PR |
| New calculated metric | metric defined in a dbt model or seed (`kpi_definitions`), never in a dashboard's custom SQL → same PR path |
| New scoring model | new versioned model (`scoring_model_v3.sql`) + weights seed row — *additive*, old versions untouched (Section 10) |
| New Tableau dashboard | build in Tableau **Dev project** against `CAFC_DEV.ANALYTICS` (or prod ANALYTICS read-only) → review → republish to Prod project pointing at `CAFC_DB.ANALYTICS` (Section 9) |
| New provider integration | Section 6 checklist; lands as RAW schema + staging + identity + tests, all through the normal PR path |
| App feature / cutover phase | recruitment-repo branch → local verify (incl. `cutover_compare` for cutover work) → PR → merge → Railway deploy → env-var flip is a *separate, reversible* prod action |

Releases: deploy app from `main` only; run dbt prod from `main` only; tag
data-platform releases when a cutover phase flips (`cutover/phase-3` etc.) so
DB state and code state can be correlated later.

---

## 6. Future provider integration framework

The pattern for any new source (Wyscout, StatsBomb, Opta, SkillCorner,
Transfermarkt, internal scouting, medical, sports science):

### Ingestion
1. **One raw schema per provider:** `CAFC_DB.WYSCOUT_RAW`, `…SKILLCORNER_RAW`.
   Land payloads verbatim (VARIANT for JSON is fine), append-only, with
   `LOADED_AT` + `INGESTION_RUN_ID` columns. Never transform on the way in —
   raw is the replay buffer.
2. **Pull APIs** → Python under `python/extract/<provider>/` (mirror the impect
   package). **File drops** (GPS exports) → Snowflake Task + `COPY INTO` from a
   stage (no Snowpipe — per-file billing).
3. **Validation at the gate:** dbt source freshness (warn 24h / error 48h) +
   schema tests on the staging views (not_null on ids, accepted_values on
   enums, row-count sanity singular tests). Failed `dbt test` fails the
   orchestrator run before anything reaches `CORE`.

### Standardisation (provider → club model)
- **IDs:** every provider entity passes through identity resolution —
  `PLAYER_IDENTITIES (SOURCE_SYSTEM='WYSCOUT', EXTERNAL_ID, → CAFC_PLAYER_ID)`,
  same for squads/competitions/fixtures via the `*_id_resolutions` bridges.
  Matching = normalised name + DOB (players) / name + date (fixtures), overrides
  win, ambiguity queues. **No provider id ever becomes a key in `CORE`.**
- **Metrics:** map provider metrics onto club-defined metrics in
  `seeds/kpi_definitions.csv` (`metric_key, provider, provider_field, transform`).
  "Pressing intensity" is a club concept; IMPECT's and SkillCorner's columns are
  *implementations* of it.
- **Events / physical data:** keep provider grain in provider-specific fact
  tables (`CORE.WYSCOUT_EVENTS` or staying in the provider schema), with
  canonical ids stamped on. Do **not** force a unified event model on day one —
  event taxonomies differ too much; unify per-use-case in ANALYTICS marts.

### Data model strategy (the direct answers)
- **Store provider-specific tables permanently?** Yes — in `*_RAW` (cheap,
  replayable, the audit trail). Staging views over them cost nothing.
- **Unified fact tables?** Yes, where the grain genuinely matches
  (player-match, player-season KPIs) — built from staging with a
  `SOURCE_SYSTEM` column and explicit precedence rules when two providers cover
  the same competition (decide and document per metric: e.g. "IMPECT wins for
  Championship; Wyscout fills gaps").
- **Canonical entities?** Already exist (`CORE.PLAYERS` etc.) — every new
  provider plugs into them via identities. This is the part you've already
  built correctly; don't let any integration shortcut around it.

New-provider checklist (copy into the PR description):
`RAW schema → extractor/Task → staging models + sources.yml + freshness →
identity rows + matcher coverage → conformed facts or provider facts →
kpi_definitions rows → dbt tests → docs page → (if app-facing) cutover_compare run`.

---

## 7. `RECRUITMENT_TEST` exit plan

**Readiness criteria (all must be true before retirement begins):**
- Phases 0–5 merged, prod env flipped, and `cutover_compare` clean at each step.
- App-owned tables physically moved to `CORE` (Phase 3–5 one-time copies) and
  app *writes* verified landing there.
- All BI/dashboards repointed (step 0 below).
- 30 consecutive days of zero non-admin queries against `RECRUITMENT_TEST`.

**Step-by-step:**
0. **Inventory readers first** (do this now, not at the end):
   ```sql
   SELECT user_name, query_text, count(*) n, max(start_time) last_seen
   FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
   WHERE start_time > dateadd(day,-30,current_timestamp())
     AND query_text ILIKE '%RECRUITMENT_TEST%'
     AND user_name NOT IN ('<app service user>')
   GROUP BY 1,2 ORDER BY n DESC;
   ```
   Every distinct reader (Tableau user, script, human) gets a migration owner.
   Repeat for `CAFC_TEST_ANALYSIS`.
1. **Snapshot:** `CREATE DATABASE RECRUITMENT_TEST_PRE_RETIRE CLONE RECRUITMENT_TEST;` (free).
2. **Migrate** (this is just cutover Phases 3–5): per app-owned table — final
   row copy into `CORE`, flip writes (`CORE_DB_SCHEMA=CORE`), repoint that
   table's `APP_COMPAT` view from legacy-passthrough to `CORE`, verify with
   `cutover_compare`, watch for a few days. **Do not migrate:** the IMPECT-era
   copies (`PLAYERS`, `MATCHES`, `POSITION_ATTRIBUTES`) — superseded by `CORE`;
   empty tables (`NOTIFICATIONS`, `SCOUT_ASSIGNMENT*` if still unused) — recreate
   shape in `CORE` only if the feature is alive.
3. **Repoint dashboards** to `CAFC_DB.ANALYTICS` marts (Section 9). Dashboards
   are the silent breakage vector because custom SQL with hardcoded
   `RECRUITMENT_TEST.PUBLIC` references fails only when someone opens the view.
   The step-0 inventory is your worklist; fix = republish data sources, not
   edit workbooks one by one.
4. **Quiet period:** 30 days; re-run the step-0 query weekly. Any hit resets
   the clock for that object.
5. **Soft retire:** `ALTER DATABASE RECRUITMENT_TEST RENAME TO RECRUITMENT_LEGACY;`
   revoke all write grants. Anything that breaks now identifies itself loudly
   while the data still exists. Wait 30 more days.
6. **Drop**, keeping the clone from step 1 for 90 days as the undo.

---

## 8. Recommended Snowflake structure

**Challenge to the question's premise:** `RECRUITMENT_DEV/PROD` and
`FOOTBALL_DEV/PROD` are both worse than what you already have. "Recruitment" is
one *application* on the platform (medical and sports-science data are coming —
they aren't "recruitment"), and `FOOTBALL_*` invents a new brand for no gain.
You already own the right name: **`CAFC_DB`**. Renaming the production database
mid-migration would invalidate every grant, dashboard, and connection string for
zero benefit. Keep it; add a dev sibling:

```
CAFC_DB        (production)
  IMPECT_RAW, WYSCOUT_RAW, GPS_RAW, …   raw layer        (one schema per provider)
  IMPECT_RAW_STAGING, …                 staging layer    (dbt views; future: STG_<provider>)
  CORE                                   curated layer    (canonical dims/facts/identities + app-owned)
  APP_COMPAT                             transition layer (time-boxed; dropped at end state)
  ANALYTICS                              analytics layer  (dbt marts; the ONLY BI surface)
CAFC_DEV       (zero-copy clone; same schema names; dbt dev target + app dev)
RECRUITMENT_TEST → RECRUITMENT_LEGACY → dropped   (Section 7)
CAFC_TEST_ANALYSIS → fold into dbt or declare scratch with a deletion date
```

Naming conventions: schemas `UPPER_SNAKE`; raw = `<PROVIDER>_RAW`; staging
models `stg_<provider>__<entity>`; canonical `core_<entity>` /
`core_<grain>_kpis`; marts `mart_<consumer>__<subject>` (e.g.
`mart_tableau__player_season_summary`); seeds for reference data; no `_TEST` /
`_V2_FINAL` objects in prod — versions are explicit models (Section 10), scratch
lives in `CAFC_DEV`.

Warehouses (all X-Small, auto-suspend 60s; resize only on evidence):
`APP_WH` (app service user), `TRANSFORM_WH` (extractors + dbt),
`ANALYST_WH` (Tableau + humans). The split is for blast-radius and per-workload
cost visibility, not performance.

---

## 9. Tableau deployment strategy

1. **Dashboards never query tables directly and never contain custom SQL with
   hardcoded database names.** They connect to **published data sources**, which
   point at `CAFC_DB.ANALYTICS` views. dbt owns those views; renames happen in
   one place.
2. **Two Tableau projects:** `Dev` (connects to `CAFC_DEV.ANALYTICS`, or prod
   ANALYTICS read-only where freshness matters) and `Production` (connects to
   `CAFC_DB.ANALYTICS` via `TABLEAU_ROLE`, which can read nothing else).
   Promotion = republish the workbook from Dev to Production; Tableau's
   built-in revision history is the rollback.
3. **One published data source per mart**, named after it
   (`Player Season Summary ← mart_tableau__player_season_summary`). Dashboard
   count can grow; data-source count stays small and audited.
4. During the cutover, the step-7.0 inventory tells you which existing
   workbooks hit `RECRUITMENT_TEST` — convert each to a published source over an
   ANALYTICS mart *before* the quiet period starts.

---

## 10. Version control & governance framework

**Code (the foundation):** everything that defines data — extractors, dbt
models, seeds, DDL — lives in git and reaches prod only via PR to `main`.
Step zero: **give `cafc-data-platform` a remote today.** Add CI (dbt parse +
build + test against a CI schema) the same day. The recruitment repo already
follows branch-per-feature; keep it.

**Tables / models (data versioning):** dbt makes tables *reproducible from
code* — that's the real versioning. Don't keep `_V2` table copies; keep the
model in git and let `dbt build` materialise it. For history-sensitive data use
dbt snapshots (SCD2) rather than ad-hoc backups. `INGESTION_RUNS` already gives
every refresh an auditable id — stamp it on facts where traceability matters.

**Scoring models & position profiles (the special case):** these change
*meaning*, not just shape, so they get explicit, additive versions:
- Weights/definitions live in seeds with a version column:
  `seeds/scoring_weights.csv` → `(model_version, position, metric_key, weight)`,
  `seeds/position_profiles.csv` → `(profile_version, position, attribute, target)`.
- Outputs carry the version: either one model per major version
  (`scoring_model_v1.sql`, `scoring_model_v2.sql`) or one model with a
  `MODEL_VERSION` column — prefer the latter once versions stabilise.
- **Published versions are immutable.** A change in philosophy = new version;
  v1 keeps producing until every consumer has moved. Dashboards pin a version
  explicitly (filter or dedicated mart view like
  `mart_tableau__scores_current` that the *platform* repoints when a version is
  blessed).
- Retire versions with a written sunset date, announced where scouts will see it.

**Dashboards:** versioned by the Dev→Production republish flow (Section 9) +
Tableau revision history.

**Governance rituals (lightweight, fit for a one-to-three-person team):**
- ADRs in `docs/decisions/` for anything you'd otherwise re-litigate (continue
  the 0001 pattern). One page, dated, decision + why.
- PR checklist: tests updated · `dbt build --target dev` clean · downstream
  models checked (`dbt ls --select model+`) · docs/handbook touched if the
  estate changed · (cutover work) `cutover_compare` clean.
- Quarterly 30-minute estate review: re-run the inventory queries, kill
  scratch objects, check `APP_COMPAT` is still on track to die.

---

## 11. Recommended next actions (before further cutover work)

In order; 1–2 are non-negotiable, 3–6 are one to two days of work total:

1. **Push `cafc-data-platform` to a private GitHub remote — today.** It
   currently exists on one laptop. (`~/.local/bin/gh auth login` then
   `gh repo create cafc-data-platform --private --source=. --push`.)
2. **Decide the player display-name policy.** Canonical `display_name` disagrees
   with legacy `PLAYERNAME` in both directions ("A. Bytyqi" vs "Armir Bytyqi");
   search ordering and on-screen names change at the flip. Either rebuild
   `CORE.PLAYERS.display_name` to prefer IMPECT `commonname` (match legacy), or
   consciously accept the change and note it in the Phase 1+2 PR. Also schedule
   a pass over the 83 duplicate `PLAYERID`s via the candidates queue +
   `merge.py`, and add a warn-level uniqueness test on `APP_COMPAT.players.PLAYERID`.
3. **Run the reader inventory** (Section 7 step 0) for `RECRUITMENT_TEST` *and*
   `CAFC_TEST_ANALYSIS` — it converts "how do we not break Tableau?" from fear
   into a finite list.
4. **Create `CAFC_DEV`** (clone), repoint the dbt dev target at it, and stop
   local app backends writing to live data (clone `RECRUITMENT_TEST` for any
   write-bearing dev until Phase 5).
5. **Create the `ANALYTICS` schema** with the first one or two marts and a
   `TABLEAU_ROLE`; begin repointing dashboards opportunistically.
6. **Split warehouses** (`APP_WH` / `TRANSFORM_WH` / `ANALYST_WH`) and schedule
   the nightly orchestrator refresh so canonical reads can't go stale.

Then resume the cutover at Phase 3 (scout reports + lists, first writes into
`CORE`) — on a platform that has backups, CI, a dev environment, and a known
set of downstream consumers.
