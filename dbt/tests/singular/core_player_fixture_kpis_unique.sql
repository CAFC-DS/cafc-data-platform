/*
  Composite-uniqueness check for core_player_fixture_kpis.
  Fails if any (cafc_player_id, cafc_fixture_id, cafc_kpi_id) tuple appears
  more than once. The incremental MERGE strategy should prevent this, but
  the test runs anyway as a backstop.
*/

select
    cafc_player_id,
    cafc_fixture_id,
    cafc_kpi_id,
    count(*) as n
from {{ ref('core_player_fixture_kpis') }}
group by 1, 2, 3
having count(*) > 1
