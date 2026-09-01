/*
  stg_dvms__fixtures
  ------------------
  One row per FIXTURE_ID over the append-only DVMS_RAW.FIXTURES replay
  buffer. The extractor writes a fresh row every time a fixture's
  score/date changes (a NULL-score pre-match row, then a post-match row
  with the final score), so a fixture accumulates history there by design.
  This view exposes only the current state of each fixture -- the latest
  row by LOADED_AT -- which is the same logic the extractor uses
  internally in load_dvms_fixtures.py::_load_known_state(). INGESTION_RUN_ID
  DESC is an added tiebreak for determinism when two rows share a LOADED_AT.
*/

with source as (
    select * from {{ source('dvms', 'FIXTURES') }}
),

deduped as (
    select
        fixture_id,
        competition_id,
        season,
        opta_match_id,
        opta_home_team_id,
        opta_away_team_id,
        home_team_name,
        away_team_name,
        match_date,
        round,
        home_score,
        away_score,
        venue_id,
        venue_name,
        num_assets_available,
        raw_payload,
        loaded_at,
        ingestion_run_id
    from source
    qualify row_number() over (
        partition by fixture_id
        order by loaded_at desc, ingestion_run_id desc nulls last
    ) = 1
)

select * from deduped
