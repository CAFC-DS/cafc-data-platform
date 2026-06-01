/*
  stg_impect__players
  -------------------
  Typed view over IMPECT_RAW.PLAYERS. The matcher in plan §2.6 reads from
  this rather than the raw table so it gets snake_case + real DATE/NUMBER
  types regardless of how IMPECT shipped them in JSON.
*/

with source as (
    select * from {{ source('impect', 'PLAYERS') }}
),

renamed as (
    select
        id                                                  as impect_player_id,
        firstname                                           as first_name,
        lastname                                            as last_name,
        commonname                                          as common_name,
        try_to_date(birthdate)                              as birth_date,
        birthplace                                          as birth_place,
        leg                                                 as strong_foot,
        gender,
        countryids                                          as country_ids,        -- VARIANT array; canonical model unpacks
        cast(currentsquadid as number(38, 0))               as current_squad_id,
        iteration_id,
        wyscout_id,
        heim_spiel_id,
        skill_corner_id
    from source
)

select * from renamed
-- Women's players excluded platform-wide (see var in dbt_project.yml). Keeps
-- women's players out of identity resolution (core_player_id_resolutions) and
-- the app_compat.players view.
{% if not var('include_womens_competitions', false) %}
where gender = 'MALE'
{% endif %}
