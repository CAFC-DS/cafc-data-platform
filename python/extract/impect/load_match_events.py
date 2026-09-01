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

    # Full historical backfill: every iteration with event-level data
    # (DATAVERSION != 'V1' on IMPECT_RAW.ITERATIONS), looped until exhausted.
    # Intended to run for days unattended -- one bad iteration is logged and
    # skipped rather than killing the whole run. Split across N terminals/
    # processes with --lane/--lanes for local parallelism (each process
    # takes a disjoint slice of the same iteration list by iteration_id mod
    # lanes, so lanes never duplicate work):
    python load_match_events.py --all-iterations --lane 0 --lanes 4
    python load_match_events.py --all-iterations --lane 1 --lanes 4
    ...

    # See what --all-iterations would process without pulling any data:
    python load_match_events.py --all-iterations --dry-run
"""
import argparse
import sys
import time

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

import config
import impect_api as api
import load_match_info as match_info
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


_kpi_dictionary_cache: dict = {}


def get_kpi_dictionary() -> dict:
    """
    {kpiId: name} for all event-level KPIs (GET /kpis/event), e.g.
    {1406: "SHOT_XG", ...}. 103 rows, effectively static -- fetched once per
    process and cached, not once per match.
    """
    if not _kpi_dictionary_cache:
        resp = api.get_event_kpi_dictionary()
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        _kpi_dictionary_cache.update({row["id"]: row["name"] for row in data})
    return _kpi_dictionary_cache


def fetch_event_kpis_grouped(match_id: int) -> dict:
    """
    {eventId: [{"position": ..., "playerId": ..., "<kpiName>": value, ...}, ...]}

    Confirmed live: ~10 scoring rows per event (the primary player plus other
    on-pitch players' attribution for the same event, e.g. every outfield
    player gets a DEF_PXT_SHOT row for one shot). Grouped as a list per event
    rather than collapsed to one dict, since collapsing would silently drop
    all but one player's rows.
    """
    try:
        resp = api.get_match_event_kpis(match_id)
    except Exception as e:
        if "does not have packing plus data" in str(e) or "400 error" in str(e):
            return {}
        raise
    rows = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not rows:
        return {}

    id_to_name = get_kpi_dictionary()
    grouped: dict = {}
    # Rows are one-KPI-per-row for a given (eventId, position, playerId);
    # collapse to one entry per (eventId, position, playerId) with every KPI
    # as a key, matching impectPy's pivot semantics.
    entries: dict = {}
    for r in rows:
        key = (r["eventId"], r.get("position"), r.get("playerId"))
        entry = entries.setdefault(key, {"position": r.get("position"), "playerId": r.get("playerId")})
        name = id_to_name.get(r["kpiId"], f"kpi_{r['kpiId']}")
        entry[name] = r["value"]
    for (event_id, _pos, _pid), entry in entries.items():
        grouped.setdefault(event_id, []).append(entry)
    return grouped


def fetch_events_for_match(match_id: int, iteration_id, run_id: int, include_kpis: bool = True) -> list[dict]:
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

    kpis_by_event = fetch_event_kpis_grouped(match_id) if include_kpis else {}
    rows = []
    for ev in events:
        row = _flatten_event(match_id, iteration_id, ev, run_id)
        row["EVENT_KPIS"] = _to_json(kpis_by_event.get(ev.get("id")))
        rows.append(row)
    return rows


_VARIANT_COLUMNS = list(_VARIANT_FIELDS.values()) + ["RAW_EVENT", "EVENT_KPIS"]


def load_events(rows: list[dict], *, replace_existing_match: bool = False) -> int:
    """Load one match's events.

    ``replace_existing_match`` makes a re-pull safe when Impect recalculates a
    match and changes its event ids.  New rows are written first; only after
    the write and VARIANT conversion succeed are older rows for that match
    removed.  A failed re-pull therefore leaves the previous usable copy in
    place rather than creating a gap in the raw landing table.
    """
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
            if replace_existing_match:
                match_id = int(df["MATCH_ID"].iloc[0])
                cur.execute(
                    f"""
                    DELETE FROM {config.SNOWFLAKE_DATABASE}.{config.SNOWFLAKE_SCHEMA}.{TABLE_NAME}
                     WHERE MATCH_ID = %(match_id)s
                       AND COALESCE(INGESTION_RUN_ID, -1) != %(run_id)s
                    """,
                    {"match_id": match_id, "run_id": run_id},
                )
        conn.commit()
        return num_rows
    finally:
        conn.close()


def backfill_kpis(match_ids: list[int]) -> dict:
    """
    Attach EVENT_KPIS to events already sitting in IMPECT_RAW.EVENTS, without
    re-inserting or re-fetching event rows. Uses a session-scoped TEMPORARY
    table + MERGE rather than a Python-side UPDATE per event -- at 1.5M+
    events across 557 matches, a per-row UPDATE would be prohibitively slow.
    """
    conn = get_connection()
    try:
        stage_table = f"{config.SNOWFLAKE_SCHEMA}.EVENT_KPIS_STAGE"
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TEMPORARY TABLE IF NOT EXISTS {stage_table} (
                    MATCH_ID NUMBER, EVENT_ID NUMBER, EVENT_KPIS_JSON VARCHAR
                )
            """)

        matches_updated = 0
        events_updated_total = 0
        matches_no_data = 0

        for match_id in match_ids:
            kpis_by_event = fetch_event_kpis_grouped(match_id)
            if not kpis_by_event:
                matches_no_data += 1
                print(f"  match {match_id}: no event-kpi data available, skipping")
                continue

            rows = [
                {"MATCH_ID": match_id, "EVENT_ID": event_id, "EVENT_KPIS_JSON": _to_json(entries)}
                for event_id, entries in kpis_by_event.items()
            ]
            df = pd.DataFrame(rows)

            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {stage_table}")
            success, _, n, _ = write_pandas(
                conn=conn, df=df, table_name="EVENT_KPIS_STAGE",
                database=config.SNOWFLAKE_DATABASE, schema=config.SNOWFLAKE_SCHEMA,
                overwrite=False, auto_create_table=False,
            )
            if not success:
                raise RuntimeError(f"write_pandas reported failure staging KPIs for match {match_id}")

            with conn.cursor() as cur:
                cur.execute(f"""
                    MERGE INTO {config.SNOWFLAKE_DATABASE}.{config.SNOWFLAKE_SCHEMA}.{TABLE_NAME} t
                    USING {config.SNOWFLAKE_DATABASE}.{stage_table} s
                       ON t.MATCH_ID = s.MATCH_ID AND t.EVENT_ID = s.EVENT_ID
                    WHEN MATCHED THEN UPDATE SET t.EVENT_KPIS = PARSE_JSON(s.EVENT_KPIS_JSON)
                """)
            conn.commit()

            matches_updated += 1
            events_updated_total += n
            print(f"  match {match_id}: attached KPIs to {n} events")

        summary = {
            "matches_updated": matches_updated,
            "events_updated": events_updated_total,
            "matches_no_data": matches_no_data,
        }
        print(f"Done: {summary}")
        return summary
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
            n = load_events(rows, replace_existing_match=force)
            match_info.fetch_and_load(match_id, it_id, run_id)
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


