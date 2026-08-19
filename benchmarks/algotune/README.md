# AlgoTune arm — running LoopLab against AlgoTune, and against everyone else

[AlgoTune](https://github.com/oripress/AlgoTune) (MIT, [arXiv 2507.15887](https://arxiv.org/abs/2507.15887))
is 154 numerical/CS functions where the agent must produce code that matches a reference
implementation's output while running faster. It is the cleanest external arena LoopLab has on a
single box, for four reasons:

1. **The metric is a ratio measured locally.** `speedup = baseline_ms / optimized_ms`, both timed on
   the same machine in the same pass — so it self-normalises against hardware.
2. **The environment is the operator's job, not the agent's.** AlgoTune's agent has no shell and no
   `pip`; its whole command set is `ls`, `view_file`, `edit`, `delete`, `revert`, `reference`, `eval`,
   `eval_input`, `profile`, `profile_lines`, and the prompt *enumerates* the 27 packages it may use.
   So the benchmark measures algorithm work, not environment wrangling.
3. **It is CPU-bound**, so GPU-generation issues (see the FML-bench notes in
   [doc 43](../../docs/43-benchmark-landscape-and-local-plan-2026-08-19.md)) do not arise.
4. **It ships 17 models × 154 tasks = 2,595 reference solvers** under `results/`, which can be
   **re-timed on our machine** rather than compared against published numbers from someone else's.

## What is in here

| File | Purpose |
|---|---|
| `looplab_eval.py` | The eval bridge. Copies a candidate `solver.py` into `results/<model>/<task>/`, runs **AlgoTune's own** `evaluate_results.py`, prints `speedup` as stdout JSON. Holds the parity cache (below). |
| `make_task.py` | Generates a LoopLab `repo` task spec + workspace for one AlgoTune task. |
| `run_evaluator.py` | Runs AlgoTune's evaluator with a **disk-backed** baseline cache. Without it the reference pass is re-measured in a fresh interpreter on **every node** — see Parity below. |
| `compare_arms.py` | Summarises a campaign: arm A from `reports/agent_summary.json`, arm B from the LoopLab run's folded event log. A missing arm prints `--`, never `0`. |
| `.baseline_cache.json` | Written at runtime; the per-task AGGREGATE baseline (stabilises the denominator). Not committed. |
| `.baseline_times/` | Written at runtime; the per-INSTANCE reference timings (saves the wall clock). Not committed. |

## Setup (once)

AlgoTune is Linux-first and its own pins have rotted; the working recipe on this box is:

```bash
git clone --depth 1 https://github.com/oripress/AlgoTune.git
cd AlgoTune
uv venv --python 3.11 .venv          # requirements.txt needs >=3.11 despite the README saying 3.10
uv pip install -e .                  # resolve pyproject, NOT requirements.txt (it pins pot==1.0.0, which does not exist)
echo "OPENROUTER_API_KEY=sk-or-..." > .env
```

Use `uv`, not `pip`: pip's resolver thrashes on this dependency tree (measured: 464 MB downloaded
and 9 s of CPU across 40 minutes before making progress).

### Known upstream bug you must patch

`AlgoTuner/utils/isolated_benchmark.py` iterates `sys.modules.items()` while `inspect.getmembers()`
inside the loop triggers lazy imports and mutates it, raising
`RuntimeError: dictionary changed size during iteration`. This fails **every** benchmark run, so no
speedup can ever be recorded (measured: 224 occurrences in one run). There are **two** occurrences —
patch both:

```bash
sed -i 's/for module_name, module in sys.modules.items():/for module_name, module in list(sys.modules.items()):/g' \
    AlgoTuner/utils/isolated_benchmark.py
```

## Running an arm

**Reference arm** — AlgoTune's own agent, so the comparison has a same-model control:

```bash
cd AlgoTune && source .venv/bin/activate && set -a && source .env && set +a
./algotune.sh agent --standalone openrouter/deepseek/deepseek-v4-flash-0731 svm
```

