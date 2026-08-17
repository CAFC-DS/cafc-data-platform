"""Discover and resumably backfill all accessible IMPECT men's event data.

This is the historical companion to ``sync_recent_match_events.py``. Run
discovery once (or safely repeat it) to queue every completed match in every
accessible non-women's V2+ iteration. Then run bounded processing batches
until the queue is empty. Successes are never re-fetched; failures retry with
backoff, and IMPECT's known permanent no-event response is terminal.

Examples:
    python backfill_historical_match_events.py --discover
    python backfill_historical_match_events.py --process --batch-size 25
    python backfill_historical_match_events.py --status
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

import config
import impect_api as api
import load_match_events as events
from snowflake_loader import get_connection


QUEUE_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE"
DISCOVERY_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_DISCOVERY"
QUEUE_SCHEMA = "CORE"
TERMINAL_STATUSES = ("SUCCESS", "NO_EVENT_DATA")


def _records(response: Any) -> list[dict]:
    return response.get("data", []) if isinstance(response, dict) else []


def _nested(row: dict, key: str) -> Any:
    current: Any = row
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _is_womens(iteration: dict) -> bool:
    gender = str(_nested(iteration, "competition.gender") or "").upper()
    return "FEMALE" in gender or "WOMEN" in gender


def _has_event_coverage(iteration: dict) -> bool:
    """V1 is IMPECT's explicit pre-event-data version; V2+ is eligible."""
    return str(iteration.get("dataVersion") or iteration.get("dataversion") or "").upper() != "V1"


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _target_iterations(iteration_ids: set[int] | None = None) -> list[dict]:
    targets = [
        iteration for iteration in _records(api.get_iterations())
        if not _is_womens(iteration) and _has_event_coverage(iteration)
    ]
    return [iteration for iteration in targets if iteration_ids is None or int(iteration["id"]) in iteration_ids]


def _already_loaded_match_ids() -> set[int]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT MATCH_ID FROM CAFC_DB.IMPECT_RAW.EVENTS")
            return {int(row[0]) for row in cur.fetchall()}
    finally:
        conn.close()


def _stage_and_merge(rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"USE SCHEMA {config.SNOWFLAKE_DATABASE}.{QUEUE_SCHEMA}")
            cur.execute(
                """
                CREATE TEMPORARY TABLE IMPECT_EVENT_BACKFILL_QUEUE_STAGE (
                  MATCH_ID NUMBER, ITERATION_ID NUMBER, COMPETITION_NAME VARCHAR,
                  SEASON VARCHAR, SCHEDULED_AT TIMESTAMP_NTZ, ALREADY_LOADED BOOLEAN
                )
                """
            )
        success, _, count, output = write_pandas(
            conn=conn, df=df, table_name="IMPECT_EVENT_BACKFILL_QUEUE_STAGE",
            database=config.SNOWFLAKE_DATABASE, schema=QUEUE_SCHEMA,
            overwrite=False, auto_create_table=False,
        )
        if not success:
            raise RuntimeError(f"Could not stage backfill queue rows: {output}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                MERGE INTO {QUEUE_TABLE} t
                USING IMPECT_EVENT_BACKFILL_QUEUE_STAGE s ON t.MATCH_ID=s.MATCH_ID
                WHEN MATCHED THEN UPDATE SET
                  ITERATION_ID=s.ITERATION_ID, COMPETITION_NAME=s.COMPETITION_NAME,
                  SEASON=s.SEASON, SCHEDULED_AT=s.SCHEDULED_AT, UPDATED_AT=CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT (
                  MATCH_ID, ITERATION_ID, COMPETITION_NAME, SEASON, SCHEDULED_AT, STATUS,
                  EVENT_COUNT, COMPLETED_AT
                ) VALUES (
                  s.MATCH_ID, s.ITERATION_ID, s.COMPETITION_NAME, s.SEASON, s.SCHEDULED_AT,
                  IFF(s.ALREADY_LOADED, 'SUCCESS', 'PENDING'),
                  IFF(s.ALREADY_LOADED, 0, NULL), IFF(s.ALREADY_LOADED, CURRENT_TIMESTAMP(), NULL)
                )
                """
            )
        conn.commit()
        return count
    finally:
        conn.close()


def _register_discovery_targets(iterations: list[dict]) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for iteration in iterations:
                cur.execute(
                    f"""
                    MERGE INTO {DISCOVERY_TABLE} t
                    USING (SELECT %(iteration_id)s AS ITERATION_ID) s ON t.ITERATION_ID=s.ITERATION_ID
                    WHEN MATCHED THEN UPDATE SET COMPETITION_NAME=%(competition_name)s, SEASON=%(season)s, UPDATED_AT=CURRENT_TIMESTAMP()
                    WHEN NOT MATCHED THEN INSERT (ITERATION_ID, COMPETITION_NAME, SEASON, STATUS)
                    VALUES (%(iteration_id)s, %(competition_name)s, %(season)s, 'PENDING')
                    """,
                    {"iteration_id": int(iteration["id"]), "competition_name": _nested(iteration, "competition.name"),
                     "season": iteration.get("season")},
                )
        conn.commit()
    finally:
        conn.close()


def _refresh_discovery_targets(iteration_ids: set[int]) -> None:
    """Re-catalogue an active season whose fixture list has grown since first discovery."""
    if not iteration_ids:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {DISCOVERY_TABLE}
                       SET STATUS='PENDING', CLAIMED_BY=NULL, CLAIMED_AT=NULL,
                           LAST_ERROR=NULL, UPDATED_AT=CURRENT_TIMESTAMP()
                     WHERE ITERATION_ID IN ({', '.join(str(value) for value in sorted(iteration_ids))})"""
            )
        conn.commit()
    finally:
        conn.close()


