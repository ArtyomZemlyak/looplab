#!/bin/bash
# AlgoTune campaign driver. One ARM per invocation, tasks run in PARALLEL LANES.
#
#   ARM=A ./campaign.sh          # the reference loop (AlgoTuner), depends on no LoopLab code
#   ARM=B ./campaign.sh          # LoopLab, AFTER a rebase onto master (see below)
#
# Run `setup_algotune.sh /path/to/AlgoTune` first, on every machine, or the numbers are not the
# numbers this arm measures.
#
# WHY PARALLEL BY TASK AND NOT INSIDE ONE
# ---------------------------------------
# `validation_pool.num_workers` parallelises nothing: `BenchmarkPool` is defined in AlgoTuner and
# instantiated NOWHERE, and the timing path forks one process per timed run and waits for it.
# Measured 2026-08-20 on an 8-core box: one task-arm draws ~1.3 cores and the machine sat at load
# 0.7 while a 20-task arm projected to ~35 hours. The parallelism that exists is BETWEEN tasks.
#
# WHY THIS DOES NOT CORRUPT THE TIMINGS
# -------------------------------------
# The score is a RATIO, baseline_ms / optimized_ms, and both halves are measured inside ONE task.
# Each lane owns dedicated cores for the whole of that task, so a task never shares a core and its
# two measurements are taken under identical conditions start to finish. That is stronger than a
# single unpinned run, where the task floats across every core.
#
# Do NOT oversubscribe a lane. A throttled lane still measures its own ratio correctly, but it
# stops being comparable to a lane that was not throttled -- and cross-task comparison is the whole
# output. CORES_PER_LANE=2 covers the measured 1.3-core appetite with headroom.
#
# THE REGIME IS PART OF THE MEASUREMENT. Arm B must run with the same LANES and CORES_PER_LANE as
# arm A did, or the two arms were not measured alike; every .done row records both so a mismatch is
# visible afterwards rather than invisible.
#
# BEFORE ARM B: rebase onto master and re-run the suite. Arm B's number is a claim about a VERSION
# of LoopLab, and the only version worth benchmarking is the one that ships.
#
# Concurrency safety, checked rather than assumed:
#   * AlgoTuner/main.py::update_summary_json takes an exclusive O_CREAT|O_EXCL lock, so lanes may
#     share reports/agent_summary.json;
#   * each AlgoTuner run creates its own temporary CODE_DIR;
#   * looplab_eval.py (arm B) writes a per-pid results dir and summary.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
AT="${ALGOTUNE_ROOT:-/root/benchmarks/AlgoTune}"
OUT="${CAMPAIGN_OUT:-/root/benchmarks/campaign}"
WS="${CAMPAIGN_WS:-/root/benchmarks/looplab_ws}"
RUNS_ROOT="${CAMPAIGN_RUNS:-/root/benchmarks/camp-runs}"
BUDGET_USD="${BUDGET_USD:-0.02}"
# The AlgoTuner config KEY for the model (an exact `config["models"].get(name)` lookup, not a suffix
# match). Defaults to the OpenRouter shape the campaign was designed with; a box whose model comes
# from a gateway sets this to that entry's key instead -- see benchmarks/meter/setup_gateway_arm.py.
ALGOTUNE_MODEL_KEY="${ALGOTUNE_MODEL_KEY:-openrouter/${LOOPLAB_LLM_MODEL:-deepseek/deepseek-v4-flash-0731}}"
# When set, every LLM call goes through the metering proxy on a path that names the arm and the
# task, so cost is attributed per task-arm without either framework knowing it is metered.
# e.g. METER_BASE=http://127.0.0.1:8801  ->  http://127.0.0.1:8801/m/B/svm/v1
METER_BASE="${METER_BASE:-}"
HARD_TIMEOUT="${HARD_TIMEOUT:-14400}"

