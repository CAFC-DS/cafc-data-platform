"""Load generic Impect match metadata for event-backed matches.

The resulting table deliberately contains no player KPI values.  It preserves
the lineup, starting-position and substitution portions of GET /matches/{id}
needed to calculate authoritative playing time after the legacy
CHAMPIONSHIP_PLAYER_KPIS table is retired.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import impect_api as api
from snowflake_loader import get_connection


TABLE = "CAFC_DB.IMPECT_RAW.MATCH_INFO"


def _payload(response: Any) -> dict[str, Any]:
    value = response.get("data", response) if isinstance(response, dict) else {}
    return value if isinstance(value, dict) else {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=True)


def payload_row(payload: dict[str, Any], match_id: int, iteration_id: int | None,
                run_id: int | None = None) -> dict[str, Any]:
    home = payload.get("squadHome") or {}
    away = payload.get("squadAway") or {}
    return {
        "match_id": int(match_id),
        "iteration_id": int(payload.get("iterationId") or iteration_id) if (payload.get("iterationId") or iteration_id) is not None else None,
        "match_datetime": payload.get("dateTime") or payload.get("scheduledDate"),
        "last_calculation": payload.get("lastCalculationDate"),
        "home_squad_id": home.get("id"),
        "away_squad_id": away.get("id"),
        "home_players": _json(home.get("players")),
        "away_players": _json(away.get("players")),
        "home_starts": _json(home.get("startingPositions")),
        "away_starts": _json(away.get("startingPositions")),
        "home_subs": _json(home.get("substitutions")),
        "away_subs": _json(away.get("substitutions")),
        "home_formations": _json(home.get("formations")),
        "away_formations": _json(away.get("formations")),
        "raw": json.dumps(payload, ensure_ascii=True),
        "run_id": run_id,
    }


def upsert(row: dict[str, Any]) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                MERGE INTO {TABLE} t
                USING (SELECT %(match_id)s::NUMBER AS MATCH_ID) s
                   ON t.MATCH_ID = s.MATCH_ID
                WHEN MATCHED THEN UPDATE SET
                  ITERATION_ID=%(iteration_id)s,
                  MATCH_DATETIME=TRY_TO_TIMESTAMP_NTZ(%(match_datetime)s),
                  SOURCE_LAST_CALCULATION_AT=TRY_TO_TIMESTAMP_NTZ(%(last_calculation)s),
                  HOME_SQUAD_ID=%(home_squad_id)s, AWAY_SQUAD_ID=%(away_squad_id)s,
                  HOME_PLAYERS=PARSE_JSON(%(home_players)s), AWAY_PLAYERS=PARSE_JSON(%(away_players)s),
                  HOME_STARTING_POSITIONS=PARSE_JSON(%(home_starts)s),
                  AWAY_STARTING_POSITIONS=PARSE_JSON(%(away_starts)s),
                  HOME_SUBSTITUTIONS=PARSE_JSON(%(home_subs)s), AWAY_SUBSTITUTIONS=PARSE_JSON(%(away_subs)s),
                  HOME_FORMATIONS=PARSE_JSON(%(home_formations)s), AWAY_FORMATIONS=PARSE_JSON(%(away_formations)s),
                  RAW_MATCH_INFO=PARSE_JSON(%(raw)s), SOURCE_FORMAT='MATCH_API',
                  LOADED_AT=CURRENT_TIMESTAMP(), INGESTION_RUN_ID=%(run_id)s
                WHEN NOT MATCHED THEN INSERT (
                  MATCH_ID, ITERATION_ID, MATCH_DATETIME, SOURCE_LAST_CALCULATION_AT,
                  HOME_SQUAD_ID, AWAY_SQUAD_ID, HOME_PLAYERS, AWAY_PLAYERS,
                  HOME_STARTING_POSITIONS, AWAY_STARTING_POSITIONS,
                  HOME_SUBSTITUTIONS, AWAY_SUBSTITUTIONS, HOME_FORMATIONS, AWAY_FORMATIONS,
                  RAW_MATCH_INFO, SOURCE_FORMAT, INGESTION_RUN_ID
                ) VALUES (
                  %(match_id)s, %(iteration_id)s, TRY_TO_TIMESTAMP_NTZ(%(match_datetime)s),
                  TRY_TO_TIMESTAMP_NTZ(%(last_calculation)s), %(home_squad_id)s, %(away_squad_id)s,
                  PARSE_JSON(%(home_players)s), PARSE_JSON(%(away_players)s),
                  PARSE_JSON(%(home_starts)s), PARSE_JSON(%(away_starts)s),
                  PARSE_JSON(%(home_subs)s), PARSE_JSON(%(away_subs)s),
                  PARSE_JSON(%(home_formations)s), PARSE_JSON(%(away_formations)s),
                  PARSE_JSON(%(raw)s), 'MATCH_API', %(run_id)s
                )
                """,
                row,
            )
        conn.commit()
    finally:
        conn.close()


def fetch_and_load(match_id: int, iteration_id: int | None, run_id: int | None = None) -> None:
    payload = _payload(api.get_match_info(match_id))
    if not payload:
        raise RuntimeError(f"Empty match-info payload for match {match_id}")
    upsert(payload_row(payload, match_id, iteration_id, run_id))


