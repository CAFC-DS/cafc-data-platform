/*
  app_compat.shared_report_links
  Shareable scout-report links.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SHARED_REPORT_LINKS') }}
