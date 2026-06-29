# Quickstart

This walks through your first run — offline first, then driven by a real LLM.

## 1. Run a task offline

No LLM and no network are required. The default `toy` backend uses a deterministic optimizer, so
you can see the full loop work in seconds:

```bash
looplab run examples/toy_task.json --out runs/demo --max-nodes 14
```

What just happened:

1. The engine created the run directory `runs/demo/`.
2. It drafted candidate solutions, ran each in a sandbox, scored it, and refined the best.
3. Every step was appended to `runs/demo/events.jsonl` (the source of truth).
4. It printed the **best** node and its metric.

## 2. Read the result

```bash
looplab inspect runs/demo     # resolved config snapshot + best node/metric/params
looplab replay  runs/demo     # rebuild the full run state purely from the event log
```

`inspect` is the quick "what did I get?"; `replay` proves the run is reproducible — it folds the
append-only log into the same state, with no side effects.

Open **`runs/demo/tree.html`** in a browser for a static lineage tree of every candidate the loop
explored and how they descend from one another.

### What's in a run directory

```
runs/demo/
├── events.jsonl          # append-only event log — the source of truth
├── config.snapshot.json  # the exact resolved settings (secret-masked)
├── task.snapshot.json    # a verbatim copy of the task (makes the run self-describing)
├── tree.html             # static lineage view
└── spans.jsonl           # diagnostic trace spans (never read by replay)
```

## 3. Run a real ML task

```bash
looplab run examples/regression_task.json --out runs/reg --max-nodes 14
```

This selects a polynomial degree + ridge λ by 5-fold cross-validation. The loop discovers the right
model complexity (the example's true degree is 2) from a profiled dataset.

Browse the [Task reference](tasks.md) for classification, time-series, MLE-bench, and repo tasks.

## 4. Drive it with a live LLM

Point LoopLab at any OpenAI-compatible endpoint. Using local Ollama:

```bash
ollama pull qwen3:8b
looplab smoke                                                   # verify endpoint + tool-calling
looplab run examples/code_regression_task.json --backend llm --max-nodes 6
```

With `--backend llm`, the model is also the **Developer**: it writes a complete numpy script, the
loop runs it in the sandbox, and when a script crashes the **self-repair operator** hands the
failing code + stderr back to the model to fix — the real *invent → implement → run → repair* loop.

Configure the endpoint with environment variables (or `.env`):

```bash
export LOOPLAB_BACKEND=llm
export LOOPLAB_LLM_BASE_URL=http://localhost:11434/v1     # Ollama default
export LOOPLAB_LLM_MODEL=qwen3:8b
# export LOOPLAB_LLM_API_KEY=sk-...                       # for hosted endpoints
```

See [LLM & coding agents](llm-and-agents.md) for hosted models, per-role models, and delegating the
Developer to an external coding agent.

## 5. Crash & resume

The event log makes a run resilient to a hard kill — it continues from the exact frontier:

```bash
looplab run examples/toy_task.json --out runs/c --max-nodes 12 --crash-after 3
#   -> hard-exits mid-run (like kill -9)
looplab resume runs/c --task-file examples/toy_task.json --max-nodes 12
#   -> replays the log and finishes; no duplicated or lost work
```

`resume` can read the task from the run's own `task.snapshot.json`, so `--task-file` is optional
when resuming a run started by `looplab run`.

## Next steps

- Tune behavior: the [Configuration](configuration.md) reference.
- Every command and flag: the [CLI reference](cli-reference.md).
- Watch a run live in the browser: the [Web UI](ui.md).
- Understand the machinery: [Concepts](concepts.md).
