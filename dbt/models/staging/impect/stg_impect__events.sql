/*
  stg_impect__events
  -------------------
  Typed view over IMPECT_RAW.EVENTS, narrowed to the columns needed to derive
  a player's most recent playing position without touching the KPI/score
  facts (see core_player_recent_position.sql). Not a full canonical event
  fact -- this platform deliberately isn't building one yet (raw per-provider
  storage is enough until bespoke analysis models are designed on top).
*/

with source as (
    select * from {{ source('impect', 'EVENTS') }}
)

select
    match_id                as impect_match_id,
    iteration_id,
    event_id,
    event_index,
    player_id               as impect_player_id,
    player_position,
    game_time_in_sec
from source
where player_id is not null
