# 48. Running the AlgoTune arm: every deviation, dependency and trap (2026-08-20)

What a second machine, or a later reader, needs in order to reproduce these numbers and to know
what was changed outside this repository to get them. Companion to
[doc 47](50-benchmark-landscape-and-local-plan-2026-08-19.md), which covers *why* AlgoTune and what
was measured; this one covers *what was touched*.

Everything below is applied by `benchmarks/algotune/setup_algotune.sh`. It exists so that none of it
lives in somebody's shell history. Run it on every machine, and again after any `git pull` in the
AlgoTune checkout.

---

## 1. A NON-OURS repository is modified: `oripress/AlgoTune`

**Four source files and one config in a third-party checkout are patched on disk.** They are not
vendored, not forked, and not upstreamed. Each keeps a `.orig` backup and each patch is idempotent.

| File | Change | Why it is not optional |
|---|---|---|
| `AlgoTuner/utils/isolated_benchmark.py` | `sys.modules.items()` → `list(...)`, two sites | `inspect.getmembers()` inside the loop triggers lazy imports and mutates the dict → `RuntimeError: dictionary changed size during iteration` on **every** benchmark run (224 occurrences in one run). No speedup can ever be recorded without it. |
| `AlgoTuner/utils/isolated_benchmark.py` | cache-clearing filter excludes `site-packages` | Upstream matches `"AlgoTune"` anywhere in `__file__`. With the venv **inside** the checkout that matches the whole virtualenv — measured 2132 of 2493 modules, torch/jax/scipy included — and inflated the oracle pass **6.5×** (8.6 s → 56 s per instance). |
| `AlgoTuner/utils/evaluator/baseline_manager.py` | disk-backed baseline cache (`patch_baseline_cache.py`) | The in-process cache starts empty in every new interpreter, so an out-of-process bridge re-pays the whole reference pass **per node** (~15 min). Measured effect: a repeat evaluation went 668 s → 215 s. |
| `scripts/evaluate_results.py` | honours `ALGOTUNE_EVAL_SUBSET` (`patch_eval_subset.py`) | It hardcodes `subset="test"` at three sites. Unpatched, **every LoopLab node is scored on the test split** while the bridge still records `subset: train` — the train/test leak present *and* the provenance claiming it was closed. |
| `AlgoTuner/config/config.yaml` | see §2 | budget, run counts, timeouts, pool, model pin |

**`reports/agent_summary.json` is also written by our runs.** Our model's rows land *beside* the 17
shipped reference models in the same file. That is upstream's own mechanism (`main.py` takes an
exclusive lock and updates it), not a hack — but it means the file in a used checkout is no longer
pristine, and `git status` will show it modified.

### Two things the patch scripts do that matter under `--revert`

- The `.orig` backup is captured **once** and never refreshed. `--revert` after an upstream update
  would restore a pre-update file over a newer one. Re-clone rather than revert if the checkout has
  moved.
- `patch_eval_subset.py` refuses to write a half-applied change: if any hardcoded `subset="test"`
  survives its substitutions it aborts rather than leaving the file in a state where numerator and
  denominator could come from different halves of the dataset.

### Known discrepancy on this box, recorded rather than fixed

The live checkout here was patched **before** a later review fix, so its `baseline_manager.py` still
carries the `or "task"` cache-key fallback where the committed script now fails closed. For a task
with a name — every campaign task — that branch is unreachable, so no recorded number is affected.
It was left alone rather than swapping measurement code under a running multi-hour campaign. A fresh
machine gets the corrected version.

---

## 2. Config deviations, and what each one cost to find

```yaml
global:
  spend_limit: 0.02        # not 1.00
  total_messages: 9999     # unchanged
benchmark:
  runs: 3                  # ABSENT upstream, defaults to 10
  dev_runs: 3              # was 2
  eval_runs: 3             # was 10
  baseline_timeout: 10000  # was 60000
  validation_pool:
    num_workers: 4                    # inert, see §4
    memory_limit_gb_per_worker: 30    # was 14
    disable_rlimit_as: true           # was false
```

