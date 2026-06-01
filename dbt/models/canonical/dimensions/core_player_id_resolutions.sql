/*
  core_player_id_resolutions
  --------------------------
  Bridge view: (source_system, source_player_id) → cafc_player_id, with
  PLAYER_IDENTITY_OVERRIDES taking precedence over PLAYER_IDENTITIES.

  Why this lives outside the matcher:
    The matcher applies override precedence at *write* time — when it links
    a new external player, the resulting PLAYER_IDENTITIES row already
    reflects any matching override. But if a human adds an override row
    *after* the matcher has linked an identity, the stale PLAYER_IDENTITIES
    row points at the old CAFC_PLAYER_ID and the override is dormant.

    This view honours overrides at *query* time, so any model joining
    through it gets the latest answer regardless of when overrides were
    added. View materialization (rather than table) keeps it always-fresh.

  Resolution rule:
    For each (source_system, source_player_id) pair that exists in either
    table, prefer overrides.cafc_player_id, fall back to identities.
*/

{{ config(materialized='view') }}

with overrides as (
    select
        source_system,
        source_player_id,
        cafc_player_id    as overridden_cafc_player_id
    from {{ source('core', 'PLAYER_IDENTITY_OVERRIDES') }}
),

-- PLAYER_IDENTITIES carries one row per (player, source squad/context)
-- appearance, so it is NOT unique on (source_system, source_player_id) —
-- ~3.7 rows per IMPECT player. Collapse to the single canonical link per pair
-- so downstream fact joins don't fan out: prefer IS_PRIMARY, then highest
-- MATCH_CONFIDENCE, then most-recently updated, then lowest CAFC_PLAYER_ID as
-- a stable deterministic tiebreak (84 players legitimately map to >1 id).
identities as (
    select
        source_system,
        source_player_id,
        linked_cafc_player_id
    from (
        select
            source_system,
            source_player_id,
            cafc_player_id    as linked_cafc_player_id,
            row_number() over (
                partition by source_system, source_player_id
                order by
                    case when is_primary then 0 else 1 end,
                    match_confidence desc nulls last,
                    updated_at desc nulls last,
                    cafc_player_id
            )                 as rn
        from {{ source('core', 'PLAYER_IDENTITIES') }}
    )
    where rn = 1
),

merged as (
    select
        coalesce(o.source_system,    i.source_system)    as source_system,
        coalesce(o.source_player_id, i.source_player_id) as source_player_id,
        o.overridden_cafc_player_id,
        i.linked_cafc_player_id
    from overrides o
    full outer join identities i
      on  o.source_system    = i.source_system
      and o.source_player_id = i.source_player_id
)

select
    source_system,
    source_player_id,
    coalesce(overridden_cafc_player_id, linked_cafc_player_id) as cafc_player_id,
    case
        when overridden_cafc_player_id is not null then 'OVERRIDE'
        else 'IDENTITY'
    end as resolution_source
from merged
where coalesce(overridden_cafc_player_id, linked_cafc_player_id) is not null
