/*
  app_compat.player_stage_history
  --------------------------------
  Recruitment-stage transitions per player. App-owned; legacy carries only
  PLAYER_ID (IMPECT id), so this view ADDS CAFC_PLAYER_ID via the canonical
  resolution. All legacy columns preserved. ~99.6% of PLAYER_IDs resolve; the
  rest (manual/stale players) get a NULL CAFC_PLAYER_ID. Bridged until cutover.
*/

select
    sh.*,
    r.cafc_player_id as CAFC_PLAYER_ID
from {{ source('core_app', 'PLAYER_STAGE_HISTORY') }} sh
left join {{ ref('core_player_id_resolutions') }} r
  on  r.source_system    = 'IMPECT'
  and r.source_player_id = sh.PLAYER_ID::varchar