ARM="${ARM:-A}"
case "$ARM" in A|B) ;; *) echo "ARM must be A or B (got '$ARM')"; exit 2 ;; esac
[ -d "$AT/AlgoTuner" ] || { echo "no AlgoTune checkout at $AT (set ALGOTUNE_ROOT)"; exit 2; }

# Lanes scale with the machine. Two cores per lane, two cores left for the driver, the LLM client
# and the OS, and never more lanes than there are tasks.
NPROC="$(nproc)"
CORES_PER_LANE="${CORES_PER_LANE:-2}"
# CORE_OFFSET moves every lane up by N cores, so a debug or second campaign can run BESIDE a live
# one without sharing a core with it. Sharing would silently break the dedicated-core guarantee the
# timing argument rests on, and the damage would be invisible in the output.
CORE_OFFSET="${CORE_OFFSET:-0}"
MAX_LANES=$(( (NPROC - 2 - CORE_OFFSET) / CORES_PER_LANE ))
[ "$MAX_LANES" -lt 1 ] && MAX_LANES=1

TASKS="${TASKS:-discrete_log multi_dim_knapsack convex_hull rbf_interpolation set_cover_conflicts \
rectanglepacking min_dominating_set max_common_subgraph queens_with_obstacles \
max_independent_set_cpsat integer_factorization edge_expansion pagerank \
count_riemann_zeta_zeros spectral_clustering max_clique_cpsat sparse_eigenvectors_complex \
kcenters max_weighted_independent_set pde_heat1d}"
NTASKS="$(echo $TASKS | wc -w)"
[ "$MAX_LANES" -gt "$NTASKS" ] && MAX_LANES=$NTASKS
LANE_COUNT="${LANES:-$MAX_LANES}"

# One dedicated, non-overlapping core range per lane.
declare -a LANE_CPUS
for L in $(seq 0 $((LANE_COUNT - 1))); do
  LO=$(( CORE_OFFSET + L * CORES_PER_LANE ))
  HI=$(( LO + CORES_PER_LANE - 1 ))
  LANE_CPUS[$L]="${LO}-${HI}"
done

cd "$AT"
# shellcheck disable=SC1091
source .venv/bin/activate
set -a; [ -f .env ] && source .env; set +a
export DATA_DIR="$AT/data"
export LOOPLAB_LLM_BASE_URL="${LOOPLAB_LLM_BASE_URL:-https://openrouter.ai/api/v1}"
export LOOPLAB_LLM_API_KEY_BASE_URL="$LOOPLAB_LLM_BASE_URL"
export LOOPLAB_LLM_MODEL="${LOOPLAB_LLM_MODEL:-deepseek/deepseek-v4-flash-0731}"
export LOOPLAB_LLM_API_KEY="${LOOPLAB_LLM_API_KEY:-${OPENROUTER_API_KEY:-}}"
export LOOPLAB_LLM_TEMPERATURE='0.0'
# The provider pin and the effort level are OpenRouter controls. On an endpoint that serves one
# deployment and exposes no reasoning channel they control nothing, and a box profile sets this to
# '{}' rather than leave a dead parameter in the record where a reader would take it for live.
export LOOPLAB_LLM_REASONING_EXTRA="${LOOPLAB_LLM_REASONING_EXTRA:-{\"provider\":{\"order\":[\"siliconflow/fp8\"],\"allow_fallbacks\":false},\"reasoning\":{\"effort\":\"medium\"}}}"
export LOOPLAB_LLM_BUDGET_USD="$BUDGET_USD"
export PYTHONPATH="$REPO"
mkdir -p "$OUT" "$WS"

reap_orphan_workers() {
  # `pkill -f <name>` does not reach a multiprocessing forkserver: its command line carries neither
  # the app name nor the script name, which is how ten of them once survived a series of restarts
  # and burned CPU on the very cores a run was pinned to. Reap by module path, and ONLY orphans, so
  # a live lane's own workers are never touched.
  for P in $(pgrep -f "multiprocessing.fork[s]erver" 2>/dev/null); do
    if [ "$(ps -o ppid= -p "$P" 2>/dev/null | tr -d ' ')" = "1" ]; then kill -9 "$P" 2>/dev/null; fi
  done
}

