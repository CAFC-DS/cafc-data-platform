/*
  Grain backstop for core_fixture_id_resolutions.
  Must emit exactly one cafc_fixture_id per (source_system, source_fixture_id)
  so fact joins stay 1:1. Sibling of core_player_id_resolutions_unique.
*/

select
    source_system,
    source_fixture_id,
    count(*) as n
from {{ ref('core_fixture_id_resolutions') }}
group by 1, 2
having count(*) > 1
