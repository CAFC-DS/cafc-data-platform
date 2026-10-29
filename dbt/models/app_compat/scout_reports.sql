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

  Explicit column list (not select *): Snowflake freezes a view's column count
  at creation, so a select-* view errors as soon as its base table gains a
  column. Listing the legacy columns lets CORE add normalisation columns
  (e.g. CANONICAL_PLAYER_ID) without breaking the app's reads.
*/

select
    id,
    player_id,
    position,
    build,
    height,
    strengths,
    weaknesses,
    summary,
    justification,
    attribute_score,
    performance_score,
    purpose,
    scouting_type,
    flag_category,
    report_type,
    created_at,
    match_id,
    formation,
    user_id,
    cafc_player_id,
    opposition_details,
    is_archived,
    is_potential,
    clip_category
from {{ source('core_app', 'SCOUT_REPORTS') }}
