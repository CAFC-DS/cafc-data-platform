"""Sync 2026/27 EFL Championship IMPECT events and event KPIs.

This is the scheduled, competition-scoped entry point. It seeds any past,
available Championship matches missing from Snowflake, then consumes IMPECT's
update and deletion feeds using a dedicated cursor. Recalculated matches are
replaced safely by sync_recent_match_events rather than being skipped forever.

Usage:
    python sync_championship_events.py
    python sync_championship_events.py --dry-run
"""
from __future__ import annotations

import argparse
import sys

import sync_recent_match_events as recent

CHAMPIONSHIP_COMPETITION_ID = 41
CHAMPIONSHIP_FEED_NAME = "MATCH_EVENTS_V5_COMPETITION_41"


def run(seasons: set[str], dry_run: bool = False) -> dict[str, int]:
    return recent.run(
        bootstrap_days=14,
        overlap_minutes=15,
        seasons=seasons,
        match_lookup_pause_seconds=0.35,
        dry_run=dry_run,
        competition_ids={CHAMPIONSHIP_COMPETITION_ID},
        feed_name=CHAMPIONSHIP_FEED_NAME,
        seed_missing=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seasons", default="26/27,2026,2027",
                        help="Comma-separated Impect season labels to sync (default covers the current rollover).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover candidates without changing Snowflake event data or cursor state.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run(seasons={s.strip() for s in args.seasons.split(",") if s.strip()}, dry_run=args.dry_run)
    if result["failed"]:
        sys.exit(1)
