# CLI reference

Every command is available as `looplab <command>` (after `pip install -e .`) or, equivalently,
`python -m looplab.cli <command>`.

```text
looplab init            Scaffold a documented looplab.yaml config template
looplab run             Start (or continue) a run from a config/task file or --goal/--kind
looplab resume          Resume/continue a run (crash, stopped, or finished) by replay
looplab stop            Stop a run: freeze it, NO wrap-up (resumable)
looplab finalize        Finalize a run: stop AND wrap up (report/lessons/cost)
looplab repair-log      Repair a mid-file-corrupted event log (FUSE/NFS/S3)
looplab inspect         Show the resolved config + best result
looplab replay          Pure fold of the event log → state (read-only)
looplab timings         Per-node wall-clock breakdown (LLM / eval / repair / tools)
looplab concept-coverage Concept-graph coverage + uncovered-region alarm (PART IV D5)
looplab asset-brief     Prior-art & on-disk asset brief for a task repo (PART IV D1)
looplab lock-in         Action-space lock-in detector (PART IV D7)
looplab board-dedup     Taxonomy-aware hypothesis-board dedup analysis (PART IV D4)
looplab research-targets Axis-structured deep-research targets from coverage (PART IV D2)
looplab cross-run-concepts Portfolio overview of concepts tried across runs (PART IV cross-run Step 3)
looplab claims          Lessons → evidence-grounded claims (support/oppose) (PART IV cross-run Step 4)
looplab smoke           Ping the configured LLM endpoint (self-test)
looplab approve         Ratify a paused run (HITL / onboarding)
looplab bench           Capability self-benchmark across tasks
looplab ui              Serve the live React UI (needs the [ui] extra)
looplab tui             Terminal control plane: start/steer runs by chat (no browser)
looplab export-mlflow   Log the champion to MLflow
looplab export-notebook Export the champion as a runnable .ipynb
looplab harden          Grow the reward-hack exploit ruleset (hacker–fixer–solver)
looplab tensorboard     Serve TensorBoard over per-node training logs
looplab build-ui        Build the React UI bundle (ui/dist)
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
  kinds (`quadratic`/…) keep their default and still run with no model. (The Web UI's genesis card
  applies the same default — an explicit backend, wherever set, always wins.)

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
looplab run --no-genesis --kind quadratic --goal "minimize x^2+y^2" --direction min   # no file, no LLM
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

## `stop`

Freeze a run **without** finalizing it — no end-of-run report, lessons, or cost roll-up. A live
engine breaks on its next loop iteration; the run stays resumable (`looplab resume`) or you can
`finalize` it later.

```bash
looplab stop RUN_DIR
```

## `finalize`

Stop a run **and** run the end-of-run wrap-up (report, cross-run lessons/case, cost roll-up,
`tree.html`). Works whether the run is live or already `stop`ped, and is idempotent. If no engine is
driving the run, `finalize` re-enters the loop itself to produce the wrap-up.

```bash
looplab finalize RUN_DIR
```

---

## `repair-log`

Repair an event log with a **mid-file corruption** — a complete corrupt line followed by more valid
records. `events.jsonl` is append-only and a single local writer never produces this, but a FUSE / NFS
/ S3-backed run directory can flip a byte in the middle. Replay stops at the first bad line, so
`run`/`resume` **fail closed** (they refuse to append behind the boundary, which would grow a durable
tail that fold can never see) and point you here. `repair-log` backs up the original bytes to
`events.jsonl.corrupt-<ts>.bak`, atomically truncates the log to its last valid boundary (the
recoverable prefix), and records the repair as a `log_repaired` event. The dropped tail is preserved
in the backup for manual salvage. A torn *final* line (the normal crash-mid-append case) is tolerated
on read and needs no repair.

```bash
looplab repair-log RUN_DIR
# then: looplab resume RUN_DIR
```

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

## `timings`

Show where a run's wall-clock actually went, **per node** — LLM generations vs eval vs repair vs
tools — computed from the `duration_s` of each span in `spans.jsonl`. Answers "what is this run
spending its time on right now" at a glance. Needs tracing on (the default); errors on a run with no
`spans.jsonl`.

```bash
looplab timings RUN_DIR [--node N]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory (reads its `spans.jsonl`) |
| `--node N` | all nodes | Restrict the breakdown to a single node id |

