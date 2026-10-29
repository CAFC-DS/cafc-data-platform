"""Report which backfill stages (season block x geographic group) have finished loading.

Intended for whatever consumes the loaded event data (for example a local scoring job):
ask which stages completed since you last ran, re-score exactly those iterations, then
move your watermark forward.

A stage is *complete* when nothing in it can still be claimed (no pending, in-progress or
retryable matches; cooling-down and out-of-attempts failures do not block it) and it has
loaded data.  Completion time is the latest COMPLETED_AT in the stage, in the queue's clock
(Europe/London), printed without a zone.  Live seasons can reopen: if the queue is refreshed
and new matches appear, the stage stops being complete until they load.

Read-only.  Examples:
    python backfill_stage_status.py                         # table of every stage
    python backfill_stage_status.py --blocks 26/27+2026
    python backfill_stage_status.py --since 2026-10-04T18:00:00 --json   # only stages completed after that
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

IMPECT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "extract", "impect")
sys.path.insert(0, IMPECT_DIR)

import backfill_plan  # noqa: E402

QUEUE_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE"
DEFAULT_PLAN = os.path.join(IMPECT_DIR, "backfill_plans", "five_seasons_men.json")
MAX_ATTEMPTS = 5


def classify_stage(iterations: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise one stage from its per-iteration queue counts."""
    total = {key: sum(row[key] for row in iterations) for key in
             ("pending", "running", "retryable", "deferred", "exhausted", "loaded", "no_data")}
    remaining = total["pending"] + total["running"] + total["retryable"]
    finished = total["loaded"] + total["no_data"]
    last = [row["last_completed"] for row in iterations if row["last_completed"] is not None]
    if remaining == 0 and finished > 0:
        status = "complete"
    elif finished > 0 or total["running"] > 0:
        status = "in_progress"
    else:
        status = "pending"
    return {"status": status, "remaining": remaining, "loaded": total["loaded"], "no_event_data": total["no_data"],
            "deferred_failures": total["deferred"], "exhausted": total["exhausted"],
            "completed_at": max(last).isoformat(timespec="seconds") if (last and status == "complete") else None}


def stage_report(plan: dict[str, Any], per_iteration: dict[int, dict[str, Any]],
                 block_labels: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for stage in backfill_plan.select_stages(plan, block_labels):
        rows = [per_iteration[i] for i in stage["iteration_ids"] if i in per_iteration]
        if not rows:
            continue
        summary = classify_stage(rows)
        out.append({
            "block": stage["block"], "group": stage["group"], **summary,
            "iterations": [{"iteration_id": r["iteration_id"], "competition": r["competition"], "season": r["season"],
                            "matches_loaded": r["loaded"]} for r in rows if r["loaded"] > 0],
        })
    return out


def only_completed_since(report: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    return [s for s in report if s["status"] == "complete" and s["completed_at"]
            and datetime.fromisoformat(s["completed_at"]) > since]


def _connection():
    if os.environ.get("SNOWFLAKE_PRIVATE_KEY"):  # key supplied as text (containers) instead of a key file
        import railway_backfill
        return railway_backfill.get_connection()
    from snowflake_loader import get_connection
    return get_connection()


def fetch_per_iteration(iteration_ids: list[int]) -> dict[int, dict[str, Any]]:
    scope = ", ".join(str(i) for i in sorted(set(iteration_ids)))
    attempts = MAX_ATTEMPTS
    conn = _connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT ITERATION_ID, ANY_VALUE(COMPETITION_NAME), ANY_VALUE(SEASON),
              COUNT_IF(STATUS = 'PENDING' AND ATTEMPT_COUNT < {attempts}),
              COUNT_IF(STATUS = 'RUNNING'),
              COUNT_IF(STATUS = 'FAILED' AND ATTEMPT_COUNT < {attempts}
                       AND (NEXT_ATTEMPT_AT IS NULL OR NEXT_ATTEMPT_AT <= CURRENT_TIMESTAMP())),
              COUNT_IF(STATUS = 'FAILED' AND ATTEMPT_COUNT < {attempts} AND NEXT_ATTEMPT_AT > CURRENT_TIMESTAMP()),
              COUNT_IF(STATUS IN ('PENDING', 'FAILED') AND ATTEMPT_COUNT >= {attempts}),
              COUNT_IF(STATUS = 'SUCCESS'), COUNT_IF(STATUS = 'NO_EVENT_DATA'), MAX(COMPLETED_AT)
            FROM {QUEUE_TABLE} WHERE ITERATION_ID IN ({scope}) GROUP BY 1
            """
        )
        keys = ("iteration_id", "competition", "season", "pending", "running", "retryable", "deferred",
                "exhausted", "loaded", "no_data", "last_completed")
        return {int(row[0]): dict(zip(keys, (int(row[0]), *row[1:3], *(int(v or 0) for v in row[3:10]), row[10])))
                for row in cur.fetchall()}
    finally:
        conn.close()


def format_table(report: list[dict[str, Any]]) -> str:
    lines = []
    for s in report:
        lines.append(f"{s['block']:<12} {s['group']:<46} {s['status']:<12} loaded={s['loaded']:>6} "
                     f"no_data={s['no_event_data']:>5} remaining={s['remaining']:>6} completed_at={s['completed_at'] or '-'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", default=DEFAULT_PLAN)
    parser.add_argument("--blocks", help="Comma-separated block labels, e.g. 26/27+2026 (default: all).")
    parser.add_argument("--since", help="Only stages completed after this ISO timestamp (queue clock, Europe/London).")
    parser.add_argument("--json", action="store_true", help="Print JSON (stages with their iterations) instead of a table.")
    args = parser.parse_args(argv)

    os.environ.setdefault("SNOWFLAKE_QUERY_TAG", "project=cafc-data-platform;job=backfill-stage-status")
    plan = backfill_plan.load_plan(args.plan)
    blocks = [b.strip() for b in args.blocks.split(",") if b.strip()] if args.blocks else None
    ids = [i for stage in backfill_plan.select_stages(plan, blocks) for i in stage["iteration_ids"]]
    report = stage_report(plan, fetch_per_iteration(ids), blocks)
    if args.since:
        report = only_completed_since(report, datetime.fromisoformat(args.since))
    print(json.dumps(report, indent=1) if args.json else format_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
