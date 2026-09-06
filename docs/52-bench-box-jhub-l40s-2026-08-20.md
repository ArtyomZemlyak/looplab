# 52. Standing the arena up on a second box: JupyterHub + L40S (2026-08-20)

Companion to [doc 50](50-benchmark-landscape-and-local-plan-2026-08-19.md) (*why* AlgoTune, and the
parity analysis) and [doc 51](51-algotune-arm-operational-notes-2026-08-20.md) (*what was touched*
in the third-party checkout). This one is the **second machine** doc 51 §6 asks for: what a box that
is not the original 8-core Windows/WSL laptop needs, what it measured differently, and what a number
produced here may and may not be set beside.

**Nothing here changes the protocol.** The arms, the task set, the patches, the run counts and the
$0.02-per-task-arm budget are doc 51's. What changes is the environment underneath, and one thing
that had to be *built* because the environment took a mechanism away: the budget.

---

## 1. The box

| Resource | Value | Consequence |
|---|---|---|
| CPU | `nproc` 96, **cgroup quota `cpu.max = 9000000 100000` = 90 CPUs** | 20 lanes x 2 cores = 40 — inside the quota, so lane pinning means what it says. An arm is **one round**, not seven. |
| RAM | 755 GB (`memory.max` 773 GB) | Never the constraint here. |
| GPU | 1x L40S, 46 GB, idle | **Unused by this arena.** AlgoTune is CPU-bound in substance (doc 50 §5d), which is why the sm_120 problem that blocks FML-bench does not arise. |
| Disk (runtime) | overlay/xfs on `/`, ~110 GB free | Where everything runs. |
| Disk (home) | `/home/jovyan/data`, **geesefs — an S3-backed FUSE mount**, 1 PB | Where nothing runs. See below. |
| Python | 3.12 system, **3.11.13 via `uv` for the AlgoTune venv** | Same version as doc 51. |
| Network | HTTPS through a local `http_proxy` on 127.0.0.1:18080 | github/pypi/HuggingFace/openrouter all reachable; the corporate LLM gateway is reached DIRECTLY, not through it. |

### The home filesystem cannot host the benchmark

`/home/jovyan/data` is geesefs. `uv venv` fails on it outright — `error: Operation not supported
(os error 95)`, i.e. no `flock` — so the environment cannot even be *built* there. That is the loud
failure; the quiet one matters more. This benchmark's cost is **process spawn and imports** (doc 51
trap 2: one fork per timed run, spawning is ~98 % of per-instance cost), which is precisely what a
network filesystem is worst at, and a timing taken over S3 is not a timing of the solver.

So the layout splits by what each filesystem is for:

```
/home/jovyan/data/looplab-bench/   persistent, geesefs   git checkouts, archived campaign output
/var/tmp/looplab-bench/            local overlay disk    AlgoTune + .venv + .hf_datasets, arm-B clone,
                                                         campaign output, meter log
```

The local side is **ephemeral** — a container restart takes it. That is acceptable only because
every part of it is scripted (`setup_algotune.sh`, `setup_gateway_arm.py`, `box-jhub-l40s.sh`) and
the repository side is pushed. It is the same argument doc 51 makes for arm B running from a pinned
clone: the environment is a thing you rebuild from a record, not a thing you preserve.

---

## 2. The LLM is a corporate gateway, and it took the budget mechanism away

The model is `deepseek-v4-flash` on an internal **LiteLLM** gateway (`/v1`, SGLang behind it), not
OpenRouter. Measured 2026-08-20:

| What | Measured | Why it matters |
|---|---|---|
| `usage.cost` | **absent.** Its own `x-litellm-response-cost-original` is `0.0` (`x-litellm-model-name: openai/default`, i.e. unpriced there too) | **Both arms budget by reading `usage.cost`** — AlgoTuner at `models/lite_llm_model.py::_extract_cost_from_response`, LoopLab at `core/llm.py::_usage_cost`. Unpriced ⇒ `spend_limit: 0.02` and `LOOPLAB_LLM_BUDGET_USD` never bind, and the SPEND budget doc 50 §5f settles on becomes no budget at all. |
| Rate limit | `x-litellm-key-rpm-limit: 50` (team 150), **enforced**: a 20-way burst returned **9 x HTTP 429**, and sequential calls kept 429-ing until the window rolled | A 20-lane campaign trips it constantly. |
| Caching | The identical prompt at `temperature 0` returned in **0.0 s with 400 completion tokens** (28,886 tok/s) — a cache hit, not a generation | A probe without a nonce measures the cache and reports an endpoint that does not exist. **The first speed numbers taken here were exactly that mistake.** |
| Real throughput | **~96 tok/s** median, 400-token completions (`deepseek-v4-flash`); `qwen3.6-35b` ~160–320 tok/s | The campaign model is the slower of the two, and is chosen anyway: it is the model doc 50/51 sized the campaign around. |
| Reasoning channel | **none** — no `reasoning_content`, and the OpenRouter `provider` / `reasoning` blocks are accepted and ignored (HTTP 200) | doc 51's `reasoning: {effort: medium}` pin and the `siliconflow/fp8` provider pin control nothing here. They are set EMPTY rather than left in: a dead parameter in the record reads like a live control. |

