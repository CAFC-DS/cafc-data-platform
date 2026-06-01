/*
  app_compat.player_information
  -----------------------------
  Supplementary per-player info captured in-app. App-owned; legacy carries only
  PLAYER_ID (IMPECT id), so this view ADDS CAFC_PLAYER_ID via the canonical
  resolution. All legacy columns preserved. Bridged until the Part 3 cutover.
*/

select
    pi.*,
    r.cafc_player_id as CAFC_PLAYER_ID
from {{ source('recruitment_legacy', 'PLAYER_INFORMATION') }} pi
left join {{ ref('core_player_id_resolutions') }} r
  on  r.source_system    = 'IMPECT'
  and r.source_player_id = pi.PLAYER_ID::varchar
