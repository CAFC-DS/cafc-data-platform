/*
  Source-grain Impect participation. Unlike the canonical participation
  models, this intentionally does not require player or fixture identity
  resolution. It preserves every player × fixture × position spell that can
  be reconstructed from IMPECT_RAW.EVENTS/MATCH_INFO, so event-derived
  consumers can support all resolvable Impect competitions.
*/

{{ config(materialized='table') }}

select
    r.impect_match_id::number as source_fixture_id,
    r.impect_player_id::number as source_player_id,
    r.impect_squad_id::number as source_squad_id,
    r.iteration_id::number as impect_iteration_id,
    s.cafc_season_id,
    s.cafc_competition_id,
    r.position_code,
    r.started,
    r.first_entry_seconds,
    r.final_exit_seconds,
    r.match_duration_seconds,
    r.play_duration_seconds,
    r.match_share,
    'IMPECT' as source_system
from {{ ref('stg_impect__player_fixture_position_participation') }} r
join {{ ref('core_seasons') }} s
  on s.impect_iteration_id = r.iteration_id
where r.play_duration_seconds > 0
