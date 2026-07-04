# CLI reference

Every command is available as `looplab <command>` (after `pip install -e .`) or, equivalently,
`python -m looplab.cli <command>`.

```text
looplab init            Scaffold a documented looplab.yaml config template
looplab run             Start (or continue) a run from a config/task file or --goal/--kind
looplab resume          Resume a crashed/incomplete run by replay
looplab inspect         Show the resolved config + best result
looplab replay          Pure fold of the event log → state (read-only)
looplab smoke           Ping the configured LLM endpoint (self-test)
looplab approve         Ratify a paused run (HITL / onboarding)
looplab bench           Capability self-benchmark across tasks
looplab ui              Serve the live React UI (needs the [ui] extra)
looplab tui             Terminal control plane: start/steer runs by chat (no browser)
looplab export-mlflow   Log the champion to MLflow
looplab export-notebook Export the champion as a runnable .ipynb
```

Anything set on the command line can also be set via a `LOOPLAB_*` environment variable or the
`settings:` block of a config file — see [Configuration](configuration.md). Precedence: `--set`/flags
win over the file, which wins over env vars, for that run. Add `--version` to print the version.

---

## `init`

Scaffold a documented config template (YAML) you can edit and run. The template leads with the task
and the knobs most runs touch (each commented), then lists every remaining setting at its default —
so it doubles as living documentation.

```bash
looplab init [--out looplab.yaml] [--kind dataset] [--force]
looplab run looplab.yaml
```

---

## `run`

Start a new run, or continue one if the output directory already has events. Ways to say what to
solve:

```bash
looplab run --goal "predict target; data is in ~/proj/data"   # Genesis authors the whole task
looplab run config.yaml                          # one file: task + settings + out
looplab run task.json --max-nodes 20             # a bare task file + flags (legacy)
looplab run --kind dataset --goal "..." -s backend=llm        # pin the kind, Genesis fills the rest
```

A config file may be **unified** (top-level `task:` / `settings:` / `out:` keys) or a **bare task**
(the legacy format — the whole file is the task). YAML and JSON are both accepted.

**Genesis (author the task from a plain goal).** Pass `--goal` and the LLM authors the task — the
headless counterpart of the Web UI's "New run" planner. It announces its choice (`Genesis -> kind=…`)
before launching, and:

- picks the `kind` from your words — *or* stays within the kind you **pin** with `--kind` (it doesn't
  skip Genesis, it constrains it; what the run does within a kind depends on the model);
- reads **where your data lives** straight from the goal — one path or several, a file or a folder —
  and authors the data mounts, so you don't need `--data` (it remains an optional shortcut);
- defaults the backend to `llm` for a generative kind (`dataset`/`repo`/`mlebench_real`/…); offline
  kinds (`quadratic`/…) keep their default and still run with no model.

Genesis needs a reachable model (it reasons about your goal). Add `--no-genesis` to build the task
from `--kind`/`--set` alone (offline), or run a complete file with no `--goal`.

| Option | Default | Description |
|---|---|---|
| `[CONFIG\|TASK]` | *(optional)* | Config or task file (YAML/JSON). Omit it and build the task from the flags below. |
| `--goal TEXT` | — | Task goal in plain words (build a task with no file) |
| `--kind NAME` | — | Task kind (`quadratic`, `dataset`, `repo`, … — see [Tasks](tasks.md)). With `--goal` it **pins** the kind for Genesis; omit it to let Genesis pick. |
| `--genesis / --no-genesis` | on | With `--goal`, let the LLM author the task (pinning to `--kind` if given, and reading data locations from your words). `--no-genesis` builds it from `--kind`/`--set` alone. |
| `--direction min\|max` | — | Optimization direction |
| `--data PATH` | — | Shortcut for a **dataset**'s data path or a **repo**'s path (rejected for other kinds); under Genesis you can instead name the location(s) in `--goal` |
| `-s, --set KEY=VALUE` | — | Override **any** engine setting (repeatable); same keys as `settings:` / `LOOPLAB_*` |
| `--out DIR` | the file's `out:` or `runs/run_local` | Run directory (created if missing) |
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
looplab init && looplab run looplab.yaml                  # scaffold a config, edit, run
looplab run --kind quadratic --goal "minimize x^2+y^2" --direction min   # no file
looplab run examples/toy_task.json --out runs/demo --max-nodes 14
looplab run examples/toy_task.json -s policy=asha -s n_seeds=5            # --set any setting
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

## `tui`

A chat-first **terminal control plane** — the most-used slice of the web UI, no browser needed. From
one dashboard you can:

- see every run at a glance (status · nodes · best metric · age), **auto-refreshing live** so changes
  show up the instant they happen,
- **describe a goal** and the boss plans + launches a run (the genesis flow), and
- open a run to see its **live** status and **chat with the boss to steer it** — free text becomes a
  plan the run applies (the same action-router the web Dock uses). Action plans and destructive
  controls ask for **confirmation** first: apply all, pick a subset (e.g. `1,3`), or cancel.

Just run bare **`looplab`** (no command) to open it, or `looplab tui` explicitly.

It is a thin HTTP client of the same server `looplab ui` serves (ADR-18). When you don't pass
`--server`, it reuses a local server if one is already up, otherwise it auto-launches one (API only —
no React build) and stops it on exit. Point it at a remote/shared server with `--server`.

```bash
looplab                       # bare command opens the TUI
looplab tui [--server URL] [--run-root DIR]
```

The live auto-refresh activates on a real terminal; piped/non-interactive stdin falls back to a plain
prompt (no redraws), so scripts stay deterministic.

| Option | Default | Description |
|---|---|---|
| `--server URL` | *(auto)* | URL of a running server, e.g. `http://127.0.0.1:8765`. Omit to reuse/auto-launch a local one |
| `--run-root DIR` | `runs` | Run-dir root, used only when auto-launching a server |

Auto-launching needs the `[ui]` extra (`pip install -e ".[ui]"`); pointing at an already-running
server needs nothing beyond the core install. Honours `LOOPLAB_UI_TOKEN` for token-gated servers.

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

## `harden`

Harden the reward-hack evaluator via a hacker–fixer–solver loop. Grows a persisted exploit ruleset
at `<memory_dir>/exploits.jsonl`: a hacker proposes eval exploits, a fixer turns each one the
current detector misses into a durable regex, and a solver guardrail rejects any rule that would
flag an honest solution. Every future run with this `memory_dir` + `reward_hack_detect` loads the
suite. Deterministic seed corpus — fully offline, no model needed.

```bash
looplab harden MEMORY_DIR [--rounds 1]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Memory dir; the exploit suite lives at `<memory_dir>/exploits.jsonl` |
| `--rounds N` | `1` | Hacker/fixer iterations |

## `tensorboard`

Serve TensorBoard over a run's per-node training logs — online curves for all metrics the training
framework logged, one comparable run per experiment. Needs `tensorboard` installed.

```bash
looplab tensorboard RUN_DIR [--port 6006] [--host 0.0.0.0]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run dir; its `nodes/` hold each experiment's training logs |
| `--port N` | `6006` | Port to serve on |
| `--host ADDR` | `0.0.0.0` | Bind address |

## `build-ui`

Build the React UI bundle (`ui/dist`) so `looplab ui` can serve it. Runs `npm ci` (first build) +
`npm run build` in the UI source tree. Normally not needed — `looplab ui` builds on demand — but
handy for CI or a warm-up step.

```bash
looplab build-ui [--force]
```

| Option | Default | Description |
|---|---|---|
| `--force` | off | Rebuild even if a bundle already exists |
