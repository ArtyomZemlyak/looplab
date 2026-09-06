#!/bin/bash
# The campaign driver: BOTH arms over ONE task list, ONE attempt per task-arm, ONE configuration,
# every line of its log carrying the ISO date.
#
#   source benchmarks/box-jhub-l40s.sh          # the box's own paths and gateway
#   benchmarks/meter/start_meter.sh              # once per boot
#   ALLOW_VOLATILE_ROOT=1 benchmarks/algotune/run_final.sh
#
# WHY THIS FILE EXISTS IN THE REPO. The driver that ran the 2026-08-24 campaign was never
# committed: it lived under `/var/tmp/looplab-bench`, and `/var/tmp` does not survive a container
# restart, so it died with the box (docs/58 s58.7 item 9). A full campaign could not be repeated
# until it existed. Three of docs/58 s58.1's corrections are about what that driver did, and each
# is a rule here rather than a paragraph there:
#
#   1. ITS LOG PRINTED BARE CLOCK TIMES. `[10:12:06] ===== FINAL CAMPAIGN ...` opens the log and
#      `[06:10:13] ===== FINAL CAMPAIGN COMPLETE =====` closes it, with no date on either, and the
#      snapshot carrying it was dated 08-29 -- so "the campaign finished at 06:10 on the 29th" was
#      read off a timestamp that never said that, and every "campaign of 2026-08-29" in a week of
#      reports was wrong by FIVE DAYS. Every line this driver writes carries `date -u
#      +%Y-%m-%dT%H:%M:%SZ`, and the campaign's own output is stamped the same way as it passes
#      through (`stamped`, below).
#   2. ARM A DID NOT RUN ONCE PER TASK; IT RAN TWO TO FIVE TIMES. The scoring markers carry
#      `attempt=a3` and `attempt=a5`, launched by a SEPARATE driver (`logs/rerun-arm-a.log`) after
#      the campaign had reported itself complete, with configuration that changed between attempts
#      (`ALGOTUNE_LLM_TIMEOUT_S=1900`). The design was therefore not paired; it was two
#      unsynchronised runs reconciled afterwards. This driver has NO attempt loop -- no `for`, no
#      `while`, no `until` anywhere in it, which `tests/test_run_final_driver.py` pins -- and it
#      refuses to start with `RETRY_WALL_CUT` or `RETRY_IMMEDIATE_EXIT` set, because a reopened
#      marker is a second attempt and a second attempt is a decision an operator makes by hand,
#      in the log, not something a driver takes on a resume.
#   3. ONE CONFIGURATION. The variables that decide what a task-arm measures are recorded into
#      `$CAMPAIGN_OUT/run_final.CONFIGURATION` before the first task starts; a resume over the same
#      output directory REFUSES unless the recorded configuration is byte-identical to the one it
#      would run under. A campaign resumed under different settings is the a3/a5 defect with a
#      nicer name.
#
# WHAT IT DOES NOT DO. It does not retry, reopen, re-score or summarise. `campaign.sh` owns the
# task-arm lifecycle (markers, the meter rungs, the stall guard, the snapshot); this file owns the
# ORDER (arm A to completion, then arm B in the same regime -- docs/52 "Unattended operation") and
# the record of what one campaign was. It exits non-zero if either arm did, and prints the
# `compare_arms.py` invocation rather than running it, because a summary printed by the thing that
# just failed is a table of nothing wearing a banner.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# The SAME defaults `campaign.sh` uses, spelled once here so the driver's refusals are about the
# directories the campaign will actually use.
AT="${ALGOTUNE_ROOT:-/root/benchmarks/AlgoTune}"
OUT="${CAMPAIGN_OUT:-/root/benchmarks/campaign}"
RUNS_ROOT="${CAMPAIGN_RUNS:-/root/benchmarks/camp-runs}"
BUDGET_USD="${BUDGET_USD:-1.00}"
# `campaign.sh`'s own twenty-task default, byte for byte (its `TASKS=` line); `TASKS` overrides
# BOTH, because this driver passes the list down and the two must not be able to disagree.
TASKS="${TASKS:-discrete_log multi_dim_knapsack convex_hull rbf_interpolation set_cover_conflicts \
rectanglepacking min_dominating_set max_common_subgraph queens_with_obstacles \
max_independent_set_cpsat integer_factorization edge_expansion pagerank \
count_riemann_zeta_zeros spectral_clustering max_clique_cpsat sparse_eigenvectors_complex \
kcenters max_weighted_independent_set pde_heat1d}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---------------------------------------------------------------------------------------------
# REFUSALS, before a log line is written anywhere that could vanish.
# ---------------------------------------------------------------------------------------------

