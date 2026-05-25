/*
  stg_impect__championship_player_kpis
  ------------------------------------
  Per-match per-player KPI values. ~37M rows. The primary input to canonical
  CORE.PLAYER_FIXTURE_KPIS (plan §2.5). Materialized as a view here because
  staging is always-fresh; the canonical layer downstream is incremental.
*/

with source as (
    select * from {{ source('impect', 'CHAMPIONSHIP_PLAYER_KPIS') }}
),

renamed as (
    select
        competitionid                           as impect_competition_id,
        competitionname                         as competition_name,
        season,
        iterationid                             as iteration_id,
        matchid                                 as impect_match_id,
        try_to_timestamp_ntz(scheduleddate)     as scheduled_at,
        squadside                               as squad_side,        -- HOME / AWAY
        squadid                                 as impect_squad_id,
        playerid                                as impect_player_id,
        position                                as position_code,
        playduration                            as play_duration_seconds,
        matchshare                              as match_share,       -- proportion of match the player was on
        kpiid                                   as impect_kpi_id,
        value                                   as kpi_value
    from source
)

select * from renamed
