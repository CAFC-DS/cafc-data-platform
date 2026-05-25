"""
Fetch stadiums for all iterations from Impect API and load into Snowflake
Table: CAFC_DB.IMPECT_RAW.STADIUMS
"""
import pandas as pd
from impect_api import get_iterations, get_stadiums
from snowflake_loader import json_to_dataframe, flatten_id_mappings, load_to_snowflake

TABLE_NAME = "STADIUMS"


def run():
    print("Fetching iterations...")
    iterations_response = get_iterations()
    iteration_ids = [row['id'] for row in iterations_response.get('data', [])]
    print(f"Found {len(iteration_ids)} iterations")

    all_frames = []

    for iteration_id in iteration_ids:
        print(f"  Fetching stadiums for iteration {iteration_id}...")
        try:
            response = get_stadiums(iteration_id)
            df = json_to_dataframe(response)
            if not df.empty:
                df['ITERATION_ID'] = iteration_id
                all_frames.append(df)
        except Exception as e:
            print(f"  Warning: Could not fetch stadiums for iteration {iteration_id}: {e}")
            continue

    if not all_frames:
        print("No stadium data retrieved")
        return

    combined_df = pd.concat(all_frames, ignore_index=True)
    combined_df = flatten_id_mappings(combined_df)
    print(f"Total rows to load: {len(combined_df)}")

    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


if __name__ == "__main__":
    run()
