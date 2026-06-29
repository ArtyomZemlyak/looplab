# CLI reference

Every command is available as `looplab <command>` (after `pip install -e .`) or, equivalently,
`python -m looplab.cli <command>`.

```text
looplab run             Start (or continue) a run from a task file
looplab resume          Resume a crashed/incomplete run by replay
looplab inspect         Show the resolved config + best result
looplab replay          Pure fold of the event log → state (read-only)
looplab smoke           Ping the configured LLM endpoint (self-test)
looplab approve         Ratify a paused run (HITL / onboarding)
looplab bench           Capability self-benchmark across tasks
looplab ui              Serve the live React UI (needs the [ui] extra)
looplab export-mlflow   Log the champion to MLflow
looplab export-notebook Export the champion as a runnable .ipynb
```

Anything set on the command line can also be set via a `LOOPLAB_*` environment variable — see
[Configuration](configuration.md). CLI flags win over env vars for that run.

---

## `run`

Start a new run, or continue one if the output directory already has events.

```bash
looplab run TASK.json [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `TASK.json` | *(required)* | Path to a task JSON file (see [Tasks](tasks.md)) |
| `--out DIR` | `runs/run_local` | Run directory (created if missing) |
| `--max-nodes N` | `8` | Node (candidate) budget for the search |
| `--backend toy\|llm` | `toy` | Role backend: offline optimizer or a live LLM |
| `--model ID` | `qwen3:8b` | LLM model id (when `--backend llm`) |
| `--developer-backend NAME` | `default` | Delegate the Developer to `opencode` / `aider` / `goose` / `continue` |
| `--agent-cmd PATH` | — | Override the external agent's launcher/path |
| `--validate-agent / --no-validate-agent` | on | Validate external-agent output, retry with feedback, fall back to the in-house Developer |
| `--agent-patch-gate / --no-agent-patch-gate` | on | Run the agent in a git worktree and surface-gate its diff |
| `--agent-surface GLOBS` | `*.py` | Comma-separated edit-surface allow-list for the agent |
| `--knowledge-dir DIR` | — | Notes directory for agentic retrieval (grep/kb_search/read tools) |
| `--memory-dir DIR` | — | Cross-run case-memory directory |
| `--max-seconds SECS` | — | Wall-clock budget; the run aborts cleanly when exceeded |
| `--ablate-every N` | `0` | Run ablation-driven refinement every N improvements (0 = off) |
| `--confirm-top-k K` | `0` | Confirm the top-k candidates under multiple seeds before finishing |
| `--confirm-seeds N` | `0` | Number of seeds for the confirmation pass |
| `--require-approval` | off | HITL: pause for `approve` before finishing |

> `--crash-after N` is a hidden test hook that hard-exits after N evaluations (used to demonstrate
> crash-resume).

**Examples**

```bash
looplab run examples/toy_task.json --out runs/demo --max-nodes 14
looplab run examples/code_regression_task.json --backend llm --max-nodes 6
looplab run examples/regression_task.json --backend llm \
    --knowledge-dir examples/knowledge --max-nodes 6
looplab run examples/repo_task.json --backend llm --developer-backend opencode
```

---

## `resume`

Resume a crashed or incomplete run by re-entering the loop. State is rebuilt by replaying the event
log, so resume continues from the exact frontier with no duplicated or lost work.

```bash
looplab resume RUN_DIR [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Existing run directory to resume |
| `--task-file PATH` | the run's `task.snapshot.json` | The task file the run was started with |
| `--max-nodes N` | from the snapshot | Override the node budget on resume |

The original run's settings are restored from `config.snapshot.json`, so run-only flags
(`--require-approval`, trust mode, confirm settings, backend, …) are not silently dropped.

---

## `inspect`

Print the resolved config snapshot and the run's best result.

```bash
looplab inspect RUN_DIR
```

## `replay`

Read-only: fold the event log into the current state and print it as JSON. This is the
reproducibility check — it has no side effects.

```bash
looplab replay RUN_DIR
```

---

## `smoke`

Ping the configured LLM endpoint as a startup self-test: it sends a text completion and a structured
(tool-call) request and reports whether each works.

```bash
looplab smoke [--model ID]
```

Use this before a `--backend llm` run to confirm the endpoint, model id, and tool-calling are wired
correctly.

---

## `approve`

Human-in-the-loop ratification of a paused run. It appends the matching event so `resume` continues.
It handles two pause points:

- An **onboarding eval spec** proposed by the agent (repo tasks, see [Tasks](tasks.md)).
- The **final-best node** when the run was started with `--require-approval`.

```bash
looplab approve RUN_DIR [--node-id N]
```

`--node-id` selects which node to approve (defaults to the current best).

---

## `bench`

Capability self-benchmark: run each task end-to-end and report best-metric / eval-seconds /
reward-hack flags. A regression test for *capability*, not just code.

```bash
looplab bench TASK.json [TASK2.json ...] [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `TASK.json ...` | *(required)* | One or more task files to benchmark |
| `--out DIR` | `runs/bench` | Output directory for the runs + `benchmark.json` |
| `--backend toy\|llm` | `toy` | Role backend |
| `--max-nodes N` | `8` | Node budget per task |

---

## `ui`

Serve the live React web UI over a directory of run dirs. Requires the `[ui]` extra
(`pip install -e ".[ui]"`). A separate read/control process — it tails the event log to SSE and
turns UI actions into appended control events; it never changes the engine.

```bash
looplab ui [--run-root DIR] [--host HOST] [--port PORT]
```

| Option | Default | Description |
|---|---|---|
| `--run-root DIR` | `runs` | Directory containing run subdirectories |
| `--host HOST` | `127.0.0.1` | Bind host |
| `--port PORT` | `8765` | Bind port |

See the [Web UI](ui.md) guide.

---

## `export-mlflow`

Log the run's champion (params / metrics / solution) to MLflow. Needs the optional `mlflow` package
(`pip install mlflow`).

```bash
looplab export-mlflow RUN_DIR [--tracking-uri URI] [--experiment NAME]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to export |
| `--tracking-uri URI` | local `./mlruns` | MLflow tracking URI |
| `--experiment NAME` | — | MLflow experiment name |

## `export-notebook`

Export the run's champion solution as a runnable Jupyter notebook.

```bash
looplab export-notebook RUN_DIR [--out champion.ipynb]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to export the champion from |
| `--out PATH` | `<run>/champion.ipynb` | Output `.ipynb` path |
