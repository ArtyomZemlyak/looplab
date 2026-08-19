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
| `.baseline_cache.json` | Written at runtime; per-task reference timings. Not committed. |

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
