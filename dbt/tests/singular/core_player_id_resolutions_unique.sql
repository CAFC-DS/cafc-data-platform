/*
  Grain backstop for core_player_id_resolutions.
  The bridge view MUST emit exactly one cafc_player_id per
  (source_system, source_player_id) — otherwise every fact model that joins
  through it fans out (this test was added after PLAYER_IDENTITIES' per-context
  rows blew core_player_iteration_kpis up ~7x). Fails if any pair repeats.
*/

select
    source_system,
    source_player_id,
    count(*) as n
from {{ ref('core_player_id_resolutions') }}
group by 1, 2
having count(*) > 1
