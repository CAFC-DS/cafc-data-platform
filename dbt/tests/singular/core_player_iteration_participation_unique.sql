select
    cafc_player_id,
    cafc_season_id,
    count(*) as row_count
from {{ ref('core_player_iteration_participation') }}
group by 1,2
having count(*) > 1

