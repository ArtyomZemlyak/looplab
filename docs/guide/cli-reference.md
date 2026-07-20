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

The original launch settings are restored from `config.snapshot.json`, so run-only flags are not silently
dropped. Five comparison/selection fields (`holdout_fraction`, `holdout_select`, `select_verifier`,
`select_verifier_samples`, `verifier_ci_tie`) are then restored from the folded `run_started` record;
`trust_gate_changed` owns later trust-gate edits. Those event-pinned semantics win over a stale or hand-edited
snapshot.

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

Print the raw on-disk launch config snapshot and the run's current folded best result. This is a diagnostic
view, not the effective per-run config API: the latter overlays the five `run_started`-pinned fields and the
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
needs a curated `--task-type` pack and cannot derive per-task importance). The five other Part IV diagnostics
below (`lock-in`, `board-dedup`, `research-targets`, `novelty-recall`, `lesson-guard`) share this
`--offline` opt-out contract; `asset-brief` is the exception — it stays offline-by-default (`--llm` opt-in)
because its agentic path is a heavier full tool-loop.

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
| `--jobs N` | `8` | Concurrent node-tagging calls in the agentic build |
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

PART IV D4 (§21.5) read-only analytic. Tags the hypothesis board and surfaces the dominant **within-concept**
redundancy (merge aggressively — e.g. the DCL cluster) plus **cross-branch** look-alike pairs a blind
lexical/vector merge would wrongly collapse (keep distinct). The LLM builds/tags the graph by default;
`--offline` forces the deterministic heuristic. Read-only; merges nothing.

```bash
looplab board-dedup RUN_DIR [--task-type NAME] [--offline] [--model ID]
```

| Option | Default | Description |
|---|---|---|
| `RUN_DIR` | *(required)* | Run directory whose hypothesis board to analyze |
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

It still does not expose each channel's per-hit contribution, carry evidence refs/source/scope/snapshot/index-
health on each result, enforce a ComparisonContract, or persist the derivation receipt. The CLI has no scope
option and reads the portfolio-wide stores. Bound agent callers instead pass a role-filtered snapshot scoped by
compatible direction plus exact task or a strict related-goal fingerprint for lessons/capsules. V2 D8 rows carry
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
| `--k N` | `8` | Requested result count; no product-level maximum/paging contract exists yet |
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
`--structured` is selected. Pure read; no LLM/endpoint.

```bash
looplab claims MEMORY_DIR [--top 20] [--contested] [--pack] [--fuzzy] [--structured] [--json]
               [--governance-receipt]
```

| Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir holding `lessons.jsonl` and/or `research_claims.jsonl` (or a lessons file) |
| `--top N` | `20` | How many most-evidenced claims to list |
| `--contested` | off | Show only `mixed` (support **and** oppose) claims |
| `--pack` | off | Render the hard claim-count-capped agent **context pack** (Step 5): pinned → ratified → mixed → support-only (`supported` wire state) → opposition-only (`refuted`) → insufficient; a caveat can replace the weakest non-pinned positive; omitted pins are counted explicitly. Concept tendencies are derived from the full retained pre-cap aggregate while the rendered labels remain bounded |
| `--fuzzy` | off | Suggestion-grade bounded token-Jaccard complete-link merge: every pair must clear the threshold and share scope, polarity and maturity; it is non-transitive and never scope-agnostic, but remains display/review grouping rather than claim identity |
| `--structured` | off | Group by the scope+polarity-safe **structured claim key** (`engine/claim_key.py`) instead of the display statement: claims from different tasks never merge, opposite-polarity assertions ("X helps" vs "X never helps") surface as a CONTRADICTION rather than collapsing, and grouping is O(n) exact-key (no transitive over-merge). Governance overlays by scope-precise `claim_uid` |
| `--json` | off | Emit the full assessments (or, with `--pack`, the pack) as JSON |
| `--governance-receipt` | off | With `--json`, emit `{claims, revision, structured}`. Use `--structured --json --governance-receipt` to obtain the exact UID/evidence-digest/revision inputs required by `claim-decide` |

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
| `--scope` | `""` | Exact task scope from the reviewed structured claim. It must participate in the supplied UID and will not reach a same-worded claim in another task; empty is valid only for a genuinely unscoped live claim. Portfolio-wide fallback writes remain a low-level migration capability, not a target the strict CLI fabricates from one scoped observation |
| `--metric` | `""` | Metric qualifier from the reviewed structured claim; participates in the stable UID |
| `--claim-uid` | *(required)* | Stable UID from the exact reviewed structured claim; must recompute from statement/scope/metric and still exist live |
| `--evidence-digest` | *(required)* | Evidence digest from the reviewed claim; a changed lesson/research snapshot rejects the write |
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
other paid steward CLIs.

```bash
looplab task-facets MEMORY_DIR "GOAL" --action-id ID [--kind K] [--model M] # propose (LLM), no write
looplab task-facets-set MEMORY_DIR TASK_ID --domain … --language … …     # operator record (deterministic)
```

| Argument / Option | Default | Description |
|---|---|---|
| `MEMORY_DIR` | *(required)* | Cross-run memory dir (where `task_facets.jsonl` lives) |
| `GOAL` | *(required)* | The task goal to classify |
| `--kind` | `""` | Task kind (dataset/repo/…) — a hint for the classifier |
| `--apply` | off | **Deprecated and disabled.** Exits before client construction or paid work; record reviewed facets with `task-facets-set` |
| `--model` | *(config)* | Override the LLM model id |
| `--action-id` | *(required)* | Stable paid invocation identity used for crash-safe at-most-once reconciliation; reuse with another goal/kind/model is rejected before client construction |

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
can disagree after a merge. It has no saved-scope, comparison, snapshot/health, pagination, stable versioned
identity or CoverageFrame contract; it is not the full backend in [doc 18 §§28, 33](../18-ui-ux-review-2026-07-11.md).
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
| `--build` / `--no-build` | `--build` | Verify/rebuild a missing, unstamped or stale default bundle (needs Node/npm); `--no-build` explicitly serves the existing prebuilt/stale bundle without freshness checks |
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
