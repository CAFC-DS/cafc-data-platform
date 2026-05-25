/*
  stg_impect__countries
  ---------------------
  IMPECT country reference. Stable, slow-changing.
*/

with source as (
    select * from {{ source('impect', 'COUNTRIES') }}
),

renamed as (
    select
        id          as impect_country_id,
        name        as country_name,
        isoname     as iso_name,
        isocode     as iso_code,
        fifaname    as fifa_name,
        fifacode    as fifa_code
    from source
)

select * from renamed
