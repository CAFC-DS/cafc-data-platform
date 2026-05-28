/*
  stg_impect__iteration_player_scores
  -----------------------------------
  Passthrough over IMPECT_RAW.ITERATION_PLAYER_SCORES — per-(iteration,
  squad, player, playerScore) standardized scores. The VALUE here is
  IMPECT's normalized score; same un-recomputable property as
  stg_impect__iteration_squad_scores.
*/

with source as (
    select * from {{ source('impect', 'ITERATION_PLAYER_SCORES') }}
)

select
    iteration_id           as impect_iteration_id,
    squad_id               as impect_squad_id,
    player_id              as impect_player_id,
    position               as position_code,
    play_duration          as play_duration_seconds,
    match_share            as match_share,
    player_score_id        as impect_player_score_id,
    value                  as score_value
from source
