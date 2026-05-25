/*
  stg_impect__coaches
  -------------------
  IMPECT coach reference. Same shape pattern as PLAYERS.
*/

with source as (
    select * from {{ source('impect', 'COACHES') }}
),

renamed as (
    select
        id                          as impect_coach_id,
        name                        as full_name,
        firstname                   as first_name,
        lastname                    as last_name,
        shortname                   as short_name,
        try_to_date(birthdate)      as birth_date,
        gender,
        countryids                  as country_ids,   -- VARIANT array
        iteration_id,
        wyscout_id,
        heim_spiel_id,
        skill_corner_id
    from source
)

select * from renamed
