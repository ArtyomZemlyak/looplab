#!/bin/bash
# Start (or restart) the metering proxy. Idempotent: an already-listening meter is left alone
# unless --restart is given.
#
#   source benchmarks/box-jhub-l40s.sh && benchmarks/meter/start_meter.sh [--restart]
#
# Killing it by pattern is deliberately NOT done with a bare `pkill -f proxy.py`: that pattern
# matches the launching shell's own command line too (docs/51 trap 6 -- it killed the campaign
# launcher twice). The PIDs are read and killed one by one instead.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${METER_PORT:-8801}"
UPSTREAM="${METER_UPSTREAM:-${LOOPLAB_LLM_BASE_URL:-}}"
KEY="${METER_API_KEY:-${LOOPLAB_LLM_API_KEY:-}}"
LOG="${METER_LOG:-${BENCH_ROOT:-/var/tmp/looplab-bench}/meter/meter.jsonl}"
RPM="${METER_RPM:-45}"
STDOUT="${METER_STDOUT:-${BENCH_ROOT:-/var/tmp/looplab-bench}/logs/meter.log}"

[ -n "$UPSTREAM" ] || { echo "set METER_UPSTREAM (or LOOPLAB_LLM_BASE_URL) to the gateway"; exit 2; }
mkdir -p "$(dirname "$LOG")" "$(dirname "$STDOUT")"

alive() { curl -s --noproxy '*' --max-time 3 "http://127.0.0.1:$PORT/healthz" 2>/dev/null; }

if [ "${1:-}" = "--restart" ]; then
  for P in $(pgrep -af "meter/proxy.py --port $PORT" | grep -v 'bash' | awk '{print $1}'); do
    kill "$P" 2>/dev/null && echo "stopped $P"
  done
  sleep 1
fi

if S="$(alive)" && [ -n "$S" ]; then
  echo "meter already up on :$PORT  $S"
  exit 0
fi

# PINNED OFF THE LANES, or loudly not. See METER_CPUS in the box profile for why; in short, the
# meter is infrastructure every lane talks to, and a lane it shares is a lane whose timings include
# a proxy. An unset METER_CPUS is not silently accepted: an unpinned meter is a measurement risk the
# operator should have to read about, not discover in `taskset -p` three hours into a campaign.
if [ -n "${METER_CPUS:-}" ] && command -v taskset > /dev/null; then
  PIN=(taskset -c "$METER_CPUS")
  echo "meter pinned to $METER_CPUS (off the lanes)"
else
  PIN=()
  echo "WARNING: METER_CPUS unset or no taskset -- the meter is UNPINNED and may land on a lane," >&2
  echo "         which puts its CPU into somebody's timings. Source the box profile first." >&2
fi
setsid nohup "${PIN[@]}" python3 "$HERE/proxy.py" --port "$PORT" --upstream "$UPSTREAM" --api-key "$KEY" \
    --log "$LOG" --rpm "$RPM" >> "$STDOUT" 2>&1 < /dev/null &
sleep 2
if S="$(alive)" && [ -n "$S" ]; then
  echo "meter up on :$PORT -> $UPSTREAM  (rpm $RPM, log $LOG)"
else
  echo "meter did NOT come up; see $STDOUT"; tail -5 "$STDOUT"; exit 1
fi