- **`spend_limit: 0.02`.** AlgoTune's `$1.00` is calibrated for expensive models. Measured here,
  seven agent messages cost **$0.0071**, so $1 buys ~1,000 messages and is not a budget at all — one
  task ran 56 minutes without approaching it.
- **`runs: 3` is the load-bearing one and it is not in the shipped file.** `runs` governs
  `scripts/evaluate_results.py`, i.e. **every node of the LoopLab arm**, and `timing_config.py`
  defaults it to 10. `dev_runs` governs AlgoTuner's own agent loop. So the two arms were iterating at
  **11 vs 3 solver executions per instance** — a 3.7× asymmetry nobody chose. Note `--num-runs` on
  `evaluate_results.py` **cannot** fix this: it only writes `EVAL_NUM_RUNS`, which nothing reads.
- **`disable_rlimit_as: true`.** `RLIMIT_AS` caps *virtual* address space, which JAX/torch/BLAS
  reserve in tens of GB without touching. Every evaluation died with *"A process in the process pool
  was terminated abruptly"* — at 14 GB and again at 30 GB, on a 321 MB task **and** on a 28 KB one,
  with 45 GB free. Neither memory nor dataset size.
- **`baseline_timeout: 10000`.** A timing-out solver costs 60 s × ~100 instances: one candidate ate
  **87 minutes and completed zero problems**. A solver 100× over a 100 ms target scores 0 either way.
- **The model entry, provider pinned.** Unpinned, one slug reached **two different fp4 providers**
  and returned 96/17/96 completion tokens for one prompt. `allow_fallbacks: false` is required —
  without it `order` is a preference, not a pin.

---

## 3. Dependencies actually installed

**System:** `uv` (`/usr/local/bin/uv`), and `taskset` (from `util-linux`, usually present).

**AlgoTune venv:** `uv venv --python 3.11 .venv && uv pip install -e .` — 177 packages including
`torch 2.13.0`, `jax 0.7.1`, `scipy 1.17.1`, `numpy 1.26.4`, `ortools 9.11.4210`, `litellm 1.83.0`.

Two traps in that one line:

- **Resolve `pyproject.toml`, not `requirements.txt`.** The latter pins `pot==1.0.0`, which does not
  exist, so the install cannot succeed.
- **Use `uv`, not `pip`.** pip's resolver thrashes on this dependency tree — measured 464 MB
  downloaded and 9 s of CPU across **40 minutes** before making progress.

**LoopLab is NOT installed into that venv.** It is reached through `PYTHONPATH=<repo>`, and the
AlgoTune venv already carries `pydantic 2.13.4` and the rest of LoopLab's core imports, so
`python -m looplab.cli` works there unchanged. Nothing was added to that venv for LoopLab's sake —
worth knowing, because it means the arm-B run inherits AlgoTune's dependency versions, not ours.

**Datasets are downloaded, not generated.** The first evaluation of each task pulls it from
HuggingFace into `<checkout>/.hf_datasets/`. Sizes are wildly uneven — `discrete_log` 28 KB, `svm`
24 MB, `base64_encoding` **19 GB** — so a first eval can look hung when it is downloading. Budget
disk accordingly, and **validate a bridge on a small task**: an hour was lost here concluding the
pipeline was broken when it was really one enormous dataset.

---

## 4. Traps that cost real time, in the order they bit

1. **`validation_pool.num_workers` parallelises nothing.** `BenchmarkPool` is defined in AlgoTuner
   and **instantiated nowhere**; the timing path forks one process per timed run and waits. Raising
   it bought exactly zero. The parallelism that exists is **between tasks** — see `campaign.sh`.
2. **One process per TIMED RUN, not per instance.** `for idx in range(num_runs): proc =
   ctx.Process(...)`. So the run count multiplies process spawns almost linearly, and spawning is
   ~98 % of the per-instance cost (the timed solver calls are 0.1–0.9 s inside ~16 s). Measured on
   one task: `runs=3` → 178 min, `runs=2` → 118, `runs=1` → 59.
3. **The per-run fork must stay.** Warmup deliberately runs a *different* problem from the timed one
   (the worker asserts `Problems are different objects`) so the code paths are warm and the **answer**
   is not. N timed calls on one problem in one process would let a memoising solver report near-zero
   time and an unbounded speedup.
