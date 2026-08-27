with invalid_values as (
    select cafc_player_id, cafc_fixture_id, source_squad_id, position_code
    from {{ ref('core_player_fixture_position_participation') }}
    where play_duration_seconds <= 0
       or play_duration_seconds > match_duration_seconds
       or match_share <= 0
       or match_share > 1
),

duplicates as (
    select cafc_player_id, cafc_fixture_id, source_squad_id, position_code
    from {{ ref('core_player_fixture_position_participation') }}
    group by 1,2,3,4
    having count(*) > 1
)

select * from invalid_values
union all
select * from duplicates

