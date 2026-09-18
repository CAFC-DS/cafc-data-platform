#!/usr/bin/env bash
# Launches N parallel workers of the queue-driven historical Impect event
# backfill (backfill_historical_match_events.py --process --until-empty) as
# background processes on this machine, meant to run for weeks unattended.
#
# This is the companion to run_backfill_lanes.sh. That script drives the
# older iteration-walking path (load_match_events.py --all-iterations); this
# one drains CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE, which is what
# `backfill_historical_match_events.py --status` reports on. Every claim is
# tokenised and stale RUNNING rows are reclaimed after 12h, so running
# several workers at once is safe -- they claim disjoint batches and the
# load path is idempotent, so an occasional double-processed match just
# re-writes the same rows.
#
# Each worker is wrapped in a restart loop: --until-empty makes a worker
# exit when it sees no claimable work (which also happens transiently when
# every remaining row is claimed by a sibling or in FAILED cooldown), so we
# relaunch it after a pause rather than letting the lane stop for good.
#
# `caffeinate -i` (macOS) keeps a laptop awake; on Linux it is simply
# absent and skipped -- a server does not idle-sleep.
#
# Usage (nohup so it survives the terminal closing):
#   nohup ./run_queue_backfill_lanes.sh 5 > backfill_logs/queue_launcher.log 2>&1 &
#
# Logs land in ./backfill_logs/queue_worker_<n>.log. Progress:
#   ./.venv/bin/python3 backfill_historical_match_events.py --status
# Stop everything:
#   pkill -f "backfill_historical_match_events.py --process"; pkill -f "caffeinate -i"

set -euo pipefail
cd "$(dirname "$0")"

WORKERS="${1:-5}"
BATCH_SIZE="${2:-25}"
LOG_DIR="backfill_logs"
mkdir -p "$LOG_DIR"

# Prefer the local venv interpreter; fall back to python3 on PATH.
PY="./.venv/bin/python3"
[ -x "$PY" ] || PY="python3"

echo "Starting queue backfill: $WORKERS worker(s), batch size $BATCH_SIZE, logs in $LOG_DIR/"

for ((w=1; w<=WORKERS; w++)); do
  (
    while true; do
      echo "[$(date)] worker $w: starting/resuming" >> "$LOG_DIR/queue_worker_${w}.log"
      "$PY" -u backfill_historical_match_events.py --process --until-empty \
        --batch-size "$BATCH_SIZE" \
        >> "$LOG_DIR/queue_worker_${w}.log" 2>&1 || true
      echo "[$(date)] worker $w: exited, restarting in 60s" >> "$LOG_DIR/queue_worker_${w}.log"
      sleep 60
    done
  ) &
  echo "  worker $w launched (pid $!)"
done

if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i &
  echo "  caffeinate launched (pid $!) to prevent idle sleep"
fi

echo "All workers launched. If not started under nohup, re-run as:"
echo "  nohup $0 $WORKERS $BATCH_SIZE > $LOG_DIR/queue_launcher.log 2>&1 &"