def _claim_discovery_batch(batch_size: int, iteration_ids: set[int] | None = None) -> tuple[str, list[dict]]:
    claim = str(uuid.uuid4())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""UPDATE {DISCOVERY_TABLE} SET STATUS='PENDING', CLAIMED_BY=NULL, CLAIMED_AT=NULL
                            WHERE STATUS='RUNNING' AND CLAIMED_AT < DATEADD(hour, -12, CURRENT_TIMESTAMP())""")
            scope = ""
            params: dict[str, Any] = {"claim": claim, "batch_size": batch_size}
            if iteration_ids:
                scope = "AND ITERATION_ID IN (" + ", ".join(str(value) for value in sorted(iteration_ids)) + ")"
            cur.execute(
                f"""
                UPDATE {DISCOVERY_TABLE} SET STATUS='RUNNING', CLAIMED_BY=%(claim)s, CLAIMED_AT=CURRENT_TIMESTAMP(), UPDATED_AT=CURRENT_TIMESTAMP()
                 WHERE ITERATION_ID IN (
                   SELECT ITERATION_ID FROM {DISCOVERY_TABLE} WHERE STATUS IN ('PENDING', 'FAILED')
                   {scope}
                   ORDER BY ITERATION_ID LIMIT %(batch_size)s
                 )
                """, params,
            )
            cur.execute(f"SELECT ITERATION_ID, COMPETITION_NAME, SEASON FROM {DISCOVERY_TABLE} WHERE CLAIMED_BY=%(claim)s ORDER BY ITERATION_ID", {"claim": claim})
            rows = [{"id": int(i), "competition_name": name, "season": season} for i, name, season in cur.fetchall()]
        conn.commit()
        return claim, rows
    finally:
        conn.close()


def _finish_discovery_iterations(results: list[tuple[int, str, int | None, str | None]]) -> None:
    """Finish a discovery batch in one Snowflake session, not one per iteration."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for iteration_id, status, matches, error in results:
                cur.execute(
                    f"""UPDATE {DISCOVERY_TABLE}
                           SET STATUS=%(status)s, DISCOVERED_MATCHES=%(matches)s, LAST_ERROR=%(error)s,
                               CLAIMED_BY=NULL, CLAIMED_AT=NULL, COMPLETED_AT=IFF(%(status)s='SUCCESS', CURRENT_TIMESTAMP(), COMPLETED_AT), UPDATED_AT=CURRENT_TIMESTAMP()
                         WHERE ITERATION_ID=%(iteration_id)s""",
                    {"iteration_id": iteration_id, "status": status, "matches": matches, "error": error[:4000] if error else None},
                )
        conn.commit()
    finally:
        conn.close()