record_done() {   # $1 = marker path, $2 = exit code, $3 = start epoch, $4 = cpus
  RC=$2
  # A `.done` marker means "this task-arm reached a TERMINAL state and must not be re-run". It must
  # NOT be written for a run that was interrupted: an interrupted task has no verdict, and a marker
  # makes a later resume SKIP it silently. Measured 2026-08-20: stopping a campaign wrote six
  # markers over live runs -- one of them 230 minutes in -- and the resume would have treated all
  # six as complete with no score.
  #   0        - the run ended on its own (a score, or the harness's own N/A)
  #   2        - a TYPED OPERATOR REFUSAL (`cli/__init__.py::REFUSAL_EXIT_CODE`), which for this
  #              campaign is almost always the LLM spend ceiling. Terminal, and this is the whole
  #              point of the exit code: a refusal is a property of the INPUT, so the next attempt
  #              spends the same allowance, or reads the same bad task file, and stops at the same
  #              wall. Before `BudgetExceeded` wore the marker it exited 1 with a traceback and was
  #              recorded as "interrupted, still owed" -- i.e. a FINISHED task queued for a retry
  #              that could never do anything different.
  #   124      - the wall-clock net fired; terminal, and deliberately recorded so it is visible
  #              rather than retried forever, but it produces no number (see docs/48)
  #   130/137/143 and anything else - interrupted. NO marker; the task is still owed.
  case "$RC" in
    0|2|124) echo "wall=$(( $(date +%s) - $3 )) rc=$RC cpus=$4 lanes=$LANE_COUNT cores_per_lane=$CORES_PER_LANE" > "$1" ;;
    *)     echo "  [$(date +%H:%M:%S)][$4] interrupted (rc=$RC) -- no marker written, task still owed" ;;
  esac
}

