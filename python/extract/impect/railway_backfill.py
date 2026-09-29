"""Container entrypoint for the queue-driven IMPECT event backfill (Railway).

Runs N worker processes of backfill_historical_match_events.py scoped to
BACKFILL_ITERATION_IDS and exits 0 once nothing in that scope can still be
claimed, so a run-to-completion service stops billing when the work is done.

Snowflake auth reads the PEM from SNOWFLAKE_PRIVATE_KEY (no key file on disk).

Env:
  BACKFILL_ITERATION_IDS   comma-separated IMPECT iteration ids (required)
  BACKFILL_WORKERS         parallel workers (default 4)
  BACKFILL_BATCH_SIZE      matches claimed per batch (default 25)
  BACKFILL_MAX_ATTEMPTS    per-match attempt cap, as in the backfill script (default 5)
  BACKFILL_MAX_STALLED     rounds with no progress before giving up (default 5)
  SNOWFLAKE_*, IMPECT_USERNAME, IMPECT_PASSWORD
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
import time

import snowflake.connector
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, load_pem_private_key,
)

import snowflake_loader

QUEUE_TABLE = "CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE"
REQUIRED_ENV = (
    "BACKFILL_ITERATION_IDS", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ROLE", "SNOWFLAKE_PRIVATE_KEY", "IMPECT_USERNAME", "IMPECT_PASSWORD",
)


def get_connection():
    pem = os.environ["SNOWFLAKE_PRIVATE_KEY"].replace("\\n", "\n").encode()
    key = load_pem_private_key(pem, password=None)
    der = key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key=der,
        database=os.environ.get("SNOWFLAKE_DATABASE", "CAFC_DB"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "IMPECT_RAW"),
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ["SNOWFLAKE_ROLE"],
    )


def _parse_ids(raw: str) -> list[int]:
    return sorted({int(part) for part in raw.split(",") if part.strip()})


def _scope_counts(iteration_ids: list[int], max_attempts: int) -> tuple[int, int]:
    """Return (remaining, done) for the scoped iterations.

    remaining = rows a worker can still claim now or later: PENDING, RUNNING
    (a sibling may hold it; stale claims are reclaimed after 12h), or FAILED
    with attempts left.  done = SUCCESS + NO_EVENT_DATA.
    """
    scope = ", ".join(str(value) for value in iteration_ids)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              COUNT_IF(STATUS IN ('PENDING', 'RUNNING')
                       OR (STATUS = 'FAILED' AND ATTEMPT_COUNT < {int(max_attempts)})),
              COUNT_IF(STATUS IN ('SUCCESS', 'NO_EVENT_DATA'))
            FROM {QUEUE_TABLE}
            WHERE ITERATION_ID IN ({scope})
            """
        )
        remaining, done = cur.fetchone()
        return int(remaining), int(done)
    finally:
        conn.close()


def run_worker() -> None:
    """Run the backfill script in-process with the env-based connection."""
    snowflake_loader.get_connection = get_connection
    sys.argv = ["backfill_historical_match_events.py"] + sys.argv[2:]
    runpy.run_path("backfill_historical_match_events.py", run_name="__main__")


def supervise() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        print(f"missing required env vars: {', '.join(missing)}", flush=True)
        return 1

    iteration_ids = _parse_ids(os.environ["BACKFILL_ITERATION_IDS"])
    workers = int(os.environ.get("BACKFILL_WORKERS", "4"))
    batch_size = os.environ.get("BACKFILL_BATCH_SIZE", "25")
    max_attempts = int(os.environ.get("BACKFILL_MAX_ATTEMPTS", "5"))
    max_stalled = int(os.environ.get("BACKFILL_MAX_STALLED", "5"))
    ids_arg = ",".join(str(value) for value in iteration_ids)

    stalled = 0
    round_no = 0
    remaining, done = _scope_counts(iteration_ids, max_attempts)
    print(f"start: {len(iteration_ids)} iterations, remaining={remaining} done={done}", flush=True)

    while remaining > 0:
        round_no += 1
        procs = [
            subprocess.Popen([
                sys.executable, "-u", os.path.abspath(__file__), "--worker",
                "--process", "--until-empty", "--batch-size", batch_size,
                "--max-attempts", str(max_attempts), "--iteration-ids", ids_arg,
            ])
            for _ in range(workers)
        ]
        for proc in procs:
            proc.wait()

        remaining, new_done = _scope_counts(iteration_ids, max_attempts)
        print(f"round {round_no}: remaining={remaining} done={new_done} (+{new_done - done})", flush=True)
        stalled = 0 if new_done > done else stalled + 1
        done = new_done
        if remaining == 0:
            break
        if stalled >= max_stalled:
            print(f"no progress for {stalled} rounds with {remaining} remaining; giving up", flush=True)
            return 1
        # Workers also exit while rows sit in FAILED cooldown or a sibling holds
        # the last claims; wait before checking the queue again.
        time.sleep(60)

    print(f"backfill complete: done={done}", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_worker()
    else:
        sys.exit(supervise())
