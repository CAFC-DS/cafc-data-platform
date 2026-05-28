/*
  core_squad_iteration_scores
  ---------------------------
  Per-(squad, season, squadScoreId) IMPECT standardized squad scores.

  These are NOT recomputable from match-level data — IMPECT normalizes
  against their cross-league comparison population, and that comparison
  set is the value-add we can't reproduce. Treat the IMPECT VALUE as the
  source of truth.
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__iteration_squad_scores') }}
),

squads as (
    select cafc_squad_id, impect_squad_id from {{ ref('core_squads') }}
),

seasons as (
    select cafc_season_id, impect_iteration_id from {{ ref('core_seasons') }}
)

select
    sq.cafc_squad_id,
    se.cafc_season_id,
    stg.impect_squad_score_id,
    stg.score_value,
    stg.matches_played,
    stg.impect_squad_id         as source_squad_id,
    stg.impect_iteration_id     as source_iteration_id,
    'IMPECT'                    as source_system
from stg
join squads  sq on sq.impect_squad_id      = stg.impect_squad_id
join seasons se on se.impect_iteration_id  = stg.impect_iteration_id
