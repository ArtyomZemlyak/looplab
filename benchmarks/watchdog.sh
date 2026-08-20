#!/bin/bash
# Keep an unattended campaign alive through the night, and leave a record of what it survived.
#
#   source benchmarks/box-jhub-l40s.sh && benchmarks/watchdog.sh start [interval_seconds]
#   benchmarks/watchdog.sh status | stop
#
# It fixes exactly one thing and reports the rest. The thing it fixes is the METER: both arms send
# every LLM call through it, so if it dies the campaign does not slow down -- it stops, with both
# arms failing on connection refused, and a task-arm that fails writes no score at all. Everything
# else (disk, orphaned workers, a lane that stopped producing) is recorded for the morning rather
# than acted on, because an unattended repair of a measurement is worse than a gap in one.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BENCH_ROOT:-/var/tmp/looplab-bench}"
PIDFILE="$ROOT/watchdog.pid"
LOG="$ROOT/logs/watchdog.log"
PORT="${METER_PORT:-8801}"
INTERVAL="${2:-300}"

check_once() {
  local ts; ts="$(date +%H:%M:%S)"
  local notes=()

  # 1. The meter. Restart it if it is not answering; both arms depend on it.
  if ! curl -s --noproxy '*' --max-time 5 "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1; then
    notes+=("METER DOWN -> restarting")
    "$HERE/meter/start_meter.sh" --restart >> "$LOG" 2>&1
  fi

  # 2. Disk. The runtime disk holds the datasets, and a full disk fails a run in ways that look
  #    like a bad solver.
  local free_gb; free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
  [ "${free_gb:-999}" -lt 15 ] && notes+=("DISK LOW: ${free_gb}G free on /")

  # 3. Orphaned forkservers. Not killed here: a live lane's workers look the same from outside and
  #    the reaper in campaign.sh already runs between tasks. Counted, because a timing taken while
  #    orphans burn the pinned cores is inflated and nothing else would say so.
  local orph=0
  for P in $(pgrep -f "multiprocessing.fork[s]erver" 2>/dev/null); do
    [ "$(ps -o ppid= -p "$P" 2>/dev/null | tr -d ' ')" = "1" ] && orph=$((orph + 1))
  done
  [ "$orph" -gt 4 ] && notes+=("$orph orphaned forkservers")

  # 4. Progress. A campaign that is alive but producing nothing is the failure mode worth waking up
  #    to, and the only cheap sign of it is that no task log has grown.
  local recent; recent=$(find "$ROOT/campaign" -name '*.log' -newermt '-10 minutes' 2>/dev/null | wc -l)
  local running=0; pgrep -f "campaign[.]sh" > /dev/null && running=1
  [ "$running" = 1 ] && [ "$recent" = 0 ] && notes+=("campaign alive but NO log grew in 10 min")

  local done_a done_b
  done_a=$(ls "$ROOT/campaign"/A-*.done 2>/dev/null | wc -l)
  done_b=$(ls "$ROOT/campaign"/B-*.done 2>/dev/null | wc -l)
  local spend
  spend=$(python3 -c "
import json
try:
    rows=[json.loads(l) for l in open('$ROOT/meter/meter.jsonl')]
    print('%.4f' % sum(r.get('cost') or 0 for r in rows))
except Exception: print('?')" 2>/dev/null)

  # THROTTLING, not load average. `/proc/loadavg` is HOST-wide on this shared node and says nothing
  # about whether WE were held back; `cpu.stat`'s throttle counter is the cgroup's own answer, and a
  # timing taken while the quota was being enforced is not comparable to one taken while it was not.
  local thr; thr=$(awk '/nr_throttled/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
  if [ ${#notes[@]} -eq 0 ]; then
    echo "[$ts] ok | A:$done_a/20 B:$done_b/20 | spend \$$spend | load $(cut -d' ' -f1 /proc/loadavg) | throttled $thr"
  else
    echo "[$ts] A:$done_a/20 B:$done_b/20 | spend \$$spend | ${notes[*]}"
  fi
}

case "${1:-status}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    mkdir -p "$(dirname "$LOG")"
    setsid nohup "$0" _loop "$INTERVAL" >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 1
    echo "watchdog started (every ${INTERVAL}s, pid $(cat "$PIDFILE")), log $LOG"
    ;;
  _loop)
    while true; do check_once; sleep "$INTERVAL"; done
    ;;
  once)   check_once ;;
  stop)
    # By PID, never `pkill -f`: that pattern matches this script's own command line (docs/48 trap 6).
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo stopped
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PIDFILE"))"; tail -5 "$LOG" 2>/dev/null
    else echo "not running"; fi
    ;;
  *) echo "usage: $0 start|stop|status|once [interval_seconds]"; exit 2 ;;
esac
