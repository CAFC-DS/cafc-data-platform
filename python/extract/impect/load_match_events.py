"""
Fetch raw match events from the Impect API and load into Snowflake.
Table: CAFC_DB.IMPECT_RAW.EVENTS

Confirmed live before writing this loader (see snowflake/ddl/20260724_impect_events.sql):
matches predating Impect's event-level ("packing plus") tracking return a
permanent 400, not empty data. For competition 41 (EFL Championship), that
boundary is season 21/22 -- 17/18 through 20/21 have no event data. This is
NOT assumed to hold for every competition; re-verify before trusting a new
competition's coverage.

This is a build-and-test loader, not a full historical backfill runner: it
is designed to be pointed at a specific iteration or a specific list of
match ids so it can be proven correct on a small sample before anyone runs
it against years of matches. Idempotent -- matches already present in
IMPECT_RAW.EVENTS are skipped unless --force is passed, so a partial or
repeated run never double-inserts.

Usage:
    # Test on a handful of specific matches:
    python load_match_events.py --match-ids 206530,206531,206532

    # All matches in one iteration (one Impect competition-season):
    python load_match_events.py --iteration-id 1410

    # Re-fetch matches even if already loaded:
    python load_match_events.py --iteration-id 1410 --force
"""
import argparse
import sys

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

import config
import impect_api as api
from snowflake_loader import get_connection

TABLE_NAME = "EVENTS"

# Nested groups kept as-is (VARIANT) rather than flattened -- see DDL comment.
_VARIANT_FIELDS = {
    "start": "START_DETAIL",
    "end": "END_DETAIL",
    "duel": "DUEL_DETAIL",
    "shot": "SHOT_DETAIL",
    "pass": "PASS_DETAIL",
    "dribble": "DRIBBLE_DETAIL",
    "setPiece": "SET_PIECE_DETAIL",
    "pxT": "PXT_DETAIL",
    "formation": "FORMATION_DETAIL",
    "opponent": "OPPONENT_DETAIL",
}


def _open_run(cur, triggered_by: str) -> int:
    cur.execute(
        """
        INSERT INTO CAFC_DB.CORE.INGESTION_RUNS (SOURCE_SYSTEM, TRIGGERED_BY, NOTES)
        VALUES (%(src)s, %(by)s, %(notes)s)
        """,
        {"src": "IMPECT", "by": triggered_by, "notes": "impect match events extractor run"},
    )
    cur.execute("SELECT MAX(RUN_ID) FROM CAFC_DB.CORE.INGESTION_RUNS")
    return int(cur.fetchone()[0])


def _close_run(cur, run_id: int, status: str, notes: str = "") -> None:
    cur.execute(
        """
        UPDATE CAFC_DB.CORE.INGESTION_RUNS
           SET STATUS = %(status)s, FINISHED_AT = CURRENT_TIMESTAMP(), NOTES = %(notes)s
         WHERE RUN_ID = %(rid)s
        """,
        {"status": status, "notes": notes, "rid": run_id},
    )


def _already_loaded_match_ids(cur) -> set:
    cur.execute("SELECT DISTINCT MATCH_ID FROM CAFC_DB.IMPECT_RAW.EVENTS")
    return {row[0] for row in cur.fetchall()}


def _flatten_event(match_id: int, iteration_id, event: dict, run_id: int) -> dict:
    game_time = event.get("gameTime") or {}
    player = event.get("player") or {}

    row = {
        "MATCH_ID": match_id,
        "ITERATION_ID": iteration_id,
        "EVENT_ID": event.get("id"),
        "EVENT_INDEX": event.get("index"),
        "SEQUENCE_INDEX": event.get("sequenceIndex"),
        "PERIOD_ID": event.get("periodId"),
        "GAME_TIME": game_time.get("gameTime"),
        "GAME_TIME_IN_SEC": game_time.get("gameTimeInSec"),
        "SQUAD_ID": event.get("squadId"),
        "CURRENT_ATTACKING_SQUAD_ID": event.get("currentAttackingSquadId"),
        "PLAYER_ID": player.get("id"),
        "PLAYER_POSITION": player.get("position"),
        "PLAYER_POSITION_SIDE": player.get("positionSide"),
        "ACTION_TYPE": event.get("actionType"),
        "ACTION": event.get("action"),
        "PHASE": event.get("phase"),
        "BODY_PART": event.get("bodyPart"),
        "BODY_PART_EXTENDED": event.get("bodyPartExtended"),
        "PREVIOUS_PASS_HEIGHT": event.get("previousPassHeight"),
        "DURATION": event.get("duration"),
        "OPPONENTS": event.get("opponents"),
        "PRESSURE": event.get("pressure"),
        "DISTANCE_TO_GOAL": event.get("distanceToGoal"),
        "DISTANCE_TO_OPPONENT": event.get("distanceToOpponent"),
        "RESULT": event.get("result"),
        "PRESSING_PLAYER_ID": event.get("pressingPlayerId"),
        "FOULED_PLAYER_ID": event.get("fouledPlayerId"),
        "INFERRED_SET_PIECE": event.get("inferredSetPiece"),
        "RAW_EVENT": _to_json(event),
        "INGESTION_RUN_ID": run_id,
    }
    for src_key, col in _VARIANT_FIELDS.items():
        row[col] = _to_json(event.get(src_key))
    return row


