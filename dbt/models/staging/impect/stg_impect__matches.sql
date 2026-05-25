/*
  stg_impect__matches
  -------------------
  IMPECT fixtures across all iterations. MATCHDAY.* columns are dot-named
  from JSON flattening and must be double-quoted in SQL.
*/

with source as (
    select * from {{ source('impect', 'MATCHES') }}
),

renamed as (
    select
        id                                                  as impect_match_id,
        iterationid                                         as iteration_id,
        homesquadid                                         as home_squad_id,
        awaysquadid                                         as away_squad_id,
        try_to_timestamp_ntz(scheduleddate)                 as scheduled_at,
        try_to_timestamp_ntz(lastcalculationdate)           as last_calculation_at,
        available                                           as is_available,

        -- Dot-named columns from JSON flattening.
        "MATCHDAY.INDEX"                                    as matchday_index,
        "MATCHDAY.NAME"                                     as matchday_name,

        wyscout_id,
        heim_spiel_id,
        skill_corner_id
    from source
)

select * from renamed