def bootstrap_legacy(*, dry_run: bool = False) -> int:
    """Copy reusable legacy match metadata without carrying KPI dependencies."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM CAFC_DB.IMPECT_RAW.CHAMPIONSHIP_MATCH_INFO")
            count = int(cur.fetchone()[0])
            if dry_run:
                return count
            cur.execute(
                f"""
                MERGE INTO {TABLE} t
                USING (
                  SELECT MATCHID::NUMBER MATCH_ID, ITERATIONID::NUMBER ITERATION_ID,
                         TRY_TO_TIMESTAMP_NTZ(DATETIME) MATCH_DATETIME,
                         TRY_TO_TIMESTAMP_NTZ(LASTCALCULATIONDATE) SOURCE_LAST_CALCULATION_AT,
                         SQUADHOMEID::NUMBER HOME_SQUAD_ID, SQUADAWAYID::NUMBER AWAY_SQUAD_ID,
                         TRY_PARSE_JSON(SQUADHOMEPLAYERSJSON) HOME_PLAYERS,
                         TRY_PARSE_JSON(SQUADAWAYPLAYERSJSON) AWAY_PLAYERS,
                         TRY_PARSE_JSON(SQUADHOMESTARTINGPOSITIONSJSON) HOME_STARTING_POSITIONS,
                         TRY_PARSE_JSON(SQUADAWAYSTARTINGPOSITIONSJSON) AWAY_STARTING_POSITIONS,
                         TRY_PARSE_JSON(SQUADHOMESUBSTITUTIONSJSON) HOME_SUBSTITUTIONS,
                         TRY_PARSE_JSON(SQUADAWAYSUBSTITUTIONSJSON) AWAY_SUBSTITUTIONS,
                         TRY_PARSE_JSON(SQUADHOMEFORMATIONSJSON) HOME_FORMATIONS,
                         TRY_PARSE_JSON(SQUADAWAYFORMATIONSJSON) AWAY_FORMATIONS
                  FROM CAFC_DB.IMPECT_RAW.CHAMPIONSHIP_MATCH_INFO
                ) s ON t.MATCH_ID=s.MATCH_ID
                WHEN NOT MATCHED THEN INSERT (
                  MATCH_ID, ITERATION_ID, MATCH_DATETIME, SOURCE_LAST_CALCULATION_AT,
                  HOME_SQUAD_ID, AWAY_SQUAD_ID, HOME_PLAYERS, AWAY_PLAYERS,
                  HOME_STARTING_POSITIONS, AWAY_STARTING_POSITIONS,
                  HOME_SUBSTITUTIONS, AWAY_SUBSTITUTIONS, HOME_FORMATIONS, AWAY_FORMATIONS,
                  SOURCE_FORMAT
                ) VALUES (
                  s.MATCH_ID, s.ITERATION_ID, s.MATCH_DATETIME, s.SOURCE_LAST_CALCULATION_AT,
                  s.HOME_SQUAD_ID, s.AWAY_SQUAD_ID, s.HOME_PLAYERS, s.AWAY_PLAYERS,
                  s.HOME_STARTING_POSITIONS, s.AWAY_STARTING_POSITIONS,
                  s.HOME_SUBSTITUTIONS, s.AWAY_SUBSTITUTIONS, s.HOME_FORMATIONS, s.AWAY_FORMATIONS,
                  'LEGACY_BOOTSTRAP'
                )
                """
            )
        conn.commit()
        return count
    finally:
        conn.close()


def missing_event_matches(limit: int | None = None) -> list[tuple[int, int | None]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = f"""
                SELECT e.MATCH_ID, MAX(e.ITERATION_ID) ITERATION_ID
                FROM CAFC_DB.IMPECT_RAW.EVENTS e
                LEFT JOIN {TABLE} i ON i.MATCH_ID=e.MATCH_ID
                WHERE i.MATCH_ID IS NULL
                GROUP BY e.MATCH_ID
                ORDER BY e.MATCH_ID
            """
            if limit is not None:
                sql += " LIMIT %(limit)s"
                cur.execute(sql, {"limit": limit})
            else:
                cur.execute(sql)
            return [(int(row[0]), int(row[1]) if row[1] is not None else None) for row in cur.fetchall()]
    finally:
        conn.close()


def backfill(*, limit: int | None, pause_seconds: float, dry_run: bool) -> dict[str, int]:
    matches = missing_event_matches(limit)
    if dry_run:
        return {"candidates": len(matches), "loaded": 0, "failed": 0}
    loaded = failed = 0
    for index, (match_id, iteration_id) in enumerate(matches):
        if index:
            time.sleep(pause_seconds)
        try:
            fetch_and_load(match_id, iteration_id)
            loaded += 1
        except Exception as exc:
            failed += 1
            print(f"match {match_id}: {exc}", file=sys.stderr)
    return {"candidates": len(matches), "loaded": loaded, "failed": failed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bootstrap-legacy", action="store_true")
    parser.add_argument("--backfill-events", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--pause-seconds", type=float, default=0.35)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.bootstrap_legacy and not args.backfill_events:
        raise SystemExit("Choose --bootstrap-legacy and/or --backfill-events")
    if args.bootstrap_legacy:
        print({"legacy_rows": bootstrap_legacy(dry_run=args.dry_run)})
    if args.backfill_events:
        print(backfill(limit=args.limit, pause_seconds=args.pause_seconds, dry_run=args.dry_run))
