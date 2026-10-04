"""Discover and resumably backfill all accessible IMPECT men's event data.

This is the historical companion to ``sync_recent_match_events.py``. Run
discovery once (or safely repeat it) to queue every completed match in every
accessible non-women's V2+ iteration. Then run bounded processing batches
until the queue is empty. Successes are never re-fetched; failures retry with
backoff, and IMPECT's known permanent no-event response is terminal.

Processing is built to be cheap on Snowflake: each claimed batch reuses one
connection, fetches matches from IMPECT concurrently while the previous group
loads, stages several matches per load and parses their VARIANT columns in one
INSERT ... SELECT, and writes every match's queue state with one UPDATE.
Unfinished claims are handed back (attempt not counted) if the process is
stopped, and --max-matches / --stop-after-minutes bound a run.

Examples:
    python backfill_historical_match_events.py --discover
    python backfill_historical_match_events.py --process --batch-size 25
    python backfill_historical_match_events.py --process --max-matches 100 \
        --query-tag "project=cafc-data-platform;job=impect-benchmark"
    python backfill_historical_match_events.py --status
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

import config
import impect_api as api
import load_match_events as events
import load_match_info as match_info
import refresh_iteration_metadata as metadata
from snowflake_loader import get_connection


QUEUE_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE"
DISCOVERY_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_DISCOVERY"
QUEUE_SCHEMA = "CORE"
TERMINAL_STATUSES = ("SUCCESS", "NO_EVENT_DATA")

DEFAULT_LOAD_MATCHES = 10    # matches per staged load / INSERT ... SELECT
DEFAULT_FETCH_WORKERS = 3    # concurrent IMPECT fetch threads per process
MAX_ROWS_PER_LOAD = 80_000   # split a load group that would stage more event rows than this
FAILED_RETRY_HOURS = 12      # cooldown before a FAILED match is claimable again


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
            metadata.refresh({item["id"]})
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


def _claim_batch(batch_size: int, max_attempts: int, iteration_ids: set[int] | None = None,
                 conn=None) -> tuple[str, list[dict]]:
    claim_token = str(uuid.uuid4())
    owns_connection = conn is None
    conn = conn or get_connection()
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
        if owns_connection:
            conn.close()


def _finish_matches(conn, run_id: int, outcomes: dict[int, dict]) -> None:
    """Write the final queue state of every match in a batch with ONE statement."""
    if not outcomes:
        return
    params: dict[str, Any] = {"run_id": run_id}
    values = []
    for index, (match_id, outcome) in enumerate(sorted(outcomes.items())):
        error = outcome.get("error")
        params[f"m{index}"] = match_id
        params[f"s{index}"] = outcome["status"]
        params[f"c{index}"] = outcome.get("count")
        params[f"e{index}"] = error[:4000] if error else None
        values.append(f"(%(m{index})s, %(s{index})s, %(c{index})s, %(e{index})s)")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {QUEUE_TABLE} t
               SET STATUS=v.STATUS, EVENT_COUNT=v.EVENT_COUNT, LAST_ERROR=v.ERROR,
                   INGESTION_RUN_ID=%(run_id)s, CLAIMED_BY=NULL, CLAIMED_AT=NULL,
                   NEXT_ATTEMPT_AT=IFF(v.STATUS='FAILED', DATEADD(hour, {FAILED_RETRY_HOURS}, CURRENT_TIMESTAMP()), NULL),
                   COMPLETED_AT=IFF(v.STATUS IN ('SUCCESS', 'NO_EVENT_DATA'), CURRENT_TIMESTAMP(), t.COMPLETED_AT),
                   UPDATED_AT=CURRENT_TIMESTAMP()
              FROM (
                SELECT column1::NUMBER AS MATCH_ID, column2::VARCHAR AS STATUS,
                       column3::NUMBER AS EVENT_COUNT, column4::VARCHAR AS ERROR
                  FROM VALUES {', '.join(values)}
              ) v
             WHERE t.MATCH_ID = v.MATCH_ID
            """,
            params,
        )
    conn.commit()


def _finish_match(match_id: int, status: str, run_id: int, event_count: int | None = None,
                  error: str | None = None) -> None:
    """Single-match form of ``_finish_matches`` (kept for callers outside the batch path)."""
    conn = get_connection()
    try:
        _finish_matches(conn, run_id, {match_id: {"status": status, "count": event_count, "error": error}})
    finally:
        conn.close()


def _release_claims(conn, claim_token: str) -> int:
    """Hand a batch's unfinished claims back to the queue without counting the attempt."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {QUEUE_TABLE}
               SET STATUS='PENDING', CLAIMED_BY=NULL, CLAIMED_AT=NULL,
                   ATTEMPT_COUNT=GREATEST(ATTEMPT_COUNT - 1, 0), UPDATED_AT=CURRENT_TIMESTAMP()
             WHERE CLAIMED_BY=%(claim)s AND STATUS='RUNNING'
            """,
            {"claim": claim_token},
        )
        released = cur.rowcount or 0
    conn.commit()
    return released


