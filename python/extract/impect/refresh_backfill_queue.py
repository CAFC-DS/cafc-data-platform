"""Queue-only refresh for the IMPECT event backfill.

Adds newly completed matches (and iterations that did not exist at the last
discovery) to CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE for the chosen seasons or
iterations.  Run it before backfilling a season that is still being played.

Unlike ``backfill_historical_match_events.py --discover --refresh`` this does NOT
delete and reload IMPECT_RAW.MATCHES / SQUADS / PLAYERS for the iteration; it
only reads IMPECT's fixture list and merges queue rows (existing rows keep their
status).  It needs a role that can write the queue and create temporary tables in
CAFC_DB.CORE (a developer role, not BACKFILL_ROLE).

Dry run by default:
    python refresh_backfill_queue.py --seasons 26/27,2026
    python refresh_backfill_queue.py --seasons 26/27,2026 --apply
    python refresh_backfill_queue.py --iteration-ids 1864,1804 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

import backfill_historical_match_events as bf


def rows_for_iteration(iteration: dict, matches: list[dict], loaded: set[int], queued: set[int],
                       now: datetime) -> tuple[list[dict], dict[str, int]]:
    """Queue rows for one iteration's completed matches, plus counts for reporting."""
    rows: list[dict] = []
    counts = {"api": len(matches), "completed": 0, "new": 0, "future": 0}
    for match in matches:
        scheduled_at = bf._as_utc(match.get("scheduledDate"))
        if scheduled_at and scheduled_at > now:
            counts["future"] += 1
            continue
        match_id = int(match["id"])
        counts["completed"] += 1
        counts["new"] += match_id not in queued
        rows.append({
            "MATCH_ID": match_id, "ITERATION_ID": int(iteration["id"]),
            "COMPETITION_NAME": bf._nested(iteration, "competition.name"), "SEASON": iteration.get("season"),
            "SCHEDULED_AT": scheduled_at, "ALREADY_LOADED": match_id in loaded,
        })
    return rows, counts


def _existing(iteration_ids: list[int]) -> tuple[set[int], set[int]]:
    scope = ", ".join(str(value) for value in iteration_ids)
    conn = bf.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT MATCH_ID FROM {bf.QUEUE_TABLE} WHERE ITERATION_ID IN ({scope})")
        queued = {int(row[0]) for row in cur.fetchall()}
        cur.execute(f"SELECT DISTINCT MATCH_ID FROM CAFC_DB.IMPECT_RAW.EVENTS WHERE ITERATION_ID IN ({scope})")
        loaded = {int(row[0]) for row in cur.fetchall()}
        return queued, loaded
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seasons", help="Comma-separated season labels, e.g. 26/27,2026.")
    parser.add_argument("--iteration-ids", help="Comma-separated IMPECT iteration ids.")
    parser.add_argument("--apply", action="store_true", help="Write to the queue (default: report only).")
    args = parser.parse_args(argv)
    if not (args.seasons or args.iteration_ids):
        parser.error("pass --seasons and/or --iteration-ids")
    os.environ.setdefault("SNOWFLAKE_QUERY_TAG", "project=cafc-data-platform;job=impect-queue-refresh")

    wanted_ids = {int(v) for v in args.iteration_ids.split(",") if v.strip()} if args.iteration_ids else set()
    wanted_seasons = {v.strip() for v in args.seasons.split(",") if v.strip()} if args.seasons else set()
    iterations = [it for it in bf._target_iterations(None)
                  if int(it["id"]) in wanted_ids or it.get("season") in wanted_seasons]
    if not iterations:
        print("no matching iterations")
        return 1

    queued, loaded = _existing([int(it["id"]) for it in iterations])
    now = datetime.now(timezone.utc)
    all_rows: list[dict[str, Any]] = []
    total_new = 0
    for iteration in iterations:
        rows, counts = rows_for_iteration(iteration, bf._records(bf.api.get_matches(int(iteration["id"]))),
                                          loaded, queued, now)
        all_rows.extend(rows)
        total_new += counts["new"]
        if counts["new"]:
            print(f"iteration {iteration['id']} {bf._nested(iteration, 'competition.name')} {iteration.get('season')}: "
                  f"completed={counts['completed']} new_to_queue={counts['new']} future={counts['future']}")
    print(f"{len(iterations)} iterations, {len(all_rows)} completed matches, {total_new} new to the queue")
    if args.apply:
        print(f"merged {bf._stage_and_merge(all_rows)} rows")
    else:
        print("dry run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
