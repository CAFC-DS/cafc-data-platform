/*
  app_compat.players
  ------------------
  Legacy-shape player view — the RECRUITMENT_TEST.PUBLIC.PLAYERS contract,
  sourced from the canonical layer. One row per canonical player.

  Materialized as a TABLE (not a view): the context columns come from the most-
  recent row per player in the 329M-row core_player_iteration_kpis, which is too
  expensive to window on every app query. Building it once per refresh also
  makes this app-facing contract fast to read. (Most app_compat models stay
  views; this one earns a table.)

  DATA_SOURCE (exclusive, matches legacy 'external'/'internal'):
    - external: player has an IMPECT identity → PLAYERID = its primary IMPECT id.
    - internal: no IMPECT identity (manually added) → PLAYERID = NULL.
    A manual player that gains an IMPECT identity becomes external automatically
    (IMPECT id takes priority); merge.py collapses duplicate canonical ids so
    everything points at the one CAFC_PLAYER_ID.

  Context columns (COMPETITIONNAME / SEASON / ITERATIONID / SQUADNAME / POSITION)
  come from the player's MOST-RECENT iteration (highest source_iteration_id) in
  core_player_iteration_kpis. NULL for players with no in-scope iteration data
  (out-of-scope/historical/internal) — legacy never had those players at all.

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

-- Most-recent iteration appearance per player (squad, position, season) from the
-- canonical season-aggregate facts. Keyed on cafc_player_id — no identity join.
recent_iter as (
    select cafc_player_id, cafc_squad_id, position_code, cafc_season_id
    from (
        select
            cafc_player_id,
            cafc_squad_id,
            position_code,
            cafc_season_id,
            row_number() over (
                partition by cafc_player_id order by source_iteration_id desc
            ) as rn
        from {{ ref('core_player_iteration_kpis') }}
    )
    where rn = 1
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
    -- Legacy PLAYERS.PLAYERID is NUMBER; identities store source ids as text.
    try_to_number(ii.source_player_id)               as PLAYERID,
    c.display_name                                   as PLAYERNAME,
    c.first_name                                     as FIRSTNAME,
    c.last_name                                      as LASTNAME,
    c.birth_date                                     as BIRTHDATE,
    c.birth_place                                    as BIRTHPLACE,
    c.strong_foot                                    as LEG,
    comp.competition_name                            as COMPETITIONNAME,
    sq.squad_name                                    as SQUADNAME,
    ri.position_code                                 as POSITION,
    ri.cafc_season_id                                as ITERATIONID,
    comp.competition_type                            as COMPETITIONTYPE,
    se.season_name                                   as SEASON,
    c.cafc_player_id                                 as CAFC_PLAYER_ID,
    case when ii.cafc_player_id is not null
         then 'external' else 'internal' end         as DATA_SOURCE
from canonical c
left join impect_identity ii   on ii.cafc_player_id   = c.cafc_player_id
left join recent_iter     ri   on ri.cafc_player_id   = c.cafc_player_id
left join seasons         se   on se.cafc_season_id   = ri.cafc_season_id
left join competitions    comp on comp.cafc_competition_id = se.cafc_competition_id
left join squads          sq   on sq.cafc_squad_id    = coalesce(ri.cafc_squad_id, c.current_squad_id)
