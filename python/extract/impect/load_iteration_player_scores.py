"""
Fetch iteration-level (season aggregate) player standardized SCORES from
Impect API and load into Snowflake.

Endpoint: GET /v5/customerapi/iterations/{iterationId}/squads/{squadId}/player-scores
Table:    CAFC_DB.IMPECT_RAW.ITERATION_PLAYER_SCORES

Output shape: one row per (iteration, squad, player, playerScore).
Columns landed: ITERATION_ID, SQUAD_ID, PLAYER_ID, POSITION, PLAY_DURATION,
                MATCH_SHARE, PLAYER_SCORE_ID, VALUE.

Usage:
    python load_iteration_player_scores.py
    python load_iteration_player_scores.py --iteration-id 1410
    python load_iteration_player_scores.py --limit-iterations 3
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from impect_api import get_iteration_player_scores, get_iterations, get_squads
from snowflake_loader import load_to_snowflake

TABLE_NAME = "ITERATION_PLAYER_SCORES"
DEFAULT_MAX_WORKERS = 8


def _flatten(records, iteration_id, squad_id):
    """Explode nested `playerScores` array into one row per (player, score)."""
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


def _discover_pairs(iteration_ids):
    pairs = []
    for i, iteration_id in enumerate(iteration_ids, start=1):
        try:
            response = get_squads(iteration_id)
            for sq in response.get("data", []):
                pairs.append((iteration_id, sq["id"]))
        except Exception as exc:  # noqa: BLE001
            print(f"  warning: failed to fetch squads for iteration {iteration_id}: {exc}")
            continue
        if i % 100 == 0 or i == len(iteration_ids):
            print(f"  discovery: {i}/{len(iteration_ids)} iterations, {len(pairs)} pairs so far")
    return pairs


def run(iteration_id=None, limit_iterations=None, max_workers=DEFAULT_MAX_WORKERS):
    if iteration_id is not None:
        iteration_ids = [iteration_id]
        print(f"Targeted run: iteration_id={iteration_id}")
    else:
        print("Fetching iterations…")
        iterations_response = get_iterations()
        iteration_ids = [row["id"] for row in iterations_response.get("data", [])]
        if limit_iterations:
            iteration_ids = iteration_ids[:limit_iterations]
        print(f"Discovered {len(iteration_ids)} iterations")

    print("Discovering (iteration, squad) pairs…")
    pairs = _discover_pairs(iteration_ids)
    print(f"Total pairs to fetch: {len(pairs)}")

    print(f"Fetching player scores with {max_workers} workers…")
    all_frames = []
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_pair, it, sq): (it, sq) for it, sq in pairs}
        for i, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result is None:
                pass
            elif isinstance(result, tuple) and result and result[0] == "error":
                errors.append(result[1:])
            else:
                all_frames.append(result)
            if i % 500 == 0 or i == len(pairs):
                print(f"  fetched: {i}/{len(pairs)}  (errors so far: {len(errors)})")

    if errors:
        print(f"\n{len(errors)} (iteration, squad) pair(s) failed:")
        for iter_id, squad_id, msg in errors[:10]:
            print(f"  ({iter_id}, {squad_id}): {msg}")
        if len(errors) > 10:
            print(f"  … and {len(errors) - 10} more")

    if not all_frames:
        print("No data retrieved")
        return

    combined_df = pd.concat(all_frames, ignore_index=True)
    print(f"Total rows to load: {len(combined_df)}  (from {len(all_frames)} non-empty pairs)")
    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--iteration-id", type=int, default=None,
                        help="Run for a specific iteration only.")
    parser.add_argument("--limit-iterations", type=int, default=None,
                        help="Cap the number of iterations processed.")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                        help=f"Parallelism. Default: {DEFAULT_MAX_WORKERS}.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(iteration_id=args.iteration_id,
        limit_iterations=args.limit_iterations,
        max_workers=args.max_workers)
