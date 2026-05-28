"""
Fetch iteration-level (season aggregate) player KPI averages from Impect API
and load into Snowflake.

Endpoint: GET /v5/customerapi/iterations/{iterationId}/squads/{squadId}/player-kpis
Table:    CAFC_DB.IMPECT_RAW.ITERATION_PLAYER_KPIS

One API call per (iteration, squad) pair. With ~708 iterations × ~25 squads
each ≈ 17,000 calls. Parallelised with ThreadPoolExecutor; default 8 workers
keeps within typical IMPECT rate limits and finishes in ~30-60 minutes.

Discovery phase: walks each iteration once to enumerate its squads.

Usage:
    python load_iteration_player_kpis.py                       # full extract
    python load_iteration_player_kpis.py --limit-iterations 3  # smoke test
    python load_iteration_player_kpis.py --max-workers 4       # gentler rate
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from impect_api import get_iteration_player_kpis, get_iterations, get_squads
from snowflake_loader import flatten_id_mappings, json_to_dataframe, load_to_snowflake

TABLE_NAME = "ITERATION_PLAYER_KPIS"
DEFAULT_MAX_WORKERS = 8


def _fetch_pair(iteration_id: int, squad_id: int):
    """Worker: fetch one (iteration, squad) and return a DataFrame or an error tuple."""
    try:
        response = get_iteration_player_kpis(iteration_id, squad_id)
        df = json_to_dataframe(response)
        if df.empty:
            return None
        df["ITERATION_ID"] = iteration_id
        df["SQUAD_ID"] = squad_id
        return df
    except Exception as exc:  # noqa: BLE001
        return ("error", iteration_id, squad_id, str(exc))


def _discover_pairs(iteration_ids):
    """Walk each iteration once and return the (iteration_id, squad_id) work list."""
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


def run(limit_iterations=None, max_workers=DEFAULT_MAX_WORKERS):
    print("Fetching iterations…")
    iterations_response = get_iterations()
    iteration_ids = [row["id"] for row in iterations_response.get("data", [])]
    if limit_iterations:
        iteration_ids = iteration_ids[:limit_iterations]
    print(f"Discovered {len(iteration_ids)} iterations")

    print("Discovering (iteration, squad) pairs…")
    pairs = _discover_pairs(iteration_ids)
    print(f"Total pairs to fetch: {len(pairs)}")

    print(f"Fetching player KPIs with {max_workers} workers…")
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
    combined_df = flatten_id_mappings(combined_df)
    print(f"Total rows to load: {len(combined_df)}")
    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--limit-iterations", type=int, default=None,
        help="Cap the number of iterations processed. Useful for smoke-tests.",
    )
    parser.add_argument(
        "--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
        help=f"Parallelism for the fetch phase. Default: {DEFAULT_MAX_WORKERS}.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(limit_iterations=args.limit_iterations, max_workers=args.max_workers)
