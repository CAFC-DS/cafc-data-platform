"""
Shared Snowflake connection helper for the data platform.

Reads credentials from python/extract/impect/.env (which is gitignored) so
the same secrets power both the IMPECT extractor and the identity loader.
Uses RSA key-pair auth to match the existing extractor pattern.

Anyone needing a connection should call get_connection() rather than
constructing one by hand — keeps insecure_mode / ocsp settings consistent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import snowflake.connector
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / "python" / "extract" / "impect" / ".env"


def load_env(env_path: Optional[Path] = None) -> None:
    """Load env vars from the repo's canonical .env. Idempotent."""
    path = env_path or DEFAULT_ENV_PATH
    if path.exists():
        load_dotenv(path, override=False)


def _resolve_key_path(raw_path: str) -> Path:
    """Resolve SNOWFLAKE_PRIVATE_KEY_PATH against the impect/ dir if it's relative."""
    p = Path(raw_path)
    if p.is_absolute():
        return p
    return REPO_ROOT / "python" / "extract" / "impect" / raw_path


def get_connection(
    *,
    role: Optional[str] = None,
    warehouse: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = None,
) -> snowflake.connector.SnowflakeConnection:
    """
    Open a Snowflake connection using RSA key auth.

    Any explicit kwarg wins over the env var of the same name; the env var
    falls back to a sensible default where applicable.
    """
    load_env()

    account = os.environ["SNOWFLAKE_ACCOUNT"]
    user    = os.environ["SNOWFLAKE_USER"]
    key_path = _resolve_key_path(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"])

    with key_path.open("rb") as f:
        private_key = load_pem_private_key(f.read(), password=None)

    return snowflake.connector.connect(
        account=account,
        user=user,
        private_key=private_key,
        role=role           or os.getenv("SNOWFLAKE_ROLE"),
        warehouse=warehouse or os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=database   or os.getenv("SNOWFLAKE_DATABASE", "CAFC_DB"),
        schema=schema       or os.getenv("SNOWFLAKE_SCHEMA", "CORE"),
        ocsp_fail_open=True,
        insecure_mode=True,
        # Confirmed live (2026-07-29): a single connection held open for a
        # ~5-hour SkillCorner backfill died with "Authentication token has
        # expired" right at the very end (2995/3005 matches already loaded)
        # -- Snowflake's session token isn't renewed by default on a
        # long-lived connection. This makes the connector send periodic
        # heartbeats so multi-hour backfill runs survive to completion.
        client_session_keep_alive=True,
    )
