/*
  app_compat.position_attributes
  -------------------------------
  Per-position attribute catalogue used by scout-report scoring. App-owned
  reference data; legacy passthrough until the Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'POSITION_ATTRIBUTES') }}
