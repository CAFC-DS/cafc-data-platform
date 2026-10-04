"""Container entrypoint for the queue-driven IMPECT event backfill (Railway).

Runs N worker processes of backfill_historical_match_events.py over a scope of
iterations and exits when nothing in that scope can still be done, so a
run-to-completion service stops billing when the work is finished.

Scope (one of these):
  BACKFILL_PLAN            path to a staged plan (see backfill_plan.py): season blocks,
                           newest first, each split by geographic group, run in order
  BACKFILL_BLOCKS          with a plan, only these block labels, e.g. "26/27+2026"
  BACKFILL_ITERATION_IDS   comma-separated IMPECT iteration ids (single scope)
  BACKFILL_SEASONS         comma-separated queue SEASON labels (single scope), e.g. "25/26,2025"
  BACKFILL_DRY_RUN=1       print each stage's live counts and exit without starting workers

Exit codes: 0 when the scope is drained, or a match/time budget was reached;
1 when a stage cannot progress (stalled; later stages still run) or the configuration is wrong.

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
  BACKFILL_MAX_MATCHES               stop after about this many matches (budget, across all stages)
  BACKFILL_MAX_MINUTES               claim no new batch after this many minutes
  BACKFILL_STALE_CLAIM_MINUTES [90]  claims older than this are handed back
  BACKFILL_MAX_STALLED_MINUTES [120] give up after this long with no progress
  BACKFILL_IDLE_POLL_SECONDS [300]   wait between checks while nothing is claimable
  BACKFILL_QUERY_TAG                 base QUERY_TAG; plan stages append ;block=...;group=...
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

import backfill_plan
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


class _Settings:
    """Run settings read once from the environment."""

    def __init__(self) -> None:
        self.workers = _env_int("BACKFILL_WORKERS", 2)
        self.batch_size = _env_int("BACKFILL_BATCH_SIZE", 25)
        self.load_matches = _env_int("BACKFILL_LOAD_MATCHES", 10)
        self.fetch_workers = _env_int("BACKFILL_FETCH_WORKERS", 3)
        self.max_attempts = _env_int("BACKFILL_MAX_ATTEMPTS", 5)
        self.stale_minutes = _env_int("BACKFILL_STALE_CLAIM_MINUTES", 90)
        self.max_stalled_minutes = _env_int("BACKFILL_MAX_STALLED_MINUTES", 120)
        self.idle_poll_seconds = _env_int("BACKFILL_IDLE_POLL_SECONDS", 300)
        self.base_tag = os.environ.get("BACKFILL_QUERY_TAG") or DEFAULT_QUERY_TAG
        self.dry_run = os.environ.get("BACKFILL_DRY_RUN", "").lower() in ("1", "true", "yes")


class _Budget:
    """Match and wall-clock budget shared by every stage of a run."""

    def __init__(self, max_matches: int | None, max_minutes: int | None) -> None:
        self.max_matches = max_matches
        self.deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
        self.done = 0  # matches finished by this run in stages that have ended

    def matches_exhausted(self, in_stage: int = 0) -> bool:
        return self.max_matches is not None and self.done + in_stage >= self.max_matches

    def time_exhausted(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def matches_left(self, in_stage: int = 0) -> int | None:
        return None if self.max_matches is None else self.max_matches - self.done - in_stage


def _prefetch_token() -> None:
    """Log in to IMPECT once here so workers start with a cached token.

    Several processes (and their fetch threads) all finding the cache empty and
    logging in at the same moment is what produced a 401 on the first batch.
    A failure here is not fatal: the workers retry the login themselves.
    """
    try:
        import impect_api
        impect_api.get_auth_token()
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not prefetch an IMPECT token ({exc}); workers will log in themselves", flush=True)


def _run_scope(label: str, iteration_ids: list[int], cfg: _Settings, budget: _Budget,
               query_tag: str, procs: list[subprocess.Popen]) -> str:
    """Run workers over one scope until it is drained.

    Returns "complete", "budget" (match or time budget reached) or "stalled".
    """
    ids_arg = ",".join(str(value) for value in iteration_ids)
    counts = scope_counts(iteration_ids, cfg.max_attempts)
    done_at_start = counts["done"]
    last_progress = time.monotonic()
    print(f"{label}: start, {len(iteration_ids)} iterations, {counts}", flush=True)

    def finish() -> None:
        budget.done += counts["done"] - done_at_start

    round_no = 0
    while True:
        released = release_stale_claims(iteration_ids, cfg.stale_minutes)
        if released:
            print(f"{label}: released {released} stale claim(s)", flush=True)
            counts = scope_counts(iteration_ids, cfg.max_attempts)

        if counts["remaining"] == 0:
            print(f"{label}: complete {counts}", flush=True)
            finish()
            return "complete"
        done_this_stage = counts["done"] - done_at_start
        if budget.matches_exhausted(done_this_stage):
            print(f"{label}: match budget reached ({budget.done + done_this_stage} >= {budget.max_matches}): {counts}",
                  flush=True)
            finish()
            return "budget"
        if budget.time_exhausted():
            print(f"{label}: time budget reached: {counts}", flush=True)
            finish()
            return "budget"

        round_no += 1
        command = [
            sys.executable, "-u", os.path.abspath(__file__), "--worker",
            "--process", "--until-empty", "--batch-size", str(cfg.batch_size),
            "--max-attempts", str(cfg.max_attempts), "--iteration-ids", ids_arg,
            "--load-matches", str(cfg.load_matches), "--fetch-workers", str(cfg.fetch_workers),
            "--query-tag", query_tag,
        ]
        left = budget.matches_left(done_this_stage)
        if left is not None:
            command += ["--max-matches", str(math.ceil(left / cfg.workers))]
        if budget.deadline is not None:
            command += ["--stop-after-minutes", f"{max(0.1, (budget.deadline - time.monotonic()) / 60):.2f}"]
        procs[:] = [subprocess.Popen(command) for _ in range(cfg.workers)]
        for proc in procs:
            proc.wait()

        previous_done = counts["done"]
        counts = scope_counts(iteration_ids, cfg.max_attempts)
        progressed = counts["done"] > previous_done
        if progressed:
            last_progress = time.monotonic()
        print(f"{label}: round {round_no}: {counts} (+{counts['done'] - previous_done})", flush=True)
        if counts["remaining"] == 0:
            continue  # re-checked (and reported) at the top of the loop
        stalled_minutes = (time.monotonic() - last_progress) / 60
        if stalled_minutes >= cfg.max_stalled_minutes:
            print(f"{label}: no progress for {stalled_minutes:.0f} minutes with {counts['remaining']} remaining; "
                  "giving up on this scope", flush=True)
            finish()
            return "stalled"
        if not progressed:
            # Nothing claimable right now (rows held by another runner or cooling down):
            # wait with the warehouse suspended rather than polling every minute.
            time.sleep(cfg.idle_poll_seconds)


def _build_stages(cfg: _Settings) -> list[dict]:
    """The ordered scopes to run: plan stages, or one stage from the id/season variables."""
    plan_path = os.environ.get("BACKFILL_PLAN")
    if plan_path:
        if not os.path.isabs(plan_path):
            plan_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), plan_path)
        plan = backfill_plan.load_plan(plan_path)
        blocks = _csv(os.environ.get("BACKFILL_BLOCKS")) or None
        stages = backfill_plan.select_stages(plan, blocks)
        return [{"label": f"[{stage['block']} | {stage['group']}]", "iteration_ids": stage["iteration_ids"],
                 "tag": f"{cfg.base_tag};block={stage['block']};group={stage['group']}"}
                for stage in stages]
    iteration_ids = resolve_scope()
    return [{"label": "[scope]", "iteration_ids": iteration_ids, "tag": cfg.base_tag}] if iteration_ids else []


def supervise() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if not (os.environ.get("BACKFILL_PLAN") or os.environ.get("BACKFILL_ITERATION_IDS")
            or os.environ.get("BACKFILL_SEASONS")):
        missing.append("BACKFILL_PLAN, BACKFILL_ITERATION_IDS or BACKFILL_SEASONS")
    if missing:
        print(f"missing required env vars: {', '.join(missing)}", flush=True)
        return 1

    cfg = _Settings()
    os.environ.setdefault("SNOWFLAKE_QUERY_TAG", cfg.base_tag)
    try:
        stages = _build_stages(cfg)
    except (OSError, ValueError) as exc:
        print(f"could not build the stage list: {exc}", flush=True)
        return 1
    if not stages:
        print("scope resolved to no iterations; nothing to do", flush=True)
        return 1

    if cfg.dry_run:
        for index, stage in enumerate(stages, 1):
            counts = scope_counts(stage["iteration_ids"], cfg.max_attempts)
            print(f"stage {index}/{len(stages)} {stage['label']}: {len(stage['iteration_ids'])} iterations, {counts}",
                  flush=True)
        print("dry run: no workers started", flush=True)
        return 0

    budget = _Budget(_env_optional_int("BACKFILL_MAX_MATCHES"), _env_optional_int("BACKFILL_MAX_MINUTES"))
    procs: list[subprocess.Popen] = []

    def _on_sigterm(signum, _frame):
        _stop_workers(procs)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_sigterm)
    _prefetch_token()

    stalled: list[str] = []
    for index, stage in enumerate(stages, 1):
        print(f"stage {index}/{len(stages)} {stage['label']}", flush=True)
        outcome = _run_scope(stage["label"], stage["iteration_ids"], cfg, budget, stage["tag"], procs)
        if outcome == "budget":
            print(f"stopping after stage {index}/{len(stages)}: budget reached", flush=True)
            return 1 if stalled else 0
        if outcome == "stalled":
            stalled.append(stage["label"])
    if stalled:
        print(f"finished, but {len(stalled)} stage(s) stalled: {', '.join(stalled)}", flush=True)
        return 1
    print(f"all {len(stages)} stage(s) complete; {budget.done} matches finished this run", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_worker()
    else:
        sys.exit(supervise())
