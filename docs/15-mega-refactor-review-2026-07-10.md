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

> **Historical refactor plan.** Counts, line references, open findings, and the invariant wording below are
> pinned to 2026-07-10. For the current persistence contract, read “engine is the sole writer” as: one live
> engine owns `RunState` reduction, and every event append—including authenticated UI-server controls—is
> serialized by the event store; snapshots and original sidecars remain separate authorities. Docs 16–18/21
> and current source/tests supersede later status claims.
>
**Verification pass (same day).** Every claim was then adversarially fact-checked against the
code (every cited `file:line` re-read), and a separate gap-hunt swept the areas the six axes
did not cover (package-root modules, the React UI, tests, prompts, concurrency, packaging/CI).
Corrections are integrated in place; §6 lists what the verification changed and added, so a
reader can see which items were amended. Line-number evidence survived verification almost
untouched; the substantive corrections are flagged inline with **[VERIFIED-AMENDED]**.

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
   lenient-JSONL read ×8, atomic-JSONL write ×3+2, "robust metric" ×12 in-package + 5 out).
   All land in dependency-light homes (`events/digest.py`, `events/eventstore.py`, a `Node`
   property) with zero behavior change — with ONE verified exception: `lessons.py:444` is an
   **append**, not a rewrite, and must stay out of the atomic-rewrite helper (§P1.4).
   **Highest ROI, lowest risk.**
2. **Un-tangle the middle band by moving two mis-filed modules** — pure read-model exporters
   (`traceview.py`/`htmlview.py`) belong in `events/`, and `make_llm_client` belongs in
   `core/llm.py`. These two moves alone delete the documented `engine→serve` lazy leaks and the
   sole `agents→adapters` edge.
3. **Continue the proven mixin decomposition of `orchestrator.py`** (still 2800 LoC / 98 `def`s,
   87 at method level). A safe zero-`fold` tier (`eval_stages` → `crash_repair` → `eval_dispatch`
   → `audit`) removes ~665 LoC with *no test edits* beyond `_LAYOUT`.
4. **Convert today's silent seams into red tests** — the duck-typed `TaskAdapter` hooks and
   Developer/Researcher output attrs (`last_files`, `choose_action`, `assets()`), the
   `Settings`↔`EngineOptions` dual-default drift, and docs/diagram sync. This *is* the
   "better for agents" goal, expressed as machine-checked contracts.
5. **Refactor `fold()` from a 497-line mega-function (63 if/elif arms, one arm handling two
   event types) into an ordered dispatch table** of pure per-event handlers — same single
   left-fold, isolated logic, golden-log-gated.

**Two things to fix that are latent traps, not cosmetics:** the one real violation of engine
invariant #1 (a *background* task appends `EV_RESEARCH_COMPLETED`, safe only by prose comment —
§4.1), and the `novelty_semantic` default that diverges *opposite* to the documented rationale —
and, verified, is **already live**, not dormant: a direct `Engine(novelty_gate=True)` caller gets
semantic dedup today while the identical product config does not (§4.4).

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
- **[ADDED by gap-hunt] Concurrency & locking discipline is sound.** `_write_lock` (anyio)
  consistently wraps every append from concurrent tasks (confirm_phase/holdout/ablation/
  orchestrator); threading locks (`_dep_lock`, `_run_setup_lock`) are only taken inside
  `anyio.to_thread.run_sync` workers (no event-loop blocking); `EventStore.append` layers
  `_append_lock` + interprocess lock; `engine.lock` probe-and-release handles FUSE/NFS
  fail-open; assistant shared dicts are `_perm_lock`-guarded. The single real gap is P4.1.
  One rule for P3 movers: keep the `to_thread`/`_write_lock` pairing with moved code.
- **[ADDED by gap-hunt] Logging/tracing is consistent.** One `logging.getLogger` in the whole
  tree (`serve/server.py:41`); engine diagnostics go exclusively through `core/tracing.py`
  spans; prints are confined to cli/tui/sweep stdout contracts.
- **[ADDED by gap-hunt] Small modules are clean.** `agents/cli_agent.py`,
  `serve/settings_store.py`, `serve/engine_proc.py` (PID-recycle-guarded reaper),
  `runtime/jupyter.py`/`notebook.py`, `tools/skills.py` — no dup/dead/misplacement.
- **[ADDED by gap-hunt] Prompt keys and defaults are currently in sync** (no typos, no divergent
  defaults) — the P4.7 registry is preventive, not corrective.
- **CI/docs nits (gap-hunt):** docs deps are duplicated between `docs/requirements.txt` (what CI
  installs) and pyproject's `[docs]` extra — drift risk; and `tests.yml` runs plain
  `python -m pytest` on GH runners where Docker IS present, so docker-marked tests DO run in CI
  despite the workflow comment saying otherwise — align the comment or add `-m "not docker"`.

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
- `core/hardware.py:129` `path_size_note` — ~30-line public helper, zero references (verified:
  no importer of flat `looplab.hardware` either). **[VERIFIED-AMENDED]** co-delete its only
  private dependency `_human_bytes` (`hardware.py:163`), which orphans with it.
