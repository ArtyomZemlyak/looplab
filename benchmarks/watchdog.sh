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
  # BOTH signals, over DISCOVERED directories, and a window wider than the gateway's own cut.
  # Three things were wrong with the line this replaces, and the first one is a false attribution
  # that would have spread:
  #
  #   * it blamed `bfs` for rejecting the relative `-newermt '-10 minutes'` GNU spelling. That is
  #     not true on this box and never was. Measured 2026-08-23: `type find` is /usr/bin/find, GNU
  #     findutils 4.9.0, `command -v bfs` is empty, and the relative and absolute spellings return
  #     the IDENTICAL 9 files over `campaign-paired/*.log`. What actually counted zero for its whole
  #     life was the DIRECTORY: it watched `$ROOT/campaign`, a campaign that finished on 2026-08-20
  #     and whose logs cannot grow again, while the live one wrote into `campaign-paired/`. Same
  #     root cause as the snapshot defect, one surface over -- a hardcoded name for an operator's
  #     `CAMPAIGN_OUT` -- which is why the globs below discover instead of naming.
  #   * arm B's per-task lane logs stay EMPTY (LoopLab writes into each run's own directory), so
  #     watching `campaign*/*.log` alone reports a working arm as stalled. The run's event stream is
  #     the honest second signal.
  #   * TEN MINUTES IS SHORTER THAN ONE CALL. This gateway CUTS a generation at ~1800 s and 23 such
  #     streams are in `meter/meter.jsonl` at 1817-1830 s each; a run waiting on one of them writes
  #     nothing to `events.jsonl` for half an hour and is perfectly healthy. The window is therefore
  #     derived from that ceiling rather than guessed, with margin: a monitor whose alarm is shorter
  #     than the slowest legitimate operation is a monitor that cries wolf, which is the cost this
  #     comment block was already about.
  local stall_min="${WATCHDOG_STALL_MIN:-45}"
  local since; since="$(date -d "-${stall_min} minutes" +%Y-%m-%dT%H:%M:%S 2>/dev/null)"
  local recent; recent=$(find "$ROOT"/campaign* \
      -name '*.log' -newermt "$since" 2>/dev/null | wc -l)
  local recent_ev; recent_ev=$(find "$ROOT"/runs-* \
      -name 'events.jsonl' -newermt "$since" 2>/dev/null | wc -l)
  recent=$((recent + recent_ev))
  local running=0; pgrep -f "campaign[.]sh" > /dev/null && running=1
  [ "$running" = 1 ] && [ "$recent" = 0 ] && \
    notes+=("campaign alive but NO log grew in $stall_min min")

  # Per campaign directory, DISCOVERED. (The original `campaign/` holds a FINISHED run whose markers
  # never move again; counting them beside a live one printed "A:20/20 B:20/20" forever while the
  # live campaign sat at zero, which is a progress line that cannot go up. Naming the live ones by
  # hand was the wrong fix for that -- see the `find` note above.)
  local notes_progress=""
  local owed=0
  for D in "$ROOT"/campaign*; do
    [ -d "$D" ] || continue
    for A in A B; do
      local N L
      N=$(ls "$D"/$A-*.done 2>/dev/null | wc -l)
      L=$(ls "$D"/$A-*.log 2>/dev/null | wc -l)
      [ "$L" -gt 0 ] && notes_progress="$notes_progress$(basename "$D"):$A $N/$L  "
      [ "$L" -gt "$N" ] && owed=$((owed + L - N))
    done
  done

  # THE CAMPAIGN BEING GONE IS THE THING THIS FILE EXISTS FOR, AND NOTHING WAS CHECKING IT.
  # Every note above fires only while `campaign.sh` is RUNNING, so the moment it dies -- OOM-killed,
  # container bounce, an operator's stray kill -- `running` goes 0, every alarm goes quiet, the
  # meter keeps answering /healthz because the watchdog itself restarts it, and this script prints
  # `ok | campaign-paired:B 17/20 | ...` on a five-minute loop for the rest of the night. The
  # progress counter is monotone, so a frozen one is indistinguishable from a finished one; and the
  # header of this file says the failure worth waking up to is "a campaign that is alive but
  # producing nothing", which quietly assumed alive.
  #
  # `owed` is task-arms with a lane LOG and no `.done` marker -- the same predicate `campaign.sh`'s
  # `final_banner` uses for "still owed". Work outstanding and no driver to do it is DEAD, not ok.
  if [ "$running" = 0 ] && [ "$owed" -gt 0 ]; then
    notes+=("NO campaign.sh RUNNING and $owed task-arm(s) still owed -- the campaign is DEAD, not done")
  fi
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
  # THE QUOTA IS 90 CORES, NOT 96 (`cpu.max` = 9000000/100000 on this box), and the campaign pins 88.
  # A second lane beside it therefore asks for more than the container may have. What matters is not
  # the throttle COUNT (15 events in the machine's whole life) but the time lost to it: measured
  # 2026-08-23, 2.7 ms per event, i.e. seven events across three and a half hours of five-lane load.
  # So the alarm is on microseconds, and 1 s inside one interval is the line: below it the effect is
  # unmeasurable beside a 30 s evaluation, above it a timing is no longer the solver's alone.
  local thr_us; thr_us=$(awk '/throttled_usec/{print $2}' /sys/fs/cgroup/cpu.stat 2>/dev/null)
  local prev_us; prev_us=$(cat "$ROOT/.throttled_usec" 2>/dev/null || echo "${thr_us:-0}")
  echo "${thr_us:-0}" > "$ROOT/.throttled_usec"
  if [ -n "$thr_us" ] && [ $((thr_us - prev_us)) -gt 1000000 ]; then
    notes+=("THROTTLED $(( (thr_us - prev_us) / 1000 )) ms since the last check — a timing taken now is not the solver's alone")
  fi
  if [ ${#notes[@]} -eq 0 ]; then
    echo "[$ts] ok | ${notes_progress:-no live campaign} | spend \$$spend | load $(cut -d' ' -f1 /proc/loadavg) | throttled $thr"
  else
    echo "[$ts] ${notes_progress:-no live campaign} | spend \$$spend | ${notes[*]}"
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
    # By PID, never `pkill -f`: that pattern matches this script's own command line (docs/51 trap 6).
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo stopped
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PIDFILE"))"; tail -5 "$LOG" 2>/dev/null
    else echo "not running"; fi
    ;;
  *) echo "usage: $0 start|stop|status|once [interval_seconds]"; exit 2 ;;
esac