Note the model name is the **full config key** from `AlgoTuner/config/config.yaml` — the lookup is an
exact `config["models"].get(name)`, not a suffix match.

**LoopLab arm:**

```bash
python benchmarks/algotune/make_task.py \
    --algotune-root /path/to/AlgoTune --task svm --out-dir /path/to/workspaces
looplab run /path/to/workspaces/algotune_svm.json --out runs/algotune-svm-looplab --backend llm
```

**Summarising a campaign:**

```bash
python benchmarks/algotune/compare_arms.py --algotune-root /path/to/AlgoTune --runs-root /path/to/camp-runs --reference
```

**Re-timing the shipped reference solvers on this machine:**

```bash
cd AlgoTune
python scripts/evaluate_results.py --models "GPT-5.4" "Claude Opus 4.6" --tasks svm
```

## Parity — read this before trusting any timing

AlgoTune's own loop pays the reference ("oracle") timing **once per run**: `BaselineManager` keeps it
in-process and every later `eval` is cheap. Measured here (RTX 5090, 2026-08-19), that one-time cost
is ~150 instances at ~3 s each — **about 30 minutes for a single task**.

A bridge that shells out once per candidate re-pays that on *every* node. The resulting slowdown
would be a property of the wiring, not of the agent under test, and granting more wall-clock does not
fix it because the overhead scales with node count.

So `looplab_eval.py` measures each task's baseline once, caches it, and scores later candidates as
`cached_baseline_ms / freshly_measured_solver_ms`. **This is parity restoration, not a protocol
deviation** — it is exactly what `BaselineManager` does inside an AlgoTuner run — but it *is* a
departure from a naive reading of the harness, so state it in any published methods note. Use
`--no-cache` to force full re-measurement.

### The trap has a second half, and caching the number does not close it

The cache above fixes the **denominator**. It does not fix the **cost**: `looplab_eval.py` shells out
to `evaluate_results.py`, which builds a fresh `BaselineManager` in a fresh interpreter — and that
class caches in `self._cache`, *process memory*. So the whole reference pass was still re-measured
per node; only its result was being thrown away.

Measured 2026-08-19: the pass advances at ~2.4 s/instance and one task's pass took **100 instances /
~15 minutes**. `run_evaluator.py` wraps `BaselineManager.get_baseline_times` in a disk cache keyed by
`(task, subset)` and runs the upstream script unmodified through `runpy`. It never caches across
tasks or across the train/test split — those are different reference sets — and honours
`force_regenerate`. Disable with `--no-baseline-cache`.

### The budget must be SPEND, not wall-clock

Arm A's own startup log settles this:

```
INFO - Configuration loaded successfully. Budget: $1.0000
INFO - Config loaded: spend_limit=1.0 total_messages=9999 max_messages_in_history=5
```

AlgoTuner is budgeted by **money**, with an effectively unlimited message count, and its ~15-minute
reference pass costs **$0** of it. A wall-clock cap therefore charges one loop for a measurement pass
neither one's *agent* performs — and on the 20-minute cap originally planned, arm B would have
completed zero evaluations.

**But $1 is not a budget for a cheap model.** Measured here: seven agent messages cost **$0.0071**,
so $1 buys ~1,000 messages; one svm run went 3,462 s / ~16 messages without approaching it. The
campaign uses **$0.02 on both arms** (~20 messages, ~1 h per task-arm): AlgoTuner's
`config.yaml global.spend_limit`, and LoopLab's `LOOPLAB_LLM_BUDGET_USD`.

### Three defects that made the bridge score 0.0 for everything

All three were found by validating end to end rather than by inspection, and any one of them alone
would have produced a campaign of zeros that looked like a bad agent.

