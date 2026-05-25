/*
  core_competitions
  -----------------
  Canonical competition dimension. One row per competition_id (e.g. EFL
  Championship, FA Cup), deduped across all seasons.

  Identity:
    cafc_competition_id = impect_competition_id today.

  Dedup:
    IMPECT_RAW.ITERATIONS has one row per (competition_id, season). We
    collapse to one row per competition_id, keeping the name and metadata
    from the most-recent season (assumption: name doesn't drift much).
*/

{{ config(materialized='table') }}

with stg as (
    select
        competition_id,
        competition_name,
        competition_type,
        competition_country_id,
        competition_gender,
        season,
        last_change_at
    from {{ ref('stg_impect__iterations') }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by competition_id
            order by last_change_at desc nulls last, season desc
        ) as rn
    from stg
),

deduped as (
    select * from ranked where rn = 1
)

select
    competition_id  as cafc_competition_id,
    competition_id  as impect_competition_id,
    competition_name,
    competition_type,
    competition_country_id,
    competition_gender
from deduped