def _to_json(value):
    import json
    if value is None:
        return None
    return json.dumps(value, default=str)


def fetch_events_for_match(match_id: int, iteration_id, run_id: int) -> list[dict]:
    """
    Returns a list of flattened event rows for one match, or [] if the match
    has no event data (confirmed 400 case -- see module docstring). Any other
    error propagates.
    """
    try:
        response = api.get_match_events(match_id)
    except Exception as e:
        if "does not have packing plus data" in str(e) or "400 error" in str(e):
            print(f"  match {match_id}: no event data available, skipping")
            return []
        raise

    events = response.get("data", response) if isinstance(response, dict) else response
    if not events:
        return []
    return [_flatten_event(match_id, iteration_id, ev, run_id) for ev in events]


_VARIANT_COLUMNS = list(_VARIANT_FIELDS.values()) + ["RAW_EVENT"]


def load_events(rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    conn = get_connection()
    try:
        # write_pandas lands a Python JSON string into a VARIANT column as a
        # literal quoted string scalar, not a parsed object (confirmed live:
        # PXT_DETAIL:team returned NULL until re-parsed). Bulk-fix with one
        # UPDATE per VARIANT column scoped to this run rather than looping
        # PARSE_JSON per row on insert -- keeps the fast bulk write_pandas
        # path while still landing real parsed VARIANT data.
        success, _, num_rows, _ = write_pandas(
            conn=conn,
            df=df,
            table_name=TABLE_NAME,
            database=config.SNOWFLAKE_DATABASE,
            schema=config.SNOWFLAKE_SCHEMA,
            overwrite=False,
            auto_create_table=False,
        )
        if not success:
            raise RuntimeError(f"write_pandas reported failure loading {TABLE_NAME}")

        run_id = int(df["INGESTION_RUN_ID"].iloc[0])
        with conn.cursor() as cur:
            for col in _VARIANT_COLUMNS:
                cur.execute(
                    f"""
                    UPDATE {config.SNOWFLAKE_DATABASE}.{config.SNOWFLAKE_SCHEMA}.{TABLE_NAME}
                       SET {col} = PARSE_JSON({col}::STRING)
                     WHERE INGESTION_RUN_ID = %(run_id)s AND {col} IS NOT NULL
                    """,
                    {"run_id": run_id},
                )
        conn.commit()
        return num_rows
    finally:
        conn.close()


def run(iteration_id: int = None, match_ids: list[int] = None, force: bool = False,
        triggered_by: str = "load_match_events.py") -> dict:
    if not iteration_id and not match_ids:
        raise ValueError("Must supply --iteration-id or --match-ids")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            run_id = _open_run(cur, triggered_by)
            conn.commit()

            already_loaded = set() if force else _already_loaded_match_ids(cur)

        targets: list[tuple[int, int]] = []  # (match_id, iteration_id)
        if match_ids:
            for mid in match_ids:
                targets.append((mid, iteration_id))
        else:
            matches = api.get_matches(iteration_id).get("data", [])
            targets = [(m["id"], iteration_id) for m in matches]

        print(f"{len(targets)} candidate match(es); {len(already_loaded)} already loaded.")

        total_events = 0
        matches_loaded = 0
        matches_skipped_existing = 0
        matches_no_data = 0

        for match_id, it_id in targets:
            if match_id in already_loaded:
                matches_skipped_existing += 1
                continue
            rows = fetch_events_for_match(match_id, it_id, run_id)
            if not rows:
                matches_no_data += 1
                continue
            n = load_events(rows)
            total_events += n
            matches_loaded += 1
            print(f"  match {match_id}: loaded {n} events")

        with conn.cursor() as cur:
            _close_run(
                cur, run_id, "SUCCESS",
                notes=f"matches_loaded={matches_loaded} events={total_events} "
                      f"skipped_existing={matches_skipped_existing} no_data={matches_no_data}",
            )
            conn.commit()

        summary = {
            "matches_loaded": matches_loaded,
            "events_loaded": total_events,
            "matches_skipped_existing": matches_skipped_existing,
            "matches_no_data": matches_no_data,
        }
        print(f"Done: {summary}")
        return summary
    finally:
        conn.close()


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--iteration-id", type=int, default=None,
                   help="Pull events for every match in this Impect iteration.")
    p.add_argument("--match-ids", type=str, default=None,
                   help="Comma-separated Impect match ids to pull directly (test/backfill-slice mode).")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch and re-insert matches even if already present in IMPECT_RAW.EVENTS.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    match_ids = [int(x) for x in args.match_ids.split(",")] if args.match_ids else None
    try:
        run(iteration_id=args.iteration_id, match_ids=match_ids, force=args.force)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
