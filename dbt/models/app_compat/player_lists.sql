/*
  app_compat.player_lists
  User-defined player shortlists.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'PLAYER_LISTS') }}
