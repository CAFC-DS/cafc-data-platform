/*
  app_compat.player_list_flags
  Shared favourite/decision markers on a player, keyed by UNIVERSAL_ID text.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'PLAYER_LIST_FLAGS') }}
