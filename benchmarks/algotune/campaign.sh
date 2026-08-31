#!/bin/bash
# AlgoTune campaign driver. One ARM per invocation, tasks run in PARALLEL LANES.
#
#   ARM=A ./campaign.sh          # the reference loop (AlgoTuner), depends on no LoopLab code
#   ARM=B ./campaign.sh          # LoopLab, AFTER a rebase onto master (see below)
#
# EXIT CODES. 0 = every task-arm reached a terminal state. 2 = this driver refused before running
# anything (bad ARM, no AlgoTune checkout, an arm-A budget that does not match). 3 = the arm ran but
# one or more task-arms REFUSED TO START and were therefore not measured -- see `record_done` and
# `final_banner`, and do not summarise an arm that exits 3.
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

# `$OUT` holds the markers, the per-task logs AND the append-only attempt ledger, and nothing was
# creating it. It existed by luck; the moment a reset wiped it, every task's FIRST `next_attempt`
# write failed with "No such file or directory" and the ledger stayed empty — so `N` recomputed as
# 1 on the next attempt too, and the attempt ids the markers carry (`a1`, `a2`, ...) would repeat
# instead of counting. Observed on the 09:02 launch: four tasks, four failed ledger writes.
mkdir -p "$OUT"
BUDGET_USD="${BUDGET_USD:-0.02}"
# The AlgoTuner config KEY for the model (an exact `config["models"].get(name)` lookup, not a suffix
# match). Defaults to the OpenRouter shape the campaign was designed with; a box whose model comes
# from a gateway sets this to that entry's key instead -- see benchmarks/meter/setup_gateway_arm.py.
ALGOTUNE_MODEL_KEY="${ALGOTUNE_MODEL_KEY:-openrouter/${LOOPLAB_LLM_MODEL:-deepseek/deepseek-v4-flash-0731}}"
# When set, every LLM call goes through the metering proxy on a path that names the arm, the task
# and THIS ATTEMPT at it, so cost is attributed per attempt without either framework knowing it is
# metered.
# e.g. METER_BASE=http://127.0.0.1:8801  ->  http://127.0.0.1:8801/m/B/svm/a1/v1
METER_BASE="${METER_BASE:-}"
# DEFAULTED HERE because the rc=0 guard below is only as good as its ability to FIND the log.
# `start_meter.sh` (the writer) and `box-jhub-l40s.sh` both default this; campaign.sh (the reader)
# did not, so an operator following the documented invocation without sourcing the box profile
# left `successful_calls` answering "" -- unknowable -- for every task, and the guard that exists
# to catch a total endpoint outage failed OPEN on exactly the launch it was written for. Same
# expression as the writer: one path, not two.
METER_LOG="${METER_LOG:-${BENCH_ROOT:-/var/tmp/looplab-bench}/meter/meter.jsonl}"
# THE WALL IS OFF BY DEFAULT, and a STALL bound replaces it. Measured 2026-08-24 on a full campaign:
# the 4 h wall cut 13 of arm A's 19 task-arms and 3 of arm B's, and three of those cuts had not spent
# the budget they were being compared at — one reached $0.14 of $1.00. So the clock, not the money,
# was deciding most of arm A's rows, and a table that says "both arms at $1.00" was true of the
# CEILING and false of the SPEND ($0.042 against $1.619 on the nine serially-evaluated tasks).
#
# But "no bound at all" is not the answer either: a lane that HANGS holds one of four lanes forever
# and the campaign never finishes, which is the failure the wall was there for. The two cases look
# nothing alike from outside and only one of them deserves a kill:
#
#   SLOW   — the run is producing events, paying for calls, evaluating nodes. It just needs longer,
#            because it evaluates 100 instances at 46 s each. Killing it destroys real work.
#   HUNG   — no event, no child process, no LLM call, for many minutes. Observed: a lane sat in
#            `poll()` on an idle socket for 18 minutes after a stream was cut.
#
# So `HARD_TIMEOUT=0` (the default now) means no wall, and `STALL_TIMEOUT` bounds SILENCE instead:
# the lane is killed only when its own event log has not grown for that long. `HARD_TIMEOUT` is kept
# as an opt-in backstop for anyone who wants a hard ceiling anyway.
HARD_TIMEOUT="${HARD_TIMEOUT:-0}"
STALL_TIMEOUT="${STALL_TIMEOUT:-2400}"        # 40 min of total silence = hung, not slow
# The graded champion pass is bounded by a WALL of its own (see its call site): it is a
# known-shape workload with no agent in it, and it is legitimately quiet while it scores.
CHAMPION_TIMEOUT="${CHAMPION_TIMEOUT:-14400}"

# `timeout 0` means "no timeout" to GNU coreutils, so one spelling serves both settings and there is
# no second code path to keep in step.
#
run_bounded() {   # run_bounded <events-file-or-empty> <cmd…>
  local watch="$1"; shift
  # STALL_FLAG is the breadcrumb that tells `record_done` WE killed this lane rather than a human.
  # A SIGTERM from the guard below arrives as rc=143, which is byte-identical to the operator's own
  # Ctrl-C, and `record_done`'s `*)` arm reads 143 as "interrupted -- still owed, no marker". So a
  # STRUCTURALLY stalled task was re-run from scratch on every resume, for ever, each time spending
  # a fresh full LLM budget to stall at the same place. The flag is a file because the guard runs in
  # a subshell and a variable set there is lost.
  [ -n "${STALL_FLAG:-}" ] && rm -f "$STALL_FLAG"
  if [ -z "$watch" ] || [ "${STALL_TIMEOUT:-0}" = "0" ]; then
    timeout "$HARD_TIMEOUT" "$@"
    return $?
  fi
  local t0; t0=$(date +%s)
  timeout "$HARD_TIMEOUT" "$@" &
  local job=$!
  ( while kill -0 "$job" 2>/dev/null; do
      sleep 60
      # The stall clock reads the RUN's own log, not the wall clock: a run that is writing is alive.
      #
      # A WATCH FILE THAT DOES NOT EXIST YET IS SILENCE, NOT LIFE. This was
      # `stat … || echo "$now"`, so an absent file made `now-last` zero and the guard could never
      # fire. For arm B the watch is `events.jsonl`, which does not exist until `Engine.__init__`
      # writes `run_started` -- AFTER `preflight_role_endpoints` and `make_roles`. So a hang in
      # preflight (a gateway that completes TLS and never answers), in a wedged repo/data mount, or
      # in an import left the lane with NO bound at all once the wall went to 0, which is the exact
      # "endpoint down, lane hung for ever" case the stall guard was added for. Falling back to the
      # START of the run measures the silence that has actually elapsed. Arm A only escaped this by
      # pre-creating its log with `: >`.
      # OPEN[stall-guard-reads-one-files-mtime] the stall clock watches events.jsonl alone, and an
      # arm-B lane inside a long, healthy `score` stage appends nothing there for the whole
      # evaluation — so a legitimately slow candidate can be killed as a stall and the task-arm
      # filed as a terminal, never-retried harness cut.
      # proof:`present:last=$(stat -c %Y "$watch"@benchmarks/algotune/campaign.sh`
      # REVIEW 2026-08-30 (correctness): stage events land at stage END; per-instance timeout is
      # max(10x baseline, floor) so a valid slow solver runs ~50 min over 100 instances against
      # STALL_TIMEOUT=2400, and the README records an 87-minute evaluation. The champion pass was
      # exempted for exactly this reason ("a scoring pass is legitimately silent for long
      # stretches"); the in-run evaluations, which go through the same evaluator, were not. Watch
      # something the eval actually grows (the node workdir's stage log, or the newest mtime under
      # the run dir), or raise the bound for the eval window.
      local now last
      now=$(date +%s)
      last=$(stat -c %Y "$watch" 2>/dev/null || echo "$t0")
      if [ $((now - last)) -gt "$STALL_TIMEOUT" ]; then
        echo "STALL: $watch has not grown in $((now - last))s — killing this lane" >&2
        [ -n "${STALL_FLAG:-}" ] && : > "$STALL_FLAG"
        kill -TERM "$job" 2>/dev/null; sleep 5; kill -KILL "$job" 2>/dev/null
        return 0
      fi
    done ) &
  local guard=$!
  wait "$job"; local rc=$?
  kill "$guard" 2>/dev/null
  return $rc
}
# A `.done` written for rc=124 means "the wall clock cut this", and by default that is still
# terminal -- see `already_measured` for why, and for what this flag costs.
RETRY_WALL_CUT="${RETRY_WALL_CUT:-0}"

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