- `runtime/sandbox.py:33` `_SECRET_ENV = SECRET_ENV` and `sandbox.py:122`
  `_json_line_metric = json_line_metric` — back-compat aliases with zero importers (the
  `_json_line_extras`/`_json_line_trials` siblings ARE used by tests — keep those).
  **[VERIFIED-AMENDED]** in the same change fix the two comments that reference the dead alias
  (`sandbox.py:121` falsely claims tests use `_json_line_metric`; `sandbox.py:164` names it)
  and reword `docs/BACKLOG.md:48`, which points at `_SECRET_ENV` in prose (→ `SECRET_ENV`).
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
- Add two convention bullets: **[VERIFIED-AMENDED]** the `bind_state` hook on tool providers is
  *optional* (`tools/_base.py:49-55` — providers that don't need run state simply omit it), but a
  provider that DOES implement it must accept the second `parent` argument or it raises
  `TypeError` at dispatch; and `search/foresight.py:357` is a *transparent* `__getattr__` proxy
  (don't "add" a method there expecting it to shadow the base).

**P0.3 Fix lying comments / stale layout docs:**
- `tools/reposcout.py:29` — `_looks_secret`/`_readable` are annotated "re-exported for
  back-compat" but have zero external importers (internal-only). Fix the comment; keep the code.
- `docs/04-file-layout.md` already carries a "⚠ design vs shipped" banner (the accurate doc is
  `docs/guide/concepts.md`) — optionally add inline "not shipped" markers to the superseded
  `commands.jsonl`/`store/objects/` sections so a mid-doc skim can't mislead.

**P0.4 [ADDED by gap-hunt] Delete the dead React component pair.** `ui/src/GenesisChat.jsx`
(292 LoC, zero importers — genesis now flows through the assistant/boss routes) +
`ui/src/StartRun.jsx` (141 LoC, imported only by GenesisChat). Their `util.js` helpers orphan
with them (`genesis`/`genesisAwait`/`genesisJob`, `util.js:574-599`, plus `listTasks`/`research`
— StartRun was their only caller); note `jobAwait` (`util.js:601`) already duplicates
`genesisAwait` by its own comment — keep `jobAwait`, delete the genesis trio. *(Verified by
full-tree grep.)* **Deferred follow-up found by the P0 review:** with the `research()` client
gone, `POST /api/research` (`serve/routers/genesis.py:54`) is unreachable from every shipped
client and has zero test coverage — decide later whether to remove the route + its
`serve_prompts.py` prompt or add coverage (removing public API surface was out of P0 scope).

---

### P1 — De-duplication (behavior-preserving; highest ROI)

Ordered by repetition count × drift risk. Homes chosen to respect layering
(`core` importable everywhere; `events` imports only `core`).

**P1.1 [HIGH] k-NN inverse-distance-weighted prediction — reimplemented 3×.**
`search/surrogate.py:76`, `serve/panel.py:18`, `runtime/proxy.py:42-65` each re-implement "filter
numeric params → L2 over shared keys → k nearest → zero-distance short-circuit else IDW-average".
**[VERIFIED-AMENDED]** the zero-distance handling is textually different but *semantically
identical* in all three; the REAL divergences a shared helper must expose as parameters are
(a) **neighbour eligibility** — surrogate requires full bounds dimensionality
(`surrogate.py:70-74`), panel requires the neighbour to contain all target keys
(`panel.py:28`), proxy intersects keys *per neighbour* (`proxy.py:53`) so different neighbours
score in different subspaces; and (b) proxy's numeric-**string** coercion (P1.2) changes its
distances too. → `knn_idw_predict(target, history, *, k, eligibility=..., keys=None) ->
(pred, nearest)` in `events/digest.py` (next to `param_distance`). *Safety:* callers want
different return shapes; keep each caller's eligibility semantics verbatim; verify surrogate's
exploration-bonus still sees the true nearest distance. **[RESOLVED at implementation with a
simpler shape]:** shipped as `knn_idw(pairs, k)` over pre-computed `(distance, value)` pairs —
eligibility/distance stay AT the callers verbatim (safer than an `eligibility=` parameter), and
the exact-match scan covers the whole top-k (a NaN distance must not hide a genuine zero).

**P1.2 [HIGH] Numeric-param filter — reimplemented 5-6×** (couples with P1.1).
`{k: float(v) for k,v in params.items() if isinstance(v,(int,float))}` inline at
`events/digest.py:17` (canonical `_numeric`), `engine/novelty.py:207`, `search/surrogate.py:70`,
`serve/panel.py:21,76`, `runtime/proxy.py:32`. → promote `digest._numeric` to public
`numeric_params(params, keys=None)`. *Safety:* `proxy.py` coerces numeric **strings** via
`try/except float()` — NOT `isinstance`-equivalent; pick `isinstance` semantics for the shared
helper and leave proxy's coercion only if a test needs string params (grep first).

**P1.3 [HIGH] Lenient JSONL reader — reimplemented ~8×.**
The "`for line in text.splitlines(): try: loads(line) except: continue`" (skip-bad-line)
loop at `engine/lessons.py:112,126,646,901`, `engine/memory.py:459`,
`tools/knowledge_tools.py:210`, `tools/memory_tools.py:60`, `trust/harden.py:63`. →
`read_jsonl_lenient(path) -> Iterator[dict]` in `events/eventstore.py`. *Safety:* must **not**
fold into `iter_jsonl` (which *stops* at the first bad line for event-log durability); these
readers *skip-and-continue*. `lessons.py:646` appends `None` for bad lines to keep row indices
aligned for a later rewrite — that site needs an index-preserving variant (`keep_bad=True`).
**[VERIFIED-AMENDED]** 4 of the 8 sites use stdlib `json.loads` (memory, knowledge_tools,
memory_tools, harden) and 4 use `orjson` — stdlib accepts `NaN`/`Infinity` literals that orjson
rejects. **[RESOLVED at implementation]:** shipped with a `loads=` parameter preserving each
store's writing parser exactly (safer than forcing one parser), plus `keep_bad=` (raw-line-index
alignment) and `dicts_only=`/`errors=` for the two sites with historical shapes.

**P1.4 [MED-HIGH] Atomic JSONL full-rewrite — reimplemented 3× (+2 stdlib variants)**
(read/write couple with P1.3). **[VERIFIED-AMENDED — the original ×4 census contained one
genuinely UNSAFE consolidation]:** `engine/lessons.py:444` is **not** an atomic rewrite — it is
an **append** (`open(path, "a")` under an interprocess lock, lines 445-447) to the *shared*
cross-run lessons store; folding it into a whole-file-rewrite helper would silently drop rows
written by concurrent runs. It may share only the line-serializer. The genuine rewrite sites:
`engine/lessons.py:705,912`, the bytes twin at `serve/routers/control.py:237`, plus two
stdlib-json variants the census missed — `engine/memory.py:471-472` (`_flush`) and
`trust/harden.py:73-74` (not byte-identical if moved onto orjson — same parser decision as
P1.3). → `write_jsonl_atomic(path, rows)` in `events/eventstore.py`. *Safety:* keep trailing
newline; leave the caller's liveness guard on the `spans.jsonl` rewrite; NEVER route an
append-mode site through it.

**P1.5 [HIGH] "Robust metric" expression — copy-pasted 12× in-package (+5 out-of-package).**
`confirmed_mean if confirmed_mean is not None else metric` at `events/replay.py:599,609`,
`events/digest.py:41,51` (the `:51` site IS `digest.node_metric`), `engine/holdout.py:148`,
`engine/lessons.py:942`, `events/mlflow_export.py:50`, `serve/routers/runs.py:495`,
`serve/htmlview.py:91`, `cli.py:361,780`, **[VERIFIED-AMENDED]** plus `looplab/bench.py:48` —
the capability-regression harness you'd use to VALIDATE this refactor, so a missed repoint
there drifts silently — and 5 more copies in repo-root `tools/e2e_report.py:85,103,109,115,202`
(outside the package; repoint or explicitly scope out). → `Node.robust_metric` plain
`@property` on `core/models.py` (verified safe: pydantic v2 `BaseModel`, no field/attr
collision, plain properties are excluded from `model_dump` — no serialization change).
*Safety:* pure deterministic property; `_select_best`'s ranking key stays byte-identical. Keep
holdout precedence explicit where it layers on top (`replay.py:638`) — do **not** fold holdout
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
  timeseries}.py`. **[RESOLVED at implementation: NO extraction.]** Close reading showed the three
  researchers are NOT copy-paste: draft/improve semantics and RNG call SEQUENCES differ materially
  (choice-menus vs randint vs random()/gauss; different clamps and rationales), and the `llm_roles`
  bodies share only a trivial one-line shape whose hint/bounds are per-task prompt contracts. A
  shared base would be parameterization indirection with nothing left to drift, while risking the
  deterministic-seed contract. Do NOT merge the embedded `_*_TEMPLATE` strings either.
- Scalar coercion `_to_float`/`_f`/`_int` (`sandbox.py:95`, `routers/misc.py:136`,
  `hardware.py:39`) → `to_float`/`to_int` in `core/parse.py` (sandbox's `isfinite` rejection is
  the `finite=True` flag). **[RESOLVED at implementation]:** `repo_developer.py:409` `_num` was
  mischaracterized by the census — it is an is-numeric bool PREDICATE, not a coercion; left as-is.
- Router state hydration `st = fold(_events(rd))` ×~15 → a `srv.state(run_id)` helper (must NOT
  cache across requests — invariant #4).
- `nvidia-smi` CSV parse ×3 (`hardware.py:29/181`, `routers/misc.py:142`) → `query_nvidia_smi(fields)`.
- `cli.py:874` `timings` hand-rolls span loading. **[VERIFIED-AMENDED]** a straight swap to
  `traceview.load_spans` is NOT behavior-preserving: cli's loop *skips* bad span lines and
  continues; `load_spans` uses `iter_jsonl`, which *stops at the first bad line* — on a
  mid-corrupt `spans.jsonl` the swap silently truncates every later span. Either accept the
  stop-at-tail semantics deliberately (document it) or give `load_spans` a `lenient=` flag.

---

### P2 — Mis-filing / layering un-tangle (import-graph only; no event surface)

The `core`/`events` layers are verified strictly downward. All the tangle is in the middle band
(`runtime → tools → agents/adapters → search/trust → engine`), where several edges point *upward*
and survive only as lazy imports — the classic latent-cycle tell.

**P2.1 [HIGH] Move pure read-model exporters `serve/traceview.py` + `serve/htmlview.py` →
`events/`. [RESOLVED as planned]** (byte-identical git mv; finalize's imports are now
top-level; the canonical `serve.*` paths were dropped rather than stubbed — verified zero
monkeypatchers; test imports repointed). htmlview imports only `html` + `core.models`; traceview imports stdlib +
`core.models` + (lazily) `events.eventstore.iter_jsonl` — which *supports* the move (becomes
intra-package). Neither touches fastapi/serve. Yet the engine hard-depends on them:
`engine/finalize.py:60` lazily imports `render_html`/`build_trace_view`/`load_spans`, and
`tools/runs_tools.py:248` reaches up too. They are `(RunState, spans) → trace.json/tree.html`
*projections* — exactly `events/`'s documented job. Moving them makes `finalize.py`/`runs_tools.py`
import *downward* (legal, top-level, no lazy hack) and drops the headless-run dependency on the
`[ui]` extra. *Safety:* pure `RunState`→string, never `store.append`; `trace.json`/`tree.html`
are rewritten-each-finalize idempotent caches. Byte-identical behavior.
**[VERIFIED-AMENDED] the full importer inventory the move must handle** (no monkeypatch on
either module — verified): `_LAYOUT` rows `traceview`/`htmlview`: `serve`→`events` (rescues the
flat `looplab.traceview` used by `tools/e2e_report.py:15`); `engine/finalize.py:60-61`;
`tools/runs_tools.py:248`; `serve/routers/runs.py:304,402,459,476` — the `:476` site imports
**private** `_tree`/`_cap_span_io`, which must move along; and the `_LAYOUT` shim does NOT
rescue the canonical `looplab.serve.traceview` path, which `tests/test_tracing.py` (8 sites)
and `tests/test_conversation_split.py:8` import directly → either update those test imports or
keep a 2-line `serve/traceview.py` re-export stub (stub is a *different module object* — fine
here only because nothing monkeypatches it). Also update the stale `CLAUDE.md:53` parenthetical
about finalize's lazy serve import in the same change.

**P2.2 [HIGH] Move `make_llm_client` `adapters/tasks.py:320` → `core/llm.py`. [RESOLVED as
planned]** (verbatim move; re-export chain verified one-object; the sole `agents→adapters`
edge is gone). Its body depends
only on `core` (`OpenAICompatibleClient`, `reasoning_body`, `CostAccountant`), yet living in
`adapters` forces the **only** `agents→adapters` edge (`agents/agent.py:719`) and a `serve`
re-export shim. Relocate to `core/llm.py`; keep thin re-exports in `adapters/tasks.py` and
`serve/server.py` so existing call sites + the `looplab.serve.server.make_llm_client` monkeypatch
point (`serve/appstate.py:10`) keep resolving. Result: `agents` becomes cleanly downward-only.
*Safety:* constructs a client object; no event/fold surface; late-bound monkeypatch preserved by
the re-export.

**P2.3 [MED] Residual latent cycles** (defused only by lazy imports):
- `tools→adapters` (`validate_task`, lazy). **[RESOLVED as a documented exception]:**
  `validate_task` is NOT pure-shape — it builds the adapter (`_KINDS` registry +
  `model_validate`), which cannot move below tools; and a constructor-injected validator would
  add a "silently unvalidated" default (worse than an ImportError). The lazy import stays,
  now explicitly commented at the site as a deliberate runtime-only upward dependency.
- `runtime→tools` (`bg_tasks.py` `git_config_env`, lazy). **[RESOLVED]:** moved verbatim to a
  new `core/gitenv.py` (+ `_LAYOUT` row); `bg_tasks` imports it top-level downward;
  `shell_tools` re-exports for its own call sites and the tests.

**P2.4 [LOW-MED] `engine/signal_delivery.py` registry references 5 subpackages as strings** and is
only called by its test. **[VERIFIED-AMENDED]** the originally proposed `looplab/_signals.py`
destination FAILS `test_no_stray_modules_at_package_root` (root allows only
`__init__/cli/bench/sweep`) — relocate to `core/signals.py` (+ `_LAYOUT` row) or leave in
`engine/` with a docstring note. **[RESOLVED: left in engine/ with the placement note]** — the
engine is the producer these signals govern, moving wouldn't lift the test's `[ui]` need, and
strings-only means zero layering edge either way.

**P2.5 [MED] Rename the one-letter-apart twin.** `tools/run_tools.py` (live run + siblings) vs
`tools/runs_tools.py` (every run on the machine), 717/700 lines, near-identical ADR-7 docstrings
— the single most likely wrong-file edit. **[VERIFIED-AMENDED — the original mechanics were
wrong twice]:** (a) the proposed class name `AllRunsTools` **already exists** at
`tools/run_tools.py:467` (used by `adapters/tasks.py:430` and `tests/test_cross_run.py`) —
reusing it would create two identically-named classes in the two twin files, aggravating the
exact hazard this item fixes. Pick `machine_runs_tools.py` / `MachineRunsTools`. (b) the
`_LAYOUT` shim **cannot express a rename** (`_CompatFinder` builds the canonical path from the
SAME flat name), so the plan is: rename the file, swap the `_LAYOUT` key
(`runs_tools`→`machine_runs_tools`), and update the 4 canonical importers
(`serve/assistant.py:243,284`, `tests/test_run_control_tools.py:10`,
`tests/test_run_logs_trace.py:16`). The old *flat* alias `looplab.runs_tools` simply disappears
— verified zero importers, so nothing breaks. **[RESOLVED as planned]** (also repointed the
class-name mentions in `tools/_runcache.py` / `run_tools.py` comments and the in-file
constructor at `serve/assistant.py::build_tools`).

*(Defensible blur, leave as-is: the task-specific developer personas in `adapters/repo_developer.py`
import only downward and cohere with `repo_task.py` — cohesion beats purity there.)*

---

### P3 — `orchestrator.py` decomposition (verbatim mixin moves)

The proven pattern: a method cluster moved **verbatim** into a `*Mixin` in a new `engine/` module;
`self` stays the Engine; zero call-site churn. Both `Engine._method` (class/bare-instance test
calls) and `eng._method` (instance monkeypatch) keep resolving. **[VERIFIED-AMENDED] the
`_LAYOUT` rule is wider than engine/:** `test_no_module_missing_from_layout` iterates ALL TEN
subpackages, so *every* new `.py` placed directly in any of core/events/runtime/tools/agents/
search/trust/engine/adapters/serve needs a `_LAYOUT` row — that includes P1.8's
`adapters/_toy_base.py` and every P5.2 module. (Files in *nested* packages like
`serve/routers/` are exempt — the glob is non-recursive.) A companion trap:
`test_no_stray_modules_at_package_root` pins the package root to `{__init__, cli, bench,
sweep}` — no new root-level module can be added without touching that test.

**Prep step — [RESOLVED at implementation: NO shared builder].** Close reading showed the
`_engine(run_dir, **kw)` builders are per-file FIXTURES, not copies: different roles/stubs,
policies, seeds and kwargs per file (`test_control` GreedyTree(2,4); `test_strategist`
(3,8)+strategist; `test_inline_repair` stub researcher + repair caps; `test_end_to_end`
max_parallel). A conftest builder would be forced parameterization; the mixin extraction has
zero call-site churn anyway, so the insurance value never materialized.

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

**Net [VERIFIED-AMENDED arithmetic]:** safe tier → 2800 → ~2135; + evaluate → ~1785;
+ node_build *split* form (~150) → ~1635; + node_build *full* form (~390) → ~1395.
**[RESOLVED at implementation]:** shipped the safe tier + evaluate + node_build SPLIT form +
proposal_cues → orchestrator.py is now ~1515 lines / 12 mixins. Two extraction findings the
plan missed, both caught by tests: (a) `_evaluate` passes the `_UNSET` sentinel POSITIONALLY
(as `next_start`) into `_run_eval` — the sentinel had to move to `engine/options.py` so both
modules share ONE object; (b) the signal-delivery registry call-sites for `trust_reflection` /
`render_hint_directives` had to follow their methods into the new mixin files (the enforcement
test went red exactly as designed).

*Per step:* add the `_LAYOUT` row, run `pytest tests/test_package_layout.py`, then the cluster's
named test(s), then the full suite.

---

### P4 — Contracts & guardrails (turn silent breakage into red tests — the "better for agents" core)

This is the theme that most directly serves the stated goal. The repo already has the pattern
(registry constant + source-scanning test); these seams are missing it.

**P4.1 [MED-HIGH] The one live violation of invariant #1: a background task appends a domain
event. [RESOLVED: BACKGROUND_APPENDABLE set in events/types.py + assertions at the append
sites + tests/test_background_appendable.py (registry sanity, selection-neutrality at every
splice position, source guard on _spawn_research). The set is {EV_RESEARCH_COMPLETED, EV_HINT}
— the census had missed that the background path appends the steering EV_HINT too.]** `orchestrator.py:965` — the concurrent-research `_bg` coroutine calls
`_record_deep_research` → appends `EV_RESEARCH_COMPLETED` from a *background* task. Safe today only
by prose comment (`orchestrator.py:955`: audit-only, fold-order-independent; `EventStore.append`
serializes under an interprocess lock). → make it explicit and enforced: define a typed
`BACKGROUND_APPENDABLE` / audit-only event set in `events/types.py`, assert at the append site that
background writers may only emit from that set, and add a fold-order test proving every member is
selection-neutral. *This tightens invariants #1/#5* rather than relaxing them. (Alternative: hand
the memo back to the main loop as a return value — true single-writer.)

**P4.2 [MED-HIGH] Formalize the `TaskAdapter` contract. [RESOLVED: TASK_OPTIONAL_HOOKS registry
+ tests/test_task_adapter_contract.py — two-way probe/registry scan + adapter near-miss check.]** `adapters/tasks.py:28` declares only the
4 required members; the 11 optional hooks (`assets`/`columns`/`leakage_inputs`/`host_grader`/
`repo_spec`/`eval_spec`/`make_onboarder`/`params`/`llm_roles`/…) are documented in the docstring
but probed with `getattr` at the engine (`orchestrator.py:455/457/463/489/491/788/2787`). Rename a
hook on one side → the run silently stages/scores nothing, suite stays green. → add a
`TASK_OPTIONAL_HOOKS` tuple in `tasks.py` + a parametrized test asserting the engine's getattr
call-sites and the tuple agree (grep both sides in one test). **[VERIFIED-AMENDED]** the
Protocol is *already* `@runtime_checkable` (`tasks.py:28`) — the registry+test is the whole
remaining gap. *Safety:* pure typing/accessor refactor; same reads, same events.

**P4.3 [MED-HIGH] Guard the Developer/Researcher output attrs. [RESOLVED: DEVELOPER_OUTPUT_ATTRS
(+ last_report/last_seed/last_run/last_patch — four MORE unregistered seams the registry's own
first run surfaced, incl. last_report read by engine/audit.py) + RESEARCHER_ACTION_ATTRS +
tests/test_role_output_contract.py (two-way scan + foresight-proxy delegation check).]** `last_files`
(`orchestrator.py:1618/1674/1750/2567` + `ablation.py`/`roles.py`), `choose_action`
(`orchestrator.py:1282` — rename silently reverts the pilot to static policy), `assets()`
(`orchestrator.py:456`) all fall back silently and each test sets them on its *own* fake, so a
coordinated rename leaves the suite green. → add
`roles.DEVELOPER_OUTPUT_ATTRS`/`RESEARCHER_ACTION_ATTRS` registries + a `test_developer_contract.py`
asserting every shipped Developer/Researcher subclass exposes them (mirroring
`test_hint_forwarding.py`). Also assert the `foresight.py` transparent proxy forwards the canonical
names.

**P4.4 [MED] Guard the `Settings`↔`EngineOptions` dual defaults. [RESOLVED:
tests/test_options_divergence.py freezes the 15-field table + a direction-rule check;
options.py novelty_semantic flipped to False (the audited breakage was exactly one test relying
on the old library default to exercise the semantic gate — it now passes the flag explicitly).]** `engine/options.py:44`
re-declares 68 Settings fields; **[VERIFIED-AMENDED] 16 deliberately differ** (not 15:
agent_control, agent_drives_actions, comparative_lessons, concurrent_research, debug_depth,
deep_repair, deep_research_every, failure_reflection, lessons_every, lessons_refresh_every,
memory_dir, merge_mode, novelty_semantic, reflection_priors, report_every, unified_agent).
`test_engine_options.py` locks only the Engine-side relationship — nothing asserts the intended
Settings-vs-Engine gap, so a Settings default change silently shifts it. → add a test snapshotting
the *set of divergent fields* as a frozen `{field: (settings_default, engine_default)}` table, so
any future default change forces a deliberate update. **And fix `novelty_semantic`
(`config.py:267`=False vs `options.py:109`=True) — it diverges *opposite* to the documented
rationale** (every other divergence is product≥library aggressiveness; this one is inverted).
**[VERIFIED-AMENDED] the divergence is LIVE today, not dormant:** semantic dedup is unreachable
while `novelty_mode="llm"` (both defaults), but a direct `Engine(novelty_gate=True)` call forces
`mode="algo"` (`orchestrator.py:351`) and picks up `novelty_semantic=True` from EngineOptions —
so a bare-Engine caller enabling the gate gets embedding dedup while the identical product
config (Settings: `novelty_semantic=False`) does not. Flipping `options.py:109` to False is
therefore a *deliberate library-default change*, not a no-op — `options.py:17` pins engine
defaults to the legacy signature defaults, so audit the novelty tests when changing it.

**Related, verified good news for the F3 `**knobs` collapse (§5 index):** `Engine.__init__` has
exactly **68** `_UNSET` knobs and `EngineOptions` exactly **68** fields — a perfect 1:1 name
match (the earlier "77 vs 68" figure was wrong; 77 is the *event-constant* count). The knob
block is keyword-only (`*` after `run_dir`) and all 119 test call sites pass keywords — so
validating `**knobs` against EngineOptions field names breaks zero call sites; only the ~16
object seams (task/roles/sandbox/policy/options + factory hooks) stay explicit parameters.

**P4.5 [MED] Automate the docs/diagram-sync rule. [RESOLVED: tests/test_config_docs_sync.py —
every Settings field must be named in configuration.md + a ghost-row reverse check. The
infographic B-map assertion was NOT implemented (its labels are prose, not parseable defaults);
the manual same-change rule still governs the diagram.]** CLAUDE.md declares stale docs "a bug" and
requires a `configuration.md` row per `Settings` field + the infographic in the same change, but
nothing enforces it (currently in sync by discipline only). → `test_config_docs_sync.py` reflecting
`Settings` fields and asserting each has a `configuration.md` row; at minimum assert every
cadence/threshold literal named in the infographic's `B` map exists as a `Settings` default.

**P4.6 [MED, larger] Replace the stringly-typed hint side-channel with a typed value object.
[RESOLVED: setattr channel KEPT; gap closed structurally instead.]** A propose()-signature
change would ripple through every researcher + dozens of test stubs for a failure mode that is
now four-ways guarded: the registry source-scan (extended to the P3 mixin homes), the four
per-wrapper forwarding tests, the NEW wrapper-enumeration guard
(test_every_researcher_wrapper_forwards_hints — any future wrapper class whose propose()
delegates without forward_hints goes red the day it is written), and the P4.3 output registries.
`roles.RESEARCHER_HINT_ATTRS` (7 attrs) delivered via `forward_hints` doing
`setattr(dst, a, getattr(src, a))` mirrored across four wrappers; a new wrapper that forgets
`forward_hints` silently drops every hint (test-guarded, but the mechanism is the fragility). →
an immutable `ResearcherHints` value object passed as `propose(..., hints=…)` / a single
`apply_hints(hints)` on a `HintSink` protocol every wrapper forwards. *Safety:* hints are ephemeral
prompt cues, never events — replay determinism unaffected. (Larger change; schedule last in P4.)

**P4.7 [ADDED by gap-hunt — MED] Prompt-key registry. [RESOLVED: PROMPT_KEYS in core/prompts.py
+ tests/test_prompt_keys.py (two-way scan + filename validity + override round-trip); the repo
Developer's intro/body now render() through the store via make_roles' existing
post-construction prompts hook — byte-identical with no override.]** 13 `render(prompts, key, default)` keys
are bare literals across 8 files (developer_system, developer_repair_prefix, researcher_system,
tool_researcher_system, strategist_system, tool_strategist_system, pilot_system, triage_system,
deep_research_system, foresight_system, merge_system, bestofn_judge_system, …). Verified clean
today (no typos, no divergent defaults), but a typo'd override *filename*
(`prompts/developer_sytem.md`) silently no-ops — the exact failure mode the repo already fixed
for event types. → `PROMPT_KEYS` registry + source-scan test (mirror `test_event_types`). Also:
the repo Developer/Onboarder prompts (`adapters/repo_developer.py:458/464`, composed `:1088`)
**bypass the PromptStore entirely** while every `agents/` persona routes through it — an
override that works for the toy Developer silently does nothing for the repo one. Route them
through `render()` with the current constants as defaults (byte-identical behavior), or
document the exclusion. *Prompt TEXT stays untouched — this is plumbing only.*

**P4.8 [ADDED by gap-hunt — MED] String-referenced entry points need a resolution test.
[RESOLVED: tests/test_entry_points.py resolves every pyproject console script + entry-point
group at suite time.]**
`pyproject.toml` declares `[project.entry-points."jupyter_serverproxy_servers"] looplab =
"looplab.runtime.jupyter:setup_looplab"` and TWO console scripts (`looplab` AND the `LoopLab`
alias → `looplab.cli:app`). No test resolves these dotted paths, so moving/renaming
`runtime/jupyter.py`, `setup_looplab`, or `cli.app` breaks deploys silently. → a trivial test
importing each declared entry point (also protects the P5.2 cli split).

---

### P5 — `fold()` dispatch table + remaining god-files

**P5.1 [MED, high-leverage] Refactor `fold()` from a 497-line mega-function into an
ordered pure-handler dispatch table.** `events/replay.py:68-565` is one `for e in events` loop
with 63 `if/elif` arms over shared mutable `st` (61 single-type `elif t ==` arms + the opening
`if`, plus one arm handling two types — `elif t in (EV_RESUME, EV_RUN_REOPENED)` at `:444`,
which becomes the same handler under two dict keys) — the single highest-blast-radius
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
- **`core/llm.py` (1357)** → **[VERIFIED-AMENDED: must be flat siblings, not a nested package]**
  `core/llm_streaming.py` (SSE/stream machinery), `core/llm_transient.py` (retry/backoff/error
  classification), `core/llm_toolcall.py` (native tool-call parsing), each with its own `_LAYOUT`
  row — a nested `core/llm/` package is blocked by `_LAYOUT["llm"]="core"` requiring `core/llm.py`
  to exist *as a file* (`test_every_layout_entry_exists_at_its_canonical_path`). Keep the 3
  client classes in `llm.py` importing the helpers back. *Grep tests for
  `monkeypatch.setattr("looplab.core.llm._X")` before any move; re-import moved names into `llm.py`.*
- **`adapters/repo_developer.py` (1323)** → split the write-tool provider `RepoWriteTools`
  (+ `_stage_output_values`/`_xlsx_to_markdown`) into `adapters/repo_write_tools.py`, leaving the
  `LLMRepoDeveloper`/`LLMOnboarder` personas (~570/~750 split along tool-vs-persona).
- **`cli.py` (1037)** → command groups. **[VERIFIED-AMENDED, three traps]:** (a) new root-level
  modules (`cli_run.py`, …) FAIL `test_no_stray_modules_at_package_root` — convert `cli.py` to a
  `looplab/cli/` *package* instead (`looplab.cli:app` keeps resolving for BOTH console scripts,
  `looplab` and the `LoopLab` alias; nested package files are exempt from `_LAYOUT`); (b)
  `looplab/bench.py:23` lazily imports **private** `cli._engine` ("lazy to avoid an import
  cycle" — its own comment): put `_engine` in the run-command module and let bench import it
  top-level, or promote it to a public engine-builder — this also retires one more load-bearing
  lazy import (P2.3's class); (c) verify no test imports a private `cli._helper`.
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
- **`ui/src/` (ADDED by gap-hunt — the UI was outside the six axes' scope).** Largest files:
  `panels.jsx` 1106, `Inspector.jsx` 1017, `AssistantBar.jsx` 689, `util.js` 687, `Dock.jsx` 647.
  `util.js` is a grab-bag → split into api-client / formatters / DAG-layout (`layoutWithGroups`,
  ~180 LoC) / CONTROL action map. Eight hand-rolled `setInterval` polls with inconsistent
  periods (`AssistantBar:138`, `Dock:192,500`, `Inspector:80,684,858`, `RunList:168`,
  `panels:639`); only RunList has a `document.hidden` guard → one shared `usePoll` hook.
  Server-side twin: `serve/routers/genesis.py:268-300` hand-rolls the spawn+inline-wait that
  `srv.jobs.run_as_job` already encapsulates (used by assistant/boss/reports) → add a progress
  hook to `run_as_job` and unify. Good news: `hooks.js` SSE and the `util.js` fetch wrappers are
  already single-sourced — the UI is under-factored at the panel layer, not a duplication swamp.

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
- **[ADDED by gap-hunt] `looplab/sweep.py` is a frozen generated-code contract — do not move,
  rename, or change its surface.** Its import path (`from looplab.sweep import run_sweep`) is
  baked into the Developer prompt (`agents/roles.py:126-138`, `_SWEEP_CONTRACT`); its
  `{"trials": [...]}` final-stdout line is parsed by `runtime/sandbox.py` (`json_line_trials`)
  and `runtime/command_eval.py:578`; it honors the `LOOPLAB_EVAL_SEED` env contract the confirm
  phase varies; and generated solution code in HISTORICAL runs imports it — a move breaks
  resume/re-run of old runs. Public API + trials-line format + env contract are frozen.
- **[ADDED by gap-hunt] External string-referenced entry points are part of the frozen surface**
  until P4.8's resolution test lands: `looplab.runtime.jupyter:setup_looplab`
  (jupyter_serverproxy), `looplab.cli:app` (BOTH the `looplab` and `LoopLab` console scripts).

## 5. Cross-axis finding index (traceability)

- **Duplication:** P1.1–P1.8.
- **Layering/architecture:** P2.1 (F1), P2.2 (F2), P2.3 (F5/F10), P2.4 (F9), P4.1 (F4), P4.2/P4.3
  (F7), P4.6 (F6), P5.1 (F8), P3+P4.4 (F3 — knob triplication → `**knobs` validated by
  `EngineOptions`, deleting ~150 redundant signature lines + the dual-default-sync obligation;
  verified 68/68 exact knob↔field parity and keyword-only signature — zero call-site breakage).
  **[F3 RESOLVED at implementation]:** the 68 `_UNSET` signature knobs are now `**knobs`
  validated against EngineOptions field names (unknown knob -> TypeError, like a real keyword);
  the 16 object seams stay explicit; per-knob docs live on EngineOptions. orchestrator.py is
  ~1455 lines. **[P5.1 RESOLVED]:** fold() is a 63-handler dispatch table gated by the new
  golden-log test (which caught a real transformation bug mid-work — its designed job).
- **God-file decomposition:** P3 (orchestrator, full detail), P5.2 (llm/repo_developer/cli/tui/
  agent/lessons).
- **Agent-friendliness:** P0.2/P0.3 (CLAUDE.md + comments), P2.5 (`runs_tools` rename), P4.2/P4.3
  (seam registries), P4.5 (docs-sync test).
- **Dead-code/cruft/naming:** P0.1 (deletions), P1.6 (perm-modes dup), P2.5 (naming),
  P0.3 (layout docs).
- **Config/events/model surface:** P1.5 (`robust_metric`), P4.4 (dual defaults + `novelty_semantic`),
  P5.1 (`fold`), P5.3 (model hygiene). Verified-clean set in §1.
- **Verification pass:** P0.4, P4.7, P4.8, the `[ADDED by gap-hunt]` §1/§4 entries, and every
  `[VERIFIED-AMENDED]` marker.

---

## 6. Verification pass — what the adversarial fact-check changed

Two independent verifiers re-checked the document the same day: one re-read **every cited
`file:line`** (80 tool calls; line evidence proved exceptionally accurate — every orchestrator/
replay/test line checked was exact), the other swept the areas the six axes never covered.
Outcome, so a reader knows the document's error profile:

**Corrections that changed a recommendation (all now integrated in place):**
1. **P1.4** — `lessons.py:444` is an append-under-interprocess-lock to the SHARED lessons store,
   not an atomic rewrite; routing it through `write_jsonl_atomic` would lose concurrent runs'
   rows. The one genuinely unsafe recommendation in the original draft.
2. **P2.5** — the `_LAYOUT` shim cannot express renames, and the proposed `AllRunsTools` name
   collides with an existing class in the twin file. New plan: `MachineRunsTools` + explicit
   importer updates.
3. **P2.1** — importer inventory was incomplete: 2 test files import the canonical
   `looplab.serve.traceview` path (the shim only rescues flat names), and `routers/runs.py:476`
   pulls private `_tree`/`_cap_span_io`.
4. **P1.1** — the real cross-site divergence is neighbour-*eligibility* semantics (3 different
   filters), not the zero-distance edge (semantically identical in all three).
5. **P4.4** — 16 divergent defaults, not 15; and the `novelty_semantic` fix is a deliberate
   library-default change (live for `Engine(novelty_gate=True)` callers), not a no-op.
6. **Layout-test traps** — `_LAYOUT` coverage spans ALL ten subpackages (not just engine/); the
   package root is pinned to `{__init__, cli, bench, sweep}`; `core/llm/` cannot become a nested
   package. These re-shaped P2.4 and the P5.2 cli/llm split plans.
7. **Numbers** — knobs are 68/68 (1:1, keyword-only; strengthens F3), orchestrator has 98 defs
   (not ~130), fold has 63 arms (one dual-type), P1.3 uses two different JSON parsers across its
   8 sites, P1.5 had 6 more copies (`bench.py:48` + `tools/e2e_report.py` ×5), P1.8's
   `load_spans` swap changes skip-vs-stop semantics, P0.2's `bind_state` is optional-but-strict,
   P4.2's Protocol is already `@runtime_checkable`.

**Additions from the gap-hunt (areas outside the six axes):** the `sweep.py` frozen contract and
string entry points (§4), dead UI components (P0.4), UI structure (P5.2 last bullet), the
prompt-key registry + repo-developer PromptStore bypass (P4.7), entry-point resolution test
(P4.8), the shared `_engine` test-builder prep before P3, and the §1 clean verdicts
(concurrency/locking, logging, small modules, prompt-key sync, CI nits).

## 7. Implementation ledger (2026-07-10, all phases executed)

Every phase landed on `claude/framework-agent-review-e49sp6`, each followed by an
adversarial ultra-review whose findings were fixed in a paired commit:
- **P0** `0255efe`+`e493f19` — deletions, CLAUDE.md truth, dead UI components.
- **P1** `4229615`+`e864052`+`cdea797` — the dedup helpers (knn_idw, numeric_params,
  read_jsonl_lenient/write_jsonl_atomic, Node.robust_metric ×18, perm-modes,
  _repropose_with_feedback, scanners/coercions/srv.state/query_nvidia_smi).
- **P2** `f6e5b0a`+`ccf1457` — projections→events/, make_llm_client→core, gitenv→core,
  MachineRunsTools rename, documented validate_task exception.
- **P3** `6705170`+`b01afa8` — seven more verbatim mixins; orchestrator 2800→1514; the
  shared `_UNSET` sentinel moved to options.py (caught by test_inline_repair).
- **P4** `aac0154`+`42bb274` — six registries/guards (BACKGROUND_APPENDABLE,
  TASK_OPTIONAL_HOOKS, DEVELOPER_OUTPUT_ATTRS/RESEARCHER_ACTION_ATTRS, divergence table,
  config-docs sync, PROMPT_KEYS, entry-point resolution, wrapper-enumeration guard); P4.6
  resolved structurally (setattr channel kept, four-ways guarded).
- **P5** `abc3632` (fold→63-handler dispatch, golden-log gated — the gate caught a real
  transformation bug mid-work), `86e3480` (F3 **knobs collapse; orchestrator ~1455),
  `83f7e62` (P5.3 banners), `eecd3ea`+`3e89272` (P5.2 splits: llm→3 siblings, agent→
  tool_loop with the run_phase seam analysis, lessons→3 mixins, repo_write_tools, cli→
  package with __main__, tui→api/format, UI api/format/layout + usePoll + run_as_job).
  Note: `eecd3ea` briefly carried the staged `cli.py` deletion ahead of the package commit
  (a cross-agent staging sweep); the branch tip is complete and green.

Final state: full suite 1685 passed / 23 skipped; offline smoke + replay byte-identical
across every phase (BEST node 7, metric=4.48271); UI builds; both console scripts + the
jupyter entry point resolve; the golden-log replay gate is now a permanent fixture.

**Net assessment:** the plan survives verification intact — no phase was invalidated. The
corrections tighten execution details (what to exclude, which importers to update, which tests
gate which move); the additions extend scope to the UI, tests, prompts-plumbing, and packaging
surfaces the original review never looked at.