### The fix is one meter in front of both arms, not an edit to either

`benchmarks/meter/proxy.py`. Both arms point at `http://127.0.0.1:8801/m/<arm>/<task>/<attempt>/v1` and
the proxy:

(The attempt segment arrived 2026-08-23. `(<arm>, <task>)` alone is not an identity — a task-arm
that is RE-RUN adds to the same bucket, and `B/kcenters` came to hold $2.0086 over 816 calls
against one `.done` marker whose run cost $1.0070. The two-segment form above is still
accepted and is recorded with an empty `attempt`; a row from a log written before the change
carries no `attempt` key at all. `campaign.sh::next_attempt` mints the id and stamps it into the
marker as `attempt=aN`, so the marker and the meter rows join by equality.)

1. **Prices every response** from a pinned table (`benchmarks/meter/pricing.json`) and writes the
   result into `usage.cost`, where both arms already look. Neither framework is modified.
2. **Shapes the traffic** — one shared 45 rpm budget (under the published 50) and one 429-retry
   policy for both arms. This is a *parity* decision before it is a politeness one: AlgoTuner retries
   with its own backoff and LoopLab with its own, so an unshaped 429 storm would charge the two loops
   differently for the same endpoint condition.
3. **Attributes by path**, so cost lands per task-arm without either framework knowing it is metered.
   Arm B reads `LOOPLAB_LLM_BASE_URL`; arm A gets the same path through `OPENAI_BASE_URL`, which
   litellm honours for an `openai/<model>` entry that carries no `api_base` of its own.
4. **Meters streams too.** LoopLab streams by default and AlgoTuner does not; pricing only the
   non-streaming half would price one arm and not the other, and switching LoopLab's streaming off to
   dodge that would change the loop under measurement. The usage frame is rewritten in flight, frame
   by frame, unbuffered.

**The price is imputed, and the doc says so.** The constants are the published OpenRouter list price
of `deepseek/deepseek-v4-flash-0731` — $0.140 in / $0.280 out per 1M, the same `siliconflow/fp8` row
doc 50 §5a chose — fetched 2026-08-20T10:16:47Z and **pinned in the file**, because a benchmark
number must not move when a vendor re-prices a model between two arms. Every response carries
`usage.cost_basis: "imputed"` and `usage.cost_source: <that timestamp>`, and an upstream that ever
starts reporting its own cost outranks the table (`cost_basis: "upstream"`).

What this buys is **parity, not accuracy**: both arms are priced by identical constants through
identical code, so "arm B spent 1.8x arm A" is a real finding. It is not an invoice, and the dollar
column here cannot be compared to a dollar column from an OpenRouter run.

---

## 3. Deviations beyond doc 51's list

doc 51 §1 enumerates what is patched in the third-party checkout; all of it applies unchanged and is
applied by the same `setup_algotune.sh`. This box adds three, in descending order of how much they
could touch a number:

| # | Deviation | Effect on the measurement |
|---|---|---|
| 1 | **The metering proxy** (above) | It is the reason a budget exists at all here. It adds a loopback hop to every call — measured overhead is below the endpoint's own jitter — and it queues calls when the arms exceed 45 rpm, which shows up as wall-clock, identically for both arms, and is recorded per call as `queued_s`. |
| 2 | **A second model entry** in AlgoTune's `config.yaml`, `gateway/deepseek-v4-flash` → `openai/deepseek-v4-flash` (`benchmarks/meter/setup_gateway_arm.py`, idempotent, keeps a `.orig`) | None on scoring. The OpenRouter entry is left exactly as `setup_algotune.sh` wrote it; a box picks one with `ALGOTUNE_MODEL_KEY`. |
| 3 | **`typer>=0.12` installed into the AlgoTune venv** | None on scoring — it is LoopLab's CLI argument parser. Worth recording because doc 51 §3 states that *nothing* was added to that venv for LoopLab's sake; on this box exactly one thing was, and arm B still inherits AlgoTune's numerical stack (`torch 2.13.0`, `numpy 1.26.4`, `scipy 1.17.1`), not ours. |