4. **A wall-clock net that BINDS deletes rows.** A cut run writes **no** `final_speedup` at all, so a
   binding timeout does not shorten a campaign — it empties it. The net is 4 h and is a hung-process
   guard only.
5. **`pkill -f <name>` does not reach multiprocessing forkservers.** Their command line carries
   neither the app name nor the script name. Ten orphans once survived a series of restarts and
   burned CPU on the very cores a run was pinned to, inflating every timing taken while they were up.
   Reap with `pkill -f multiprocessing.forkserver`; check `pgrep -f forkserver | wc -l` before
   trusting a measurement.
6. **`pkill -f <pattern>` also matches its own `bash -lc` command line.** It killed the campaign
   launcher twice here. Use a bracket (`campaign_[r]un`) or separate the kill from the launch.
7. **`N/A` is not a low score.** `set_cover_conflicts` ended in **78 seconds** with
   `reason: import_error` — the agent wrote `from scipy.optimize import integrality` (a *parameter* of
   `milp`, not an importable name), every evaluation died on that import so no 100-instance pass ever
   ran, and the whole $0.02 went on model round-trips. The same budget bought 178 minutes on a task
   whose code ran. Both arms pay this identically so parity holds — but **"same budget" is not "same
   amount of work"**, and a methods note has to say so.
8. **Stopping a campaign used to mark live tasks complete.** `.done` was written unconditionally
   after `timeout`, so killing a run recorded it as finished — measured 2026-08-20, one stop wrote
   six markers over live tasks, one of them 230 minutes in, and a resume would have skipped all six
   with no score. A marker is now written only for exit `0` (ended on its own) or `124` (the
   wall-clock net fired — terminal, recorded so it is visible rather than retried forever). An
   interrupted run leaves no marker and is still owed. The exit code rides in the marker.
9. **The metric is noisy on cheap tasks.** The same solver scored **1.0006** then **1.4468** on
   consecutive runs. Read the aggregate over 20 tasks, not one row; `compare_arms.py` prints every
   per-task row so a wild value cannot hide in a mean.

---

## 5. This machine specifically

- **8 cores presented from a 16-core Ryzen 9 9950X3D** (`nproc` 8, `Win32_Processor.NumberOfCores` 8,
  affinity mask `255`, no `processors=` in `.wslconfig`) — a CCD disabled in firmware, SMT off. The
  missing 8 are unavailable to any process. **Re-enabling them in BIOS would roughly double this box**
  for a workload that is mostly process spawn.
- **WSL2 requires `networkingMode=mirrored`.** Under the default NAT, outbound HTTPS from WSL failed
  3/10 while Windows managed 10/10 at the same moment from the same public IP. It surfaces as a slow
  installer or an `APIError`, far from the real cause. See `~/.wslconfig`.
- **`/root/benchmarks/FML-bench` is cloned but NOT working** — half-installed and blocked on the
  sm_120 GPU generation vs its pinned torch, plus its baselines are committed constants that would
  need re-baselining. It is not part of this arm; ignore it.

---

## 6. Reproducing on another machine

```bash
git clone --depth 1 https://github.com/oripress/AlgoTune.git /srv/AlgoTune
cd /srv/AlgoTune && uv venv --python 3.11 .venv && uv pip install -e .
echo "OPENROUTER_API_KEY=sk-or-..." > .env

benchmarks/algotune/setup_algotune.sh /srv/AlgoTune
ALGOTUNE_ROOT=/srv/AlgoTune ARM=A benchmarks/algotune/campaign.sh
```

`campaign.sh` sizes itself: `lanes = (nproc - 2 - CORE_OFFSET) / CORES_PER_LANE`, capped at the task
count. On an 8-core box that is 3 lanes; on a 90-vCPU server it is 20 — every task at once, so an arm
is one round rather than seven.

**Both arms must run under the same `LANES` and `CORES_PER_LANE`**, or they were not measured alike;
every `.done` row records them. **Arm B additionally requires a rebase onto master first** — its
number is a claim about a *version* of LoopLab, and the only version worth benchmarking is the one
that ships.

---

