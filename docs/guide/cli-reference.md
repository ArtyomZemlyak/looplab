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
looplab inspect         Show the raw launch snapshot + current folded best result
looplab replay          Pure fold of the event log → state (read-only)
looplab speculation-gate Validate paired Card-speculation evidence and publish the rollout receipt
looplab timings         Per-node wall-clock breakdown (LLM / eval / repair / tools)
looplab concept-coverage Concept-graph coverage + uncovered-region alarm (PART IV D5)
looplab asset-brief     Prior-art & on-disk asset brief for a task repo (PART IV D1)
looplab lock-in         Action-space lock-in detector (PART IV D7)
looplab board-dedup     Taxonomy-aware hypothesis-board dedup analysis (PART IV D4)
looplab research-targets Axis-structured deep-research targets from coverage (PART IV D2)
looplab novelty-recall  Audit executed proposals for paraphrases the novelty gate missed (PART IV E3)
looplab lesson-guard    Audit distilled lessons for over-generalization and contradiction (PART IV D6/E4)
looplab cross-run-index Lean diagnostic run-passport/facts rebuild (PART IV cross-run Step 1)
looplab cross-run-concepts Valid-capsule raw-slug concept overview (PART IV cross-run Step 3)
looplab cross-run-search Bounded hybrid cross-run query + lean receipt (PART IV CR2a)
looplab cross-run-digest Read-only axis-prefix concept rollup (PART IV Step 7)
looplab concept-merge   Append a concept alias/purge overlay (PART IV CR1a)
looplab concept-split   Operator split one coarse concept into finer ones, re-tagged per run (PART IV §21.20.13)
looplab concept-steward AGENTIC taxonomy curator: proposal-only merge/split/purge review (PART IV §22.4)
looplab task-facets     AGENTIC task faceting: proposal-only LLM classify (domain/language/...) (PART IV §21.20.2)
looplab task-facets-set Operator deterministic facet write (the ratify half of task-facets) (PART IV §22.4)
looplab claims          Lean statement/reference claim projection (PART IV cross-run Step 4)
looplab claim-decide    Lean operator decision overlay (PART V §22.4)
looplab claim-steward   AGENTIC claim curator: proposal-only ratify/reject/pin review (PART IV §22.4)
looplab atlas           Capped Atlas summary: explored / thin / contradictory (PART IV Step 6)
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

Every engine setting supplied through `run -s/--set` (and the typed setting overrides) can also come
from a `LOOPLAB_*` environment variable or the `settings:` block of a config file — see
[Configuration](configuration.md). Task inputs (`--goal`, `--kind`, `--data`) and command-specific
options such as `--out` are CLI/file concerns, not `Settings` fields. Precedence for engine settings is
CLI over file over environment. Add `--version` to print the version.

---

## `init`

