/*
  stg_impect__squads
  ------------------
  IMPECT clubs and national teams. Driver of canonical CORE.SQUADS.
*/

with source as (
    select * from {{ source('impect', 'SQUADS') }}
),

renamed as (
    select
        id                  as impect_squad_id,
        name                as squad_name,
        countryid           as impect_country_id,
        type                as squad_type,
        gender,
        imageurl            as image_url,
        access              as has_access,
        iteration_id,
        wyscout_id,
        heim_spiel_id,
        skill_corner_id
    from source
)

select * from renamed
-- Women's squads excluded platform-wide (see var in dbt_project.yml). Keeps
-- the canonical squad dimension free of women's clubs/national teams.
{% if not var('include_womens_competitions', false) %}
where gender = 'MALE'
{% endif %}
