"""Incrementally synchronise IMPECT men's match events from V5 feeds.

IMPECT's ``update/matchdata`` feed is the primary trigger: it returns only
matches whose event/KPI data changed since a timestamp.  ``delete/matches``
handles provider deletions and merges.  The stored high-water mark is queried
with a short overlap each run, which is required because IMPECT exposes a
timestamp rather than an opaque cursor.

The iteration catalogue is refreshed on every run to exclude women's
competitions and limit syncs to the agreed season labels: 26/27, 2026, and
2027 once it becomes available.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import impect_api as api
import load_match_events as events
from snowflake_loader import get_connection


STATE_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_SYNC_STATE"
CURSOR_TABLE = "CAFC_DB.CORE.IMPECT_FEED_CURSORS"
FEED_NAME = "MATCH_EVENTS_V5"
SUCCESS, NO_EVENT_DATA, FAILED, DELETED = "SUCCESS", "NO_EVENT_DATA", "FAILED", "DELETED"


def _records(response: Any) -> list[dict]:
    return response.get("data", []) if isinstance(response, dict) else []


def _one_record(response: Any) -> dict | None:
    data = response.get("data") if isinstance(response, dict) else None
    return data if isinstance(data, dict) else None


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


def eligible_iterations(
    allowed_seasons: set[str], competition_ids: set[int] | None = None,
) -> dict[int, dict]:
    selected = {}
    for iteration in _records(api.get_iterations()):
        if _is_womens(iteration):
            continue
        competition_id = int(_nested(iteration, "competition.id") or 0)
        if competition_ids is not None and competition_id not in competition_ids:
            continue
        if str(iteration.get("season") or "").strip() in allowed_seasons:
            selected[int(iteration["id"])] = iteration
    return selected


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes"}


def load_known_state() -> dict[int, dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT MATCH_ID, STATUS FROM {STATE_TABLE}")
            return {int(match_id): {"status": status} for match_id, status in cur.fetchall()}
    finally:
        conn.close()


def load_event_match_ids() -> set[int]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT MATCH_ID FROM CAFC_DB.IMPECT_RAW.EVENTS")
            return {int(row[0]) for row in cur.fetchall()}
    finally:
        conn.close()


def load_since(default_since: datetime, feed_name: str = FEED_NAME) -> datetime:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT LAST_SUCCESSFUL_SINCE FROM {CURSOR_TABLE} WHERE FEED_NAME = %(feed)s", {"feed": feed_name})
            row = cur.fetchone()
            return _as_utc(row[0]) if row else default_since
    finally:
        conn.close()


def save_since(value: datetime, feed_name: str = FEED_NAME) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                MERGE INTO {CURSOR_TABLE} t
                USING (SELECT %(feed)s AS FEED_NAME, %(since)s AS LAST_SUCCESSFUL_SINCE) s
                   ON t.FEED_NAME = s.FEED_NAME
                WHEN MATCHED THEN UPDATE SET LAST_SUCCESSFUL_SINCE=s.LAST_SUCCESSFUL_SINCE, UPDATED_AT=CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT (FEED_NAME, LAST_SUCCESSFUL_SINCE) VALUES (s.FEED_NAME, s.LAST_SUCCESSFUL_SINCE)
                """,
                {"feed": feed_name, "since": value},
            )
        conn.commit()
    finally:
        conn.close()


