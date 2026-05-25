"""
Configuration for Impect API and Snowflake connections
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

# Impect API Configuration
IMPECT_BASE_URL = os.getenv("IMPECT_BASE_URL", "https://api.impect.com")
IMPECT_TOKEN_URL = os.getenv("IMPECT_TOKEN_URL", "https://login.impect.com/auth/realms/production/protocol/openid-connect/token")
IMPECT_USERNAME = os.getenv("IMPECT_USERNAME")
IMPECT_PASSWORD = os.getenv("IMPECT_PASSWORD")

# Snowflake Configuration
SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER")
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "CAFC_DB")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "IMPECT_RAW")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE")
SNOWFLAKE_PRIVATE_KEY_PATH = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH", "keys/rsa_key_unencrypted.pem")

# Request settings
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# Token cache settings
TOKEN_CACHE_FILE = ".impect_token_cache.json"
TOKEN_EXPIRY_HOURS = 24
