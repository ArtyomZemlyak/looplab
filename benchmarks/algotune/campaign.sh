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
HARD_TIMEOUT="${HARD_TIMEOUT:-14400}"
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
phys_per_lane = max(1, per_lane // 2)          # a lane owns whole cores; 22 logical = 11 physical
pairs = []
seen = set()
for cpu in range(4096):
    try:
        with open(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list") as fh:
            sibs = tuple(sorted(int(x) for x in fh.read().strip().split(",")))
    except OSError:
        break
    if sibs not in seen:
        seen.add(sibs)
        pairs.append(sibs)
need = offset + lanes * phys_per_lane
if need > len(pairs):                          # not enough physical cores: fall back and SAY SO
    print("FALLBACK", file=sys.stderr)
    for lane in range(lanes):
        lo = offset + lane * per_lane
        print(f"{lo}-{lo + per_lane - 1}")
    raise SystemExit(0)
for lane in range(lanes):
    chunk = pairs[offset + lane * phys_per_lane: offset + (lane + 1) * phys_per_lane]
    print(",".join(str(c) for pair in chunk for c in sorted(pair)))
PYEOF
)"
_L=0
while IFS= read -r _line; do
  [ -n "$_line" ] || continue
  LANE_CPUS[$_L]="$_line"
  _L=$((_L + 1))
done <<< "$_LANE_PLAN"
[ "$_L" = "$LANE_COUNT" ] || { echo "lane plan produced $_L ranges for $LANE_COUNT lanes"; exit 2; }

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
  # generic OpenAI endpoint. AlgoTuner picks the litellm model name at `AlgoTuner/main.py:270`,
  # `llm_model_name = model_info.get("model_name", desired_model_name)` -- so an entry with NO
  # `model_name` is named by its own config KEY. For an `openrouter/...` key litellm resolves the
  # base as `api_base or litellm.api_base or OPENROUTER_API_BASE or "https://openrouter.ai/api/v1"`
  # (`litellm/main.py:3250`) and OPENAI_BASE_URL appears nowhere in that chain.
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
# AlgoTuner/main.py:270 -- the same resolution, re-derived rather than assumed.
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
run_started_evidence() {   # $1 = run dir ("" = no LoopLab run dir, i.e. arm A). echoes metered calls
  [ -n "${1:-}" ] && [ -s "$1/events.jsonl" ] || return 1
  N="$(grep -c '"type":"llm_usage"' "$1/events.jsonl" 2>/dev/null)"
  echo "${N:-0}"
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
already_measured() {   # $1 = marker path. Success = do NOT run this task-arm again.
  [ -s "$1" ] || return 1
  case "$(cat "$1")" in
    *state=wall_cut*|*rc=124*)      # `rc=124` alone: markers written before `state=` existed
      [ "${RETRY_WALL_CUT:-0}" = "1" ] && return 1 ;;
  esac
  return 0
}

record_done() {   # $1 = marker path, $2 = exit code, $3 = start epoch, $4 = cpus, $5 = run dir
  RC=$2
  WALL=$(( $(date +%s) - $3 ))
  REGIME="cpus=$4 lanes=$LANE_COUNT cores_per_lane=$CORES_PER_LANE"
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
      echo "wall=$WALL rc=0 state=ran_to_completion $REGIME attempt=${ATTEMPT:-none}" > "$1" ;;
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
      echo "  [$(date +%H:%M:%S)][$4] interrupted (rc=$RC) -- no marker written, task still owed" ;;
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
    case "$(cat "$M")" in
      *state=wall_cut*|*rc=124*) WALL_CUT="$WALL_CUT $(basename "${M%.done}")" ;;
    esac
  done
  if [ -n "$WALL_CUT" ]; then
    echo "[$(date +%H:%M:%S)] WALL-CUT (rc=124, state=wall_cut) --$WALL_CUT"
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
  echo "[$(date +%H:%M:%S)] ===== arm $2 COMPLETE ($DONE_N/$3 markers) ====="
  return 0
}

run_one() {                       # $1 = task, $2 = cpu list
  T=$1; CPUS=$2
  MARKER="$OUT/$ARM-$T.done"
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
    timeout "$HARD_TIMEOUT" taskset -c "$CPUS" ./algotune.sh agent --standalone \
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
        --out-dir "$WS" ${MAKE_TASK_ARGS:-} >/dev/null 2>&1
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
    if [ "$RC" = 2 ] && ! run_started_evidence "$TASK_ROOT/run" >/dev/null; then
      # The SAME predicate `record_done` uses four lines down, applied to the results row. A run that
      # refused to start has no fold, so there is no champion to extract -- and the usual
      # null-speedup row would enter compare_arms' table looking exactly like a task that ran hard
      # and found nothing. `compare_arms.py` reads only `speedup`, so the sentence is for the human.
      echo '{"speedup": null, "error": "the LoopLab run refused to start (exit 2, no event log) -- not a measurement"}' \
        > "$OUT/B-$T.final.json"
    elif python "$REPO/benchmarks/algotune/extract_champion.py" --run-dir "$TASK_ROOT/run" \
           --out "$TASK_ROOT/champion_solver.py" >> "$OUT/B-$T.log" 2>&1; then
      (cd "$TASK_ROOT" && timeout "$HARD_TIMEOUT" taskset -c "$CPUS" \
          python "$REPO/benchmarks/algotune/looplab_eval.py" --algotune-root "$AT" --task "$T" \
          --model LoopLabFinal --solver champion_solver.py --subset test) \
          > "$OUT/B-$T.final.json" 2>>"$OUT/B-$T.log"
    else
      echo '{"speedup": null, "error": "no champion to score"}' > "$OUT/B-$T.final.json"
    fi
    record_done "$MARKER" "$RC" "$S" "$CPUS" "$TASK_ROOT/run"
    [ -s "$MARKER" ] && echo "[$(date +%H:%M:%S)][$CPUS] $T arm B done ($(cat "$MARKER"))"
  fi
}

echo "arm $ARM | $NTASKS tasks | $LANE_COUNT lanes x $CORES_PER_LANE cores from core $CORE_OFFSET (of $NPROC) | budget \$$BUDGET_USD"
echo "model $ALGOTUNE_MODEL_KEY | llm ${METER_BASE:-$LOOPLAB_LLM_BASE_URL}${METER_BASE:+ (metered, per-attempt paths)}"
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
