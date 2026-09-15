"""Load IMPECT set-piece sub-phase classification (corner/FK/throw-in type,
swing direction, first/second touch) for event-backed matches.

One row per sub-phase (a phase can carry multiple sub-phases, e.g. recycled
corners), keyed on (SET_PIECE_ID, SUB_PHASE_ID). Replaces the geometry-derived
approximation previously used downstream in pre-match-set-piece-report.

Uses the same stage-table + MERGE pattern as load_match_events.py's
backfill_kpis(): a per-match match can carry 50-100+ sub-phase rows, and a
full historical backfill is ~35k matches, so a per-row MERGE (viable for
load_match_info.py's one-row-per-match table) would be prohibitively slow
here. write_pandas bulk-loads the match's rows into a TEMPORARY stage table,
then one MERGE upserts them all in a single statement.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import config
import impect_api as api
import pandas as pd
from snowflake.connector.pandas_tools import write_pandas
from snowflake_loader import get_connection


TABLE = "CAFC_DB.IMPECT_RAW.SET_PIECES"
STAGE_TABLE_NAME = "SET_PIECES_STAGE"
STAGE_TABLE = f"{config.SNOWFLAKE_SCHEMA}.{STAGE_TABLE_NAME}"

# Column order matches the SET_PIECES DDL (snowflake/ddl/20260915_impect_set_pieces.sql).
# AGGREGATES/RAW_PHASE are staged as VARCHAR (write_pandas can't land parsed
# VARIANT directly -- see load_match_events.py's load_events() comment for the
# same finding) and PARSE_JSON'd inside the MERGE below.
COLUMNS = [
    "MATCH_ID", "ITERATION_ID", "SET_PIECE_ID", "PHASE_INDEX", "SQUAD_ID",
    "SET_PIECE_CATEGORY", "ADJ_SET_PIECE_CATEGORY", "SET_PIECE_EXECUTION_TYPE",
    "START_TIME_IN_SEC", "END_TIME_IN_SEC", "SUB_PHASE_ID", "SUB_PHASE_INDEX",
    "START_ZONE", "CORNER_END_ZONE", "CORNER_TYPE", "FREE_KICK_END_ZONE", "FREE_KICK_TYPE",
    "THROW_IN_END_ZONE", "THROW_IN_TYPE", "GOAL_KICK_END_ZONE", "GOAL_KICK_TYPE",
    "BALL_TRAJECTORY", "MAIN_EVENT_PLAYER_ID", "MAIN_EVENT_OUTCOME", "PASS_RECEIVER_ID",
    "FIRST_TOUCH_PLAYER_ID", "FIRST_TOUCH_WON", "INDIRECT_HEADER",
    "SECOND_TOUCH_PLAYER_ID", "SECOND_TOUCH_WON", "SECOND_TOUCH_END_ZONE",
    "AGGREGATES", "RAW_PHASE", "INGESTION_RUN_ID",
]

_STAGE_DDL = f"""
    CREATE TEMPORARY TABLE IF NOT EXISTS {STAGE_TABLE} (
      MATCH_ID NUMBER, ITERATION_ID NUMBER, SET_PIECE_ID NUMBER, PHASE_INDEX NUMBER,
      SQUAD_ID NUMBER, SET_PIECE_CATEGORY VARCHAR, ADJ_SET_PIECE_CATEGORY VARCHAR,
      SET_PIECE_EXECUTION_TYPE VARCHAR, START_TIME_IN_SEC FLOAT, END_TIME_IN_SEC FLOAT,
      SUB_PHASE_ID NUMBER, SUB_PHASE_INDEX NUMBER, START_ZONE VARCHAR,
      CORNER_END_ZONE VARCHAR, CORNER_TYPE VARCHAR, FREE_KICK_END_ZONE VARCHAR, FREE_KICK_TYPE VARCHAR,
      THROW_IN_END_ZONE VARCHAR, THROW_IN_TYPE VARCHAR, GOAL_KICK_END_ZONE VARCHAR, GOAL_KICK_TYPE VARCHAR,
      BALL_TRAJECTORY VARCHAR, MAIN_EVENT_PLAYER_ID NUMBER, MAIN_EVENT_OUTCOME VARCHAR, PASS_RECEIVER_ID NUMBER,
      FIRST_TOUCH_PLAYER_ID NUMBER, FIRST_TOUCH_WON BOOLEAN, INDIRECT_HEADER VARCHAR,
      SECOND_TOUCH_PLAYER_ID NUMBER, SECOND_TOUCH_WON BOOLEAN, SECOND_TOUCH_END_ZONE VARCHAR,
      AGGREGATES VARCHAR, RAW_PHASE VARCHAR, INGESTION_RUN_ID NUMBER
    )
