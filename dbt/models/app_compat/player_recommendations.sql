/*
  app_compat.player_recommendations
  Player recommendations workflow.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'PLAYER_RECOMMENDATIONS') }}
