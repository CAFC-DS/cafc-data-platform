"""
Simple procedural functions to load data into Snowflake
"""
import snowflake.connector
import pandas as pd
from snowflake.connector.pandas_tools import write_pandas
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import config


def get_connection():
    """
    Create a connection to Snowflake using RSA key pair authentication

    Returns:
        Snowflake connection object
    """
    with open(config.SNOWFLAKE_PRIVATE_KEY_PATH, 'rb') as key_file:
        private_key = load_pem_private_key(key_file.read(), password=None)

    conn = snowflake.connector.connect(
        account=config.SNOWFLAKE_ACCOUNT,
        user=config.SNOWFLAKE_USER,
        private_key=private_key,
        database=config.SNOWFLAKE_DATABASE,
        schema=config.SNOWFLAKE_SCHEMA,
        warehouse=config.SNOWFLAKE_WAREHOUSE,
        role=config.SNOWFLAKE_ROLE,
        ocsp_fail_open=True,
        insecure_mode=True
    )
    return conn


def flatten_id_mappings(df: pd.DataFrame) -> pd.DataFrame:
    """
    Expand the idMappings column into flat columns: wyscout_id, heim_spiel_id, skill_corner_id
    Drops the original idMappings column.

    Args:
        df: DataFrame containing an idMappings column

    Returns:
        DataFrame with idMappings replaced by flat ID columns
    """
    if 'idMappings' not in df.columns:
        return df

    def extract_mapping(mappings, key):
        if not isinstance(mappings, list):
            return None
        for m in mappings:
            if isinstance(m, dict) and key in m:
                values = m[key]
                return values[0] if values else None
        return None

    df['wyscout_id']     = df['idMappings'].apply(lambda x: extract_mapping(x, 'wyscout'))
    df['heim_spiel_id']  = df['idMappings'].apply(lambda x: extract_mapping(x, 'heim_spiel'))
    df['skill_corner_id'] = df['idMappings'].apply(lambda x: extract_mapping(x, 'skill_corner'))
    df = df.drop(columns=['idMappings'])

    return df


def json_to_dataframe(response: dict) -> pd.DataFrame:
    """
    Convert an Impect API JSON response to a pandas DataFrame

    Args:
        response: JSON response dict from Impect API

    Returns:
        Pandas DataFrame
    """
    if 'data' in response:
        records = response['data']
    elif isinstance(response, list):
        records = response
    else:
        records = [response]

    if not records:
        return pd.DataFrame()

    return pd.json_normalize(records)


def load_to_snowflake(df: pd.DataFrame, table_name: str, overwrite: bool = True):
    """
    Load a DataFrame into a Snowflake table using write_pandas (TRUNCATE + INSERT)

    Args:
        df: Pandas DataFrame to load
        table_name: Target table name (auto-created if it doesn't exist)
        overwrite: If True, truncates the table before loading (default True)
    """
    if df.empty:
        print(f"No data to load into {table_name}")
        return

    # Snowflake column names must be uppercase
    df.columns = [col.upper() for col in df.columns]

    conn = get_connection()
    try:
        success, num_chunks, num_rows, output = write_pandas(
            conn=conn,
            df=df,
            table_name=table_name.upper(),
            database=config.SNOWFLAKE_DATABASE,
            schema=config.SNOWFLAKE_SCHEMA,
            overwrite=overwrite,
            auto_create_table=True
        )

        if success:
            print(f"Loaded {num_rows} rows into {config.SNOWFLAKE_DATABASE}.{config.SNOWFLAKE_SCHEMA}.{table_name.upper()}")
        else:
            print(f"Load failed for {table_name}: {output}")
    finally:
        conn.close()
