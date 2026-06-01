/*
  app_compat.scout_report_views
  ------------------------------
  Read-receipts (which user viewed which scout report when). App-owned legacy
  passthrough; bridged until the Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SCOUT_REPORT_VIEWS') }}
