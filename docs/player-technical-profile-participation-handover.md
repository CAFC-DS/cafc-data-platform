# Player technical-profile participation handover

The data platform replaces the retiring `CORE_PLAYER_FIXTURE_KPIS` dependency
with participation derived from Impect match lineups, substitutions, position
changes, red cards, and raw event clocks.

## Supported contracts

- `CAFC_DB.CORE.CORE_PLAYER_FIXTURE_POSITION_PARTICIPATION`: exact minutes at
  player × fixture × position grain. Use this for position history and event
  per-90 denominators.
- `CAFC_DB.CORE.CORE_PLAYER_ITERATION_PARTICIPATION`: total season seconds and
  duration-weighted dominant position. Use this for available seasons and
  season-level eligibility.
- Continue using `CAFC_DB.IMPECT_RAW.EVENTS` for technical action counts.

Do not read `CORE_PLAYER_FIXTURE_KPIS`, `CHAMPIONSHIP_PLAYER_KPIS`,
`ITERATION_PLAYER_KPIS`, or dbt staging views.

For external players, `find_player_by_universal_or_legacy_id` returns the
Impect ID in `player_data[0]` and canonical `CAFC_PLAYER_ID` in
`player_data[1]`. Raw events use the former; iteration participation uses the
latter.

## Available competition-season query

Use this in both `/position-history` and `/technical-metrics`:

```sql
SELECT DISTINCT c.COMPETITION_NAME, s.SEASON_NAME
FROM CAFC_DB.CORE.CORE_PLAYER_ITERATION_PARTICIPATION p
JOIN CAFC_DB.CORE.CORE_SEASONS s
  ON s.CAFC_SEASON_ID = p.CAFC_SEASON_ID
JOIN CAFC_DB.CORE.CORE_COMPETITIONS c
  ON c.CAFC_COMPETITION_ID = p.CAFC_COMPETITION_ID
WHERE p.CAFC_PLAYER_ID = %s
  AND p.TOTAL_PLAY_DURATION_SECONDS > 0
ORDER BY s.SEASON_NAME DESC, c.COMPETITION_NAME
```

Bind `int(player_data[1])`, not the external ID.

## Position minutes query

Replace the old fixture-KPI `minutes` CTE with:

```sql
SELECT
    p.SOURCE_PLAYER_ID AS PLAYER_ID,
    p.SOURCE_FIXTURE_ID AS MATCH_ID,
    p.POSITION_CODE AS POSITION,
    p.PLAY_DURATION_SECONDS AS SECONDS
FROM CAFC_DB.CORE.CORE_PLAYER_FIXTURE_POSITION_PARTICIPATION p
JOIN CAFC_DB.CORE.CORE_SEASONS s
  ON s.CAFC_SEASON_ID = p.CAFC_SEASON_ID
JOIN CAFC_DB.CORE.CORE_COMPETITIONS c
  ON c.CAFC_COMPETITION_ID = p.CAFC_COMPETITION_ID
WHERE c.COMPETITION_NAME = %s
  AND s.SEASON_NAME = %s
  AND p.PLAY_DURATION_SECONDS > 0
```

The table is already unique at player × fixture × squad × position grain. Do
not apply the old `MAX(PLAY_DURATION_SECONDS)` KPI-row deduplication. Join raw
event aggregates on player ID, match ID, and position code. Preserve the
existing event definitions, per-90 calculations, 450-minute peer threshold,
position-profile mapping, response schema, and 60-minute cache duration.

Use `player_position_history_v2` and `technical_metrics_v3` cache-key prefixes
to avoid serving values calculated from the retired table.

Before merging, run:

```bash
rg "CORE_PLAYER_FIXTURE_KPIS|CHAMPIONSHIP_PLAYER_KPIS" backend
```

Neither affected endpoint should retain a legacy reference. The platform
tables must be deployed before the application branch.
