/*
  app_compat.scout_report_attribute_scores
  -----------------------------------------
  Per-(scout report, attribute) scores. App-owned legacy passthrough; bridged
  until the Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'SCOUT_REPORT_ATTRIBUTE_SCORES') }}
