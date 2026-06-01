/*
  app_compat.scout_assignment_players
  Players within a scout assignment.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SCOUT_ASSIGNMENT_PLAYERS') }}
