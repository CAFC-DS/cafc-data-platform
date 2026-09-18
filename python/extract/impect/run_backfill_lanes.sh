#!/usr/bin/env bash
# Launches N parallel lanes of the full Impect events historical backfill
# (load_match_events.py --all-iterations) as background processes on this
# machine, meant to run for days unattended.
#
# Each lane is wrapped in a restart loop: if a lane's Python process dies
# (crash, network drop, laptop briefly losing power), it's relaunched after
# a short pause rather than silently stopping the whole backfill. This is
# safe because every layer underneath is idempotent -- restarting a lane
# just re-skips whatever it already loaded.
#
# `caffeinate -i` (macOS) keeps the machine from idle-sleeping while this
# runs; it does NOT override an explicit lid-close or a manual sleep, so
# keep the lid open (or run with the lid closed only if the laptop is set
# to stay awake on external power -- check System Settings > Battery).
#
# Usage (nohup so it survives closing the terminal -- this is the part that
# actually makes it "leave it running unattended", not just backgrounding):
#   nohup ./run_backfill_lanes.sh 5 > backfill_logs/launcher.log 2>&1 &
#
# Logs land in ./backfill_logs/lane_<n>.log. Check progress with:
#   tail -f backfill_logs/lane_*.log
# Stop everything, including the caffeinate keep-awake, with:
#   pkill -f "load_match_events.py --all-iterations"; pkill -f "caffeinate -i"

set -euo pipefail
cd "$(dirname "$0")"

LANES="${1:-5}"
LOG_DIR="backfill_logs"
mkdir -p "$LOG_DIR"

echo "Starting Impect full backfill: $LANES lane(s), logs in $LOG_DIR/"

for ((lane=0; lane<LANES; lane++)); do
  (
    while true; do
      echo "[$(date)] lane $lane: starting/resuming" >> "$LOG_DIR/lane_${lane}.log"
      python3 load_match_events.py --all-iterations --lane "$lane" --lanes "$LANES" \
        >> "$LOG_DIR/lane_${lane}.log" 2>&1 || true
      echo "[$(date)] lane $lane: process exited, restarting in 30s" >> "$LOG_DIR/lane_${lane}.log"
      sleep 30
    done
  ) &
  echo "  lane $lane launched (pid $!)"
done

if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i &
  echo "  caffeinate launched (pid $!) to prevent idle sleep"
else
  echo "  caffeinate not found (non-macOS?) -- check this machine's own sleep settings instead."
fi

echo "All lanes launched. If this script wasn't started under nohup, the lanes"
echo "and caffeinate will still die when this terminal closes -- re-run as:"
echo "  nohup $0 $LANES > $LOG_DIR/launcher.log 2>&1 &"
