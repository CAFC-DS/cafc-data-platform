"""
Regular health check for the all-competitions fixture sync
(.github/workflows/recruitment-sync.yml).

Two checks, run after reference-refresh has loaded IMPECT_RAW.MATCHES and
fixture_matcher.py has minted anything new:

1. Backlog: a dry-run of fixture_matcher.py should always report 0 linkable
   fixtures immediately after --apply. Anything else means minting silently
   failed (or didn't run) and the app's fixture search will be missing
   matches again -- this is what actually broke for MLS/Premier League 2 on
   2026-09-22 (see PR discussion / that day's incident). Exits non-zero so
   the workflow run fails and shows up in the Actions tab / failure email.

2. Thin current-season competitions: any iteration for the current-year
   season string ('26/27' or '2026') with under MIN_CURRENT_SEASON_MATCHES
   total matches loaded. A near-empty season for a competition that should
   be underway (PL2 26/27 had 15 rows before the 2026-09-22 fix) is the
   other failure shape seen live. Warning-only, not a failure: legitimate
   one-off competitions (Community Shield) and very-early qualifying rounds
   also show low counts, so this needs a human glance rather than a hard
   gate -- printed clearly so it's visible in the workflow log.

Usage: python -m python.identity.verify_fixture_sync
"""
from __future__ import annotations

import logging
import sys

from python import _snowflake
from python.identity.fixture_matcher import run as fixture_matcher_run

log = logging.getLogger("cafc.identity.verify_fixture_sync")

MIN_CURRENT_SEASON_MATCHES = 10
CURRENT_SEASON_STRINGS = ("26/27", "2026")


def check_backlog() -> int:
    result = fixture_matcher_run(dry_run=True)
    backlog = result["would_mint"]
    if backlog:
        log.error(
            "::error::%d IMPECT fixtures are unlinked after the sync ran -- "
            "fixture_matcher --apply did not clear the backlog. The app's "
            "fixture search will be missing these matches.",
            backlog,
        )
    else:
        log.info("Backlog check OK: 0 unlinked fixtures.")
    return backlog


def check_thin_seasons() -> list[tuple]:
    conn = _snowflake.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.ID, i."COMPETITION.NAME", i.SEASON, COUNT(m.ID) AS total_matches
                FROM CAFC_DB.IMPECT_RAW.ITERATIONS i
                LEFT JOIN CAFC_DB.IMPECT_RAW.MATCHES m ON m.ITERATIONID = i.ID
                WHERE i.SEASON IN (%s, %s)
                GROUP BY 1, 2, 3
                HAVING COUNT(m.ID) < %s
                ORDER BY 4
                """,
                (*CURRENT_SEASON_STRINGS, MIN_CURRENT_SEASON_MATCHES),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if rows:
        log.warning(
            "%d current-season competitions have under %d matches loaded "
            "-- worth a human glance (could be legitimate, e.g. a one-off "
            "cup or an early qualifying round):",
            len(rows), MIN_CURRENT_SEASON_MATCHES,
        )
        for iteration_id, competition, season, total in rows:
            log.warning("  iteration %s: %s (%s) -- %d matches",
                        iteration_id, competition, season, total)
    else:
        log.info("Thin-season check OK: no current-season competition under "
                  "%d matches.", MIN_CURRENT_SEASON_MATCHES)
    return rows


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s  %(message)s")
    backlog = check_backlog()
    check_thin_seasons()
    return 1 if backlog else 0


if __name__ == "__main__":
    sys.exit(main())
