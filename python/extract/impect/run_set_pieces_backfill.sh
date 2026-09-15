#!/bin/zsh
# One-shot or cron-invoked backfill for CAFC_DB.IMPECT_RAW.SET_PIECES.
# Loads set-piece sub-phase data for every match present in EVENTS but
# missing from SET_PIECES. Safe to re-run (idempotent MERGE upsert).

set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h:h:h}

cd "$SCRIPT_DIR"
source .env

exec "$REPO_ROOT/.venv/bin/python" -u load_set_pieces.py \
  --backfill-events --pause-seconds 0.35 "$@"
