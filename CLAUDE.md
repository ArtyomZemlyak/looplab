# LoopLab — guide for coding agents

LoopLab is an autonomous ML/DS research engine: a Researcher proposes ideas, a Developer writes
code, a sandbox runs it, an evaluator scores it, and the loop refines/merges the best candidates.
**The append-only event log (`events.jsonl`) is the single source of truth**; all state is rebuilt
by replaying it. Design docs live in `docs/` (see `docs/02-architecture.md`, ADRs in
`docs/03-decisions.md`).

## Commands

```bash
pip install -e ".[dev,ui]"        # dev deps; [ui] needed for server/assistant/TUI tests (fastapi)
python -m pytest                  # full suite (~1.7k tests, a few minutes; addopts already has -q)
python -m pytest tests/test_events_replay.py           # targeted run — always do this first
python -m pytest -o addopts="" -q ...                  # if you need to override the default -q
python -m pytest -m "not docker"  # skip Docker-daemon tests
looplab run --no-genesis --kind quadratic --goal "min (x-3)^2" --direction min --out runs/demo  # offline smoke
# (--no-genesis matters: any --goal otherwise invokes Genesis, which needs a reachable LLM)
looplab replay runs/demo          # rebuild state from the event log (reproducibility check)
looplab timings runs/demo         # per-node wall-clock: LLM / eval / repair / tools (from spans.jsonl)
looplab ui                        # FastAPI server + React UI (see looplab/serve/)
```

The suite runs fully offline in ~1-2 minutes; live-LLM tests auto-skip (opt in with
`LOOPLAB_LIVE_SCENARIOS=1`). There is no lint/format config (no ruff/black); match the style of
surrounding code (~100-col lines, heavy why-comments) and do not reformat.
Docs are built with `mkdocs build --strict` in CI — broken doc links fail the deploy.
`looplab build-ui` builds the React UI (`npm ci && npm run build` in `ui/`); `looplab ui`
auto-builds when the dist is missing.

**Keep the docs and the process diagram in sync with the code — in the SAME change.** When you
change a default, a cadence/threshold, an event type, or add/rename a subsystem, update: (1) the
settings table in `docs/guide/configuration.md` (every `Settings` field must have a row with the
CORRECT default) and the relevant `docs/guide/*.md` page; (2) the **full process diagram**
`docs/infographic/agent-architecture.html` — a self-contained boxes-and-arrows flowchart whose
numbers/cadences/thresholds are verified against `looplab/` (embedded on `docs/guide/architecture.md`).
Stale docs/diagram are treated as a bug. The diagram is data-driven (a `B` block map + `E` edge list
in its inline `<script>`); edit the data, not hand-placed SVG.

## Package map (what lives where)