def _all_loaded_match_ids() -> list[int]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            return sorted(_already_loaded_match_ids(cur))
    finally:
        conn.close()


def target_iterations(lane: int = 0, lanes: int = 1) -> list[int]:
    """
    Every iteration with event-level data available: DATAVERSION != 'V1' on
    IMPECT_RAW.ITERATIONS (confirmed live 2026-08-12 -- V2/V3/V4 all carry
    real event data, V1 doesn't; see docs/raw-event-data-usage-guide.md).
    No gender filter -- raw lands everything, dbt staging filters women's
    competitions downstream, per platform convention.

    Sharded by `iteration_id % lanes == lane` so multiple local processes
    can run the same target list concurrently without duplicating work.
    Sorted ascending so re-running after a crash resumes in the same order
    (not required for correctness -- run() re-checks already-loaded matches
    regardless -- just keeps progress easy to reason about across restarts).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ID FROM CAFC_DB.IMPECT_RAW.ITERATIONS WHERE DATAVERSION != 'V1' ORDER BY ID"
            )
            all_ids = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    return [i for i in all_ids if i % lanes == lane]


def run_all_iterations(iteration_ids: list[int], force: bool = False,
                        triggered_by: str = "load_match_events.py --all-iterations") -> None:
    """
    Loop `run()` across every target iteration, forever resilient to a
    single bad iteration -- this is meant to run unattended for days, so one
    iteration raising (a genuinely new API shape, a transient Snowflake
    blip that survived make_request's own retries, etc.) is logged and
    skipped rather than killing every iteration queued behind it.
    """
    start = time.time()
    for n, iteration_id in enumerate(iteration_ids, start=1):
        elapsed_h = (time.time() - start) / 3600
        print(f"\n=== iteration {iteration_id} ({n}/{len(iteration_ids)}, {elapsed_h:.1f}h elapsed) ===")
        try:
            run(iteration_id=iteration_id, force=force, triggered_by=triggered_by)
        except Exception as e:
            print(f"  iteration {iteration_id} FAILED, skipping: {e}")
    print(f"\nAll {len(iteration_ids)} target iteration(s) attempted in {(time.time() - start) / 3600:.1f}h.")


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--iteration-id", type=int, default=None,
                   help="Pull events for every match in this Impect iteration.")
    p.add_argument("--match-ids", type=str, default=None,
                   help="Comma-separated Impect match ids to pull directly (test/backfill-slice mode).")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch and re-insert matches even if already present in IMPECT_RAW.EVENTS.")
    p.add_argument("--backfill-kpis", action="store_true",
                   help="Attach EVENT_KPIS to matches already loaded, without re-inserting events. "
                        "Combine with --match-ids to scope it, or omit to backfill every loaded match.")
    p.add_argument("--all-iterations", action="store_true",
                   help="Loop every iteration with event-level data (DATAVERSION != 'V1'), "
                        "resilient to per-iteration failures. Intended for a long-running "
                        "unattended process, not a single CI job.")
    p.add_argument("--lane", type=int, default=0,
                   help="With --all-iterations: this process's lane index (0-based).")
    p.add_argument("--lanes", type=int, default=1,
                   help="With --all-iterations: total number of parallel lanes.")
    p.add_argument("--dry-run", action="store_true",
                   help="With --all-iterations: print the target iteration list and exit.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    match_ids = [int(x) for x in args.match_ids.split(",")] if args.match_ids else None
    try:
        if args.backfill_kpis:
            backfill_kpis(match_ids or _all_loaded_match_ids())
            sys.exit(0)
        if args.all_iterations:
            ids = target_iterations(lane=args.lane, lanes=args.lanes)
            print(f"Lane {args.lane}/{args.lanes}: {len(ids)} target iteration(s): {ids}")
            if args.dry_run:
                sys.exit(0)
            run_all_iterations(ids, force=args.force)
            sys.exit(0)
        run(iteration_id=args.iteration_id, match_ids=match_ids, force=args.force)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
