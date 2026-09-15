#!/bin/zsh
# One-shot or cron-invoked backfill for CAFC_DB.IMPECT_RAW.SET_PIECES.
# Loads set-piece sub-phase data for every match present in EVENTS but
# missing from SET_PIECES. Safe to re-run (idempotent MERGE upsert).

set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h:h:h}

cd "$SCRIPT_DIR"
source .env

# SET_PIECES_ITERATION_IDS scopes the backfill (e.g. "2114" for Championship
# 26/27); unset runs against every iteration present in EVENTS.
iteration_flag=()
if [[ -n "${SET_PIECES_ITERATION_IDS:-}" ]]; then
  iteration_flag=(--iteration-ids "$SET_PIECES_ITERATION_IDS")
fi

exec "$REPO_ROOT/.venv/bin/python" -u load_set_pieces.py \
  --backfill-events --pause-seconds 0.35 "${iteration_flag[@]}" "$@"