def discover(iteration_batch_size: int, iteration_ids: set[int] | None = None, refresh: bool = False) -> dict[str, int]:
    """Queue a bounded, resumable batch of eligible iteration catalogues."""
    now = datetime.now(timezone.utc)
    all_iterations = _target_iterations(iteration_ids)
    _register_discovery_targets(all_iterations)
    if refresh:
        _refresh_discovery_targets({int(iteration["id"]) for iteration in all_iterations})
    _, claimed = _claim_discovery_batch(iteration_batch_size, iteration_ids)
    if not claimed:
        print("No eligible iteration discovery work remains.")
        return {"eligible_iterations": len(all_iterations), "claimed_iterations": 0, "queue_rows_seen": 0, "failures": 0}
    loaded = _already_loaded_match_ids()
    rows: list[dict] = []
    results: list[tuple[int, str, int | None, str | None]] = []
    failures = 0
    for item in claimed:
        try:
            matches = _records(api.get_matches(item["id"]))
            for match in matches:
                scheduled_at = _as_utc(match.get("scheduledDate"))
                if scheduled_at and scheduled_at > now:
                    continue
                rows.append({"MATCH_ID": int(match["id"]), "ITERATION_ID": item["id"],
                             "COMPETITION_NAME": item["competition_name"], "SEASON": item["season"],
                             "SCHEDULED_AT": scheduled_at, "ALREADY_LOADED": int(match["id"]) in loaded})
            results.append((item["id"], "SUCCESS", len(matches), None))
            print(f"iteration {item['id']}: {len(matches)} match metadata rows")
        except Exception as exc:
            failures += 1
            results.append((item["id"], "FAILED", None, str(exc)))
            print(f"iteration {item['id']}: discovery failed: {exc}", file=sys.stderr)
    staged = _stage_and_merge(rows)
    _finish_discovery_iterations(results)
    summary = {"eligible_iterations": len(all_iterations), "claimed_iterations": len(claimed), "queue_rows_seen": staged, "failures": failures}
    print(f"Discovery batch complete: {summary}")
    return summary


def _claim_batch(batch_size: int, max_attempts: int, iteration_ids: set[int] | None = None) -> tuple[str, list[dict]]:
    claim_token = str(uuid.uuid4())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # A failed or interrupted runner cannot leave work stuck forever.
            scope = ""
            if iteration_ids:
                scope = "AND ITERATION_ID IN (" + ", ".join(str(value) for value in sorted(iteration_ids)) + ")"
            cur.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                   SET STATUS='PENDING', CLAIMED_BY=NULL, CLAIMED_AT=NULL, UPDATED_AT=CURRENT_TIMESTAMP()
                 WHERE STATUS='RUNNING' AND CLAIMED_AT < DATEADD(hour, -12, CURRENT_TIMESTAMP())
                """
            )
            cur.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                   SET STATUS='RUNNING', CLAIMED_BY=%(claim)s, CLAIMED_AT=CURRENT_TIMESTAMP(),
                       ATTEMPT_COUNT=ATTEMPT_COUNT+1, UPDATED_AT=CURRENT_TIMESTAMP()
                 WHERE MATCH_ID IN (
                   SELECT MATCH_ID FROM {QUEUE_TABLE}
                    WHERE STATUS IN ('PENDING', 'FAILED')
                      AND (NEXT_ATTEMPT_AT IS NULL OR NEXT_ATTEMPT_AT <= CURRENT_TIMESTAMP())
                      AND ATTEMPT_COUNT < %(max_attempts)s
                      {scope}
                    ORDER BY SCHEDULED_AT NULLS FIRST, MATCH_ID
                    LIMIT %(batch_size)s
                 )
                """,
                {"claim": claim_token, "max_attempts": max_attempts, "batch_size": batch_size},
            )
            cur.execute(
                f"""
                -- SCHEDULED_AT is used only for ordering.  Some historic provider
                -- records contain timestamps the connector cannot materialise in
                -- Python, so don't fetch it back into the worker.
                SELECT MATCH_ID, ITERATION_ID, COMPETITION_NAME, SEASON
                  FROM {QUEUE_TABLE} WHERE CLAIMED_BY=%(claim)s ORDER BY SCHEDULED_AT NULLS FIRST, MATCH_ID
                """,
                {"claim": claim_token},
            )
            claimed = [
                {"match_id": int(mid), "iteration_id": int(iid), "competition_name": name,
                 "season": season}
                for mid, iid, name, season in cur.fetchall()
            ]
        conn.commit()
        return claim_token, claimed
    finally:
        conn.close()


