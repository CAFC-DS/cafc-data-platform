/*
  app_compat.players
  ------------------
  Legacy-shape player view for the recruitment-platform app.

  Projects the exact column names that RECRUITMENT_TEST.PUBLIC.PLAYERS
  currently exposes, sourced from CORE.* via staging instead of from the
  legacy denormalised landing. The app's existing SQL queries
  (backend/main.py) continue to work unchanged when its DB-search path
  flips from RECRUITMENT_TEST.PUBLIC.x to APP_COMPAT.x.

  Row shape (matches legacy):
    PLAYERID            IMPECT player ID (legacy "external" identifier)
    PLAYERNAME          common name
    FIRSTNAME / LASTNAME
    BIRTHDATE / BIRTHPLACE / LEG
    COMPETITIONNAME / SEASON / ITERATIONID / COMPETITIONTYPE
    SQUADNAME           current squad name
    POSITION            most-recent position; NULL if no KPI rows for player
    CAFC_PLAYER_ID      canonical surrogate (resolved through overrides)
    DATA_SOURCE         constant 'IMPECT' on this row; UNION ALL other
                        sources here when they come online.

  Identity resolution goes through core_player_id_resolutions so live
  overrides are honoured.

  Row count caveat:
    The legacy table has ~91k rows; this view exposes all IMPECT_RAW player
    rows (~417k) because the legacy table was filtered (championship-scope?
    last-active-iteration?). Add WHERE clauses to match the legacy filter
    once it's confirmed; the column shape is the contract, not the row count.
*/

with players as (
    select * from {{ ref('stg_impect__players') }}
),

squads as (
    select cafc_squad_id, squad_name from {{ ref('core_squads') }}
),

seasons as (
    select cafc_season_id, cafc_competition_id, season_name from {{ ref('core_seasons') }}
),

competitions as (
    select cafc_competition_id, competition_name, competition_type from {{ ref('core_competitions') }}
),

resolved as (
    select source_system, source_player_id, cafc_player_id
    from {{ ref('core_player_id_resolutions') }}
    where source_system = 'IMPECT'
),

-- Most recent position per player from KPI rows.
positions as (
    select
        cafc_player_id,
        position_code,
        row_number() over (partition by cafc_player_id order by scheduled_at desc) as rn
    from {{ ref('core_player_fixture_kpis') }}
    where position_code is not null
)

select
    p.impect_player_id                              as PLAYERID,
    p.common_name                                   as PLAYERNAME,
    p.first_name                                    as FIRSTNAME,
    p.last_name                                     as LASTNAME,
    p.birth_date                                    as BIRTHDATE,
    p.birth_place                                   as BIRTHPLACE,
    p.strong_foot                                   as LEG,
    c.competition_name                              as COMPETITIONNAME,
    sq.squad_name                                   as SQUADNAME,
    pos.position_code                               as POSITION,
    p.iteration_id                                  as ITERATIONID,
    c.competition_type                              as COMPETITIONTYPE,
    se.season_name                                  as SEASON,
    r.cafc_player_id                                as CAFC_PLAYER_ID,
    'IMPECT'::varchar(10)                           as DATA_SOURCE
from players p
left join resolved      r   on r.source_player_id = p.impect_player_id::varchar
left join squads        sq  on sq.cafc_squad_id   = p.current_squad_id
left join seasons       se  on se.cafc_season_id  = p.iteration_id
left join competitions  c   on c.cafc_competition_id = se.cafc_competition_id
left join positions     pos on pos.cafc_player_id = r.cafc_player_id and pos.rn = 1