Scaffold a documented config template (YAML) you can edit and run. The template leads with the task
and active values for the common knobs, then lists every remaining setting commented out at its
default. Active template values override matching `LOOPLAB_*` variables until edited or removed.

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
looplab run task.json --max-nodes 20             # a bare task file + flags (legacy; needs a live endpoint)
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
- defaults the backend to `llm` for a generative kind (`dataset`/`repo`/`mlebench_real`/…). Since
  2026-08-04 `llm` is also the GLOBAL default (`core/config.py:927`), so the offline kinds
  (`quadratic`/…) no longer fall back to a model-free run on their own — pass `--backend toy` when you
  want one. (The Web UI's genesis card applies the same default — an explicit backend, wherever set,
  always wins.)

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
| `-s, --set KEY=VALUE` | — | Override an engine setting (repeatable); same keys as `settings:` / `LOOPLAB_*`. **Not quite "any"**: the credential fields `llm_api_key` / `llm_api_key_base_url` are refused, so a secret never lands in shell history or the resolved snapshot — set them via `LOOPLAB_*` env or the secret store. Because of that split, `-s llm_base_url=…` moves the endpoint but **cannot** move the key with it: set `LOOPLAB_LLM_API_KEY` + `LOOPLAB_LLM_API_KEY_BASE_URL` to match, or the run refuses (see [moving a run to a different endpoint](llm-and-agents.md#moving-a-run-to-a-different-endpoint)) |
| `--out DIR` | the file's `out:` or `runs/run_local` | Run directory (created if missing) |
| `--max-nodes N` | `8` | Node (candidate) budget for the search |
| `--backend toy\|llm` | `llm` | Role backend: offline optimizer or a live LLM. **Default changed toy→llm on 2026-08-04** (operator decision, `core/config.py:927`) — pass `--backend toy` for an offline run |
| `--model ID` | `qwen3:8b` | LLM model id (when `--backend llm`) |
| `--developer-backend NAME` | `default` | Delegate the Developer to `opencode` / `aider` / `goose` / `continue` |
| `--agent-cmd PATH` | — | Override the external agent's launcher/path |
| `--validate-agent / --no-validate-agent` | on | Validate external-agent output, retry with feedback, fall back to the in-house Developer |
| `--agent-patch-gate / --no-agent-patch-gate` | on | Run the agent in a git worktree and surface-gate its diff |
| `--agent-surface GLOBS` | `*.py` | Comma-separated edit-surface allow-list for the agent |
| `--knowledge-dir DIR` | `~/.looplab/knowledge` | Notes directory for agentic retrieval (grep/kb_search/read tools). The flag overrides the Settings/env default. |
| `--memory-dir DIR` | `~/.looplab/memory` | Cross-run case-memory directory. The flag overrides the Settings/env default. |
| `--max-seconds SECS` | — | Wall-clock budget; the run aborts cleanly when exceeded |
| `--ablate-every N` | `0` | Run ablation-driven refinement every N improvements (0 = off) |
| `--confirm-top-k K` | `0` | Confirm the top-k candidates under multiple seeds before finishing |
| `--confirm-seeds N` | `0` | Number of seeds for the confirmation pass |
| `--require-approval / --no-require-approval` | off | HITL: pause for `approve` before finishing. `--no-require-approval` is the explicit off form (it is the default, so it only matters when a config file / `LOOPLAB_REQUIRE_APPROVAL` turns approval on) |

> `--crash-after N` is a hidden test hook that hard-exits after N evaluations (used to demonstrate
> crash-resume).

**Examples**

Every example below that does **not** pass `--backend toy` needs a reachable LLM endpoint: `backend`
defaults to `llm`, and the [endpoint preflight](llm-and-agents.md#endpoint-preflight-before-a-run-starts)
refuses the run with an `LLMError` before the event log exists if the model is not there. (The run
directory itself is created first, to take `engine.lock` — but the refusal lands before any event,
`config.snapshot.json` or `task.snapshot.json` is written, so there is no half-started run to clean up.)

```bash
looplab init && looplab run looplab.yaml                  # scaffold a config, edit, run
looplab run --no-genesis --kind quadratic --goal "minimize x^2+y^2" --direction min --backend toy   # no file, no LLM
looplab run examples/toy_task.json --out runs/demo --max-nodes 14 --backend toy
looplab run examples/toy_task.json -s policy=asha -s n_seeds=5 --backend toy   # --set any setting
looplab run examples/code_regression_task.json --backend llm --max-nodes 6
looplab run examples/regression_task.json --backend llm \
    --knowledge-dir examples/knowledge --max-nodes 6
looplab run examples/repo_task.json --backend llm --developer-backend opencode
```

**Exit codes.** `run` and `resume` share them, because a wrapper, a CI step or an `&&` chain reads
the status and nothing else:

| Code | Meaning |
|---|---|
| `0` | The run made progress: it evaluated at least one experiment, or it is **frozen and resumable** — paused by `looplab stop`, by the developer-crash breaker, or by the provider breaker. A pause is not a failure; `looplab resume` picks it up |
| `1` | The command failed: the run **finished having evaluated nothing** (0 experiments and no champion — see below), the engine aborted with a fatal error (the traceback lands in `engine.stderr.log`), or `resume` gave up waiting for the previous owner's `engine.lock` |
| `2` | The command was refused before the run started: a bad flag or setting, a run dir that holds a different task, a Genesis goal it could not author a task from |

The "finished having evaluated nothing" case is a real failure that used to be reported as success.
A run whose first candidate builds all fail — five HTTP 429s from the provider is the measured case —
finishes cleanly, writes a report whose own text says *"No experiments have been evaluated yet"*, and
has nothing to report on. It now says so on stderr and exits 1:

```
run runs/demo finished with no evaluated experiments (reason=time_budget) — there is nothing to
report on. Check the run's `run_finished` reason and the last `card_build_done` / `node_failed`
rows in events.jsonl.
```

Only a **finished** run is judged this way, and only when the command could actually have run
experiments. Two cases still exit 0 with nothing evaluated:

- a run that **stopped or auto-paused** — the answer is `looplab resume`, not a failed build;
- a `run`/`resume` that landed on a **wrap-up boundary** and could only complete an existing
  finalization ([`finalize`](#finalize)). It was forbidden from proposing, so the emptiness belongs
  to the run that ended earlier; a non-zero exit there means the wrap-up itself was refused.

---

## `resume`

Resume a crashed or incomplete run by re-entering the loop. State is rebuilt from the complete durable
event prefix, and fulfillment receipts prevent already-recorded requests from being served again.
External effects and cross-run sidecars have narrower per-operation recovery contracts; resume does not
promise exactly-once behavior for work that has no durable receipt.

```bash
looplab resume RUN_DIR [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Existing run directory to resume |
| `--task-file PATH` | the run's `task.snapshot.json` | The task file the run was started with |
| `--max-nodes N` | from the snapshot | Override the node budget on resume |

The original launch settings are restored from `config.snapshot.json`, so run-only flags are not silently
dropped. Seven comparison/selection fields (`card_driven_selection`, `speculation_depth`, `holdout_fraction`,
`holdout_select`, `select_verifier`, `select_verifier_samples`, `verifier_ci_tie`) are then restored
from the folded `run_started` record;
`trust_gate_changed` owns later trust-gate edits. Those event-pinned semantics win over a stale or hand-edited
snapshot.

**The concurrency WIDTHS are pinned too, and AUTO is what makes that safe.** `eval_parallel` and
`llm_parallel` ship the AUTO sentinel `0`, and `speculation_depth` spells the same thing `-1` —
which it has also **shipped** since 2026-08-05 (`0` is its explicit off switch). AUTO resolves off
the **live box** (`_detect_gpu_ids`) — one evaluation per
detected GPU, an LLM/build width derived from that, one prefetch per evaluation lane.
`config.snapshot.json` therefore stores your *intent*, never the treatment the log
was written under, so before this was pinned a 1-GPU run resumed on a 2-GPU host silently doubled its
eval concurrency and flipped the build spine from the serial one to the concurrent-append seam
mid-log. `run_started` now records the **resolved integers**, and resume applies one rule per axis:

| How the axis was spelled on the resume command | What resume does |
|---|---|
| **AUTO** (`0` for the two widths, `-1` for `speculation_depth` — each of them that axis's default) | **Adopts** the width the log pinned — including on a *smaller* box. One treatment spans the whole log; a width above what the hardware can serve is bounded by the resource scheduler, not by silently rewriting the treatment. For `speculation_depth` the value adopted is the **last one the log recorded**, not `run_started`'s: AUTO may ratchet the depth down mid-run once the run has measured its own evaluations (see [Adaptive AUTO depth](configuration.md#adaptive-auto-depth)), and each such move is a durable `speculation_depth_settled` event. AUTO's own adaptation is therefore never read as an operator disagreement; a *spelled* depth never adapts at all. |
| An **explicit** width equal to the pin | Proceeds unchanged. |
| An **explicit** width that disagrees with the pin | **Fails closed**, naming the axis and both values, and writes nothing to the log it declined to trust. |
| Any width, on an axis an operator already retuned mid-run through a durable `budget_extend` control event | **Left alone.** That override is re-applied every loop, so the launch flag has no effect on the running width and refusing over it would be a false alarm. |

A log written before widths were pinned records none, and resume then keeps this process's own startup
resolution — byte-identical to the pre-pin behaviour, because inventing a width for a legacy log would
be exactly the re-derivation the pin exists to prevent.

**A positive-depth run pins its speculation LANE, not a receipt.** On any workload other than the shipped
quadratic Toy adapter the run takes the **product lane**: `run_started` pins the search *treatment*
(`card_driven_selection`, the resolved `speculation_depth`, the `greedy` policy scope) plus a lane token,
and deliberately **not** the whole-source implementation digest — that digest hashes every shipped `.py`
file, so a comment edit or a `pip install -U` would otherwise make a half-finished run permanently
unresumable. Resume compares the lane, so a product-lane run stays resumable across source changes.

- **Do not add `speculation_gate_receipt` to a resume command to make it work.** Off the calibration lane a
  receipt authorizes nothing and pins nothing: a valid one is accepted and ignored, an invalid one is
  *declined* (the run proceeds in the product lane) rather than raising. Adding or dropping it changes
  nothing about whether the resume succeeds.
- Runs started by an older build that *did* record a receipt's identity on a product-lane workload are
  **adopted** on resume — that precise legacy shape (no calibration fields, no runtime-scope pin, same
  depth and policy scope) keeps working instead of being stranded forever.
- Only a **receipt-carrying replay of the Toy calibration workload** binds a receipt. There resume pins the
  receipt's self-digest, the implementation digest and the runtime-scope digest, and fails closed on an
  absent/stale/forged receipt or a changed Settings/roles/sandbox envelope. Re-measuring that lane costs
  minutes, which is why it can afford the strict pin.
- A resume that *does* fail closed names the specific pin that moved, and refuses **without writing
  anything** to the log it declined to trust. Re-run with the launch settings the log pinned rather than
  editing them on the resume command.

**Grandfathering.** `resume`/`finalize` re-validate the run's own `task.snapshot.json`, and a run that
already exists must stay resumable even when its recorded spec is one a newer validator would refuse at
submit. Those refusals are printed as `warning: …` on stderr instead — the diagnosis survives, only the
refusal is dropped (see the `eval.metric.path` note in [Tasks](tasks.md)).

**The LLM preflight depends on what this resume can DO.** A resume that lifts a finished or
stopped run back into the loop will propose again, so an unreachable endpoint — or an unusable
credential — is refused exactly as on `run`. A resume that lands on a wrap-up boundary — the run has an
incomplete terminal projection, or a pending finalize — can only complete that wrap-up, so the same
gates warn instead and name what the missing model costs; see [`finalize`](#finalize).

Which of the two it is **is decided under the run's singleton lock**, together with the decision to lift.
That matters because `resume` may have to *wait* for the lock: when the run is stopped, finished or
mid-wrap-up, a previous owner can still be in its finalization tail, and this command waits up to 600 s
for it (echoing `waiting for the engine lock on … — the previous owner is finishing up` while it does).
The wait exists precisely to let that owner **finish its wrap-up** — the transition from "wrap-up only"
to "liftable". Deciding before the wait meant a `resume` that had warned about a dead endpoint could
come out of the wait, find a plainly finished run, lift it, and spend the remaining budget on identical
empty fallback nodes at exit 0. So nothing is decided until the lock is held, and the promise is
re-checked once more immediately before the loop starts: if the run was lifted out from under a wrap-up
in between, the command refuses and tells you which one to run instead.

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
looplab finalize RUN_DIR [--task-file TASK.json]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to stop and wrap up |
| `--task-file PATH` | `RUN_DIR/task.snapshot.json` | Explicit task definition for recovery of a legacy run whose canonical task snapshot is absent |

**A dead LLM endpoint does not block the wrap-up.** `backend` defaults to `llm`, and the
[endpoint preflight](llm-and-agents.md#endpoint-preflight-before-a-run-starts) refuses a run whose model
is unreachable — but a run that is already over makes no proposals, so there is nothing there for a
missing model to degrade into a false success. On a **wrap-up-only** entry point the same probe warns
instead of refusing and the wrap-up proceeds:

```
⚠ LLM endpoint unreachable while wrapping up: the default target (deepseek-v4-flash at http://…): …
  This run is over, so no proposal can degrade — wrapping it up anyway. What the missing model costs:
    · end-of-run report → the placeholder "(report unavailable)", not the written report
    · cross-run lessons, skills and curation → nothing the model would have authored; …
finalized RUN_DIR — wrapped up WITHOUT the model: …
```

| | Without a reachable model |
|---|---|
| **Still written** | budget summary, diversity archive, case + concept capsule, LLM cost roll-up, `readmodel.sqlite`, `trace.json`, `tree.html`, and the `finalization_finished` completion marker — none of them need a model |
| **Degraded** | the end-of-run report is the literal placeholder `"(report unavailable)"`; nothing model-authored reaches cross-run memory (the reflection note and each curation log still record the run deterministically) |
| **Irreversible** | each step is marked complete once attempted, so bringing the endpoint back and re-running `finalize` is a no-op — which is why the warning is printed **before** the wrap-up runs. Stop there if you want the written report and lessons |

**Neither does an unusable credential.** The gate that runs one step before the endpoint probe —
a key whose endpoint binding is missing or points somewhere else, a role bound to a connection profile
whose `api_key_env` is unset — used to refuse here too, with the same result the paragraph above exists
to prevent: exit 1, not one artifact, and the run stuck at `finalization_pending` with its spend
stranded in `.llm-usage-outbox`. It now warns on exactly the same boundaries:

```
⚠ LLM credential unusable while wrapping up: LOOPLAB_LLM_BASE_URL was overridden without its
  credential.
    this target would call: https://new-endpoint/v1
    but LOOPLAB_LLM_API_KEY (from the .env file) is bound to: https://old-endpoint/v1
    Why this is refused rather than retargeted for you: …
    Fix: move the credential with the endpoint — …
  This one cause is why all 7 of these fail; they share the credential configuration and are not
  7 separate problems: the default target, implement, pilot, propose, repair, researcher, strategy.
  This run is over, so no proposal can degrade — wrapping it up anyway. What the missing model costs:
    …
  The wrap-up below runs with NO credential at all rather than sending this one somewhere it
  is not bound to, so any call it does attempt is refused by the provider, not silently mis-sent.
```

**One root cause is diagnosed once.** Seven roles resolve the same shared credential, so a single
wrong variable used to print seven copies of one sentence — which told you nothing the first copy did
not, and hid *which knob* was wrong behind the repetition. The refusal now names the action you took
(not the state it left behind), the endpoint each side is pointing at, why the key+endpoint pair is
atomic, the exact variables to set together — and lists the affected roles once, underneath.

That last line is the one difference between the two gates. A dead endpoint still lets every client be
*built* (it fails at request time), while an unusable credential fails while the client is being
constructed — so this wrap-up, and only this wrap-up, drops the credential entirely rather than
carrying a key to a host it was not issued for. Your configuration is not modified; fix the pair and
the next command uses it normally.

The wrap-up-only entry points are `finalize`, and a `run`/`resume` that lands on a boundary it may only
complete (`run has an incomplete terminal projection …` / `run has a pending finalize …`). A `resume`
that would **lift** a finished or stopped run back into the loop can still propose, so it keeps the full
refusal — and it classifies the boundary **under the singleton lock**, so a `finalize` that completes
while that `resume` waits cannot turn its warning into a lift (see [`resume`](#resume)).

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

The run must be **offline**: repair replaces the log with a prefix of itself, and a live engine's
appends are authoritative events a replace would silently delete. The command refuses while
`engine.lock` is held (and holds it for the repair, so one cannot start halfway through); detection,
backup and truncation all happen under the event log's own lock.

```bash
looplab repair-log RUN_DIR
# then: looplab resume RUN_DIR
```

---

## `inspect`

Print the raw on-disk launch config snapshot and the run's current folded best result. This is a diagnostic
view, not the effective per-run config API: the latter overlays the seven `run_started`-pinned fields and the
event-sourced trust gate.

```bash
looplab inspect RUN_DIR
```

## `replay`

Read-only: fold the event log into the current state and print it as JSON. This is the
reproducibility check — it has no side effects.

```bash
looplab replay RUN_DIR
```

## `speculation-gate`

Build the **optional** local rollout receipt for positive-depth Card speculation. It is a maintainer's
BENCHMARK — it re-measures scorer fidelity, hit/divergence rate and normalized regret on the shipped
quadratic Toy adapter — **not a licence**: positive `speculation_depth` runs on any task without one
(see `speculation_gate_receipt` in [Configuration](configuration.md#backend-roles)), and every run gets
the cheap per-run substitute for free in its `budget` receipt. Do not spend these six real-GPU runs to
unlock speculation; spend them only to re-measure the benchmark itself. Supply alternating
depth-0 baseline and positive-depth treatment directories: exactly three pairs, in fixed seed order
`0`, `1`, `2` (six directories total). All six runs use the same `max_nodes`; every treatment uses the
same positive depth. Evidence runs must be created in fresh directories by the source-owned offline
calibration protocol (`looplab run ... --no-genesis --speculation-gate-calibration`). Within each pair
the task bytes and non-placement configuration are exact; between pairs only the integer Toy-task seed
changes. Every run must finish its complete node budget through the effective `CUDA_VISIBLE_DEVICES`
GPU inventory, and every evaluated artifact must create a real CUDA context and allocate/free 4096
bytes through the CUDA Driver API before reporting its objective metric.
The fixed v1 checks cover the exact 15-case scorer-fidelity suite, normalized regret, speculation
hit/divergence rates and non-vacuous trusted concept coverage. A failing gate never writes a receipt.

The repository includes the exact canonical task inputs. Produce one baseline/treatment pair per seed
in fresh directories (shown with admitted depth `1` and budget `12`):

```bash
looplab run examples/speculation_gate_seed_0.json --no-genesis --out runs/seed0-depth0 --max-nodes 12 -s speculation_depth=0 --speculation-gate-calibration
looplab run examples/speculation_gate_seed_0.json --no-genesis --out runs/seed0-depth1 --max-nodes 12 -s speculation_depth=1 --speculation-gate-calibration
looplab run examples/speculation_gate_seed_1.json --no-genesis --out runs/seed1-depth0 --max-nodes 12 -s speculation_depth=0 --speculation-gate-calibration
looplab run examples/speculation_gate_seed_1.json --no-genesis --out runs/seed1-depth1 --max-nodes 12 -s speculation_depth=1 --speculation-gate-calibration
looplab run examples/speculation_gate_seed_2.json --no-genesis --out runs/seed2-depth0 --max-nodes 12 -s speculation_depth=0 --speculation-gate-calibration
looplab run examples/speculation_gate_seed_2.json --no-genesis --out runs/seed2-depth1 --max-nodes 12 -s speculation_depth=1 --speculation-gate-calibration
```

The maintainer-only calibration flag forces the source-owned offline Toy/Greedy/Card profile and rejects
a missing effective GPU, a non-canonical task, ambient receipt authority or a reused run directory.

```bash
looplab speculation-gate \
  runs/seed0-depth0 runs/seed0-depth1 \
  runs/seed1-depth0 runs/seed1-depth1 \
  runs/seed2-depth0 runs/seed2-depth1 \
  --output "$PWD/.looplab/speculation-quality.receipt.json"   # -o is the short form
```

Use the exact absolute path printed by the command as `speculation_gate_receipt`, together with
`card_driven_selection=true`, the receipt's exact admitted `speculation_depth`, and its exact
`admitted_max_nodes`. The printed receipt also exposes `calibration_seeds`, `runtime_scope_sha256`,
the implementation/environment/profile digests, and seven stable GPU fields (`index`, UUID, PCI bus,
name, total memory, display-driver version and CUDA-driver version). It admits only the measured Greedy
policy, deterministic quadratic Toy-task profile and exact tested runtime envelope; it is not authority
for other policies, workloads, task profiles, depths, budgets or settings. Validation re-reads the raw
evidence and binds the current implementation, environment and effective GPU identity, so retain every
referenced run directory. Regenerate after any byte change covered by the implementation digest (including
Python comments, the packaged settings schema and `pyproject.toml`) or any bound environment change.

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
which fires from the first node rather than waiting for narrowing to accumulate. The default is read-only
and never touches selection. The explicit `--persist` exception retro-tags only a fully finalized,
non-running run; it owns `engine.lock`, revalidates the terminal protocol, and CASes the exact event-log
snapshot it analyzed. A run that is stopped, finalizing, resume-pending, still holding `engine.lock`, or
changed during analysis is rejected rather than receiving stale tags.

**Agentic by default** (agentic-first concept): the LLM builds the map — it grows the concept vocabulary
from the actual experiments (reading each node's code/logs), so **it sends node code/logs to the configured
LLM endpoint by default**. Pass `--offline` for the fully local, no-network deterministic heuristic (coarser:
needs a curated `--task-type` pack and cannot derive per-task importance). Four other Part IV diagnostics
below (`lock-in`, `board-dedup`, `research-targets`, `novelty-recall`) share this `--offline` opt-out
contract. `lesson-guard` is LLM-only and has no offline verdict path. `asset-brief` is the other exception:
it stays offline-by-default (`--llm` opt-in) because its agentic path is a heavier full tool-loop.

```bash
looplab concept-coverage RUN_DIR [--task-type dense-retrieval] [--offline] [--model ID] [--repo PATH]
                         [--jobs N] [--persist]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to fold and diagnose |
| `--task-type NAME` | inferred from the run's `task_id` | Concept pack to SEED the agent's build (e.g. `dense-retrieval`); the LLM verifies/expands it, or builds from scratch when no pack matches |
| `--offline` | off (**default is the agentic build**) | Skip the LLM/network and use only the deterministic alias heuristic over the curated seed pack — a fast local fallback (needs a pack; no per-task importance) |
| `--model ID` | configured model | Override the model for the agentic build |
| `-j, --jobs N` | `8` | Concurrent node-tagging calls in the agentic build |
| `--persist` | off | Append generation-fenced memberships to an exact, fully finalized finished snapshot. Repeated same-source results are idempotent. Agentic/LLM results carry reviewed `classifier` provenance, become visible to replay-derived indexes, and can upgrade identical earlier heuristic ids; the command does **not** rebuild a capsule already emitted during finalization. Exact `--offline` results carry `offline-heuristic` provenance and remain display-only. Operator edits and existing classifier evidence cannot be downgraded. There is no live-run override. |
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

PART IV D7 (§21.8) read-only analytic. Over the concept graph, finds the longest run of **consecutive**
experiments confined to one axis-region — the "same-lever streak" the flat coverage signal was blind to
(on the `rubertlite` replay it trips at ~node 29) — and fires when it exceeds the threshold. Read-only,
deterministic once the concept tags exist. The LLM builds those tags by default; `--offline` uses the
coarser deterministic tagger.

```bash
looplab lock-in RUN_DIR [--task-type NAME] [--threshold 5] [--offline] [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory to fold and diagnose |
| `--task-type NAME` | inferred from `task_id` | Concept-graph skeleton (e.g. `dense-retrieval`) |
| `--threshold N` | `5` | Consecutive same-lever experiments that trip the alarm |
| `--offline` | off | Do not call the LLM; build tags with the deterministic heuristic |
| `--model ID` | configured model | Override the model used for the agentic tag build |

---

## `board-dedup`

PART IV D4 (§21.5) read-only analytic. Tags the Card belief board (1 card = 1 hypothesis) and surfaces the dominant **within-concept**
redundancy (merge aggressively — e.g. the DCL cluster) plus **cross-branch** look-alike pairs a blind
lexical/vector merge would wrongly collapse (keep distinct). The LLM builds/tags the graph by default;
`--offline` forces the deterministic heuristic. Read-only; merges nothing.

```bash
looplab board-dedup RUN_DIR [--task-type NAME] [--offline] [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory whose Card belief board to analyze |
| `--task-type NAME` | inferred from `task_id` | Concept-graph skeleton |
| `--offline` | off | Do not call the LLM; use deterministic graph and hypothesis tags |
| `--model ID` | configured model | Override the model used for the agentic build/tag pass |

---

## `research-targets`

PART IV D2 (§21.3) read-only analytic. Turns the coverage map into a ranked set of axis-structured
deep-research targets: **uncovered** axes first (the blind regions), **failed directions** re-framed as
"research a different implementation" (so the loop stops re-proposing the failed variant), then
**under-covered** axes. The agentic path also derives task-specific important-but-uncovered directions;
`--offline` emits only deterministic axis targets. Read-only; produces the targets, runs no research.

```bash
looplab research-targets RUN_DIR [--task-type NAME] [--asset-repo PATH] [--offline] [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory whose coverage to target |
| `--task-type NAME` | inferred from `task_id` | Concept-graph skeleton |
| `--asset-repo PATH` | — | Task repo used to ground the derived importance and queries in a D1 asset brief |
| `--offline` | off | Do not call the LLM; use the deterministic graph and axis targets only |
| `--model ID` | configured model | Override the model used for the agentic build |

---

## `novelty-recall`

PART IV E3 (§21.12) read-only novelty-gate audit. It clusters near-duplicate ideas among experiments
that were actually built, then — by default — asks the configured LLM to distinguish a true paraphrase
from a legitimate variant. `--max-pairs` bounds the paid adjudication calls and the command sends at most
the two truncated idea texts for each selected pair. `--offline` reports unjudged candidate pairs without
calling an endpoint.

The displayed recall is explicitly an **optimistic diagnostic**, not a calibrated quality metric: its
numerator counts gate-rejection events while its denominator adds adjudicated leaked pairs, and only the
most-similar bounded tail is judged. Treat the leaked-pair list as the actionable output.

```bash
looplab novelty-recall RUN_DIR [--offline] [--max-pairs 60] [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run whose executed proposals should be audited |
| `--offline` | off | Skip LLM adjudication and return candidate near-duplicate pairs only |
| `--max-pairs N` | `60` | Maximum most-similar pairs to adjudicate (`0..100000`; one LLM call per attempted pair) |
| `--model ID` | configured model | Override the adjudication model |

---

## `lesson-guard`

PART IV D6/E4 (§21.7/§21.12) read-only, LLM-backed audit of the run's distilled lessons. It checks
whether a lesson generalized one failed implementation into a rejection of an otherwise sound direction,
then scans up to 40 lesson pairs for contradiction. It writes no events and never changes selection or the
lesson store. This command has no offline verdict path: an unreachable or fully abstaining verifier is
reported as **inconclusive**, not as a clean result.

```bash
looplab lesson-guard RUN_DIR [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run whose distilled lessons should be audited |
| `--model ID` | configured model | Override the verifier model |

---

## `cross-run-index`

PART IV cross-run Step 1 (§21.20.3). Builds a **lean diagnostic** index: a versioned run passport plus
latest folded node rows with optional metric and best, from `<run_root>/*/events.jsonl` + task snapshots.
Rebuild ordering is content-deterministic even when copied runs share coarse keys. `--incremental` reuses a
source-digest cache and prints built/cached/skipped receipts for unusable run projections.

Portfolio scanning accepts only ordinary, non-symlink event logs and task snapshots. Event logs are hashed
as a stream; `task.snapshot.json` is capped at 4 MiB before JSON materialization. An oversized/non-regular
snapshot or event log is excluded with a bounded skip reason that never reflects a source path or damaged
content. The disposable incremental cache is likewise rejected and rebuilt before parsing when it exceeds
256 MiB or is not a regular file.

This is still not the full CR0 corpus-health/per-generation measurement index: reset generations collapse
to the latest folded attempt, duplicate run identities are not deduplicated, and a missing/garbled task
snapshot degrades its kind/metric fields to empty without a dedicated receipt. The default non-incremental
CLI returns only the index and therefore discards the incremental builder's skip receipts. No LLM/endpoint
and no new source of truth.

```bash
looplab cross-run-index RUN_ROOT [--incremental] [--json]
```

| Option | Default | Description |
|---|---|---|
| `RUN_ROOT` | *(required)* | Directory holding run subdirs (each with `events.jsonl` + `task.snapshot.json`) |
| `--incremental` | off | Reuse `<run_root>/.cross_run_index.json`; print built/cached/skipped receipts and save the refreshed cache when the index is non-empty |
| `--json` | off | Emit the lean index array as JSON (receipts are not included in this JSON payload) |

---

## `cross-run-concepts`

PART IV cross-run Step 3 (§21.20). A portfolio overview over **valid concept capsules present in `MEMORY_DIR`**
from finalized opt-in runs: which raw concept slugs appear and in which recorded runs, each with its own
metric-bearing outcome. Each new capsule has bounded-source completeness triplets for concepts and outcomes,
plus an active-classifier-node producer receipt. Retained labels from a partial classifier row remain visible,
but the capsule, overview, run card, graph and digest stay partial; aborted/tombstoned nodes are excluded and a
partial-only run persists as an empty lower-bound capsule rather than disappearing;
the text command warns when known items were omitted, and `--json` exposes aggregate plus per-run source
receipts. The aggregate overview is also independently capped at 512 concept rows: its exact `n_concepts` /
`concepts_omitted` receipt is present in JSON and text mode explicitly reports a non-zero projection omission.
Legacy v2 capsules without collection or producer fields remain positive observations but have unknown totals and do
not contribute their potentially post-truncation rank signs. Malformed JSON, schema-invalid rows and duplicate
run identities are quarantined rather than returned; their read-health counts make `source_complete=false`, so
the text form cannot report an exact absence from a damaged capsule file. Missing, untagged or non-opt-in runs
remain outside this corpus, so capsule completeness is not whole-portfolio coverage. Raw metrics are deliberately **not** compared across tasks (different
task/direction ⇒ no shared contract), so a concept lists `run_id=metric` per run rather than a single
fabricated "best". Each concept also carries a **direction-normalized rank rollup** (`+better/~neutral/-worse`
half sign counts, with a within-run neutral band around the median): unlike raw metrics, a per-run "did this
concept land in the better or worse half of THIS run's own field, in THIS direction" rank IS comparable, so it
aggregates across runs into an advisory tendency (a relative rank, not causal profit, and never a selection
input). Pure read of `<memory_dir>/concept_capsules.jsonl` — no LLM/endpoint.

```bash
looplab cross-run-concepts MEMORY_DIR [--top 20] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `concept_capsules.jsonl` (or the file itself) |
| `--top N` | `20` | How many most-explored concepts to list |
| `--json` | off | Emit the bounded overview, per-run cards, and capsule source-completeness/omission receipts as JSON |

---

## `concept-merge`

PART IV CR1a. Appends an exact-slug alias (`FROM_CONCEPT → TO_CONCEPT`) to
`concept_aliases.jsonl`; omitting the target writes a purge/tombstone overlay. Raw per-run tags are not
rewritten. Alias writes append under the shared interprocess lock; Atlas, digest, retrieval, agent-tool and
`cross-run-concepts` reads load the same latest alias/split projection.

This local CLI is a **lean alias overlay**, not full taxonomy governance: the content-addressed `concept_uid`
helper is not a release-pinned entity identity, and source/target existence, scope and taxonomy release are not
validated. Empty sources, self-links and cycles are rejected under the shared append lock, and keys are
case-normalized. This command has no `expected_revision`, `action_id` or explicit clear verb; a later record can
replace the effective mapping and raw capsule tags remain intact. The owner HTTP governance surface is stricter:
it separates merge from confirmed purge and supplies per-alias-ledger `expected_revision`, cross-ledger
`expected_governance_revision`, idempotency and alias-clear actions. Mutation receipts and Atlas reads expose
the resulting shared governance revision. Owner HTTP merge/purge additionally requires a live canonical source,
and merge requires a live canonical target, from the current capsule/split projection; its receipt carries that
projection's digest. Neither surface
provides impact preview, assignment backfill or a queryable taxonomy-history workbench.

All concept/claim governance CLI reads and writes fail with exit code 2 when an operator-policy
sidecar is unhealthy. They print only the ledger name and a bounded reason (`torn_tail`,
`malformed_json`, `unknown_action`, `unsupported_schema`, duplicate/revision failure, etc.); they do
not echo the damaged row or path. A normal mutation never appends over that state. Preserve a backup
and repair/restore the sidecar offline before retrying; `repair-log` applies only to a run's
`events.jsonl`. Steward commands also refuse an unhealthy paid-invocation curation history before
client creation, so a malformed cached begin/outcome cannot become a paid cache miss.

```bash
looplab concept-merge MEMORY_DIR FROM_CONCEPT [TO_CONCEPT]
```

| Argument | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir where `concept_aliases.jsonl` is appended |
| `FROM_CONCEPT` | *(required)* | Exact stored slug to alias or tombstone |
| `TO_CONCEPT` | `""` | Exact canonical display slug; omitted/empty means purge from alias-aware reads |

---

## `concept-split`

PART IV §21.20.13 — the OPERATOR concept **SPLIT**: declare one coarse concept really covers several finer
ones, RE-TAGGED per each run's OWN sibling concepts. The append-only `concept_splits.jsonl` overlay is applied
at READ time and raw per-run tags are never rewritten; the latest record for a source replaces its effective
split. This local CLI has no expected-revision/action-id/clear options; the owner HTTP surface requires both the
per-split-ledger `expected_revision` and cross-ledger `expected_governance_revision`, adds idempotency and
`concept-split-clear`, and exposes the shared revision in receipts. Neither surface is a revisioned
taxonomy/assignment release. For a given run the FIRST rule
whose `when_any` terms appear among that run's sibling concept tokens wins; otherwise `--default` (or the
original slug). Under the shared governance lock, writes reject an empty, purged or aliased source. Rule
targets must be live canonical concepts, differ from the source and from every other target, and a non-empty
default must differ from every rule target. The default may intentionally equal the source so unmatched
observations remain under the coarse concept while matched observations move to children; a rules-empty
identity-only split is still rejected as inert. Owner HTTP additionally requires the source in the live
canonical portfolio projection; split children may be new provisional taxonomy entities, while any
already-known child must be canonical.

```bash
looplab concept-split MEMORY_DIR FROM_CONCEPT --rule 'TARGET:term1,term2' [--rule ...] [--default TARGET]
```

| Argument / Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir where `concept_splits.jsonl` is appended |
| `FROM_CONCEPT` | *(required)* | The coarse concept slug to split |
| `--rule 'TARGET:t1,t2'` | `[]` | A re-tag rule — a run whose sibling concepts contain ANY term is re-tagged to `TARGET`. Repeatable (ordered, first match wins) |
| `--default` | `""` | Fallback target when no rule matches (else the original slug is kept) |

---

## `concept-steward`

PART IV §21.20.13 / §22.4 — the **AGENTIC taxonomy steward**: an LLM reviews the cross-run concept graph and
PROPOSES a curation (merge duplicate slugs / split conflated ones / purge noise). It is **proposal-only**:
review the exact returned rows, then translate only the selected operations into typed `concept-merge` /
`concept-split` commands or owner HTTP governance actions. The deprecated `--apply` spelling remains so old
scripts fail clearly, but exits 2 **before model setup, paid inference or mutation**; it never re-runs and applies
an unreviewed batch. The prompt carries separate capsule-source and model-visible vocabulary-projection
receipts. If either is incomplete, deterministic validation allows direct synonym merges only and rejects
split/purge proposals whose rarity or absence premise could depend on omitted concepts. Needs a reachable LLM.
This is the on-demand companion to finalize-time
`cross_run_curation`. Every invocation requires a stable `--action-id`: the CLI durably appends a begun row
before model dispatch and a terminal proposed/empty/error row afterwards. A restart that finds only begun
reports an unknown outcome and never repeats the same potentially paid call; a completed retry replays the
durable terminal proposal without constructing a provider client.

```bash
looplab concept-steward MEMORY_DIR --action-id ID [--apply] [--model M] [--max-proposals 12] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `concept_capsules.jsonl` |
| `--apply` | off | **Deprecated and disabled.** Exits 2 before any LLM call or write. Run without it, review the exact proposal, then use typed `concept-merge` / `concept-split` or owner HTTP governance |
| `--model` | *(config)* | Override the LLM model id |
| `--max-proposals` | `12` | Cap the total merge/split/purge proposals per pass |
| `--action-id` | *(required)* | Stable paid invocation identity. Reuse only to reconcile/replay that exact model/proposal-budget request; a changed request or CLI/HTTP surface is rejected before client construction |
| `--json` | off | Emit `{proposals, receipt, invocation}` as JSON; `invocation` carries the durable action id/revision/outcome and whether this was a replay |

---

## `cross-run-digest`

PART IV Step 7. Builds a deterministic **one-level axis-prefix rollup**: each concept is grouped by the text
before its first `/`, with concept/run counts. Counts are computed from the full validated, de-duplicated
retained capsule snapshot before display limits. `n_axes`, `n_concepts`, and each axis's `n_runs` /
`n_concepts` are exact for that snapshot; `axes_omitted`, top-level `concepts_omitted`, and per-axis
`concepts_omitted` disclose bounded payload lists. The separate `source_*` receipt says whether the capsules
themselves were complete. Despite the historical “recursive digest” name, this is not a hierarchy/tree and
has no proof or eligible-outcome contract. It is inspector data only and is not injected into prompts.

```bash
looplab cross-run-digest MEMORY_DIR [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `concept_capsules.jsonl` and optional alias/split overlays |
| `--json` | off | Emit bounded axes plus exact totals, omission counters, and the capsule-source receipt instead of the compact text rollup |

---

## `cross-run-search`

PART IV CR2a. Runs a bounded hybrid recall over statement-grouped claims plus alias/split-aware concept labels
and excludes operator-rejected claims. The payload includes an intent classification, aggregate result score
and relevance rank, corpus digest, corpus/hit/truncation counts, the effective contradiction quota/caveat count,
and a declaration that the 64-bucket hash "vector" channel is a lexical proxy rather than a semantic model.
The corpus is capped internally at 2,000 records by default and rebuilt per call. Query-aware preselection sees
the full validated canonical retained concept set before that cap, rather than the overview's top-512 display
projection. Receipts distinguish exact concept/claim totals, indexed counts and omissions, and commit those
counts plus any bypassed overview projection tail to the versioned retrieval digest.

Claim hits carry their scope, evidence digest, and nested research/combined-source health receipts (including
the live snapshot digest), but the result still does not expose raw evidence references, each channel's per-hit
contribution, a frozen portfolio watermark/index release, a ComparisonContract, or a persisted derivation
receipt. The CLI has no scope option and reads the portfolio-wide stores. Bound agent callers instead pass a
role-filtered snapshot scoped by compatible direction plus exact task or a strict related-goal fingerprint for
lessons/capsules. V3 D8 rows carry
no goal fingerprint and are exact-task-only. Task facets are advisory metadata reserved for future post-scope
ranking; they grant no visibility and currently do not change order. This is useful applicability filtering,
not a security boundary. This remains an experimental recall, not the full “Why recalled?” proof contract in
doc 18, and there is no matching HTTP query route yet.

```bash
looplab cross-run-search MEMORY_DIR "QUERY" [--k 8] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir; CLI reads are intentionally portfolio-wide |
| `QUERY` | *(required)* | Free-text idea, technique or question |
| `--k N` | `8` | Requested result count, validated in the inclusive range `1..64`; the retrieval receipt records the effective `k` |
| `--json` | off | Emit ranked results plus the intent/corpus/quota receipt |

---

## `claims`

PART IV cross-run Step 4 (§21.20). Projects `lessons.jsonl` plus persisted D8 `research_claims.jsonl` into a
**statement-grouped lean claim view** with support/oppose/unverified attempt references. The legacy wire labels
`supported`/`refuted` mean **support-only/opposition-only evidence**, `mixed` means both kinds of reference,
and `inconclusive` means insufficient evidence; none is by itself a proposition verdict. New v3 D8 rows preserve `task_id`, direction, run-qualified node
references, source URLs, the verifier verdict/method/note, and a per-run producer-cap receipt. A memo may
retain at most 256 valid claims; `claims_total` / `claims_retained` / `claims_omitted` and
`producer_complete` make that lower bound explicit; a non-empty all-invalid source leaves a non-indexed receipt
sentinel rather than disappearing. A partial or legacy-unknown source keeps retained refs citable but withholds
both exact one-sided states (`supported`/`refuted`) and steward ratification because an omitted tail may make
the evidence mixed. Legacy rows without the verifier payload remain
`unverified` and never become positive support merely because they cited a node. New distilled lessons carry an
explicit `claim_stance` separating literal proposition support from action guidance, so a confirmed negative
fact is no longer inverted; legacy rows without the field keep the historical outcome mapping. This is still not
an independent-evidence assessment: refs are attempts rather than independent evidence families. Identity is normalized statement text unless
`--structured` is selected. `--scope` narrows every joined store (lessons, D8 research claims and, with
`--pack`, concept capsules) to one task — the CLI spelling of the HTTP `/api/cross-run/claims?scope_task=`
read. Pure read; no LLM/endpoint.

```bash
looplab claims MEMORY_DIR [--top 20] [--contested] [--pack] [--fuzzy] [--structured] [--scope TASK_ID]
               [--json] [--governance-receipt]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `lessons.jsonl` and/or `research_claims.jsonl` (or a lessons file) |
| `--top N` | `20` | How many most-evidenced claims to list — **and, with `--pack`, the pack's `max_claims` cap** (`engine/claims_retrieval.py::build_context_pack`), so it bounds both listings and the rendered context pack |
| `--contested` | off | Show only `mixed` (support **and** oppose) claims |
| `--pack` | off | Render the hard claim-count-capped agent **context pack** (Step 5): pinned → ratified → mixed → support-only (`supported` wire state) → opposition-only (`refuted`) → insufficient; a caveat can replace the weakest non-pinned positive; omitted pins are counted explicitly. Concept tendencies are derived from the full retained pre-cap aggregate while the rendered labels remain bounded |
| `--fuzzy` | off | Suggestion-grade bounded token-Jaccard complete-link merge: every pair must clear the threshold and share scope, polarity and maturity; it is non-transitive and never scope-agnostic, but remains display/review grouping rather than claim identity |
| `--structured` | off | Group by the scope+polarity-safe **structured claim key** (`engine/claim_key.py`) instead of the display statement: claims from different tasks never merge, opposite-polarity assertions ("X helps" vs "X never helps") surface as a CONTRADICTION rather than collapsing, and grouping is O(n) exact-key (no transitive over-merge). Governance overlays by scope-precise `claim_uid` |
| `--scope TASK_ID` | `""` (portfolio-wide) | Project only this task's evidence, filtering **every** joined store through the same access boundary the Atlas and HTTP reads use. **Required to obtain a usable `--governance-receipt` for a task-scoped claim** — see the projection rule below. Empty keeps the portfolio-wide read |
| `--json` | off | Emit the full assessments (or, with `--pack`, the pack) as JSON |
| `--governance-receipt` | off | With `--json`, emit `{claims, revision, structured, scope}`. Use `--structured --scope TASK_ID --json --governance-receipt` to obtain the exact UID/evidence-digest/revision inputs required by `claim-decide`. `scope` echoes the projection the digests describe, exactly as the HTTP claims response echoes `scope_task` |

### The projection rule: review at the scope you decide at

`evidence_digest` is a token for the **projection**, not for the claim alone: it commits the projection's
whole-source health receipt (snapshot digest, producer-run counts, quarantine counters) alongside the claim's
own support/oppose evidence. That is deliberate — a partial or quarantined source withholds exact one-sided
states, so a decision taken over one must be refused once the omitted tail becomes readable and can flip the
verdict. `claim-decide` always validates against the projection scoped to **its** `--scope`.

So the scope you review at must be the scope you decide at:

```bash
# a task-scoped claim (every engine-produced claim carries its run's task_id)
looplab claims MEMORY_DIR --scope blob_classification --structured --json --governance-receipt
looplab claim-decide MEMORY_DIR "STATEMENT" --ratify --scope blob_classification \
  --claim-uid ... --evidence-digest ... --expected-revision ... --action-id ...
```

Reviewing portfolio-wide (no `--scope`) and then deciding a task-scoped claim supplies a digest computed over
a different source snapshot; `claim-decide` exits 2 with `claim_evidence_changed` and names the projection it
validated against. A `--governance-receipt` taken without `--scope` over a portfolio that contains scoped
claims warns about exactly this **on stderr** (stdout stays pure JSON), naming the scopes it found. Omit
`--scope` on both halves only for a genuinely unscoped claim. This mirrors the HTTP pair
(`GET /api/cross-run/claims?scope_task=T` then `POST /api/cross-run/claim-decide` with `scope: T`), which has
always required the same pairing.

Operator decisions (from `claim-decide`) are overlaid: a `[RATIFIED]`/`[REJECTED]`/`[PINNED]` marker is shown.
Structured lookup prefers exact scope+metric, then scope-only, global metric and global. An unscoped decision
is therefore an intentional portfolio-wide fallback; a scoped one does not reach another task.
A rejected claim remains human-visible in the unfiltered claims CLI/API and can still contribute to Atlas
top-level `n_claims`/`n_claims_total`; it is excluded from the **active** context pack, Atlas contradictions,
agent-tool projection and hybrid retrieval. Pins have first retention priority, followed by ratified claims. This closes
the earlier steering leak without pretending rejection deletes evidence or history; scope, D8-verification and
stable-identity gates in [doc 17 §22.8](../17-project-review-and-directions-2026-07-11.md) still block production
advisory.

---

## `claim-decide`

PART V §22.4 — the **lean operator decision overlay**: ratify / reject / pin a cross-run claim. Agents have no
matching mutation tool. Records append under an interprocess lock with fsync to `claim_decisions.jsonl`, keyed
both by normalized statement text and by a stable scope+polarity `claim_uid`; the latest matching record wins.
The CLI now requires the currently projected `claim_uid`, exact `evidence_digest`, observed ledger revision and
a stable action id. UID validation, lost-response replay, CAS, live-target lookup, evidence freshness validation
and durable append execute as one policy-then-evidence locked operation shared with the owner HTTP endpoint.
It therefore cannot govern a typo/future claim or silently ratify evidence that changed after review. The local
CLI still has no clear verb, server-derived actor/time or queryable history endpoint; owner HTTP additionally
provides `clear` and stable structured 409 responses. A later valid decision can replace the effective badge
while raw JSONL history remains.
That live digest fence is not a versioned evidence release, and no queryable decision-history workbench exists.
Operator maturity remains explicit policy until a later `clear`; it does not silently expire when evidence
changes. Structured JSON exposes `decision_fresh`, while claims/context/tool text labels current,
stale-evidence, or unknown freshness separately so `RATIFIED`/`PINNED` never implies that the reviewed
evidence digest is still current.

The supplied `--evidence-digest` is validated against the claim projection scoped to **this command's**
`--scope`, so it must come from a `claims` read taken at that same scope (see
[the projection rule](#the-projection-rule-review-at-the-scope-you-decide-at)). A digest taken from a
different projection — in particular the portfolio-wide default — exits 2 with `claim_evidence_changed`; the
refusal names the scope it validated against and that projection's current digest, so a mis-scoped review is
distinguishable from evidence that genuinely moved.

```bash
looplab claim-decide MEMORY_DIR "STATEMENT" (--ratify | --reject | --pin) \
  --claim-uid UID --evidence-digest DIGEST --expected-revision N --action-id ID \
  [--note "..."] [--scope TASK_ID] [--metric METRIC]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir (where `claim_decisions.jsonl` is written) |
| `STATEMENT` | *(required)* | The claim statement (matched by normalized text) |
| `--ratify` | — | Mark `operator-ratified`; surfaced after explicit pins in the current context-pack projection |
| `--reject` | — | Mark `operator-rejected`; remains human-visible and may remain in top-level claim totals, but is excluded from active context, Atlas contradictions, agent-tool and hybrid-retrieval projections |
| `--pin` | — | Mark `operator-pinned`; pinned claims receive first retention priority in bounded context packs. The hard claim-count cap still applies, and any omitted pin count is surfaced explicitly |
| `--note` | `""` | Optional rationale recorded with the decision |
| `--scope` | `""` | Exact task scope from the reviewed structured claim. It must participate in the supplied UID and will not reach a same-worded claim in another task; empty is valid only for a genuinely unscoped live claim. It also selects the projection the evidence digest is validated against, so it must equal the `claims --scope` used to review. Portfolio-wide fallback writes remain a low-level migration capability, not a target the strict CLI fabricates from one scoped observation |
| `--metric` | `""` | Metric qualifier from the reviewed structured claim; participates in the stable UID |
| `--claim-uid` | *(required)* | Stable UID from the exact reviewed structured claim; must recompute from statement/scope/metric and still exist live |
| `--evidence-digest` | *(required)* | Evidence digest from the reviewed claim, taken from a `claims` read at this same `--scope`; a changed lesson/research snapshot — including a change only to the source's completeness receipt — rejects the write |
| `--expected-revision` | *(required)* | Claim-governance revision observed with the reviewed projection; stale concurrent policy writes reject the append |
| `--action-id` | *(required)* | Stable idempotency id. A lost-response retry returns the original durable receipt before CAS/freshness revalidation |

---

## `claim-steward`

PART IV §22.4 — the **AGENTIC claim steward**: an LLM reviews the evidence-grounded claim assessments (with
their support/oppose counts and epistemic state) and PROPOSES operator decisions — ratify a well-evidenced
consistent claim, reject a contradicted/over-generalized/noise claim, pin a load-bearing one. It reviews ONLY
machine-proposed claims (never re-litigates a human verdict). It is **proposal-only**: review the exact returned
claim identity, scope, metric and decision, then apply only selected decisions through typed `claim-decide` or
owner HTTP governance. The deprecated `--apply` spelling exits 2 **before model setup, paid inference or
mutation** and never re-runs an LLM batch for immediate application. Needs a reachable LLM. The on-demand
companion to finalize-time `cross_run_curation`. Its required action id has the same durable begun/terminal
recovery contract as `concept-steward`.

A proposal carries the claim's `statement`, `claim_uid`, `scope`, `metric` and suggested decision — **not** an
evidence digest, which is a property of a projection rather than of a proposal. To act on one, re-read the
claim at the proposal's own scope and use that receipt's digest:

```bash
looplab claim-steward MEMORY_DIR --action-id review-1 --json          # proposals carry scope + claim_uid
looplab claims MEMORY_DIR --scope <proposal scope> --structured --json --governance-receipt
looplab claim-decide MEMORY_DIR "<statement>" --ratify --scope <proposal scope> \
  --claim-uid ... --evidence-digest ... --expected-revision ... --action-id ...
```

That re-read is the point of the propose/ratify split: the operator ratifies the evidence that is live at the
moment of the write, not the evidence the steward happened to see.

```bash
looplab claim-steward MEMORY_DIR --action-id ID [--apply] [--model M] [--max-proposals 10] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `lessons.jsonl` (+ persisted claims) |
| `--apply` | off | **Deprecated and disabled.** Exits 2 before any LLM call or write. Run without it, review the exact proposal, then use typed `claim-decide` or owner HTTP governance |
| `--model` | *(config)* | Override the LLM model id |
| `--max-proposals` | `10` | Cap the total ratify/reject/pin proposals per pass |
| `--action-id` | *(required)* | Stable paid invocation identity used for crash-safe at-most-once reconciliation; reuse with another model/proposal budget or CLI/HTTP surface is rejected before client construction |
| `--json` | off | Emit `{proposals, receipt, invocation}` including the durable action receipt |

---

## `task-facets`

PART IV §21.20.2 — **AGENTIC task FACETING**: an LLM classifies a task's goal into a small fixed set of facets
(`domain` / `language` / `modality` / `interaction` / `objective`) so the system can recognize when two
differently-worded tasks are the same KIND of problem. An advisory OVERLAY only — it never touches the
deterministic passport fingerprint (`scope_profile`); facets live in their own append-only `task_facets.jsonl`.
They are currently stored/surfaced metadata reserved for a future post-scope ranking experiment: they grant no
visibility and do not change retrieval order. `build_index` stays byte-identical rebuildable. **PROPOSAL-ONLY**
(consistent with `concept-steward`/`claim-steward`, §22.4 — the agentic steward only proposes): it classifies +
prints; `--apply` is deprecated/rejected. Record the reviewed facets deterministically with `task-facets-set`.
Needs a reachable LLM. It uses the same required action-id and durable begun/terminal recovery contract as the
other paid steward CLIs. Proposal-only means it does not update `task_facets.jsonl` or ratify governance; the
command still appends its paid-call begun/outcome/proposal audit to `task_facets_curation_log.jsonl`.

```bash
looplab task-facets MEMORY_DIR "GOAL" --action-id ID [--kind K] [--model M] # propose + durable audit
looplab task-facets-set MEMORY_DIR TASK_ID --domain … --language … …     # operator record (deterministic)
```

| Argument / Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir; the command reads/writes `task_facets_curation_log.jsonl`, while only `task-facets-set` writes `task_facets.jsonl` |
| `GOAL` | *(required)* | The task goal to classify |
| `--kind` | `""` | Task kind (dataset/repo/…) — a hint for the classifier |
| `--apply` | off | **Deprecated and disabled.** Exits before client construction or paid work; record reviewed facets with `task-facets-set` |
| `--model` | *(config)* | Override the LLM model id |
| `--action-id` | *(required)* | Stable paid invocation identity used for crash-safe at-most-once reconciliation; reuse with another goal/kind/model is rejected before client construction |

---

## `task-facets-set`

The deterministic **operator** half of task faceting. It appends one reviewed, task-scoped overlay; it never
calls an LLM. Values are canonicalized with Unicode NFKC, case-folding and whitespace collapse. At least one
non-empty axis is required after normalization, so this command cannot perform an implicit clear. A later
valid row for the same exact `TASK_ID` replaces the effective facets while append-only history remains.

```bash
looplab task-facets-set MEMORY_DIR TASK_ID [--domain V] [--language V] [--modality V]
                         [--interaction V] [--objective V]
```

| Argument / Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir where `task_facets.jsonl` is appended |
| `TASK_ID` | *(required)* | Exact task identity governed by this overlay |
| `--domain` | `""` | Domain facet (for example `information-retrieval`) |
| `--language` | `""` | Language facet |
| `--modality` | `""` | Data modality facet |
| `--interaction` | `""` | Interaction/task-shape facet |
| `--objective` | `""` | Objective family facet |

---

## `atlas`

PART IV cross-run Step 6 (§21.20). A **capped Experimental portfolio summary** exposed by the historical
`atlas` command — one payload that composes the concept
overview (Step 3), claim assessments (Step 4) and the bounded context pack (Step 5) into **concept
observations** (concept × returned runs), concepts **observed in one returned run** (not an untried or
underexplored gap), and **mixed-evidence claim records** (both support and opposition references, not a
proposition-level contradiction verdict). The compatibility payload still uses the historical
`explored`/`thin_coverage`/`contradictions` keys. Each collection has an exact retained-snapshot
`*_total` / `*_omitted` receipt computed before its independent display cap; single-run observations and
rank tendencies are derived from the full canonical retained concept set rather than the overview's top 512.
The separate top-level `concept_source` receipt says whether the underlying capsules were complete. It reads the
available `lessons.jsonl`, `concept_capsules.jsonl`, persisted `research_claims.jsonl`,
`claim_decisions.jsonl`, `concept_aliases.jsonl` and `concept_splits.jsonl` sidecars; active contradictions exclude operator-rejected
claims, while top-level raw claim totals may still include them.
Aggregate concept buckets apply exact-slug aliases, but responses still carry display slugs and raw run cards
can disagree after a merge. The payload carries live governed source-health and snapshot digests, but has no
saved/frozen scope, ComparisonContract, portfolio watermark or versioned evidence/taxonomy release,
pagination, stable versioned identity or CoverageFrame contract; it is not the full backend in
[doc 18 §§28, 33](../18-ui-ux-review-2026-07-11.md).
The CLI accepts any non-empty combination of lessons, concept capsules or D8 `research_claims.jsonl`, including
a D8-only store. It remains a bounded live summary rather than a frozen/paged Atlas query.

```bash
looplab atlas MEMORY_DIR [--max-items 8] [--json]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding any of lessons, capsules or D8 research claims, plus optional decision and concept-governance sidecars |
| `--max-items N` | `8` | Cap per compatibility section (concept observations / mixed-evidence / observed in one run) |
| `--json` | off | Emit the capped lean Atlas-summary payload with per-section total/omission and capsule-source receipts as JSON |

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

For final-result approval, omitting `--node-id` approves the exact pending approval subject recorded in
the event log; it does not recompute the current best. `--node-id` does not apply to eval-spec approval.

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

What is reproducible on the toy backend: every **scientific** field (`best_metric`, `best_node`,
`nodes`/`evaluated`/`failed`, `reward_hack_flags`, `stop_reason`) and the folded `RunState`.
`benchmark.json` is never byte-identical — it records `eval_seconds`/`wall_seconds` — and the event
log's byte ORDER additionally depends on cross-run memory: the first suite run against an empty
`LOOPLAB_MEMORY_DIR` has no prior lessons and so lacks one `lessons_reconciled` event that every
later suite has. Pin `LOOPLAB_MEMORY_DIR` to a fresh (or pre-warmed) directory per suite when you
need log-byte comparability.

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
| `--build` / `--no-build` | `--build` | Verify/rebuild a missing, unstamped or stale default bundle (needs Node/npm); `--no-build` explicitly serves the existing prebuilt/stale bundle without freshness checks. A publish an earlier process was killed mid-swap is repaired **first, under every flag** (`--build`, `--no-build` and `--rebuild` alike), which needs no toolchain |
| `--rebuild` | off | Force a fresh `npm run build` even if a bundle already exists |

Dependency install plus Vite output are serialized by a required source-root interprocess lock. Freshness is
rechecked inside that lock, dependency manifest changes trigger reinstall (`npm ci`, with a visible
`npm install` fallback), and a lock/install/build/moving-input/stamp failure cannot certify or silently serve
an old bundle under a requested refresh.

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
| `--run-root DIR` | `$LOOPLAB_RUN_ROOT` or `runs` | Run-dir root, used only when auto-launching a server |

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

Build the React UI bundle (`ui/dist`) so `looplab ui` can serve it. Under the same required interprocess
lock as `looplab ui`, it rechecks source/dependency stamps, installs missing or manifest-mismatched
dependencies (`npm ci`, then a visible `npm install` fallback if needed), and runs `npm run build`.
Normally not needed — `looplab ui` builds on demand — but handy for CI or a warm-up step.

```bash
looplab build-ui [--force]
```

| Option | Default | Description |
|---|---|---|
| `--force` | off | Rebuild even if a bundle already exists |
