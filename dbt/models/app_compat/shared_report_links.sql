/*
  app_compat.shared_report_links
  Shareable scout-report links.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'SHARED_REPORT_LINKS') }}