## 7. Arm B runs from a PINNED CLONE, never from the working tree

`campaign.sh` derives `REPO` from its own location, so an arm launched out of a live checkout
measures whatever that checkout happens to contain at each moment. The first arm-B debug runs did
exactly that — reaching the repository at `/mnt/c/Users/.../worktrees/<branch>` while the branch was
still being edited and rebased. Nothing went wrong, and nothing would have *shown* if it had: a
LoopLab edit landing between task 3 and task 12 produces a campaign whose rows are not from one
program, and the output records the arm, not the commit.

So the measurement arm runs from a detached clone on the native filesystem:

```bash
git clone --no-hardlinks --no-checkout <main-repo> /root/benchmarks/looplab-armb
cd /root/benchmarks/looplab-armb && git checkout --detach <commit>
ARM=B CAMPAIGN_OUT=... /root/benchmarks/looplab-armb/benchmarks/algotune/campaign.sh
```

Three things this buys, in descending order of importance:

1. **The number is a claim about a commit.** `git log --oneline -1` in that tree is the provenance,
   and `git status` is empty by construction.
2. **Editing the branch during a campaign is safe.** The suite, a rebase, a review fix — none of it
   reaches the running arm.
3. **ext4 instead of the 9p/drvfs mount.** Import-time only, so it is worth little; listed last
   because it is the reason that is easy to mistake for the point.

Note the clone must come from the **main repository**, not from a git *worktree*: a worktree's
`.git` is a file naming a Windows path (`C:/Users/.../.git/worktrees/<name>`), which resolves to
nothing inside WSL and fails with `fatal: not a git repository`.

**Do not launch an arm while the test suite — or anything else — is using the cores.** The lane
pinning guarantees a lane its own cores against *other lanes*, not against the rest of the box, and
a co-running load inflates every timing taken while it is up without leaving a trace in the output.
This is trap 5 in the list above, with a different process on the other end.

---

## 8. The grader fence, and why closing one channel was not enough

Trap 10, and the most expensive one, because it invalidates results rather than wasting time.

`tools/dev_probe.py` fences the execution probe against reading the source tree, and that fence
exists *because of this benchmark*: an earlier AlgoTune run spent 150 of its 239 tool calls inside
`run_probe` reading `validation_pipeline.py` and `isolated_benchmark.py` — the checker and the timer
sitting beside the run. A solver written after reading the checker is not a result.

**The fence held and the behaviour moved.** On 2026-08-20 a `--role-split` run — whose goal told the
Developer not to use the probe to *choose* an algorithm — made **213 of its 216** env-inspection
calls against `AlgoTuner` / `AlgoTuneTasks`:

```
grep_installed  {"package":"AlgoTuner","query":"is_solution"}
grep_installed  {"package":"AlgoTuner","query":"def run_isolated_benchmark"}
grep_installed  {"package":"AlgoTuner","query":"mean_speedup"}
read_installed  {"module":"AlgoTuner.utils.isolated_benchmark","start_line":1070}
read_installed  {"module":"AlgoTuneTasks.base"}
```

`read_installed` / `grep_installed` come from `tools/env_inspect.py`, whose whole job is answering
"what does this installed library look like" — and AlgoTune is `uv pip install -e .` into the same
venv, so the harness *is* an installed library as far as that tool can tell.

Two things make this worth reading twice:

- **The probe count fell while the harness reads rose.** Probes went 101 → 24 (43% → 8% of tool
  calls) and env reads went 20 → 216. The constraint bound the TOOL, not the ACTIVITY; the
  Developer still needed the instance sizes and used whatever channel was open.
- **The two control runs beside it touched the harness ZERO times** (20 and 16 env reads each). So
  this is not a route that is always taken — it is one that opens under pressure. A rule that is
  merely stated in a prompt holds right up until the moment it matters.

Fixed by `EvalSpec.protect_packages`, declared in `make_task.py` as `["AlgoTuner", "AlgoTuneTasks"]`
and enforced in `EnvInspectTools` before dispatch, on every tool that names a package. It is
DECLARED rather than derived because only the operator who wrote `eval.command` knows which
installed distribution is the grader. `tests/test_grader_package_fence.py` drives it, including an
AST check that no construction site builds the inspector without the fence — a missed site is the
whole hole for that phase.

