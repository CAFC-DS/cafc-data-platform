/*
  core_player_iteration_kpis
  --------------------------
  Per-(player, season, squad, kpi) season averages from IMPECT.

  This is different from core_player_season_kpis: that one aggregates
  match-level data we collected (championship-only). This one uses IMPECT's
  pre-computed season aggregates across all iterations we have access to.
  The two can coexist; the IMPECT-aggregated values are the more comprehensive
  source for cross-league recruitment work.

  Grain notes:
    - One row per (cafc_player_id, cafc_season_id, cafc_squad_id, impect_kpi_id).
    - cafc_squad_id is in the grain because a player can play for multiple
      squads in a single season (mid-season transfers).
    - position_code is preserved as a descriptor of where the player played;
      the same (player, season, squad) might have one row per kpi but each
      with the same position from the IMPECT response.

  Identity resolution:
    - cafc_player_id via core_player_id_resolutions (honours overrides).
    - cafc_squad_id via core_squads.
    - cafc_season_id via core_seasons.
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__iteration_player_kpis') }}
),

players as (
    select source_player_id, cafc_player_id
    from {{ ref('core_player_id_resolutions') }}
    where source_system = 'IMPECT'
),

squads as (
    select cafc_squad_id, impect_squad_id from {{ ref('core_squads') }}
),

seasons as (
    select cafc_season_id, impect_iteration_id from {{ ref('core_seasons') }}
)

select
    pl.cafc_player_id,
    se.cafc_season_id,
    sq.cafc_squad_id,
    stg.impect_kpi_id           as cafc_kpi_id,    -- placeholder until kpi_definitions seed lands
    stg.kpi_value,
    stg.position_code,
    stg.play_duration_seconds,
    stg.match_share,

    stg.impect_player_id        as source_player_id,
    stg.impect_squad_id         as source_squad_id,
    stg.impect_iteration_id     as source_iteration_id,
    'IMPECT'                    as source_system
from stg
join players pl on pl.source_player_id     = stg.impect_player_id::varchar
join squads  sq on sq.impect_squad_id      = stg.impect_squad_id
join seasons se on se.impect_iteration_id  = stg.impect_iteration_id
