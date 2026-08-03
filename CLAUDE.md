# LoopLab — guide for coding agents

LoopLab is an autonomous ML/DS research engine: a Researcher proposes ideas, a Developer writes
code, a sandbox runs it, an evaluator scores it, and the loop refines/merges the best candidates.
**The append-only event log (`events.jsonl`) is the single source of truth**; all state is rebuilt
by replaying it. Design docs live in `docs/` (see `docs/02-architecture.md`, ADRs in
`docs/03-decisions.md`).

## Commands

```bash
pip install -e ".[dev,ui]"        # dev deps; [ui] needed for server/assistant/TUI tests (fastapi)
python -m pytest                  # full suite (5k+ collected tests, a few minutes; addopts already has -q)
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
auto-builds when the dist is missing. `looplab/cli/` is a PACKAGE (command groups in
`run_cmds`/`export_cmds`/`inspect_cmds`/`ui_cmds`; the Typer app + patchable builders live in
its `__init__`; `python -m looplab.cli` works via `__main__.py`).

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
| `looplab/core/` | foundation: domain models, `Settings` (config.py = schema, appconfig.py = loader), LLM client (`llm.py` + its `llm_streaming`/`llm_toolcall`/`llm_transient` siblings — every split name re-exported through `llm.py`), parsing, tracing, shared errors; `pathsafe.py` (the single spelling of `is_reparse` / `WINDOWS_RESERVED` / `filesystem_identity`) and `atomicio.file_identity` (the canonical same-file-unchanged stat tuple) — a site needing a SUBSET of either documents why against the shared definition rather than re-deriving it; `jsonutil.py` (`canonical_json` + `canonical_json_digest`/`DIGEST_TEXT_CAP` — the ONE dump/hash tail every identity minter shares, preimages stay at their call sites) and `numeric.py` (`numeric_params`/`knn_idw`, re-exported from `events/digest.py`) |
| `looplab/events/` | event store, `types.py` (event-type registry), `replay.py::fold` (event log → `RunState`), digest, readmodel, exporters + the pure UI projections (`traceview.py`, `htmlview.py`, `comment_projection.py` = threaded-comment lifecycle projection), `span_index.py` (light span index → `spans.index.jsonl`), `notebook.py` (champion-notebook renderer, an export not a runtime) |
| `looplab/runtime/` | sandboxes (subprocess/Docker tiers), command evaluation, dep install, background tasks — process execution ONLY, and it imports nothing above `core` |
| `looplab/tools/` | agent-facing tools; `_base.py` documents the ToolProvider contract; `env_inspect.py` (repo Developer's read-only env inspector: pkg version/API/source), `vectorstore.py` + `memora.py` (embeddings + harmonic index) |
| `looplab/agents/` | LLM personas: plain roles (`roles.py`), the tool-loop machinery (`tool_loop.py::drive_tool_loop`/`agentic_*`; `agent.py` keeps `run_phase` + `ToolUsingResearcher` and re-exports the rest — patch seams resolve through `agent.py`), external CLI backend (`cli_agent.py`), facade (`unified_agent.py`) |
| `looplab/search/` | search policies (`policy.py` — action kinds/meta-key constants live here), `operators.py`, best-of-N, surrogate, archive, `panel.py` (the K-idea Researcher wrapper picked by the empirical surrogate), `foresight.py` (hypothesis prioritization / predict-before-execute), `hybrid_merge.py` (grep+BM25+vector RRF retrieval + agent-decided merge, shared by lesson & hypothesis-board consolidation), `proxy.py` (the predictive pre-eval scorer — a policy over folded `RunState`, sibling of `surrogate.py`) |
| `looplab/trust/` | gates that keep results honest: leakage, reward-hack, CV, redaction, confirmation |
| `looplab/engine/` | **the orchestrator loop** + cross-run memory; see invariants below. The `Engine` class spans SEVENTEEN files: `orchestrator.py` (`__init__`, the `run` spine, node creation — the module-global `fold` seam lives here) + sixteen mixins — `confirm_phase.py`, `ablation.py`, `novelty.py`, `strategy.py` (cadence; the Strategist agent is `agents/strategist.py`), `research_cadence.py`, `eval_stages.py`, `crash_repair.py`, `eval_dispatch.py`, `audit.py`, `resources.py`, `speculation.py`, `evaluate.py`, `node_build.py`, `proposal_cues.py`, and the two per-eval live-log watchdogs `train_monitor.py` (LLM health verdict + gated kill) and `asha_monitor.py` (live-curve rank vs finished siblings + opt-in kill; both append only fold-ignored DIAGNOSTIC events and reuse `_evaluate`'s `kill_signal`). In a mixin `self` IS the Engine — grep the engine package before renaming an engine attribute or hunting a method. The engine package also holds `costs.py` (the durable per-run `llm_usage`/`llm_cost` cost ledger + `.llm-usage-outbox`) and an expanded `finalize.py` (the `finalize_step`/`finalization_finished` wrap-up handshake) |
| `looplab/adapters/` | task types (toy → dataset → repo → MLE-bench); the TaskAdapter contract is documented in `adapters/tasks.py` |
| `looplab/serve/` | FastAPI server (`server.py` is a thin composition root; routes live in `serve/routers/*` — control/runs/genesis/assistant/boss/org/reports/misc/attention/collaboration/reviews/cross_run), TUI, assistant; the authoritative command lifecycle (`run_commands.py`), incremental command-ack observation (`command_observation.py`), the owner attention feed (`attention.py`), the isolated reviewer read namespace (`reviews.py`), and `jupyter.py` (the `jupyter_serverproxy_servers` launch spec); never imported by the engine (the run-end projections live in `events/`) |
| `ui/` | React control plane (built artifacts served by `serve/server.py`) |

## Engine invariants (violating these breaks replay/resume)

1. **The engine is the sole writer of domain events.** Background tasks return values; only the
   main task appends FOLDED events — with ONE typed exception: the concurrent-research task may append the
   selection-neutral FOLDED types in `events/types.py::BACKGROUND_APPENDABLE` (asserted at the append
   sites; `tests/test_background_appendable.py` proves splice-position neutrality). A concurrent task
   MAY additionally append `DIAGNOSTIC_EVENTS` (fold-ignored, so splice-neutral BY CONSTRUCTION — the
   fold never reads them): the training-monitor task appends `EV_TRAIN_MONITOR_ALERT` this way under
   `_write_lock`, asserting membership in `DIAGNOSTIC_EVENTS` at its append site.
   *Thread-side setup* is the remaining typed exception: `_ensure_run_setup` (`engine/eval_dispatch.py`)
   appends the FOLDED `events/types.py::SETUP_THREAD_APPENDABLE` pair from an eval WORKER THREAD
   (`_run_eval` under `anyio.to_thread`), outside `_write_lock`. Safe because `EventStore.append`
   serializes bytes, `_run_setup_lock` makes the section once-per-run, and the fold keys
   `run_setup_open`/`run_setup_done` purely BY COMMAND; membership is asserted at both append sites and
   `tests/test_setup_thread_appendable.py` proves splice-position neutrality. UI/CLI append
   only *control intents* (allow-listed in `serve/protocol.py::CONTROL_EVENTS`, enforced by
   `serve/routers/control.py`).
   *Concurrent build fan-out* (canonical `llm_parallel`; legacy `parallel_build`) is a further
   accepted seam: `_create_node` runs
   in worker threads (`anyio.to_thread`) that append their own node's FOLDED events (`node_created`,
   `node_failed`, per-node audit). This is safe because each thread writes an INDEPENDENT node's
   events, `EventStore.append`/`read_all` serialize via their own `threading.Lock`s, ids are reserved
   serially under `_id_lock` up front, and the fold is order-tolerant across independent nodes — so
   only the log's byte-order (not the folded state) becomes nondeterministic. A settled build width
   of `1` keeps the strict "only the main task appends" behaviour, byte-identical. The seam is
   OWN-NODE ONLY: a worker that needs a run-GLOBAL gate (the developer-crash / build-crash
   auto-pause) calls `_request_create_pause` and the MAIN task appends the `pause` after the join,
   via `_drain_create_pause` — a worker-written EV_PAUSE would race a concurrent EV_RESUME for byte
   position, which the fold is not order-tolerant across.
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
  anything; the engine must not grow new dependencies on `serve`. **`search` may import `agents` at
  module level; `agents` may reach `search` ONLY through a deferred (function-local) import.** That
  asymmetry is what keeps the cycle open — five search modules import `agents` at module scope, so a
  module-level `looplab.search` import in `agents/` closes the loop into an ImportError at startup.
  Guarded by `tests/test_agents_search_direction.py`.
- **Comments are load-bearing.** The codebase documents *why* (ADR references, review provenance,
  replay-safety notes) inline. Preserve comments verbatim when moving code; write the same style.
- **Prompt strings are contracts.** Changes to prompt text alter agent behavior — never "clean up"
  prompt wording as part of a refactor. Several prompts are routed through the PromptStore
  (`render(prompts, key, default)`); grep for `render(` to find overridable prompts.
- **An HTTP-contract change must move its tests in the SAME change.** Changing a route's verb,
  path, required params, response shape, or refusal codes silently retires every test that still
  speaks the old contract: the request 404s, 422s, or raises `ImportError` on a renamed private,
  and the assertion below it never runs. This is worse than a red test, because the properties
  these tests guard are disproportionately the security ones — token gating, share allow-listing,
  provider-error redaction, path-traversal allow-lists. A single day of contract hardening left
  23 such tests stranded across `test_server`/`test_assistant_*`/`test_attention`, and every one
  read as "a regression" when the production code was fine. When you change a contract: grep the
  suite for the old spelling, re-point it, and RE-VERIFY the property still holds rather than
  making the test green. If a refusal code now depends on a race, pin the fail-closed SET, not one
  member.
- **Duck-typed seams are REGISTRY-GUARDED** — a rename that used to break silently is now a red
  test. The registries (each with a two-way source-scan test): TaskAdapter hooks
  `adapters/tasks.py::TASK_OPTIONAL_HOOKS`; role outputs `agents/roles.py::DEVELOPER_OUTPUT_ATTRS`
  / `RESEARCHER_ACTION_ATTRS`; hint attrs `agents/roles.py::RESEARCHER_HINT_ATTRS`; prompt keys
  `core/prompts.py::PROMPT_KEYS`; delivered signals `engine/signal_delivery.py::SIGNALS`;
  background-appendable events `events/types.py::BACKGROUND_APPENDABLE` + its thread-side sibling
  `SETUP_THREAD_APPENDABLE`. Adding/renaming any such
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
