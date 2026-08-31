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
# The SAME answer `snapshot.sh` archives from. Two copies of "which trees hold measurements" is
# exactly how `camp-runs/` came to be archived by neither and watched by neither -- bench_trees.sh.
. "$HERE/bench_trees.sh"
ROOT="${BENCH_ROOT:-/var/tmp/looplab-bench}"
PIDFILE="$ROOT/snapshot_timer.pid"
LOGFILE="$ROOT/logs/snapshot_timer.log"
INTERVAL="${2:-1800}"
[ "${1:-}" = "_loop" ] && INTERVAL="${2:-1800}"

fingerprint() {
  # Cheap "has anything been measured since last time": newest mtime + size of the outputs.
  #
  # EVERY campaign directory, for the same reason `snapshot.sh` discovers them rather than naming
  # one: `$ROOT/campaign` held a run that finished on 2026-08-20 and nothing in it has changed
  # since, so this fingerprint was watching a directory that cannot move while the LIVE campaign
  # wrote into `campaign-paired/`. It kept firing only because `meter/` and `.baseline_times/` also
  # change -- i.e. the campaign's own progress was never one of the signals, and a quiet meter would
  # have stopped snapshotting the one thing worth snapshotting.
  # AND EVERY RUN TREE, added 2026-08-31 with the same argument one paragraph up, for a source that
  # did not exist when that paragraph was written. `snapshot.sh` archives each run's events.jsonl
  # and spans.jsonl -- the evidence docs/56 is written from and the thing the 2026-08-29 restart
  # actually destroyed -- but archiving it is no use if this function cannot see it grow. Measured
  # while two probes were live: the fingerprint moved only because `meter/` was moving, i.e. the
  # probes were covered by accident. A run evaluating locally for twenty minutes makes no LLM calls,
  # and a probe metered on another port makes none here at all; in both cases the timer would report
  # "nothing new" while the one irreplaceable directory on the box filled up.
  # AND THE LIST IS NOT WRITTEN HERE. It was, as `campaign* runs-* model-probes probes`, and
  # `$CAMPAIGN_RUNS` -- `camp-runs/`, where a campaign puts every task-arm's run -- matched none of
  # those four patterns: `grep -c camp-runs` over this file returned 0. A campaign could fill that
  # tree for hours and this function would report "nothing new" throughout. `bench_trees.sh` answers
  # it once, for the archiver and for this, so the two cannot drift again.
  local -a P=()
  while IFS= read -r d; do P+=("$d"); done < <(bench_campaign_trees "$ROOT"; bench_run_trees "$ROOT")
  P+=("$ROOT/AlgoTune/reports" "$ROOT/meter" \
      "$ROOT/looplab/benchmarks/algotune/.baseline_times")
  find "${P[@]}" -type f -newermt '-1 day' -printf '%T@ %s\n' 2>/dev/null \
    | sort | tail -20 | md5sum
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
        # `${PIPESTATUS[0]}`, because the `sed` that indents the output is what `$?` would report.
        # And `last` advances ONLY on success: `snapshot.sh` exits 1 for a snapshot that is short of
        # a source, and remembering the fingerprint of a run it could not archive means the timer
        # sits quiet until something ELSE changes -- so the one measurement that failed to be
        # archived is the one nobody retries.
        "$HERE/snapshot.sh" 2>&1 | sed 's/^/    /'
        snap_rc=${PIPESTATUS[0]}
        if [ "$snap_rc" = "0" ]; then
          last="$cur"
        else
          echo "    (snapshot exited $snap_rc -- NOT recording this fingerprint, so the next tick"
          echo "     tries again rather than treating an incomplete archive as done)"
        fi
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
