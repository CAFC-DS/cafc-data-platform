/*
  KPI-independent player participation at fixture × position grain. This is
  the canonical denominator for event metrics grouped by PLAYER_POSITION.
  Position-side changes are deliberately collapsed into the position code.
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
)

select
    cafc_player_id,
    cafc_fixture_id,
    cafc_season_id,
    cafc_competition_id,
    cafc_squad_id,
    position_code,
    boolor_agg(started) as started,
    min(first_entry_seconds) as first_entry_seconds,
    max(final_exit_seconds) as final_exit_seconds,
    max(match_duration_seconds) as match_duration_seconds,
    sum(play_duration_seconds) as play_duration_seconds,
    sum(play_duration_seconds) / nullif(max(match_duration_seconds), 0) as match_share,
    'IMPECT' as source_system,
    impect_player_id::varchar as source_player_id,
    impect_match_id::varchar as source_fixture_id,
    impect_squad_id::varchar as source_squad_id
from resolved
group by
    cafc_player_id, cafc_fixture_id, cafc_season_id, cafc_competition_id,
    cafc_squad_id, position_code, impect_player_id, impect_match_id,
    impect_squad_id

