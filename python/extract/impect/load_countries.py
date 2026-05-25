"""
Fetch all countries from Impect API and load into Snowflake
Table: CAFC_DB.IMPECT_RAW.COUNTRIES
"""
from impect_api import get_countries
from snowflake_loader import json_to_dataframe, load_to_snowflake

TABLE_NAME = "COUNTRIES"


def run():
    print(f"Fetching countries from Impect API...")
    response = get_countries()

    df = json_to_dataframe(response)
    print(f"Retrieved {len(df)} countries")

    load_to_snowflake(df, TABLE_NAME, overwrite=True)


if __name__ == "__main__":
    run()
