/*
  app_compat.player_list_items
  Shortlist members (already carries PLAYER_ID + CAFC_PLAYER_ID).
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).

  Explicit column list (not select *): Snowflake freezes a view's column count
  at creation, so a select-* view errors as soon as its base table gains a
  column. Listing the legacy columns lets CORE add normalisation columns
  (e.g. CANONICAL_PLAYER_ID) without breaking the app's reads.
*/

select
    id,
    list_id,
    player_id,
    cafc_player_id,
    display_order,
    notes,
    added_by,
    created_at,
    stage
from {{ source('core_app', 'PLAYER_LIST_ITEMS') }}