| Path | Contents |
|---|---|
| `looplab/core/` | foundation: domain models, `Settings` (config.py = schema, appconfig.py = loader), LLM client (`llm.py`), parsing, tracing, shared errors |
| `looplab/events/` | event store, `types.py` (event-type registry), `replay.py::fold` (event log → `RunState`), digest, readmodel, exporters + the pure UI projections (`traceview.py`, `htmlview.py`) |
| `looplab/runtime/` | sandboxes (subprocess/Docker tiers), command evaluation, dep install, background tasks |
| `looplab/tools/` | agent-facing tools; `_base.py` documents the ToolProvider contract; `env_inspect.py` (repo Developer's read-only env inspector: pkg version/API/source), `vectorstore.py` + `memora.py` (embeddings + harmonic index) |
| `looplab/agents/` | LLM personas: plain roles (`roles.py`), tool-loop roles (`agent.py::drive_tool_loop`), external CLI backend (`cli_agent.py`), facade (`unified_agent.py`) |
| `looplab/search/` | search policies (`policy.py` — action kinds/meta-key constants live here), `operators.py`, best-of-N, surrogate, archive, `foresight.py` (hypothesis prioritization / predict-before-execute), `hybrid_merge.py` (grep+BM25+vector RRF retrieval + agent-decided merge, shared by lesson & hypothesis-board consolidation) |
| `looplab/trust/` | gates that keep results honest: leakage, reward-hack, CV, redaction, confirmation |
| `looplab/engine/` | **the orchestrator loop** + cross-run memory; see invariants below. The `Engine` class spans THIRTEEN files: `orchestrator.py` (`__init__`, the `run` spine, node creation — the module-global `fold` seam lives here) + twelve mixins — `confirm_phase.py`, `ablation.py`, `novelty.py`, `strategy.py` (cadence; the Strategist agent is `agents/strategist.py`), `research_cadence.py`, `eval_stages.py`, `crash_repair.py`, `eval_dispatch.py`, `audit.py`, `evaluate.py`, `node_build.py`, `proposal_cues.py`. In a mixin `self` IS the Engine — grep the engine package before renaming an engine attribute or hunting a method |
| `looplab/adapters/` | task types (toy → dataset → repo → MLE-bench); the TaskAdapter contract is documented in `adapters/tasks.py` |
| `looplab/serve/` | FastAPI server (`server.py` is a thin composition root; routes live in `serve/routers/*` — control/runs/genesis/assistant/boss/org/reports/misc), TUI, assistant; never imported by the engine (the run-end projections live in `events/`) |
| `ui/` | React control plane (built artifacts served by `serve/server.py`) |

## Engine invariants (violating these breaks replay/resume)

1. **The engine is the sole writer of domain events.** Background tasks return values; only the
   main task appends — with ONE typed exception: the concurrent-research task may append the
   selection-neutral types in `events/types.py::BACKGROUND_APPENDABLE` (asserted at the append
   sites; `tests/test_background_appendable.py` proves splice-position neutrality). UI/CLI append
   only *control intents* (allow-listed in `serve/protocol.py::CONTROL_EVENTS`, enforced by
   `serve/routers/control.py`).
2. **Exactly one terminal event per node** (`node_evaluated` | `node_failed`). The fold is
   idempotent on duplicate terminals (first terminal wins).
3. **Every side effect must be gated on a domain event** so resume-by-replay is idempotent
   (`fork_done`, `inject_done`, `confirm_done`, `<x>_requests`/`<x>s_done` counter pairs).
4. **State is only observed via `fold(store.read_all())`** — never cache derived state across
   loop iterations without re-folding.
5. **`fold` must stay deterministic and order-tolerant**: no I/O, no LLM calls, unknown event
   types are ignored (forward compat), new event data fields are additive-only with reader-side
   defaults for old logs.
6. Settings recorded in the `run_started` event win over live config on resume.
7. Event type names are constants in `looplab/events/types.py`; a typo'd literal silently
   no-ops (unknown types are skipped), and `tests/test_event_types.py` guards against that —
   always add new event types to the registry.

## Conventions and traps

- **Back-compat import shim**: `looplab/__init__.py` aliases every pre-split flat module path
  (`looplab.orchestrator` → `looplab.engine.orchestrator`, …) via a meta-path finder; both names
  resolve to the SAME module object, so monkeypatching either path works. Many tests use old flat
  paths — keep the `_LAYOUT` map in sync when moving modules.
- **Layering**: `core` imports nothing above itself; `events` only `core`; `serve` may import
  anything; the engine must not grow new dependencies on `serve`.
- **Comments are load-bearing.** The codebase documents *why* (ADR references, review provenance,
  replay-safety notes) inline. Preserve comments verbatim when moving code; write the same style.
- **Prompt strings are contracts.** Changes to prompt text alter agent behavior — never "clean up"
  prompt wording as part of a refactor. Several prompts are routed through the PromptStore
  (`render(prompts, key, default)`); grep for `render(` to find overridable prompts.
- **Duck-typed seams are REGISTRY-GUARDED** — a rename that used to break silently is now a red
  test. The registries (each with a two-way source-scan test): TaskAdapter hooks
  `adapters/tasks.py::TASK_OPTIONAL_HOOKS`; role outputs `agents/roles.py::DEVELOPER_OUTPUT_ATTRS`
  / `RESEARCHER_ACTION_ATTRS`; hint attrs `agents/roles.py::RESEARCHER_HINT_ATTRS`; prompt keys
  `core/prompts.py::PROMPT_KEYS`; delivered signals `engine/signal_delivery.py::SIGNALS`;
  background-appendable events `events/types.py::BACKGROUND_APPENDABLE`. Adding/renaming any such
  seam means updating the registry in the SAME change. Note `search/foresight.py`'s
  panel is a `__getattr__` proxy over the wrapped agent: a typo'd read silently resolves to the
  base object, and an attribute SET on the panel shadows reads *through the panel* but does NOT
  reach the base's own `self.<attr>` reads until `forward_hints` mirrors it — set hints on the
  outermost wrapper and let the registry forward them, never on `base` directly.
- **Tool providers**: the `bind_state` hook is OPTIONAL (`tools/_base.py` — providers that don't
  need run state simply omit it), but a provider that DOES implement it must accept the second
  `parent` argument (`bind_state(self, state, parent=None)`) or it raises `TypeError` at dispatch.
- **Tests isolate the environment** (`tests/conftest.py`): dotenv loading is disabled and
  `LOOPLAB_MEMORY_DIR`/`LOOPLAB_KNOWLEDGE_DIR` point at tmp dirs. Engine tests construct
  `Engine(...)` directly (~100 call sites) — keep its keyword API stable.
- Settings are flat on purpose (`LOOPLAB_<FIELD>` env vars map 1:1); never nest or rename fields —
  snapshots and env compat depend on the names.
- `looplab/sweep.py` is NOT a CLI subcommand — it is a runtime helper imported by *generated*
  solution code inside the sandbox (see its docstring).
- A run directory contains `events.jsonl`, `config.snapshot.json`, `task.snapshot.json`,
  `engine.lock`, and per-node workdirs (`docs/guide/concepts.md` is accurate;
  `docs/04-file-layout.md` is the original *design* and differs from what shipped).
