/*
  KPI-independent player participation at fixture grain. One row per
  canonical player, fixture and squad; position_code is the position in
  which the player accumulated the most time in that fixture.
*/

{{ config(materialized='table') }}

with position_rows as (
    select * from {{ ref('stg_impect__player_fixture_position_participation') }}
),

players as (
    select source_player_id, cafc_player_id
    from {{ ref('core_player_id_resolutions') }}
    where source_system = 'IMPECT'
),

fixtures as (
    select source_fixture_id, cafc_fixture_id
    from {{ ref('core_fixture_id_resolutions') }}
    where source_system = 'IMPECT'
),

resolved as (
    select
        p.cafc_player_id,
        f.cafc_fixture_id,
        r.iteration_id as cafc_season_id,
        se.cafc_competition_id,
        sq.cafc_squad_id,
        r.impect_player_id,
        r.impect_match_id,
        r.impect_squad_id,
        r.position_code,
        r.started,
        r.first_entry_seconds,
        r.final_exit_seconds,
        r.match_duration_seconds,
        r.play_duration_seconds
    from position_rows r
    join players p on p.source_player_id = r.impect_player_id::varchar
    join fixtures f on f.source_fixture_id = r.impect_match_id::varchar
    join {{ ref('core_seasons') }} se on se.impect_iteration_id = r.iteration_id
    left join {{ ref('core_squads') }} sq on sq.impect_squad_id = r.impect_squad_id
),

position_totals as (
    select
        cafc_player_id, cafc_fixture_id, cafc_season_id, cafc_competition_id,
        cafc_squad_id, impect_player_id, impect_match_id, impect_squad_id,
        position_code,
        sum(play_duration_seconds) as position_duration_seconds
    from resolved
    group by 1,2,3,4,5,6,7,8,9
),

dominant_positions as (
    select *
    from position_totals
    qualify row_number() over (
        partition by cafc_player_id, cafc_fixture_id, impect_squad_id
        order by position_duration_seconds desc, position_code
    ) = 1
),

totals as (
    select
        cafc_player_id, cafc_fixture_id, cafc_season_id, cafc_competition_id,
        cafc_squad_id, impect_player_id, impect_match_id, impect_squad_id,
        boolor_agg(started) as started,
        min(first_entry_seconds) as first_entry_seconds,
        max(final_exit_seconds) as final_exit_seconds,
        max(match_duration_seconds) as match_duration_seconds,
        sum(play_duration_seconds) as play_duration_seconds
    from resolved
    group by 1,2,3,4,5,6,7,8
)

select
    t.cafc_player_id,
    t.cafc_fixture_id,
    t.cafc_season_id,
    t.cafc_competition_id,
    t.cafc_squad_id,
    d.position_code as dominant_position_code,
    t.started,
    t.first_entry_seconds,
    t.final_exit_seconds,
    t.match_duration_seconds,
    t.play_duration_seconds,
    t.play_duration_seconds / nullif(t.match_duration_seconds, 0) as match_share,
    'IMPECT' as source_system,
    t.impect_player_id::varchar as source_player_id,
    t.impect_match_id::varchar as source_fixture_id,
    t.impect_squad_id::varchar as source_squad_id
from totals t
join dominant_positions d
  on d.cafc_player_id = t.cafc_player_id
 and d.cafc_fixture_id = t.cafc_fixture_id
 and d.impect_squad_id = t.impect_squad_id

