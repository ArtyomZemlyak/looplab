#!/bin/bash
# Run snapshot.sh on a timer, because this box has no crontab and a campaign is hours long.
#
#   source benchmarks/box-jhub-l40s.sh && benchmarks/snapshot_timer.sh start [interval_seconds]
#   benchmarks/snapshot_timer.sh status | stop
#
# The campaign already snapshots when an arm finishes -- that is the snapshot that matters, because
# it happens exactly when the data changed. This one is insurance against the other case: the
# container restarting in the MIDDLE of a multi-hour arm, where the arm's own hook never runs.
#
# It skips a cycle when nothing has changed, so a quiet box does not fill the mount with identical
# copies (snapshot.sh also keeps only the last SNAPSHOT_KEEP).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BENCH_ROOT:-/var/tmp/looplab-bench}"
PIDFILE="$ROOT/snapshot_timer.pid"
LOGFILE="$ROOT/logs/snapshot_timer.log"
INTERVAL="${2:-1800}"
[ "${1:-}" = "_loop" ] && INTERVAL="${2:-1800}"

fingerprint() {
  # Cheap "has anything been measured since last time": newest mtime + size of the outputs.
  find "$ROOT/campaign" "$ROOT/AlgoTune/reports" "$ROOT/meter" \
       "$ROOT/looplab/benchmarks/algotune/.baseline_times" \
       -type f -newermt '-1 day' -printf '%T@ %s\n' 2>/dev/null | sort | tail -20 | md5sum
}

case "${1:-status}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    mkdir -p "$(dirname "$LOGFILE")"
    # Re-exec THIS script in loop mode rather than embedding the loop in a quoted string: a quoted
    # heredoc-in-a-string is where shell scripts go to acquire quoting bugs nobody can see.
    setsid nohup "$0" _loop "$INTERVAL" >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 1
    echo "snapshot timer started (every ${INTERVAL}s, pid $(cat "$PIDFILE")), log $LOGFILE"
    ;;

  _loop)
    last=""
    while true; do
      cur="$(fingerprint)"
      if [ "$cur" != "$last" ]; then
        echo "[$(date +%H:%M:%S)] change detected, snapshotting"
        "$HERE/snapshot.sh" 2>&1 | sed 's/^/    /'
        last="$cur"
      else
        echo "[$(date +%H:%M:%S)] nothing new since the last snapshot; skipping"
      fi
      sleep "$INTERVAL"
    done
    ;;

  stop)
    # Killed by PID, never by `pkill -f <pattern>`: that pattern matches this script's own command
    # line too, which is how a launcher gets killed by its own stop command (docs/51 trap 6).
    if [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null && echo "stopped $(cat "$PIDFILE")"
      rm -f "$PIDFILE"
    else
      echo "no pidfile at $PIDFILE"
    fi
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PIDFILE"))"; tail -3 "$LOGFILE" 2>/dev/null
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 start|stop|status [interval_seconds]"; exit 2 ;;
esac
