/*
  Season/iteration participation for event-derived player profiles. One row
  per canonical player and iteration; dominant position is based on summed
  position-spell duration, not event frequency.
*/

{{ config(materialized='table') }}

with position_rows as (
    select * from {{ ref('stg_impect__player_fixture_position_participation') }}
),

resolved as (
    select
        p.cafc_player_id,
        r.iteration_id as cafc_season_id,
        se.cafc_competition_id,
        r.impect_match_id,
        r.impect_squad_id,
        r.position_code,
        r.started,
        r.match_duration_seconds,
        r.play_duration_seconds
    from position_rows r
    join {{ ref('core_player_id_resolutions') }} p
      on p.source_system = 'IMPECT'
     and p.source_player_id = r.impect_player_id::varchar
    join {{ ref('core_seasons') }} se on se.impect_iteration_id = r.iteration_id
),

position_totals as (
    select
        cafc_player_id,
        cafc_season_id,
        cafc_competition_id,
        position_code,
        sum(play_duration_seconds) as position_duration_seconds
    from resolved
    group by 1,2,3,4
),

dominant_positions as (
    select *
    from position_totals
    qualify row_number() over (
        partition by cafc_player_id, cafc_season_id
        order by position_duration_seconds desc, position_code
    ) = 1
),

fixture_totals as (
    select
        cafc_player_id,
        cafc_season_id,
        cafc_competition_id,
        impect_match_id,
        max(started::integer) as started,
        sum(play_duration_seconds) as play_duration_seconds
    from resolved
    group by 1,2,3,4
),

totals as (
    select
        cafc_player_id,
        cafc_season_id,
        cafc_competition_id,
        count(*) as appearances,
        sum(started) as starts,
        sum(play_duration_seconds) as total_play_duration_seconds
    from fixture_totals
    group by 1,2,3
)

select
    t.cafc_player_id,
    t.cafc_season_id,
    t.cafc_competition_id,
    d.position_code as dominant_position_code,
    t.appearances,
    t.starts,
    t.total_play_duration_seconds,
    'IMPECT' as source_system
from totals t
join dominant_positions d
  on d.cafc_player_id = t.cafc_player_id
 and d.cafc_season_id = t.cafc_season_id
