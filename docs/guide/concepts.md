# Concepts

This explains the machinery behind a run. It's the *how it works* companion to the task-oriented
guides. For the full design rationale and decision records, see [`../00-INDEX.md`](../00-INDEX.md).

## The loop

A run is an `Engine` (orchestrator) driving four roles in a cycle:

1. **Researcher** — proposes an `Idea` (an operator + params), reasoning about the goal and prior
   results. Foresight ranks the candidates *before* an eval; the idea also states a one-line
   **hypothesis** that lands on the board.
2. **Novelty gate** *(opt-in — `novelty_gate`, off by default; on under `profile=thorough`)* — rejects
   a proposal too close to one already tried (idea-text cosine ≥ `0.92`, or numeric param distance
   < `0.05`), with one informed re-propose.
3. **Developer** — implements the idea as runnable code (or applies params to an existing repo).
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
└── spans.jsonl           # diagnostic trace spans (regenerable; never read by replay)
```

Because the task and config are snapshotted, a run can be resumed from its directory alone.

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

- **Strategist** (`strategist_backend`, default `llm`) — an optional meta-controller that adapts the
  search policy, operator mix, and fidelity per situation. Every choice it makes is also a direct
  config knob, so you can run fully static (`off`).
- **Unified agent** (`unified_agent`, on by default) — one LLM identity plays Researcher +
  Developer (+ Strategist) across stages, choosing its model/toolset per stage and driving the next
  macro action within a *pure legal-action gate* that keeps pipeline discipline. Set
  `unified_agent=false` and `agent_drives_actions=false` for the legacy split-role behavior.

What an agent may change at runtime is governed by `agent_control` (a per-setting allow-list of
roles) — see [Configuration → Strategist & meta-control](configuration.md#strategist--meta-control).

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
