/*
  app_compat.player_notes
  ------------------------
  Free-text player notes. App-owned; legacy carries only PLAYER_ID (an IMPECT
  player id), so this view ADDS CAFC_PLAYER_ID via the canonical resolution
  (dual-id, like SCOUT_REPORTS / PLAYER_LIST_ITEMS already do). All legacy
  columns preserved verbatim. Bridged until the Part 3 cutover.
*/

select
    n.*,
    r.cafc_player_id as CAFC_PLAYER_ID
from {{ source('core_app', 'PLAYER_NOTES') }} n
left join {{ ref('core_player_id_resolutions') }} r
  on  r.source_system    = 'IMPECT'
  and r.source_player_id = n.PLAYER_ID::varchar
