#!/usr/bin/env bash
# Launches N parallel workers of the queue-driven IMPECT event backfill,
# scoped to the 25/26 season plus calendar-year "2025"/"2026" seasons
# (MLS, Brazil, Nordics/Baltics, K League, CONMEBOL competitions, etc.)
# across every accessible men's competition.
#
# This is run_queue_backfill_lanes.sh with two corrections: it resolves the
# repo-root virtualenv (there is no venv inside this directory, so the
# generic runner silently falls back to a bare `python3` with no Snowflake
# connector), and it passes --iteration-ids so workers drain only this
# scoped slice of CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE rather than all
# history.
#
# The iteration list is every ITERATION_ID with SEASON IN ('25/26','2025',
# '2026') that still had PENDING/FAILED queue rows at launch; Premier League
# (1381) and Championship (1410) are absent because they are already fully
# loaded.
#
# Each worker restarts after exiting: --until-empty makes a worker stop when
# it sees no claimable work, which also happens transiently while siblings
# hold every remaining claim or rows sit in FAILED cooldown.
#
# Usage:
#   nohup ./run_2526_backfill_lanes.sh 4 > backfill_logs/s2526_launcher.log 2>&1 &
#
# Progress:
#   tail -f backfill_logs/s2526_worker_*.log
# Stop everything:
#   pkill -f "backfill_historical_match_events.py --process"; pkill -f "caffeinate -i"

set -euo pipefail
cd "$(dirname "$0")"

WORKERS="${1:-4}"
BATCH_SIZE="${2:-25}"
LOG_DIR="backfill_logs"
mkdir -p "$LOG_DIR"

PY="../../../.venv/bin/python"
[ -x "$PY" ] || { echo "repo-root venv interpreter not found at $PY" >&2; exit 1; }

ITERATION_IDS=$(tr -d '\n' < season_2526_plus_calendar_iteration_ids.txt)

echo "Starting 25/26 + calendar-year backfill: $WORKERS worker(s), batch size $BATCH_SIZE, logs in $LOG_DIR/"

for ((w=1; w<=WORKERS; w++)); do
  (
    while true; do
      echo "[$(date)] worker $w: starting/resuming" >> "$LOG_DIR/s2526_worker_${w}.log"
      "$PY" -u backfill_historical_match_events.py --process --until-empty \
        --batch-size "$BATCH_SIZE" --iteration-ids "$ITERATION_IDS" \
        >> "$LOG_DIR/s2526_worker_${w}.log" 2>&1 || true
      echo "[$(date)] worker $w: exited, restarting in 60s" >> "$LOG_DIR/s2526_worker_${w}.log"
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
echo "  nohup $0 $WORKERS $BATCH_SIZE > $LOG_DIR/s2526_launcher.log 2>&1 &"
