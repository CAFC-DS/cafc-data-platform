/*
  core_squad_iteration_kpis
  -------------------------
  Per-(squad, season, kpi) season averages from IMPECT. The squad-level
  equivalent of core_player_iteration_kpis.

  Materialized as a table (full refresh) — squad-level data across all 708
  iterations is small (~25M rows), so the simplicity of table beats
  incremental complexity. Switch to incremental if/when row count grows.

  Identity resolution:
    - cafc_squad_id comes from core_squads (which today equals impect_squad_id;
      will become a stable surrogate once squad identity resolution is built).
    - cafc_season_id comes from core_seasons.
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__iteration_squad_kpis') }}
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
    stg.impect_kpi_id           as cafc_kpi_id,  -- placeholder until kpi_definitions seed lands
    stg.kpi_value,
    stg.matches_played,
    stg.impect_squad_id         as source_squad_id,
    stg.impect_iteration_id     as source_iteration_id,
    'IMPECT'                    as source_system
from stg
join squads  sq on sq.impect_squad_id      = stg.impect_squad_id
join seasons se on se.impect_iteration_id  = stg.impect_iteration_id
