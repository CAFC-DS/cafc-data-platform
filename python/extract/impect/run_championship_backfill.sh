#!/bin/zsh
# Persistent local worker for the scoped English Championship IMPECT backfill.
# Stop it with: launchctl remove com.cafc.impect.championship-backfill

set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h:h:h}

cd "$SCRIPT_DIR"

exec /usr/bin/caffeinate -i "$REPO_ROOT/.venv/bin/python" -u \
  "$SCRIPT_DIR/backfill_historical_match_events.py" \
  --process --until-empty --batch-size 25 \
  --iteration-ids 414,512,728,1022,1410,2114 \
  >> /private/tmp/impect-championship-backfill.log 2>&1
