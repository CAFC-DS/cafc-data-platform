"""
Fetch all iterations from Impect API and load into Snowflake
Table: CAFC_DB.IMPECT_RAW.ITERATIONS
"""
from impect_api import get_iterations
from snowflake_loader import json_to_dataframe, flatten_id_mappings, load_to_snowflake

TABLE_NAME = "ITERATIONS"


def run():
    print(f"Fetching iterations from Impect API...")
    response = get_iterations()

    df = json_to_dataframe(response)
    df = flatten_id_mappings(df)
    print(f"Retrieved {len(df)} iterations")

    load_to_snowflake(df, TABLE_NAME, overwrite=True)


if __name__ == "__main__":
    run()
