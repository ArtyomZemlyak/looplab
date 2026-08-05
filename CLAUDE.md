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
looplab run --no-genesis --kind quadratic --goal "min (x-3)^2" --direction min --backend toy --out runs/demo  # offline smoke
# (--no-genesis matters: any --goal otherwise invokes Genesis, which needs a reachable LLM.
#  --backend toy is now REQUIRED for an offline run: the `backend` default was changed from
#  "toy" to "llm" on 2026-08-04, so without it this command hits the LLM endpoint preflight.)
looplab replay runs/demo          # rebuild state from the event log (reproducibility check)
looplab timings runs/demo         # wall-clock: per node + run-level (from spans.jsonl), reconciled
                                  # against the run's duration (events.jsonl first->last ts), residual named
looplab ui                        # FastAPI server + React UI (see looplab/serve/)
```

The suite runs fully offline in ~1-2 minutes; live-LLM tests auto-skip (opt in with
`LOOPLAB_LIVE_SCENARIOS=1`). There is no lint/format config (no ruff/black); match the style of
surrounding code (~100-col lines, heavy why-comments) and do not reformat.
Docs are built with `mkdocs build --strict` in CI — broken doc links fail the deploy.
`looplab build-ui` builds the React UI (`npm ci && npm run build` in `ui/`); `looplab ui`
auto-builds when the dist is missing. `looplab/cli/` is a PACKAGE (command groups in
`run_cmds`/`export_cmds`/`inspect_cmds`/`concept_cmds`/`governance_cmds`/`ui_cmds` — `inspect_cmds`
is run diagnostics ONLY, the Part IV concept/novelty diagnostics are `concept_cmds`, and everything
that WRITES cross-run memory or spends money on a steward is `governance_cmds`; the Typer app +
patchable builders (including the shared `_make_llm_client`/`_settings_for_run`) live in
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
| `looplab/core/` | foundation: domain models (`models.py` — `Idea`/`Node`/`RunState`; card IDENTITY split out into `cards.py`: the versioned idea/card action digests, the three ownership-receipt constructors, the footprint + steering-context vocabularies they bind, and `Card` with its provenance family, ALL re-exported through `models.py` so both spellings name the SAME objects), `Settings` (config.py = schema, appconfig.py = loader), LLM client (`llm.py` + its `llm_streaming`/`llm_toolcall`/`llm_transient` siblings — every split name re-exported through `llm.py`), `llm_broker.py` (run-local concurrency ADMISSION for outbound provider calls — a closed lane vocabulary selected through a `ContextVar`, borrowed by both clients around the real request only, so a producer can be scoped without holding capacity while it reads files or runs tools), parsing, tracing, shared errors; `pathsafe.py` (the single spelling of `is_reparse` / `WINDOWS_RESERVED` / `filesystem_identity`) and `atomicio.file_identity` (the canonical same-file-unchanged stat tuple) — a site needing a SUBSET of either documents why against the shared definition rather than re-deriving it; `jsonutil.py` (`canonical_json` + `canonical_json_digest`/`DIGEST_TEXT_CAP` — the ONE dump/hash tail every identity minter shares, preimages stay at their call sites), `setup_identity.py` (the two run-start digests `Engine._setup_phase` stamps — `run_started.config_hash` dumps the task payload UNSORTED, `setup_finished.manifest` hashes a SORTED inner config hash beside the workspace/provenance; `search/speculation_quality.py` re-derives both to prove calibration evidence came from the shipped writer, so the derivation lives here rather than being hand-copied) and `numeric.py` (`numeric_params`/`knn_idw`, re-exported from `events/digest.py`); `jsonlio.py` (the GENERIC JSONL store I/O — lenient read + health, region scan, atomic write/rewrite — moved down out of `events/` so a subsystem that only wants to read a JSONL file need not import `events`; `events/eventstore.py` re-exports the whole surface, so both spellings name the SAME objects and every existing monkeypatch seam through `looplab.events.eventstore` still works) |
| `looplab/events/` | event store, `types.py` (event-type registry), `replay.py::fold` (event log → `RunState`), digest, readmodel, exporters + the pure UI projections (`traceview.py`, `htmlview.py`, `comment_projection.py` = threaded-comment lifecycle projection, `belief_projection.py` = the BELIEF view over the card board — derived, never folded state), `card_ledger.py` (the derived Card ledger split out of `replay.py` — the fold-time `_bounded_card_*` receipt bounds its `card_*` handlers admit through, plus `derive_cards(st)`, the once-per-fold post-pass `_finalize_fold` runs over already-folded state; a LEAF that imports only `core`, so `replay` imports it and never the other way round, and only the names replay's own handlers CALL are re-exported — a re-export of a helper the ledger calls internally would look like a patch seam while a monkeypatch through it missed the fold), digest, readmodel, exporters + the pure UI projections (`traceview.py`, `htmlview.py`, `comment_projection.py` = threaded-comment lifecycle projection, `belief_projection.py` = the BELIEF view over the card board — derived, never folded state), `span_index.py` (light span index → `spans.index.jsonl`), `notebook.py` (champion-notebook renderer, an export not a runtime), and the two finalization modules `search` may import DOWNWARD but the engine owns the writing of: `finalize_scope.py` (the finalize-SCOPE read side) and `finalize_protocol.py` (the step-name vocabulary, the `budget` receipt constructor + its exact field set, and `QUIET_FINALIZATION_SUFFIX` — the ordered event suffix a finalization with nothing optional configured emits; `engine/finalize.py` writes it and `search/speculation_quality.py` refuses calibration evidence that does not match it, so a second hand-synced copy breaks the gate silently) |
| `looplab/runtime/` | sandboxes (subprocess/Docker tiers), command evaluation, dep install, background tasks — process execution ONLY, and it imports nothing above `core` |
| `looplab/tools/` | agent-facing tools; `_base.py` documents the ToolProvider contract; `env_inspect.py` (repo Developer's read-only env inspector: pkg version/API/source), `vectorstore.py` + `memora.py` (embeddings + harmonic index) |
| `looplab/agents/` | LLM personas: plain roles (`roles.py`), the tool-loop machinery (`tool_loop.py::drive_tool_loop`/`agentic_*`; `agent.py` keeps `run_phase` + `ToolUsingResearcher` and re-exports the rest — patch seams resolve through `agent.py`), external CLI backend (`cli_agent.py`), facade (`unified_agent.py`), and `preflight.py` — the pre-run reachability probe for every role's endpoint, called from `cli/__init__.py::_engine` BEFORE any role is built so an unreachable model refuses the run instead of degrading it to fallback proposals that report success |
| `looplab/search/` | search policies (`policy.py` — action kinds/meta-key constants live here), `operators.py`, best-of-N, surrogate, archive, `panel.py` (the K-idea Researcher wrapper picked by the empirical surrogate), `foresight.py` (hypothesis prioritization / predict-before-execute), `hybrid_merge.py` (grep+BM25+vector RRF retrieval + agent-decided merge, shared by lesson & hypothesis-board consolidation), `proxy.py` (the predictive pre-eval scorer — a policy over folded `RunState`, sibling of `surrogate.py`), and the PART IV/V **concept cluster** — five modules in a strict layer order that `concept_graph.py` alone used to be (doc 25 SE-09): `concept_graph.py` (the `Concept`/`ConceptGraph` axis-DAG + the curated task skeletons) ← `concept_tagging.py` (heuristic + LLM taggers; `experiment_nodes`/`node_text` are the two surfaces every tagger describes an experiment by) and `concept_lens.py` (the hierarchy/lens view projections `serve/` and `tools/` read — they stay in `search` because `tools` may not import `serve` and `events` may not import `search`) ← `concept_analytics.py` (the PURE coverage/metrics/alarm read-models) ← `concept_map.py` (LLM consolidation + `build_concept_map`). There is deliberately NO re-export façade on `concept_graph.py` (a stale spelling must be an ImportError, not a resolving alias), and a cluster module reaches a sibling's FUNCTIONS through the module object — a `from … import <fn>` binds by value and would silently cost the monkeypatch seams the one-file version had. `tests/test_concept_module_split.py` drives both rules |
| `looplab/trust/` | gates that keep results honest: leakage, reward-hack, CV, redaction, confirmation |
| `looplab/engine/` | **the orchestrator loop** + cross-run memory; see invariants below. The `Engine` class spans NINETEEN files: `orchestrator.py` (`__init__`, the `run` spine, node creation — the module-global `fold` seam lives here) + eighteen mixins — `confirm_phase.py`, `ablation.py`, `novelty.py`, `strategy.py` (the Strategist consult/apply/coverage core; the Strategist agent is `agents/strategist.py`), `concept_cadence.py` (the PART IV/V concept subsystem the strategist cadence used to carry: classifier re-tag / consolidation / edges / hypothesis tags / concept-coverage snapshot / run-base seed — on its OWN `concept_retag_every` gate, NOT `strategist_every`), `verifier_tiebreak.py` (R1-c calibrated-verifier metric tie-break — SELECTION machinery, it can move the reported champion), `research_cadence.py`, `eval_stages.py`, `crash_repair.py`, `eval_dispatch.py`, `audit.py`, `resources.py`, `speculation.py`, `evaluate.py`, `node_build.py`, `proposal_cues.py`, and the two per-eval live-log watchdogs `train_monitor.py` (LLM health verdict + gated kill) and `asha_monitor.py` (live-curve rank vs finished siblings, with the rank result handed to an LLM judge that owns the stop decision and can only make the gate stricter + opt-in kill; both append only fold-ignored DIAGNOSTIC events and reuse `_evaluate`'s `kill_signal`). In a mixin `self` IS the Engine — grep the engine package before renaming an engine attribute or hunting a method. A TWENTIETH, `shared.py::SharedEngineMixin`, is the home for members that belong to every cluster and therefore to none (`_agent_may`, `_op_span`, `_cadence_due`, plus `effective_researcher_eval_timeout`): the bar is called-from-more-than-one-cluster AND no state of its own, so it does not become a second god-module. The engine package also holds `costs.py` (the durable per-run `llm_usage`/`llm_cost` cost ledger + `.llm-usage-outbox`) and an expanded `finalize.py` (the `finalize_step`/`finalization_finished` wrap-up handshake). `memory.py` is no longer the cross-run god-module: `lesson_hygiene.py` (lesson dedup/quality hygiene) and `concept_capsules.py` (per-run concept capsules + their validators) are its two extracted halves, and two shared rules live beside them — `cadence.py::cadence_due` (the since-last node-count gate, NOT `n % every`, which a fan-out of k > 1 steps clean over — its at_node-idempotence twin is `search/coverage.py::already_covered_at`, parametrized over the snapshot LIST so the two snapshot producers cannot satisfy each other's gate) and `widths.py` (the ONE live concurrency-width settling rule: reject a bool, reject a non-integral float, refuse out-of-range rather than clamp, settle live `0` to serial `1` — live `0` is never AUTO, which belongs to launch-time `Settings`) |
| `looplab/adapters/` | task types (toy → dataset → repo → MLE-bench); the TaskAdapter contract is documented in `adapters/tasks.py` |
| `looplab/serve/` | FastAPI server (`server.py` is a thin composition root; routes live in `serve/routers/*` — control/runs/genesis/assistant/boss/org/reports/misc/attention/collaboration/reviews/cross_run), TUI, assistant; the authoritative command lifecycle (`run_commands.py` — every per-event control rule is a `ControlSpec` field, joined from FIVE tables each asserted equal to `protocol.py::CONTROL_EVENTS` at import: `CONTROL_DATA_FIELDS`/`_CONTROL_NORMALIZERS`/`_CONTROL_PRECONDITIONS`/`_CONTROL_DECISIONS`/`_CONTROL_POLICIES`, plus a cross-check that exactly the `COLLABORATION_EVENTS` declare an append-time precondition. Adding a control event means adding a row to all five — `None` is an explicit "no rule of its own", and a missing row refuses the import rather than inheriting a neighbour's handler), incremental command-ack observation (`command_observation.py`), the owner attention feed (`attention.py`), the isolated reviewer read namespace (`reviews.py`), `scope_report_store.py` (the durable scope-report STORE — paths/receipts/leases/fences/record validation, none of it HTTP; `routers/reports.py` star-re-exports it, so patch the seams HERE, not on `reports.<name>`), `scope_actions.py` (the paid ACTION protocol ABOVE that store — reconciling a claim against its receipt + lease marker + fence + two OS byte-range locks, plus the two `/api/scope-report-actions/…` bodies; it IMPORTS store names, which binds them BY VALUE, so it is a THIRD patch surface — `tests/test_report.py::_STORE_PATCH_MODULE_PATHS` is the sweep and `tests/test_scope_actions_service.py` fails if a module imports from the store without being listed), `run_projections.py` (the run-list projections behind `AppState.run_summaries()`/`run_membership()`, imported BY routers and never the other way round), and `jupyter.py` (the `jupyter_serverproxy_servers` launch spec); never imported by the engine (the run-end projections live in `events/`) |
| `ui/` | React control plane (built artifacts served by `serve/server.py`) |

## Engine invariants (violating these breaks replay/resume)

1. **The engine is the sole writer of domain events.** Background tasks return values; only the
   main task appends FOLDED events — with ONE typed exception: the concurrent-research task may append the
   selection-neutral FOLDED types in `events/types.py::BACKGROUND_APPENDABLE` (asserted at the append
   sites; `tests/test_background_appendable.py` proves splice-position neutrality). A concurrent task
   MAY additionally append `DIAGNOSTIC_EVENTS` (fold-ignored, so splice-neutral BY CONSTRUCTION — the
   fold never reads them): the training-monitor task appends `EV_TRAIN_MONITOR_ALERT` this way under
   `_write_lock`, asserting membership in `DIAGNOSTIC_EVENTS` at its append site.
   *An UNLOCKED main-task append* is the third accepted shape, and the one that is deliberately NOT in a
   registry: `EV_NODE_EVAL_STARTED` (`engine/evaluate.py::_record_eval_start_boundary`, called by the MAIN
   task from `engine/speculation.py`'s admission decision and again as a funnel backstop in `_evaluate`).
   It is FOLDED but appended outside `_write_lock`. That is safe because it is ONE independent per-node
   row the fold keys by `(node_id, generation)` and applies SET-ONLY, AND because both writers are on the
   main task *after* that node's own `node_created` — not because its position is immaterial. This row is
   NOT splice-neutral, and saying it "pairs with nothing, so its splice position cannot change any other
   event's meaning" (as this invariant did until 2026-08-05) is false: `_on_node_eval_started` silently
   drops a row whose node does not exist yet, and `Node.eval_started` is one of the durable facts
   `core/models.py::is_unevaluated_speculative_discard` proves the Layer-5 budget refund from — which
   `node_counts_toward_card_budget` reads, which BOTH the L3 budget and the fold's debug anchor
   (`events/replay.py::_card_debug_leaf_children`) read. So splicing it before rather than after its own
   `node_created` measurably flips a DIFFERENT Card's `selection_ready`: measured
   `{budget 2, leafs [2,3], later Card ready}` vs `{budget 3, leafs [3], not ready}`. Not reachable
   today (both writes are main-task and node-created-first), which is exactly why it has to be written
   down as an ordering PRECONDITION rather than left as a property of the event.
   It has no registry because the constraint it
   must satisfy is the opposite of `BACKGROUND_APPENDABLE`'s — placement is load-bearing in the other
   direction: writing it from the eval WORKER lands it inside `_request_card_build`'s tail-CAS window and
   makes every prefetch election lose its CAS (measured: depth-1 speculation silently went serial, 17
   builds / 5 discards became 12 / 0). Keep it on the main task, at the dispatch decision.
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
  paths — keep the `_LAYOUT` map in sync when MOVING modules, and `looplab/__init__.py::_RENAMED`
  (old FULL path → canonical full path, checked FIRST) when RENAMING one: `_LAYOUT` maps a canonical
  module STEM to its package, so a renamed module's old name is no longer a stem anywhere and both of
  its old spellings (dotted + flat pre-split) have to be routed explicitly. Both go through the same
  `_CompatLoader`, which is the point — these modules are patch seams, and a second module object
  would make every existing monkeypatch a silent no-op instead of an error.
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
  crash-triage verdicts `engine/triage.py::TRIAGE_ACTIONS` / `AGENT_TRIAGE_ACTIONS` (the
  inline-repair STOP decision — a typo'd literal turns a stop into "keep repairing");
  background-appendable events `events/types.py::BACKGROUND_APPENDABLE`, its thread-side sibling
  `SETUP_THREAD_APPENDABLE`, and its Card-conditional extension
  `NON_CARD_SELECTION_BACKGROUND_APPENDABLE` (legacy Hypothesis/Policy selection ONLY — the call site
  must prove Card-driven selection is off, because `hypothesis_merged` became a Card
  ownership/lifecycle input and is not universally neutral); LLM retry handlers
  `core/llm.py::OpenAICompatibleClient._RETRY_POLICY` (a `(exception types, handler)` table dispatched in
  order — the same order as the `except` ladder it replaced, so reordering it changes behaviour);
  the build-width LLM markers `agents/roles.py::LLM_PRESENCE_ATTRS` and the `UnifiedAgent` per-stage
  descent `agents/roles.py::FACADE_STAGE_ATTRS` (`_build_calls_an_llm`'s probes — the other half of
  the same AUTO width decision `gpu_capable` is registered for, and the half whose absent marker
  means "no LLM", i.e. a CHANGED treatment rather than the historical default: making the facade's
  public `researcher` handle private silently pinned `llm_parallel` 4 -> 1 and `speculation_depth`
  4 -> 0); capped-LLM-lane producers `core/llm_broker.py::BACKGROUND_LANE_PRODUCERS` (a lane name is
  a bare string, so this is what stops a FOREGROUND producer from joining a lane capped at one
  concurrent request because its prompt looks like background work — the per-eval inter-stage check
  did exactly that and serialized every concurrent eval's stage gate); the tool-loop keyword split
  `agents/loop_options.py::LOOP_OPTION_FIELDS` + `EXPLICIT_ONLY_LOOP_ARGS`, which must PARTITION
  `drive_tool_loop`'s keyword-only parameters — an option travels only inside a `LoopOptions` bundle,
  a per-call callback (and the two prompt contracts `nudge_prompt`/`stuck_prompt`) only as an
  explicit keyword. Adding a parameter to exactly one of the two lists is how the original defect
  comes back: a name reachable BOTH ways raises a duplicate-keyword `TypeError` that the loop's own
  containment `except` swallows, and the agentic Researcher silently degrades to non-agentic in the
  DEFAULT config. Also the engine's sub-object forwarding seam
  `engine/orchestrator.py::Engine.FORWARDED_SUBOBJECT_MEMBERS` (name -> sub-object + `@in_llm_lane`);
  a delegator whose lane is forgotten does not fail, it just runs outside the capped enrichment lane
  and competes with foreground work for provider concurrency, which reads as an unexplained stall.
  Adding/renaming any such seam means updating the registry in the SAME change. Note `search/foresight.py`'s
  panel is a `__getattr__` proxy over the wrapped agent: a typo'd read silently resolves to the
  base object, and an attribute SET on the panel shadows reads *through the panel* but does NOT
  reach the base's own `self.<attr>` reads until `forward_hints` mirrors it — set hints on the
  outermost wrapper and let the registry forward them, never on `base` directly.
- **A deliberate refusal is a TYPE, not a message.** `core/errors.py::OperatorRefusal` marks every
  exception LoopLab raises *on purpose* about the operator's own input (`ConfigRefusal`,
  `EnvironmentRefusal`, `LLMError`, the `RunStartPinError` family); `cli/__init__.py`'s
  `_RefusalBoundaryGroup` prints those as one message at exit `REFUSAL_EXIT_CODE` (2) and lets
  everything else keep its full traceback at exit 1. Raising a BARE `ValueError`/`RuntimeError` for
  a new refusal silently puts it back in the 42-lines-of-frames presentation this split removed —
  and widening the boundary to catch bare `ValueError` instead is the worse failure, because a bug
  reported as a tidy one-line message looks handled. The bar for wearing the marker is in the
  `OperatorRefusal` docstring; `tests/test_cli_refusals.py` pins both halves, control included.
- **Tool providers**: the `bind_state` hook is OPTIONAL (`tools/_base.py` — providers that don't
  need run state simply omit it), but a provider that DOES implement it must accept the second
  `parent` argument (`bind_state(self, state, parent=None)`) or it raises `TypeError` at dispatch.
- **Tests isolate the environment** (`tests/conftest.py`): dotenv loading is disabled,
  `LOOPLAB_MEMORY_DIR`/`LOOPLAB_KNOWLEDGE_DIR` point at tmp dirs, and the host GPU-pool lease is
  redirected to a per-test file. Engine tests construct `Engine(...)` directly (~170 call sites) —
  keep its keyword API stable. New tests should use `tests/factories.py::make_engine` (the one
  canonical construction: toy task + its roles + a subprocess sandbox + `GreedyTree`, everything else
  passed through as keyword overrides). It is deliberately NOT a fixture — resume/crash tests build a
  second Engine over the same run dir — and migration of the existing call sites is opportunistic.
- **A guard test must not be satisfiable by a COMMENT.** ~200 assertions in the suite read production
  source (`inspect.getsource` + `in` / `.index()`). 62 are POSITIVE pins (`assert "<literal>" in
  source`); none is vacuous today, and every one is one comment away from it, because the cheapest
  mutation is *delete the code, leave a comment carrying the pinned literal*. Full call EXPRESSIONS
  and their ORDER are not enough either: `pass  # self._record_eval_start_boundary(chosen)` satisfies
  all three `source.index()` lookups in `test_card_speculation_engine.py:1731-1733` **in order**
  while the boundary event is never written — the defect whose cost is recorded in invariant 1
  (17 builds / 5 discards became 12 / 0; the budget refund reads `eval_started`). Reach for these in
  order, best first:
  1. **Drive the property.** A real fallback with a counting accountant
     (`test_llm_stream_setup.py`), a real socket that goes silent mid-body
     (`test_llm_streaming_surface.py`), an `lstat` that under-reports a file's size
     (`test_fence_protocol.py`), a real run whose event log is then read
     (`test_trust_gates_reach_the_ledger.py`). Only this kind survives a refactor of the code it
     guards, and only this kind distinguishes "the call happened" from "the effect landed".
  2. **Make the rule statable.** When the property is buried where no caller can reach it — a money
     rule in a `finally` whose guard clause no call site exercises yet — HOIST it into a named
     function and test its truth table (`core/llm.py::_stream_envelope_is_billable`). A rule nobody
     can state is a rule nobody reviews.
  3. **AST, never substrings**, for the residue: that a call is REACHED, that two guards are in the
     right ORDER. `tests/_source_scan.py::called_names` / `names_read` / `function_tree` resolve real
     `ast.Call` / `ast.Name(Load)` nodes, and comments are not AST nodes.
  NEGATIVE pins (`assert "x" not in source`, 35 of them) stay substrings on purpose — what must not
  come back is the TEXT, and a commented-out copy of a re-derivation is as much of a drift risk as a
  live one. Finally, a source pin is not automatically what protects a property:
  `test_background_appendable.py:156-157`'s pins are evadable, but the property IS covered
  behaviourally by `test_deep_research_loop.py` / `test_research_attempt_settlement.py`. Check what
  actually holds the line before rewriting a pin — and re-verify by MUTATING a throwaway copy of the
  tree (`git archive HEAD | tar -x -C /tmp/...`), never the real one.
- **The host GPU-pool lease is ONE file per OS user** (`/tmp/looplab-gpu-pool-<uid>.lock`,
  `engine/resources.py`) and is exclusive ACROSS PROCESSES on purpose. So a GPU-owning run waits for
  every co-hosted GPU-owning run — potentially for hours — and that wait used to be completely
  silent, which reads as a deadlock and repeatedly got debugged as one. It now logs the lease path
  and the holding PID at WARNING. Work that needs no GPU must not enter that queue at all: an
  UNSPECIFIED footprint resolves against the TASK (`adapters/tasks.py::gpu_capable`, absent means
  capable) as well as the box, so the offline/synthetic adapters never take the lease. If a run
  genuinely stalls before its first eval, check that lock before suspecting the engine.
- Settings are flat on purpose (`LOOPLAB_<FIELD>` env vars map 1:1); never nest or rename fields —
  snapshots and env compat depend on the names.
- `looplab/sweep.py` is NOT a CLI subcommand — it is a runtime helper imported by *generated*
  solution code inside the sandbox (see its docstring).
- A run directory contains `events.jsonl`, `config.snapshot.json`, `task.snapshot.json`,
  `engine.lock`, and per-node workdirs (`docs/guide/concepts.md` is accurate;
  `docs/04-file-layout.md` is the original *design* and differs from what shipped).
