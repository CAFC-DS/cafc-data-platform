# app_compat

Legacy-shape views for the recruitment-platform app.

## Why this directory exists

The recruitment-platform backend reads from a bunch of denormalised tables in
`RECRUITMENT_TEST.PUBLIC` (`PLAYERS`, `MATCHES`, `SCOUT_REPORTS`, etc.).
Per the cutover plan (`cafc-data-platform-plan.md` Part 3), the app will switch
to reading from `CAFC_DB.APP_COMPAT.*` — same column names, sourced from
`CAFC_DB.CORE.*` underneath. This directory holds those views.

Each view's contract is **column shape, not row count.** The app expects a
specific set of column names + types; this layer guarantees them.

## Pattern

A typical `app_compat` view:

1. Selects from one or more `ref('stg_*')` (raw passthrough) or
   `ref('core_*')` (canonical) models.
2. Joins through `core_player_id_resolutions` /
   `core_fixture_id_resolutions` to honour live overrides.
3. Aliases columns with `as LEGACY_COLUMN_NAME` to match the legacy shape
   exactly (case-insensitive in Snowflake but kept uppercase for clarity).
4. Emits a constant `DATA_SOURCE` per source-system row, then `UNION ALL`s
   additional providers as they come online.

See `players.sql` and `matches.sql` for examples.

## Build status

| Legacy table                  | App_compat view | Status     | Notes                                            |
|-------------------------------|-----------------|------------|--------------------------------------------------|
| PLAYERS                       | `players.sql`   | ✅ Done    | Pattern. Row-count filter still TODO.            |
| MATCHES                       | `matches.sql`   | ✅ Done    | Pattern. Cross-provider IDs are NULL today.      |
| PLAYER_INFORMATION            |                 | ⏳ Pending | Read-only tier (plan §3.2 first cutover wave).   |
| POSITION_ATTRIBUTES           |                 | ⏳ Pending | Read-only tier.                                  |
| AGENT_PROFILES                |                 | ⏳ Pending | Read-only tier.                                  |
| PLAYER_NOTES                  |                 | ⏳ Pending | Notes/intel tier.                                |
| PLAYER_RECOMMENDATIONS        |                 | ⏳ Pending | Notes/intel tier.                                |
| PLAYER_STAGE_HISTORY          |                 | ⏳ Pending | Notes/intel tier.                                |
| STATUS_HISTORY                |                 | ⏳ Pending | Notes/intel tier.                                |
| SCOUT_REPORTS                 |                 | ⏳ Pending | Scout-reports tier — app writes; APP_ROLE allow-list. |
| SCOUT_REPORT_ATTRIBUTE_SCORES |                 | ⏳ Pending | Scout-reports tier.                              |
| SCOUT_REPORT_VIEWS            |                 | ⏳ Pending | Scout-reports tier.                              |
| PLAYER_LISTS                  |                 | ⏳ Pending | Lists tier.                                      |
| PLAYER_LIST_ITEMS             |                 | ⏳ Pending | Lists tier.                                      |
| SHARED_REPORT_LINKS           |                 | ⏳ Pending | Lists tier.                                      |
| SCOUT_ASSIGNMENTS             |                 | ⏳ Pending | Admin tier. Currently zero rows in legacy.       |
| SCOUT_ASSIGNMENT_AUDIT        |                 | ⏳ Pending | Admin tier. Currently zero rows.                 |
| SCOUT_ASSIGNMENT_PLAYERS      |                 | ⏳ Pending | Admin tier. Currently zero rows.                 |
| NOTIFICATIONS                 |                 | ⏳ Pending | Admin tier. Currently zero rows.                 |
| USERS                         |                 | ⏳ Pending | Identity tier — already in CORE.USERS.           |

## Per-table cutover checklist

For each new view:

1. `DESCRIBE TABLE RECRUITMENT_TEST.PUBLIC.<NAME>` in Snowflake to see the legacy column list.
2. Write `<name>.sql` projecting those exact columns, sourced from `CORE.*`.
3. Add an entry to `_models.yml` documenting the view + key columns.
4. `dbt parse && dbt build --select <name>` against your dev profile to confirm.
5. Visually diff a sample of rows against the legacy table to verify shape parity.
6. The recruitment-platform PR that points its read at this view goes through
   the test plan in `cafc-data-platform-plan.md` §3.3.