# One dedicated core range per lane -- OVER WHOLE PHYSICAL CORES, not over CPU NUMBERS.
#
# THE DEFECT THIS REPLACES, and it invalidated every number the arena produced before 2026-08-24.
# A contiguous `LO-HI` range assumes CPU numbering is a partition of the hardware. On this box
# (AMD EPYC 9454P, 48 physical cores, SMT 2) it is not: cpus 0-47 are the FIRST thread of each
# physical core and 48-95 are their siblings, so `0-21` and `44-65` — lanes 1 and 3 — sat on the
# SAME SILICON (cpu0 pairs with cpu48, cpu17 with cpu65). Every lane shared physical cores with
# another; `taskset` separated the numbers and nothing separated the hardware.
#
# What that cost: re-scoring ONE unchanged solver under a quiet machine against the number the
# campaign recorded for it under four live lanes gave **57.36 against 1.0103**. A speedup is
# `baseline_ms / solver_ms` with the baseline taken at one moment and the solver at another, so a
# neighbour on your own physical core does not add noise — it moves the answer by a factor of 57.
#
# The lane is therefore built from SIBLING PAIRS: 11 whole physical cores = 22 logical, four lanes
# = 44 of the 48, and the remaining four physical cores carry the meter, the watchdog and any
# diagnostic. `CORE_OFFSET` still shifts the allocation, now in units of physical cores.
declare -a LANE_CPUS
_LANE_PLAN="$(python3 - "$LANE_COUNT" "$CORES_PER_LANE" "$CORE_OFFSET" <<'PYEOF'
import sys

lanes, per_lane, offset = (int(x) for x in sys.argv[1:4])


def _cpulist(text):
    """Parse the kernel's cpulist format: `0,4` AND `0-1` AND `0-3,8-11`.

    `thread_siblings_list` emits a RANGE whenever siblings are numbered adjacently, which is the
    standard layout on KVM/cloud guests and on any host configured `sibling = core*2, core*2+1`.
    Splitting on commas alone then hands `int()` the string `"0-1"`, and the resulting ValueError
    is NOT an OSError, so it escaped the guard below, killed the heredoc, and left the shell with
    zero ranges -- `lane plan produced 0 ranges`, exit 2, the whole arm refusing to start on a
    machine where nothing was wrong.
    """
    out = []
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            out.extend(range(int(lo_s), int(hi_s) + 1))
        else:
            out.append(int(part))
    return tuple(sorted(out))


