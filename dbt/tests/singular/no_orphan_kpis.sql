/*
  no_orphan_kpis
  --------------
  Fail the refresh if any IMPECT player who has KPI rows in
  IMPECT_RAW.CHAMPIONSHIP_PLAYER_KPIS doesn't resolve to a CAFC_PLAYER_ID
  via core_player_id_resolutions.

  When this fires, the matcher saw the external player but either:
    a) Queued them as AMBIGUOUS (in PLAYER_IDENTITY_CANDIDATES) and didn't
       link — so the KPIs would be silently dropped from the canonical layer.
    b) Hasn't been re-run since IMPECT shipped this player's data — the
       extractor and matcher are out of sync. Re-run the matcher.

  The fix path is:
    1. Inspect PLAYER_IDENTITY_CANDIDATES for the listed source_player_ids.
    2. Resolve each by inserting a row into PLAYER_IDENTITY_OVERRIDES.
    3. Re-run the orchestrator; the override-precedence resolution links
       the previously-orphaned KPIs.

  dbt singular test semantics: rows returned == test failures. Zero rows
  means clean.
*/

with kpi_players as (
    select distinct impect_player_id::varchar as source_player_id
    from {{ ref('stg_impect__championship_player_kpis') }}
),

linked as (
    select source_player_id
    from {{ ref('core_player_id_resolutions') }}
    where source_system = 'IMPECT'
)

select
    'IMPECT'                  as source_system,
    kp.source_player_id,
    'has KPI rows but no CAFC_PLAYER_ID linked through PLAYER_IDENTITIES or PLAYER_IDENTITY_OVERRIDES' as issue
from kpi_players kp
left join linked l using (source_player_id)
where l.source_player_id is null
