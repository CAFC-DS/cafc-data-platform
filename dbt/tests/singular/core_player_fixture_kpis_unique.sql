/*
  Composite-uniqueness check for core_player_fixture_kpis.
  Grain is (player, fixture, kpi, squad, position): IMPECT logs each KPI once
  per position the player occupied in the match, so position_code and
  impect_squad_id are part of the natural key. Fails if that tuple repeats.
  The incremental MERGE keys on the same composite.
*/

select
    cafc_player_id,
    cafc_fixture_id,
    cafc_kpi_id,
    impect_squad_id,
    position_code,
    count(*) as n
from {{ ref('core_player_fixture_kpis') }}
group by 1, 2, 3, 4, 5
having count(*) > 1