"""

_MERGE_SQL = f"""
    MERGE INTO {TABLE} t
    USING {STAGE_TABLE} s
       ON t.SET_PIECE_ID = s.SET_PIECE_ID AND t.SUB_PHASE_ID = s.SUB_PHASE_ID
    WHEN MATCHED THEN UPDATE SET
      MATCH_ID=s.MATCH_ID, ITERATION_ID=s.ITERATION_ID, PHASE_INDEX=s.PHASE_INDEX,
      SQUAD_ID=s.SQUAD_ID, SET_PIECE_CATEGORY=s.SET_PIECE_CATEGORY,
      ADJ_SET_PIECE_CATEGORY=s.ADJ_SET_PIECE_CATEGORY,
      SET_PIECE_EXECUTION_TYPE=s.SET_PIECE_EXECUTION_TYPE,
      START_TIME_IN_SEC=s.START_TIME_IN_SEC, END_TIME_IN_SEC=s.END_TIME_IN_SEC,
      SUB_PHASE_INDEX=s.SUB_PHASE_INDEX, START_ZONE=s.START_ZONE,
      CORNER_END_ZONE=s.CORNER_END_ZONE, CORNER_TYPE=s.CORNER_TYPE,
      FREE_KICK_END_ZONE=s.FREE_KICK_END_ZONE, FREE_KICK_TYPE=s.FREE_KICK_TYPE,
      THROW_IN_END_ZONE=s.THROW_IN_END_ZONE, THROW_IN_TYPE=s.THROW_IN_TYPE,
      GOAL_KICK_END_ZONE=s.GOAL_KICK_END_ZONE, GOAL_KICK_TYPE=s.GOAL_KICK_TYPE,
      BALL_TRAJECTORY=s.BALL_TRAJECTORY,
      MAIN_EVENT_PLAYER_ID=s.MAIN_EVENT_PLAYER_ID, MAIN_EVENT_OUTCOME=s.MAIN_EVENT_OUTCOME,
      PASS_RECEIVER_ID=s.PASS_RECEIVER_ID,
      FIRST_TOUCH_PLAYER_ID=s.FIRST_TOUCH_PLAYER_ID, FIRST_TOUCH_WON=s.FIRST_TOUCH_WON,
      INDIRECT_HEADER=s.INDIRECT_HEADER,
      SECOND_TOUCH_PLAYER_ID=s.SECOND_TOUCH_PLAYER_ID, SECOND_TOUCH_WON=s.SECOND_TOUCH_WON,
      SECOND_TOUCH_END_ZONE=s.SECOND_TOUCH_END_ZONE,
      AGGREGATES=PARSE_JSON(s.AGGREGATES), RAW_PHASE=PARSE_JSON(s.RAW_PHASE),
      SOURCE_FORMAT='SET_PIECES_API', LOADED_AT=CURRENT_TIMESTAMP(),
      INGESTION_RUN_ID=s.INGESTION_RUN_ID
    WHEN NOT MATCHED THEN INSERT (
      MATCH_ID, ITERATION_ID, SET_PIECE_ID, PHASE_INDEX, SQUAD_ID,
      SET_PIECE_CATEGORY, ADJ_SET_PIECE_CATEGORY, SET_PIECE_EXECUTION_TYPE,
      START_TIME_IN_SEC, END_TIME_IN_SEC, SUB_PHASE_ID, SUB_PHASE_INDEX,
      START_ZONE, CORNER_END_ZONE, CORNER_TYPE, FREE_KICK_END_ZONE, FREE_KICK_TYPE,
      THROW_IN_END_ZONE, THROW_IN_TYPE, GOAL_KICK_END_ZONE, GOAL_KICK_TYPE,
      BALL_TRAJECTORY, MAIN_EVENT_PLAYER_ID, MAIN_EVENT_OUTCOME, PASS_RECEIVER_ID,
      FIRST_TOUCH_PLAYER_ID, FIRST_TOUCH_WON, INDIRECT_HEADER,
      SECOND_TOUCH_PLAYER_ID, SECOND_TOUCH_WON, SECOND_TOUCH_END_ZONE,
      AGGREGATES, RAW_PHASE, SOURCE_FORMAT, INGESTION_RUN_ID
    ) VALUES (
      s.MATCH_ID, s.ITERATION_ID, s.SET_PIECE_ID, s.PHASE_INDEX, s.SQUAD_ID,
      s.SET_PIECE_CATEGORY, s.ADJ_SET_PIECE_CATEGORY, s.SET_PIECE_EXECUTION_TYPE,
      s.START_TIME_IN_SEC, s.END_TIME_IN_SEC, s.SUB_PHASE_ID, s.SUB_PHASE_INDEX,
      s.START_ZONE, s.CORNER_END_ZONE, s.CORNER_TYPE, s.FREE_KICK_END_ZONE, s.FREE_KICK_TYPE,
      s.THROW_IN_END_ZONE, s.THROW_IN_TYPE, s.GOAL_KICK_END_ZONE, s.GOAL_KICK_TYPE,
      s.BALL_TRAJECTORY, s.MAIN_EVENT_PLAYER_ID, s.MAIN_EVENT_OUTCOME, s.PASS_RECEIVER_ID,
      s.FIRST_TOUCH_PLAYER_ID, s.FIRST_TOUCH_WON, s.INDIRECT_HEADER,
      s.SECOND_TOUCH_PLAYER_ID, s.SECOND_TOUCH_WON, s.SECOND_TOUCH_END_ZONE,
      PARSE_JSON(s.AGGREGATES), PARSE_JSON(s.RAW_PHASE), 'SET_PIECES_API', s.INGESTION_RUN_ID
    )
