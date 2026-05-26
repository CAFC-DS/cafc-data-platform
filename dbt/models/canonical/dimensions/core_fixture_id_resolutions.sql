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

select
    source_system,
    source_fixture_id,
    cafc_fixture_id,
    'IDENTITY' as resolution_source
from {{ source('core', 'FIXTURE_IDENTITIES') }}
where cafc_fixture_id is not null
