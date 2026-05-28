/*
  stg_impect__iteration_player_kpis
  ---------------------------------
  Passthrough over IMPECT_RAW.ITERATION_PLAYER_KPIS — per-(iteration, squad,
  player, kpi) season averages. Already flat from the loader; staging just
  applies cafc-side naming.

  POSITION is the IMPECT position code (e.g. CENTRAL_DEFENDER, GOALKEEPER).
  PLAY_DURATION is total seconds; MATCH_SHARE is fraction of available
  match-time the player accumulated.
*/

with source as (
    select * from {{ source('impect', 'ITERATION_PLAYER_KPIS') }}
)

select
    iteration_id           as impect_iteration_id,
    squad_id               as impect_squad_id,
    player_id              as impect_player_id,
    position               as position_code,
    play_duration          as play_duration_seconds,
    match_share            as match_share,
    kpi_id                 as impect_kpi_id,
    value                  as kpi_value
from source
