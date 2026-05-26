/*
  Composite-uniqueness check for core_player_season_kpis.
  Fails if any (cafc_player_id, cafc_season_id, cafc_kpi_id) tuple appears
  more than once.
*/

select
    cafc_player_id,
    cafc_season_id,
    cafc_kpi_id,
    count(*) as n
from {{ ref('core_player_season_kpis') }}
group by 1, 2, 3
having count(*) > 1
