"""
Fetch per-player physical summary data from the SkillCorner API and load into
Snowflake. Table: CAFC_DB.SKILLCORNER_RAW.PHYSICAL_SUMMARY (auto-created on
first load -- see snowflake/ddl/20260727_skillcorner_raw_schema.sql for why).

Confirmed live before writing this loader: the club's SkillCorner account
only has real match data for the 2024/2025 (competition_edition=890) and
2025/2026 (competition_edition=1216) Championship editions -- every earlier
edition listed in SkillCorner's catalogue returns zero matches for this
account. This is a build-and-test loader, not a full historical backfill
runner -- point it at a specific competition edition or explicit match ids
and prove it on a small sample first.

Idempotent -- matches already present in PHYSICAL_SUMMARY are skipped unless
--force is passed.

Usage:
    # Test on a handful of specific matches:
    python load_physical_summary.py --match-ids 2069332,2067736

    # All matches in one competition edition (one Championship season):
    python load_physical_summary.py --competition-edition 1216
"""
import argparse
import re
import sys

import pandas as pd
import requests
from snowflake.connector.pandas_tools import write_pandas

import config
import skillcorner_api as api

sys.path.insert(0, "../../..")
from python import _snowflake  # noqa: E402

TABLE_NAME = "PHYSICAL_SUMMARY"


def _clean_column(col: str) -> str:
    """'M/min' -> 'M_MIN', 'PSV-99' -> 'PSV_99', 'HSR Distance TIP' -> 'HSR_DISTANCE_TIP'."""
    col = col.strip().upper()
    col = re.sub(r"[^A-Z0-9]+", "_", col)
    return col.strip("_")


def _open_run(cur, triggered_by: str) -> int:
    cur.execute(
        """
        INSERT INTO CAFC_DB.CORE.INGESTION_RUNS (SOURCE_SYSTEM, TRIGGERED_BY, NOTES)
        VALUES (%(src)s, %(by)s, %(notes)s)
        """,
        {"src": "SKILLCORNER", "by": triggered_by, "notes": "skillcorner physical summary extractor run"},
    )
    cur.execute("SELECT MAX(RUN_ID) FROM CAFC_DB.CORE.INGESTION_RUNS")
    return int(cur.fetchone()[0])


def _close_run(cur, run_id: int, status: str, notes: str = "") -> None:
    cur.execute(
        """
        UPDATE CAFC_DB.CORE.INGESTION_RUNS
           SET STATUS = %(status)s, FINISHED_AT = CURRENT_TIMESTAMP(), NOTES = %(notes)s
         WHERE RUN_ID = %(rid)s
        """,
        {"status": status, "notes": notes, "rid": run_id},
    )


def _already_loaded_match_ids(cur) -> set:
    """Returns match ids as strings. MATCH_ID lands as VARCHAR (auto_create_table
    inferred it from the CSV's string values) -- confirmed live that comparing
    against the API's int match ids silently never matched and re-inserted
    duplicates every run. Callers must compare via str(match_id)."""
    try:
        cur.execute(f"SELECT DISTINCT MATCH_ID FROM CAFC_DB.{config.SNOWFLAKE_SCHEMA}.{TABLE_NAME}")
        return {str(row[0]) for row in cur.fetchall()}
    except Exception:
        # Table doesn't exist yet (first-ever run) -- nothing loaded.
        return set()


def fetch_physical_for_match(match_id: int, run_id: int) -> list[dict]:
    """
    Returns [] for a match with no physical data available. Confirmed live
    (2026-07-28 backfill run) that SkillCorner returns a plain 404 for
    matches it has no physical data for -- crashed the whole backfill the
    first time this loader hit one, 413/3005 matches in. Treated the same
    way Impect's "no event data" 400 is handled: skip and move on, don't
    let one unavailable match take down an unattended run.
    """
    try:
        rows = api.get_match_physical(match_id)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(f"  match {match_id}: no physical data available (404), skipping")
            return []
        raise
    if not rows:
        return []
    cleaned = []
    for row in rows:
        cleaned_row = {_clean_column(k): v for k, v in row.items()}
        cleaned_row["INGESTION_RUN_ID"] = run_id
        cleaned.append(cleaned_row)
    return cleaned


