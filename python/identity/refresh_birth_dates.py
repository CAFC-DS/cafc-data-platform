"""
Refresh CORE.PLAYERS.BIRTH_DATE from the provider's latest season.

Why: mint.py stamps BIRTH_DATE once, from whatever the provider said at mint
time, and nothing updated it afterwards. Providers correct DOBs between
seasons (e.g. George Brown, IMPECT 224261: 2004-05-22 in iteration 1074,
2006-01-01 in every later iteration), so CORE drifted and the app showed the
wrong age.

Rule, per active canonical player with a provider identity (best identity
ranked as in app_compat/players_base.sql — primary, then match confidence,
then identity id), using the provider row with the highest ITERATION_ID:

  - Provider DOB NULL / unparseable  → leave CORE alone (never blank a DOB).
  - Player minted from this provider → take the provider DOB.
  - Any other origin (e.g. MANUAL)   → only fill a NULL BIRTH_DATE; a
                                       hand-entered DOB is never overwritten.

Idempotent. The orchestrator runs it before the matcher so that
(normalized name, birth_date) matching sees corrected DOBs.
"""

from __future__ import annotations

import argparse
import logging
import sys

from python import _snowflake


log = logging.getLogger("cafc.identity.refresh_birth_dates")

SUPPORTED_SOURCES = {"IMPECT"}

# Shared by the dry-run count and the live MERGE so both see the same rows.
_CHANGES_SQL = """
    WITH ident AS (
        SELECT CAFC_PLAYER_ID, SOURCE_PLAYER_ID
        FROM CAFC_DB.CORE.PLAYER_IDENTITIES
        WHERE SOURCE_SYSTEM = %(src)s
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY CAFC_PLAYER_ID
            ORDER BY CASE WHEN IS_PRIMARY THEN 0 ELSE 1 END,
                     MATCH_CONFIDENCE DESC NULLS LAST,
                     PLAYER_IDENTITY_ID
        ) = 1
    ),
    latest AS (
        SELECT ID::VARCHAR AS source_player_id,
               TRY_TO_DATE(BIRTHDATE::VARCHAR) AS birth_date
        FROM CAFC_DB.IMPECT_RAW.PLAYERS
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY ID ORDER BY ITERATION_ID DESC NULLS LAST
        ) = 1
    )
    SELECT p.CAFC_PLAYER_ID, p.BIRTH_DATE AS old_birth_date,
           l.birth_date AS new_birth_date
    FROM CAFC_DB.CORE.PLAYERS p
    JOIN ident  i ON i.CAFC_PLAYER_ID = p.CAFC_PLAYER_ID
    JOIN latest l ON l.source_player_id = i.SOURCE_PLAYER_ID
    WHERE COALESCE(p.IS_ACTIVE, TRUE)
      AND l.birth_date IS NOT NULL
      AND p.BIRTH_DATE IS DISTINCT FROM l.birth_date
      AND (p.CREATED_FROM_SOURCE = %(src)s OR p.BIRTH_DATE IS NULL)
"""


def refresh_birth_dates(cur, source_system: str = "IMPECT", dry_run: bool = True) -> int:
    """Return the number of CORE.PLAYERS rows changed (or that would change)."""
    source_system = source_system.strip().upper()
    if source_system not in SUPPORTED_SOURCES:
        raise NotImplementedError(
            f"No birth-date refresh wired for SOURCE_SYSTEM={source_system!r}"
        )
    params = {"src": source_system}

    if dry_run:
        cur.execute(f"SELECT COUNT(*) FROM ({_CHANGES_SQL})", params)
        n = int(cur.fetchone()[0])
        log.info("[dry-run] %d CORE.PLAYERS birth dates would be refreshed.", n)
        return n

    cur.execute(
        f"""
        MERGE INTO CAFC_DB.CORE.PLAYERS p
        USING ({_CHANGES_SQL}) u
        ON p.CAFC_PLAYER_ID = u.CAFC_PLAYER_ID
        WHEN MATCHED THEN UPDATE SET
            BIRTH_DATE = u.new_birth_date,
            UPDATED_AT = CURRENT_TIMESTAMP()
        """,
        params,
    )
    n = int(cur.fetchone()[0])  # MERGE returns (rows updated,)
    log.info("Refreshed %d CORE.PLAYERS birth dates from %s.", n, source_system)
    return n


def run(source_system: str = "IMPECT", dry_run: bool = True) -> int:
    conn = _snowflake.get_connection()
    try:
        with conn.cursor() as cur:
            n = refresh_birth_dates(cur, source_system=source_system, dry_run=dry_run)
        if not dry_run:
            conn.commit()
        return n
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--source", default="IMPECT")
    p.add_argument("--apply", action="store_true",
                   help="Write changes (default is a dry-run count).")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(source_system=args.source, dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
