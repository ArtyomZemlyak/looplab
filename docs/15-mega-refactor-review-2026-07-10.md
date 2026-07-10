# LoopLab — Mega-Refactor Review (2026-07-10)

**Goal of this review.** A whole-codebase pass to prepare a *mega-refactor with zero
functional loss*: minimize duplication, apply architecture best-practices that fit an
event-sourced engine, raise code quality and hierarchy, and — explicitly — make the code
**easier and safer for coding agents** to modify. This document is the plan. **No fixes are
applied here**; every item lists the concrete change and an invariant-safety note so the work
can be sequenced and test-gated later.

**Method.** Six independent read-only review axes were run in parallel over `looplab/`
(~38k LoC, 148 test files), each grounded in real `file:line` evidence:
duplication · layering/architecture · god-file decomposition · agent-friendliness ·
dead-code/cruft/naming · config/events/model surface. Findings were de-duplicated and merged
below (several show up on multiple axes — those are the highest-value).

**Non-negotiable invariants every item below preserves** (from `CLAUDE.md` / engine
invariants): engine is the sole writer of domain events; exactly one terminal event per node;
every side effect gated on a domain event (resume-by-replay idempotent); state observed only
via `fold(store.read_all())`; `fold` stays deterministic, order-tolerant, unknown-type-tolerant,
additive-only with reader-side defaults; `Settings` fields are flat and never renamed/nested
(env + snapshot compat is by name); event-type names are constants in `events/types.py`; prompt
strings are behavioral contracts (never "clean up").

---

## 0. Executive summary — the north star

The codebase is in **genuinely good shape** at the contract layer: the event registry is
airtight (77 constants, all registered *and* emitted), all 147 `Settings` fields are read and
documented, `core`/`events` layering is strictly downward and clean, and there are already
**four exemplary "silent-breakage → red-test" mechanisms** to copy from (`ALL_EVENT_TYPES` +
`test_event_types`, `RESEARCHER_HINT_ATTRS` + `test_hint_forwarding`, `SIGNALS` +
`test_signal_delivery`, `_LAYOUT` + `test_package_layout`).

So the mega-refactor is **not** a rescue — it is consolidation. Five themes, in value order:

1. **De-duplicate the drift-prone clusters** (k-NN/IDW prediction ×3, numeric-param filter ×6,
   lenient-JSONL read ×8, atomic-JSONL write ×4, "robust metric" ×11). All land in
   dependency-light homes (`events/digest.py`, `events/eventstore.py`, a `Node` property) with
   zero behavior change. **Highest ROI, lowest risk.**
2. **Un-tangle the middle band by moving two mis-filed modules** — pure read-model exporters
   (`traceview.py`/`htmlview.py`) belong in `events/`, and `make_llm_client` belongs in
   `core/llm.py`. These two moves alone delete the documented `engine→serve` lazy leaks and the
   sole `agents→adapters` edge.
3. **Continue the proven mixin decomposition of `orchestrator.py`** (still 2800 LoC / ~130
   methods). A safe zero-`fold` tier (`eval_stages` → `crash_repair` → `eval_dispatch` →
   `audit`) removes ~665 LoC with *no test edits* beyond `_LAYOUT`.
4. **Convert today's silent seams into red tests** — the duck-typed `TaskAdapter` hooks and
   Developer/Researcher output attrs (`last_files`, `choose_action`, `assets()`), the
   `Settings`↔`EngineOptions` dual-default drift, and docs/diagram sync. This *is* the
   "better for agents" goal, expressed as machine-checked contracts.
5. **Refactor `fold()` from a 497-line/61-branch mega-function into an ordered dispatch table**
   of pure per-event handlers — same single left-fold, isolated logic, golden-log-gated.

**Two things to fix that are latent traps, not cosmetics:** the one real violation of engine
invariant #1 (a *background* task appends `EV_RESEARCH_COMPLETED`, safe only by prose comment —
§4.1), and the `novelty_semantic` default that diverges *opposite* to the documented rationale
(§4.4).

---

## 1. Verified CLEAN — do **not** spend refactor effort here

These came back healthy and are called out so the work doesn't chase ghosts:

- **Event registry airtight.** All 77 `EV_*` are registered *and* emitted; no
  registered-but-unemitted, no emitted-but-unregistered. The only raw-string `.append("…")`
  literals are operator/stage names (`merge`/`refine_block`/`score`), not event types.
- **Config docs complete.** All 147 `Settings` fields appear in `docs/guide/configuration.md`
  (the 4 `*_model`/`*_temperature` without a solo row are covered by combined rows).
