/*
  app_compat.matches
  ------------------
  Legacy-shape match view — the RECRUITMENT_TEST.PUBLIC.MATCHES contract,
  sourced from the canonical layer. One row per canonical fixture.

  DATA_SOURCE (exclusive, matches legacy 'external'/'internal'):
    - external: fixture has an IMPECT identity → ID = its primary IMPECT match id,
      rich metadata (matchday, cross-provider ids, scheduling) from IMPECT.
    - internal: no IMPECT identity (manually-added match) → ID = NULL; home/away/
      date come from CORE.FIXTURES. (~203 internal, matching legacy's ~212.)

  29 columns, matching legacy column-for-column. CAFC_MATCH_ID = CAFC_FIXTURE_ID.
  Stays a view: the metadata join is over ~145k fixtures, not the big fact tables.
*/

{{ config(materialized='view') }}

with fixtures as (
    select cafc_fixture_id, home_squad_id, away_squad_id, fixture_date
    from {{ source('core', 'FIXTURES') }}
),

-- Primary IMPECT identity per fixture; presence => external.
impect_identity as (
    select cafc_fixture_id, source_fixture_id
    from (
        select
            cafc_fixture_id,
            source_fixture_id,
            row_number() over (
                partition by cafc_fixture_id
                order by case when is_primary then 0 else 1 end,
                         match_confidence desc nulls last,
                         fixture_identity_id
            ) as rn
        from {{ source('core', 'FIXTURE_IDENTITIES') }}
        where source_system = 'IMPECT'
    )
    where rn = 1
),

impect_match as (
    select * from {{ ref('stg_impect__matches') }}
),

squads as (
    select cafc_squad_id, squad_name, squad_type, impect_country_id from {{ ref('core_squads') }}
),
countries as (
    select impect_country_id, country_name from {{ ref('stg_impect__countries') }}
)

select
    ii.source_fixture_id                            as ID,
    im.skill_corner_id                              as SKILLCORNERID,
    im.heim_spiel_id                                as HEIMSPIELID,
    im.wyscout_id                                   as WYSCOUTID,
    im.iteration_id                                 as ITERATIONID,
    im.matchday_index                               as MATCHDAYINDEX,
    im.matchday_name                                as MATCHDAYNAME,

    -- Home squad denormalisation
    f.home_squad_id                                 as HOMESQUADID,
    hs.squad_name                                   as HOMESQUADNAME,
    hs.squad_type                                   as HOMESQUADTYPE,
    hs.impect_country_id                            as HOMESQUADCOUNTRYID,
    hc.country_name                                 as HOMESQUADCOUNTRYNAME,
    null::number(38,0)                              as HOMESQUADSKILLCORNERID,
    null::number(38,0)                              as HOMESQUADHEIMSPIELID,
    null::number(38,0)                              as HOMESQUADWYSCOUTID,

    -- Away squad denormalisation
    f.away_squad_id                                 as AWAYSQUADID,
    a_s.squad_name                                  as AWAYSQUADNAME,
    a_s.squad_type                                  as AWAYSQUADTYPE,
    a_s.impect_country_id                           as AWAYSQUADCOUNTRYID,
    ac.country_name                                 as AWAYSQUADCOUNTRYNAME,
    null::number(38,0)                              as AWAYSQUADSKILLCORNERID,
    null::number(38,0)                              as AWAYSQUADHEIMSPIELID,
    null::number(38,0)                              as AWAYSQUADWYSCOUTID,

    coalesce(im.scheduled_at, f.fixture_date)       as SCHEDULEDDATE,
    im.last_calculation_at                          as LASTCALCULATIONDATE,
    im.is_available                                 as AVAILABLE,
    concat(hs.squad_name, ' vs ', a_s.squad_name)   as MATCH_NAME,
    f.cafc_fixture_id                               as CAFC_MATCH_ID,
    case when ii.cafc_fixture_id is not null
         then 'external' else 'internal' end         as DATA_SOURCE
from fixtures f
left join impect_identity ii  on ii.cafc_fixture_id = f.cafc_fixture_id
left join impect_match    im  on im.impect_match_id::varchar = ii.source_fixture_id
left join squads          hs  on hs.cafc_squad_id   = f.home_squad_id
left join squads          a_s on a_s.cafc_squad_id  = f.away_squad_id
left join countries       hc  on hc.impect_country_id = hs.impect_country_id
left join countries       ac  on ac.impect_country_id = a_s.impect_country_id
