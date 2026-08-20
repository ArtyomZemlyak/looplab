# 44. Running the AlgoTune arm: every deviation, dependency and trap (2026-08-20)

What a second machine, or a later reader, needs in order to reproduce these numbers and to know
what was changed outside this repository to get them. Companion to
[doc 43](43-benchmark-landscape-and-local-plan-2026-08-19.md), which covers *why* AlgoTune and what
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
