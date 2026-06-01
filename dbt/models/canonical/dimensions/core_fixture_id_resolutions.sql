/*
  core_fixture_id_resolutions
  ---------------------------
  Bridge view: (source_system, source_fixture_id) → cafc_fixture_id.

  No FIXTURE_IDENTITY_OVERRIDES table exists today — fixtures aren't yet
  part of the override workflow. The view exists for symmetry with
  core_player_id_resolutions so all fact models can use the same JOIN
  pattern, and so adding fixture overrides later doesn't require touching
  every downstream consumer.
*/

{{ config(materialized='view') }}

-- FIXTURE_IDENTITIES, like PLAYER_IDENTITIES, can carry more than one row per
-- (source_system, source_fixture_id) (re-links across contexts). Collapse to a
-- single canonical link per pair so fact joins stay 1:1 — same precedence as
-- core_player_id_resolutions: IS_PRIMARY, then MATCH_CONFIDENCE, then most
-- recent, then lowest CAFC_FIXTURE_ID as a stable tiebreak.
select
    source_system,
    source_fixture_id,
    cafc_fixture_id,
    'IDENTITY' as resolution_source
from (
    select
        source_system,
        source_fixture_id,
        cafc_fixture_id,
        row_number() over (
            partition by source_system, source_fixture_id
            order by
                case when is_primary then 0 else 1 end,
                match_confidence desc nulls last,
                updated_at desc nulls last,
                cafc_fixture_id
        ) as rn
    from {{ source('core', 'FIXTURE_IDENTITIES') }}
    where cafc_fixture_id is not null
)
where rn = 1
