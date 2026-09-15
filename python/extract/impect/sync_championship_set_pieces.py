"""Sync 2026/27 EFL Championship IMPECT set-piece sub-phase data.

Scheduled, competition-scoped entry point for CAFC_DB.IMPECT_RAW.SET_PIECES,
mirroring sync_championship_events.py's scoping (competition id 41, season
labels 26/27/2026/2027) so it doesn't sweep the full ~35k-match historical
range on every run. Resolves the current season's iteration id(s) live via
eligible_iterations() rather than hardcoding one, since a new iteration id
is minted each season rollover.

Depends on EVENTS already containing the target matches -- run after
sync_championship_events.py in the same job.

Usage:
    python sync_championship_set_pieces.py
    python sync_championship_set_pieces.py --dry-run
"""
from __future__ import annotations

import argparse
import sys

import load_set_pieces
from sync_recent_match_events import eligible_iterations

CHAMPIONSHIP_COMPETITION_ID = 41


def run(seasons: set[str], dry_run: bool = False, pause_seconds: float = 0.35) -> dict[str, int]:
    iterations = eligible_iterations(seasons, {CHAMPIONSHIP_COMPETITION_ID})
    if not iterations:
        raise RuntimeError(f"No eligible IMPECT Championship iterations found in seasons {sorted(seasons)}")
    return load_set_pieces.backfill(
        limit=None, pause_seconds=pause_seconds, dry_run=dry_run,
        iteration_ids=set(iterations.keys()),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seasons", default="26/27,2026,2027",
                        help="Comma-separated Impect season labels to sync (default covers the current rollover).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report candidates without loading any set-piece data.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run(seasons={s.strip() for s in args.seasons.split(",") if s.strip()}, dry_run=args.dry_run)
    print(result)
    if result["failed"]:
        sys.exit(1)
