/*
  app_compat.players
  ------------------
  The app-facing legacy-shape player contract: players_base (the dbt-built
  snapshot, see players_base.sql) plus a LIVE TAIL of canonical players
  minted into CORE.PLAYERS since the last dbt build.

  Why: the recruitment app (Phase 5) mints manual players straight into
  CORE.PLAYERS and must see them immediately — players_base only refreshes on
  dbt runs. Between runs the only writers to CORE.PLAYERS are the app
  (CREATED_FROM_SOURCE='MANUAL') and merge.py; IMPECT players arrive via the
  orchestrator, which rebuilds players_base in the same run. So tail rows are
  manual by construction: PLAYERID NULL, DATA_SOURCE 'internal', context
  columns NULL (no iteration data exists for a just-minted manual player).

  The anti-join probes players_base on CAFC_PLAYER_ID only — cheap next to
  any real app query against this view.
*/

{{ config(materialized='view') }}

select * from {{ ref('players_base') }}

union all

select
    null::number(38,0)        as PLAYERID,
    c.display_name            as PLAYERNAME,
    c.first_name              as FIRSTNAME,
    c.last_name               as LASTNAME,
    c.birth_date              as BIRTHDATE,
    c.birth_place             as BIRTHPLACE,
    c.strong_foot             as LEG,
    null::varchar             as COMPETITIONNAME,
    null::varchar             as SQUADNAME,
    null::varchar             as POSITION,
    null::number(38,0)        as ITERATIONID,
    null::varchar             as COMPETITIONTYPE,
    null::varchar             as SEASON,
    c.cafc_player_id          as CAFC_PLAYER_ID,
    'internal'                as DATA_SOURCE
from {{ source('core', 'PLAYERS') }} c
where not exists (
    select 1
    from {{ ref('players_base') }} b
    where b.CAFC_PLAYER_ID = c.cafc_player_id
)