1. **`RLIMIT_AS` killed every evaluation.** `validation_pool.disable_rlimit_as: false` caps VIRTUAL
   address space, which JAX/torch/BLAS reserve in tens of GB without touching. Every run died with
   *"A process in the process pool was terminated abruptly"* — at 14 GB and again at 30 GB, on a
   321 MB task and on a 28 KB one, with 45 GB free. Set `disable_rlimit_as: true`. Applies to both
   arms.

2. **The summary reader was keyed on fields nothing writes.** `evaluate_summary.json` is
   `{"discrete_log": {"BV4": {"final_speedup": "0.9963"}}}` — no `task_name`, no `speedup`, and the
   value is a **string**. `looplab_eval.py` searched for `task_name`/`speedup`, so it reported
   `speedup: 0.0` on a summary that had just been written successfully. It could never have returned
   a number, for any task, on any node.

3. **Wrapping `BaselineManager` in-process crashed the pool.** The persistent cache was first
   implemented by patching the class from the parent and running the script via `runpy`. Same task,
   same config: direct → `0.9963x`; through the wrapper → pool crash. The cache now ships as an
   on-disk patch (`patch_baseline_cache.py`), applied before anything imports the module.

Measured after all three: `discrete_log` scores end to end, and the cache takes a repeat evaluation
from **668 s to 215 s** (3.1x).

### The metric is noisy on small tasks, and both arms are noisy the same way

The same solver on `discrete_log` scored **1.0006** and then **1.4468** on consecutive runs. The
cache reuses the baseline from an earlier pass while re-timing the solver now, so machine drift no
longer cancels between numerator and denominator.

**This is not an asymmetry.** AlgoTuner's own `BaselineManager` does exactly the same thing inside a
run — reference measured once at the start, every later `eval` timed against it — so both arms carry
it. What follows is about how to READ the results: a single task's ratio is weak evidence, and the
aggregate over the 20 tasks is the comparison. `compare_arms.py` prints per-task rows precisely so a
single wild row cannot hide inside a mean.

### Bound `baseline_timeout`, or one bad candidate eats the campaign

Shipped default 60 s per instance. Measured 2026-08-19: after its 14th message, one arm-A run spent
**87 minutes on a single candidate** — 69 isolated benchmark runs, **zero problems completed** —
because a solver that times out costs 60 s × ~100 instances, i.e. up to 100 minutes for ONE
evaluation. That run was then cut by the campaign's wall-clock net and wrote **no `final_speedup` at
all**, so the pathological case does not merely slow a campaign, it empties it.

The campaign sets `benchmark.baseline_timeout: 10000`. It applies to **both** arms (both evaluate
through this harness), so it is parity-preserving, and it cannot flatter either one: a solver slower
than 10 s per instance on a task whose target is `oracle_time_limit: 100` ms is already 100x off and
scores 0 either way.

**Corollary for the wall-clock net:** it must sit far ABOVE what a task-arm needs. A run cut by the
clock writes nothing, so a binding net does not shorten the campaign — it deletes rows from it. The
campaign uses 4 h purely as a hung-process guard.

### The first eval of a task downloads its dataset

`evaluate_results.py` fetches from HuggingFace into `.hf_datasets/` (19 GB for the whole repo is not
unusual — `base64_encoding` alone is 19 GB, while `svm` is 24 MB and `discrete_log` is 28 KB). This
is a once-per-machine, per-task cost, but it means the first eval of a task can look hung. If you
are validating the bridge, **pick a small task**: an hour spent thinking the pipeline was broken here
was really just base64_encoding being enormous.

### Two parity choices that cost LoopLab something, on purpose

* **Reasoning effort `medium` on both arms.** Measured: medium 21.5 s/call, high 111.3 s/call, with
  quality at `high` *not* measured either way. Under any bounded budget `high` buys so few calls that
  the comparison is between two truncated runs.
