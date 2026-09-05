/*
  app_compat.recommendation_notes_history
  Append-only history of notes left on a recommendation.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'RECOMMENDATION_NOTES_HISTORY') }}
