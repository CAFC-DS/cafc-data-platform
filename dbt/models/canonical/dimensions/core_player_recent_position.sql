/*
  core_player_recent_position
  -----------------------------
  Per-player most-recent on-pitch position, sourced from raw match events
  (IMPECT_RAW.EVENTS via stg_impect__events) rather than the KPI/score facts.

  Confirmed limitation, not a bug: event-level data only exists from
  dataVersion V2 onward (season 21/22+ for most competitions -- see
  snowflake/ddl/20260724_impect_events.sql), and as of this model's creation
  only a handful of matches have actually been loaded (Phase 1's small-sample
  test, not a full backfill). Most players will have NULL here until the
  event backfill (a separate, later step) actually runs -- this is the
  correct long-term source, populated incrementally as ingestion catches up,
  not a KPI-derived shortcut like the column it replaces.

  Grain: one row per cafc_player_id, keeping their most recent event by
  (iteration_id, match_id, event_index) -- a reasonable proxy for recency
  given events don't carry a wall-clock match date directly.
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__events') }}
    where player_position is not null
),

resolved as (
    select
        r.cafc_player_id,
        s.player_position,
        s.iteration_id,
        s.impect_match_id,
        s.event_index
    from stg s
    inner join {{ ref('core_player_id_resolutions') }} r
            on r.source_system    = 'IMPECT'
           and r.source_player_id = s.impect_player_id::varchar
),

most_recent as (
    select
        cafc_player_id,
        player_position,
        row_number() over (
            partition by cafc_player_id
            order by iteration_id desc, impect_match_id desc, event_index desc
        ) as rn
    from resolved
)

select
    cafc_player_id,
    player_position as position_code
from most_recent
where rn = 1