* **Cross-run memory off.** LoopLab can read its own past runs and a shared memory store; AlgoTuner
  has no equivalent and each of its runs starts blind. Left shared, arm B would reach task 12 with
  eleven prior runs to mine — measuring a capability the other arm lacks rather than the loop. Each
  task gets its own run root and memory dir. This **discards a real LoopLab advantage**, which is the
  direction that cannot flatter us.

> **General rule this instance illustrates.** When the reference agent has an in-process cache and
> our integration is out-of-process, equal *budgets* are not equal *work*. Compare the cost structure
> of the two loops before trusting any timing — the same question applies to FML-bench (`conda run`
> per command) and to MLE-bench.

## Things that will bite you

- `evaluate_results.py`'s docstring says it "reads generation.json for baseline timings". **It does
  not.** `generation_data` is used only for the task list, task filtering and summary formatting; the
  baseline is always re-measured locally. (That property is what makes re-timing other agents' code
  here valid — it just should not be paid per node.)
- **100 % instance validity is required for any speedup at all.** A solver wrong on one instance
  scores 0, not a partial credit.
- `data/` starts empty and is regenerated per run; `./algotune.sh generate` persists it.
- `pkill -f 'AlgoTuner.main'` matches its **own** `bash -lc` command line and kills your shell. Use
  `pkill -f 'AlgoTuner[.]main'`.

## What this comparison does and does not isolate

The 17 shipped arms were all produced by *AlgoTuner's* loop driving different models, so re-timing
them compares **artifacts**; a LoopLab-vs-reference row mixes "different loop" with "different model".
The controlled comparison is **LoopLab vs AlgoTuner on the same model**, produced here. The 17 are
context, not controls.

| Arm | Loop | Model | Provenance |
|---|---|---|---|
| A | AlgoTuner | `deepseek-v4-flash-0731` | produced here |
| B | LoopLab | `deepseek-v4-flash-0731` | produced here — **the control** |
| ref ×17 | AlgoTuner | GPT-5.4, Opus 4.6, Gemini 3.1 Pro, R1, … | shipped, re-timed here |

## Model pinning

Use a **dated** OpenRouter slug and pin the provider, or the same "model" silently varies between
requests. Measured 2026-08-19: three unpinned calls to `deepseek/deepseek-v4-flash-0731` hit **two
different fp4 providers** and returned 96 / 17 / 96 completion tokens for one prompt; 24 endpoints
serve that slug at fp4/fp8/bf16. Pin in `AlgoTuner/config/config.yaml`:

```yaml
  openrouter/deepseek/deepseek-v4-flash-0731:
    api_key_env: "OPENROUTER_API_KEY"
    temperature: 0.0
    drop_params: true
    usage: {include: true}
    extra_body:
      provider:
        order: ["siliconflow/fp8"]
        allow_fallbacks: false      # without this, `order` is only a preference
```

### Bound the reasoning budget, or every call is a runaway

**Measured 2026-08-19 on `deepseek-v4-flash-0731`.** With no reasoning bound the model thinks
without limit — this endpoint's ceiling is `max_completion_tokens: 393216`, and nothing else stops
it. In one LoopLab run, **6 calls held 84 % of all completion tokens and 84 % of a 45-minute phase**;
the largest returned **66,459 completion tokens** from a 13,193-token prompt. An isolated probe of a
default call did not finish inside 10 minutes.

**Capping `max_tokens` instead is a trap that fails silently.** At `max_tokens: 4096`, all 4,096
tokens went to reasoning and the answer came back **empty** — the run would not crash, it would just
receive nothing and look like a stupid model.

| config | wall | completion | reasoning | answer |
|---|---:|---:|---:|---:|
| default | **>600 s (timeout)** | — | — | — |
| `max_tokens: 4096` | 40.2 s | 4,096 | **4,096** | **empty** |
| `reasoning: {enabled: false}` | 18.6 s | 2,861 | 0 | 5.4 KB |
| **`reasoning: {max_tokens: 2000}`** | **30.4 s** | 3,555 | 1,916 | 5.4 KB |
| `reasoning: {effort: medium}` | 23.0 s | 2,754 | 1,062 | 5.9 KB |

