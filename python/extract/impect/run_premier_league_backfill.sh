#!/bin/zsh
# One-time local worker for the scoped men's Premier League IMPECT backfill.

set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h:h:h}

cd "$SCRIPT_DIR"
exec /usr/bin/caffeinate -i "$REPO_ROOT/.venv/bin/python" -u \
  "$SCRIPT_DIR/backfill_historical_match_events.py" \
  --process --until-empty --batch-size 25 \
  --iteration-ids 422,522,744,1014,1381,2108 \
  >> /private/tmp/impect-premier-league-backfill.log 2>&1