`campaign.sh` gained three env knobs whose defaults reproduce the previous behaviour exactly:
`ALGOTUNE_MODEL_KEY`, `METER_BASE`, and `LOOPLAB_LLM_REASONING_EXTRA` becoming overridable rather
than hardcoded. Box-specific values live in `benchmarks/box-jhub-l40s.sh`, not in the campaign
script, so the campaign script stays the same file on every machine.

---

## 4. What was verified here, and what it measured

| Check | Result |
|---|---|
| AlgoTune install (`uv venv --python 3.11` + `uv pip install -e .`) | 158 packages in **2 m 07 s**. Same headline versions as doc 51: `torch 2.13.0`, `jax 0.7.1`, `scipy 1.17.1`, `numpy 1.26.4`, `ortools 9.11.4210`. `litellm` is 1.97.0 here vs 1.83.0 there. |
| The 27 packages the agent's prompt promises | **27/27 importable**, matching doc 50 §5d's audit on the other box. |
| All seven `setup_algotune.sh` steps | Applied: 2 `sys.modules` sites, the site-packages narrowing, `runs/dev_runs/eval_runs: 3`, `baseline_timeout: 10000`, `disable_rlimit_as: true`, both on-disk patches. |
| **The evaluator, end to end, with no LLM in the loop** | Re-timed a SHIPPED reference solver: `GPT-5.4` on `discrete_log` → **0.9967x**, ~9 minutes cold on 2 pinned cores including the HuggingFace dataset fetch. This is doc 50 §5e's re-timing plan working on this box. |
| `pick_tasks.py` | Reproduces campaign.sh's 20-task list exactly (cheapest 20 by `reports/generation.json` median eval pass; worst case `pde_heat1d` 60.5 s on the authors' machine). |
| Cost reaching **arm A** | `litellm` yields `_hidden_params.response_cost = None` (it does not know this model), falls through to method 2, and `_extract_cost_from_response` returns the injected figure. Verified against a live call. |
| Cost reaching **arm B** | LoopLab records `calls 1 priced 1 spent 1.764e-05` for 16 in / 55 out — exactly the pinned rate. `priced_calls` equalling `calls` is the property that makes `spent` an invoice rather than a floor. |
| The rate limiter | 50 concurrent requests: **50/50 ok, 0 x 429, 0 retries**, 28 of them queued, max wait 60 s. Direct to the gateway the same shape gave 9 x 429. |
| A real arm-A task through the whole chain | `ARM=A TASKS=discrete_log` reaches the agent loop and its first message meters as `arm A, task discrete_log, 2571 in / 205 out, $0.00041734` — so the $0.02 budget is ~48 messages here. |

---

## 5. What a number from this box may be set beside

- **Against another arm run HERE — yes.** That is the whole design: same machine, same clock, same
  meter, same task set, one model. Arm A vs arm B is valid, and so is re-timing the 17 shipped
  reference solvers on these cores.
- **Against doc 50/51's numbers from the 5090 box — no.** Different CPU count, different model
  deployment, unknown quantization on the gateway, and an imputed price. Ratios are
  hardware-self-normalising *within* a task-run (`speedup = baseline_ms / optimized_ms`, both timed
  here), so the SPEEDUP column travels better than the cost and wall-clock columns do — but the arms
  were budgeted in dollars that mean different things, so treat a cross-box comparison as
  qualitative.
- **Against AlgoTune's published table — no**, for the reason doc 50 §5e already gives: the 17
  shipped arms are artifacts of AlgoTuner's own loop driving other models, and re-timing them here
  makes them context, not controls.

---

## 6. Reproducing this box