---

## `concept-coverage`

PART IV D5 (§21.11) read-only diagnostic. Folds a run's event log and tags each experiment with the
research **concepts** it touches over a concept **axis-DAG** (multi-label — one experiment can touch
`loss/decoupled-contrastive` *and* `regularization/r-drop`), then reports per-axis coverage, the dominant
concept / axis-clique **concentration**, and the standing **uncovered-region alarm** — the regions the
search footprint never entered (e.g. *"0 coverage in {negatives/external-mining, distillation/teacher-distill,
data/synthetic-queries} across all N experiments — direct the next proposals there (not just 'broaden')"*),
which fires from the first node rather than waiting for narrowing to accumulate. Read-only; never touches
selection.

**Agentic by default** (agentic-first concept): the LLM builds the map — it grows the concept vocabulary
from the actual experiments (reading each node's code/logs), so **it sends node code/logs to the configured
LLM endpoint by default**. Pass `--offline` for the fully local, no-network deterministic heuristic (coarser:
needs a curated `--task-type` pack and cannot derive per-task importance). The five other Part IV diagnostics
below (`lock-in`, `board-dedup`, `research-targets`, `novelty-recall`, `lesson-guard`) share this
`--offline` opt-out contract; `asset-brief` is the exception — it stays offline-by-default (`--llm` opt-in)
because its agentic path is a heavier full tool-loop.

```bash
looplab concept-coverage RUN_DIR [--task-type dense-retrieval] [--offline] [--model ID] [--repo PATH]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to fold and diagnose |
| `--task-type NAME` | inferred from the run's `task_id` | Concept pack to SEED the agent's build (e.g. `dense-retrieval`); the LLM verifies/expands it, or builds from scratch when no pack matches |
| `--offline` | off (**default is the agentic build**) | Skip the LLM/network and use only the deterministic alias heuristic over the curated seed pack — a fast local fallback (needs a pack; no per-task importance) |
| `--model ID` | configured model | Override the model for the agentic build |
| `--repo PATH` | — | Task repo to ground the per-task uncovered-region derivation with a D1 prior-art brief |

---

## `asset-brief`

PART IV D1 (§21.2) offline diagnostic. Produces the seed-time **prior-art & available-assets brief** for
a task repo — the on-disk result tables, sibling checkpoints (metrics carried in their filenames), and
reusable trainer capabilities a fresh proposer would otherwise miss. The primary path (`--llm`) is an
**agent** that explores the repo with read-only tools and writes a grounded brief; the default is a
bounded, task-agnostic heuristic scan (its domain vocabulary is a pluggable per-task-type pack, opted in
via `--task-type`). Read-only — nothing is executed or written.

```bash
looplab asset-brief REPO [--task-type dense-retrieval] [--llm] [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `REPO` | *(required)* | Task repo to sweep for prior art & on-disk assets |
| `--task-type NAME` | generic | Task family whose capability vocabulary to apply (e.g. `dense-retrieval`); omit for a purely generic scan |
| `--llm` | off (offline scan) | Use the **agentic** brief (an LLM explores the repo with read-only tools) instead of the heuristic scan. Needs a reachable endpoint |
| `--model ID` | configured model | Override the model for `--llm` |

---

## `lock-in`

PART IV D7 (§21.8) offline analytic. Over the concept graph, finds the longest run of **consecutive**
experiments confined to one axis-region — the "same-lever streak" the flat coverage signal was blind to
(on the `rubertlite` replay it trips at ~node 29) — and fires when it exceeds the threshold. Read-only,
deterministic.

```bash
looplab lock-in RUN_DIR [--task-type NAME] [--threshold 5]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to fold and diagnose |
| `--task-type NAME` | inferred from `task_id` | Concept-graph skeleton (e.g. `dense-retrieval`) |
| `--threshold N` | `5` | Consecutive same-lever experiments that trip the alarm |

---

## `board-dedup`

PART IV D4 (§21.5) offline analytic. Tags the hypothesis board and surfaces the dominant **within-concept**
redundancy (merge aggressively — e.g. the DCL cluster) plus **cross-branch** look-alike pairs a blind
lexical/vector merge would wrongly collapse (keep distinct). Read-only; merges nothing.

```bash
looplab board-dedup RUN_DIR [--task-type NAME]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory whose hypothesis board to analyze |
| `--task-type NAME` | inferred from `task_id` | Concept-graph skeleton |

---

## `research-targets`

PART IV D2 (§21.3) offline analytic. Turns the coverage map into a ranked set of axis-structured
deep-research targets: **uncovered** axes first (the blind regions), **failed directions** re-framed as
"research a different implementation" (so the loop stops re-proposing the failed variant), then
**under-covered** axes. Read-only; produces the targets, runs no research.

```bash
looplab research-targets RUN_DIR [--task-type NAME] [--asset-repo PATH]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory whose coverage to target |
| `--task-type NAME` | inferred from `task_id` | Concept-graph skeleton |
| `--asset-repo PATH` | — | Task repo to ground the queries in the D1 asset brief (offline scan) |

---

## `cross-run-concepts`

PART IV cross-run Step 3 (§21.20). A portfolio overview over the per-run **concept capsules** written when
`cross_run_concepts` is on: which concepts have been explored across the whole portfolio and in which runs,
each with its OWN outcome. Raw metrics are deliberately **not** compared across tasks (different
task/direction ⇒ no shared contract), so a concept lists `run_id=metric` per run rather than a single
fabricated "best". Pure read of `<memory_dir>/concept_capsules.jsonl` — no LLM/endpoint.

```bash
looplab cross-run-concepts MEMORY_DIR [--top 20] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `concept_capsules.jsonl` (or the file itself) |
| `--top N` | `20` | How many most-explored concepts to list |
| `--json` | off | Emit the full overview (concepts + per-run cards) as JSON |

---

## `claims`

PART IV cross-run Step 4 (§21.20). Projects the distilled `lessons.jsonl` into **evidence-grounded claims**:
each claim groups a statement's support vs oppose node-id evidence and an epistemic state — `supported`,
`refuted`, `mixed` (the portfolio disagrees with itself), or `inconclusive`. Identity reuses the shipped
lesson `normalize_statement`, and the shape unifies with the D8 research-claim `{statement, node_ids}` —
no forked claim type. Pure read; no LLM/endpoint.

```bash
looplab claims MEMORY_DIR [--top 20] [--contested] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `lessons.jsonl` (or the file itself) |
| `--top N` | `20` | How many most-evidenced claims to list |
| `--contested` | off | Show only `mixed` (support **and** oppose) claims |
| `--pack` | off | Render the bounded agent **context pack** (Step 5): contested-first, a caveat slot reserved so positives never crowd out opposition, plus a portfolio-coverage line (composed with `concept_capsules.jsonl` when present) |
| `--json` | off | Emit the full assessments (or, with `--pack`, the pack) as JSON |

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
looplab ui [--run-root DIR] [--host HOST] [--port PORT] [--root-path PATH] [--build/--no-build] [--rebuild]
```

| Option | Default | Description |
|---|---|---|
| `--run-root DIR` | `$LOOPLAB_RUN_ROOT` or `runs` | Directory containing run subdirectories |
| `--host HOST` | `127.0.0.1` | Bind host |
| `--port PORT` | `8765` | Bind port |
| `--root-path PATH` | `""` | ASGI `root_path` for a non-prefix-stripping proxy; auto-derived from `JUPYTERHUB_SERVICE_PREFIX` when unset |
| `--build` / `--no-build` | `--build` | Auto-build the React bundle if it's missing (needs Node/npm); `--no-build` serves a prebuilt bundle only |
| `--rebuild` | off | Force a fresh `npm run build` even if a bundle already exists |

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
looplab tensorboard RUN_DIR [--port 6006] [--host 127.0.0.1]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run dir; its `nodes/` hold each experiment's training logs |
| `--port N` | `6006` | Port to serve on |
| `--host ADDR` | `127.0.0.1` | Bind address. Defaults to localhost — TensorBoard has no auth, so training logs (and any secret a script printed) aren't exposed on all interfaces. Pass `--host 0.0.0.0` to bind everywhere. |

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
