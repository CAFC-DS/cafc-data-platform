/*
  stg_impect__iteration_squad_scores
  ----------------------------------
  Passthrough over IMPECT_RAW.ITERATION_SQUAD_SCORES.

  The VALUE column is IMPECT's standardized squad score for the given
  squad_score_id — typically a 0-1 percentile-style normalisation against
  their cross-league comparison population. Not recomputable from raw
  match-level data; treat as the source of truth.
*/

with source as (
    select * from {{ source('impect', 'ITERATION_SQUAD_SCORES') }}
)

select
    iteration_id           as impect_iteration_id,
    squad_id               as impect_squad_id,
    matches                as matches_played,
    squad_score_id         as impect_squad_score_id,
    value                  as score_value
from source