def save_state(match: dict, iteration: dict, status: str, source_updated_at: datetime | None,
               event_count: int | None, error: str | None, run_id: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                MERGE INTO {STATE_TABLE} t
                USING (SELECT %(match_id)s AS MATCH_ID) s ON t.MATCH_ID=s.MATCH_ID
                WHEN MATCHED THEN UPDATE SET
                  ITERATION_ID=%(iteration_id)s, COMPETITION_NAME=%(competition_name)s, SEASON=%(season)s,
                  SCHEDULED_AT=%(scheduled_at)s, MATCH_AVAILABLE=%(available)s,
                  SOURCE_LAST_CALCULATION_AT=%(last_calculation)s, SOURCE_UPDATED_AT=%(source_updated_at)s,
                  STATUS=%(status)s, EVENT_COUNT=%(event_count)s, LAST_ATTEMPTED_AT=CURRENT_TIMESTAMP(),
                  LAST_SUCCEEDED_AT=IFF(%(status)s='SUCCESS', CURRENT_TIMESTAMP(), t.LAST_SUCCEEDED_AT),
                  NEXT_RETRY_AT=IFF(%(status)s='FAILED', DATEADD(hour, 6, CURRENT_TIMESTAMP()), NULL),
                  LAST_ERROR=%(error)s, INGESTION_RUN_ID=%(run_id)s, UPDATED_AT=CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT (
                  MATCH_ID, ITERATION_ID, COMPETITION_NAME, SEASON, SCHEDULED_AT, MATCH_AVAILABLE,
                  SOURCE_LAST_CALCULATION_AT, SOURCE_UPDATED_AT, STATUS, EVENT_COUNT, LAST_ATTEMPTED_AT,
                  LAST_SUCCEEDED_AT, NEXT_RETRY_AT, LAST_ERROR, INGESTION_RUN_ID
                ) VALUES (
                  %(match_id)s, %(iteration_id)s, %(competition_name)s, %(season)s, %(scheduled_at)s, %(available)s,
                  %(last_calculation)s, %(source_updated_at)s, %(status)s, %(event_count)s, CURRENT_TIMESTAMP(),
                  IFF(%(status)s='SUCCESS', CURRENT_TIMESTAMP(), NULL),
                  IFF(%(status)s='FAILED', DATEADD(hour, 6, CURRENT_TIMESTAMP()), NULL), %(error)s, %(run_id)s
                )
                """,
                {
                    "match_id": int(match["id"]), "iteration_id": int(iteration["id"]),
                    "competition_name": _nested(iteration, "competition.name"), "season": iteration.get("season"),
                    "scheduled_at": _as_utc(match.get("scheduledDate")), "available": _truthy(match.get("available")),
                    "last_calculation": match.get("lastCalculationDate"), "source_updated_at": source_updated_at,
                    "status": status, "event_count": event_count, "error": error[:4000] if error else None, "run_id": run_id,
                },
            )
        conn.commit()
    finally:
        conn.close()


def mark_deleted(match_id: int, run_id: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM CAFC_DB.IMPECT_RAW.EVENTS WHERE MATCH_ID = %(match_id)s", {"match_id": match_id})
            cur.execute(
                f"UPDATE {STATE_TABLE} SET STATUS=%(status)s, EVENT_COUNT=0, LAST_ATTEMPTED_AT=CURRENT_TIMESTAMP(), "
                "LAST_ERROR='Removed by IMPECT delete/merge feed', INGESTION_RUN_ID=%(run_id)s, UPDATED_AT=CURRENT_TIMESTAMP() "
                "WHERE MATCH_ID=%(match_id)s",
                {"match_id": match_id, "status": DELETED, "run_id": run_id},
            )
        conn.commit()
    finally:
        conn.close()


def _open_run() -> tuple[Any, int]:
    conn = get_connection()
    with conn.cursor() as cur:
        run_id = events._open_run(cur, "sync_recent_match_events.py")
    conn.commit()
    return conn, run_id


def _close_run(conn: Any, run_id: int, status: str, notes: str) -> None:
    try:
        with conn.cursor() as cur:
            events._close_run(cur, run_id, status, notes)
        conn.commit()
    finally:
        conn.close()


def run(*, bootstrap_days: int, overlap_minutes: int, seasons: set[str], match_lookup_pause_seconds: float,
        max_match_lookups: int | None = None, dry_run: bool = False,
        competition_ids: set[int] | None = None, feed_name: str = FEED_NAME,
        seed_missing: bool = False) -> dict[str, int]:
    started_at = datetime.now(timezone.utc)
    iterations = eligible_iterations(seasons, competition_ids)
    if not iterations:
        scope = f" for competition ids {sorted(competition_ids)}" if competition_ids else ""
        raise RuntimeError(f"No eligible IMPECT iterations found{scope} in seasons {sorted(seasons)}")
    known = load_known_state()
    loaded_match_ids = load_event_match_ids()
    stored_since = load_since(started_at - timedelta(days=bootstrap_days), feed_name)
    query_since = stored_since - timedelta(minutes=overlap_minutes)
    since_text = query_since.isoformat()

    match_data_updates = {int(row["id"]): _as_utc(row.get("date")) for row in _records(api.get_match_data_updates(since_text))}
    # Match-data updates are intentionally compact ({id, date}).  The match
    # update feed often, but not always, has the matching metadata row, so
    # fall back to the individual match endpoint. Pace that fallback: a first
    # run can contain many historic recalculations and IMPECT rate-limits
    # bursts of per-match requests.
    match_updates = {int(row["id"]): row for row in _records(api.get_match_updates(since_text))}
    deletions = _records(api.get_match_deletes(since_text))
    candidates: dict[int, tuple[dict, dict, datetime | None]] = {}

    # A scoped scheduled job must be able to catch up even when its cursor is
    # first created after the season has started. Enumerate the selected
    # iteration catalogues and seed only past, data-ready matches not already
    # present in EVENTS. The delta feed below then adds recalculations.
    scoped_match_ids: set[int] = set()
    if seed_missing or competition_ids is not None:
        for iteration_id, iteration in iterations.items():
            for match in _records(api.get_matches(iteration_id)):
                match_id = int(match["id"])
                scoped_match_ids.add(match_id)
                match.setdefault("iterationId", iteration_id)
                scheduled_at = _as_utc(match.get("scheduledDate"))
                available = "available" not in match or _truthy(match.get("available"))
                prior_status = (known.get(match_id) or {}).get("status")
                should_seed = prior_status not in {NO_EVENT_DATA, DELETED}
                if (seed_missing and should_seed and match_id not in loaded_match_ids
                        and available and scheduled_at and scheduled_at <= started_at):
                    candidates[match_id] = (iteration, match, _as_utc(match.get("lastCalculationDate")))

    match_lookups = 0
    for match_id, updated_at in match_data_updates.items():
        match = match_updates.get(match_id)
        if match is None:
            if max_match_lookups is not None and match_lookups >= max_match_lookups:
                break
            if match_lookups:
                time.sleep(match_lookup_pause_seconds)
            match = _one_record(api.get_match_info(match_id))
            match_lookups += 1
        if not match or int(match.get("iterationId") or 0) not in iterations:
            continue
        if not _truthy(match.get("available")):
            continue
        scheduled_at = _as_utc(match.get("scheduledDate"))
        if not scheduled_at or scheduled_at > started_at:
            continue
        candidates[match_id] = (iterations[int(match["iterationId"])], match, updated_at)

    known_ids = set(known) | loaded_match_ids
    known_deletions = [
        int(row["id"]) for row in deletions
        if int(row.get("id") or 0) in known_ids
        and (competition_ids is None or int(row.get("id") or 0) in scoped_match_ids)
    ]
    for match_id in known_deletions:
        candidates.pop(match_id, None)
    print(f"Feed since {since_text}: {len(match_data_updates)} match-data updates, {len(deletions)} deletions, "
          f"{len(candidates)} eligible men's event sync(s), {len(known_deletions)} tracked deletion(s), "
          f"{match_lookups} metadata lookup(s).")
    if dry_run:
        return {"candidates": len(candidates), "deletions": len(known_deletions), "loaded": 0, "failed": 0}

    run_conn, run_id = _open_run()
    loaded = failed = no_data = deleted = 0
    try:
        for match_id in known_deletions:
            mark_deleted(match_id, run_id)
            deleted += 1
        for iteration, match, updated_at in candidates.values():
            try:
                rows = events.fetch_events_for_match(int(match["id"]), int(iteration["id"]), run_id)
                if not rows:
                    no_data += 1
                    save_state(match, iteration, NO_EVENT_DATA, updated_at, 0, None, run_id)
                    continue
                count = events.load_events(rows, replace_existing_match=True)
                save_state(match, iteration, SUCCESS, updated_at, count, None, run_id)
                loaded += 1
            except Exception as exc:
                failed += 1
                save_state(match, iteration, FAILED, updated_at, None, str(exc), run_id)
                print(f"match {match['id']}: {exc}", file=sys.stderr)
        status = "SUCCESS" if not failed else "PARTIAL_SUCCESS"
        _close_run(run_conn, run_id, status, f"loaded={loaded} no_event_data={no_data} deleted={deleted} failed={failed}")
    except Exception:
        _close_run(run_conn, run_id, "FAILED", "unexpected feed-sync failure")
        raise
    # Advance only after every feed record was handled. Failed match fetches
    # remain eligible through the overlap and are also visible in state.
    if not failed:
        save_since(started_at, feed_name)
    return {"candidates": len(candidates), "deletions": deleted, "loaded": loaded, "failed": failed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bootstrap-days", type=int, default=7, help="First-run feed lookback (default: 7).")
    parser.add_argument("--overlap-minutes", type=int, default=15, help="Timestamp overlap between successful polls (default: 15).")
    parser.add_argument("--seasons", default="26/27,2026,2027",
                        help="Comma-separated IMPECT season labels to sync.")
    parser.add_argument("--match-lookup-pause-seconds", type=float, default=0.35,
                        help="Pace fallback match-metadata requests to respect IMPECT limits (default: 0.35).")
    parser.add_argument("--max-match-lookups", type=int, default=None,
                        help="Cap fallback metadata lookups; intended only for bounded dry-runs.")
    parser.add_argument("--competition-ids", default=None,
                        help="Optional comma-separated IMPECT competition ids to include.")
    parser.add_argument("--feed-name", default=FEED_NAME,
                        help="Cursor key in IMPECT_FEED_CURSORS (use a distinct key for a scoped sync).")
    parser.add_argument("--seed-missing", action="store_true",
                        help="Also load past, available matches missing from IMPECT_RAW.EVENTS.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(bootstrap_days=args.bootstrap_days, overlap_minutes=args.overlap_minutes,
        seasons={value.strip() for value in args.seasons.split(",") if value.strip()},
        match_lookup_pause_seconds=args.match_lookup_pause_seconds,
        max_match_lookups=args.max_match_lookups, dry_run=args.dry_run,
        competition_ids=({int(value.strip()) for value in args.competition_ids.split(",") if value.strip()}
                         if args.competition_ids else None),
        feed_name=args.feed_name, seed_missing=args.seed_missing)
