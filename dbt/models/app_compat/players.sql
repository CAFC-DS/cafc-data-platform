/*
  app_compat.players
  ------------------
  Legacy-shape player view — the RECRUITMENT_TEST.PUBLIC.PLAYERS contract,
  sourced from the canonical layer. One row per canonical player.

  Materialized as a TABLE (not a view): keeps this app-facing contract fast
  to read regardless of how the context columns are derived. (Most
  app_compat models stay views; this one earns a table.)

  DATA_SOURCE (exclusive, matches legacy 'external'/'internal'):
    - external: player has an IMPECT identity → PLAYERID = its primary IMPECT id.
    - internal: no IMPECT identity (manually added) → PLAYERID = NULL.
    A manual player that gains an IMPECT identity becomes external automatically
    (IMPECT id takes priority); merge.py collapses duplicate canonical ids so
    everything points at the one CAFC_PLAYER_ID.

  Context columns (COMPETITIONNAME / SEASON / ITERATIONID / SQUADNAME) come
  from core_player_current_context (reference data — IMPECT_RAW.PLAYERS'
  most-recent iteration appearance). POSITION comes from
  core_player_recent_position (raw match events). Both replace an earlier
  version of this model that derived all of this from the 329M-row
  core_player_iteration_kpis analysis fact purely because that table
  happened to have player+squad+season already joined — a hidden dependency
  that would have broken this view the moment the KPI facts were retired in
  favour of event data. NULL for players with no in-scope data (out-of-
  scope/historical/internal, or — for POSITION specifically — players whose
  matches haven't had event data backfilled yet).

  15 columns, matching legacy column-for-column.
*/

{{ config(materialized='table') }}

with canonical as (
    select
        cafc_player_id, display_name, first_name, last_name,
        birth_date, birth_place, strong_foot, current_squad_id
    from {{ source('core', 'PLAYERS') }}
),

-- Primary IMPECT identity per canonical player; presence => external.
impect_identity as (
    select cafc_player_id, source_player_id
    from (
        select
            cafc_player_id,
            source_player_id,
            row_number() over (
                partition by cafc_player_id
                order by case when is_primary then 0 else 1 end,
                         match_confidence desc nulls last,
                         player_identity_id
            ) as rn
        from {{ source('core', 'PLAYER_IDENTITIES') }}
        where source_system = 'IMPECT'
    )
    where rn = 1
),

context as (
    select * from {{ ref('core_player_current_context') }}
),

position as (
    select * from {{ ref('core_player_recent_position') }}
)

select
    -- Legacy PLAYERS.PLAYERID is NUMBER; identities store source ids as text.
    try_to_number(ii.source_player_id)               as PLAYERID,
    c.display_name                                   as PLAYERNAME,
    c.first_name                                     as FIRSTNAME,
    c.last_name                                      as LASTNAME,
    c.birth_date                                     as BIRTHDATE,
    c.birth_place                                    as BIRTHPLACE,
    c.strong_foot                                    as LEG,
    ctx.competition_name                             as COMPETITIONNAME,
    ctx.squad_name                                    as SQUADNAME,
    pos.position_code                                as POSITION,
    ctx.cafc_season_id                               as ITERATIONID,
    ctx.competition_type                             as COMPETITIONTYPE,
    ctx.season_name                                  as SEASON,
    c.cafc_player_id                                 as CAFC_PLAYER_ID,
    case when ii.cafc_player_id is not null
         then 'external' else 'internal' end         as DATA_SOURCE
from canonical c
left join impect_identity ii   on ii.cafc_player_id   = c.cafc_player_id
left join context         ctx  on ctx.cafc_player_id  = c.cafc_player_id
left join position        pos  on pos.cafc_player_id  = c.cafc_player_id