Use an explicit **`reasoning: {max_tokens: N}`** rather than `effort`: providers interpret an effort
level however they like, whereas a token budget reads the same on both arms — and parity requires
both arms carry the *same* bound. Disabling reasoning entirely also works but handicaps the model's
quality, which is not what we want to measure.

> **What the campaign actually uses, and why it is not this.** `reasoning: {max_tokens: N}` was
> measured NOT to hold on the real prompts (see below: 21,759 reasoning tokens against a 2,000
> budget), so the bound it promises is not one it delivers. The campaign pins `effort: medium` on
> both arms instead — the same value on both, which is what parity requires; the level itself is
> chosen because `high` is 5x slower for no measured quality gain.

Effect on the LoopLab arm after applying it: max completion 66,459 → **1,957**, gaps over 60 s
**6 → 0**, longest gap 1,089 s → **26.9 s**.

### Where the tokens actually go: the model solves the task inside its scratchpad, then throws it away

Traced through `spans.jsonl` on a real LoopLab run, 2026-08-19. **6 calls out of 89 held 84 % of all
completion tokens.** The largest:

```
duration 310 s | prompt 12,677 | completion 21,759
thinking:      64,000 characters  — a COMPLETE SMO solver, written out in full
visible output:   157 characters  + 3 tool calls
```

The model receives the task, the reference implementation and a set of evidence-gathering tools in
the same turn. It solves the whole problem in its reasoning trace, then — because the prompt asks it
to gather evidence first — emits only tool calls. **Verified: none of that reasoning is carried into
the next call's input.** The work is done and discarded.

This is not a defect either side introduced. It is what reasoning models do inside tool loops, and
the same model shows it on the AlgoTuner arm too (29 calls across 2 hours). **Both arms pay the same
tax, so the comparison stays valid — the cost is wall-clock, not money** (a run is cents).

**`reasoning: {max_tokens: N}` does not reliably bound it.** The parameter is accepted and sent, and
on synthetic prompts of the same shape it is honoured (23–119 reasoning tokens against a 2,000
budget) — but on the real prompts it is ignored (21,759 against the same budget). It holds when
there is little to think about and lapses when there is something to chew on.

If this ever needs fixing, disable reasoning **only on the evidence-gathering phases** and keep it
where the answer matters — and apply the same split to both arms, or the parity above is lost.

### Reasoning effort levels, measured

| effort | wall | note |
|---|---:|---|
| `low` | 20.8 s | |
| **`medium`** | **21.5 s** | the working setting |
| `high` | **111.3 s** | 5x slower; answer quality NOT measured, do not assume either way |
| `max`, `xhigh` | **hangs** | no response and no error — this killed one benchmark script outright |

### Provider selection: measured, and the answer is that it barely matters

13 providers, `effort=medium`, 3 calls each; then the top two re-measured with 6 calls because three
samples decide nothing:

| provider | 3-sample | **6-sample** |
|---|---|---|
| `coreweave/fp8` | 11.4 s / 162 tok/s | **21.1 s / 98 tok/s** |
| `siliconflow/fp8` | 20.4 s / 112 tok/s | **20.7 s / 106 tok/s** |

The three-sample run said coreweave was 1.8x faster. **The six-sample run says they are
indistinguishable** — spread is 16–38 s, so three samples can order them any way you like. Seven
providers all land around 20 s / ~100 tok/s.

What *does* differ is availability: `baseten` 0/3 (429), `novita` and `fireworks` 1/3, `baidu` and
`together` 2/3, and DeepSeek's own first-party endpoint returns **404** through OpenRouter. Pick on
stability, not on a speed difference that is not there.

Verify the pin rather than trusting it: published `uptime_last_30m` did not predict availability
here (DeepInfra at 99.0 % returned 502; Novita at 99.5 % hung for 300 s).
