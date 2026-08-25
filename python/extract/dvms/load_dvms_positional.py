"""
Entry point: PUT the raw positional tracking feed (Second Spectrum Data
JSONL, ASSET_SUBTYPE=38 — confirmed ~420MB/match) to
CAFC_DB.DVMS_RAW.POSITIONAL_STAGE for assets already known via
DVMS_RAW.ASSETS (written by load_dvms_fixtures.py — this script does not
call the fixtures API at all, it works purely off what's already cataloged).

Only JSONL is staged, not the equivalent XML (ASSET_SUBTYPE=39) — same data,
no reason to pay to store both.

Deliberately separate from load_dvms_fixtures.py: these files are ~30-50x
bigger than everything else this pipeline handles, so the mechanics differ
throughout — stream to a local temp file (never held in memory), PUT to a
stage (not an inline VARCHAR write_pandas call), and a much longer per-item
duration that makes the small-asset pacing model irrelevant (a single
~420MB download already takes minutes; no need to additionally sleep
between them).

Idempotent like the rest of this pipeline: an asset already marked
STAGED_AT is skipped. Given the size and cost of this operation, it does
NOT run unscoped by default — pass --fixture-id to stage one match at a
time, or --limit N to cap how many it attempts in one run.

Usage:
    python -m python.extract.dvms.load_dvms_positional --limit 1
    python -m python.extract.dvms.load_dvms_positional --fixture-id <id>
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from python import _snowflake
from python.extract.dvms import config, dvms_auth, dvms_client

log = logging.getLogger("cafc.extract.dvms.positional")

STAGE = "@CAFC_DB.DVMS_RAW.POSITIONAL_STAGE"
POSITIONAL_SUBTYPE = 38  # SecondSpectrumDataJSON(L) — see DVMS API docs DataSubType enum


def _pending_assets(conn, limit: int | None, fixture_ids: str | None) -> list[tuple[str, str, str]]:
    """(fixture_id, asset_id, asset_key) for positional assets not yet staged."""
    where = ["ASSET_SUBTYPE = %(subtype)s", "STAGED_AT IS NULL"]
    params = {"subtype": POSITIONAL_SUBTYPE}
    if fixture_ids:
        values = [value.strip() for value in fixture_ids.split(",") if value.strip()]
        placeholders = []
        for index, fixture_id in enumerate(values):
            key = f"fid_{index}"
            placeholders.append(f"%({key})s")
            params[key] = fixture_id
        where.append(f"FIXTURE_ID IN ({', '.join(placeholders)})")

    sql = f"""
        SELECT FIXTURE_ID, ASSET_ID, ASSET_KEY
        FROM CAFC_DB.DVMS_RAW.ASSETS
        WHERE {' AND '.join(where)}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ASSET_ID ORDER BY LOADED_AT DESC) = 1
        ORDER BY FIXTURE_ID
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _stage_one(conn, session, competition_id: str, fixture_id: str, asset_id: str, asset_key: str) -> bool:
    """Download one positional file to a local temp path, PUT it to the
    stage, delete the local copy, and mark it staged. Returns True on
    success. Raises load_dvms_fixtures.RateLimitedError on a 403 (imported
    lazily to avoid a hard dependency between the two scripts for anything
    but this shared error type)."""
    from python.extract.dvms.load_dvms_fixtures import RateLimitedError  # noqa: PLC0415

    url = f"{config.DVMS_BASE_URL}/dvms/{competition_id}/fixtures/{fixture_id}/download/{asset_id}"
    remote_path = f"{STAGE}/{asset_key}"

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = Path(tmpdir) / Path(asset_key).name
        log.info("Downloading %s -> %s", asset_key, local_path)
        t0 = time.time()
        try:
            with session.get(url, timeout=config.POSITIONAL_DOWNLOAD_TIMEOUT, stream=True) as response:
                if response.status_code == 403:
                    raise RateLimitedError(f"403 downloading positional asset {asset_id} (fixture {fixture_id})")
                response.raise_for_status()
                with local_path.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
        except Exception:
            log.warning("Download failed for positional asset %s (fixture %s)", asset_id, fixture_id)
            raise

        size_mb = local_path.stat().st_size / 1e6
        log.info("Downloaded %.1fMB in %.0fs, uploading to stage...", size_mb, time.time() - t0)

        t1 = time.time()
        with conn.cursor() as cur:
            # PUT wants a file:// URI; parallel=4 speeds up the compress+upload
            # for a single large file (Snowflake chunks it internally).
            cur.execute(f"PUT file://{local_path} {remote_path.rsplit('/', 1)[0]}/ AUTO_COMPRESS=TRUE PARALLEL=4 OVERWRITE=TRUE")
            cur.execute(
                """
                UPDATE CAFC_DB.DVMS_RAW.ASSETS
                   SET STAGED_AT = CURRENT_TIMESTAMP()
                 WHERE ASSET_ID = %(aid)s
                """,
                {"aid": asset_id},
            )
        conn.commit()
        log.info("Staged %s (%.1fMB) in %.0fs", asset_key, size_mb, time.time() - t1)
        return True


def run(competition_id: str, limit: int | None, fixture_ids: str | None) -> dict:
    conn = _snowflake.get_connection(schema=config.SNOWFLAKE_SCHEMA)
    try:
        pending = _pending_assets(conn, limit, fixture_ids)
        log.info("%d positional asset(s) pending staging.", len(pending))
        if not pending:
            return {"staged": 0, "failed": 0}

        token = dvms_auth.authenticate()
        session = dvms_client.build_session(token)

        staged, failed = 0, 0
        for fid, aid, key in pending:
            try:
                if _stage_one(conn, session, competition_id, fid, aid, key):
                    staged += 1
            except Exception as exc:
                failed += 1
                log.warning("Giving up on asset %s (fixture %s): %s", aid, fid, exc)
                if type(exc).__name__ == "RateLimitedError":
                    log.error("Rate limited — aborting remaining positional pulls this run.")
                    break

        log.info("Done: %d staged, %d failed.", staged, failed)
        return {"staged": staged, "failed": failed}
    finally:
        conn.close()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage raw DVMS positional tracking files (~420MB/match) to Snowflake.")
    p.add_argument("--competition-id", default=config.DVMS_COMPETITION_ID)
    p.add_argument("--limit", type=int, default=None, help="Max number of files to stage this run.")
    p.add_argument("--fixture-id", default=None,
                   help="Stage one or more comma-separated fixture IDs only.")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_argparser().parse_args()
    result = run(competition_id=args.competition_id, limit=args.limit, fixture_ids=args.fixture_id)
    if result["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
