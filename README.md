# LoopLab

> An autonomous ML/DS research engine. Give it a goal; it **invents → implements → tests → improves** candidate solutions in a loop and returns the best *verified* result.

[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1700%2B-brightgreen.svg)](#testing)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#license)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-0f9c8c.svg)](https://artyomzemlyak.github.io/looplab/)

📖 **[Documentation site](https://artyomzemlyak.github.io/looplab/)** · 🗺️ **[Architecture infographic](https://artyomzemlyak.github.io/looplab/guide/architecture/)** — the whole agent, every component and stage, in one picture.

LoopLab runs a closed research loop: a **Researcher** proposes ideas, a **Developer** writes the
code, a sandbox runs it, an evaluator scores it, and the loop refines and **merges** the best
candidates — repeating until the budget runs out. On a fresh repo node the Developer works in three
phases — **stages → plan → implement** (declare the eval pipeline — skipped when the operator already
declared `cmd.stages` — decompose into atomic steps, then write the code). Every step is appended to an **event log that is the single source of truth**, so a
run is fully **reproducible and crash-resumable by replay**.

It runs **fully offline with zero external services** (no API keys, no Docker) on a local task, and
scales up to driving a live LLM, working inside a real repo, or grading actual Kaggle competitions.

## Architecture

Three planes and their connections — **magenta marks where the LLM / agent is invoked**, the engine
plane is deterministic, and the Search / Memory / Knowledge stores feed the loop over the append-only
`events.jsonl` spine. Full walkthrough on the [documentation site](https://artyomzemlyak.github.io/looplab/guide/architecture/).

![LoopLab architecture — one-pager schema](docs/infographic/architecture-one-pager.svg)

## Key features

- **Closed research loop** — a Researcher proposes, a Developer writes the code, a sandbox runs it, an evaluator scores it, and the loop refines and merges the best candidates. See [Concepts](docs/guide/concepts.md).
- **Event log as the single source of truth** — every step is appended to an append-only log, so runs are fully reproducible and **crash-resumable by replay**. See [Concepts](docs/guide/concepts.md).
- **Offline, or any OpenAI-compatible LLM** — no keys or Docker for local runs; change `base_url` to drive it with Ollama, vLLM, SGLang, or OpenAI. See [LLM & coding agents](docs/guide/llm-and-agents.md).
- **Describe the task in words** — Genesis (the LLM planner) authors the whole task from a `--goal`, including where your data lives. See [Generating code](docs/guide/generating-code.md).
- **Nine task adapters** — from a toy objective to your own dataset, an existing repo, or real Kaggle competitions. See [Tasks](docs/guide/tasks.md).
- **The agent writes and repairs the code** — on a fresh repo node the Developer runs three phases (**stages → plan → implement**): it declares the eval pipeline, decomposes the work into atomic steps, then writes the code; a self-repair operator feeds failing code plus stderr back to fix it in one focused session. See [Generating code](docs/guide/generating-code.md).
- **Cross-run memory and knowledge** — cases, lessons, causal meta-notes, skills, and a knowledge base accumulate across runs, both injected into prompts and **agentically retrievable**. See [Memory & knowledge](docs/guide/memory.md).
- **Adaptive search** — MCTS/ASHA policies, a novelty gate, and stagnation-driven broadening of the idea space. See [Concepts](docs/guide/concepts.md).
- **Trust tiers and sandbox** — a subprocess sandbox by default (no Docker), a `--network none` Docker tier for untrusted code, and reward-hack / leakage gates on scoring. See [Deployment](docs/guide/deployment.md).
- **Live web UI and terminal control plane** — a React control plane with a full execution trace, or steer runs by chat from the terminal. See [Web UI](docs/guide/ui.md).
- **Verified, returnable results** — held-out grading, MLE-bench scoring, and a returnable [live-scenario suite](docs/guide/live-scenarios.md); export the champion to MLflow or a notebook.

---

## Installation

```bash
pip install -e .                 # core engine + CLI
pip install -e ".[ui]"           # + live React web UI  (FastAPI/uvicorn)
pip install -e ".[otel]"         # + OpenTelemetry span export
pip install -e ".[dev]"          # + test deps (pytest, httpx)
```

Requires **Python ≥ 3.11**. Core dependencies are small and pure-Python (`pydantic`, `orjson`,
`anyio`, `typer`). Installing exposes a `looplab` command; you can also run `python -m looplab.cli`.

## Quick start

```bash
# 1. Run a toy optimization task offline (no LLM, no network) — no file needed:
looplab run --no-genesis --kind quadratic --goal "minimize (x-3)^2+(y+1)^2" --direction min --out runs/demo

# 2. Inspect the result and verify reproducibility.
looplab inspect runs/demo          # resolved config + best result
looplab replay  runs/demo          # rebuild full state from the event log

# 3. A real ML task: polynomial-degree + ridge selection via 5-fold CV.
looplab run examples/regression_task.json --out runs/reg --max-nodes 14
```

### Three ways to configure a run

You don't have to hand-write JSON. Pick whichever fits:

```bash
# a) Just describe it — Genesis (the LLM) authors the whole task from your words, including where
#    your data lives. No file, no --kind, no --data:
looplab run --goal "predict the target column; my data is in ~/proj/data and ~/extra/feats.csv"

# b) One readable YAML file — what to solve AND how. Scaffold a documented template, edit, run:
looplab init                       # writes looplab.yaml (every setting, commented)
looplab run looplab.yaml

# c) Pin the kind but still let Genesis fill the rest:
looplab run --kind dataset --goal "predict target, data in data.csv" -s max_nodes=20

# d) A bare task file + flags (the original style still works):
looplab run examples/toy_task.json --max-nodes 14
```

When you pass `--goal`, **Genesis** (the same "New run" planner the Web UI uses) authors the task for
you: it picks the `kind` — `dataset`, `repo`, `mlebench_real`, … — and reads your words for where the
data lives (one path or several, a file or a folder), so you don't pre-format anything. `--kind`
**pins** the kind and lets Genesis fill the rest within it; `--data` is an optional shortcut for the
path. Add `--no-genesis` to build the task from `--kind`/`--set` alone (offline, no model).

`-s/--set key=value` overrides **any** engine setting (full parity with the `settings:` block and the
`LOOPLAB_*` env vars). A unified `looplab.yaml` looks like:

```yaml
out: runs/demo
task:
  kind: dataset
  goal: predict `target` from the features
  direction: max
  data_path: data.csv
settings:
  backend: llm
  max_nodes: 20
```

Open `runs/demo/tree.html` for a static lineage view of every candidate the loop tried.

## Run with a real LLM

The Researcher/Developer can be driven by **any OpenAI-compatible endpoint** (Ollama, vLLM, SGLang,
OpenAI) — it's a `base_url` change, not a code change. The example below uses local Ollama:

```bash
ollama pull qwen3:8b
looplab smoke                                                   # verify endpoint + tool-calling
looplab run examples/code_regression_task.json --backend llm --max-nodes 6
```

With `--backend llm`, the model writes a complete solution, the loop runs it in the sandbox, and a
self-repair operator hands failing code + stderr back to the model to fix. Point it at a hosted
model by setting `LOOPLAB_LLM_BASE_URL` / `LOOPLAB_LLM_MODEL` / `LOOPLAB_LLM_API_KEY`.

## Task types

A task is a small JSON file describing what you **have** — `repo` / `dataset` / `cmd` /
`kaggle`/`competition` / `benchmark` — and the engine infers the adapter (an explicit legacy `kind`
still works). The fields desugar to nine adapters:

| `kind` | What it optimizes | Example |
|---|---|---|
| `quadratic` | A toy numeric objective (offline default) | `examples/toy_task.json` |
| `regression` | Polynomial + ridge model selection via CV | `examples/regression_task.json` |
| `classification` | Tune a classifier for K-fold CV accuracy | `examples/classification_task.json` |
| `timeseries` | Forecaster smoothing/seasonality via backtest | `examples/timeseries_task.json` |
| `code_regression` | LLM **writes the code** that fits the model | `examples/code_regression_task.json` |
| `mlebench` | Competition-shaped task with a private held-out grader | `examples/mlebench_task.json` |
| `mlebench_real` | **Real Kaggle competitions** scored by the official grader | `examples/mlebench_real_spooky.json` |
| `repo` | Edit/tune an **existing repo**; success = the repo's own eval | `examples/repo_task.json` |
| `dataset` | Point at your data; LLM **writes the whole solution** + picks the metric | `examples/dataset_task.json` |

See the [Task reference](docs/guide/tasks.md) for every field and more examples.

## CLI

```bash
looplab init                             # scaffold a documented looplab.yaml (config-as-docs)
looplab run     [CONFIG|TASK] [-s k=v]   # YAML/JSON config, a bare task, or --goal/--kind (no file)
looplab resume  RUN_DIR                  # continue a crashed/incomplete run by replay
looplab inspect RUN_DIR                  # resolved config snapshot + best result
looplab replay  RUN_DIR                  # pure fold of the event log → state (read-only)
looplab smoke                            # ping the configured LLM endpoint
looplab approve RUN_DIR                  # ratify a paused run (HITL / onboarding)
looplab bench   TASK.json ...            # capability self-benchmark across tasks
looplab ui                               # serve the live React UI (auto-builds the bundle; needs [ui])
looplab          # (or `looplab tui`) terminal control plane: start/steer runs by chat, no browser
looplab export-mlflow    RUN_DIR         # log the champion to MLflow
looplab export-notebook  RUN_DIR         # export the champion as a runnable .ipynb
```

Full flag-by-flag reference: [CLI reference](docs/guide/cli-reference.md).

## Crash & resume (the keystone)

Because the event log is the source of truth, a run survives a hard kill and continues from the
exact frontier — no duplicated or lost work:

```bash
looplab run examples/toy_task.json --out runs/c --max-nodes 12 --crash-after 3
#   -> hard-exits (code 137) mid-run, like kill -9
looplab resume runs/c --task-file examples/toy_task.json --max-nodes 12
#   -> replays the log, continues from the frontier, finishes cleanly
```

## Docker is optional

The sandbox tier is chosen by **trust mode**, not your environment:

- **`trusted_local`** (default) — `SubprocessSandbox`: process isolation + timeout + tree-kill +
  output caps. No Docker, no daemon. This is the whole local CLI.
- **`untrusted`** — `DockerSandbox` (`--network none`): a real boundary, needed only when you
  execute untrusted code on shared infra (e.g. a hosted multi-tenant UI). Set `LOOPLAB_TRUST_MODE=untrusted`.

A one-command Docker Compose stack (LLM + UI + engine) is available for the hosted scenario — see
[Deployment](docs/guide/deployment.md).

## Documentation

The full guide lives in **[`docs/guide/`](docs/guide/index.md)**:

| Guide | Contents |
|---|---|
| [Installation](docs/guide/installation.md) | Requirements, extras, optional backends |
| [Quickstart](docs/guide/quickstart.md) | Your first run, offline → LLM-driven |
| [CLI reference](docs/guide/cli-reference.md) | Every command and option |
| [Configuration](docs/guide/configuration.md) | Every `LOOPLAB_*` setting, grouped |
| [Tasks](docs/guide/tasks.md) | All nine task kinds and their fields |
| [Generating train & test code](docs/guide/generating-code.md) | Let the agent write the code (Genesis-first); bring your own repo + data |
| [LLM & coding agents](docs/guide/llm-and-agents.md) | Backends, external agents, per-role models, reasoning |
| [Concepts](docs/guide/concepts.md) | Event log, replay, sandbox/trust, operators, gates, memory |
| [Memory & knowledge](docs/guide/memory.md) | Every memory type (cases, lessons, meta-notes, skills, KB), the methodologies, and agentic retrieval |
| [Web UI](docs/guide/ui.md) | The live React control plane |
| [Deployment](docs/guide/deployment.md) | Docker Compose, the untrusted tier |
| [Live scenarios](docs/guide/live-scenarios.md) | Situational end-to-end tests of the main features — a returnable collection |
| [MLE-bench runbook](docs/MLEBENCH.md) | Running real Kaggle competitions |

Design records (the *why* behind the architecture) are in [`docs/00-INDEX.md`](docs/00-INDEX.md).

## Testing

```bash
python -m pytest -q          # ~1.7k tests, fully offline, a few minutes
```

Live-LLM and external-agent tests auto-skip when no endpoint/agent is configured, so the suite runs
fully offline.

## License

MIT — see [LICENSE](LICENSE).
