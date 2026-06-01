/*
  app_compat.scout_assignment_audit
  Scout assignment audit trail.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SCOUT_ASSIGNMENT_AUDIT') }}
