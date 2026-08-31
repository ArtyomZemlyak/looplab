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

# OPEN[meter-key-rides-argv] the gateway API key is passed as an argv flag, world-readable in
# /proc/<pid>/cmdline for the meter's whole multi-day lifetime on the shared JupyterHub box.
# proof:`present:--api-key "$KEY"@benchmarks/meter/start_meter.sh`
# REVIEW 2026-08-30 (security): the deployment this script exists for is the box CLAUDE.md's
# `_on_shared_hub` describes — one origin, many users — and every one of them can read another
# process's command line. `proxy.py::main` already reads METER_API_KEY from the environment, so the
# fix is to export the variable and drop the flag: same idempotence, zero proxy changes. (The proxy
# itself is clean — rows and stderr never carry headers or the key.)
setsid nohup python3 "$HERE/proxy.py" --port "$PORT" --upstream "$UPSTREAM" --api-key "$KEY" \
    --log "$LOG" --rpm "$RPM" >> "$STDOUT" 2>&1 < /dev/null &
sleep 2
if S="$(alive)" && [ -n "$S" ]; then
  echo "meter up on :$PORT -> $UPSTREAM  (rpm $RPM, log $LOG)"
else
  echo "meter did NOT come up; see $STDOUT"; tail -5 "$STDOUT"; exit 1
fi
