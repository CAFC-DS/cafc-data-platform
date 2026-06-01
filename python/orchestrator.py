"""
Refresh orchestrator.

Drives the seven-step refresh loop in plan §2.7:

    1. Open a CORE.INGESTION_RUNS row, capture RUN_ID.
    2. Extract: pull from the source system into IMPECT_RAW.* (or other
       *_RAW landing zone). Today that's the vendored IMPECT loaders;
       future sources add their own extractors.
    3. dbt build --select tag:staging — typed views over raw.
    4. Identity matcher — link / mint / queue. Stamps RUN_ID on every
       row it writes.
    5. dbt build --select tag:dimensions tag:facts tag:app_compat — the
       canonical layer plus the legacy-shape views the app reads.
    6. dbt test — schema + singular tests. Fails the run if any KPI row
       references a player still in PLAYER_IDENTITY_CANDIDATES.
    7. Close the INGESTION_RUNS row with SUCCESS or FAILED.

About the transaction model
---------------------------
The plan describes step 7 as "on success commit; on failure roll back,"
which implies one Snowflake transaction wrapping all seven steps. That's
not actually achievable across subprocess boundaries — dbt runs as a
separate process with its own connections and commits its own writes
incrementally. So this orchestrator implements an *envelope-correlation*
model instead:

    - RUN_ID is stamped on every row written during the refresh.
    - Each step commits its own writes.
    - On failure, INGESTION_RUNS.STATUS = 'FAILED', rows already committed
      stay (recoverable by DELETE WHERE RUN_ID = X if needed).

Same observability as one big transaction; works with subprocesses.

CLI
---
    python -m python.orchestrator refresh --source impect [options]

Options:
    --dry-run        Run matcher in dry-run (no writes); pass --target dev
                     so dbt writes to prefixed dev schemas.
    --skip-extract   Skip step 2. Useful when raw data is already loaded
                     out-of-band, or when iterating on the orchestrator.
    --target NAME    dbt target name. Default: dev.
    --triggered-by S Free-text label written to INGESTION_RUNS.TRIGGERED_BY.
                     Default: $USER (or "manual" if unset).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from python import _snowflake
from python.identity import matcher as matcher_mod


log = logging.getLogger("cafc.orchestrator")
REPO_ROOT = Path(__file__).resolve().parent.parent
DBT_PROJECT_DIR = REPO_ROOT / "dbt"
IMPECT_EXTRACT_DIR = REPO_ROOT / "python" / "extract" / "impect"


# ---------------------------------------------------------------------------
# INGESTION_RUNS envelope
# ---------------------------------------------------------------------------

def open_run(cur, *, source_system: str, dry_run: bool, triggered_by: str) -> int:
    """Insert a CORE.INGESTION_RUNS row with STATUS='RUNNING'; return RUN_ID."""
    cur.execute(
        """
        INSERT INTO CAFC_DB.CORE.INGESTION_RUNS
          (SOURCE_SYSTEM, DRY_RUN, TRIGGERED_BY, STATUS)
        VALUES
          (%(src)s, %(dry)s, %(by)s, 'RUNNING')
        """,
        {"src": source_system, "dry": dry_run, "by": triggered_by},
    )
    cur.execute(
        """
        SELECT MAX(RUN_ID) FROM CAFC_DB.CORE.INGESTION_RUNS
        WHERE SOURCE_SYSTEM = %(src)s AND TRIGGERED_BY = %(by)s
        """,
        {"src": source_system, "by": triggered_by},
    )
    run_id = int(cur.fetchone()[0])
    log.info("Opened INGESTION_RUNS RUN_ID=%d (source=%s, dry_run=%s)",
             run_id, source_system, dry_run)
    return run_id


def close_run(cur, *, run_id: int, status: str, notes: str = "") -> None:
    """Mark the run row finished and commit (caller still owns conn.commit())."""
    cur.execute(
        """
        UPDATE CAFC_DB.CORE.INGESTION_RUNS
           SET STATUS = %(status)s,
               FINISHED_AT = CURRENT_TIMESTAMP(),
               NOTES = COALESCE(NULLIF(%(notes)s, ''), NOTES)
         WHERE RUN_ID = %(rid)s
        """,
        {"status": status, "notes": notes, "rid": run_id},
    )
    log.info("Closed INGESTION_RUNS RUN_ID=%d with STATUS=%s", run_id, status)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run_subprocess(cmd: list[str], cwd: Path, label: str) -> None:
    """Run a subprocess, stream its output, raise on non-zero exit."""
    log.info("[%s] $ %s  (cwd=%s)", label, " ".join(cmd), cwd)
    proc = subprocess.run(cmd, cwd=str(cwd), check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"[{label}] exited non-zero ({proc.returncode})")


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def step_extract_impect() -> None:
    """
    Step 2 — invoke the vendored IMPECT loaders.

    The vendored modules are flat (load_players.py imports `config` directly)
    so we run them as scripts from inside python/extract/impect/. Order
    matters: dimensions before facts so FK relationships are populated.
    """
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    py = str(venv_python) if venv_python.exists() else sys.executable

    # Order: reference data → players/squads → matches → KPIs/championship.
    # Mirrors the original cafc_utils refresh order; tweak when the extractor
    # surface stabilises.
    scripts = [
        "load_countries.py",
        "load_iterations.py",
        "load_squads.py",
        "load_players.py",
        "load_coaches.py",
        "load_stadiums.py",
        "load_matches.py",
        "load_championship_match_details.py",
    ]
    for script in scripts:
        path = IMPECT_EXTRACT_DIR / script
        if not path.exists():
            log.warning("[extract] %s not present; skipping.", script)
            continue
        _run_subprocess([py, str(path)], cwd=IMPECT_EXTRACT_DIR, label=f"extract:{script}")


def step_dbt_build(*, select: str, target: str) -> None:
    """
    Step 3 + step 5 — invoke dbt build with a model selector.

    --indirect-selection=cautious is essential for staged builds: by default
    (eager) dbt runs a test if *any* of its parents are selected, so a
    cross-layer singular test like no_orphan_kpis (which refs a staging model
    AND core_player_id_resolutions) would fire during the staging-only build —
    and fail with "object does not exist" on a fresh target where the canonical
    models aren't built yet. Cautious only runs a test when *all* its parents
    are in the selection, so cross-layer tests defer to the final `dbt test`.
    """
    dbt = REPO_ROOT / ".venv" / "bin" / "dbt"
    cmd = [str(dbt), "build", "--select", select, "--target", target,
           "--indirect-selection", "cautious",
           "--project-dir", str(DBT_PROJECT_DIR)]
    _run_subprocess(cmd, cwd=REPO_ROOT, label=f"dbt build {select}")


def step_dbt_test(*, target: str) -> None:
    """Step 6 — invoke dbt test (separate from build for clearer error attribution)."""
    dbt = REPO_ROOT / ".venv" / "bin" / "dbt"
    cmd = [str(dbt), "test", "--target", target, "--project-dir", str(DBT_PROJECT_DIR)]
    _run_subprocess(cmd, cwd=REPO_ROOT, label="dbt test")


def step_matcher(*, source_system: str, run_id: int, dry_run: bool) -> dict[str, int]:
    """
    Step 4 — invoke the identity matcher in-process.

    Note: the matcher.run() function defaults to opening its own
    INGESTION_RUNS row when --apply. Here we want it to write inside the
    envelope we already opened, so we pass run_id explicitly. matcher.run()
    treats run_id=None as "open a fresh run" and a passed run_id as
    "write inside this run."
    """
    log.info("[matcher] source=%s run_id=%d dry_run=%s", source_system, run_id, dry_run)
    return matcher_mod.run(source_system=source_system, dry_run=dry_run, run_id=run_id)


# ---------------------------------------------------------------------------
# The main refresh loop
# ---------------------------------------------------------------------------

@contextmanager
def _connection():
    conn = _snowflake.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def refresh(
    *,
    source_system: str = "IMPECT",
    dry_run: bool = False,
    skip_extract: bool = False,
    skip_app_compat: bool = False,
    target: str = "dev",
    triggered_by: Optional[str] = None,
) -> int:
    """
    Run one refresh end-to-end. Returns the exit code (0 success, non-zero failure).
    """
    # Canonicalize SOURCE_SYSTEM to uppercase — it's stored uppercase and every
    # downstream comparison (matcher identity joins, override lookups) is
    # case-sensitive. A lowercase --source would make the matcher treat the
    # whole player base as unlinked and re-mint it.
    source_system = source_system.strip().upper()
    triggered_by = triggered_by or os.environ.get("USER", "manual")

    with _connection() as conn:
        # Step 1 — open the envelope. Skipped on dry-runs so the orchestrator
        # writes nothing at all. A dry-run uses RUN_ID=-1 as a sentinel that
        # the matcher's dry-run path never actually persists.
        if dry_run:
            run_id = -1
            log.info("[step 1/7] dry-run: skipping INGESTION_RUNS open; using sentinel RUN_ID=-1")
        else:
            with conn.cursor() as cur:
                run_id = open_run(
                    cur,
                    source_system=source_system,
                    dry_run=dry_run,
                    triggered_by=triggered_by,
                )
            conn.commit()

        try:
            # Step 2 — extract (out-of-process). Only on real refresh; dry-runs
            # skip extract because the loaders themselves don't have a dry-run
            # mode today.
            if skip_extract or dry_run:
                log.info("[step 2/7] extract: skipped (skip_extract=%s, dry_run=%s)",
                         skip_extract, dry_run)
            else:
                if source_system == "IMPECT":
                    step_extract_impect()
                else:
                    raise NotImplementedError(
                        f"No extractor wired for SOURCE_SYSTEM={source_system!r}"
                    )

            # Step 3 — build staging.
            log.info("[step 3/7] dbt build staging")
            step_dbt_build(select="tag:staging", target=target)

            # Step 4 — identity matcher.
            log.info("[step 4/7] identity matcher")
            counts = step_matcher(
                source_system=source_system, run_id=run_id, dry_run=dry_run
            )
            log.info("matcher classification: %s", counts)

            # Step 5 — build canonical (+ app_compat unless deferred).
            # app_compat is skippable because promoting it to prod replaces the
            # existing hand-built APP_COMPAT.PLAYERS/MATCHES views, which per
            # docs/decisions/0001 are deferred until they're formalised as dbt
            # models with dual IDs. Dev runs normally include it.
            canonical_select = ("tag:dimensions tag:facts"
                                 if skip_app_compat
                                 else "tag:dimensions tag:facts tag:app_compat")
            log.info("[step 5/7] dbt build canonical%s",
                     "" if skip_app_compat else " + app_compat")
            step_dbt_build(select=canonical_select, target=target)

            # Step 6 — tests.
            log.info("[step 6/7] dbt test")
            step_dbt_test(target=target)

            # Step 7 — close success. Skipped on dry-run because we never opened.
            if not dry_run:
                with conn.cursor() as cur:
                    close_run(
                        cur,
                        run_id=run_id,
                        status="SUCCESS",
                        notes=f"matcher: {counts}",
                    )
                conn.commit()
            log.info("REFRESH SUCCESS  RUN_ID=%d", run_id)
            return 0

        except Exception as exc:  # noqa: BLE001
            log.exception("REFRESH FAILED — closing run as FAILED")
            if not dry_run:
                try:
                    with conn.cursor() as cur:
                        close_run(
                            cur,
                            run_id=run_id,
                            status="FAILED",
                            notes=f"{type(exc).__name__}: {exc}"[:1000],
                        )
                    conn.commit()
                except Exception:  # noqa: BLE001
                    log.exception("Failed to mark RUN_ID=%d as FAILED. Manual cleanup needed.",
                                  run_id)
            return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CAFC data-platform orchestrator")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("refresh", help="Run one full refresh end-to-end.")
    pr.add_argument("--source", default="IMPECT",
                    help="SOURCE_SYSTEM to refresh (default: IMPECT).")
    pr.add_argument("--dry-run", action="store_true",
                    help="Skip extract; run matcher in dry-run (no writes); use dev target.")
    pr.add_argument("--skip-extract", action="store_true",
                    help="Skip the extract step; useful when data is already loaded.")
    pr.add_argument("--skip-app-compat", action="store_true",
                    help="Build only dimensions + facts in step 5, not the app_compat "
                         "views. Use for prod until the app_compat views are formalised "
                         "(docs/decisions/0001).")
    pr.add_argument("--target", default="dev",
                    help="dbt target name (default: dev).")
    pr.add_argument("--triggered-by", default=None,
                    help="Label for INGESTION_RUNS.TRIGGERED_BY (default: $USER).")
    pr.add_argument("--verbose", "-v", action="store_true",
                    help="Enable DEBUG logging.")
    return p


def main() -> int:
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    if args.command == "refresh":
        return refresh(
            source_system=args.source,
            dry_run=args.dry_run,
            skip_extract=args.skip_extract,
            skip_app_compat=args.skip_app_compat,
            target=args.target,
            triggered_by=args.triggered_by,
        )
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