- **No dead settings.** Every one of the 147 fields is read outside `config.py`/`appconfig.py`.
- **Invariant #4 respected.** Every derived `RunState`/`Node` field (`best_node_id`,
  `generalization_gap`, `champion`, hypothesis `priority`, …) is recomputed inside `fold` each
  call; `readmodel.py`/`mlflow_export.py` operate on fresh `fold(...)` output.
- **The "~18 TODO/FIXME/HACK" are noise.** Exactly one genuine marker exists and it lives
  *inside* the `cli_agent._SEED` placeholder string. No commented-out code blocks.
- **Already-unified helpers (do not re-flag):** `core/atomicio.py`, `tools/vectorstore._cosine`,
  `search/hybrid_merge.py` (RRF), `core/llm._backoff`, `runtime/command_eval`,
  `events/eventstore.iter_jsonl`, `core/prompts.render`, `agents/roles.forward_hints`,
  `serve/protocol` SSE constants.

---

## 2. Prioritized roadmap

| Phase | Theme | Risk | Test cost | LoC / drift removed |
|---|---|---|---|---|
| **P0** | Dead-code deletions + CLAUDE.md/comment fixes | none | none | ~120 LoC dead, doc drift |
| **P1** | De-duplication (behavior-preserving helpers) | low | run touched suites | ~30 call sites unified |
| **P2** | Mis-filing / layering un-tangle (import-graph only) | low | `test_package_layout` + smoke | removes 3 lazy leaks + 1 cycle |
| **P3** | `orchestrator.py` mixin decomposition (verbatim) | low→med | `_LAYOUT` + per-cluster suite | 2800 → ~1450 LoC |
| **P4** | Contracts & guardrails (silent seam → red test) | med | new tests | converts silent breakage |
| **P5** | `fold()` → dispatch table; other god-files | med | golden-log replay gate | 497-line reducer isolated |

Each phase is independently shippable and leaves the suite green. Recommended order is P0→P5,
but P1 and P2 are parallelizable.

---

## 3. Findings by phase

### P0 — Zero-risk deletions & documentation truth (do first)

**P0.1 Delete confirmed dead code** (grep-verified zero callers in `looplab/` + `tests/`):
- `core/llm.py:277` `_is_transient` (pre-httpx retry classifier; live path uses `_sdk_transient`
  at `llm.py:137`) — and with it the now-orphaned `import http.client` (`llm.py:35`).
- `runtime/deps.py:132` `PrepResult` dataclass — never constructed; `install()` returns
  `InstallResult` instead.
- `core/hardware.py:129` `path_size_note` — ~30-line public helper, zero references. *(VERIFY:
  no external importer of `looplab.hardware`.)*
- `runtime/sandbox.py:33` `_SECRET_ENV = SECRET_ENV` and `sandbox.py:122`
  `_json_line_metric = json_line_metric` — back-compat aliases with zero importers (the
  `_json_line_extras`/`_json_line_trials` siblings ARE used by tests — keep those).
- *Safety:* leave the real monkeypatch seams untouched — `sandbox._run_argv` (test_security),
  `vectorstore._cosine` (novelty gate), `agent.agentic_struct`/`drive_tool_loop`.

**P0.2 Fix stale `CLAUDE.md` facts** (the doc agents trust most; each is high-blast-radius):
- Routes moved to `serve/routers/*` — CLAUDE.md still says "routes live inside `make_app`"
  (`server.py` is now 250 lines, a thin composition root). The control-intent allow-list is
  `CONTROL_EVENTS` in `serve/protocol.py:41` (consumed at `routers/control.py:85`), **not**
  `server.py`. Add a `serve/routers/` package-map row; repoint invariant #1.
- Test count "~1150" → **~1650** (1666 collected).
- **The `Engine` is 6 files.** `class Engine(ConfirmPhaseMixin, AblationMixin, NoveltyGateMixin,
  StrategyCadenceMixin, ResearchCadenceMixin)` — add a mixin→responsibility→file map so an agent
  greping `orchestrator.py` for `_confirm_phase`/`_maybe_consult_strategist`/`_run_ablation`
  knows where they live. Note the implicit contract: a mixin reads `Engine.__init__` attributes
  freely, so renaming an attr silently breaks a mixin on an untaken path.