run_one() {                       # $1 = task, $2 = cpu list
  T=$1; CPUS=$2
  if [ -n "$METER_BASE" ]; then
    # Both arms, same meter, one path segment apart. Arm A reaches it through OPENAI_BASE_URL,
    # which litellm honours for an `openai/<model>` entry that carries no api_base of its own.
    export LOOPLAB_LLM_BASE_URL="$METER_BASE/m/$ARM/$T/v1"
    export LOOPLAB_LLM_API_KEY_BASE_URL="$LOOPLAB_LLM_BASE_URL"
    export OPENAI_BASE_URL="$LOOPLAB_LLM_BASE_URL"
    export OPENAI_API_KEY="${LOOPLAB_LLM_API_KEY:-meter}"
  fi
  if [ "$ARM" = "A" ]; then
    if [ -s "$OUT/A-$T.done" ]; then echo "[$CPUS] $T arm A already done"; return; fi
    S=$(date +%s)
    timeout "$HARD_TIMEOUT" taskset -c "$CPUS" ./algotune.sh agent --standalone \
        "$ALGOTUNE_MODEL_KEY" "$T" > "$OUT/A-$T.log" 2>&1
    RC=$?
    record_done "$OUT/A-$T.done" "$RC" "$S" "$CPUS"
    [ -s "$OUT/A-$T.done" ] && echo "[$(date +%H:%M:%S)][$CPUS] $T arm A done ($(cat "$OUT/A-$T.done"))"
  else
    if [ -s "$OUT/B-$T.done" ]; then echo "[$CPUS] $T arm B already done"; return; fi
    TASK_ROOT="$RUNS_ROOT/$T"
    rm -rf "$TASK_ROOT"; mkdir -p "$TASK_ROOT/memory" "$TASK_ROOT/knowledge"
    python "$REPO/benchmarks/algotune/make_task.py" --algotune-root "$AT" --task "$T" \
        --out-dir "$WS" >/dev/null 2>&1
    S=$(date +%s)
    # Per-task memory and knowledge dirs: LoopLab can mine its own past runs and a shared store,
    # and AlgoTuner has no equivalent -- left shared, arm B would reach task 12 with eleven prior
    # runs to read, measuring a capability the other arm does not have rather than the loop.
    LOOPLAB_MEMORY_DIR="$TASK_ROOT/memory" LOOPLAB_KNOWLEDGE_DIR="$TASK_ROOT/knowledge" \
      timeout "$HARD_TIMEOUT" taskset -c "$CPUS" python -m looplab.cli run \
        "$WS/algotune_$T.json" --out "$TASK_ROOT/run" --backend llm --max-nodes 20 \
        > "$OUT/B-$T.log" 2>&1
    RC=$?   # captured HERE: the champion extraction and the test scoring below both clobber $?
    # Champion from the FOLD, then ONE scoring pass on TEST: every node above ran on TRAIN, which
    # is what AlgoTuner's own agent does. Without this the arm optimises against its graded split.
    if python "$REPO/benchmarks/algotune/extract_champion.py" --run-dir "$TASK_ROOT/run" \
           --out "$TASK_ROOT/champion_solver.py" >> "$OUT/B-$T.log" 2>&1; then
      (cd "$TASK_ROOT" && timeout "$HARD_TIMEOUT" taskset -c "$CPUS" \
          python "$REPO/benchmarks/algotune/looplab_eval.py" --algotune-root "$AT" --task "$T" \
          --model LoopLabFinal --solver champion_solver.py --subset test) \
          > "$OUT/B-$T.final.json" 2>>"$OUT/B-$T.log"
    else
      echo '{"speedup": null, "error": "no champion to score"}' > "$OUT/B-$T.final.json"
    fi
    record_done "$OUT/B-$T.done" "$RC" "$S" "$CPUS"
    [ -s "$OUT/B-$T.done" ] && echo "[$(date +%H:%M:%S)][$CPUS] $T arm B done ($(cat "$OUT/B-$T.done"))"
  fi
}

echo "arm $ARM | $NTASKS tasks | $LANE_COUNT lanes x $CORES_PER_LANE cores from core $CORE_OFFSET (of $NPROC) | budget \$$BUDGET_USD"
echo "model $ALGOTUNE_MODEL_KEY | llm ${METER_BASE:-$LOOPLAB_LLM_BASE_URL}${METER_BASE:+ (metered, per-task paths)}"
reap_orphan_workers

# One PID slot per lane, assigned by ACTUAL freeness. Round-robin by index would hand task N+k the
# same lane whether or not it is still busy, so two tasks would share a core range while another
# sits idle -- which breaks the dedicated-core guarantee the timing argument rests on.
declare -a LANE_PID
for L in $(seq 0 $((LANE_COUNT - 1))); do LANE_PID[$L]=""; done

for T in $TASKS; do
  SLOT=""
  while [ -z "$SLOT" ]; do
    for L in $(seq 0 $((LANE_COUNT - 1))); do
      if [ -z "${LANE_PID[$L]}" ] || ! kill -0 "${LANE_PID[$L]}" 2>/dev/null; then SLOT=$L; break; fi
    done
    [ -z "$SLOT" ] && sleep 5
  done
  reap_orphan_workers
  run_one "$T" "${LANE_CPUS[$SLOT]}" &
  LANE_PID[$SLOT]=$!
done
wait
reap_orphan_workers
echo "[$(date +%H:%M:%S)] ===== arm $ARM COMPLETE ====="
echo "summarise with:  python $REPO/benchmarks/algotune/compare_arms.py \\"
echo "    --algotune-root $AT --runs-root $RUNS_ROOT --final-dir $OUT --reference"
