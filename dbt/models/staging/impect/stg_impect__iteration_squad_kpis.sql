/*
  stg_impect__iteration_squad_kpis
  --------------------------------
  Passthrough over IMPECT_RAW.ITERATION_SQUAD_KPIS. Source is already flat
  (one row per (iteration, squad, kpi)) and snake_case from the loader, so
  staging just declares the contract and renames the IMPECT-side IDs.
*/

with source as (
    select * from {{ source('impect', 'ITERATION_SQUAD_KPIS') }}
)

select
    iteration_id           as impect_iteration_id,
    squad_id               as impect_squad_id,
    matches                as matches_played,
    kpi_id                 as impect_kpi_id,
    value                  as kpi_value
from source
