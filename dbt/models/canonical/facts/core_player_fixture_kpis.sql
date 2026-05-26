/*
  core_player_fixture_kpis
  ------------------------
  Per-(player, fixture, KPI) values, canonicalised across providers.

  Materialization: incremental MERGE keyed on the natural composite
  (cafc_player_id, cafc_fixture_id, cafc_kpi_id). Per dbt_project.yml:
    on_schema_change: append_new_columns  (new IMPECT KPI fields auto-added)
    incremental_strategy: merge           (UPSERT semantics)

  Incremental cursor:
    Process rows whose source.scheduled_at falls inside a 7-day lookback
    window from the highest scheduled_at currently in the target. The
    lookback catches late-arriving recalculations (IMPECT re-computes KPIs
    for recent fixtures); the MERGE on the unique key dedupes any overlap.

  Provider scope:
    Today only IMPECT (CHAMPIONSHIP_PLAYER_KPIS via staging). When other
    providers come online (Wyscout, Statsbomb), UNION ALL their staging
    models here — the resolution bridges already accept any source_system.

  CAFC_KPI_ID:
    Equal to impect_kpi_id today. Once the kpi_definitions seed lands
    (dbt/seeds/kpi_definitions.csv per plan §2.3), this resolves through
    that seed instead — provider-agnostic surrogate.
*/

{{ config(
    materialized='incremental',
    unique_key=['cafc_player_id', 'cafc_fixture_id', 'cafc_kpi_id'],
    on_schema_change='append_new_columns',
    incremental_strategy='merge'
) }}

with stg as (
    select * from {{ ref('stg_impect__championship_player_kpis') }}
),

resolved_player as (
    select * from {{ ref('core_player_id_resolutions') }}
),

resolved_fixture as (
    select * from {{ ref('core_fixture_id_resolutions') }}
),

joined as (
    select
        rp.cafc_player_id,
        rf.cafc_fixture_id,
        stg.impect_kpi_id           as cafc_kpi_id,    -- placeholder; canonicalises via kpi seed later
        stg.kpi_value,
        stg.scheduled_at,
        stg.season,
        stg.iteration_id            as cafc_season_id, -- from core_seasons surrogate
        stg.impect_competition_id   as cafc_competition_id,
        stg.impect_squad_id,
        stg.squad_side,
        stg.position_code,
        stg.play_duration_seconds,
        stg.match_share,
        'IMPECT'                    as source_system,
        stg.impect_player_id        as source_player_id,
        stg.impect_match_id         as source_fixture_id
    from stg
    join resolved_player rp
      on  rp.source_system    = 'IMPECT'
      and rp.source_player_id = stg.impect_player_id::varchar
    join resolved_fixture rf
      on  rf.source_system     = 'IMPECT'
      and rf.source_fixture_id = stg.impect_match_id::varchar

    {% if is_incremental() %}
    where stg.scheduled_at >= (
        select dateadd(day, -7, coalesce(max(scheduled_at), '1900-01-01'::timestamp_ntz))
        from {{ this }}
    )
    {% endif %}
)

select * from joined
