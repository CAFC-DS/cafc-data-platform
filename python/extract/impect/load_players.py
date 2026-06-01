"""
Fetch players for all iterations from Impect API and load into Snowflake
Table: CAFC_DB.IMPECT_RAW.PLAYERS

Usage:
    python load_players.py
    python load_players.py --seasons "25/26,24/25,23/24,2026,2025,2024,2023"

--seasons scopes the fetch to the given seasons (matching the iteration
loaders' interface). The player-level facts (ITERATION_PLAYER_KPIS/SCORES)
only cover the last ~3 seasons, so scoping PLAYERS to those keeps the
dimension aligned with what the facts reference instead of pulling every
historical season.
"""
import argparse

import pandas as pd
from impect_api import get_iterations, get_players
from snowflake_loader import json_to_dataframe, flatten_id_mappings, load_to_snowflake

TABLE_NAME = "PLAYERS"


def run(seasons=None):
    print("Fetching iterations...")
    iterations_response = get_iterations()
    iterations = iterations_response.get('data', [])

    if seasons:
        season_set = {s.strip() for s in seasons.split(",")}
        before = len(iterations)
        iterations = [it for it in iterations if it.get('season') in season_set]
        print(f"Season filter {sorted(season_set)}: {len(iterations)}/{before} iterations")

    iteration_ids = [row['id'] for row in iterations]
    print(f"Found {len(iteration_ids)} iterations")

    all_frames = []

    for iteration_id in iteration_ids:
        print(f"  Fetching players for iteration {iteration_id}...")
        try:
            response = get_players(iteration_id)
            df = json_to_dataframe(response)
            if not df.empty:
                df['ITERATION_ID'] = iteration_id
                all_frames.append(df)
        except Exception as e:
            print(f"  Warning: Could not fetch players for iteration {iteration_id}: {e}")
            continue

    if not all_frames:
        print("No player data retrieved")
        return

    combined_df = pd.concat(all_frames, ignore_index=True)
    combined_df = flatten_id_mappings(combined_df)
    print(f"Total rows to load: {len(combined_df)}")

    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seasons", type=str, default=None,
                        help='Comma-separated season strings to scope the fetch, '
                             'e.g. "25/26,24/25,23/24,2026,2025,2024,2023".')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(seasons=args.seasons)
