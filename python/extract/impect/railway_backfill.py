"""Container entrypoint for the queue-driven IMPECT event backfill (Railway).

Runs N worker processes of backfill_historical_match_events.py over a scope of
iterations and exits when nothing in that scope can still be done, so a
run-to-completion service stops billing when the work is finished.

Scope (at least one is required, both may be given):
  BACKFILL_ITERATION_IDS   comma-separated IMPECT iteration ids
  BACKFILL_SEASONS         comma-separated queue SEASON labels, e.g. "25/26,2025"

Exit codes: 0 when the scope is drained, or a match/time budget was reached;
1 when it cannot progress (stalled) or the configuration is wrong.

"Drained" means no row is left that a worker could still claim.  Matches in a
FAILED cooldown, or whose attempts are used up, are reported but do not keep
the service alive.

Snowflake auth reads the PEM from SNOWFLAKE_PRIVATE_KEY (no key file on disk).

Env (defaults in brackets):
  BACKFILL_WORKERS [2]               parallel worker processes
  BACKFILL_BATCH_SIZE [25]           matches claimed per batch
  BACKFILL_LOAD_MATCHES [10]         matches staged per INSERT ... SELECT
  BACKFILL_FETCH_WORKERS [3]         concurrent IMPECT fetch threads per worker
  BACKFILL_MAX_ATTEMPTS [5]          per-match attempt cap
  BACKFILL_MAX_MATCHES               stop after about this many matches (budget)
  BACKFILL_MAX_MINUTES               claim no new batch after this many minutes
  BACKFILL_STALE_CLAIM_MINUTES [90]  claims older than this are handed back
  BACKFILL_MAX_STALLED_MINUTES [120] give up after this long with no progress
  BACKFILL_IDLE_POLL_SECONDS [300]   wait between checks while nothing is claimable
  BACKFILL_QUERY_TAG                 QUERY_TAG for every Snowflake session
  SNOWFLAKE_*, IMPECT_USERNAME, IMPECT_PASSWORD
"""
from __future__ import annotations

import math
import os
import runpy
import signal
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
    "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_ROLE",
    "SNOWFLAKE_PRIVATE_KEY", "IMPECT_USERNAME", "IMPECT_PASSWORD",
)
DEFAULT_QUERY_TAG = "project=cafc-data-platform;job=impect-backfill"
COOLDOWN_GRACE_MINUTES = 5


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
        session_parameters={"QUERY_TAG": os.environ.get("SNOWFLAKE_QUERY_TAG") or DEFAULT_QUERY_TAG},
    )


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _env_optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value else None