```bash
mkdir -p /var/tmp/looplab-bench && cd /var/tmp/looplab-bench
git clone --depth 1 https://github.com/oripress/AlgoTune.git AlgoTune
cd AlgoTune && uv venv --python 3.11 .venv && VIRTUAL_ENV=.venv uv pip install -e . \
    && VIRTUAL_ENV=.venv uv pip install 'typer>=0.12'
printf 'LOOPLAB_LLM_API_KEY=%s\nMETER_UPSTREAM=%s\n' "$KEY" "$GATEWAY/v1" > .env

git clone <looplab> /var/tmp/looplab-bench/looplab && cd /var/tmp/looplab-bench/looplab
benchmarks/algotune/setup_algotune.sh /var/tmp/looplab-bench/AlgoTune
python3 benchmarks/meter/setup_gateway_arm.py --algotune-root /var/tmp/looplab-bench/AlgoTune

source benchmarks/box-jhub-l40s.sh
benchmarks/meter/start_meter.sh
python3 benchmarks/meter/probe_endpoint.py --models deepseek-v4-flash --sequential 5 --concurrent 20
ARM=A benchmarks/algotune/campaign.sh
```

**Probe the endpoint before every campaign, with a nonce.** doc 50 §5a's rule — the catalogue is not
evidence — has a second edge here: on a caching gateway, the *probe itself* is not evidence unless
each call is unique.

---

## 7. Timing a task's instances AT ONCE — what 90 cores actually buy

The original box had 8 cores and doc 50 called that its handicap. This one has the opposite problem:
the arena spends ~97 % of its wall clock in the per-instance timing pass (an arm-A run made **5 LLM
calls in 40 minutes**), and that pass walked instances one at a time. Not a decision anybody made —
the harness's own pool class, `BenchmarkPool`, is defined and instantiated **nowhere**.

### Measure whether the timer still reads the same number, then parallelise

`benchmarks/algotune/probe_parallel_timing.py`, same instances at 1 / 8 / 24 / 48 concurrent workers,
each pinned to its own core:

| task | serial | 48-way | median inflation | p90 | cores per instance |
|---|---:|---:|---:|---:|---:|
| discrete_log | 4.3 s | 1.0 s | 1.04× | 1.18× | **1.0** |
| convex_hull | 6.1 s | 0.9 s | 0.99× | 1.01× | **1.0** |
| spectral_clustering | 106.7 s | 5.8 s | 1.01× | 1.09× | **1.0** |

Two things had to be true and are. **One instance uses one core** — the harness sets
`OMP/MKL/OPENBLAS_NUM_THREADS=1` itself, so one-per-core is not oversubscription. And the score is a
RATIO whose halves both run through this path, so the few per cent that does appear cancels.

Shipped as `patch_parallel_eval.py` + `parallel_eval.py`: instances are prefetched through a pool
whose workers each claim one core **out of the mask the process already holds** (a lane is pinned
with `taskset`; a pool that picked core numbers of its own would walk onto cores another lane is
timing on). `ALGOTUNE_EVAL_WORKERS` unset or 1 leaves upstream behaviour bit-for-bit, and it applies
to both arms because both evaluate through this harness.

Measured effect: the instance pass on `discrete_log` goes **~130 s → 2.35 s**, a whole scorer run
132 s → 23 s, and `spectral_clustering` 348 s → 45 s.

### Three things this cost, all of which are the finding

1. **Threads do not work.** The first pool was threads, reasoning that `evaluate_single` waits on
   the child the harness forks per timed run. 100 instances took **109.6 s** against ~130 s serial.
   Enough of it holds the GIL. A forked pool does it in 2.35 s.
2. **The pool silently never ran, twice.** `multiprocessing.Pool` pickles its initializer and mapped
   function *even under fork*, so a closure raises, the caller catches, and the run falls back to
   serial reporting nothing — while the harness's logging config swallows our own warning. The fix
   was module-level functions; the thing that made it *findable* was a breadcrumb file. Without it,
   "the pool ran and did not help" and "the pool never ran" are indistinguishable and have opposite
   fixes.
3. **The per-run timeout floor is a startup allowance, not a solver bound** — upstream's own comment
   says so. Under concurrency startup costs more, and at 24 workers the floor fired **94 times**;
   ONE timeout makes a whole task `N/A`, because 100 % instance validity is required for any speedup
   at all. `ALGOTUNE_MIN_TIMEOUT_S` makes it tunable, defaulting to upstream's value.

### What is NOT established, and was briefly claimed here

