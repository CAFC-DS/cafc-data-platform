/*
  core_fixture_details
  ---------------------
  Denormalised fixture view: one row per canonical fixture, with squad
  names/countries resolved and IMPECT match metadata joined in.

  Built for Phase 6 sub-project 4 (recruitment app reading CORE natively
  instead of through app_compat.matches) - this is the CORE-native twin of
  app_compat/matches.sql, with canonical (snake_case) column names instead
  of the legacy MATCHES contract. app_compat.matches keeps its own
  independent join logic (don't rewrite it to select from this model -
  it's retired outright once the app stops reading it, not refactored to
  depend on it).

  data_source (exclusive):
    - external: fixture has an IMPECT identity -> source_fixture_id = its
      primary IMPECT match id, rich metadata (matchday, cross-provider ids,
      scheduling) from IMPECT.
    - internal: no IMPECT identity (manually-added fixture) ->
      source_fixture_id = NULL; home/away/date come from CORE.FIXTURES.
      Squad NAME is the scout's own entry (FIXTURE_IDENTITIES.SOURCE_*_
      SQUAD_NAME, manual_names cte), falling back to the core_squads name
      only if that's absent. Squad id/type/country still come from
      core_squads via the (repaired) home/away_squad_id.

  Stays a view: the metadata join is over ~177k fixtures, not the big fact
  tables.

  The six always-NULL *_skill_corner_id/_heim_spiel_id/_wyscout_id columns
  exist only for shape parity with the legacy MATCHES contract (which never
  populated per-squad cross-provider ids either) - no CORE-native consumer
  needs them; candidates for removal once app_compat.matches retires.

  source_fixture_id isn't strictly unique: ~114 rows (2026-09-27) share a
  source_fixture_id with another cafc_fixture_id (max fan-out 3) - a
  pre-existing duplication in FIXTURE_IDENTITIES inherited from the shared
  join logic, not introduced here; app_compat.matches has the identical
  issue today, untested until this model's warn-severity test surfaced it.
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

-- Source-system team names for manually-added fixtures. Fallback for the
-- residual where home/away_squad_id doesn't resolve in core_squads (legacy
-- free-text matches with no real squad id).
manual_names as (
    select cafc_fixture_id, source_home_squad_name, source_away_squad_name
    from (
        select
            cafc_fixture_id,
            source_home_squad_name,
            source_away_squad_name,
            row_number() over (
                partition by cafc_fixture_id
                order by case when is_primary then 0 else 1 end,
                         match_confidence desc nulls last,
                         fixture_identity_id
            ) as rn
        from {{ source('core', 'FIXTURE_IDENTITIES') }}
        where source_system = 'MANUAL'
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
    f.cafc_fixture_id                                     as cafc_fixture_id,
    ii.source_fixture_id                                  as source_fixture_id,
    im.skill_corner_id                                    as skill_corner_id,
    im.heim_spiel_id                                      as heim_spiel_id,
    im.wyscout_id                                         as wyscout_id,
    im.iteration_id                                       as iteration_id,
    im.matchday_index                                     as matchday_index,
    im.matchday_name                                      as matchday_name,

    -- Home squad denormalisation
    f.home_squad_id                                        as home_squad_id,
    coalesce(mn.source_home_squad_name, hs.squad_name)     as home_squad_name,
    hs.squad_type                                          as home_squad_type,
    hs.impect_country_id                                   as home_squad_country_id,
    hc.country_name                                        as home_squad_country_name,
    null::number(38,0)                                     as home_squad_skill_corner_id,
    null::number(38,0)                                     as home_squad_heim_spiel_id,
    null::number(38,0)                                     as home_squad_wyscout_id,

    -- Away squad denormalisation
    f.away_squad_id                                        as away_squad_id,
    coalesce(mn.source_away_squad_name, a_s.squad_name)    as away_squad_name,
    a_s.squad_type                                         as away_squad_type,
    a_s.impect_country_id                                  as away_squad_country_id,
    ac.country_name                                        as away_squad_country_name,
    null::number(38,0)                                     as away_squad_skill_corner_id,
    null::number(38,0)                                     as away_squad_heim_spiel_id,
    null::number(38,0)                                     as away_squad_wyscout_id,

    coalesce(im.scheduled_at, f.fixture_date)              as scheduled_at,
    im.last_calculation_at                                 as last_calculation_at,
    im.is_available                                        as is_available,
    concat(
        coalesce(mn.source_home_squad_name, hs.squad_name),
        ' vs ',
        coalesce(mn.source_away_squad_name, a_s.squad_name)
    )                                                       as match_name,
    case when ii.cafc_fixture_id is not null
         then 'external' else 'internal' end                as data_source
from fixtures f
left join impect_identity ii  on ii.cafc_fixture_id = f.cafc_fixture_id
left join manual_names    mn  on mn.cafc_fixture_id = f.cafc_fixture_id
left join impect_match    im  on im.impect_match_id::varchar = ii.source_fixture_id
left join squads          hs  on hs.cafc_squad_id   = f.home_squad_id
left join squads          a_s on a_s.cafc_squad_id  = f.away_squad_id
left join countries       hc  on hc.impect_country_id = hs.impect_country_id
left join countries       ac  on ac.impect_country_id = a_s.impect_country_id
