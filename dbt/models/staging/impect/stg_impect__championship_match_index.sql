/*
  stg_impect__championship_match_index
  ------------------------------------
  Header index of championship matches. 1:1 with stg_impect__championship_match_info.
  Lightweight: just IDs + scheduling.
*/

with source as (
    select * from {{ source('impect', 'CHAMPIONSHIP_MATCH_INDEX') }}
),

renamed as (
    select
        competitionid                                       as impect_competition_id,
        competitionname                                     as competition_name,
        season,
        iterationid                                         as iteration_id,
        matchid                                             as impect_match_id,
        try_to_timestamp_ntz(scheduleddate)                 as scheduled_at,
        try_to_timestamp_ntz(lastcalculationdate)           as last_calculation_at,
        homesquadid                                         as home_squad_id,
        awaysquadid                                         as away_squad_id,
        matchdayindex                                       as matchday_index
    from source
)

select * from renamed
