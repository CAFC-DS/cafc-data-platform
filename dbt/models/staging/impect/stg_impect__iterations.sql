/*
  stg_impect__iterations
  ----------------------
  Typed view over IMPECT_RAW.ITERATIONS. Worth using as a pattern reference
  for the rest of the staging models because it shows the two main quirks
  in IMPECT's payload shape:
    1. Dot-named columns (COMPETITION.NAME etc.) come from JSON flattening
       and must be double-quoted in SQL.
    2. ISO timestamp strings live in VARCHAR and need TRY_TO_TIMESTAMP_NTZ.
*/

with source as (
    select * from {{ source('impect', 'ITERATIONS') }}
),

renamed as (
    select
        id                                                  as iteration_id,
        season,
        dataversion                                         as data_version,
        try_to_timestamp_ntz(lastchangetimestamp)           as last_change_at,

        -- Dot-named columns from JSON flattening must be quoted.
        "COMPETITION.ID"                                    as competition_id,
        "COMPETITION.NAME"                                  as competition_name,
        "COMPETITION.TYPE"                                  as competition_type,
        "COMPETITION.COUNTRYID"                             as competition_country_id,
        "COMPETITION.GENDER"                                as competition_gender,

        wyscout_id,
        heim_spiel_id,
        skill_corner_id
    from source
)

select * from renamed
-- Women's competitions excluded platform-wide (see var in dbt_project.yml).
-- This is the master gatekeeper: core_competitions and core_seasons derive
-- from this model, and the iteration facts inner-join core_seasons, so a
-- single filter here cascades to every downstream canonical + app_compat model.
{% if not var('include_womens_competitions', false) %}
where competition_gender = 'MALE'
{% endif %}
