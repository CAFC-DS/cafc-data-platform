/*
  core_player_current_context
  ----------------------------
  Per-player "what squad/competition/season are they in right now" lookup,
  sourced entirely from IMPECT_RAW.PLAYERS (a reference/dimension table --
  one row per player per iteration appearance) rather than the KPI/score
  facts.

  Why this exists: app_compat.players previously derived these same columns
  from core_player_iteration_kpis (a 329M-row analysis fact table), purely
  because that table happened to already have player+squad+season joined
  together -- not because squad/competition/season are KPI-derived facts.
  That created a hidden dependency: dropping the KPI facts (the whole point
  of the event-data migration) would have silently broken this view. This
  model answers the same question from PLAYERS.CURRENT_SQUAD_ID / ITERATION_ID
  instead, which is reference data and safe to keep once KPI facts retire.

  Grain: one row per cafc_player_id, keeping their most-recent iteration
  appearance (highest iteration_id). NULL squad/season/competition for
  players with no IMPECT PLAYERS row at all (manually-added/internal players).
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__players') }}
),

resolved as (
    select
        r.cafc_player_id,
        s.current_squad_id,
        s.iteration_id
    from stg s
    inner join {{ ref('core_player_id_resolutions') }} r
            on r.source_system    = 'IMPECT'
           and r.source_player_id = s.impect_player_id::varchar
),

most_recent as (
    select
        cafc_player_id,
        current_squad_id,
        iteration_id,
        row_number() over (
            partition by cafc_player_id order by iteration_id desc
        ) as rn
    from resolved
    where iteration_id is not null
),

seasons as (
    select cafc_season_id, cafc_competition_id, season_name from {{ ref('core_seasons') }}
),
competitions as (
    select cafc_competition_id, competition_name, competition_type from {{ ref('core_competitions') }}
),
squads as (
    select cafc_squad_id, squad_name from {{ ref('core_squads') }}
)

select
    mr.cafc_player_id,
    sq.cafc_squad_id,
    sq.squad_name,
    mr.iteration_id             as cafc_season_id,
    se.season_name,
    comp.cafc_competition_id,
    comp.competition_name,
    comp.competition_type
from most_recent mr
left join seasons      se   on se.cafc_season_id        = mr.iteration_id
left join competitions comp on comp.cafc_competition_id = se.cafc_competition_id
left join squads       sq   on sq.cafc_squad_id         = mr.current_squad_id
where mr.rn = 1
