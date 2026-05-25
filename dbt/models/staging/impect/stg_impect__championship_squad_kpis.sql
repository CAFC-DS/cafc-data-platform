/*
  stg_impect__championship_squad_kpis
  -----------------------------------
  Per-match per-squad KPI values. Aggregated counterpart to the player KPIs.
*/

with source as (
    select * from {{ source('impect', 'CHAMPIONSHIP_SQUAD_KPIS') }}
),

renamed as (
    select
        competitionid                           as impect_competition_id,
        competitionname                         as competition_name,
        season,
        iterationid                             as iteration_id,
        matchid                                 as impect_match_id,
        try_to_timestamp_ntz(scheduleddate)     as scheduled_at,
        squadside                               as squad_side,        -- HOME / AWAY
        squadid                                 as impect_squad_id,
        kpiid                                   as impect_kpi_id,
        value                                   as kpi_value
    from source
)

select * from renamed