- Add two convention bullets: new tool providers must accept `bind_state(self, state, parent=None)`
  (`tools/_base.py:56` — else `TypeError` at dispatch); `search/foresight.py:357` is a
  *transparent* `__getattr__` proxy (don't "add" a method there expecting it to shadow the base).

**P0.3 Fix lying comments / stale layout docs:**
- `tools/reposcout.py:29` — `_looks_secret`/`_readable` are annotated "re-exported for
  back-compat" but have zero external importers (internal-only). Fix the comment; keep the code.
- `docs/04-file-layout.md` already carries a "⚠ design vs shipped" banner (the accurate doc is
  `docs/guide/concepts.md`) — optionally add inline "not shipped" markers to the superseded
  `commands.jsonl`/`store/objects/` sections so a mid-doc skim can't mislead.

---

### P1 — De-duplication (behavior-preserving; highest ROI)

Ordered by repetition count × drift risk. Homes chosen to respect layering
(`core` importable everywhere; `events` imports only `core`).

**P1.1 [HIGH] k-NN inverse-distance-weighted prediction — reimplemented 3×.**
`search/surrogate.py:76`, `serve/panel.py:18`, `runtime/proxy.py:47` each re-implement "filter
numeric params → L2 over shared keys → k nearest → zero-distance short-circuit else IDW-average",
each handling the zero-distance edge slightly differently (the exact silent-drift a shared helper
kills). → `knn_idw_predict(target, history, *, k, keys=None) -> (pred, nearest)` in
`events/digest.py` (next to `param_distance`). *Safety:* callers want different return shapes;
the helper must reproduce the zero-distance branch verbatim and let callers drop the 2nd element;
verify surrogate's exploration-bonus still sees the true nearest distance.

**P1.2 [HIGH] Numeric-param filter — reimplemented 5-6×** (couples with P1.1).
`{k: float(v) for k,v in params.items() if isinstance(v,(int,float))}` inline at
`events/digest.py:17` (canonical `_numeric`), `engine/novelty.py:207`, `search/surrogate.py:70`,
`serve/panel.py:21,76`, `runtime/proxy.py:32`. → promote `digest._numeric` to public
`numeric_params(params, keys=None)`. *Safety:* `proxy.py` coerces numeric **strings** via
`try/except float()` — NOT `isinstance`-equivalent; pick `isinstance` semantics for the shared
helper and leave proxy's coercion only if a test needs string params (grep first).

**P1.3 [HIGH] Lenient JSONL reader — reimplemented ~8×.**
The "`for line in text.splitlines(): try: orjson.loads(line) except: continue`" (skip-bad-line)
loop at `engine/lessons.py:112,126,646,901`, `engine/memory.py:459`,
`tools/knowledge_tools.py:210`, `tools/memory_tools.py:60`, `trust/harden.py:63`. →
`read_jsonl_lenient(path) -> Iterator[dict]` in `events/eventstore.py`. *Safety:* must **not**
fold into `iter_jsonl` (which *stops* at the first bad line for event-log durability); these
readers *skip-and-continue*. `lessons.py:646` appends `None` for bad lines to keep row indices
aligned for a later rewrite — that site needs an index-preserving variant (`keep_bad=True`).

**P1.4 [MED-HIGH] Atomic JSONL full-rewrite — reimplemented 4×** (read/write couple with P1.3).
`atomic_write_text(path, "".join(orjson.dumps(o).decode()+"\n" for o in rows))` at
`engine/lessons.py:444,705,912` and the bytes twin at `serve/routers/control.py:237`. →
`write_jsonl_atomic(path, rows)` in `events/eventstore.py`. *Safety:* keep trailing newline +
`orjson` (byte-identical output); leave the caller's liveness guard on the `spans.jsonl` rewrite.

**P1.5 [HIGH] "Robust metric" expression — copy-pasted 11×.**
`confirmed_mean if confirmed_mean is not None else metric` at `events/replay.py:599,609`,
`events/digest.py:41,51`, `engine/holdout.py:148`, `engine/lessons.py:942`,
`events/mlflow_export.py:50`, `serve/routers/runs.py:495`, `serve/htmlview.py:91`,
`cli.py:361,780`. → `Node.robust_metric` computed `@property` on `core/models.py`; repoint all 11
+ `digest.node_metric`. *Safety:* pure deterministic property; `_select_best`'s ranking key stays
byte-identical. Keep holdout precedence explicit where it layers on top — do **not** fold holdout
into this property.

**P1.6 [MED] Duplicated permission-mode table.** `serve/assistant.py:35` re-declares `MODES`,
`DEFAULT_MODE`, and a byte-identical `normalize_mode` that `tools/perm_modes.py` documents as the
"single source of truth". → import the three symbols (serve→tools is legal); drop the local copy
(else assistant's 4 call sites + `routers/assistant.py:293` normalize against a stale set).

**P1.7 [MED] Novelty "repropose-with-feedback" try/finally — duplicated verbatim 2×.**
`engine/novelty.py:130` and `:190` (only the hint text differs). → `self._repropose_with_feedback(
repropose, hint, idea)` on `NoveltyGateMixin`. *Safety:* preserve `BudgetExceeded` re-raise and
the mandatory `finally` restore (the load-bearing comment at `novelty.py:201-204`).

**P1.8 [MED/LOW] Smaller clusters** (do as encountered, each low payoff individually):
- "Last JSON-dict line" reverse scanner ×3 in `runtime/sandbox.py:105/131/162` (+ `mlebench_grade.py:58`)
  → `_last_json_dict(text, predicate)` private to `sandbox.py`.
- Toy-adapter scaffolding + blind-perturb Researcher across `adapters/{classification,regression,
  timeseries}.py` → a `BlindPerturbResearcher` base + `_toy_llm_roles` factory in a new
  `adapters/_toy_base.py`. *Safety:* preserve each RNG draw sequence exactly (deterministic-seed
  demos depend on it); do NOT merge the embedded `_*_TEMPLATE` solution strings (generated-code
  contracts).
- Scalar coercion `_to_float`/`_f`/`_int`/`_num` (`sandbox.py:95`, `routers/misc.py:136`,
  `hardware.py:39`, `repo_developer.py:409`) → `to_float`/`to_int` in `core/` (keep sandbox's
  `isfinite` NaN/Inf rejection in the shared `to_float`).
- Router state hydration `st = fold(_events(rd))` ×~15 → a `srv.state(run_id)` helper (must NOT
  cache across requests — invariant #4).
- `nvidia-smi` CSV parse ×3 (`hardware.py:29/181`, `routers/misc.py:142`) → `query_nvidia_smi(fields)`.
- `cli.py:874` `timings` hand-rolls span loading → call `serve/traceview.load_spans`.

---

### P2 — Mis-filing / layering un-tangle (import-graph only; no event surface)

The `core`/`events` layers are verified strictly downward. All the tangle is in the middle band
(`runtime → tools → agents/adapters → search/trust → engine`), where several edges point *upward*
and survive only as lazy imports — the classic latent-cycle tell.

**P2.1 [HIGH] Move pure read-model exporters `serve/traceview.py` + `serve/htmlview.py` →
`events/`.** Both import *only* `core.models` (no fastapi), yet the engine hard-depends on them:
`engine/finalize.py:60` lazily imports `render_html`/`build_trace_view`/`load_spans`, and
`tools/runs_tools.py:248` reaches up too. They are `(RunState, spans) → trace.json/tree.html`
*projections* — exactly `events/`'s documented job. Moving them makes `finalize.py`/`runs_tools.py`
import *downward* (legal, top-level, no lazy hack) and drops the headless-run dependency on the
`[ui]` extra. Update `_LAYOUT` (`traceview`/`htmlview`: `serve`→`events`); `routers/runs.py`
re-imports the new path. *Safety:* pure `RunState`→string, never `store.append`; `trace.json`/
`tree.html` are rewritten-each-finalize idempotent caches. Byte-identical behavior.

**P2.2 [HIGH] Move `make_llm_client` `adapters/tasks.py:320` → `core/llm.py`.** Its body depends
only on `core` (`OpenAICompatibleClient`, `reasoning_body`, `CostAccountant`), yet living in
`adapters` forces the **only** `agents→adapters` edge (`agents/agent.py:719`) and a `serve`
re-export shim. Relocate to `core/llm.py`; keep thin re-exports in `adapters/tasks.py` and
`serve/server.py` so existing call sites + the `looplab.serve.server.make_llm_client` monkeypatch
point (`serve/appstate.py:10`) keep resolving. Result: `agents` becomes cleanly downward-only.
*Safety:* constructs a client object; no event/fold surface; late-bound monkeypatch preserved by
the re-export.

**P2.3 [MED] Residual latent cycles** (defused only by lazy imports):
- `tools→adapters` (`runs_tools.py:374` `validate_task`, lazy) vs `adapters→tools` (top-level).
  Either move `validate_task`'s pure spec-shape checks to a neutral home, or have the `serve`
  proposal layer validate (serve may import adapters freely).
- `runtime→tools` (`bg_tasks.py:60` `from tools.shell_tools import git_config_env`, lazy) →
  move `git_config_env` down to `core`/`runtime`; `shell_tools` re-exports.
- Goal: no lazy import should be load-bearing purely for cycle-breaking.

**P2.4 [LOW-MED] `engine/signal_delivery.py` registry references 5 subpackages as strings** and is
only called by its test. Consider relocating to a neutral `looplab/_signals.py` (or `core`) so
cross-layer wiring knowledge isn't parked in `engine` and the enforcement test doesn't need `[ui]`
to resolve the `serve.llm_context` entry. Strings-only at import time — no behavior change.

**P2.5 [MED] Rename the one-letter-apart twin.** `tools/run_tools.py` (live run + siblings) vs
`tools/runs_tools.py` (every run on the machine), ~700 lines each, near-identical ADR-7 docstrings
— the single most likely wrong-file edit. → rename `runs_tools.py`→`all_runs_tools.py`, class
`RunsTools`→`AllRunsTools`; `_LAYOUT` + `test_package_layout` keep the flat-import shim honest.

*(Defensible blur, leave as-is: the task-specific developer personas in `adapters/repo_developer.py`
import only downward and cohere with `repo_task.py` — cohesion beats purity there.)*

---

### P3 — `orchestrator.py` decomposition (verbatim mixin moves)

The proven pattern: a method cluster moved **verbatim** into a `*Mixin` in a new `engine/` module;
`self` stays the Engine; zero call-site churn. Both `Engine._method` (class/bare-instance test
calls) and `eng._method` (instance monkeypatch) keep resolving. **Every new `engine/<mod>.py`
needs a `_LAYOUT` entry** or `test_package_layout` fails immediately.

**The one real trap:** the module-global `fold` monkeypatch seam.
`tests/test_creation_runaway_guard.py:46` does `monkeypatch.setattr(orch, "fold", …)` and relies
on `_create_node`'s `fold(...)` resolving through `orchestrator`'s globals. A moved function
resolves `fold` against its *new* module — so any method that calls module-global `fold` breaks
that patch when moved. `fold(` is called at orchestrator lines 517/530/806/809/989/1002 (the
`run` spine — **stays**), and **1507/1699/2381/2577** (`_create_node`/`_create_injected_node`/
`_evaluate`). The existing extracted mixins all sidestep this by taking `state` as a param and
never calling `fold` — that's the safe convention. Extraction order is ranked by this.

**Safe tier — zero `fold`, `_LAYOUT`-only, no test edits (~665 LoC out):**

| New module | Mixin | Methods (orchestrator lines) | Key safety note |
|---|---|---|---|
| `engine/eval_stages.py` | `EvalStagesMixin` | `_resolve_stages` 1862, `_resolved_stages` 1922, `_imported_modules` 1941, `_module_file_candidates` 1968, `_stage_reachable_files` 1992, `_safe_reuse_start` 2067, `_stage_check_fn` 2129 (~315 LoC) | called as class/bare-instance methods in test_inline_repair / test_composable_schema — mixin preserves |
| `engine/crash_repair.py` | `CrashRepairMixin` | `_triage_crash` 1314, `_repair_error_context` 1358, `_prepare_env` 1404 (~125 LoC) | new module must `from engine.triage import _rule_triage`; `_triage_crash` is instance-monkeypatched (survives) |
| `engine/eval_dispatch.py` | `EvalDispatchMixin` | `_agent_may` 1796, `_ensure_run_setup` 1802, `_do_run_setup` 1824, `_data_binds` 1842, `_run_eval` 2180, `_apply_sweep_best` 2269 (~135 LoC) | `_run_eval` is instance-monkeypatched (test_confirm_integration/test_holdout) — survives; import `EV_RUN_SETUP_*` |
| `engine/audit.py` | `AuditMixin` | `_emit_agent_report` 2319, `_emit_role_telemetry` 2336, `_emit_hypothesis_ranked` 2352, `_emit_foresight_selected` 2359, `_audit_workdir_writes` 2727, `_redact`, `_maybe_crash`, `_leakage_blocks` (~110 LoC) | telemetry exercised on bare `Engine.__new__` instances; move the 3 leakage imports along |

**Deferred tier — needs a `fold` import (and, for node_build, a 2-line test patch-target change):**
- `engine/evaluate.py::EvaluateMixin` — `_evaluate` 2377 (the single largest method, ~350 LoC) +
  `_probe_developer` property. Calls `fold` at 2381/2577 but **no test patches `orch.fold` in a
  path that reaches `_evaluate`**, so only the `fold` import is needed, no test edit. Removes ~13%
  of the file. Validate with the full `test_inline_repair` run.
- `engine/node_build.py::NodeBuildMixin` — the node-creation cluster (`_create_node` 1506,
  `_create_injected_node` 1689, `_rerun_node`, `_emit_node_created`, `_implement`, `_directed_idea`,
  `_repair`, `_ensemble_idea`, `_agent_next_actions`, `_activate_spec`). `_create_node`/
  `_create_injected_node` call module-global `fold` → repoint `test_creation_runaway_guard.py:46`
  and `test_hypothesis_merge.py` patch targets to the new module. **Lower-risk alternative:** keep
  the 3 `fold`-callers in orchestrator, extract only the stateless build sub-helpers (~150 LoC,
  zero `fold`) — those are already exercised on bare instances (test_parent_aware_developer).
- `engine/proposal_cues.py::ProposalCuesMixin` — `_set_complexity_hint` 1043, `_stamp_novelty_hint`
  1117. **Touches a guard test:** `test_hint_forwarding.py:71` source-scans
  `looplab.engine.orchestrator` for `setattr(...hint...)` sites — moving them means adding
  `_setattr_hint_names(proposal_cues)` to that test's union. Rank after the safe tier.

**Cannot move (must stay in `orchestrator.py`):** `__init__` (the wiring), the `run` spine +
phase helpers (`_setup_phase`/`_reentry_repin`/`_apply_control_overrides`/`_run_cadences`/
`_spawn_research`/`_dispatch_evals` — six call module-global `fold`), `_op_span` (explicitly
shared by the strategy/research mixins), `_cadence_due` (`@staticmethod` shared gate), and the thin
delegator blocks (workspace/lessons/holdout/novelty — they ARE the seam keeping `eng._method` +
instance monkeypatch alive after extraction).

**Net:** safe tier → 2800 → ~2135; + evaluate → ~1785; + node_build split → ~1450.

*Per step:* add the `_LAYOUT` row, run `pytest tests/test_package_layout.py`, then the cluster's
named test(s), then the full suite.

---

### P4 — Contracts & guardrails (turn silent breakage into red tests — the "better for agents" core)

This is the theme that most directly serves the stated goal. The repo already has the pattern
(registry constant + source-scanning test); these seams are missing it.

**P4.1 [MED-HIGH] The one live violation of invariant #1: a background task appends a domain
event.** `orchestrator.py:965` — the concurrent-research `_bg` coroutine calls
`_record_deep_research` → appends `EV_RESEARCH_COMPLETED` from a *background* task. Safe today only
by prose comment (`orchestrator.py:955`: audit-only, fold-order-independent; `EventStore.append`
serializes under an interprocess lock). → make it explicit and enforced: define a typed
`BACKGROUND_APPENDABLE` / audit-only event set in `events/types.py`, assert at the append site that
background writers may only emit from that set, and add a fold-order test proving every member is
selection-neutral. *This tightens invariants #1/#5* rather than relaxing them. (Alternative: hand
the memo back to the main loop as a return value — true single-writer.)

**P4.2 [MED-HIGH] Formalize the `TaskAdapter` contract.** `adapters/tasks.py:28` declares only the
4 required members; the 11 optional hooks (`assets`/`columns`/`leakage_inputs`/`host_grader`/
`repo_spec`/`eval_spec`/`make_onboarder`/`params`/`llm_roles`/…) are documented in the docstring
but probed with `getattr` at the engine (`orchestrator.py:455/457/463/489/491/788/2787`). Rename a
hook on one side → the run silently stages/scores nothing, suite stays green. → add a
`TASK_OPTIONAL_HOOKS` tuple in `tasks.py` + a parametrized test asserting the engine's getattr
call-sites and the tuple agree (grep both sides in one test); promote the informal contract to a
`@runtime_checkable Protocol` where cheap. *Safety:* pure typing/accessor refactor; same reads,
same events.

**P4.3 [MED-HIGH] Guard the Developer/Researcher output attrs.** `last_files`
(`orchestrator.py:1618/1674/1750/2547` + `ablation.py`/`roles.py`), `choose_action`
(`orchestrator.py:1282` — rename silently reverts the pilot to static policy), `assets()`
(`orchestrator.py:456`) all fall back silently and each test sets them on its *own* fake, so a
coordinated rename leaves the suite green. → add
`roles.DEVELOPER_OUTPUT_ATTRS`/`RESEARCHER_ACTION_ATTRS` registries + a `test_developer_contract.py`
asserting every shipped Developer/Researcher subclass exposes them (mirroring
`test_hint_forwarding.py`). Also assert the `foresight.py` transparent proxy forwards the canonical
names.

**P4.4 [MED] Guard the `Settings`↔`EngineOptions` dual defaults.** `engine/options.py:44`
re-declares 68 Settings fields; **15 deliberately differ** (product-opinionated vs library-conservative).
`test_engine_options.py` locks only the Engine-side relationship — nothing asserts the intended
Settings-vs-Engine gap, so a Settings default change silently shifts it. → add a test snapshotting
the *set of divergent fields* as a frozen `{field: (settings_default, engine_default)}` table, so
any future default change forces a deliberate update. **And fix `novelty_semantic`
(`config.py:267`=False vs `options.py:109`=True) — it diverges *opposite* to the documented
rationale** (every other divergence is product≥library aggressiveness; this one is inverted).
Behavior-neutral today (semantic dedup only fires when `novelty_gate` is on, and both gate defaults
are False), but a latent trap if the two ever decouple. Set `options.py:109`=False to match.

**P4.5 [MED] Automate the docs/diagram-sync rule.** CLAUDE.md declares stale docs "a bug" and
requires a `configuration.md` row per `Settings` field + the infographic in the same change, but
nothing enforces it (currently in sync by discipline only). → `test_config_docs_sync.py` reflecting
`Settings` fields and asserting each has a `configuration.md` row; at minimum assert every
cadence/threshold literal named in the infographic's `B` map exists as a `Settings` default.

**P4.6 [MED, larger] Replace the stringly-typed hint side-channel with a typed value object.**
`roles.RESEARCHER_HINT_ATTRS` (7 attrs) delivered via `forward_hints` doing
`setattr(dst, a, getattr(src, a))` mirrored across four wrappers; a new wrapper that forgets
`forward_hints` silently drops every hint (test-guarded, but the mechanism is the fragility). →
an immutable `ResearcherHints` value object passed as `propose(..., hints=…)` / a single
`apply_hints(hints)` on a `HintSink` protocol every wrapper forwards. *Safety:* hints are ephemeral
prompt cues, never events — replay determinism unaffected. (Larger change; schedule last in P4.)

---

### P5 — `fold()` dispatch table + remaining god-files

**P5.1 [MED, high-leverage] Refactor `fold()` from a 497-line/61-branch mega-function into an
ordered pure-handler dispatch table.** `events/replay.py:68-565` is one `for e in events` loop
with a 61-way `if/elif t == EV_*` chain over shared mutable `st` — the single highest-blast-radius
function in the system. → `HANDLERS: dict[str, Callable[[RunState, Event], None]]`, one verbatim
handler per event type; loop becomes `HANDLERS.get(t, _ignore)(st, e)`. **Determinism preserved
exactly:** events consumed in log order, each handler is the current branch body (pure `st`
mutation, no I/O), unknown types no-op via `.get`, first-terminal idempotence stays inside the
`node_evaluated`/`node_failed` handlers, and the `EV_NODE_CREATED` `MemoryError`/`RecursionError`
re-raise (`replay.py:124`) stays in its handler. **The one wrinkle:** the branch at `replay.py:434`
threads a fold-local `best_confirmed` into `_select_best` — the only cross-branch state in the loop.
Carry it explicitly via a tiny mutable `FoldCtx` alongside `st` (or a transient `RunState` field
like `building`). **Gate the change with a golden-log replay test** (fold old logs → byte-identical
`RunState`) + the existing `test_events_replay.py` + a re-fold-idempotence check.

**P5.2 Other large files — natural seams** (each preserves monkeypatch/import compat via re-export
back into the original module, exactly like `engine/triage.py` re-exports into orchestrator):
- **`core/llm.py` (1357)** → `llm/streaming.py` (SSE/stream machinery), `llm/transient.py`
  (retry/backoff/error classification), `llm/toolcall.py` (native tool-call parsing); keep the 3
  client classes in `llm.py` importing the helpers back. *Grep tests for
  `monkeypatch.setattr("looplab.core.llm._X")` before any move; re-import moved names into `llm.py`.*
- **`adapters/repo_developer.py` (1323)** → split the write-tool provider `RepoWriteTools`
  (+ `_stage_output_values`/`_xlsx_to_markdown`) into `adapters/repo_write_tools.py`, leaving the
  `LLMRepoDeveloper`/`LLMOnboarder` personas (~570/~750 split along tool-vs-persona).
- **`cli.py` (1037)** → command groups `cli_run` / `cli_export` / `cli_inspect` / `cli_ui`;
  `cli.py` stays the entrypoint wiring them (console-script `looplab` intact). Verify no test
  imports a private `cli._helper`.
- **`serve/tui.py` (971)** → `tui_api.py` (`Api` client) + `tui_format.py` (the unit-tested pure
  formatters), leaving `Tui`+`main`. *Grep `test_tui.py` — it exercises the formatters directly;
  re-export or repoint.*
- **`agents/agent.py` (900)** → `agents/tool_loop.py` (the reusable `drive_tool_loop`/
  `agentic_text`/`agentic_struct`/`CompositeTools` machinery), leaving `ToolUsingResearcher`.
  *Critical: keep `agentic_struct`/`drive_tool_loop` importable from `agents.agent` (documented
  monkeypatch targets) via re-export.*
- **`engine/lessons.py` (944)** → decompose the `LessonMemory` class the same way the Engine was —
  mixins on `LessonMemory`: `lessons_priors.py` (load/render priors), `lessons_distill.py`
  (reflect/distill/write-note), `lessons_reconcile.py` (comparative/reconcile/evidence-sig). Keep
  `append_lessons`/`store_case`/static file-maintenance + `__init__` in `lessons.py`. *The Engine
  delegators reference `LessonMemory.spent_pairs`/`consolidate_lessons_file`/`compact_lessons` as
  `staticmethod` class refs — mixin inheritance preserves those.*

**P5.3 [LOW] Model hygiene** (do only if P5 touches models anyway):
- `RunState` (~60 flat fields) and `Node` — do **not** nest into sub-models (readers spell
  `st.<field>` in dozens of places; nesting is risky churn for cosmetic gain). Add banner comments
  separating core / control-intent counter-pairs / audit-only sidecar regions; optionally a
  `RunState.control` helper bundling the `len(requests)-done` outstanding math.
- Treat `Idea`'s `mode="after"` validators (`core/models.py:54` `_backfill_rationale`,
  `_clamp_params_to_space`) as **"do not simplify"** — like a prompt string. `fold` rebuilds ideas
  via `Idea(**d["idea"])`, so these run on every replay and are deliberately self-healing for old
  logs; switching to `model_construct` (validators skipped) would silently change historical
  replays. Determinism holds only while they stay pure (they are).
- `Node.rerun_from`/`rerun_stage` are transient markers on the durable model (always None on a
  settled node) — leave unless P5 separates a live/transient node view; if touched, keep the
  reset logic co-located with the terminal handlers.

---

## 4. Sequencing & test-gating

- **Every phase leaves the suite green and is independently shippable.** P0/P1/P2 are the
  low-risk, high-clarity wins; do them first and in parallel where they don't overlap files.
- **Standard gate per change:** `python -m pytest -p no:warnings -m "not docker"` (full, ~2 min),
  plus the offline reproducibility check for anything touching engine/fold/replay:
  `looplab run --no-genesis --kind quadratic --goal "min (x-3)^2" --direction min --out <dir>`
  then `looplab replay <dir>` — the BEST node + metric must reproduce byte-identically.
- **fold/replay changes (P5.1) additionally require a golden-log test** (fold a checked-in old
  `events.jsonl` → assert identical `RunState`) before merge.
- **Any new `engine/` submodule (P3):** add the `_LAYOUT` row and run
  `pytest tests/test_package_layout.py` *first*.
- **Keep docs + the process diagram in sync in the SAME change** (CLAUDE.md rule): the settings
  table in `docs/guide/configuration.md` and the data-driven `docs/infographic/agent-architecture.html`.
- **Never touch prompt strings or the `Idea` validators as part of a "cleanup".**

## 5. Cross-axis finding index (traceability)

- **Duplication:** P1.1–P1.8.
- **Layering/architecture:** P2.1 (F1), P2.2 (F2), P2.3 (F5/F10), P2.4 (F9), P4.1 (F4), P4.2/P4.3
  (F7), P4.6 (F6), P5.1 (F8), P3+P4.4 (F3 — knob triplication → `**knobs` validated by
  `EngineOptions`, deleting ~150 redundant signature lines + the dual-default-sync obligation).
- **God-file decomposition:** P3 (orchestrator, full detail), P5.2 (llm/repo_developer/cli/tui/
  agent/lessons).
- **Agent-friendliness:** P0.2/P0.3 (CLAUDE.md + comments), P2.5 (`runs_tools` rename), P4.2/P4.3
  (seam registries), P4.5 (docs-sync test).
- **Dead-code/cruft/naming:** P0.1 (deletions), P1.6 (perm-modes dup), P2.5 (naming),
  P0.3 (layout docs).
- **Config/events/model surface:** P1.5 (`robust_metric`), P4.4 (dual defaults + `novelty_semantic`),
  P5.1 (`fold`), P5.3 (model hygiene). Verified-clean set in §1.
