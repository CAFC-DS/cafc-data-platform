/*
  app_compat.matches
  ------------------
  Legacy-shape match view for the recruitment-platform app.

  Mirrors RECRUITMENT_TEST.PUBLIC.MATCHES. Sourced from
  stg_impect__championship_match_info (which has the richer per-match
  metadata: home/away coaches, formations, stadium) plus core_squads for
  the squad-side denormalisations.

  Cross-provider IDs (SKILLCORNERID, HEIMSPIELID, WYSCOUTID) come from
  IMPECT_RAW.MATCHES directly — those columns are passthrough.

  Row count caveat: legacy MATCHES has ~122k rows, IMPECT_RAW.MATCHES has
  ~145k. Probably the same championship-scope filter as players. Confirm
  and add WHERE clause once known.
*/

with matches as (
    select * from {{ ref('stg_impect__matches') }}
),

info as (
    select * from {{ ref('stg_impect__championship_match_info') }}
),

squads as (
    select cafc_squad_id, squad_name, squad_type, impect_country_id
    from {{ ref('core_squads') }}
),

countries as (
    -- Country reference; here we use the IMPECT-side ID to feed both
    -- HOME / AWAY squad's COUNTRYNAME columns in the legacy shape.
    select impect_country_id, country_name from {{ ref('stg_impect__countries') }}
),

resolved as (
    select source_system, source_fixture_id, cafc_fixture_id
    from {{ ref('core_fixture_id_resolutions') }}
    where source_system = 'IMPECT'
)

select
    m.impect_match_id                               as ID,
    m.skill_corner_id                               as SKILLCORNERID,
    m.heim_spiel_id                                 as HEIMSPIELID,
    m.wyscout_id                                    as WYSCOUTID,
    m.iteration_id                                  as ITERATIONID,
    m.matchday_index                                as MATCHDAYINDEX,
    m.matchday_name                                 as MATCHDAYNAME,

    -- Home squad denormalisation
    m.home_squad_id                                 as HOMESQUADID,
    hs.squad_name                                   as HOMESQUADNAME,
    hs.squad_type                                   as HOMESQUADTYPE,
    hs.impect_country_id                            as HOMESQUADCOUNTRYID,
    hc.country_name                                 as HOMESQUADCOUNTRYNAME,
    null::number(38,0)                              as HOMESQUADSKILLCORNERID,
    null::number(38,0)                              as HOMESQUADHEIMSPIELID,
    null::number(38,0)                              as HOMESQUADWYSCOUTID,

    -- Away squad denormalisation
    m.away_squad_id                                 as AWAYSQUADID,
    a_s.squad_name                                  as AWAYSQUADNAME,
    a_s.squad_type                                  as AWAYSQUADTYPE,
    a_s.impect_country_id                           as AWAYSQUADCOUNTRYID,
    ac.country_name                                 as AWAYSQUADCOUNTRYNAME,
    null::number(38,0)                              as AWAYSQUADSKILLCORNERID,
    null::number(38,0)                              as AWAYSQUADHEIMSPIELID,
    null::number(38,0)                              as AWAYSQUADWYSCOUTID,

    m.scheduled_at                                  as SCHEDULEDDATE,
    m.last_calculation_at                           as LASTCALCULATIONDATE,
    m.is_available                                  as AVAILABLE,
    concat(hs.squad_name, ' vs ', a_s.squad_name)   as MATCH_NAME,
    r.cafc_fixture_id                               as CAFC_MATCH_ID,
    'IMPECT'::varchar(10)                           as DATA_SOURCE
from matches m
left join resolved   r   on r.source_fixture_id = m.impect_match_id::varchar
left join squads     hs  on hs.cafc_squad_id    = m.home_squad_id
left join squads     a_s on a_s.cafc_squad_id   = m.away_squad_id
left join countries  hc  on hc.impect_country_id = hs.impect_country_id
left join countries  ac  on ac.impect_country_id = a_s.impect_country_id
