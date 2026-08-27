/*
  Player minutes by fixture and position, reconstructed from match starters
  and the complete position-transition timeline. Impect encodes each period
  in a 10,000-second bucket; preceding periods' actual durations are used so
  stoppage time is preserved exactly.
*/

with period_lengths as (
    select
        impect_match_id,
        period_id,
        greatest(max(game_time_in_sec) - ((period_id - 1) * 10000), 0) as period_duration_seconds
    from {{ ref('stg_impect__events') }}
    where period_id is not null and game_time_in_sec is not null
    group by impect_match_id, period_id
),

periods as (
    select
        impect_match_id,
        period_id,
        period_duration_seconds,
        coalesce(sum(period_duration_seconds) over (
            partition by impect_match_id order by period_id
            rows between unbounded preceding and 1 preceding
        ), 0) as period_start_seconds
    from period_lengths
),

match_durations as (
    select impect_match_id, sum(period_duration_seconds) as match_duration_seconds
    from period_lengths
    group by impect_match_id
),

match_sides as (
    select
        impect_match_id, iteration_id, 'HOME' as squad_side,
        home_squad_id as impect_squad_id,
        home_starting_positions as starting_positions,
        home_substitutions as substitutions
    from {{ ref('stg_impect__match_info') }}
    union all
    select
        impect_match_id, iteration_id, 'AWAY' as squad_side,
        away_squad_id as impect_squad_id,
        away_starting_positions as starting_positions,
        away_substitutions as substitutions
    from {{ ref('stg_impect__match_info') }}
),

starters as (
    select
        m.impect_match_id,
        m.iteration_id,
        m.squad_side,
        m.impect_squad_id,
        s.value:playerId::number as impect_player_id,
        0::float as state_time_seconds,
        s.value:position::varchar as state_position,
        s.value:positionSide::varchar as state_position_side,
        true as started,
        0 as state_priority
    from match_sides m,
         lateral flatten(input => m.starting_positions) s
    where s.value:playerId is not null
),

starter_players as (
    select distinct impect_match_id, impect_squad_id, impect_player_id
    from starters
),

raw_transitions as (
    select distinct
        m.impect_match_id,
        m.iteration_id,
        m.squad_side,
        m.impect_squad_id,
        s.value:playerId::number as impect_player_id,
        s.value:gameTime.gameTimeInSec::float as raw_time_seconds,
        s.index::number as transition_order,
        floor(s.value:gameTime.gameTimeInSec::float / 10000)::number + 1 as period_id,
        mod(s.value:gameTime.gameTimeInSec::float, 10000) as within_period_seconds,
        s.value:toPosition::varchar as state_position,
        s.value:positionSide::varchar as state_position_side
    from match_sides m,
         lateral flatten(input => m.substitutions) s
    where s.value:playerId is not null
      and s.value:gameTime.gameTimeInSec is not null
),

transitions as (
    select
        t.impect_match_id,
        t.iteration_id,
        t.squad_side,
        t.impect_squad_id,
        t.impect_player_id,
        p.period_start_seconds + t.within_period_seconds as state_time_seconds,
        t.state_position,
        t.state_position_side,
        false as started,
        1 as state_priority,
        t.transition_order as state_order
    from raw_transitions t
    join periods p
      on p.impect_match_id = t.impect_match_id
     and p.period_id = t.period_id
),

states_raw as (
    select starters.*, 0::number as state_order from starters
    union all
    select * from transitions
),

states_deduped as (
    select *
    from states_raw
    qualify row_number() over (
        partition by impect_match_id, impect_squad_id, impect_player_id, state_time_seconds
        order by state_priority desc, state_order desc, state_position
    ) = 1
),

spells as (
    select
        s.*,
        coalesce(
            lead(state_time_seconds) over (
                partition by impect_match_id, impect_squad_id, impect_player_id
                order by state_time_seconds, state_priority, state_order
            ),
            d.match_duration_seconds
        ) as spell_end_seconds,
        d.match_duration_seconds
    from states_deduped s
    join match_durations d using (impect_match_id)
),

valid_spells as (
    select
        impect_match_id,
        iteration_id,
        squad_side,
        impect_squad_id,
        impect_player_id,
        state_position as position_code,
        state_position_side as position_side,
        started,
        state_time_seconds as spell_start_seconds,
        spell_end_seconds,
        match_duration_seconds,
        spell_end_seconds - state_time_seconds as play_duration_seconds
    from spells
    where upper(coalesce(state_position, 'BANK')) <> 'BANK'
      and spell_end_seconds > state_time_seconds
),

aggregated as (
    select
        impect_match_id,
        iteration_id,
        squad_side,
        impect_squad_id,
        impect_player_id,
        position_code,
        position_side,
        min(spell_start_seconds) as first_entry_seconds,
        max(spell_end_seconds) as final_exit_seconds,
        max(match_duration_seconds) as match_duration_seconds,
        sum(play_duration_seconds) as play_duration_seconds
    from valid_spells
    group by 1,2,3,4,5,6,7
)

select
    a.impect_match_id,
    a.iteration_id,
    a.squad_side,
    a.impect_squad_id,
    a.impect_player_id,
    a.position_code,
    a.position_side,
    (sp.impect_player_id is not null) as started,
    a.first_entry_seconds,
    a.final_exit_seconds,
    a.match_duration_seconds,
    a.play_duration_seconds,
    a.play_duration_seconds / nullif(a.match_duration_seconds, 0) as match_share
from aggregated a
left join starter_players sp
  on sp.impect_match_id = a.impect_match_id
 and sp.impect_squad_id = a.impect_squad_id
 and sp.impect_player_id = a.impect_player_id
