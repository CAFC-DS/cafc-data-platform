/*
  stg_impect__events
  -------------------
  Typed view over IMPECT_RAW.EVENTS, narrowed to the columns needed for recent
  player position and match-period timing. Playerless events are retained
  because the final event in a period can define its authoritative duration.
  This is not a full canonical event fact.
*/

with source as (
    select * from {{ source('impect', 'EVENTS') }}
)

select
    match_id                as impect_match_id,
    iteration_id,
    event_id,
    event_index,
    period_id,
    game_time,
    game_time_in_sec,
    player_id               as impect_player_id,
    player_position,
    player_position_side
from source