class _Session:
    """One reusable Snowflake connection that reconnects if it was closed."""

    def __init__(self) -> None:
        self._conn = None

    def get(self):
        if self._conn is None or self._conn.is_closed():
            self._conn = get_connection()
        return self._conn

    def rollback(self) -> None:
        try:
            if self._conn is not None and not self._conn.is_closed():
                self._conn.rollback()
        except Exception:
            self._conn = None

    def close(self) -> None:
        try:
            if self._conn is not None and not self._conn.is_closed():
                self._conn.close()
        except Exception:
            pass
        self._conn = None


def _fetch_one(item: dict, run_id: int) -> dict:
    """Fetch everything IMPECT has for one match. No database access, safe to run in a thread."""
    try:
        rows = events.fetch_events_for_match(item["match_id"], item["iteration_id"], run_id)
        if not rows:
            return {"item": item, "status": "NO_EVENT_DATA", "rows": [], "info": None, "error": None}
        info = match_info.fetch_row(item["match_id"], item["iteration_id"], run_id)
        return {"item": item, "status": "FETCHED", "rows": rows, "info": info, "error": None}
    except Exception as exc:
        return {"item": item, "status": "FAILED", "rows": [], "info": None, "error": str(exc)}


def _split_by_rows(fetched: list[dict], max_rows: int) -> list[list[dict]]:
    """Group fetched matches, in order, so no group stages more than ``max_rows`` event rows."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_rows = 0
    for result in fetched:
        size = len(result["rows"])
        if current and current_rows + size > max_rows:
            groups.append(current)
            current, current_rows = [], 0
        current.append(result)
        current_rows += size
    if current:
        groups.append(current)
    return groups


def _load_group(session: _Session, group: list[dict], run_id: int, outcomes: dict[int, dict]) -> None:
    """Load a group of matches with one events load and one match-info MERGE.

    If the combined load fails, retry the matches one by one so a single bad
    match is marked FAILED instead of failing its neighbours.  Re-loading is
    safe: ``replace_existing=True`` swaps out any rows a first attempt left.
    """
    rows = [row for result in group for row in result["rows"]]
    infos = [result["info"] for result in group]
    try:
        conn = session.get()
        events.load_events_batch(rows, replace_existing=True, conn=conn)
        match_info.upsert_many(infos, conn=conn)
    except Exception as exc:
        session.rollback()
        if len(group) == 1:
            match_id = group[0]["item"]["match_id"]
            outcomes[match_id] = {"status": "FAILED", "count": None, "error": str(exc)}
            print(f"match {match_id}: FAILED: {exc}", file=sys.stderr)
            return
        print(f"  load of {len(group)} matches failed ({exc}); retrying one by one", file=sys.stderr)
        for result in group:
            _load_group(session, [result], run_id, outcomes)
        return
    for result in group:
        match_id = result["item"]["match_id"]
        count = len(result["rows"])
        outcomes[match_id] = {"status": "SUCCESS", "count": count, "error": None}
        print(f"match {match_id}: loaded {count} events")


def _abandon(session: _Session, claim_token: str | None, run_id: int | None) -> None:
    try:
        conn = session.get()
        released = _release_claims(conn, claim_token) if claim_token else 0
        if run_id is not None:
            with conn.cursor() as cur:
                events._close_run(cur, run_id, "FAILED", "backfill batch interrupted")
            conn.commit()
        print(f"interrupted: released {released} unfinished claim(s)", file=sys.stderr)
    except Exception as exc:
        print(f"could not release claims after interruption: {exc}", file=sys.stderr)


def process(batch_size: int, max_attempts: int, iteration_ids: set[int] | None = None, *,
            load_matches: int = DEFAULT_LOAD_MATCHES,
            fetch_workers: int = DEFAULT_FETCH_WORKERS) -> dict[str, int]:
    session = _Session()
    claim_token: str | None = None
    run_id: int | None = None
    pool: ThreadPoolExecutor | None = None
    started = time.perf_counter()
    try:
        claim_token, claimed = _claim_batch(batch_size, max_attempts, iteration_ids, conn=session.get())
        if not claimed:
            print("No eligible backfill work remains.")
            return {"claimed": 0, "success": 0, "no_event_data": 0, "failed": 0, "rows": 0}
        conn = session.get()
        with conn.cursor() as cur:
            run_id = events._open_run(cur, "backfill_historical_match_events.py")
        conn.commit()

        outcomes: dict[int, dict] = {}
        chunks = [claimed[i:i + max(1, load_matches)] for i in range(0, len(claimed), max(1, load_matches))]
        fetch_wait = load_time = 0.0
        loaded_rows = 0
        pool = ThreadPoolExecutor(max_workers=max(1, fetch_workers))

        def submit(chunk: list[dict]) -> list:
            return [pool.submit(_fetch_one, item, run_id) for item in chunk]

        # Fetch chunk N+1 from IMPECT while chunk N loads into Snowflake.
        pending = submit(chunks[0])
        for index in range(len(chunks)):
            waited = time.perf_counter()
            fetched = [future.result() for future in pending]
            fetch_wait += time.perf_counter() - waited
            pending = submit(chunks[index + 1]) if index + 1 < len(chunks) else []

            loadable = []
            for result in fetched:
                match_id = result["item"]["match_id"]
                if result["status"] == "FAILED":
                    outcomes[match_id] = {"status": "FAILED", "count": None, "error": result["error"]}
                    print(f"match {match_id}: FAILED: {result['error']}", file=sys.stderr)
                elif result["status"] == "NO_EVENT_DATA":
                    outcomes[match_id] = {"status": "NO_EVENT_DATA", "count": 0, "error": None}
                else:
                    loadable.append(result)
            loading = time.perf_counter()
            for group in _split_by_rows(loadable, MAX_ROWS_PER_LOAD):
                _load_group(session, group, run_id, outcomes)
                loaded_rows += sum(len(result["rows"]) for result in group)
            load_time += time.perf_counter() - loading

        for item in claimed:  # never leave a claimed match without a final state
            outcomes.setdefault(item["match_id"], {"status": "FAILED", "count": None,
                                                   "error": "internal: batch finished without an outcome"})
        _finish_matches(session.get(), run_id, outcomes)

        success = sum(1 for o in outcomes.values() if o["status"] == "SUCCESS")
        no_event_data = sum(1 for o in outcomes.values() if o["status"] == "NO_EVENT_DATA")
        failed = sum(1 for o in outcomes.values() if o["status"] == "FAILED")
        conn = session.get()
        with conn.cursor() as cur:
            events._close_run(cur, run_id, "SUCCESS" if not failed else "PARTIAL_SUCCESS",
                              f"claimed={len(claimed)} success={success} no_event_data={no_event_data} failed={failed}")
        conn.commit()
        elapsed = time.perf_counter() - started
        print(f"batch: claimed={len(claimed)} success={success} no_event_data={no_event_data} failed={failed} "
              f"rows={loaded_rows} fetch_wait={fetch_wait:.1f}s load={load_time:.1f}s total={elapsed:.1f}s "
              f"({len(claimed) / max(elapsed, 1e-9) * 3600:.0f} matches/hour)")
        return {"claimed": len(claimed), "success": success, "no_event_data": no_event_data,
                "failed": failed, "rows": loaded_rows}
    except BaseException:
        # Includes SystemExit from SIGTERM: stop fetching and hand unfinished claims back.
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        _abandon(session, claim_token, run_id)
        raise
    finally:
        if pool is not None:
            pool.shutdown(wait=False)
        session.close()


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
    parser.add_argument("--load-matches", type=int, default=DEFAULT_LOAD_MATCHES,
                        help="Matches staged and parsed per INSERT ... SELECT (default: %(default)s).")
    parser.add_argument("--fetch-workers", type=int, default=DEFAULT_FETCH_WORKERS,
                        help="Concurrent IMPECT fetch threads per process (default: %(default)s).")
    parser.add_argument("--max-matches", type=int, default=None,
                        help="With --process, stop after claiming this many matches in total.")
    parser.add_argument("--stop-after-minutes", type=float, default=None,
                        help="With --process, claim no new batch once this many minutes have elapsed.")
    parser.add_argument("--query-tag", type=str, default=None,
                        help="Snowflake QUERY_TAG for every session this process opens.")
    return parser.parse_args()


def _terminate(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    args = parse_args()
    if args.query_tag:
        os.environ["SNOWFLAKE_QUERY_TAG"] = args.query_tag
    ids = ({int(value) for value in args.iteration_ids.split(",")}
           if args.iteration_ids and args.iteration_ids.strip() else None)
    if args.discover:
        discover(args.iteration_batch_size, ids, refresh=args.refresh)
    elif args.process:
        signal.signal(signal.SIGTERM, _terminate)  # let a redeploy/stop release claims cleanly
        budget = args.max_matches
        deadline = time.monotonic() + args.stop_after_minutes * 60 if args.stop_after_minutes else None
        while True:
            size = args.batch_size if budget is None else min(args.batch_size, budget)
            if size <= 0:
                print("Match budget reached.")
                break
            if deadline is not None and time.monotonic() >= deadline:
                print("Time budget reached.")
                break
            summary = process(size, args.max_attempts, ids,
                              load_matches=args.load_matches, fetch_workers=args.fetch_workers)
            if budget is not None:
                budget -= summary["claimed"]
            if not args.until_empty or summary["claimed"] == 0:
                break
    else:
        status()