**Any number produced before this landed must be read with the question "could this node have read
the checker?"** For the runs on this box the answer is: the two `discrete_log` controls, no
(measured, zero harness reads); the `--role-split` run, yes — it is discarded.

---

## 9. The fork is the primary path (2026-08-21)

`github.com/ArtyomZemlyak/AlgoTune`, branch `looplab-bench`, is upstream `dff9914` with every
deviation in §1–2 applied **as a commit**. Use it:

```bash
git clone -b looplab-bench https://github.com/ArtyomZemlyak/AlgoTune.git /srv/AlgoTune
```

That retires the sharpest hazard in §1 — a number that could only be attributed to "a checkout
somebody patched" can now name a commit, and `git status` on a prepared checkout is empty.

**`setup_algotune.sh` no longer reproduces that branch, and the difference changes measurements.**
The fork additionally carries `AlgoTuner/utils/evaluator/looplab_parallel.py` and its wiring in
`evaluation_orchestrator.py` / `solver_executor.py`: instances are evaluated concurrently, one core
per worker, out of the mask the process already holds (measured on the reference box: a scorer run
132 s → 23 s; 100 instances of `discrete_log` 2.35 s against ~130 s serial). No patch script
produces those, so a checkout prepared by the script evaluates **serially** and one prepared from
the fork evaluates **in parallel**. Both are valid harnesses. Numbers from the two are not
comparable, because the regime a solver is timed under differs — so the script now detects the fork
and says which one you have rather than being quiet about it.

### Verified here, end to end, on the fork

