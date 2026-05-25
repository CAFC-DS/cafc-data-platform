"""
Fetch matches for iterations from 2020/21 onwards from Impect API and load into Snowflake
Table: CAFC_DB.IMPECT_RAW.MATCHES
"""
import pandas as pd
from impect_api import get_iterations, get_matches
from snowflake_loader import json_to_dataframe, flatten_id_mappings, load_to_snowflake

TABLE_NAME = "MATCHES"


def is_2020_onwards(season):
    """Return True if the season is 2020/21 or later."""
    s = str(season).strip()
    if '/' in s:
        try:
            yy = int(s.split('/')[0].strip())
            # yy=20 → 2020/21, yy=21 → 2021/22, etc.
            return 20 <= yy <= 50
        except ValueError:
            return False
    else:
        try:
            return int(s) >= 2020
        except ValueError:
            return False


def run():
    print("Fetching iterations...")
    iterations_response = get_iterations()
    all_iterations = iterations_response.get('data', [])
    filtered = [row for row in all_iterations if is_2020_onwards(row.get('season', ''))]
    iteration_ids = [row['id'] for row in filtered]
    print(f"Found {len(all_iterations)} total iterations, {len(iteration_ids)} from 2020/21 onwards")

    all_frames = []

    for iteration_id in iteration_ids:
        print(f"  Fetching matches for iteration {iteration_id}...")
        try:
            response = get_matches(iteration_id)
            df = json_to_dataframe(response)
            if not df.empty:
                all_frames.append(df)
        except Exception as e:
            print(f"  Warning: Could not fetch matches for iteration {iteration_id}: {e}")
            continue

    if not all_frames:
        print("No match data retrieved")
        return

    combined_df = pd.concat(all_frames, ignore_index=True)
    combined_df = flatten_id_mappings(combined_df)
    print(f"Total rows to load: {len(combined_df)}")

    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


if __name__ == "__main__":
    run()
