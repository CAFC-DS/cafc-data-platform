/* Generic typed view over IMPECT_RAW.MATCH_INFO. */

with source as (
    select * from {{ source('impect', 'MATCH_INFO') }}
)

select
    match_id                    as impect_match_id,
    iteration_id,
    match_datetime,
    source_last_calculation_at,
    home_squad_id,
    away_squad_id,
    home_players,
    away_players,
    home_starting_positions,
    away_starting_positions,
    home_substitutions,
    away_substitutions,
    home_formations,
    away_formations,
    source_format,
    loaded_at
from source