An earlier draft of this section said the metric is regime-sensitive, on the strength of the same
shipped solver scoring 1.09× / 1.43× / 1.78× under different pairings of contended halves. **That
does not follow.** The same box then produced **1.0007 and 1.6318 in the SAME regime** on the same
task — which is the spread doc 50 §5f already recorded for `discrete_log` (1.0006 then 1.4468 on
consecutive runs). One task at n=1 cannot separate a regime effect from that noise; the honest
reading is the one doc 50 already prescribes — **read the aggregate over 20 tasks, never a row.**

The wall-clock win is not affected by any of this: it was measured directly, inside the pool, by the
breadcrumb.

### The ORACLE half ships OFF

`prefetch_oracle` reproduces the serial path's isolated call for the REFERENCE timing and every job
returns `AttributeError: Class 'Solver' not found in solver module` — `run_isolated_benchmark` loads
a solver module out of `code_dir`, and for the reference pass that directory is the task package,
which has no `Solver` class. The serial loop reaches its number some other way. Gated behind
`ALGOTUNE_PATCH_ORACLE=1` rather than deleted, because the *campaign* spends real time in that pass
(unlike the scorer path, where some tasks' datasets ship `median_oracle_time_ms`), and it is one
identified call away from working.

## 8. The campaign as launched, 2026-08-20

OPEN[docs52-launch-block-contradicts-docs51-regime] the launch block below and docs/51 §10 cannot
both be true of one campaign.
proof:`present:ALGOTUNE_EVAL_WORKERS=auto@docs/52-bench-box-jhub-l40s-2026-08-20.md`
REVIEW 2026-08-25 (docs): docs/51 §10 measures the parallel eval regime inflating the metric ~75 %
and mandates the opposite of this block ("leave `ALGOTUNE_EVAL_WORKERS` unset ... every campaign so
far is serial"). A reader reproducing from this block gets numbers docs/51 declares invalid (the
solver pass parallel while the reference stays serial — the oracle half ships OFF and broken); a
reader following docs/51 runs serial, where the bridge's reference-timed-in-pass guard cannot fire
at all (its glob never matches serial-regime cache names — see the annotation at
`_baseline_fingerprint` in `benchmarks/algotune/looplab_eval.py`). Either way one of the two shipped
protections is inoperative. Fix: date-stamp this block as superseded by docs/51 §10 (or drop the
`auto`), and make campaign.sh refuse or warn when a >1 value is inherited from the environment —
the regime is "part of the measurement" by the driver's own header.

```
20 tasks | 4 lanes x 22 cores (of 96, quota 90) | ALGOTUNE_EVAL_WORKERS=auto | $0.02 per task-arm
model gateway/deepseek-v4-flash through the meter, per-task paths
```

Few lanes, many cores per lane — the shape instance-level parallelism makes possible, and it retires
the endpoint problem as a side effect: **4 lanes x 0.12 rpm = 0.5 requests/min against a 50/min
limit**, where 20 lanes of arm B would have sat at ~41. Measured per-lane demand, from the smoke
runs: arm A 0.12 rpm (peak 3/min), arm B 2.05 rpm (peak 9/min).

Arm B runs from `/var/tmp/looplab-bench/looplab-armb`, detached at a commit rebased onto master, per
doc 51 §7. The rebase cost two conflicts, and the second is worth recording: `settings_ui_schema.json`
is a 150 KB single line, merged programmatically by inserting this branch's two fields into master's
document; and `test_calibration_profile_home.py` arrived with **two adjacent `_EXPECTED_FIELD_COUNT`
statements** (212 from master, 213 from the branch) of which Python silently uses the last — the
vacuous-guard shape that file's own header warns about, and the second time this table has produced
it at a merge. Collapsed to one and re-measured over the rebased tree: 214 fields, digest
`838bdfda…`.

### Unattended operation

`benchmarks/algotune/run_final.sh` (in the repo since 2026-09-06; the `run_both_arms.sh` /
`run_final.sh` this paragraph used to name lived only under `/var/tmp` and died with the container,
docs/58 §58.7 item 9) runs arm A to completion and then arm B in the SAME regime, one attempt per
task-arm, one recorded configuration, every log line dated; `campaign.sh` snapshots after each arm
and the driver prints the summary command — because arm B must start when arm A frees the cores and
nobody is awake at that moment. `benchmarks/watchdog.sh` restarts the meter if it stops answering (both arms fail on
connection refused, and a failed task-arm writes no score) and RECORDS everything else — disk,
orphaned forkservers, a campaign that is alive but whose logs stopped growing. It repairs one thing
and reports the rest, because an unattended repair of a measurement is worse than a gap in one.
