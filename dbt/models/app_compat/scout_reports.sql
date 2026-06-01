/*
  app_compat.scout_reports
  -------------------------
  Legacy-shape scout reports for the recruitment app. App-owned data: the app
  writes RECRUITMENT_TEST.PUBLIC.SCOUT_REPORTS directly, and that table already
  carries PLAYER_ID (legacy) + CAFC_PLAYER_ID (canonical), so this is a thin
  passthrough — no extra identity join needed.

  Fixes the previous hand-built view, which selected from CORE.SCOUT_REPORTS_TEST
  (an §1.3 migration artifact that has since been dropped — the view was
  dangling). Points at the authoritative live table instead, consistent with
  the sibling scout views. Stays a passthrough until the Part 3 cutover moves
  this table's home (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SCOUT_REPORTS') }}