def _csv(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def resolve_scope() -> list[int]:
    """Iteration ids from BACKFILL_ITERATION_IDS plus those queued under BACKFILL_SEASONS."""
    ids = {int(part) for part in _csv(os.environ.get("BACKFILL_ITERATION_IDS"))}
    seasons = _csv(os.environ.get("BACKFILL_SEASONS"))
    if seasons:
        placeholders = ", ".join(f"%(s{index})s" for index in range(len(seasons)))
        params = {f"s{index}": season for index, season in enumerate(seasons)}
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT DISTINCT ITERATION_ID FROM {QUEUE_TABLE} WHERE SEASON IN ({placeholders})", params)
            ids.update(int(row[0]) for row in cur.fetchall())
        finally:
            conn.close()
    return sorted(ids)


def _scope_sql(iteration_ids: list[int]) -> str:
    return ", ".join(str(value) for value in iteration_ids)


def release_stale_claims(iteration_ids: list[int], stale_minutes: int) -> int:
    """Hand back claims whose worker is gone (e.g. a redeploy killed it mid-batch).

    Only called between rounds, when this supervisor has no live worker; the
    threshold keeps other runners' recent claims untouched.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE {QUEUE_TABLE}
               SET STATUS='PENDING', CLAIMED_BY=NULL, CLAIMED_AT=NULL,
                   ATTEMPT_COUNT=GREATEST(ATTEMPT_COUNT - 1, 0), UPDATED_AT=CURRENT_TIMESTAMP()
             WHERE STATUS='RUNNING' AND ITERATION_ID IN ({_scope_sql(iteration_ids)})
               AND CLAIMED_AT < DATEADD(minute, -{int(stale_minutes)}, CURRENT_TIMESTAMP())
            """
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def scope_counts(iteration_ids: list[int], max_attempts: int) -> dict[str, int]:
    """Classify the scoped queue rows.

    remaining  = work a worker can still do: PENDING with attempts left, RUNNING
                 (another process holds it), or FAILED whose cooldown is over.
    deferred   = FAILED rows still cooling down (not waited for).
    exhausted  = PENDING/FAILED rows with no attempts left (not waited for).
    done       = SUCCESS + NO_EVENT_DATA.
    """
    attempts = int(max_attempts)
    soon = f"DATEADD(minute, {COOLDOWN_GRACE_MINUTES}, CURRENT_TIMESTAMP())"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              COUNT_IF(STATUS = 'PENDING' AND ATTEMPT_COUNT < {attempts}) AS pending,
              COUNT_IF(STATUS = 'RUNNING') AS running,
              COUNT_IF(STATUS = 'FAILED' AND ATTEMPT_COUNT < {attempts}
                       AND (NEXT_ATTEMPT_AT IS NULL OR NEXT_ATTEMPT_AT <= {soon})) AS retryable,
              COUNT_IF(STATUS = 'FAILED' AND ATTEMPT_COUNT < {attempts}
                       AND NEXT_ATTEMPT_AT > {soon}) AS deferred,
              COUNT_IF(STATUS IN ('PENDING', 'FAILED') AND ATTEMPT_COUNT >= {attempts}) AS exhausted,
              COUNT_IF(STATUS IN ('SUCCESS', 'NO_EVENT_DATA')) AS done
            FROM {QUEUE_TABLE}
            WHERE ITERATION_ID IN ({_scope_sql(iteration_ids)})
            """
        )
        pending, running, retryable, deferred, exhausted, done = (int(value or 0) for value in cur.fetchone())
        return {
            "remaining": pending + running + retryable,
            "pending": pending, "running": running, "retryable": retryable,
            "deferred": deferred, "exhausted": exhausted, "done": done,
        }
    finally:
        conn.close()


def run_worker() -> None:
    """Run the backfill script in-process with the env-based connection."""
    snowflake_loader.get_connection = get_connection
    sys.argv = ["backfill_historical_match_events.py"] + sys.argv[2:]
    runpy.run_path("backfill_historical_match_events.py", run_name="__main__")


def _stop_workers(procs: list[subprocess.Popen], grace_seconds: int = 20) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()  # workers hand their unfinished claims back on SIGTERM
    deadline = time.monotonic() + grace_seconds
    for proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()


def supervise() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if not (os.environ.get("BACKFILL_ITERATION_IDS") or os.environ.get("BACKFILL_SEASONS")):
        missing.append("BACKFILL_ITERATION_IDS or BACKFILL_SEASONS")
    if missing:
        print(f"missing required env vars: {', '.join(missing)}", flush=True)
        return 1

    os.environ.setdefault("SNOWFLAKE_QUERY_TAG", os.environ.get("BACKFILL_QUERY_TAG") or DEFAULT_QUERY_TAG)
    workers = _env_int("BACKFILL_WORKERS", 2)
    batch_size = _env_int("BACKFILL_BATCH_SIZE", 25)
    load_matches = _env_int("BACKFILL_LOAD_MATCHES", 10)
    fetch_workers = _env_int("BACKFILL_FETCH_WORKERS", 3)
    max_attempts = _env_int("BACKFILL_MAX_ATTEMPTS", 5)
    stale_minutes = _env_int("BACKFILL_STALE_CLAIM_MINUTES", 90)
    max_stalled_minutes = _env_int("BACKFILL_MAX_STALLED_MINUTES", 120)
    idle_poll_seconds = _env_int("BACKFILL_IDLE_POLL_SECONDS", 300)
    max_matches = _env_optional_int("BACKFILL_MAX_MATCHES")
    max_minutes = _env_optional_int("BACKFILL_MAX_MINUTES")

    iteration_ids = resolve_scope()
    if not iteration_ids:
        print("scope resolved to no iterations; nothing to do", flush=True)
        return 1
    ids_arg = ",".join(str(value) for value in iteration_ids)

    started = time.monotonic()
    deadline = started + max_minutes * 60 if max_minutes else None
    procs: list[subprocess.Popen] = []

    def _on_sigterm(signum, _frame):
        _stop_workers(procs)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_sigterm)

    counts = scope_counts(iteration_ids, max_attempts)
    done_at_start = counts["done"]
    last_progress = time.monotonic()
    print(f"start: {len(iteration_ids)} iterations, {counts}", flush=True)

    round_no = 0
    while True:
        released = release_stale_claims(iteration_ids, stale_minutes)
        if released:
            print(f"released {released} stale claim(s)", flush=True)
            counts = scope_counts(iteration_ids, max_attempts)

        if counts["remaining"] == 0:
            print(f"backfill complete: {counts}", flush=True)
            return 0
        done_this_run = counts["done"] - done_at_start
        if max_matches is not None and done_this_run >= max_matches:
            print(f"match budget reached ({done_this_run} >= {max_matches}): {counts}", flush=True)
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            print(f"time budget reached: {counts}", flush=True)
            return 0

        round_no += 1
        command = [
            sys.executable, "-u", os.path.abspath(__file__), "--worker",
            "--process", "--until-empty", "--batch-size", str(batch_size),
            "--max-attempts", str(max_attempts), "--iteration-ids", ids_arg,
            "--load-matches", str(load_matches), "--fetch-workers", str(fetch_workers),
        ]
        if max_matches is not None:
            command += ["--max-matches", str(math.ceil((max_matches - done_this_run) / workers))]
        if deadline is not None:
            command += ["--stop-after-minutes", f"{max(0.1, (deadline - time.monotonic()) / 60):.2f}"]
        procs = [subprocess.Popen(command) for _ in range(workers)]
        for proc in procs:
            proc.wait()

        previous_done = counts["done"]
        counts = scope_counts(iteration_ids, max_attempts)
        progressed = counts["done"] > previous_done
        if progressed:
            last_progress = time.monotonic()
        print(f"round {round_no}: {counts} (+{counts['done'] - previous_done})", flush=True)
        if counts["remaining"] == 0:
            continue  # re-checked (and reported) at the top of the loop
        stalled_minutes = (time.monotonic() - last_progress) / 60
        if stalled_minutes >= max_stalled_minutes:
            print(f"no progress for {stalled_minutes:.0f} minutes with {counts['remaining']} remaining; giving up",
                  flush=True)
            return 1
        if not progressed:
            # Nothing claimable right now (rows held by another runner or cooling down):
            # wait with the warehouse suspended rather than polling every minute.
            time.sleep(idle_poll_seconds)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_worker()
    else:
        sys.exit(supervise())
