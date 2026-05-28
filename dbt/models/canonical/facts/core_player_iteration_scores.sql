/*
  core_player_iteration_scores
  ----------------------------
  Per-(player, season, squad, playerScoreId) IMPECT standardized player
  scores. Same identity-resolution + grain pattern as
  core_player_iteration_kpis. The VALUE column is IMPECT's normalized score
  (typically 0-1 percentile-style); not recomputable from match-level data.

  This is the most directly-useful table for cross-league recruitment
  comparison — "show me every left-back across every league we have access
  to, ranked by their progressive-passing score this season."
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__iteration_player_scores') }}
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
    stg.impect_player_score_id,
    stg.score_value,
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
