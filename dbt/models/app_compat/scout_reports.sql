/*
  app_compat.scout_reports
  -------------------------
  Legacy-shape scout reports for the recruitment app. App-owned data: as of
  the Phase 3 cutover the app writes CAFC_DB.CORE.SCOUT_REPORTS directly
  (moved from RECRUITMENT_TEST.PUBLIC via
  snowflake/ddl/20260610_phase3_move_scout_lists_to_core.sql — runbook in
  docs/runbooks/phase-3-cutover.md). The table carries PLAYER_ID (legacy) +
  CAFC_PLAYER_ID (canonical), so this stays a thin passthrough — no identity
  join needed. The view itself is retired at end-state when the app reads
  CORE natively (docs/decisions/0001).
*/

select * from {{ source('core_app', 'SCOUT_REPORTS') }}
