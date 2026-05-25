/*
  stg_impect__stadiums
  --------------------
  IMPECT stadium reference. Pitch dimensions are FLOAT.
*/

with source as (
    select * from {{ source('impect', 'STADIUMS') }}
),

renamed as (
    select
        id                  as impect_stadium_id,
        name                as stadium_name,
        address,
        timezone            as time_zone,
        pitchlength         as pitch_length_m,
        pitchwidth          as pitch_width_m,
        iteration_id,
        wyscout_id,
        heim_spiel_id,
        skill_corner_id
    from source
)

select * from renamed
