/*
  app_compat.scout_assignments
  Scout assignment headers.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SCOUT_ASSIGNMENTS') }}