def load_physical(rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    conn = _snowflake.get_connection(schema=config.SNOWFLAKE_SCHEMA)
    try:
        success, _, num_rows, _ = write_pandas(
            conn=conn,
            df=df,
            table_name=TABLE_NAME,
            database=config.SNOWFLAKE_DATABASE,
            schema=config.SNOWFLAKE_SCHEMA,
            overwrite=False,
            auto_create_table=True,
        )
        if not success:
            raise RuntimeError(f"write_pandas reported failure loading {TABLE_NAME}")
        return num_rows
    finally:
        conn.close()


def _matches_for_edition(competition_edition: int) -> list[int]:
    matches = []
    offset = 0
    while True:
        resp = api.get_matches(competition_edition, limit=500, offset=offset)
        results = resp.get("results", [])
        if not results:
            break
        matches.extend(results)
        offset += 500
        if offset >= resp.get("count", 0):
            break
    return [m["id"] for m in matches]


def run(competition_edition: int = None, match_ids: list[int] = None, all_available: bool = False,
        force: bool = False, triggered_by: str = "load_physical_summary.py") -> dict:
    if not competition_edition and not match_ids and not all_available:
        raise ValueError("Must supply --competition-edition, --match-ids, or --all-available")

    conn = _snowflake.get_connection(schema=config.SNOWFLAKE_SCHEMA)
    try:
        with conn.cursor() as cur:
            run_id = _open_run(cur, triggered_by)
            conn.commit()
            already_loaded = set() if force else _already_loaded_match_ids(cur)

        if match_ids:
            targets = list(match_ids)
        elif all_available:
            # Not every competition Impect/DVMS-style -- this account's real
            # access spans several competitions/seasons, not just Charlton's
            # own (confirmed live: 7 editions, 3,005 matches). Discover
            # rather than assume, since what's licensed can change.
            editions = api.get_available_competition_editions()
            print(f"{len(editions)} available competition edition(s) discovered.")
            targets = []
            for ed in editions:
                targets.extend(_matches_for_edition(ed["competition_edition_id"]))
        else:
            targets = _matches_for_edition(competition_edition)

        print(f"{len(targets)} candidate match(es); {len(already_loaded)} already loaded.")

        total_rows = 0
        matches_loaded = 0
        matches_skipped = 0
        matches_no_data = 0

        for match_id in targets:
            if str(match_id) in already_loaded:
                matches_skipped += 1
                continue
            rows = fetch_physical_for_match(match_id, run_id)
            if not rows:
                matches_no_data += 1
                print(f"  match {match_id}: no physical data available, skipping")
                continue
            n = load_physical(rows)
            total_rows += n
            matches_loaded += 1
            print(f"  match {match_id}: loaded {n} player rows")

        with conn.cursor() as cur:
            _close_run(
                cur, run_id, "SUCCESS",
                notes=f"matches_loaded={matches_loaded} rows={total_rows} "
                      f"skipped_existing={matches_skipped} no_data={matches_no_data}",
            )
            conn.commit()

        summary = {
            "matches_loaded": matches_loaded,
            "rows_loaded": total_rows,
            "matches_skipped_existing": matches_skipped,
            "matches_no_data": matches_no_data,
        }
        print(f"Done: {summary}")
        return summary
    finally:
        conn.close()


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--competition-edition", type=int, default=None,
                   help="Pull physical data for every match in this SkillCorner competition edition id.")
    p.add_argument("--match-ids", type=str, default=None,
                   help="Comma-separated SkillCorner match ids to pull directly (test/backfill-slice mode).")
    p.add_argument("--all-available", action="store_true",
                   help="Discover and pull every competition edition this account actually has match data for "
                        "(not just Charlton's own competition) -- see config.py for what's currently licensed.")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch and re-insert matches even if already present.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    match_ids = [int(x) for x in args.match_ids.split(",")] if args.match_ids else None
    try:
        run(competition_edition=args.competition_edition, match_ids=match_ids,
            all_available=args.all_available, force=args.force)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
