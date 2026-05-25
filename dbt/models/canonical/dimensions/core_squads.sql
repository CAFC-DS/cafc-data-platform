/*
  core_squads
  -----------
  Canonical squad dimension. One row per club / national team, deduped
  across iterations.

  Identity:
    cafc_squad_id = impect_squad_id today (IMPECT is the only provider).
    Once squad identity resolution is built (sibling of the player matcher
    in plan §2.6), this column becomes a stable surrogate populated through
    CORE.SQUAD_IDENTITIES and overridden by CORE.SQUAD_NAME_OVERRIDES (the
    table created in snowflake/ddl/20260525_identity_overrides.sql).

  Dedup strategy:
    IMPECT_RAW.SQUADS carries one row per (iteration_id, squad_id), so the
    same club can appear N times across seasons. We pick the most-recent
    iteration's row per impect_squad_id and keep its name/country/type, on
    the assumption that the most-recent IMPECT extract has the most-current
    metadata. If two rows tie on iteration_id (shouldn't happen), the
    deterministic ORDER BY breaks the tie.
*/

{{ config(materialized='table') }}

with stg as (
    select * from {{ ref('stg_impect__squads') }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by impect_squad_id
            order by coalesce(iteration_id, 0) desc, squad_name
        ) as rn
    from stg
),

deduped as (
    select * from ranked where rn = 1
)

select
    impect_squad_id                                 as cafc_squad_id,    -- placeholder until squad identity is resolved
    impect_squad_id,
    squad_name,
    impect_country_id,
    squad_type,
    gender,
    image_url,
    has_access,
    wyscout_id,
    heim_spiel_id,
    skill_corner_id
from deduped