"""


def _phases(response: Any) -> list[dict[str, Any]]:
    value = response.get("data", response) if isinstance(response, dict) else []
    return value if isinstance(value, list) else []


def _json(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=True) if value is not None else None


def payload_row(phases: list[dict[str, Any]], match_id: int, iteration_id: int | None,
                run_id: int | None = None) -> list[dict[str, Any]]:
    """One dict per sub-phase, keyed by the uppercase SET_PIECES column names."""
    rows: list[dict[str, Any]] = []
    for phase in phases:
        for sub in phase.get("setPieceSubPhase") or []:
            rows.append({
                "MATCH_ID": int(match_id),
                "ITERATION_ID": int(iteration_id) if iteration_id is not None else None,
                "SET_PIECE_ID": phase.get("id"),
                "PHASE_INDEX": phase.get("phaseIndex"),
                "SQUAD_ID": phase.get("squadId"),
                "SET_PIECE_CATEGORY": phase.get("setPieceCategory"),
                "ADJ_SET_PIECE_CATEGORY": phase.get("adjSetPieceCategory"),
                "SET_PIECE_EXECUTION_TYPE": phase.get("setPieceExecutionType"),
                "START_TIME_IN_SEC": phase.get("startTimeInSec"),
                "END_TIME_IN_SEC": phase.get("endTimeInSec"),
                "SUB_PHASE_ID": sub.get("id"),
                "SUB_PHASE_INDEX": sub.get("index"),
                "START_ZONE": sub.get("startZone"),
                "CORNER_END_ZONE": sub.get("cornerEndZone"),
                "CORNER_TYPE": sub.get("cornerType"),
                "FREE_KICK_END_ZONE": sub.get("freeKickEndZone"),
                "FREE_KICK_TYPE": sub.get("freeKickType"),
                "THROW_IN_END_ZONE": sub.get("throwInEndZone"),
                "THROW_IN_TYPE": sub.get("throwInType"),
                "GOAL_KICK_END_ZONE": sub.get("goalKickEndZone"),
                "GOAL_KICK_TYPE": sub.get("goalKickType"),
                "BALL_TRAJECTORY": sub.get("ballTrajectory"),
                "MAIN_EVENT_PLAYER_ID": sub.get("mainEventPlayerId"),
                "MAIN_EVENT_OUTCOME": sub.get("mainEventOutcome"),
                "PASS_RECEIVER_ID": sub.get("passReceiverId"),
                "FIRST_TOUCH_PLAYER_ID": sub.get("firstTouchPlayerId"),
                "FIRST_TOUCH_WON": sub.get("firstTouchWon"),
                "INDIRECT_HEADER": sub.get("indirectHeader"),
                "SECOND_TOUCH_PLAYER_ID": sub.get("secondTouchPlayerId"),
                "SECOND_TOUCH_WON": sub.get("secondTouchWon"),
                "SECOND_TOUCH_END_ZONE": sub.get("secondTouchEndZone"),
                "AGGREGATES": _json(sub.get("aggregates")),
                "RAW_PHASE": json.dumps(phase, ensure_ascii=True),
                "INGESTION_RUN_ID": run_id,
            })
    return rows


def upsert(rows: list[dict[str, Any]], conn=None) -> None:
    """Bulk-load one match's rows via a TEMPORARY stage table, then MERGE.

    A per-row MERGE (viable for load_match_info.py's one-row-per-match table)
    doesn't scale here: this table is one-row-per-sub-phase, so a single
    match can carry 50-100+ rows, and a full historical backfill is ~35k
    matches -- see module docstring.
    """
    if not rows:
        return
    owns_connection = conn is None
    conn = conn or get_connection()
    try:
        df = pd.DataFrame(rows, columns=COLUMNS)
        with conn.cursor() as cur:
            cur.execute(_STAGE_DDL)
            cur.execute(f"TRUNCATE TABLE {STAGE_TABLE}")

        success, _, _, _ = write_pandas(
            conn=conn, df=df, table_name=STAGE_TABLE_NAME,
            database=config.SNOWFLAKE_DATABASE, schema=config.SNOWFLAKE_SCHEMA,
            overwrite=False, auto_create_table=False,
        )
        if not success:
            raise RuntimeError("write_pandas reported failure staging SET_PIECES rows")

        with conn.cursor() as cur:
            cur.execute(_MERGE_SQL)
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def fetch_and_load(match_id: int, iteration_id: int | None, run_id: int | None = None, conn=None) -> None:
    phases = _phases(api.get_match_set_pieces(match_id))
    rows = payload_row(phases, match_id, iteration_id, run_id)
    if conn is None:
        upsert(rows)
    else:
        upsert(rows, conn=conn)


def missing_set_piece_matches(limit: int | None = None, lane: int = 0, lanes: int = 1,
                              iteration_ids: set[int] | None = None) -> list[tuple[int, int | None]]:
    """Matches in EVENTS with no SET_PIECES rows yet.

    ``iteration_ids``, when given, scopes the candidate set to those
    iterations (e.g. {2114} for Championship 26/27) -- mirrors
    backfill_historical_match_events.py's --iteration-ids convention.
    Without it, candidates span every iteration ever loaded into EVENTS
    (~35k matches), which is the right scope for a full historical backfill
    but far too broad for a scheduled/competition-scoped sync.

    Note: a match with genuinely zero set-piece phases (no corners, FKs,
    throw-ins or goal kicks -- vanishingly rare for a full match, but
    possible for short/abandoned ones) writes no rows and is therefore
    re-attempted on every backfill run. This mirrors load_match_events.py's
    accepted behaviour for "no event data" matches (also retried every run)
    rather than adding a sentinel-row mechanism this codebase doesn't
    otherwise use.
    """
    if lanes < 1 or lane < 0 or lane >= lanes:
        raise ValueError("lane must be in the range 0 <= lane < lanes")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            scope = ""
            if iteration_ids:
                ids = ", ".join(str(int(i)) for i in sorted(iteration_ids))
                scope = f" AND e.ITERATION_ID IN ({ids})"
            sql = f"""
                SELECT e.MATCH_ID, MAX(e.ITERATION_ID) ITERATION_ID
                FROM CAFC_DB.IMPECT_RAW.EVENTS e
                LEFT JOIN {TABLE} sp ON sp.MATCH_ID=e.MATCH_ID
                WHERE sp.MATCH_ID IS NULL
                  AND MOD(e.MATCH_ID, %(lanes)s) = %(lane)s
                  {scope}
                GROUP BY e.MATCH_ID
                ORDER BY e.MATCH_ID
            """
            if limit is not None:
                sql += " LIMIT %(limit)s"
                cur.execute(sql, {"limit": limit, "lane": lane, "lanes": lanes})
            else:
                cur.execute(sql, {"lane": lane, "lanes": lanes})
            return [(int(row[0]), int(row[1]) if row[1] is not None else None) for row in cur.fetchall()]
    finally:
        conn.close()


def backfill(*, limit: int | None, pause_seconds: float, dry_run: bool,
             lane: int = 0, lanes: int = 1, iteration_ids: set[int] | None = None) -> dict[str, int]:
    matches = missing_set_piece_matches(limit, lane=lane, lanes=lanes, iteration_ids=iteration_ids)
    if dry_run:
        return {"candidates": len(matches), "loaded": 0, "failed": 0}
    loaded = failed = 0
    conn = get_connection()
    try:
        for index, (match_id, iteration_id) in enumerate(matches):
            if index:
                time.sleep(pause_seconds)
            try:
                fetch_and_load(match_id, iteration_id, conn=conn)
                loaded += 1
            except Exception as exc:
                failed += 1
                print(f"match {match_id}: {exc}", file=sys.stderr)
            processed = index + 1
            if processed % 100 == 0 or processed == len(matches):
                print(f"lane {lane}/{lanes} progress: {processed}/{len(matches)} loaded={loaded} failed={failed}")
    finally:
        conn.close()
    return {"candidates": len(matches), "loaded": loaded, "failed": failed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backfill-events", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--pause-seconds", type=float, default=0.35)
    parser.add_argument("--lane", type=int, default=0, help="Deterministic lane number (default: 0).")
    parser.add_argument("--lanes", type=int, default=1, help="Total parallel lanes (default: 1).")
    parser.add_argument("--iteration-ids", type=str, default=None,
                        help="Comma-separated iteration ids to scope the backfill to (default: all iterations in EVENTS).")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.backfill_events:
        raise SystemExit("Choose --backfill-events")
    ids = {int(v) for v in args.iteration_ids.split(",")} if args.iteration_ids and args.iteration_ids.strip() else None
    print(backfill(limit=args.limit, pause_seconds=args.pause_seconds, dry_run=args.dry_run,
                   lane=args.lane, lanes=args.lanes, iteration_ids=ids))