# `/var/tmp` IS WHERE THE LAST DRIVER DIED. The box profile puts the whole bench there on purpose
# (geesefs cannot host a venv), and that is fine for the runtime -- but a driver that writes its
# only record under a volatile root and is not told so is how a campaign's log came to exist only
# in a snapshot that misdated it. The refusal names the variable and the flag; `ALLOW_VOLATILE_ROOT=1`
# is the operator saying "I know, and the snapshot at the end is where the record goes".
case "$AT" in
  /var/tmp/*)
    if [ "${ALLOW_VOLATILE_ROOT:-0}" != "1" ]; then
      echo "[$(now)] REFUSED: ALGOTUNE_ROOT=$AT is under /var/tmp, which does not survive a" >&2
      echo "[$(now)]   container restart -- the 2026-08-24 driver and its log died exactly there." >&2
      echo "[$(now)]   Set ALLOW_VOLATILE_ROOT=1 to run anyway (and keep SNAPSHOT=1, the default)." >&2
      exit 2
    fi ;;
esac
case "$OUT" in
  /var/tmp/*)
    if [ "${ALLOW_VOLATILE_ROOT:-0}" != "1" ]; then
      echo "[$(now)] REFUSED: CAMPAIGN_OUT=$OUT is under /var/tmp; the markers, the attempt" >&2
      echo "[$(now)]   ledgers and this driver's own log would live only until the next restart." >&2
      echo "[$(now)]   Set ALLOW_VOLATILE_ROOT=1 to run anyway (and keep SNAPSHOT=1, the default)." >&2
      exit 2
    fi ;;
esac
# A REOPEN FLAG IS A SECOND ATTEMPT. Both flags belong to a human running `campaign.sh` directly
# after reading a banner; inherited by this driver they would make a blind resume re-run task-arms
# the campaign had already decided about, and the markers would then carry `attempt=a2` from the
# driver that promised one.
if [ "${RETRY_WALL_CUT:-0}" = "1" ] || [ "${RETRY_IMMEDIATE_EXIT:-0}" = "1" ]; then
  echo "[$(now)] REFUSED: RETRY_WALL_CUT/RETRY_IMMEDIATE_EXIT is set. This driver makes ONE attempt" >&2
  echo "[$(now)]   per task-arm; reopen a marker by running campaign.sh by hand and say so in the log." >&2
  exit 2
fi
[ -d "$AT/AlgoTuner" ] || { echo "[$(now)] REFUSED: no AlgoTune checkout at $AT (set ALGOTUNE_ROOT)" >&2; exit 2; }
[ -f "$HERE/campaign.sh" ] || { echo "[$(now)] REFUSED: no campaign.sh beside $0" >&2; exit 2; }
[ -n "$(echo $TASKS)" ] || { echo "[$(now)] REFUSED: TASKS is empty" >&2; exit 2; }

mkdir -p "$OUT"
LOG="$OUT/run_final.log"
say() { echo "[$(now)] $*" | tee -a "$LOG"; }
# Every line of the CAMPAIGN's output gets the date too, as it passes through. `awk` rather than a
# shell `while read` loop on purpose: the property this file pins is that it contains no loop at
# all, and a timestamping loop is one `continue` away from becoming a retry loop at the next merge.
stamped() { awk '{ cmd = "date -u +%Y-%m-%dT%H:%M:%SZ"; cmd | getline d; close(cmd);
                   print "[" d "] " $0; fflush() }' | tee -a "$LOG"; }

# ---------------------------------------------------------------------------------------------
# ONE CONFIGURATION, recorded before the first task and checked on every resume.
# ---------------------------------------------------------------------------------------------
# Exactly the variables that change what a task-arm MEASURES or how long it is allowed to: the
# task list, the budget, the model, the ruler (width, cores, cache dir, startup floor), the two
# clocks, the immediate-exit bar and the arm-A timeout the relaunch driver changed between
# attempts. `SNAPSHOT_DEST`/`METER_BASE` are deliberately absent: where the record is copied and
# which port the meter listens on do not change a number.
configuration() {
  echo "TASKS=$(echo $TASKS)"
  echo "BUDGET_USD=$BUDGET_USD"
  echo "ALGOTUNE_MODEL_KEY=${ALGOTUNE_MODEL_KEY:-}"
  echo "LOOPLAB_LLM_MODEL=${LOOPLAB_LLM_MODEL:-}"
  echo "ALGOTUNE_EVAL_WORKERS=${ALGOTUNE_EVAL_WORKERS:-}"
  echo "ALGOTUNE_EVAL_CORES_PER_WORKER_PIN=${ALGOTUNE_EVAL_CORES_PER_WORKER_PIN:-}"
  echo "ALGOTUNE_BASELINE_CACHE_DIR=${ALGOTUNE_BASELINE_CACHE_DIR:-}"
  echo "ALGOTUNE_MIN_TIMEOUT_S=${ALGOTUNE_MIN_TIMEOUT_S:-}"
  echo "ALGOTUNE_LLM_TIMEOUT_S=${ALGOTUNE_LLM_TIMEOUT_S:-}"
  echo "HARD_TIMEOUT=${HARD_TIMEOUT:-}"
  echo "STALL_TIMEOUT=${STALL_TIMEOUT:-}"
  echo "IMMEDIATE_EXIT_S=${IMMEDIATE_EXIT_S:-}"
  echo "CORES_PER_LANE=${CORES_PER_LANE:-}"
  echo "LANES=${LANES:-}"
  echo "CARD_ARGS=${CARD_ARGS:-}"
  echo "MAKE_TASK_ARGS=${MAKE_TASK_ARGS:-}"
}
CONF="$OUT/run_final.CONFIGURATION"
if [ -s "$CONF" ]; then
  if ! diff -u "$CONF" <(configuration) > "$OUT/run_final.CONFIGURATION.diff"; then
    say "REFUSED: $OUT already holds a campaign recorded under a DIFFERENT configuration."
    say "  A resume under changed settings is the a3/a5 defect of 2026-08-24 (docs/58 s58.1)."
    say "  The difference is in $OUT/run_final.CONFIGURATION.diff; either restore the recorded"
    say "  settings, or start a new campaign in a new CAMPAIGN_OUT."
    exit 2
  fi
  rm -f "$OUT/run_final.CONFIGURATION.diff"
  say "resuming the campaign recorded in $CONF (configuration unchanged)"
else
  configuration > "$CONF"
  say "configuration recorded in $CONF"
fi

# ---------------------------------------------------------------------------------------------
# THE CAMPAIGN: arm A to completion, then arm B in the same regime.
# ---------------------------------------------------------------------------------------------
NTASKS="$(echo $TASKS | wc -w)"
say "===== FINAL CAMPAIGN: both arms, $NTASKS tasks, \$$BUDGET_USD each, one attempt per task-arm ====="
say "  AlgoTune $AT | out $OUT | runs $RUNS_ROOT | looplab $(cd "$REPO" && git log --oneline -1 2>/dev/null)"
say "  tasks: $(echo $TASKS)"

say "===== ARM A ====="
ARM=A TASKS="$TASKS" BUDGET_USD="$BUDGET_USD" bash "$HERE/campaign.sh" 2>&1 | stamped
RC_A=${PIPESTATUS[0]}
say "arm A exited $RC_A"

say "===== ARM B ====="
ARM=B TASKS="$TASKS" BUDGET_USD="$BUDGET_USD" bash "$HERE/campaign.sh" 2>&1 | stamped
RC_B=${PIPESTATUS[0]}
say "arm B exited $RC_B"

# THE ATTEMPT LEDGERS ARE THE PROOF of rule 2, printed rather than assumed: `campaign.sh::
# next_attempt` appends one line per attempt per task-arm, so any ledger with more than one line
# names a task-arm this driver did NOT run exactly once (a resume after an interruption, which is
# legitimate and is still worth seeing in the log).
MULTI="$(wc -l "$OUT"/[AB]-*.attempts 2>/dev/null | awk '$1 > 1 && $2 != "total" { print $2 }')"
if [ -n "$MULTI" ]; then
  say "NOTE: task-arms with MORE THAN ONE attempt in their ledger (a resume re-ran them):"
  say "  $(echo $MULTI)"
fi

if [ "$RC_A" -ne 0 ] || [ "$RC_B" -ne 0 ]; then
  say "===== FINAL CAMPAIGN UNFINISHED: arm A rc=$RC_A, arm B rc=$RC_B ====="
  say "  Do not summarise an arm that exited non-zero (see campaign.sh's exit codes)."
  exit 3
fi
say "===== FINAL CAMPAIGN COMPLETE ====="
say "summarise with:  python $REPO/benchmarks/algotune/compare_arms.py \\"
say "    --algotune-root $AT --runs-root $RUNS_ROOT --final-dir $OUT --reference"
exit 0
