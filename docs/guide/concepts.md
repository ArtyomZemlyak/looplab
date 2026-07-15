# Concepts

This explains the machinery behind a run. It's the *how it works* companion to the task-oriented
guides. For the full design rationale and decision records, see [`../00-INDEX.md`](../00-INDEX.md).

## The loop

A run is an `Engine` (orchestrator) driving four roles in a cycle:

1. **Researcher** — proposes an `Idea` (an operator + params), reasoning about the goal and prior
   results. Foresight ranks the candidates *before* an eval; the idea also states a one-line
   **hypothesis** that lands on the board.
2. **Novelty gate** *(opt-in — `novelty_gate`, off by default)* — an algorithmic reject of a
   proposal too close to one already tried (idea-text cosine ≥ `0.92`, or numeric param distance
   < `0.05`), with one informed re-propose. **Off by default** (and *not* turned on by
   `profile=thorough`): duplicate-detection is the agentic Researcher's call — it reads past
   experiments and *decides* — and the semantic/embedding search only **suggests** candidates for the
   LLM to adjudicate.
3. **Developer** — implements the idea as runnable code (or applies params to an existing repo). On
   a fresh repo node it runs three phases — **stages → plan → implement** (see
   [below](#the-developers-three-phases-stages-plan-implement)); a repair is one focused session.
4. **Sandbox** — runs the candidate in isolation with a timeout and output caps.
5. **Evaluator** — scores it (cross-validation, a held-out grader, or a repo's own eval command);
   then the trust gates + optional multi-seed confirmation decide what may be selected best.

The **policy** then picks the next node to expand, and the cycle repeats until a budget is hit. At
the end, the top candidates can be **confirmed** under multiple seeds, and the best becomes the
**champion**.

```
Researcher → Novelty gate → Developer → Sandbox → Evaluator → trust/confirm → policy picks next → repeat → champion
```

## Event log = source of truth

The orchestrator is the **sole writer** of an append-only event log, `events.jsonl`. Everything that
happens — a node created, code run, a metric recorded, a merge, a confirmation, an approval — is an
appended event. Nothing else is canonical.

This buys two properties:

- **Reproducibility.** `looplab replay RUN_DIR` folds the log into the current state with a pure
  function (`replay.fold`) — no side effects, identical result every time.
- **Crash-resume.** A run can be hard-killed at any point; `looplab resume RUN_DIR` replays the log,
  reconstructs the exact frontier, and continues. No duplicated or lost work. The append-only writer
  tolerates a torn final line from a mid-write kill.

Derived views (the SQLite read-model, the HTML tree, trace spans) are all **regenerable** from the
log and are never read back by `replay`.

### Run directory

```
runs/<name>/
├── events.jsonl          # append-only event log — the source of truth
├── config.snapshot.json  # resolved settings at launch (secret-masked)
├── task.snapshot.json    # verbatim task copy → the run is self-describing
├── engine.lock           # single-writer lock (one live engine per run dir)
├── nodes/node_<id>/      # per-node eval workdirs (also confirm/ and ablate/ scratch dirs)
├── tree.html             # static lineage view (regenerable)
├── spans.jsonl           # diagnostic trace spans (regenerable; never read by replay)
└── spans.index.jsonl     # derived LIGHT span index for the UI trace views (regenerable cache;
                          #   ~25× smaller than spans.jsonl — see below)
```

The **light span index** (`spans.index.jsonl`) is a derived cache the UI's trace views read instead
of parsing the whole (up to multi-GB) `spans.jsonl` on every click: it holds each span's structural
fields (ids, kind, timing, token usage) minus the heavy prompt/output/reasoning, plus the byte offset
of the full span in `spans.jsonl`. The run-level timeline reads only this ~25×-smaller file; a
per-span/per-node detail view seeks straight to the needed byte range. It is maintained incrementally
and is *strictly an accelerator* — if it is missing, stale, or corrupt the views transparently rebuild
it from `spans.jsonl` (the sole source of truth), so results are always identical, never lost.

The agent tool-loop re-sends the whole growing conversation to the LLM every turn, which once made
each generation's recorded input a near-duplicate of the last. That input is now **delta-encoded** at
write time — a generation that only appended to the prior turn stores just the appended messages plus a
back-reference — so `spans.jsonl` itself is ~6× smaller before the index even applies; the trace views
reconstruct the full verbatim prompt on demand when you expand a single call.

Because the task and config are snapshotted, a run can be resumed from its directory alone.

## Stopping & resuming a run — three verbs

Operator control (the UI transport buttons, the TUI `stop/finalize/resume`, the `looplab` commands,
and the boss chat) is exactly **three verbs**, all appended as events to the log:

| Verb | Event | Effect | Wrap-up? |
|---|---|---|---|
| **stop** | `pause` | Freeze the run where it is. Reversible. | **No** — no report/lessons/cost |
| **finalize** | `run_abort` → `run_finished` | Stop **and** run the end-of-run wrap-up | **Yes** — report, cross-run lessons + KB case, cost roll-up, tree.html |
| **resume** | `resume` | Continue from any stopped state (stopped / finalized / naturally finished) | — |

The one real difference is **finalization**: the end-of-run wrap-up (`finalize.py`) is gated on the
run being *finished*, and only **finalize** sets that. **stop** is a cheap freeze — so you can stop to
look at something and `resume` with no premature/duplicate lessons, or `finalize` later to wrap it up
(finalize works on a live *or* an already-stopped run). `resume` lifts every stopped state, so it also
"reopens" a run that finished naturally to continue it with more budget. (Under the hood the fold still
understands the legacy `run_reopened` alias of `resume` for old logs.)

**Reopening a *finished* run starts a new search epoch.** The nodes you add with the extra budget are
a fresh candidate set, so reopening bumps `search_epoch` and re-opens the promotion gates: the
multi-seed **confirmation** pass runs again (already-confirmed nodes are reused for free, only the new
candidates spend seeds) and, under HITL, **approval** is requested again for the possibly-new champion.
Without this, a champion confirmed in the first epoch would lock selection and a strictly-better node
found after the reopen could never be confirmed or win. (Reopening a merely *stopped* run — one that
never finished — is the same epoch and leaves those gates untouched.)

The UI also shows a node the **instant** the engine starts building it: a transient `node_building`
marker (folded to a `building` slot, *not* the event-sourced node set, so it never affects node-id
allocation or resume) that streams the node's live agent-trace right away, then is superseded by
`node_created` when the node materializes — or dropped if the run ends first.

## Search policies

The policy decides which node to expand next. It's pluggable (`make_policy`) and swapping it changes
no other part of the loop:

| `policy` | Behavior |
|---|---|
| `greedy` (default) | Greedy tree search with a multi-parent merge — strong because the *operators* do the heavy lifting |
| `asha` | ASHA / successive-halving: wide cheap base, keep the top 1/η per rung (`asha_eta`, `asha_rung_nodes`) |
| `mcts` | Monte-Carlo tree search |
| `evolutionary` | Evolutionary policy with a diversity archive (`archive_resolution`) |

Add `policy=bohb` behavior by combining ASHA racing with the surrogate proposer
(`surrogate_proposer`).

## Operators

The win comes from rich operators, not exotic search. The Researcher/Developer apply:

- **draft** — a fresh candidate.
- **improve** — refine the current best.
- **debug / repair** — on a crash, hand the failing code + stderr back to fix it. Mechanical crashes
  can be repaired **in place** within the same eval (`inline_repair`), which doesn't consume the
  node budget; deeper failures get a structured "reproduce then fix" directive (`deep_repair`).
- **ablation-driven refinement** — neutralize a parameter (or a whole code block with
  `ablate_code_blocks`) to find the highest-impact lever, then refine it (`ablate_every`).
- **merge / ensemble** — recombine two parents: a param mean, or a code-recombination ensemble
  (`merge_mode=ensemble`). This is the multi-parent DAG.
- **sweep** — one node runs a whole grid of trials in a single process.

## The Developer's three phases (stages → plan → implement)

On a fresh (non-repair) implement of a repo node the Developer runs **three separately-traced
phases**, each its own focused tool-loop so the context stays small and the trace reads cleanly
(`Developer · stages → plan → implement`):

1. **STAGES** (mandatory, **first** — unless the operator pre-empts it, below) — a **read-only**
   phase whose only exit is a `declare_stages`
   emit. The repo-savvy Developer studies the repo *and* the operator's `cmd`, then declares the
   ordered eval pipeline (`data_prep → train → …`) that runs **before** the operator's protected
   `score` step, baking **this node's** hyperparameters into the `train` command. It writes
   `looplab_stages.json`. The Developer owns the stages, **not** the planner/Genesis — the phase is
   skipped only when the **operator** pre-empts it: a valid `cmd.stages` pipeline (the engine uses
   it verbatim; the Developer implements the code those stages run) or a protected
   `looplab_stages.json` (the knob that disables Developer pipelines — skipping avoids burning an
   LLM loop whose manifest would be dropped). Good practice: separate stages for data/feature
   **preparation**, **training** (a fresh model every node — never reuse a checkpoint), and
   **testing**.
2. **PLAN** — the read-only atomic-step decomposition (`propose_plan`; **C4**,
   `developer_plan_decompose`), unchanged.
3. **IMPLEMENT** — writes the code the stages run, one bounded session per plan step.

A **repair** (an error to fix) stays a single focused session — no stages, no plan.

The **cmd-context rule** governs the stages phase: the operator's `cmd` is passed in as context. If
it is **present**, it is shown as **immutable** — the Developer declares only the *preceding*
stages. If it is **absent**, the Developer must declare the **full** pipeline, including a final
stage that runs the evaluation and prints the metric. Either way the stage name `score` is
**reserved** (it always denotes the engine-appended operator step) — with no `cmd`, name the
scorer e.g. `evaluate`.

## Multi-stage eval pipeline

For a repo task the eval is a **declared pipeline of named stages** instead of one opaque command.
The Developer declares the **preceding** stages in its dedicated STAGES phase above (or the operator
sets them on the `cmd` via `eval.stages`); the operator's `cmd` is appended as the final, protected
`score` stage (the trust boundary — the agent adds work before scoring but never rewrites it):

```json
{"stages": [
  {"name": "data_prep", "command": ["python", "prep.py"]},
  {"name": "train",     "command": ["python", "train.py"], "timeout": 7200, "check": true}
]}
// + the operator's cmd (e.g. ["python", "test.py"]) runs last as the protected `score` stage
```

Stages run in order in the **same workdir** so artifacts persist (train writes a checkpoint → eval reads
it). Each stage gets its own span + `<name>.log` and a pass/fail (`stage_finished` events fold onto
`node.stages`). Three payoffs:

- **A crash is pinpointed** to its stage (`node.failed_stage`), not hidden behind one command — a run
  that never actually trains is obvious (no `train` stage / a red one).
- **Fix only the broken stage** — re-run the node *from* a stage (the Overview's clickable "eval
  pipeline" strip, `reset(stage)` in chat, or a `node_reset` with the stage name): earlier stages are
  marked *reused* and skipped, so a failed `eval` is fixed in seconds without paying to re-`train`.
- **Optional inter-stage verify** — a stage flagged `"check": true` hands its output to an agentic
  checker (Researcher/Developer) before the next stage runs; a concern stops the pipeline early so a
  diverged train can't silently feed eval.

The operator's `cmd` is the **authoritative, non-rewritable scoring stage** and its stdout is where the
trusted metric reader reads. The Developer's STAGES phase supplies only the stages that run BEFORE it
(`data_prep`, `train`, …); the engine appends `cmd` as the final protected `score` stage. When `cmd`
itself declares `stages`, those are canonical (the agent implements the scripts, not the structure). With
no operator `cmd` at all, the STAGES phase declares the full pipeline including the final scoring stage.
A `%params%` token in any command expands to the node's tuned hyperparameters.

## Evaluation rigor

A reported number is only useful if it generalizes. The trust layer is leakage-first:

- **Consistent cross-validation** — K-fold and purged walk-forward (no look-ahead) so every
  candidate is scored the same way.
- **Leakage detectors** — train/test contamination, target leakage, and temporal leakage are
  flagged.
- **Variance gate** — a candidate must beat the incumbent by more than ~1 standard error to be
  promoted, so noise doesn't crown a lucky run.
- **Multi-seed confirmation** — at the frontier, re-run the top-k under several seeds
  (`confirm_top_k`, `confirm_seeds`) and pick the robust best. A seed-lucky leader is demoted.

The replay model keeps these promotions honest across resets and reopens:

- **Attempt identity** — re-running a node in place (`node_reset`) bumps that node's *attempt*
  generation. A late terminal from the attempt the reset abandoned (its eval was still in flight)
  carries the old generation and is dropped, so it can't land a metric from the discarded code onto
  the new attempt.
- **Subject-bound approval** — a human `approve` grant is folded only if it names a real candidate
  node; a forged/stale grant for a node that isn't in the run can't silently flip the run to approved.

## Trust & the sandbox

The sandbox tier is chosen by **trust mode**, not your environment (`make_sandbox`):

| `trust_mode` | Sandbox | Use |
|---|---|---|
| `trusted_local` (default) | `SubprocessSandbox` | Your own research on your own box. Process isolation + timeout + tree-kill + output caps. **No Docker.** |
| `untrusted` | `DockerSandbox` (`--network none`) | Executing untrusted code on shared infra (hosted/multi-tenant UI) |
| `hostile` | `DockerSandbox` (`--network none` + gVisor `--runtime runsc`) | Actively hostile code — a real kernel-level isolation boundary |

Additional, audit-only safety monitors (all off by default, never change selection):

- `redact_output` — mask credentials in stdout/stderr before they're persisted.
- `reward_hack_detect` — flag suspicious wins (grader/answer-key access, frozen-file writes,
  suspiciously perfect metrics).
- `code_leakage_detect` — static scan for fit-before-split / fit-on-test.
- `critic_check` — an execution-free critic of each solution.

These surface in the UI's Trust panel as audit events. See [Deployment](deployment.md) for the
untrusted tier.

## Meta-control: the Strategist & unified agent

- **Strategist** (`strategist_backend`, default `agent`) — a meta-controller that adapts the
  search policy, operator mix, and fidelity per situation. It defaults to the **agentic** backend: a
  tool-using loop that *reads* the run / data / siblings / KB / memory before deciding (the `llm`
  backend is a single-shot call over aggregate stats; `rule` is a fixed heuristic; `off` runs fully
  static). Every choice it makes is also a direct config knob, so you can run fully static (`off`). At its consult cadence it reads a **coverage
  read-model** (`coverage_context`, on by default): a deterministic breadth summary of the run so
  far — distinct themes and parameter-niches, the theme entropy, and the dominant-theme fraction —
  recorded as a `coverage_snapshot` audit event (the run's *narrowing curve*). This is context, not
  a decision: it gives the controller eyes on whether the search is broadening or collapsing onto a
  single line of attack, so breadth can be a deliberate signal rather than only a reaction to metric
  stagnation. From that reading the Strategist sets a **novelty stance** (`explore` / `balanced` /
  `exploit`) — the single dial for how hard the run pushes for NEW directions. `balanced` is today's
  behavior; `explore` (chosen when coverage shows narrowing) threads one directive into the three
  places ideas are shaped — the Researcher's proposal (propose a different theme), the foresight
  rank (break near-ties toward the more divergent candidate), and the novelty gate (engage a soft
  dedup + one informed re-propose even when the static gate is off) — so novelty pressure is one
  meta-decision, applied coherently, and always via the LLM roles rather than a hard-coded rule.
- **Unified agent** (`unified_agent`, on by default) — one LLM identity plays Researcher +
  Developer (+ Strategist) across stages, choosing its model/toolset per stage and driving the next
  macro action within a *pure legal-action gate* that keeps pipeline discipline. Set
  `unified_agent=false` and `agent_drives_actions=false` for the legacy split-role behavior.

What an agent may change at runtime is governed by `agent_control` (a per-setting allow-list of
roles) — see [Configuration → Strategist & meta-control](configuration.md#strategist-meta-control).

## Cross-run memory

Cross-run memory is **on by default** — `memory_dir` / `knowledge_dir` default to
`~/.looplab/memory` and `~/.looplab/knowledge` (set `LOOPLAB_MEMORY_DIR=""` to disable). The best
result of each run is retained as a **case** (retain-on-improvement); at run end `reflection_priors`
(also on by default) distills a causal **meta-note** ("why the winner won"), generalizable
**lessons** (good *and* bad, with a verdict + evidence count), and reusable **skills** — all stamped
with a task fingerprint and matched into the next similar run's proposal prompt. Duplicate lessons
are merged (exact-hash **plus** a hybrid-retrieval → agentic paraphrase-merge pass); the in-run
**hypothesis board** is deduped the same way and prioritized by foresight. See
**[Memory & knowledge](memory.md)** for the full tier-by-tier breakdown.

This shipped lesson/case memory should not be confused with the planned **portfolio research index**. At
present, concept tags and concept coverage are per-run, and cross-run retrieval does not yet maintain a
versioned technology graph plus application/domain/data/objective applicability. The target CR0–CR3 design
adds evidence-grounded claims, incremental run/portfolio summaries, structured “tried before” context and a
Research Atlas for 50–500 heterogeneous runs; see
[Project review §21.20](../17-project-review-and-directions-2026-07-11.md#cross-run-research-architecture).

### Harmonic memory (`memora`, optional)

An idea import from [Memora](https://github.com/microsoft/Memora) (Microsoft Research, ICML'26).
**On by default** (`memora=true`): the case library + knowledge index key each memory not by its
**raw text** but by a short **abstraction** (a 6–8 word essence) plus a few **cue anchors** (tags
giving alternative retrieval paths). Three things follow:

1. **Abstraction + anchors as the index** — only the abstraction/anchors are embedded; the rich memory
   value is stored alongside, unindexed.
2. **Consolidation on write** — a new memory whose abstraction closely matches an existing one is
   *merged* into it (union of anchors, better metric kept) instead of adding a near-duplicate, so the
   index carries roughly half the entries of a flat store.
3. **Anchor-expansion on retrieval** — `kb_search` / case lookup follow the top hits' anchors to
   surface *related-but-not-similar* memories the plain query missed.

**LLM-optional by design.** Abstractions are written by the wired chat model (`memora_llm=true`, the
default) — **cached** by content hash so a re-built index never re-calls the model on unchanged
notes/cases, and degrading to a deterministic **lexical** abstractor whenever the endpoint is
unreachable (so an offline box just gets lexical abstractions, never a crash). Set `memora_llm=false`
to force lexical everywhere, or `memora=false` to restore the pre-Memora raw-text index. Like the rest
of cross-run memory, abstractions live only in the derived, rebuildable retrieval index — never in the
event log or the canonical `cases.jsonl`.

## Observability

Every step emits a trace **span** to `spans.jsonl` (files-as-truth, zero-dep). With `trace_llm_io`
on (default), each LLM call's full prompt + completion is captured so the UI can show exactly what
the model read and wrote per node. Installing the `[otel]` extra and setting `OTEL_*` sends the same
spans to any OTLP collector (Jaeger / Tempo / Honeycomb) with no code change. Spans are diagnostics
only — `replay` never reads them.

**Per-operation traces.** A node's own work (propose → implement → repair, then evaluate/training)
is one trace, shown under the node. But every OTHER LLM sub-operation runs in its **own** named trace
(`new_trace`) — `strategist_consult`, `hypothesis_merge`, `deep_research`, `report`, `lessons_distill`/
`lessons_refresh`, and the two Researcher ranking steps — `hyp_prioritize` behind `hypothesis_ranked`
(board prioritization) and `foresight_rank` behind `foresight_selected` (idea predict-before-execute). The event
that operation emits is **stamped with that trace's id** (the event store reads the active span's ids
on append; a telemetry event whose op-span already closed carries the captured id explicitly), so the
UI expands that event's row to ONLY that operation's trace — never the whole node's Researcher+Developer
tree. `GET /api/runs/{id}/trace/by_trace/{trace_id}` returns one operation's span sub-tree; the node's
full trace is at `/api/runs/{id}/trace`. Events with no LLM (e.g. `coverage_snapshot`, deterministic)
carry no trace; a `node_evaluated` row shows the **training** run (the `evaluate` span), not an LLM call.

Every one of these auxiliary LLM steps is now **agentic** (via the shared `agentic_text` /
`agentic_struct` helpers): lessons distillation (reflect / comparative / skill / causal), the
research + reward-hack / leakage **verify** pass, the end-of-run **report**, and **Genesis** (goal →
task plan) each *read* the real experiments / code / data through read-only tools before emitting,
rather than reasoning single-shot over a text preview. (`best_of_n` and `hybrid_merge` ride the same
agentic path but with `tools=None` — there is no run state to read at those call sites.)

**Live status.** The UI reads what an LLM is doing right now from the append-only markers: a
`node_building` marker (emitted the instant `_create_node` starts, before the minutes-long author step)
drives a `✍️ writing` / `🔧 repairing` / `🔀 merging` status (by the node's operator) and streams that
node's trace live; a `pending` node is being **trained** (the sandbox eval — no LLM), shown as
`running (training)` with no live pulse. The assistant chat streams the same way — interstitial prose
(`SSE_TEXT`) and tool steps (`SSE_STEP`) between tool rounds, Claude-Desktop-style.

## Module map

Where each concept lives in the code:

| Concept | Module |
|---|---|
| Domain models + event envelope | `models.py` |
| Layered settings + masked snapshot | `config.py` |
| Append-only log / pure fold / SQLite read-model | `eventstore.py`, `replay.py`, `readmodel.py` |
| Sandbox seam + subprocess/Docker bodies | `sandbox.py` |
| Researcher/Developer roles (toy + LLM) | `roles.py`, `unified_agent.py` |
| Structured output + LLM client + cost accountant | `parse.py`, `llm.py` |
| Operators (merge/ensemble, sweep) | `operators.py`, `sweep.py` |
| Control loop + crash-resume | `orchestrator.py` |
| Variance gate + multi-seed confirmation | `gate.py`, `confirm.py` |
| CV harness, K-fold, purged walk-forward | `cv.py` |
| Leakage detectors + data profiler | `leakage.py`, `profile.py` |
| Vector store + agentic retrieval | `vectorstore.py`, `retrieval.py`, `knowledge_tools.py`, `agent.py` |
| Cross-run case library | `memory.py` |
| Trace span exporter | `tracing.py` |
| Search policies | `policy.py` |
| Static HTML lineage tree | `htmlview.py` |
| Task adapters + loader | `tasks.py`, `toytask.py`, `regression.py`, `classification.py`, `timeseries.py`, `mlebench*.py`, `repo_task.py` |
| Strategist / Deep-Research / report | `strategist.py`, `deep_research.py`, `report.py` |
