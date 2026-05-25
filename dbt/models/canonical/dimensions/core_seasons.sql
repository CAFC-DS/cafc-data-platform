/*
  core_seasons
  ------------
  Canonical season dimension. One row per (competition_id, season). An
  IMPECT "iteration" is exactly this granularity, so this is largely a
  passthrough of stg_impect__iterations with renamed columns.

  Identity:
    cafc_season_id = iteration_id (IMPECT is the only source today).
    cafc_competition_id matches core_competitions.cafc_competition_id.
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__iterations') }}
)

select
    iteration_id        as cafc_season_id,
    iteration_id        as impect_iteration_id,
    competition_id      as cafc_competition_id,
    season              as season_name,
    data_version,
    last_change_at,
    competition_name,
    competition_type,
    competition_country_id,
    competition_gender
from stg
