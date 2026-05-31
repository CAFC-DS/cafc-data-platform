"""
Fetch iteration-level (season aggregate) player standardized SCORES from
Impect API and load into Snowflake INCREMENTALLY, per-iteration.

Endpoint: GET /v5/customerapi/iterations/{iterationId}/squads/{squadId}/player-scores
Table:    CAFC_DB.IMPECT_RAW.ITERATION_PLAYER_SCORES

Same per-iteration write strategy as load_iteration_player_kpis.py —
each iteration's data is committed to Snowflake before moving to the
next, keeping memory bounded and crash-recoverable.

Usage:
    python load_iteration_player_scores.py --seasons "25/26,24/25,23/24"
    python load_iteration_player_scores.py --iteration-id 1410
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from impect_api import get_iteration_player_scores, get_iterations, get_squads
from snowflake_loader import get_connection, load_to_snowflake

TABLE_NAME = "ITERATION_PLAYER_SCORES"
DEFAULT_MAX_WORKERS = 2


def _fetch_loaded_iteration_ids():
    """Return the set of ITERATION_ID values already in CAFC_DB.IMPECT_RAW.<TABLE_NAME>.
    Returns an empty set if the table doesn't exist or is empty."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT DISTINCT ITERATION_ID FROM CAFC_DB.IMPECT_RAW.{TABLE_NAME}"
            )
            return {row[0] for row in cur.fetchall()}
        except Exception:  # noqa: BLE001
            return set()
        finally:
            cur.close()
    finally:
        conn.close()


def _flatten(records, iteration_id, squad_id):
    if not records:
        return pd.DataFrame()
    df = pd.json_normalize(
        records,
        record_path="playerScores",
        meta=["playerId", "position", "playDuration", "matchShare"],
    )
    df["iteration_id"] = iteration_id
    df["squad_id"] = squad_id
    df = df.rename(columns={
        "playerScoreId": "player_score_id",
        "value":         "value",
        "playerId":      "player_id",
        "position":      "position",
        "playDuration":  "play_duration",
        "matchShare":    "match_share",
    })
    return df[["iteration_id", "squad_id", "player_id", "position",
               "play_duration", "match_share", "player_score_id", "value"]]


def _fetch_pair(iteration_id, squad_id):
    try:
        response = get_iteration_player_scores(iteration_id, squad_id)
        data = response.get("data", [])
        df = _flatten(data, iteration_id, squad_id)
        return df if not df.empty else None
    except Exception as exc:  # noqa: BLE001
        return ("error", iteration_id, squad_id, str(exc))


def _filter_iterations_by_season(iterations, seasons):
    season_set = {s.strip() for s in seasons}
    return [it for it in iterations if it.get("season") in season_set]


def _process_one_iteration(iteration, max_workers):
    iter_id = iteration["id"]

    try:
        squads_response = get_squads(iter_id)
        squad_ids = [sq["id"] for sq in squads_response.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        print(f"  iter {iter_id}: failed to fetch squads — {exc}")
        return pd.DataFrame(), 1

    if not squad_ids:
        return pd.DataFrame(), 0

    iteration_frames = []
    errors = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_pair, iter_id, sq): sq for sq in squad_ids}
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            if isinstance(result, tuple) and result and result[0] == "error":
                errors += 1
                continue
            iteration_frames.append(result)

    if not iteration_frames:
        return pd.DataFrame(), errors

    return pd.concat(iteration_frames, ignore_index=True), errors


def run(iteration_id=None, seasons=None, limit_iterations=None,
        max_workers=DEFAULT_MAX_WORKERS, skip_loaded=False):
    print("Fetching iterations…")
    iterations_response = get_iterations()
    iterations = iterations_response.get("data", [])
    print(f"  {len(iterations)} total iterations in IMPECT")

    if iteration_id is not None:
        iterations = [it for it in iterations if it["id"] == iteration_id]
        print(f"Targeted run: iteration_id={iteration_id} ({len(iterations)} match)")
    elif seasons:
        seasons_list = [s.strip() for s in seasons.split(",")]
        iterations = _filter_iterations_by_season(iterations, seasons_list)
        print(f"Season filter {seasons_list}: {len(iterations)} matching iterations")
    elif limit_iterations:
        iterations = iterations[:limit_iterations]
        print(f"Limited to first {limit_iterations}: {len(iterations)} iterations")

    resuming = False
    if skip_loaded:
        already_loaded = _fetch_loaded_iteration_ids()
        before = len(iterations)
        iterations = [it for it in iterations if it["id"] not in already_loaded]
        skipped = before - len(iterations)
        print(f"--skip-loaded: {len(already_loaded)} iterations already in {TABLE_NAME}; "
              f"skipping {skipped}, {len(iterations)} remaining")
        if skipped > 0:
            resuming = True

    if not iterations:
        print("No iterations to process")
        return

    total_rows = 0
    total_errors = 0
    first_write = not resuming

    for i, iteration in enumerate(iterations, start=1):
        iter_id = iteration["id"]
        season = iteration.get("season", "?")
        comp = iteration.get("competition", {}).get("name", "?")

        iter_df, errs = _process_one_iteration(iteration, max_workers)
        total_errors += errs

        if iter_df.empty:
            print(f"  [{i}/{len(iterations)}] iter {iter_id} ({comp} {season}): 0 rows  errs={errs}")
            continue

        load_to_snowflake(iter_df, TABLE_NAME, overwrite=first_write)
        first_write = False
        total_rows += len(iter_df)
        print(f"  [{i}/{len(iterations)}] iter {iter_id} ({comp} {season}): "
              f"+{len(iter_df):,} rows  errs={errs}  running_total={total_rows:,}")

    print(f"\nDONE: {total_rows:,} total rows across {len(iterations)} iterations  "
          f"(errors: {total_errors})")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--iteration-id", type=int, default=None,
                        help="Run for a specific iteration only.")
    parser.add_argument("--seasons", type=str, default=None,
                        help='Comma-separated season strings, e.g. "25/26,24/25,23/24".')
    parser.add_argument("--limit-iterations", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--skip-loaded", action="store_true",
                        help="Resume mode: skip iterations whose ITERATION_ID is already "
                             "in the target Snowflake table.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(iteration_id=args.iteration_id,
        seasons=args.seasons,
        limit_iterations=args.limit_iterations,
        max_workers=args.max_workers,
        skip_loaded=args.skip_loaded)