A reference-equivalent solver (sympy's own `discrete_log`) scored **1.0048** in 195 s and, on a
second run with the harness baseline cached, **1.0161** in 108 s. That is the first end-to-end proof
of the whole bridge — dataset load, baseline pass, solver timing, ratio — and it also measures the
**noise floor: ~1 % between identical runs**, which is the resolution any per-task claim has to
clear.

### `baseline_source` was lying, and it cost an investigation

The bridge printed `baseline_source: "unavailable"` on every successful evaluation. It never meant
the baseline was missing — the speedup is the harness's own and arrives fine. It meant only that the
record exposes no `baseline_time_ms` for the bridge's *aggregate* cache to store, which is the normal
shape and is noted in the code beside the read. On 2026-08-20 a node that scored 0.0 was first
diagnosed from that label; the real cause was an empty working set, three layers away. The label now
says what it means.

---

## 10. `ALGOTUNE_EVAL_WORKERS` inflates the metric ~75 %. Keep it at 1 until that is explained.

The fork's parallel evaluator is real and fast — but on this box it also **changes the number**, and
that disqualifies it for measurement until the mechanism is understood.

Reproduction, same solver every time (sympy's own `discrete_log` wrapped as `Solver.solve`, i.e.
reference-equivalent, whose honest score is ~1.00), same four pinned cores, `discrete_log`, train:

| `ALGOTUNE_EVAL_WORKERS` | speedup | eval wall |
|---|---|---|
| 1 | **1.0011** | 107 s |
| 2 | **1.7795** | 113 s |
| 4 | **1.7156** | 20 s |
| 1 (repeats) | 1.0048 / 1.0074 / 1.0161 | 195 / 108 / 109 s |

Three things this rules out:

- **Not noise.** Serial repeats span 1.0011–1.0161 (~1 %, the measured noise floor); every
  parallel run lands at 1.72–1.78. The gap is ~75×  the noise.
- **Not contention scaling.** The jump appears the moment workers goes 1 → 2 and does **not** grow
  from 2 → 4. At `workers=2` the run was *slower* in wall time (113 s vs 107 s) while reporting
  1.78×, so whatever moved is not "more cores made it faster".
- **Not the regime-keyed baseline cache.** That mechanism exists in `baseline_manager.py`, and its
  comment records the authors measuring this same family of swing (1.46× → 1.04×) and fixing it that
  way. Here the cache directory does not exist at all, so every run re-measures its own baseline in
  its own regime — and the discontinuity survives anyway.

What is left is a structural difference between the serial timing path and the pooled one: once the
pool is used at all, the solver's measured time and the reference's stop being taken the same way.
A ratio only cancels overhead when both halves carry it.

**Operational rule until this is explained: leave `ALGOTUNE_EVAL_WORKERS` unset.** `campaign.sh`
does not set it, so every campaign so far is serial and unaffected. The prize for fixing it is large
— 107 s → 20 s per evaluation, and evaluation is ~98 % of a real arm's wall clock — but a 75 %
inflation would have read as "LoopLab beats the reference by 1.7×" on a solver that is literally the
reference.

---

## 11. The task must declare `eval.stages`, not `eval.command` (2026-08-21)

A bare `eval.command` leaves the Developer's **stages phase** switched on, and that phase is written
for ML pipelines. Its prompt says:

> declare the ordered stages that run BEFORE it … GOOD PRACTICE: separate stages for data/feature
> PREPARATION, TRAINING (a fresh model every node — the pipeline must not point at another
> experiment's checkpoint), and TESTING; bake this node's hyperparameters into the `train` command

An AlgoTune task has none of that. It is one file that the scorer runs directly, so the honest
answer is *"no stages"* — which the prompt never offers. What the model does instead is invent one:

```json
{"name": "check", "command": ["python", "-c", "print('Ready')"],
 "expect": {"assert": "Check solver environment readiness"}}
```

That is the entire output of all **five** nodes of the 2026-08-21 `google/gemini-3.7-flash` run —
the first arm-B run that ever reached an evaluation. `solver.py` was the untouched template in every
one, each evaluated honestly in 12–17 s, and each recorded 0.0. On the DeepSeek control the same
phase was the single largest consumer of wall clock (232 generations).

Declaring the scoring command as a **one-stage operator pipeline** instead removes the phase
altogether: `repo_developer.py::_operator_stage_list` reads `eval.stages`, and when it is present and
valid the engine runs it verbatim, the Developer's own manifest is ignored, and the phase is skipped.
Verified on the generated task — `_operator_stage_list -> ['score']`, `has_cmd: True`, grader fence
unchanged.

`score` is the right stage name: it is reserved *against a Developer manifest* precisely because it
denotes the operator's own scoring step, and this is the operator's.

**Read every arm-B number before this date with that in mind.** The Developer was being asked, on
every node, to design a training pipeline for a task that has none — and the phase where it should
have written the solver came after it.

---

## 12. The two arms did not share a budget, and the banner said they did (2026-08-21)

`BUDGET_USD` reaches `LOOPLAB_LLM_BUDGET_USD` — **LoopLab's ceiling and nothing else**. AlgoTuner
resolves its own as `model_info.get("spend_limit", global_config.spend_limit)` out of `config.yaml`,
which the campaign never touched. So a `$1.00` arm-B run could sit beside a `$0.02` arm-A run while
the driver's own banner printed one budget for both.

Two changes, and the split between them is the point:

- **`patch_model_entry.py`** adds an OpenRouter model entry *with* its `spend_limit`. A per-model
  limit wins over the global one, so this is the only lever the campaign has over the reference
  arm's budget. It is also what makes running both arms on a NEW model possible at all —
  `ALGOTUNE_MODEL_KEY` can only pick an entry that exists.
- **`campaign.sh` REFUSES rather than rewrites.** For `ARM=A` it reads the entry's limit and exits 2
  if it is missing or disagrees with `BUDGET_USD`, naming the exact command that fixes it. Silently
  rewriting `config.yaml` from a campaign driver would make every run authoritative over a file the
  fork owns — the worse failure, and the one that would leave no trace.

Driven in all three states before use: matching budget passes, a mismatch refuses, an unknown model
refuses.

### While this was found: the two arms were on different reasoning depths

Arm A sets effort only through `extra_body.reasoning.effort` (OpenRouter's spelling) and AlgoTuner
sends no `reasoning_effort` of its own — so it has never had the collision §…/`reasoning_body` now
refuses, and it has been running at **medium** all along. Arm B was on **high** until 2026-08-20
(see the two-spellings finding). Both are `medium` now. Every cross-arm comparison from before that
date carries this difference on top of everything else.