pairs = []
seen = set()
for cpu in range(4096):
    try:
        with open(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list") as fh:
            sibs = _cpulist(fh.read())
    except OSError:
        break
    if sibs and sibs not in seen:
        seen.add(sibs)
        pairs.append(sibs)

# SMT WIDTH IS MEASURED, NOT ASSUMED. This was `per_lane // 2`, which is only the right divisor on
# an SMT-2 box. With SMT OFF -- most cloud VMs and containers, and this very box, where cpu0's
# sibling list is just `0` -- each lane got `CORES_PER_LANE // 2` logical cpus, i.e. HALF what the
# operator asked for and, at the shipped default of 2, a SINGLE cpu against the measured 1.3-core
# appetite. Lanes then throttle each other while `$REGIME` still records `cores_per_lane=2`, so the
# marker asserts a regime that never ran. Round UP: a lane owns whole physical cores, and giving it
# one more core than it asked for is honest where giving it fewer is the defect the header names.
smt = max(1, min(len(p) for p in pairs)) if pairs else 1
phys_per_lane = max(1, -(-per_lane // smt))    # ceil(per_lane / smt)
need = offset + lanes * phys_per_lane
if need > len(pairs):
    # NOT ENOUGH PHYSICAL CORES. The contiguous `LO-HI` layout below is the exact allocation this
    # whole block exists to abolish -- it is what measured 1.0103 where a quiet machine measured
    # 57.36 -- so falling into it silently is the worst outcome available. It used to print the
    # bare word FALLBACK on STDERR, which `$( )` does not capture and nothing therefore read.
    # It now leads STDOUT, so the shell sees it, tells the operator, and stamps the regime: a
    # number taken under this layout is still recorded, and can never again be mistaken for one
    # taken under whole cores.
    print("FALLBACK")
    for lane in range(lanes):
        lo = offset + lane * per_lane
        print(f"{lo}-{lo + per_lane - 1}")
    raise SystemExit(0)
for lane in range(lanes):
    chunk = pairs[offset + lane * phys_per_lane: offset + (lane + 1) * phys_per_lane]
    print(",".join(str(c) for pair in chunk for c in sorted(pair)))
PYEOF
)"
# `layout=` rides in $REGIME beside lanes/cores_per_lane because it is the regime property that
# INVALIDATES a number rather than merely describing it. `whole_cores` = no lane shares silicon
# with another; `contiguous_fallback` = they may, and the 57.36-vs-1.0103 measurement applies.
LANE_LAYOUT="whole_cores"
_L=0
while IFS= read -r _line; do
  [ -n "$_line" ] || continue
  if [ "$_line" = "FALLBACK" ]; then LANE_LAYOUT="contiguous_fallback"; continue; fi
  LANE_CPUS[$_L]="$_line"
  _L=$((_L + 1))
done <<< "$_LANE_PLAN"
[ "$_L" = "$LANE_COUNT" ] || { echo "lane plan produced $_L ranges for $LANE_COUNT lanes"; exit 2; }
if [ "$LANE_LAYOUT" = "contiguous_fallback" ]; then
  echo "############################################################################"
  echo "# LANE PLAN FELL BACK TO CONTIGUOUS CPU RANGES."
  echo "# There are not enough physical cores for $LANE_COUNT lanes x $CORES_PER_LANE cpus"
  echo "# at CORE_OFFSET=$CORE_OFFSET. Lanes may now SHARE PHYSICAL CORES, which is the"
  echo "# allocation that measured a speedup of 1.0103 for a solver a quiet machine"
  echo "# measured at 57.36. Every marker from this campaign records"
  echo "# layout=contiguous_fallback so the numbers are not mistaken for clean ones."
  echo "# Reduce LANES or CORES_PER_LANE to get whole-core lanes back."
  echo "############################################################################"
fi

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
#
# The default is held in its own single-quoted variable and NOT inlined as `${VAR:-{...}}`: bash
# ends a `${...}` expansion at the first unquoted `}`, so the inline form truncated BOTH the
# override (`{}` came out as `{},"reasoning":...`) and the default itself (which lost the brace
# closing `provider` and shipped malformed JSON). Measured 2026-08-20 -- arm B died at
# `SettingsError: error parsing value for field "llm_reasoning_extra"`, which is the loud version;
# the default's silent corruption is the half that would have travelled into a campaign.
# The depth is NOT in here. It used to be, as `reasoning.effort` -- OpenRouter's spelling and a
# DIFFERENT request key from the `reasoning_effort` that `Settings.llm_reasoning` emits, so both
# were sent and the provider chose. The `medium` this campaign believed it ran at never took
# effect on a single call. Measured 2026-08-20: two `propose` calls burned the FULL 65,536-token
# completion cap without emitting a tool call -- 889 s and 593 s, $0.019, both ERROR ("no
# tool_calls in response"), both retried. `core/llm.py::reasoning_body` now REFUSES that pair, so
# the split below is enforced at the client rather than remembered here. What it refuses is a second
# DEPTH setting, member by member (`core/llm.py::REASONING_DEPTH_KNOBS`) -- not the mere use of a
# key: OpenRouter's `reasoning.exclude` and any non-thinking `chat_template_kwargs` belong in EXTRA
# and are accepted beside `LOOPLAB_LLM_REASONING`.
export LOOPLAB_LLM_REASONING="${LOOPLAB_LLM_REASONING:-medium}"
# EXTRA carries only what is NOT the depth: the provider pin. Unpinned, one slug reached two
# different fp4 providers and returned 96/17/96 completion tokens for one prompt.
DEFAULT_REASONING_EXTRA='{"provider":{"order":["siliconflow/fp8"],"allow_fallbacks":false}}'
export LOOPLAB_LLM_REASONING_EXTRA="${LOOPLAB_LLM_REASONING_EXTRA:-$DEFAULT_REASONING_EXTRA}"
export LOOPLAB_LLM_BUDGET_USD="$BUDGET_USD"

# ARM A'S BUDGET DOES NOT LIVE HERE, and pretending otherwise is how two arms end up on two
# budgets under one banner. `BUDGET_USD` reaches `LOOPLAB_LLM_BUDGET_USD`, which is LoopLab's
# ceiling and nothing else; AlgoTuner resolves its own as
# `model_info.get("spend_limit", global_config.spend_limit)` out of `config.yaml`. Measured
# 2026-08-21: the banner below printed one budget for both arms while arm A was still on the
# shipped `spend_limit: 0.02` -- so a $1.00 arm-B run could sit beside a $0.02 arm-A run and the
# log would say they matched.
#
# So arm A REFUSES rather than guesses. Rewriting somebody's config from a campaign driver is the
# worse failure: it would make every run silently authoritative over a file the fork owns.
# `patch_model_entry.py --spend-limit` is the one place that value is set.
# The hint an operator is about to FOLLOW, so it must be right for the key actually in play.
#
# `patch_model_entry.py --slug X` writes the key `openrouter/X`. That is correct only while the
# campaign runs on an OpenRouter key. `box-jhub-l40s.sh` sets `ALGOTUNE_MODEL_KEY=gateway/...`
# (the corporate gateway), where `${KEY#openrouter/}` strips nothing, so the printed command wrote
# `openrouter/gateway/...` -- a key the check does not look up, leaving the operator to fail the
# same check again having done exactly what it said. Measured 2026-08-22.
budget_hint() {
  case "$ALGOTUNE_MODEL_KEY" in
    openrouter/*)
      echo "  python3 $HERE/patch_model_entry.py --algotune-root $AT \\" >&2
      echo "      --slug ${ALGOTUNE_MODEL_KEY#openrouter/} --spend-limit $BUDGET_USD" >&2 ;;
    *)
      echo "  '$ALGOTUNE_MODEL_KEY' is not an OpenRouter key, so patch_model_entry.py does not" >&2
      echo "  own it. Set the ceiling ON THAT ENTRY in $AT/AlgoTuner/config/config.yaml:" >&2
      echo "      $ALGOTUNE_MODEL_KEY:" >&2
      echo "        spend_limit: $BUDGET_USD" >&2
      echo "  PER-MODEL, not the global: AlgoTuner resolves" >&2
      echo "  model_info.get('spend_limit', global_config.spend_limit), so an entry without one" >&2
      echo "  silently inherits the global (measured: 0.02 under a banner that said 1.00)." >&2 ;;
  esac
}

if [ "$ARM" = "A" ]; then
  A_LIMIT="$(python3 - "$AT" "$ALGOTUNE_MODEL_KEY" <<'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(f"{sys.argv[1]}/AlgoTuner/config/config.yaml")) or {}
entry = (cfg.get("models") or {}).get(sys.argv[2])
if entry is None:
    print("MISSING")
else:
    print(entry.get("spend_limit", (cfg.get("global") or {}).get("spend_limit", "")))
PYEOF
)"
  if [ "$A_LIMIT" = "MISSING" ]; then
    echo "arm A: no model entry '$ALGOTUNE_MODEL_KEY' in $AT/AlgoTuner/config/config.yaml." >&2
    echo "  add one:" >&2
    budget_hint
    exit 2
  fi
  if [ "$(python3 -c "print(abs(float('$A_LIMIT')-float('$BUDGET_USD'))<1e-9)")" != "True" ]; then
    echo "arm A budget MISMATCH: config.yaml says spend_limit=$A_LIMIT, BUDGET_USD=$BUDGET_USD." >&2
    echo "  The two arms would not be measured on the same budget. Re-point one of them:" >&2
    budget_hint
    exit 2
  fi
  echo "arm A budget: spend_limit=$A_LIMIT (matches BUDGET_USD)"

  # ARM A MUST ACTUALLY REACH THE METER, and whether it does is a property of its config ENTRY.
  #
  # `run_one` gives both arms the same per-task meter URL, arm B through LOOPLAB_LLM_BASE_URL and
  # arm A through OPENAI_BASE_URL. That second half only works for an entry litellm treats as a
  # generic OpenAI endpoint. AlgoTuner picks the litellm model name in `AlgoTuner/main.py`,
  # `llm_model_name = model_info.get("model_name", desired_model_name)` -- so an entry with NO
  # `model_name` is named by its own config KEY. For an `openrouter/...` key litellm resolves the
  # base as `api_base or litellm.api_base or OPENROUTER_API_BASE or "https://openrouter.ai/api/v1"`
  # (`litellm/main.py`, in `completion`'s base resolution) and OPENAI_BASE_URL appears nowhere in
  # that chain. Cited by SYMBOL and by the quoted expression, never by line: these are
  # third-party files whose numbering moves on every upgrade.
  #
  # The default `ALGOTUNE_MODEL_KEY` at the top of this file is exactly such a key, and the
  # `openrouter/deepseek/deepseek-v4-flash-0731` entry shipped in this checkout carries no
  # `model_name`. So the DEFAULT configuration with METER_BASE set sends arm A straight to
  # openrouter.ai: no shared RPM queue, no shared price table, no per-task attribution, zero rows in
  # meter.jsonl -- while the banner below still prints "(metered, per-task paths)" for both arms.
  # Every cost number the campaign then reports compares one arm's metered spend against the other
  # arm's absence, which is the one failure this whole meter exists to prevent.
  #
  # So it REFUSES rather than warns: an unmetered arm A is not a slower measurement, it is a
  # different experiment, and it is invisible afterwards. `setup_gateway_arm.py` writes the entry
  # that works (`model_name: "openai/<model>"`, no api_base, base URL from the environment).
  if [ -n "$METER_BASE" ]; then
    A_LITELLM_NAME="$(python3 - "$AT" "$ALGOTUNE_MODEL_KEY" <<'PYMETER'
import sys, yaml
cfg = yaml.safe_load(open(f"{sys.argv[1]}/AlgoTuner/config/config.yaml")) or {}
entry = (cfg.get("models") or {}).get(sys.argv[2]) or {}
# `AlgoTuner/main.py`, the `llm_model_name = model_info.get(...)` line -- the same resolution,
# re-derived rather than assumed.
print(entry.get("model_name", sys.argv[2]))
PYMETER
)"
    case "$A_LITELLM_NAME" in
      openai/*) echo "arm A meter: model_name=$A_LITELLM_NAME honours OPENAI_BASE_URL" ;;
      *)
        echo "arm A would BYPASS the meter: '$ALGOTUNE_MODEL_KEY' resolves to litellm model" >&2
        echo "  '$A_LITELLM_NAME', which is not an openai/* endpoint, so the OPENAI_BASE_URL this" >&2
        echo "  driver sets per task is ignored and the calls go to the provider directly." >&2
        echo "  Arm B WOULD be metered, so the two arms would be priced, rate-limited and" >&2
        echo "  attributed by different machinery -- not a comparison." >&2
        echo "  Give the entry an OpenAI-shaped model_name:" >&2
        echo "  python3 $REPO/benchmarks/meter/setup_gateway_arm.py --algotune-root $AT \\" >&2
        echo "      --key $ALGOTUNE_MODEL_KEY --model ${LOOPLAB_LLM_MODEL}" >&2
        echo "  (or unset METER_BASE and accept that neither arm is metered)." >&2
        exit 2 ;;
    esac
  fi
fi
export PYTHONPATH="$REPO"

# THE TWO COLD-BASELINE FUSES, DECLARED HERE INSTEAD OF LEFT AMBIENT.
#
# Every speedup this campaign reports is a RATIO, and this driver said nothing at all about the
# denominator. `looplab_eval.py` carries two guards over that half and BOTH were disarmed by the
# silence:
#
#   * `_regime_mismatch` opens with `if not (ALGOTUNE_BASELINE_CACHE_DIR or "--baseline-times-dir"
#     in sys.argv): return None` -- deliberately, so a unit test cannot be refused because of a
#     data directory it never asked for. The campaign set neither, so on every campaign run to date
#     that guard returned None on its FIRST LINE and no regime was ever checked.
#   * `_baseline_fingerprint`, which backs the `baseline_measured_in_pass` refusal, watches
#     `--baseline-times-dir`, whose default resolves beside `looplab_eval.py` in whatever clone the
#     bridge is executed from -- not necessarily the directory the patched `BaselineManager`
#     writes. Its own comment says so and names this file as the proof (`proof:absent:
#     --baseline-times-dir@benchmarks/algotune/campaign.sh`).
#
# WHAT THE SILENCE COST, measured. A cold cache is not a slow measurement, it is a different one:
# when AlgoTune has no per-instance reference for this (task, subset, regime) it measures one in
# the same pass and the CANDIDATE IS NEVER TIMED -- the evaluator reports the reference against
# itself at ~1.0 whatever was submitted. That is eight of this campaign's twenty final numbers.
#
# And the WIDTH is what decides whether the cache is cold. With nothing set, `resolve_workers`
# answers 1 worker and the arena keys `__lane<N>r3`; `run_probe.sh` declares `auto` and keys
# `__w<N>x1r3`. The reference cache on the live box holds `__w22x1r3` entries, so the campaign
# missed every one of them and re-timed -- while the probes hit. The two references sum to 3898 ms
# and 2976 ms over the same hundred instances, a 24 % difference in the denominator of every
# speedup, and the campaign and the probes were reporting numbers off two different instruments
# with nothing in either record saying which.
#
# So the ruler is DECLARED, in the same words `run_probe.sh` declares it, and both halves stay
# overridable for a side experiment that means to use another one.

# WHICH DIRECTORY, ASKED OF THE PATCH RATHER THAN GUESSED.
#
# `patch_baseline_cache.py` bakes the cache path into `BaselineManager` AT PATCH TIME, out of the
# clone that ran it, and this driver may be a different clone (docs/51 SS7 runs the campaign from
# the pinned `looplab-armb`). Pointing the guard at `$REPO`'s own `.baseline_times` would
# reproduce the exact defect it is being armed against -- fingerprinting a directory nothing
# writes -- so the value is READ OUT of the patched file, and $REPO's is only the fallback for a
# checkout that carries no patch at all.
baseline_cache_dir() {   # $1 = AlgoTune root. Echoes the directory the patch really writes.
  python3 - "$1" "$REPO" <<'PYEOF'
import re, sys
from pathlib import Path
patched = Path(sys.argv[1]) / "AlgoTuner" / "utils" / "evaluator" / "baseline_manager.py"
try:
    src = patched.read_text(encoding="utf-8", errors="replace")
except OSError:
    src = ""
# The assignment in either shape the patch has worn: a bare literal, or an
# `os.environ.get('ALGOTUNE_BASELINE_CACHE_DIR', '<default>')`. The first ABSOLUTE path in it is
# the answer -- the env-var NAME is quoted too and does not start with a slash.
m = re.search(r"_ll_cache_dir\s*=\s*(.{0,400}?)\n\s*_ll_key", src, re.S)
found = re.findall(r"['\"](/[^'\"]*)['\"]", m.group(1)) if m else []
print(found[0] if found else str(Path(sys.argv[2]) / "benchmarks" / "algotune" / ".baseline_times"))
PYEOF
}

declare_baseline_ruler() {
  # BOTH FUSES, and the width that decides which reference file they name.
  #
  # `auto` is one worker per core of the lane (`__w<N>x1r3`), which is what `run_probe.sh`
  # declares and what the live reference cache is keyed for. `1` is not a quieter setting of the
  # same instrument, it is a DIFFERENT one: at workers <= 1 the pool is bypassed and both halves
  # run in the lane's whole cpuset, keyed `__lane<N>r3`.
  ALGOTUNE_BASELINE_CACHE_DIR="${ALGOTUNE_BASELINE_CACHE_DIR:-$(baseline_cache_dir "$AT")}"
  export ALGOTUNE_BASELINE_CACHE_DIR
  export ALGOTUNE_EVAL_WORKERS="${ALGOTUNE_EVAL_WORKERS:-auto}"
  # Pinned at the arena's own default rather than inherited: a box profile that set this would
  # move the regime key under a campaign that never mentioned it.
  export ALGOTUNE_EVAL_CORES_PER_WORKER="${ALGOTUNE_EVAL_CORES_PER_WORKER:-1}"
  # The guard globs this directory; a missing one makes `_baseline_fingerprint` answer `{}` both
  # times and compare equal, which is the silence again by another route.
  mkdir -p "$ALGOTUNE_BASELINE_CACHE_DIR" 2>/dev/null || true
}
declare_baseline_ruler

# THE GOAL CARD IS PART OF THE ARM, not something an operator has to remember to export.
#
# `run_probe.sh` builds its card with `--deliver --one-card --enforce-rules`. This driver passed
# NOTHING but `${MAKE_TASK_ARGS:-}`, and the default of that is empty -- measured on af13b4dd,
# `--enforce-rules` appears once in `run_probe.sh` and zero times here. Two consequences, both
# measured 2026-08-30 by building both cards over one synthetic checkout:
#
#   * the goal was 5,010 characters against the probe's 10,111 -- no YOUR OUTPUT IS THE FILE, no
#     ONE HYPOTHESIS, no rules and no solution space -- so a campaign number and a probe number are
#     answers to two different questions;
#   * `--enforce-rules` also rides into the `eval_train` and `score` commands, and without it
#     nothing runs AlgoTune's OWN validator over the candidate. Arm A cannot even WRITE a solver
#     that violates those rules -- `editor_functions.py` refuses the edit -- so arm B could submit
#     one, score it, and win on a primitive the other arm is physically unable to use. That is not
#     a comparison, and it is invisible in the result.
#
# It is a SEPARATE variable from `MAKE_TASK_ARGS` on purpose. That one is documented as carrying
# goal VARIANTS and is appended after this; folding the base card into its default would mean an
# operator who exports `MAKE_TASK_ARGS=--role-split` silently loses all three flags -- the same
# class of defect, arriving through the fix for it.
CARD_ARGS="${CARD_ARGS:---deliver --one-card --enforce-rules}"


mkdir -p "$OUT" "$WS"
# `.refused` is THIS invocation's tally of task-arms that never started, so a fixed-and-re-run
# arm must not inherit the last one's. Only the tally is cleared -- never a `.done` marker, which
# is the durable record of a task-arm that has already been measured.
rm -f "$OUT/$ARM"-*.refused

reap_orphan_workers() {
  # `pkill -f <name>` does not reach a multiprocessing forkserver: its command line carries neither
  # the app name nor the script name, which is how ten of them once survived a series of restarts
  # and burned CPU on the very cores a run was pinned to. Reap by module path, and ONLY orphans, so
  # a live lane's own workers are never touched.
  for P in $(pgrep -f "multiprocessing.fork[s]erver" 2>/dev/null); do
    if [ "$(ps -o ppid= -p "$P" 2>/dev/null | tr -d ' ')" = "1" ]; then kill -9 "$P" 2>/dev/null; fi
  done
}

# THE EVIDENCE THAT A TASK-ARM ACTUALLY STARTED, which is the one thing exit 2 cannot tell you.
#
# `rc=2` is `cli/__init__.py::REFUSAL_EXIT_CODE` and it is worn by EVERY `OperatorRefusal`, not by
# the spend ceiling this branch was written for. That family only grows: `BudgetExceeded` joined it
# on 2026-08-20, every new `ConfigRefusal` raise site joins it, and it already contains `LLMError`,
# whose ~35 raise sites cover an unreachable base URL, a half-set credential pair and a throttled
# key. None of those is a verdict about a task -- they are the run declining to begin, and they all
# arrive in about a second.
#
# So the whole arm can fail in one shape: every task exits 2 within seconds, 20 `.done` markers are
# written, this driver prints COMPLETE, the results table is all nulls, and a resume SKIPS all 20
# because the markers exist. A full-looking table of nothing is the worst output a benchmark harness
# has, and it needs one bad environment variable.
#
# The two states ARE distinguishable, from the run's own artifacts rather than from its exit code.
# Measured on this box 2026-08-22 and against the preserved arm-B corpus under camp-runs/:
#   refused to start - the run dir holds `engine.lock`, zero bytes, and NOTHING else; there is no
#                      `events.jsonl` at all, because the refusal happens before the engine opens
#                      one. Reproduced identically for three separate exit-2 refusals (a half-set
#                      credential pair, an unreachable endpoint, the reasoning-depth clash), each
#                      1-2 s of wall.
#   ran and stopped  - `events.jsonl` exists and carries `run_started` plus the `llm_usage` rows the
#                      cost accountant wrote. camp-runs/convex_hull: 24 kB, 17 `llm_usage` rows,
#                      marker `wall=136 rc=2`. All 20 preserved arm-B markers are rc=2 and every one
#                      of them has such a log.
# The event log is the discriminator, NOT the wall clock (a threshold between 2 s and 136 s is a
# guess that a slow endpoint invalidates) and NOT the exit code (which cannot separate them, by
# construction -- that is the defect).
ended_on_failure() {   # $1 = arm, $2 = task, $3 = attempt, $4 = start epoch (0 = no window).
  # DID THE RUN END ON ITS OWN TERMS? `successful_calls` below asks only whether the run ever paid
  # for anything, and that catches a total outage (attempt 1 of arm A: sixteen task-arms, zero calls
  # each). It does NOT catch the other half, measured 2026-08-25 when the gateway fell a second
  # time: four task-arms that HAD spent money were cut mid-search and still exited 0, so they earned
  # `ran_to_completion` markers over runs that had used 15 %, 27 %, 37 % and 69 % of their $1.00 —
  # against arm B, which spent the whole of it. The numbers those runs report are real and are
  # measurements of a TRUNCATED search, which is the one thing a marker must not hide.
  #
  # The discriminator is the LAST metered row of the attempt. A run that ends on its own terms ends
  # after a call that worked; a run the endpoint killed ends after one that did not. Checked against
  # the live log: the one task-arm that reached its ceiling (`edge_expansion`, 107 % spent) has a
  # 200 last, and all four cut ones have a 503.
  #
  # AND THE WINDOW IS PART OF THE QUESTION, because `(arm, task, attempt)` is not enough to name
  # ONE RUN. Two ways it fails, and they compose:
  #   * a row whose `attempt` key is absent or empty matches EVERY attempt -- deliberately, since
  #     rows written before 2026-08-23 carry no such key and `/m/<arm>/<task>/v1` is still accepted;
  #   * the attempt LEDGER lives in `$OUT` while the meter log does not, so a fresh `CAMPAIGN_OUT`
  #     restarts numbering at `a1` over a log that already holds an `a1`.
  # Reproduced 2026-08-30 by driving these functions over a meter log holding two three-day-old
  # untagged 200s: a run that made NO calls at all was told `ok_calls=2`, `ended_on_failure=no`,
  # and earned `state=ran_to_completion`. The rung written to catch a total endpoint outage failed
  # OPEN on exactly the evidence it exists to weigh.
  #
  # `record_done` already knows when this attempt STARTED, and `meter/proxy.py` stamps `ts` when it
  # WRITES the row -- after the call returned -- so every row belonging to this attempt has
  # `ts >= start`. That is a fact about the two clocks, not a heuristic about sessions, and it
  # closes both holes at once. A log whose rows carry no `ts` AT ALL cannot be windowed, so it
  # answers "" (unknowable) rather than 0: "" and "0" are different answers here and only "0"
  # refuses a marker.
  [ -n "${METER_LOG:-}" ] && [ -s "${METER_LOG:-/nonexistent}" ] || { echo ""; return 0; }
  grep -q "\"arm\": \"$1\"" "$METER_LOG" 2>/dev/null || { echo ""; return 0; }
  python3 - "$METER_LOG" "$1" "$2" "${3:-}" "${4:-0}" <<'PYEOF'
import json, sys
log, arm, task, attempt = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
since = float(sys.argv[5] or 0)
last, seen, timestamped = None, 0, 0
with open(log, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("arm") != arm or d.get("task") != task:
            continue
        seen += 1
        if attempt and d.get("attempt") not in ("", None, attempt):
            continue
        if since:
            try:
                ts = float(d["ts"])
            except (KeyError, TypeError, ValueError):
                continue            # no clock on the row: it cannot be shown to be this attempt's
            timestamped += 1
            if ts < since:
                continue
        last = d
if since and seen and not timestamped:
    print("")                       # a log with no clock at all: unwindowable, so unknowable
elif last is None:
    print("")                       # no rows for this attempt: unknowable, not a verdict
else:
    ok = str(last.get("status")) == "200" and not last.get("error")
    print("no" if ok else "yes")
PYEOF
}

successful_calls() {   # $1 = arm, $2 = task, $3 = attempt, $4 = start epoch (0 = no window).
  # "" and "0" are DIFFERENT ANSWERS and the caller only acts on "0": "" means the meter log is
  # missing, unreadable, or carries no rows for this arm, and refusing a marker on that would punish
  # a run for a bookkeeping gap it did not cause.
  [ -n "${METER_LOG:-}" ] && [ -s "${METER_LOG:-/nonexistent}" ] || { echo ""; return 0; }
  grep -q "\"arm\": \"$1\"" "$METER_LOG" 2>/dev/null || { echo ""; return 0; }
  python3 - "$METER_LOG" "$1" "$2" "${3:-}" "${4:-0}" <<'PYEOF'
import json, sys
log, arm, task, attempt = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
since = float(sys.argv[5] or 0)
n, seen, timestamped = 0, 0, 0
with open(log, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("arm") != arm or d.get("task") != task:
            continue
        seen += 1
        if attempt and d.get("attempt") not in ("", None, attempt):
            continue
        if since:
            try:
                ts = float(d["ts"])
            except (KeyError, TypeError, ValueError):
                continue            # no clock on the row: it cannot be shown to be this attempt's
            timestamped += 1
            if ts < since:
                continue
        if str(d.get("status")) == "200" and not d.get("error"):
            n += 1
if since and seen and not timestamped:
    print("")                       # a log with no clock at all: unwindowable, so unknowable
else:
    print(n)
PYEOF
}

run_started_evidence() {   # $1 = run dir ("" = no LoopLab run dir, i.e. arm A). echoes metered calls
  # PARSED, not grepped. `grep -c '"type":"llm_usage"'` counts LINES and is coupled to the
  # serializer twice over: it assumes compact orjson (no space after the colon) and it assumes one
  # event per line, which `eventstore.py::_EVENT_BATCH_TYPE` breaks by design -- a batched envelope
  # carries many events on one line and would be counted once. It also disagreed with the other
  # reader of this same fact (`compare_arms.py` matched `"llm_usage"` without the `"type":` prefix),
  # which is two spellings of one question.
  [ -n "${1:-}" ] && [ -s "$1/events.jsonl" ] || return 1
  python3 - "$1/events.jsonl" <<'PYEOF'
import json, sys
n = 0
with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        # a batch envelope carries its rows under `data.events`; a plain row IS the event
        inner = (row.get("data") or {}).get("events") if isinstance(row, dict) else None
        for ev in (inner if isinstance(inner, list) else [row]):
            if isinstance(ev, dict) and ev.get("type") == "llm_usage":
                n += 1
print(n)
PYEOF
}

# THE IDENTITY OF ONE ATTEMPT, ALLOCATED HERE AND NOWHERE ELSE.
#
# `(arm, task)` is not an identity, because a task-arm gets RE-RUN and every attempt used to land in
# the same meter bucket. Measured 2026-08-23 on `meter/meter.jsonl`: `B/kcenters` holds $2.0086 over
# 816 calls in four sessions against ONE `.done` marker whose run cost $1.0070 -- a naive per-task
# sum reads 2x the $1.00 ceiling and looks like a budget breach that never happened. `B/discrete_log`
# is $1.4749 over 526 calls; `B/count_riemann_zeta_zeros` $0.8386 over 127.
#
# WHY THE CAMPAIGN ALLOCATES IT AND NOT THE PROXY. The proxy sees a URL and nothing else: any id it
# invented (a start-up counter, a first-seen-at stamp) would renumber on every meter restart, and
# there would be nothing on the campaign's side to join it to. The id has to be minted by whoever
# also writes the `.done` marker, which is here, so `attempt=aN` in the marker and the `aN` in the
# path name the same thing and a reader can sum ONE attempt by equality -- no session-gap heuristic,
# no date arithmetic. (That heuristic is not merely inconvenient, it is undecidable: the same
# `count_riemann_zeta_zeros` rows split into 19 / 16 / 14 / 12 / 2 sessions at a 5 / 10 / 15 / 20 /
# 40-minute gap. Nothing in the log says which is right.)
#
# The ledger is APPEND-ONLY and one file per task-arm, so it survives the `rm -rf "$TASK_ROOT"` a
# re-run does and records attempts the marker no longer mentions. It is not locked: two lanes never
# hold the same task inside one invocation (one PID slot per lane, one task per slot), and two
# campaigns sharing one $CAMPAIGN_OUT would already be racing on the markers themselves.
next_attempt() {   # $1 = arm, $2 = task. Echoes this attempt's id AND records the allocation.
  LEDGER="$OUT/$1-$2.attempts"
  N=$(( $(wc -l < "$LEDGER" 2>/dev/null || echo 0) + 1 ))
  printf 'a%s started=%s epoch=%s\n' "$N" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(date +%s)" \
    >> "$LEDGER"
  echo "a$N"
}

# IS THIS TASK-ARM ALREADY MEASURED? The resume predicate, hoisted out of the two arm branches so
# there is ONE answer to it -- and so the wall-cut exception below is not written twice.
#
# A WALL CUT IS TERMINAL BY DEFAULT, AND THAT IS A DECISION, NOT AN OVERSIGHT.
# The argument for making it resumable is real: rc=124 is not a property of the INPUT the way the
# rc=2 spend refusal is. Measured 2026-08-23, five task-arms were cut at the 4 h wall and THREE had
# not spent the budget they are compared at -- A-convex_hull $0.70, A-count_riemann_zeta_zeros
# $0.139, B-max_weighted_independent_set $0.866 of $1.00. `A-count_riemann_zeta_zeros` ran out of
# clock because forty nginx 504s at 300 s each ate three and a half of its four hours: an
# environment condition that a re-run under a different transport would simply not meet.
#
# It stays terminal anyway, because THIS DRIVER CANNOT TELL THOSE APART from a task that genuinely
# needs more than four hours, and auto-retrying the second kind spends four hours and a dollar to
# reproduce the same cut, every resume, forever -- which is what "recorded so it is visible rather
# than retried forever" was protecting. A resume must be safe to run blind; that is the whole reason
# `.done` exists.
#
# So the retry is one flag away instead of zero: `RETRY_WALL_CUT=1` re-runs exactly the wall-cut
# task-arms and nothing else. An operator CAN tell the two apart -- from `state=wall_cut` in the
# marker plus the attempt's own metered spend -- and the alternative to a flag is deleting `.done`
# files by hand, which is how a marker over a real measurement gets destroyed. `PENDING_FIXES.md`
# item 4 (raise the wall once the transport fixes land) is exactly this operation.
# THE ONE SHELL SPELLING of "this harness stopped the run", mirroring
# `compare_arms.py::HARNESS_CUT_STATES`. It was written out twice (here and in `final_banner`) and a
# third state would have meant a third pair of edits; the copies coming apart is what makes a resume
# skip a task the banner reports as owed. `rc=124` alone is kept because markers written before the
# `state=` field existed carry only the integer.
marker_is_harness_cut() {   # $1 = marker text
  case "$1" in
    *state=wall_cut*|*state=stall_cut*|*rc=124*) return 0 ;;
  esac
  return 1
}

# THE ONE SHELL SPELLING of "this marker was written rather than earned", mirroring
# `compare_arms.py::marker_state`'s `state=operator_skip` branch.
#
# `already_measured` skips on ANY non-empty marker that is not a harness cut, and that is how a
# running campaign is told to stop taking new work without editing a file a live bash is reading
# incrementally. The mechanism is right; what it leaves behind is a marker indistinguishable from a
# completed run to every later reader. `compare_arms.py` learned that on 2026-08-26, when five
# CP-SAT task-arms were skipped by decision. This driver did not, and `final_banner` counted them
# among the finished: reproduced 2026-08-30 over a five-task directory holding two skips, the
# banner printed `===== arm B COMPLETE (5/5 markers) =====` and returned 0 over three measurements.
#
# DELIBERATELY NOT FOLDED INTO `marker_is_harness_cut`. That predicate also decides what
# `RETRY_WALL_CUT=1` reopens, and a skip is a DECISION rather than a clock -- reopening it would
# undo the operator's own instruction on the next resume, which is the opposite of the wall-cut
# argument. Two states, two predicates, one reader each.
marker_is_operator_skip() {   # $1 = marker text
  case "$1" in
    *state=operator_skip*) return 0 ;;
  esac
  return 1
}

already_measured() {   # $1 = marker path. Success = do NOT run this task-arm again.
  [ -s "$1" ] || return 1
  if marker_is_harness_cut "$(cat "$1")"; then
    [ "${RETRY_WALL_CUT:-0}" = "1" ] && return 1
  fi
  return 0
}

record_done() {   # $1 = marker path, $2 = exit code, $3 = start epoch, $4 = cpus, $5 = run dir
  RC=$2
  WALL=$(( $(date +%s) - $3 ))
  REGIME="cpus=$4 lanes=$LANE_COUNT cores_per_lane=$CORES_PER_LANE layout=$LANE_LAYOUT"
  # A `.done` marker means "this task-arm reached a TERMINAL state and must not be re-run". It must
  # NOT be written for a run that was interrupted: an interrupted task has no verdict, and a marker
  # makes a later resume SKIP it silently. Measured 2026-08-20: stopping a campaign wrote six
  # markers over live runs -- one of them 230 minutes in -- and the resume would have treated all
  # six as complete with no score.
  #   0        - the run ended on its own (a score, or the harness's own N/A)
  #   2        - a TYPED OPERATOR REFUSAL (`cli/__init__.py::REFUSAL_EXIT_CODE`). Terminal ONLY with
  #              the evidence above that the run STARTED: "this task-arm finished, having spent its
  #              budget" and "this task-arm refused to start" are the same exit code and opposite
  #              facts. With the evidence the old reasoning holds and the marker is right -- a
  #              refusal is a property of the INPUT, so the next attempt spends the same allowance
  #              and stops at the same wall (before `BudgetExceeded` wore the marker it exited 1 with
  #              a traceback and was recorded as "interrupted, still owed", i.e. a FINISHED task
  #              queued for a retry that could never do anything different). WITHOUT it there is no
  #              measurement to preserve and no allowance that was spent, so no marker is written.
  #   124      - the wall-clock net fired; terminal (see `already_measured` for why, and for the
  #              one flag that reopens it), and it CAN still carry a number -- see below.
  #   130/137/143 and anything else - interrupted. NO marker; the task is still owed.
  #
  # EVERY MARKER NOW NAMES ITS STATE IN WORDS, and `rc=` stays beside it. A reader that has to
  # recover "the clock killed this" from an integer gets it wrong: `compare_arms.py` learned to
  # match the substring `rc=124`, and nothing else did -- `final_banner` counted a wall cut into
  # `COMPLETE` and `campaign_status.py` printed it as a finished task. The vocabulary is closed:
  #   state=ran_to_completion   rc=0,   the run ended on its own terms
  #   state=stopped_after_start rc=2,   a typed OperatorRefusal from a run that HAD started
  #   state=wall_cut            rc=124, `timeout` sent SIGTERM at HARD_TIMEOUT
  # `attempt=` joins the marker to the meter rows this attempt wrote (`next_attempt`); a marker
  # written outside `run_one` -- i.e. by a test driving this function -- says `attempt=none`.
  #
  # "IT PRODUCES NO NUMBER (see docs/51)" WAS FALSE FOR ARM B, and the comment used to say it for
  # both arms. What docs/51 item 4 measures is arm A: a cut AlgoTuner run writes no `final_speedup`
  # into `reports/agent_summary.json` at all, and the live corpus agrees -- neither wall-cut arm-A
  # task has an entry there. But arm B's champion extraction and TEST scoring run in `run_one`
  # AFTER `timeout` has killed the run, so a cut arm-B task-arm scores whatever fold it had reached:
  # measured 2026-08-23, `B-pde_heat1d.final.json` = 3.1223, `B-sparse_eigenvectors_complex` =
  # 1.0045, `B-max_weighted_independent_set` = 1.0393, each from a clean un-cut eval pass (47 s,
  # 44 s, 350 s).
  #
  # THE COMMENT IS CORRECTED AND THE BEHAVIOUR IS KEPT. Suppressing that number would destroy a real
  # measurement -- the champion is the fold's own, and the eval pass that scored it was not cut --
  # and it would blind the one reader that needs it: `compare_arms.py` PRINTS a `wall_cut` row and
  # keeps it out of the means precisely so an operator can see the wall binding at all. What was
  # wrong was never the number, only the claim that it is comparable, and that claim is already
  # refused downstream. So: a wall-cut arm-B row is a number from a TRUNCATED search, reported and
  # never averaged; a wall-cut arm-A row has no number at all.
  case "$RC" in
    0)
      # `rc=0` IS NOT ENOUGH TO CALL A RUN COMPLETE. Measured 2026-08-25: the gateway's model group
      # went to `503 No available workers (all circuits open or unhealthy)` mid-campaign, and arm A's
      # remaining sixteen task-arms each exited 0 after THREE TO NINETEEN SECONDS having made no
      # successful call at all. Every one was marked `ran_to_completion`, `final_banner` counted
      # 20/20 and the driver printed "FINAL CAMPAIGN COMPLETE". A total outage of the endpoint is
      # indistinguishable, in the markers, from a campaign that worked — and a marker means "do not
      # re-run this", so a resume would have skipped all sixteen for ever.
      #
      # So a run that paid for nothing gets NO MARKER and stays owed, exactly like an interruption.
      # The check is positive-evidence only: it needs the meter log to be readable AND to hold rows
      # for this arm, so a missing or untagged log leaves the old behaviour rather than refusing
      # markers for runs that were fine.
      OK_CALLS="$(successful_calls "$ARM" "$T" "${ATTEMPT:-}" "$3")"
      if [ "$(ended_on_failure "$ARM" "$T" "${ATTEMPT:-}" "$3")" = "yes" ]; then
        echo "  [$(date +%H:%M:%S)][$4] ENDED ON A FAILED CALL after ${WALL}s (rc=0, ok_calls=${OK_CALLS:-?})" \
             "-- the endpoint cut this run, it did not finish. No marker written, task still owed"
        return 0
      fi
      if [ "$OK_CALLS" = "0" ]; then
        echo "  [$(date +%H:%M:%S)][$4] NO SUCCESSFUL CALLS in ${WALL}s (rc=0) -- endpoint down?" \
             "no marker written, task still owed"
        return 0
      fi
      echo "wall=$WALL rc=0 state=ran_to_completion $REGIME ok_calls=$OK_CALLS attempt=${ATTEMPT:-none}" > "$1" ;;
    124)
      echo "wall=$WALL rc=124 state=wall_cut $REGIME attempt=${ATTEMPT:-none}" > "$1" ;;
    2)
      if METERED="$(run_started_evidence "${5:-}")"; then
        # `metered` is recorded because a started run that paid for nothing is still worth seeing in
        # the marker; it is not what decides the marker.
        echo "wall=$WALL rc=$RC state=stopped_after_start $REGIME metered=$METERED attempt=${ATTEMPT:-none}" > "$1"
      else
        refuse_to_start "$1" "$WALL" "$4" "${5:-}"
      fi ;;
    *)
      # A STALL KILL IS NOT AN INTERRUPTION. Both arrive as rc=143 and used to be filed together as
      # "still owed", so a task that hangs structurally was re-run from scratch on every resume for
      # ever, spending a fresh full budget each time to stall at the same place. The breadcrumb
      # `run_bounded` drops is the only thing that can tell them apart -- an exit code cannot, by
      # construction. It is TERMINAL (a `.done`, so a resume stops re-running it) but it is NOT a
      # finish: `state=stall_cut` is read exactly like `wall_cut` downstream -- printed, never
      # averaged -- because the run was stopped by the harness rather than on its own terms.
      if [ -n "${STALL_FLAG:-}" ] && [ -f "${STALL_FLAG:-/nonexistent}" ]; then
        rm -f "$STALL_FLAG"
        echo "wall=$WALL rc=$RC state=stall_cut $REGIME attempt=${ATTEMPT:-none}" > "$1"
        echo "  [$(date +%H:%M:%S)][$4] STALL-CUT after ${WALL}s of silence -- recorded, not averaged"
      else
        echo "  [$(date +%H:%M:%S)][$4] interrupted (rc=$RC) -- no marker written, task still owed"
      fi ;;
  esac
}

refuse_to_start() {   # $1 = marker path (NOT written), $2 = wall, $3 = cpus, $4 = run dir
  # A `.refused` file rather than a variable: every task runs in a backgrounded subshell, so a
  # counter would be incremented in a child and lost. This is the tally `final_banner` reads, and it
  # is deliberately NOT a `.done` -- the resume check keys on `.done`, and this task is still owed.
  echo "wall=$2 rc=2 cpus=$3 evidence=none" > "${1%.done}.refused"
  if [ -n "$4" ]; then
    WHERE="no event log at $4/events.jsonl -- the run refused before the engine opened one"
  else
    # Arm A never runs LoopLab, so it has no event log to check AND no claim on exit 2: the branch
    # above is justified entirely by `cli/__init__.py::REFUSAL_EXIT_CODE`, which AlgoTuner does not
    # implement. Its exit 2 means whatever AlgoTuner means by it, which is not "this task is done".
    # All 20 preserved arm-A markers are rc=0, so this has never fired on a real arm A.
    WHERE="arm A runs no LoopLab engine, so exit 2 carries none of this campaign's meaning"
  fi
  echo "  [$(date +%H:%M:%S)][$3] REFUSED TO START after ${2}s: $WHERE." >&2
  echo "      NO .done marker written. This task-arm measured NOTHING and is still owed;" >&2
  echo "      the cause is in the last lines of ${1%.done}.log." >&2
}

final_banner() {   # $1 = out dir, $2 = arm, $3 = task count, $4 = task list. 3 if anything is short.
  # The banner is a FUNCTION so it can be driven by tests/test_campaign_marker_evidence.py: the
  # property that matters is "the driver does not say COMPLETE over an arm that never ran", and the
  # only honest way to check that is to run it over a directory that holds a refusal.
  REFUSED_N="$(ls "$1/$2"-*.refused 2>/dev/null | wc -l)"
  DONE_N="$(ls "$1/$2"-*.done 2>/dev/null | wc -l)"

  # A MISSING MARKER IS NOT A REFUSAL, AND UNTIL 2026-08-23 ONLY REFUSALS COULD STOP THIS BANNER.
  # `record_done` writes NOTHING for rc 130/137/143 -- "interrupted; the task is still owed" -- and
  # writes no `.refused` either, by design: a `.refused` is specifically an exit-2-with-no-event-log.
  # So the whole interrupted family fell through both counters and the arm was declared COMPLETE.
  # Reproduced on this box 2026-08-23 with a one-task arm whose task exited 1: the driver printed
  # `===== arm A COMPLETE (0/1 markers) =====` and exited 0, with the marker count printed inside its
  # own success banner. And it is not hypothetical at 20 tasks: `campaign-paired/` currently holds 17
  # `.done` against 20 `B-*.log`, zero `.refused`, and pagerank + rbf_interpolation have a full
  # `.final.json` each and no marker at all.
  #
  # This driver's own header says exit 0 means "every task-arm reached a terminal state", so the
  # marker count IS the predicate and it is now checked against the task count.
  MISSING=""
  for T in ${4:-}; do
    [ -s "$1/$2-$T.done" ] || MISSING="$MISSING $T"
  done

  # A WALL CUT HAS A MARKER AND IS NOT A RESULT AT THE BUDGET. It counts into DONE_N -- it really is
  # terminal, and `already_measured` will not re-run it -- but a banner that only prints a count
  # hides the fact that the wall is binding at all. Measured 2026-08-23: five of the campaign's
  # task-arms were cut at 4 h and three of them had not spent the ceiling they are compared at, one
  # at $0.14 of $1.00. That is the single thing an operator has to see to decide whether to raise
  # HARD_TIMEOUT, and it was in no banner.
  WALL_CUT=""
  for M in "$1/$2"-*.done; do
    [ -s "$M" ] || continue
    marker_is_harness_cut "$(cat "$M")" && WALL_CUT="$WALL_CUT $(basename "${M%.done}")"
  done
  if [ -n "$WALL_CUT" ]; then
    echo "[$(date +%H:%M:%S)] STOPPED BY THE HARNESS (wall cut or stall cut) --$WALL_CUT"
    echo "  These reached a TERMINAL state and hold a .done marker, so a resume will not retry them."
    echo "  They were stopped by HARD_TIMEOUT=${HARD_TIMEOUT:-?} s, NOT by the budget every other row is"
    echo "  compared at, so they are not measurements at that ceiling. compare_arms.py prints them"
    echo "  and leaves them out of the means; arm B may still carry a score (the champion is"
    echo "  extracted and scored after the kill), arm A writes no final_speedup at all."
    echo "  RETRY_WALL_CUT=1 re-runs exactly these; raise HARD_TIMEOUT first or expect the same cut."
  fi
  if [ "$REFUSED_N" -eq 0 ] && [ -n "$MISSING" ]; then
    # "UNFINISHED", never "INCOMPLETE": a watcher greps this log for `arm $ARM COMPLETE` and
    # "INCOMPLETE" CONTAINS it -- the same trap the refusal banner below was already written around.
    echo "[$(date +%H:%M:%S)] ===== arm $2 UNFINISHED: $DONE_N of $3 task-arms have a .done marker ====="
    echo "  no marker, so still owed:$MISSING"
    echo "  record_done writes no marker for an interrupted task-arm (rc 130/137/143 and anything"
    echo "  it does not recognise) -- these have no verdict, and any B-<task>.final.json left behind"
    echo "  for them was written BEFORE that was decided. compare_arms.py reads these same markers"
    echo "  and will report those rows as owed rather than as scores."
    echo "  Re-run this arm: no marker was written, so exactly these are retried."
    return 3
  fi
  if [ "$REFUSED_N" -gt 0 ]; then
    # "NOT MEASURED", not "INCOMPLETE": a watcher greps this log for `arm $ARM COMPLETE`, and
    # "INCOMPLETE" CONTAINS that string -- the failure banner would have matched the success one.
    echo "[$(date +%H:%M:%S)] ===== arm $2 NOT MEASURED: $REFUSED_N of $3 task-arms REFUSED TO START ====="
    for R in "$1/$2"-*.refused; do
      echo "    $(basename "${R%.refused}"): $(cat "$R")"
    done
    echo "  These exited 2 without starting a run. Exit 2 is REFUSAL_EXIT_CODE, worn by every"
    echo "  OperatorRefusal -- an unreachable endpoint, a bad credential pair or a refused setting"
    echo "  all land here, and none of them is a measurement. $DONE_N of $3 task-arms have a marker."
    echo "  Fix the cause and re-run this arm: no marker was written, so exactly these are retried."
    echo "  Do NOT summarise this arm -- the numbers below it would be a table of nothing."
    return 3
  fi
  # A SKIPPED TASK-ARM IS TERMINAL AND IS NOT A MEASUREMENT. Same shape as the wall-cut block
  # above and for the same reason: it really is terminal, `already_measured` will not re-run it,
  # and a banner that only prints a marker count hides that nothing measured it. See
  # `marker_is_operator_skip` for the measurement that forced this.
  SKIPPED=""
  SKIPPED_N=0
  for M in "$1/$2"-*.done; do
    [ -s "$M" ] || continue
    if marker_is_operator_skip "$(cat "$M")"; then
      SKIPPED="$SKIPPED $(basename "${M%.done}")"
      SKIPPED_N=$((SKIPPED_N + 1))
    fi
  done
  if [ "$SKIPPED_N" -gt 0 ]; then
    echo "[$(date +%H:%M:%S)] SKIPPED BY THE OPERATOR --$SKIPPED"
    echo "  These carry a .done marker that was WRITTEN rather than earned, so a resume will not"
    echo "  run them and nothing measured them. compare_arms.py reads these same markers, prints"
    echo "  them as SKIPPED and leaves those pairs out of the means. Delete a marker to queue that"
    echo "  task-arm again; RETRY_WALL_CUT does NOT reopen a skip, because a skip is a decision."
    echo "[$(date +%H:%M:%S)] ===== arm $2 COMPLETE ($DONE_N/$3 markers;" \
         "$((DONE_N - SKIPPED_N)) MEASURED, $SKIPPED_N SKIPPED) ====="
    return 0
  fi
  echo "[$(date +%H:%M:%S)] ===== arm $2 COMPLETE ($DONE_N/$3 markers) ====="
  return 0
}

run_one() {                       # $1 = task, $2 = cpu list
  T=$1; CPUS=$2
  MARKER="$OUT/$ARM-$T.done"
  # Per task-arm, so two lanes cannot read each other's breadcrumb. `run_bounded` clears it on
  # entry and writes it only when the stall guard itself does the killing.
  STALL_FLAG="$OUT/.$ARM-$T.stalled"
  # THE RESUME CHECK IS ASKED FIRST, and once. It used to sit inside each arm branch, BELOW the
  # meter block -- so a skipped task-arm still ran the export. That was harmless while the path held
  # no per-attempt state; it is not harmless now, because `next_attempt` allocates an id and a
  # resume that skips 17 of 20 tasks would burn 17 attempt numbers on runs that never happened.
  if already_measured "$MARKER"; then echo "[$CPUS] $T arm $ARM already done"; return; fi
  # Allocated whether or not the meter is on: the attempt id is this driver's own name for this
  # run of this task-arm, it goes into the marker either way, and a marker whose `attempt=` means
  # something different depending on METER_BASE would be worse than one that has no attempt at all.
  ATTEMPT="$(next_attempt "$ARM" "$T")"
  if [ -n "$METER_BASE" ]; then
    # Both arms, same meter, one path segment apart. Arm A reaches it through OPENAI_BASE_URL,
    # which litellm honours for an `openai/<model>` entry that carries no api_base of its own.
    #
    # THE THIRD SEGMENT IS THE ATTEMPT (see `next_attempt` for the measurement that forced it). The
    # proxy still accepts the two-segment `/m/<arm>/<task>/v1` -- `docs/52` and any hand-built curl
    # spell it, and a metered call must never be refused for arriving on the old shape -- and
    # records it with an empty `attempt`. Rows in a log written before 2026-08-23 carry no `attempt`
    # KEY at all, which is a third, distinguishable state; `meter/proxy.py`'s docstring says how to
    # read such a log, and the short answer is per `(arm, task)` and labelled "all attempts".
    export LOOPLAB_LLM_BASE_URL="$METER_BASE/m/$ARM/$T/$ATTEMPT/v1"
    export LOOPLAB_LLM_API_KEY_BASE_URL="$LOOPLAB_LLM_BASE_URL"
    export OPENAI_BASE_URL="$LOOPLAB_LLM_BASE_URL"
    export OPENAI_API_KEY="${LOOPLAB_LLM_API_KEY:-meter}"
  fi
  if [ "$ARM" = "A" ]; then
    S=$(date +%s)
    # Arm A keeps no event log, so its OWN lane log is the liveness signal — it is this arm's
    # stdout and it grows on every message, every edit and every evaluation. An empty watch path
    # would have left this arm with no bound at all now that the wall is off, which is worse than
    # the wall was: one hung lane and the campaign never finishes.
    : > "$OUT/A-$T.log"
    run_bounded "$OUT/A-$T.log" taskset -c "$CPUS" ./algotune.sh agent --standalone \
        "$ALGOTUNE_MODEL_KEY" "$T" > "$OUT/A-$T.log" 2>&1
    RC=$?
    record_done "$MARKER" "$RC" "$S" "$CPUS" ""
    [ -s "$MARKER" ] && echo "[$(date +%H:%M:%S)][$CPUS] $T arm A done ($(cat "$MARKER"))"
  else
    TASK_ROOT="$RUNS_ROOT/$T"
    rm -rf "$TASK_ROOT"; mkdir -p "$TASK_ROOT/memory" "$TASK_ROOT/knowledge"
    # MAKE_TASK_ARGS carries goal VARIANTS (e.g. --role-split). A pass-through rather than a knob
    # per variant: the goal is this experiment's independent variable, and every value of it has
    # to be readable off the `task.snapshot.json` the run preserves for itself.
    # shellcheck disable=SC2086
    python "$REPO/benchmarks/algotune/make_task.py" --algotune-root "$AT" --task "$T" \
        --out-dir "$WS" $CARD_ARGS ${MAKE_TASK_ARGS:-} >/dev/null 2>&1
    S=$(date +%s)
    # Per-task memory and knowledge dirs: LoopLab can mine its own past runs and a shared store,
    # and AlgoTuner has no equivalent -- left shared, arm B would reach task 12 with eleven prior
    # runs to read, measuring a capability the other arm does not have rather than the loop.
    LOOPLAB_MEMORY_DIR="$TASK_ROOT/memory" LOOPLAB_KNOWLEDGE_DIR="$TASK_ROOT/knowledge" \
      run_bounded "$TASK_ROOT/run/events.jsonl" taskset -c "$CPUS" python -m looplab.cli run \
        "$WS/algotune_$T.json" --out "$TASK_ROOT/run" --backend llm --max-nodes 20 \
        > "$OUT/B-$T.log" 2>&1
    RC=$?   # captured HERE: the champion extraction and the test scoring below both clobber $?
    # Champion from the FOLD, then ONE scoring pass on TEST: every node above ran on TRAIN, which
    # is what AlgoTuner's own agent does. Without this the arm optimises against its graded split.
    if [ "$RC" = 2 ] && ! run_started_evidence "$TASK_ROOT/run" >/dev/null; then
      # The SAME predicate `record_done` uses four lines down, applied to the results row. A run that
      # refused to start has no fold, so there is no champion to extract -- and the usual
      # null-speedup row would enter compare_arms' table looking exactly like a task that ran hard
      # and found nothing. `compare_arms.py` reads only `speedup`, so the sentence is for the human.
      echo '{"speedup": null, "error": "the LoopLab run refused to start (exit 2, no event log) -- not a measurement"}' \
        > "$OUT/B-$T.final.json"
    elif python "$REPO/benchmarks/algotune/extract_champion.py" --run-dir "$TASK_ROOT/run" \
           --all-files --out "$TASK_ROOT/champion/solver.py" >> "$OUT/B-$T.log" 2>&1; then
      # ITS OWN WALL, not $HARD_TIMEOUT. That default became 0 ("no timeout" to GNU
      # coreutils) when the wall was replaced by the stall guard, and this call was never
      # migrated to `run_bounded` -- so the graded TEST pass had no bound of any kind. A
      # wedge outside `looplab_eval`'s own inner `subprocess.run(timeout=…)` (the build_ext
      # child holding a lock, a geesefs stat, an evaluator child that ignores SIGTERM) held
      # the lane for ever and the driver's final `wait` never returned. It does NOT go
      # through `run_bounded`: a scoring pass is legitimately silent for long stretches
      # (the README measures a single-candidate evaluation at 87 minutes), so a stall guard
      # would kill healthy work. A generous wall is the right instrument here.
      # `--protect` carries the TASK's own declaration rather than a second copy of it, and the
      # champion is scored out of its OWN directory so its sibling files are the submission (see
      # `extract_champion.py --all-files`). Scoring it from $TASK_ROOT made `src.parent` a directory
      # whose other entries are directories, so a multi-file champion submitted nothing, built
      # nothing, and failed to import -- recorded as the solver's own 0.0.
      CHAMPION_PROTECT="$(python3 - "$WS/algotune_$T.json" <<'PROTEOF'
import json, sys
try:
    spec = json.loads(open(sys.argv[1], encoding="utf-8").read())
except Exception:
    raise SystemExit(0)
print(",".join(str(x) for x in (spec.get("protect") or []) if x))
PROTEOF
)"
      (cd "$TASK_ROOT/champion" && timeout "$CHAMPION_TIMEOUT" taskset -c "$CPUS" \
          python "$REPO/benchmarks/algotune/looplab_eval.py" --algotune-root "$AT" --task "$T" \
          --model LoopLabFinal --solver solver.py --subset test \
          ${CHAMPION_PROTECT:+--protect "$CHAMPION_PROTECT"}) \
          > "$OUT/B-$T.final.json" 2>>"$OUT/B-$T.log"
    else
      # EXIT 1 AND EXIT 2 ARE DIFFERENT ANSWERS, and this used to be one `else`. The extractor was
      # rewritten to separate them and BOTH its callers threw the distinction away: `if cmd; then`
      # cannot tell them apart, so a broken bridge was recorded as a run that found nothing.
      #   1 - the FOLD says there is no champion: no event log, no best node, no `solver.py` in the
      #       committed set. That is a fact about the RUN and a legitimate null.
      #   2 - the log could not be READ or `looplab` could not be IMPORTED. That is a fact about
      #       THIS HARNESS and says nothing whatever about the run -- the scores are still in
      #       `events.jsonl` and the champion can be re-extracted without spending the budget
      #       again. Measured 2026-08-31 on the `accEE` probe, whose summary read "champion: NONE"
      #       while its own log held 27.466 and 221.5387.
      CHRC=$?
      if [ "$CHRC" = 2 ]; then
        echo '{"speedup": null, "harness_failure": "extract_champion_rc2",' \
             '"error": "champion extraction FAILED (exit 2): a broken bridge, NOT a run without a' \
             'champion. The scores are in the run event log and can be re-extracted."} ' \
          > "$OUT/B-$T.final.json"
        echo "  [$(date +%H:%M:%S)][$CPUS] $T: BROKEN BRIDGE -- extract_champion exited 2, so this" \
             "row is a harness failure and NOT a null result. Re-extract and re-score without"
        echo "      re-running the task:  python $REPO/benchmarks/algotune/extract_champion.py" \
             "--run-dir $TASK_ROOT/run --all-files --out $TASK_ROOT/champion/solver.py"
      else
        echo '{"speedup": null, "error": "no champion to score"}' > "$OUT/B-$T.final.json"
      fi
    fi
    record_done "$MARKER" "$RC" "$S" "$CPUS" "$TASK_ROOT/run"
    [ -s "$MARKER" ] && echo "[$(date +%H:%M:%S)][$CPUS] $T arm B done ($(cat "$MARKER"))"
  fi
}

echo "arm $ARM | $NTASKS tasks | $LANE_COUNT lanes x $CORES_PER_LANE cores from core $CORE_OFFSET (of $NPROC) | budget \$$BUDGET_USD"
echo "model $ALGOTUNE_MODEL_KEY | llm ${METER_BASE:-$LOOPLAB_LLM_BASE_URL}${METER_BASE:+ (metered, per-attempt paths)}"
# The RULER, in the log, beside the model. A speedup is a ratio and this names the instrument
# that measured its denominator; every number below is only comparable to numbers from the
# same two values.
echo "baseline cache $ALGOTUNE_BASELINE_CACHE_DIR | eval workers $ALGOTUNE_EVAL_WORKERS x $ALGOTUNE_EVAL_CORES_PER_WORKER core(s)"
echo "card $CARD_ARGS ${MAKE_TASK_ARGS:-}"
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
final_banner "$OUT" "$ARM" "$NTASKS" "$TASKS"
CAMPAIGN_RC=$?

# The runtime disk does not survive a container restart and an arm is hours of measurement that
# cannot be recomputed. Snapshot at the one moment we know the data just changed. SNAPSHOT=0 skips.
if [ "${SNAPSHOT:-1}" = "1" ] && [ -x "$REPO/benchmarks/snapshot.sh" ]; then
  "$REPO/benchmarks/snapshot.sh" || echo "  (snapshot failed; the measurements are still on disk)"
fi
echo "summarise with:  python $REPO/benchmarks/algotune/compare_arms.py \\"
echo "    --algotune-root $AT --runs-root $RUNS_ROOT --final-dir $OUT --reference"

# Exit 3 = the arm did not measure everything it was asked to (see `final_banner`). It is a
# distinct code on purpose: 0 would let a wrapper summarise a table of nothing, and the 2 this
# script already uses is its own pre-flight refusal, raised before a single task ran.
exit "$CAMPAIGN_RC"
