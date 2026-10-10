# CLI walkthrough

The following steps run offline first, then use a real LLM when you configure one.

## 1. Run a task offline

No LLM and no network are required. The demo file sets the offline `toy` backend itself, so you can
see the full loop work in seconds with no flags:

```bash
looplab run examples/demo.yaml
```

The same task as a bare JSON file needs the backend on the command line, because `backend` defaults
to `llm` (a real run wants a live model):

```bash
looplab run examples/toy_task.json --out runs/toy --max-nodes 14 --backend toy
```

What just happened:

1. The engine created the run directory `runs/demo/`.
2. It drafted candidate solutions, ran each in a sandbox, scored it, and refined the best.
3. Every domain decision was appended to `runs/demo/events.jsonl` (the replay authority for `RunState`).
4. It printed the **best** node and its metric.

## 2. Read the result

```bash
looplab inspect runs/demo           # best experiment, metric, stop reason, trust, comparability
looplab replay  runs/demo --summary # rebuild the run's state from the event log alone
```

`inspect` is the quick "what did I get?" (`--config` adds the launch settings,
[CLI reference](cli-reference.md)); `replay` rebuilds the run's state from the append-only log with no
side effects — `--summary` prints a few lines, without it the full state as JSON.

Open **`runs/demo/tree.html`** in a browser for a static lineage tree of every candidate the loop
explored and how they descend from one another.

### What's in a run directory

```
runs/demo/
├── events.jsonl          # append-only event log — replay authority for RunState
├── config.snapshot.json  # the exact resolved settings (secret-masked)
├── task.snapshot.json    # canonical resolved task after overlays + comparison normalization; resume input
├── engine.lock           # single-writer lock — taken for the whole run
├── nodes/                # one workdir per candidate (its code, logs, artifacts)
├── tree.html             # static lineage view
├── trace.json            # end-of-run trace projection
├── readmodel.sqlite      # derived read model (rebuildable; `looplab readmodel RUN_DIR`)
├── AGENTS.md             # what the engine tells a coding agent about this task
└── spans.jsonl           # diagnostic trace spans (never read by replay)
```

Plus `*.lock` files and a `.looplab-fence/` directory you can ignore.

## 3. Run a real ML task

Still offline — `--backend toy` again, because you have not pointed LoopLab at a model yet (that is
step 4). Without it the run is **refused before it starts** by the endpoint preflight (one message,
[exit code 2](cli-reference.md#exit-codes-a-refusal-is-not-a-crash)), since `backend` defaults to
`llm`:

```bash
looplab run examples/regression_task.json --out runs/reg --max-nodes 14 --backend toy
```

This searches a polynomial degree and a ridge λ for the lowest 5-fold cross-validated error on a
profiled dataset. The degree it settles on is in the BEST line's params (applied rounded to an integer).

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
# A hosted endpoint takes a key, and the key names the endpoint it belongs to (both lines):
# export LOOPLAB_LLM_API_KEY=sk-...
# export LOOPLAB_LLM_API_KEY_BASE_URL=$LOOPLAB_LLM_BASE_URL   # the same URL as above
```

From here on you can drop `--backend toy`. LoopLab probes each configured endpoint once before a run
starts and refuses the run outright if it will not serve — rather than degrading to empty fallback
proposals that report success. The refusal names *which* of six causes it measured (`[unreachable]`,
`[throttled]`, `[credential]`, `[model]`, …) and prints only the remedies that can reach that one, so
a rate-limited endpoint is never diagnosed as an absent one — see
[endpoint preflight](llm-and-agents.md#endpoint-preflight-before-a-run-starts).

See [LLM & coding agents](llm-and-agents.md) for hosted models, per-role models, and delegating the
Developer to an external coding agent.

## 5. Crash & resume

The event log makes a run resilient to a hard kill — it continues from the durable replay frontier:

```bash
looplab run examples/toy_task.json --out runs/c --max-nodes 12 --crash-after 3 --backend toy
#   -> hard-exits mid-run: --crash-after is a hidden test flag that simulates kill -9
looplab resume runs/c --task-file examples/toy_task.json --max-nodes 12
#   -> replays the complete event prefix; recorded fulfillment receipts are not served twice
```

`resume` restores the launch settings from `config.snapshot.json`, so it stays offline without
repeating `--backend toy`.

`resume` can read the task from the run's own `task.snapshot.json`, so `--task-file` is optional
when resuming a run started by `looplab run`.

This guarantee covers replayed state and explicitly receipted operations. External effects and cross-run
sidecars use their own recovery gates; unreceipted work is not covered by a blanket exactly-once promise.

## Next steps

- Tune behavior: the [Configuration](configuration.md) reference.
- Every command and flag: the [CLI reference](cli-reference.md).
- Watch a run live in the browser: the [Web UI](ui.md).
- Understand the machinery: [Concepts](concepts.md).
