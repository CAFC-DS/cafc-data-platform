/*
  app_compat.player_list_items
  Shortlist members (already carries PLAYER_ID + CAFC_PLAYER_ID).
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'PLAYER_LIST_ITEMS') }}