def _finish_match(match_id: int, status: str, run_id: int, event_count: int | None = None,
                  error: str | None = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {QUEUE_TABLE}
                   SET STATUS=%(status)s, EVENT_COUNT=%(event_count)s, LAST_ERROR=%(error)s,
                       INGESTION_RUN_ID=%(run_id)s, CLAIMED_BY=NULL, CLAIMED_AT=NULL,
                       NEXT_ATTEMPT_AT=IFF(%(status)s='FAILED', DATEADD(hour, 12, CURRENT_TIMESTAMP()), NULL),
                       COMPLETED_AT=IFF(%(status)s IN ('SUCCESS', 'NO_EVENT_DATA'), CURRENT_TIMESTAMP(), COMPLETED_AT),
                       UPDATED_AT=CURRENT_TIMESTAMP()
                 WHERE MATCH_ID=%(match_id)s
                """,
                {"match_id": match_id, "status": status, "event_count": event_count,
                 "error": error[:4000] if error else None, "run_id": run_id},
            )
        conn.commit()
    finally:
        conn.close()


def process(batch_size: int, max_attempts: int, iteration_ids: set[int] | None = None) -> dict[str, int]:
    claim_token, claimed = _claim_batch(batch_size, max_attempts, iteration_ids)
    if not claimed:
        print("No eligible backfill work remains.")
        return {"claimed": 0, "success": 0, "no_event_data": 0, "failed": 0}
    conn = get_connection()
    with conn.cursor() as cur:
        run_id = events._open_run(cur, "backfill_historical_match_events.py")
    conn.commit()
    success = no_event_data = failed = 0
    try:
        for item in claimed:
            try:
                rows = events.fetch_events_for_match(item["match_id"], item["iteration_id"], run_id)
                if not rows:
                    _finish_match(item["match_id"], "NO_EVENT_DATA", run_id, event_count=0)
                    no_event_data += 1
                    continue
                count = events.load_events(rows, replace_existing_match=True)
                _finish_match(item["match_id"], "SUCCESS", run_id, event_count=count)
                success += 1
                print(f"match {item['match_id']}: loaded {count} events")
            except Exception as exc:
                _finish_match(item["match_id"], "FAILED", run_id, error=str(exc))
                failed += 1
                print(f"match {item['match_id']}: FAILED: {exc}", file=sys.stderr)
        with conn.cursor() as cur:
            events._close_run(cur, run_id, "SUCCESS" if not failed else "PARTIAL_SUCCESS",
                              f"claimed={len(claimed)} success={success} no_event_data={no_event_data} failed={failed}")
        conn.commit()
    except Exception:
        with conn.cursor() as cur:
            events._close_run(cur, run_id, "FAILED", "unexpected historical backfill failure")
        conn.commit()
        raise
    finally:
        conn.close()
    return {"claimed": len(claimed), "success": success, "no_event_data": no_event_data, "failed": failed}


def status() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("Iteration discovery:")
            cur.execute(f"SELECT STATUS, COUNT(*) FROM {DISCOVERY_TABLE} GROUP BY STATUS ORDER BY STATUS")
            for state, count in cur.fetchall():
                print(f"  {state}: {count}")
            print("Match-event backfill:")
            cur.execute(f"SELECT STATUS, COUNT(*) FROM {QUEUE_TABLE} GROUP BY STATUS ORDER BY STATUS")
            for state, count in cur.fetchall():
                print(f"  {state}: {count}")
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--discover", action="store_true")
    action.add_argument("--process", action="store_true")
    action.add_argument("--status", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--iteration-batch-size", type=int, default=25,
                        help="Iterations to catalogue for each --discover run (default: 25).")
    parser.add_argument("--iteration-ids", type=str, default=None,
                        help="Optional comma-separated IMPECT iteration IDs to restrict discovery.")
    parser.add_argument("--until-empty", action="store_true",
                        help="With --process, keep claiming batches until no eligible scoped work remains.")
    parser.add_argument("--refresh", action="store_true",
                        help="With --discover, re-catalogue selected active iterations.")
    parser.add_argument("--max-attempts", type=int, default=5)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.discover:
        ids = {int(value) for value in args.iteration_ids.split(",")} if args.iteration_ids and args.iteration_ids.strip() else None
        discover(args.iteration_batch_size, ids, refresh=args.refresh)
    elif args.process:
        ids = {int(value) for value in args.iteration_ids.split(",")} if args.iteration_ids and args.iteration_ids.strip() else None
        while True:
            summary = process(args.batch_size, args.max_attempts, ids)
            if not args.until_empty or summary["claimed"] == 0:
                break
    else:
        status()
