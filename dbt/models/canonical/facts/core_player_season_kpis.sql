/*
  core_player_season_kpis
  -----------------------
  Per-(player, season, KPI) aggregates rolled up from
  core_player_fixture_kpis.

  Materialization: table (full refresh nightly). Source row count is on
  the order of N_players × N_seasons × N_kpis, which stays small enough
  (≪ 10M for the current scope) that a full rebuild beats incremental
  complexity. Switch to incremental if/when row count justifies it.

  Aggregations:
    - season_total    SUM of kpi_value across fixtures
    - season_average  AVG of kpi_value (only fixtures the player appeared in)
    - matches_played  COUNT of distinct fixtures the player has a row in
    - total_play_duration  SUM of play_duration_seconds (proxy for minutes played)
*/

{{ config(materialized='table') }}

with fixtures as (
    select * from {{ ref('core_player_fixture_kpis') }}
)

select
    cafc_player_id,
    cafc_season_id,
    cafc_competition_id,
    cafc_kpi_id,
    sum(kpi_value)                     as season_total,
    avg(kpi_value)                     as season_average,
    count(distinct cafc_fixture_id)    as matches_played,
    sum(play_duration_seconds)         as total_play_duration_seconds
from fixtures
group by 1, 2, 3, 4
