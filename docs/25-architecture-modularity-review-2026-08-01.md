# LoopLab — Architecture, Modularity & Duplication Review (2026-08-01)

> Reviewed repository baseline: `756ad1372f9acb9a66213bceee7a0f152256bf50` (`master`, 2026-08-01).
> Snapshot size: 235 production Python modules under `looplab/` (~122.7k physical lines), 287
> `test_*.py` files, 123 registered event-type constants, and a ~50.9k-line React control plane in
> `ui/src`.
>
> This document is a **structural review ledger**: duplication, over-engineering, god-modules,
> under-decomposition, mergeable entities, dead code, layering drift and inconsistency. It is
> deliberately complementary to [doc 16](16-architecture-code-review-2026-07-11.md) (the
> correctness/bug finding ledger) and [doc 17](17-project-review-and-directions-2026-07-11.md)
> (the delivery plan): findings here are about **maintenance cost and drift risk**, not (except
> where explicitly noted) about currently-wrong behavior. Doc 17 still owns release ordering.

## 0. Scope and method

The whole tree was reviewed by fourteen parallel subsystem reviews, each reading its assigned
files in full (including every file above 1,000 lines, chunk by chunk), plus one dedicated
cross-package pass (import graph, cross-package duplication, dead top-level code, registry
sprawl, test-suite structure) and one UI pass that also cross-checked every client-constructed
API path against `looplab/serve/routers/*`. The fourteen scopes together cover every package in
the map in `CLAUDE.md` plus `ui/`:

| § | Scope | Findings (high/med/low) |
|---|---|---|
| 4.1 | engine — execution spine (orchestrator, node_build, eval_dispatch, evaluate, eval_stages, crash_repair) | 2 / 6 / 6 |
| 4.2 | engine — cadence/monitoring/wrap-up mixins | 3 / 7 / 5 |
| 4.3 | engine — cross-run memory & knowledge (memory, lessons, claims, concept_registry, stewards) | 2 / 10 / 3 |
| 4.4 | events | 1 / 7 / 5 |
| 4.5 | core | 1 / 6 / 6 |
| 4.6 | serve — non-router modules | 3 / 9 / 4 |
| 4.7 | serve — routers | 3 / 9 / 3 |
| 4.8 | search | 1 / 8 / 6 |
| 4.9 | agents | 1 / 2 / 7 |
| 4.10 | tools | 2 / 7 / 2 |
| 4.11 | runtime + adapters | 2 / 5 / 3 |
| 4.12 | cli + trust + top-level modules | 2 / 7 / 6 |
| 4.13 | cross-package / whole-tree | 1 / 6 / 5 |
| 4.14 | ui/src + ui/test | 3 / 6 / 5 |
| | **Total: 188 findings** | **27 / 95 / 66** |

Rules of evidence used by every reviewer: each finding cites concrete `file:line` locations
against the reviewed baseline; every dead-code claim was checked by repo-wide grep including
`tests/`, `docs/` and `ui/`; documented intentional design (the engine mixin layout, the
back-compat import shim, registry-guarded seams, the flat `RunState`, prompt-string contracts)
was not reported as a defect unless the report argues concretely why it still costs. A sample of
load-bearing claims was independently re-verified during synthesis (the seven `_is_reparse`
copies, the byte-identical `_PathLocks` pair, zero `METRIC_READERS` consumers, the tests-only
`_read_stream` stack, the four `_json_object` router copies, the sizes/positions of
`normalize_control` and `_derive_cards`).

Line numbers in this document are anchored to the baseline commit above and will drift; treat
them as starting points for `grep`, not as durable references.

### 0.1 Post-baseline reconciliation and verification (2026-08-01)

After the baseline was pinned, `master` advanced by one commit — `c92b89f` («perf: stop
blocking the ASGI event loop, and make two hot paths sub-quadratic») — which closes the entire
in-code `[PERF]`-marker backlog this review catalogues: the blocking async handlers (SR-08,
including the span-scan item), the O(n²) cross-process `EventStore.read_all` validation
(EV-07), the full-log read on the per-command append path (SC-08), and the invalid-pin
strategist re-derivation (EC-15). Those findings are retained below exactly as they held at the
baseline, each carrying a *Status (post-baseline)* note; theme T7 and the P5 plan reflect the
fix. `c92b89f` also grew `serve/routers/*`, `api.js` and `eventstore.py`, so cited line numbers
in those files lag the current tree by up to ~100 lines.

Separately, all 188 findings were re-verified by a dedicated adversarial fact-checking pass
(one verifier per section, re-reading the cited code and re-running every count and dead-code
grep): 137 findings were confirmed as written; 47 carried factual corrections — miscounts,
misquoted comments, misattributed locations — that are folded into the text below; and the only
4 refutations were the post-baseline `c92b89f` fixes above. No finding was refuted at the
baseline.

### 0.2 Second reconciliation (2026-08-02, HEAD `41813bd`)

`master` has since advanced by further commits (mobile/UI hardening, settings, attention and
assistant features, a test re-pointing sweep — and `a077d86`, the first structural commit acting
on this document). A dedicated per-finding status pass over the new HEAD produced the
remediation ledger in **§5** (running total as of `cea97c3`: 7 fixed, 4 partial, 177 open — and
several flagged god-modules grew substantially in the same window, §5.3). **§6** adds the
target-design proposals for resolving the finding clusters; §6 was itself adversarially
validated against HEAD `a077d86` by nine design reviewers, whose corrections (naming collisions
with existing modules, seam-preservation blockers, split-mechanics fixes) are folded in. Line
numbers below still reference the baseline.

### Severity model

- **HIGH** — structural debt that demonstrably multiplies maintenance cost today: an
  invariant-critical protocol that exists in N hand-synced copies, a god-module that every
  change to a subsystem must churn, or an unguarded cross-package seam whose silent breakage is
  the exact failure class the project's registry discipline exists to prevent.
- **MEDIUM** — real duplication/misplacement with a bounded blast radius, or acknowledged
  perf/structure debt with an in-code marker that was never resolved.
- **LOW** — dead code, stale load-bearing comments, naming hazards, micro-duplication worth
  fixing opportunistically.

Category vocabulary: `duplication`, `over-engineering`, `mergeable-entities`,
`under-decomposition`, `flat-code`, `dead-code`, `layering`, `inconsistency`,
`excessive-logic`, `other`.

## 1. Executive summary

The codebase's engineering discipline is genuinely unusual — and genuinely uneven. The
correctness machinery (replay safety, at-most-once paid-call protocols, fail-closed receipt
validation, registry-guarded seams, load-bearing why-comments) is applied with rigor that most
codebases never reach, and reviewers repeatedly found that the *hard* problems are solved well.
The structural debt is concentrated in three repeating shapes:

**1. God-modules survived their own documented splits.** The 17-file Engine mixin split, the
router split out of `make_app`, the `util.js` → pure-models UI refactor and the `llm.py`
three-way split all happened — and then accretion resumed at the residual centers. Today the
tree holds one 5,880-line orchestrator that still embeds two whole subsystems that postdate the
split (a ~1,000-line Card ledger and a ~500-line speculation-gate calibration envelope, ES-01),
a 5,563-line `replay.py` in which the derived-Card projection (~2,200 lines — about 40% of
the file, including one 818-line function) has outgrown the fold core it sits beside (EV-01), a 4,103-line
`run_commands.py` spanning five separable subsystems (SC-01), a 2,896-line `claims.py`
spanning six (EM-01), a 3,103-line router file carrying ~1,400 lines of distributed-storage
machinery with zero HTTP content (SR-02), and — on the UI side — a 2,216-line `api.js` and
three ~2,000-line components (UI-03, UI-04, UI-05). The pattern is consistent: the split
mechanism exists and works (mixins, `_LAYOUT` shim, barrel re-exports), but nothing stops the
largest module in each package from regrowing one 300–800-line function (XP-06).

**2. Invariant-critical protocols exist in N hand-synchronized copies.** This is the highest-risk
finding class, because these are exactly the places where a one-copy fix has already happened at
least once: the node-creation commit epilogue exists three times and the false-success sentinel
guard was retrofitted into all three copies separately (ES-02); the developer-crash
terminal+pause event pair is spelled five times with already-drifting pause reasons (EC-03); the
completion-certificate invalidation block exists five times in the fold and its own comment
records the selection bug one drifted copy shipped (EV-03); the tail-CAS append retry loop is
hand-rolled eight times with accidentally-inconsistent exhaustion policy (ES-07); the durable
paid-work idempotency protocol was re-invented five-plus times across routers with
byte-near-identical helper pairs (SR-01); canonical run-path validation exists in six-plus
variants and `_is_reparse` in seven copies (SC-03); the file-identity stat tuple is hand-rolled
in 10+ modules across five packages (XP-02); and the UI's durable command lifecycle state
machine is copy-pasted nearly line-for-line between two components (UI-01).

**3. The consolidation pattern is known but unevenly applied.** The repo's own best artifacts —
`SearchFitness`, `hybrid_merge.agent_merge`, `RunStateCache`, `drive_tool_loop`,
`comment_projection.apply_comment_event`, `usePoll`, the fold handler registry — are all
explicit one-spelling extractions that replaced counted copies, usually with the rationale
written at the seam. The newer accretions (cross-run context builders, steward drivers,
paid-work ledgers, resource-load hooks in the UI) simply haven't received the same treatment
yet. Relatedly, roughly thirty **acknowledged in-code review markers** shipped in production at the
baseline (15 `CLAUDE REVIEW: [PERF]` + 14 `CODEX AGENT:` comments), each describing a diagnosed
perf or design defect — the blocking-handler-on-event-loop notes in routers (SR-08), the
O(events) re-fold apologies in the engine (ES-12, EC-02), the whole-repo source hash that
revokes speculation receipts on comment-only commits (SE-01). Notably, the entire `[PERF]`
subset was fixed on `master` by `c92b89f` immediately after this baseline (see §0.1); the
`CODEX AGENT:` queue still ships and this document folds it into the ledger so it stops living
only in comments.

Alongside these, the review verified a concrete **dead-code inventory** (~25 items, §3), a
handful of **layering violations** against the documented rules (the single core→agents import,
tools→serve imports that sibling modules explicitly forbid, the lazy search↔engine cycle —
XP-01, XP-03, XP-04, CO-07, TO-03, SE-07), and one systemic **unguarded seam**: twelve
underscore-private functions of `engine/memory.py`/`engine/claims.py` consumed across the
package boundary by `tools/` (~20 import sites) with no registry guard, where a "safe" engine-internal rename
silently turns every cross-run tool into a permanent `(cross-run tool unavailable)` (XP-01,
TO-09).

What this review did **not** find is also worth recording: the documented layering otherwise
holds across the whole tree (zero events→above-core edges, zero engine→serve edges); all 200
`_LAYOUT` back-compat shim entries resolve; no CLI command re-implements an `events/`
projection; and no client/server API route mismatches exist between `ui/src` and
`serve/routers/*`.

## 2. Cross-cutting themes and priority plan

The 188 findings compress into seven themes. Each theme lists its member findings by ID
(§4 holds the full evidence); the plan at the end of this section orders the work.

### T1 — Invariant-critical duplication (fix first)

The copies where drift is a correctness event, not a style problem:

- **ES-02** node-creation commit epilogue ×3 (`orchestrator.py` — the same bug already fixed 3×).
- **EC-03** developer-crash `node_failed`+`pause` pair ×5 (reason strings already drift).
  *(resolved 2026-08-02 — `node_build.developer_crash_records`.)*
- **EV-03** completion-certificate invalidation ×5 in the fold (one drifted copy already shipped a
  selection bug). *(resolved 2026-08-02 — `replay._invalidate_completion_certificates`.)*
- **ES-07** tail-CAS append retry loop ×8 (inconsistent exhaustion behavior by accident).
- **ES-06** eval-admission fence triplicated between serial/parallel dispatch.
- **SR-01** durable paid-work idempotency protocol ×5+ across routers (byte-identical helper pairs prove the abstraction exists).
- **SC-03** canonical run-path/run-id validation ×6, `_is_reparse` ×7, Windows reserved names ×4.
- **EM-04** durable curation-key derivation duplicated between writer and validator (drift = ledger poisoning).
- **EV-05** tolerant-JSONL-prefix scan ×4–5 (equivalence maintained by comments). *(resolved
  2026-08-02 — `eventstore.decode_jsonl_line` / `scan_jsonl_region`.)*
- **EV-06** `EventStore.append`/`append_many` duplicate the ~55-line critical section. *(resolved
  2026-08-02 — `EventStore._locked_append`.)*
- **RA-03** Docker hardening argv mirrored by comment (already drifted once). *(resolved
  2026-08-02 — `sandbox.docker_run_argv` / `require_docker_cli`.)*
- **RA-06** direction validator present in only 2 of 9 task models (objective-flip risk).
- **ES-11** `"(developer error:"` magic-string protocol at 6 consumer sites, no shared constant.
- **XP-02** file-identity stat tuple ×10+ across five packages.
- **UI-01** durable command lifecycle state machine copy-pasted between Dock and AssistantBar.

### T2 — God-modules / under-decomposition

`looplab/engine/orchestrator.py` 5,880 (ES-01, ES-04, ES-05), `looplab/events/replay.py` 5,563
(EV-01, EV-08), `looplab/serve/run_commands.py` 4,103 (SC-01, SC-02, SC-07),
`looplab/serve/routers/reports.py` 3,103 (SR-02), `looplab/engine/claims.py` 2,896 (EM-01),
`looplab/serve/routers/runs.py` 2,845 (SR-04), `looplab/search/speculation_quality.py` 2,615
(SE-01), `looplab/core/models.py` 2,181 (CO-02), `looplab/engine/speculation.py` 1,968 (EC-02),
`looplab/search/concept_graph.py` 1,680 (SE-09), `looplab/tools/machine_runs_tools.py` 1,659
(TO-02), `looplab/cli/inspect_cmds.py` 1,701 (CT-01), `looplab/engine/memory.py` 1,600 (EM-10),
`looplab/agents/roles.py` 1,058 (AG-02); UI: `api.js` 2,216 (UI-02), `panels.jsx` 2,351
(UI-04), `AssistantBar.jsx` 2,031 (UI-05), `RunView.jsx` ~2,000 (UI-03), `Dock.jsx` 1,605
(UI-08). Giant single functions inside them: `_derive_cards` 818 lines, `normalize_control`
775, `Engine.__init__` 770, `_evaluate` ~700, `cross_run_tools._execute` ~856,
`_run_card_session` ~500, `generate_scope_report_ep` ~530, `_execute` ~460, `_reset_blocking`
~410, `run()` 347, `drive_tool_loop` ~370 (XP-06 collects these).

### T3 — Mergeable parallel entities

Two-or-three parallel implementations of one concept: reset vs deletion durable-transaction
frameworks (SC-06, CO-01), two at-most-once curation protocols + four row-schema generations
(EM-03), three claim-identity systems with three decision-overlay resolvers (EM-06), three
receipt-carrying list subclasses (EM-09), three foreign-run reader tool wrappers (TO-05),
RepoTools vs RepoScoutTools (TO-06), MemoryTools vs CrossRunTools over the same ledger with
contradictory scoping (TO-07), train/asha monitor scaffolding (EC-04), three researcher-wrapper
delegation styles (SE-02), verify.py vs verifier.py (CT-09), ShareStore vs ReviewStore (SC-10),
command_observation vs log_pages incremental indexes with a byte-identical `_PathLocks` (SC-04),
legacy hypothesis board mirroring the Card event family end-to-end (EV-13), five copy-paste
synthetic task adapters (RA-06).

### T4 — Unguarded seams and layering drift

- **XP-01 / TO-09** tools→engine: ~20 private `_`-names imported lazily with exceptions swallowed
  into `(cross-run tool unavailable)` — silent-rename class, no registry guard.
- **XP-03 / TO-03** tools→serve imports in `machine_runs_tools.py`, contradicting the rule its
  sibling modules state as a design invariant (and duplicating `run_generation` to avoid the
  import in the same file that makes it).
- **CO-07 / XP-04** the single core→agents import (`Settings` validation lazily imports
  `agents.cli_agent.PRESETS`).
- **SE-07 / XP-07** the lazy search↔engine cycle around speculation calibration constants —
  contradicting `speculation_calibration.py`'s own stated purpose.
- **AG-07** agents↔search cycle held apart only by import placement, direction undocumented.
  *(resolved 2026-08-02 — stated in CLAUDE.md, enforced by `tests/test_agents_search_direction.py`.)*
- **SR-12** router-to-router private imports and late-bound `srv.*_fn` attribute wiring (also XP-05).
- **AG-03** the Strategy field set synchronized across five encodings with no registry test —
  *(resolved 2026-08-02 — `tests/test_strategy_field_registry.py`.)*
  the exact failure class the repo's registry convention exists to prevent.

### T5 — Over-engineering / excessive logic

- **SE-01** the speculation-gate stack (~3,450 lines guarding one Settings knob) whose
  whole-repo raw-byte source hash revokes every receipt on comment-only edits — the file's own
  comment calls the consequence an operational outage and names the fix.
- **SE-12** a 15-case fixture-built test suite shipped as production code and re-executed twice
  per gate validation.
- **EM-12** the fail-closed receipt discipline implemented as ~8 hand-rolled validators with no
  declarative helper.
- **CO-04/CO-05** three parallel SSE stream-reassembly loops in one client, one of them
  production-dead; **CO-10** blanket private re-export shim with a subtly wrong monkeypatch claim.
- **CT-12** trust library surfaces (cv splitters, detector calibration) with no production
  caller across multiple review cycles.

### T6 — Dead code (verified; see §3 inventory)

### T8 — Non-deterministic full-suite failures (added 2026-08-02)

FOUR concurrency tests have each failed EXACTLY ONCE across ~12 full-suite runs on 2026-08-02,
and each passes 6–8/8 when run alone. They are in unrelated subsystems and each failure landed in a
run whose only changes were elsewhere, so the common factor is load, not any one edit:

| test | observed failure |
|---|---|
| `test_run_command_service.py::test_reload_finalize_reattaches_existing_record_without_event_or_spawn_duplication` | worker still `executing` when `_terminal` gave up |
| `test_cross_run_server.py::test_concept_global_cas_fences_alias_and_split_ledgers` | CAS fence |
| `test_lessons_fingerprint.py::test_run_writes_lessons_with_fingerprint` | — |
| `test_report.py::test_scope_report_clear_fence_prevents_rebill_after_receipt_and_marker_loss` | paid-work clear fence |

This is **not** a tight-budget problem, and raising ceilings again would only hide it. `_terminal`
already allows **60 s** (raised from 15 s for exactly this symptom, per its own comment), and
`_WORKER_START_TIMEOUT_S` is 60 s for the same reason. A background command worker that has not
reached a terminal status in a full minute is stalling, not merely descheduled.

Two of this family WERE diagnosed properly the same day and are fixed rather than deferred, which
is what makes the remainder worth treating as a real defect class:

* `test_concurrent_disjoint_settings_puts_do_not_lose_updates` — the rendezvous barrier timed out
  because one `PUT /api/settings` came back **405**: FastAPI 0.13x's lazy `include_router` cache is
  not thread-safe, and two concurrent first requests matched against a half-built candidate list.
  Fixed in production (`_warm_route_matching`), not in the test.
* `test_concurrent_same_key_returns_one_start_identity_and_one_popen` — the test pinned one member
  of a fail-closed SET as if it were the contract. Fixed by asserting the set plus the invariants
  that never vary (one Popen, one shared `start_id`).

*Recommendation:* treat each remaining case the same way — reproduce under parallel load with
thread-stack dumps (the technique that found the 405), and fix the stall. Every guard added by this
campaign is worth less while the suite is intermittently red for reasons no one has read.

### T7 — Acknowledged-but-unfixed in-code review markers

`CLAUDE REVIEW:`/`CODEX AGENT:` comments shipping in production, each describing a diagnosed
defect with a prescribed fix that had not been applied. At the baseline these included the
twelve async-handler-blocks-event-loop notes in routers (SR-08), the O(events) full-log read on
the per-command append path (SC-08), the O(n²) cross-process `read_all` validation (EV-07) and
the strategist short-circuit defeat (EC-15) — all four closed on `master` by `c92b89f`
immediately after the baseline, together with every `CLAUDE REVIEW: [PERF]` marker. Still
shipping: the 14 `CODEX AGENT:` comments — the per-idle-turn ~8× full-log re-folds in
`_run_card_session` (EC-02) alongside the engine spine's repeated fold apologies (ES-12), the
per-candidate concept-projection rebuild in `card_score` (SE-04), and the gameable
self-reported selection signals (SE-11). Recommendation: convert each remaining marker into
either a fix or a tracked issue — shipped annotations are where this class of debt goes to be
forgotten.

### Priority plan

1. **P1 — single-spelling extractions for T1** (mostly small/medium effort, mechanical,
   behavior-preserving, each kills a live drift risk): `_commit_built_node`,
   `_append_developer_crash`, `_invalidate_completion_certificates`, `retry_tail_cas`,
   `core/pathsafe.py` (run-path validation + `_is_reparse` + reserved names),
   `core/atomicio.file_identity`, the shared JSONL-prefix scanner, `_locked_append`, the shared
   Docker argv builder, the direction validator on all task models, `DEVELOPER_ERROR_PREFIX`,
   and the UI `useDurableRunCommand` hook.
2. **P2 — seam guarding (T4)**: promote the tools-consumed engine privates to a public
   read-model facade (or registry-guard the import list); move the developer-backend key set
   into core; break the tools→serve imports via injected callables; add the Strategy-fields
   registry test; document the agents↔search direction.
3. **P3 — the five worst splits (T2)**, in order of churn-reduction per effort: extract the Card
   ledger from `orchestrator.py` and from `replay.py` (they are two halves of one subsystem);
   split `run_commands.py` and turn `normalize_control` into the per-event strategy table its
   registries already imply; split `claims.py` along its comment-delimited seams; extract the
   reports.py storage machinery and control.py trace-clear into serve modules (matching
   reset/deletion precedent); split `api.js`/`RunView.jsx` along the P5.2 precedent.
4. **P4 — dead-code deletion (§3)** — cheap, immediate reader-cost reduction.
5. **P5 — decision items** (need an owner decision, not just a patch): speculation-gate
   source-hash → versioned manifest (SE-01); converge the three claim-identity systems (EM-06)
   and the two curation protocols (EM-03); CaseLibrary wire-or-delete (EM-11); cv.py/trust
   seams wire-or-move (CT-12); hypothesis-board shadow-family normalization (EV-13). (Two items
   originally slated here — the blocking handlers SR-08 and read_all's O(n²) validation EV-07 —
   were fixed on `master` by `c92b89f` before this document merged.)

## 3. Verified dead-code inventory

Every entry below was confirmed by repo-wide grep (`looplab/`, `tests/`, `docs/`, `ui/`) at the
reviewed baseline; "tests-only" means the only callers are test files pinning the dead code
itself. Full evidence in the referenced findings.

| Item | Where | Status | Finding |
|---|---|---|---|
| Legacy urllib-era streaming stack: `_read_stream`, `_sse_chunks`, `_socket_watchdog`, `_SSETail`, `_raw_socket`, the `urllib.request` import (~185 lines) | `core/llm.py`, `core/llm_streaming.py` | tests-only | CO-03 |
| `CaseLibrary` (VectorStore-backed episodic store, ~105 lines) | `engine/memory.py:1496` | tests-only; `JsonlCaseLibrary` is the real store | EM-11 |
| `apply_concept_curation` (~60 lines, bypasses CAS discipline) | `engine/concept_steward.py:355` | tests-only; contradicts the module's own invariant | EM-14 |
| `_acquire_gpu`/`_release_gpu` single-GPU wrappers | `engine/resources.py:440` | tests-only; replacement already planned in docs/23 | EC-14 |
| `locked_claim_evidence_snapshot` (~60-line lock context manager) | `engine/claims.py` | **DELETED** (2026-08-02) | XP-08 |
| `claim_governance_snapshot` | `engine/governance_health.py` | **DELETED** (2026-08-02) | XP-08 |
| `build_digest` | `serve/scope_report.py` | **DELETED** (2026-08-02) | XP-08 |
| `_scope_action_lease_marker_exists` | `serve/routers/reports.py` | **DELETED** (2026-08-02) | SR-13, XP-08 |
| `_normalized_rename_map`, `_canon_set` | `search/concept_graph.py` | **DELETED** (2026-08-02) | XP-08 |
| `_explored_concepts` | `search/card_selection.py` | **DELETED** (2026-08-02) | SE-13, XP-08 |
| `METRIC_READERS` registry (docstring falsely claimed it was shared) | `adapters/tasks.py` | **DELETED** (2026-08-02) | RA-04 |
| `perm_modes.decide()` (kind-only compat helper) | `tools/perm_modes.py:232` | tests-only; nothing left to be compatible with | TO-10 |
| `VectorStore.delete`/`rebuild` protocol methods | `tools/vectorstore.py:246-256` | rebuild: zero callers; delete: tests-only (documented future seam) | TO-10 |
| `RunTools.parent` stored-but-never-read attribute | `tools/run_tools.py:66` | write-only | TO-10 |
| Unreachable "paired core commit" fallback lattice (~80 lines) | `search/concept_projection.py` | `core/concepts.py` shipped; branches cannot execute | SE-05 |
| `RunState.grouped_beliefs()` (60-line projection, self-described "no production consumer") | `core/models.py:2079` | tests+docs only | CO-11 |
| `POST /api/research`, `GET …/agents_md` endpoints | `serve/routers/genesis.py`, `runs.py` | no UI/TUI caller; tests only — confirm no external consumers before removal | SR-13 |
| `_portfolio_identity` compat wrapper | `serve/routers/cross_run.py:56` | tests-only | SR-13 |
| Dead `state` parameter of `_persist_node_concepts` (rebound before first read) | `cli/inspect_cmds.py:108` | parameter never read (the caller's fold itself is still consumed elsewhere) | CT-11 |
| `Inspector.Agent` component (never rendered; tab removed) | `ui/src/Inspector.jsx:2128` | zero JSX references | UI-10 |
| Speculative Card-kanban lanes self-described as unreachable | `ui/src/panels.jsx:1329` | dead configuration in the render path | UI-14 |

Adjacent stale-comment fixes (the repo treats stale load-bearing comments as bugs):
`atomicio.strict_atomic_write_bytes`'s "zero production callers" REVIEW note is now false
(CO-12); `SearchFitness.selection_key`'s "no callers" framing is stale (CO-11); `parse.py`'s
"one spelling of scalar coercion" claim is no longer true (CO-09); `mlebench_real._competition`'s
"single place" claim is contradicted by two copies (RA-10).

**Update (2026-08-02):** the seven zero-reference helpers and `METRIC_READERS` were deleted by
`a077d86` (the rows above are flipped to DELETED in that same change, per the §6.8 upkeep rule).

**Explicitly not dead** (checked and cleared): `adapters/kaggle_dl.check_auth` is a documented
operator command (docs/MLEBENCH.md); all 200 `_LAYOUT` shim entries resolve; `timings` is a
genuinely distinct spans aggregation, not a re-implemented projection.

## 4. Subsystem findings

Finding IDs are `<scope>-<nn>` where the scope prefixes are: ES (engine spine), EC (engine
cadence), EM (engine memory), EV (events), CO (core), SC (serve core), SR (serve routers), SE
(search), AG (agents), TO (tools), RA (runtime+adapters), CT (cli/trust/misc), XP
(cross-package), UI (React UI). Within each scope, findings are ordered roughly most-severe
first, as ranked by the subsystem review.
### 4.1 Engine — execution spine

Scope: `looplab/engine/`: orchestrator.py, node_build.py, eval_dispatch.py, evaluate.py, eval_stages.py, crash_repair.py.

**Reviewer assessment.** The engine spine is a deliberately event-sourced orchestrator whose replay/idempotence discipline (one terminal per node, receipt-before-paid-call, CAS-guarded finish) is genuinely strong and well-tested. However, orchestrator.py at 5,880 lines remains a god-module even after the 17-file mixin split: the split moved out the leaf clusters (eval, stages, crash repair) but left at least three large, separable subsystems inline — the ~500-line speculation-gate calibration/admission envelope, the ~1,000-line native-Card ledger, and a 770-line __init__ — plus a run() loop whose `creates` branch alone is ~230 inline lines. The dominant maintenance risks are triplicated node-creation epilogues (which have already required the same bug fix applied three times), duplicated eval-admission fencing between the serial and parallel dispatch branches, and eight hand-rolled tail-CAS retry loops with no shared helper.

**Strengths worth preserving:**

- Rigorous replay-safety discipline applied consistently: receipt-before-paid-producer ordering (fork/inject at orchestrator.py:2890-2924, 2951-2962), exactly-one-terminal-per-node (evaluate.py:252-256, 811-823), and CAS-adjacency-checked finish paths (_finish_if_quiescent / _finish_with_report_if_quiescent, orchestrator.py:1371-1478).
- Load-bearing incident-grounded comments that record real failure modes and why each guard exists (the 401-window false-success nodes 50-54 at orchestrator.py:5225-5234, the 184MB node_created(0) spin guard at 1521-1526, the recall@50-vs-@100 stage-checker incident at eval_stages.py:304-315) — this materially lowers the cost of touching the code.
- Fail-closed static-analysis design in the stage-reuse predicate (eval_stages.py:_safe_reuse_start/_stage_reachable_files, 167-302) with explicit false-positive/false-negative direction analysis for every refusal branch.
- The single _emit_node_created emitter with the _OMIT sentinel (node_build.py:194-225) collapses four historical payload shapes into one function while provably preserving byte-identical event data per call site.
- Concurrency seams are typed and registry-guarded rather than ad hoc: BACKGROUND_APPENDABLE / SETUP_THREAD_APPENDABLE membership asserted at append sites (eval_dispatch.py:118-119) and the worker-side pause queue (_request_create_pause/_drain_create_pause, orchestrator.py:2519-2541) keeps run-global folded events on the main task.

#### ES-01 · HIGH · under-decomposition · effort: large

**orchestrator.py is still a god-module: three large separable subsystems remain inline after the 17-file mixin split**

*Locations:* `looplab/engine/orchestrator.py:141-310`, `looplab/engine/orchestrator.py:357-378`, `looplab/engine/orchestrator.py:853-1073`, `looplab/engine/orchestrator.py:2431-2517`, `looplab/engine/orchestrator.py:3761-4750`

*Evidence:* The mixin split (documented in CLAUDE.md) extracted the leaf clusters, but orchestrator.py still holds: (a) the speculation-gate calibration subsystem — ~170 lines of import-time profile construction/validation (141-310: _declared_settings_json_defaults, _SPECULATION_CALIBRATION_PROFILE_OVERRIDES, digest computation with module-level RuntimeError raises), plus ~220 lines of nested closures inside __init__ (_narrow_runtime_envelope_errors, _guard_calibrated_role_factory, 853-1073), plus the 87-line _require_pinned_speculation_receipt (2431-2517) — none of which touch the run loop; (b) the entire native-Card ledger (~1,000 lines, 3761-4750: _canonical_card_id, _engine_card_number, _card_id_ceiling, _card_statement, _card_action, _card_added_payload, _card_event_matches, _card_score_snapshot, _next_available_card_id, _plan_native_card, _reserve_node_build, _stage_prepared_card, _stage_card_creates, _card_claim_receipt_action, _prepare_existing_card_claim, _claim_existing_card_builds, _drop_card_once, _record_node_less_card, _mirror_hypothesis_card_merges) — a cohesive reservation/receipt subsystem larger than most existing mixins (eval_dispatch.py is 288 lines). The stated reason for keeping code in orchestrator.py (the module-global fold monkeypatch seam, comment at 3736-3741) names only _create_node/_rerun_node/_create_injected_node, not these clusters.

*Recommendation:* Extract (1) the calibration profile + envelope validation into looplab/search/speculation_calibration.py (where SPECULATION_CALIBRATION_SEEDS etc. already live) or a new engine/speculation_gate.py, and (2) the Card ledger into an engine/card_ledger.py mixin, following the established pattern. For the Card cluster's internal fold() calls, either import fold from the canonical home (as evaluate.py does, with the same docstring note that the orchestrator seam gates creation only) or route through a small engine hook, and verify the two fold-monkeypatching tests still intercept the paths they target.

#### ES-02 · HIGH · duplication · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**Three node-creation paths triplicate a ~70-line commit epilogue that has already forced the same fix to be applied three times**

*Resolution so far:* two of the epilogue's five stages are single-sourced. The developer-error
sentinel handling went through `node_build.developer_crash_records` (EC-03), and the telemetry
consumption is now `_consume_node_build_telemetry(node_id, generation, *, researcher, developer)`,
called by all three paths. The latter mattered more than its size suggests: it is three separate
emits with no shared name, so a path could quietly keep two and lose the third, and the telemetry
left behind is then attributed to the NEXT created node — which is exactly what
`_emit_role_telemetry` exists to prevent.

*Still open:* the parent-refetch guard, `_emit_node_created` and the landed-check. Those carry the
real per-path divergence (`materialize_abort`'s first-terminal branch, generation payloads,
`developer_called`) and the review's own §6 requires the extraction to read as parameterized
unification with the two fold-monkeypatch test files re-verified — a change that wants its own
pass rather than being appended to this one.

`tests/test_developer_crash_transaction.py` guards the resolved half: every creation path consumes
through the shared helper and none re-spells an individual emit, and the consume/discard pairing is
pinned. Verified to have teeth by making the inject path keep two emits and drop the third.

*Locations:* `looplab/engine/orchestrator.py:5173-5260`, `looplab/engine/orchestrator.py:5418-5463`, `looplab/engine/orchestrator.py:5604-5675`

*Evidence:* _create_node_scoped, _rerun_node and _create_injected_node each hand-code the same post-build sequence: re-fold and check parent generations -> _fail_reserved_build(error="parent lifecycle changed while building", reason="superseded") -> _emit_node_created -> re-fold and check the node landed ("... was rejected during replay") -> detect the "(developer error:" sentinel -> append node_failed(reason="developer_crash") + a pause -> consume role telemetry (_emit_agent_report/_emit_hypothesis_ranked/_emit_foresight_selected). The comment at 5655-5661 explicitly says the injected path "mirrors _create_node / _rerun_node ... the exact bug the two sibling create paths already fix" — i.e. the false-success sentinel guard was retrofitted into all three copies separately. The copies have also diverged mechanically: _create_node_scoped routes the pause through _request_create_pause (worker-seam-safe, 5251), while _rerun_node (5454) and _create_injected_node (5666) append EV_PAUSE directly.

*Recommendation:* Extract a shared `_commit_built_node(reservation, code, files, ..., pause_via_queue: bool)` helper covering parent-refetch guard, node_created emission, landed-check, developer-error sentinel handling and telemetry consumption; keep the three callers responsible only for how they obtained idea/code. This makes the next cross-cutting fix a one-site change.

#### ES-03 · MEDIUM · under-decomposition · effort: large

**_evaluate is a single ~700-line method mixing ten concerns in one attempt loop**

*Locations:* `looplab/engine/evaluate.py:114-823`, `looplab/engine/evaluate.py:280-697`, `looplab/engine/evaluate.py:718-823`

*Evidence:* The file's own docstring calls _evaluate "the engine's single largest method". Its body interleaves: pre-start lifecycle fencing, stale-reservation gating, proxy scoring, workdir manifest stamping (three nested closures 204-231), a 420-line while-loop (280-697) containing the intervention watcher closure (_intervention_seen/_watch, 306-382), two watchdog task spawns, GPU-pin failure terminalization with shielded cancel-scope commentary, stall salvage, dep auto-install rounds, anti-stuck signatures, mid-loop eval-budget refolds, the inline-repair pipeline with stage-reuse computation (579-696), and finally a 100-line terminal-emission block (718-823) that also runs three trust scans (reward-hack, leakage, critic). Each concern reads/writes loop-local state (attempt, dep_rounds, total_eval, stuck_sig, next_start, full_retrains, triage_outcome), which is why it has resisted decomposition.

*Recommendation:* Split along the existing seams without changing event order: (1) an _EvalAttempt dataclass owning the loop-local counters; (2) extract the watcher closures to methods (they only need node_id/generation/start_seq/cancel); (3) extract the trust-scan block (748-810) into a `_trust_scan_signals(node, res, workdir)` helper — it is already side-effect-free until the single append; (4) extract the inline-repair step (558-696). The one-terminal invariant stays in the residual ~150-line driver.

#### ES-04 · MEDIUM · excessive-logic · effort: medium

**Engine.__init__ is ~770 lines: 110 knob resolutions copied into locals then re-assigned to attributes, plus embedded calibration validation**

*Locations:* `looplab/engine/orchestrator.py:483-1252`, `looplab/engine/orchestrator.py:548-649`, `looplab/engine/orchestrator.py:853-1073`

*Evidence:* The constructor resolves every EngineOptions field into a local (`train_monitor = _opt("train_monitor")` ... ~100 consecutive lines, 548-649) purely so the assignment body below could stay textually identical to pre-EngineOptions code, then assigns each to self with per-knob normalization. Every new knob costs three edits in this file alone (an _opt line, an assignment, plus the EngineOptions field), contradicting the "adding a knob is TWO edits" comment at 519-521. On top of that, ~220 lines of speculation-gate validation closures and branching (853-1073) and GPU/broker/holdout/repo-spec wiring make the constructor the hardest-to-navigate span in the package. It also re-imports threading twice despite the module-level import (line 18 vs 742 `import threading as _threading` and 1486 in run()).

*Recommendation:* Collapse the local-variable indirection: iterate EngineOptions fields and setattr the trivial pass-through knobs from a table, keeping explicit code only for knobs with real normalization (about 30). Move the calibration-envelope closures out to module-level functions taking an explicit context (they already only read locals that are attributes or arguments). This is mechanical and preserves the keyword API the ~100 test call sites depend on.

#### ES-05 · MEDIUM · under-decomposition · effort: medium

**run() loop's `creates` branch inlines ~230 lines of parallel-build chunking, card-lane claiming and batch-drop bookkeeping**

*Locations:* `looplab/engine/orchestrator.py:1788-2018`, `looplab/engine/orchestrator.py:1880-1974`, `looplab/engine/orchestrator.py:2035-2038`

*Evidence:* The "§4 decomposition" comment (2035-2038) claims run() reads as "a table of guarded steps", and the terminal/budget gates mostly do — but the creates branch (1788-2018) is a 230-line inline block containing: the runaway counter arithmetic, the speculation receipt-owned/raw split, the parallel-build chunk loop (1880-1974) with per-chunk re-fold, batch proposal, telemetry zip, reservation, node-less-card drop recording, task-group fan-out and the pause circuit breaker, followed by the serial per-create loop (1975-2017) with its own card-reservation cancellation cascade. Four distinct `continue`-with-counter-adjustment exits (`_created_no_terminal -= ...` at 1809, 1828, 1866, 1870) make the control flow hard to trace.

*Recommendation:* Extract `_handle_create_actions(creates, state, ...) -> bool` (and within it `_run_parallel_build_chunks` and `_run_serial_creates`) as further §4 phase helpers, keeping every append and fold exactly in place, so the spine loop returns to gate-per-line readability.

#### ES-06 · MEDIUM · duplication · effort: medium

**Serial and parallel eval-dispatch branches triplicate the admission fence (terminal-gate + lifecycle_current + reservation release)**

*Locations:* `looplab/engine/orchestrator.py:3314-3384`, `looplab/engine/orchestrator.py:3513-3556`, `looplab/engine/orchestrator.py:3319-3333`

*Evidence:* The expression `bool(getattr(x, "paused", False) or getattr(x, "finished", False) or getattr(x, "stop_requested", None))` plus the 8-clause `lifecycle_current` check (live is not None, attempt == generation, status pending, not tombstoned, not aborted, not terminal, not over eval budget) plus the release-reservation-and-skip handling appears three times with cosmetic variation: twice in the serial wait loop (3319-3338 and 3354-3374) and once in the parallel admitted-recheck (3521-3553). The eval-budget guard `total_eval_seconds >= max_es` alone appears 8 times in this file (grep-confirmed: 1672, 3293, 3332, 3367, 3443, 3450, 3535 plus speculation.py:1488). A drift bug here (one copy missing a clause) would silently evaluate a superseded lifecycle or leak a GPU reservation.

*Recommendation:* Extract `_eval_admission_current(state, node, generation, max_es) -> bool` (and a `_release_and_skip(reservation)` helper) used by all three sites; the parallel branch's getattr-defensive variants can be folded in since RunState always has those attributes.

#### ES-07 · MEDIUM · duplication · effort: medium — **RESOLVED (2026-08-02)**

**Eight hand-rolled `for _attempt in range(64)` tail-CAS retry loops with no shared helper**

*Resolution:* `events/eventstore.py::retry_tail_cas(store, plan, *, attempts=64, on_exhaust)`
now owns the read-tail/append/retry loop and all eight sites call it; `grep 'range(64)'
looplab/engine/` returns nothing. The exhaustion policy each copy used to pick by accident is
a REQUIRED argument, so the three genuinely different behaviours (`_drop_card_once` raises,
the receipt appends return their own falsy value, the speculative commit distinguishes
created/closed/lost) are now stated rather than divergent. Pinned by three tests in
`tests/test_eventstore_cache.py`.

*Locations:* `looplab/engine/orchestrator.py:2183-2201`, `looplab/engine/orchestrator.py:2795-2821`, `looplab/engine/orchestrator.py:4163-4240`, `looplab/engine/orchestrator.py:4300-4350`, `looplab/engine/orchestrator.py:4646-4663`, `looplab/engine/speculation.py:523`, `looplab/engine/speculation.py:631`, `looplab/engine/speculation.py:694`

*Evidence:* Grep confirms eight identical-shaped loops: read_all -> check-already-done predicate -> compute tail seq -> append(expected_last_seq=tail) -> `except EventStoreConcurrencyError: continue`, capped at 64 attempts (_append_rung_promotion, _append_inject_failure, _reserve_node_build, _stage_prepared_card, _drop_card_once, plus three in speculation.py). 24 EventStoreConcurrencyError handling sites exist across the engine package. Each copy re-decides edge behavior independently (e.g. _drop_card_once raises RuntimeError on exhaustion at 4663 while _append_rung_promotion silently returns False at 2201), so the retry policy is inconsistent by accident rather than design.

*Recommendation:* Add one helper, e.g. `retry_tail_cas(store, plan: Callable[[events], Optional[AppendPlan]], attempts=64)` returning the appended event / None / raising per an explicit exhaustion policy, and port the eight loops. Behavior differences that survive review become explicit arguments instead of divergent copies.

#### ES-08 · MEDIUM · duplication · effort: small

**Batch-proposal scaffolding duplicated between run()'s parallel-build chunk and _stage_card_creates**

*Locations:* `looplab/engine/orchestrator.py:1893-1944`, `looplab/engine/orchestrator.py:4364-4446`

*Evidence:* Both sites run the same sequence around _propose_batch: call _propose_batch(state, n), read and pad `_pending_batch_telemetry` to align 1:1 with ideas, snapshot `_pending_batch_dropped`, iterate dropped entries calling _record_node_less_card(idea, reason=str(drop.get("reason") or "proposal_rejected")[:160], steering_context=...), and reset the three `_pending_batch_*` attributes. The run() copy (1893-1944) then reserves via _reserve_node_build; _stage_card_creates (4364-4446) stages via _stage_prepared_card — but the ~40 lines of mutable-attribute choreography (a fragile protocol with novelty.py's _propose_batch, which communicates results through 3 instance attributes) is copy-pasted, including the exact truncation constant and default reason string.

*Recommendation:* Extract a `_consume_batch_proposal(state, n) -> (ideas, telemetry, dropped)` helper that owns the _pending_batch_* attribute protocol (including the finally-reset), leaving only the commit strategy (reserve vs stage) at each call site. Longer term, make _propose_batch return a result object instead of signaling through instance attributes.

#### ES-09 · LOW · duplication · effort: small

**_apply_control_overrides contains two copy-pasted parallelism-override loops**

*Locations:* `looplab/engine/orchestrator.py:2697-2711`, `looplab/engine/orchestrator.py:2712-2726`

*Evidence:* Two 15-line for-loops over ("max_parallel", "eval_parallel") and ("parallel_build", "llm_parallel") are byte-identical except for the bound (0..1024 vs 0..64) and the target attribute (self._eval_parallel vs self._llm_parallel): same bool-exclusion, same float-integrality check, same int coercion, same try/except tuple.

*Recommendation:* One helper `_override_width(bo, keys, bound, current) -> int` called twice; the legacy-first/canonical-last ordering is preserved by the keys tuple.

#### ES-10 · LOW · inconsistency · effort: medium

**Four GPU-probe implementations with two different nvidia-smi parsers**

*Locations:* `looplab/engine/orchestrator.py:417-442`, `looplab/core/hardware.py:22-44`, `looplab/core/hardware.py:251`, `looplab/engine/resources.py:153-196`

*Evidence:* GPU discovery exists in four forms: orchestrator._detect_gpu_ids (CVD tokens -> torch.cuda.device_count -> counting `nvidia-smi -L` output lines), core/hardware.detect_gpus (nvidia-smi CSV query with comma-in-name repair), core/hardware.effective_gpu_inventory (ctypes CUDA Driver API with UUID/PCI identity), and core/hardware.detect_gpu (first GPU name, kept for back-compat). Two independent nvidia-smi invocation/parsing styles exist (`-L` line counting vs CSV query), and resources.detect_gpu_inventory even contains a defensive cross-check comment ("_detect_gpu_ids derives the same count. A mismatch means one of the probes changed...", resources.py:166-169) — evidence the duplication is a known hazard being papered over with a runtime consistency check.

*Recommendation:* Make core/hardware the single probe owner: _detect_gpu_ids's count should derive from detect_gpus() (falling back to torch), eliminating the `-L` parser and the cross-probe mismatch failure mode that detect_gpu_inventory currently guards against.

#### ES-11 · LOW · inconsistency · effort: small — **RESOLVED (2026-08-02)**

**"(developer error:" is a magic-string protocol checked by startswith at six consumer sites across three modules**

*Resolution:* `core/models.DEVELOPER_ERROR_PREFIX` + `is_developer_error(code)` are used by the
producer and all six consumers. `tests/test_developer_error_sentinel.py` source-scans every module
for a re-spelled literal (the definition site is the only permitted occurrence) and checks the
reverse direction too — a module that DROPS the import has stopped participating in the contract.

*Locations:* `looplab/adapters/repo_developer.py:913`, `looplab/engine/orchestrator.py:5230`, `looplab/engine/orchestrator.py:5450`, `looplab/engine/orchestrator.py:5662`, `looplab/engine/node_build.py:146`, `looplab/engine/speculation.py:191`

*Evidence:* The Developer-crash contract is the literal prefix "(developer error:" produced by repo_developer.py:913 (f-string) and consumed via `isinstance(code, str) and code.startswith("(developer error:")` at six sites (orchestrator x3, node_build._finalize_developer_footprint, speculation.py x2). There is no shared constant or `is_developer_error_sentinel(code)` predicate; a Developer backend that words its error differently (or a future i18n/format tweak) silently converts a crash into a false-success node — precisely the bug class the guard exists for (documented at orchestrator.py:5225-5229).

*Recommendation:* Define DEVELOPER_ERROR_PREFIX plus a predicate in a shared module (core/models or agents/roles, where the producer contract lives), use it at the producer and all six consumers, and add a registry-style test in the spirit of the existing seam guards.

#### ES-12 · LOW · other · effort: medium

**Redundant full-log folds within a single stable decision iteration**

*Locations:* `looplab/engine/orchestrator.py:1529-1547`, `looplab/engine/orchestrator.py:1693`, `looplab/engine/orchestrator.py:2747-2756`, `looplab/engine/orchestrator.py:3305-3313`

*Evidence:* Invariant #4 (always re-fold, never cache derived state) is documented design and correct — but the spine folds the same unchanged snapshot repeatedly within one iteration: fold(decision_events) at 1530, a fold inside _mirror_hypothesis_card_merges (4750) even when nothing was written is avoided, but the speculation path folds again at 1693 and 1729, and _run_cadences' sub-steps each re-read/fold. The code itself names the O(total-events) busy-poll cost class three separate times (_defer_for_node_budget 2747-2752 added geometric backoff for it; the serial resource-wait comment 3305-3313 says "gate the re-fold on the tail seq having changed"; the parallel branch folds twice per admission). The mitigations are ad hoc per-site rather than a shared mechanism.

*Recommendation:* Introduce a tail-seq-keyed fold memo on the EventStore or Engine (`fold_cached(events)` returning the previous RunState when the last seq and object identity match, mirroring _ack_commands' existing cursor/identity technique at 1278-1292). This preserves invariant #4 semantics exactly (any append invalidates) while removing the repeated O(n) folds that the comments repeatedly apologize for.

#### ES-13 · LOW · excessive-logic · effort: medium

**Delegator/seam boilerplate has drifted into four coexisting styles (~50 forwarding members in orchestrator.py)**

*Locations:* `looplab/engine/orchestrator.py:1254-1268`, `looplab/engine/orchestrator.py:3613-3728`, `looplab/engine/orchestrator.py:5692-5861`, `looplab/engine/orchestrator.py:460-481`

*Evidence:* The lessons/holdout/workspace extraction pattern (documented in CLAUDE.md as an intentional monkeypatch seam) is implemented four different ways within one file: property pairs with setters (_lessons_seen_stamp, _prior_note_text, 3613-3635), plain one-line delegators (~30 of them for lessons/holdout/workspace, 3637-3728 and 5692-5861), staticmethod aliases (`_spent_pairs = staticmethod(LessonMemory.spent_pairs)`, 3665, 3699-3700), and read-through property shims for deprecated names (max_parallel/parallel_build, 460-481). This is documented design, but the argument that it still costs: every new LessonMemory/HoldoutGrader method requires a hand-written forwarder (with the correct @in_llm_lane decorator — eleven delegators carry it, easy to forget), and the four styles mean a reader must check per-name which forwarding semantics apply. The same monkeypatch-interception guarantee could be provided by one mechanism.

*Recommendation:* Pick one forwarding style per sub-object and generate the delegators from a small name registry (a class-body loop or a tested __getattr__ with an explicit allow-list mirroring the existing registry-test pattern), including lane decoration as registry data. Keep the two deprecated-width properties as-is (they carry real semantics).

#### ES-14 · LOW · layering · effort: small

**Mixin-boundary misplacements: governance and generic helpers live in unrelated clusters**

*Locations:* `looplab/engine/eval_dispatch.py:33-47`, `looplab/engine/action_governance.py:1-33`, `looplab/engine/orchestrator.py:3585-3592`, `looplab/engine/orchestrator.py:3724-3729`

*Evidence:* The 17-file split is mostly cohesive, but a few members landed by history rather than concern: _agent_may — the agent-governance gate consulted by the strategist/boss/researcher seams — lives in EvalDispatchMixin (eval_dispatch.py:33-47) while its sibling helper effective_researcher_eval_timeout lives in the separate 33-line engine/action_governance.py; the generic _op_span trace helper (used by strategist/research/lessons clusters) and the shared _cadence_due gate stay in orchestrator.py (3585-3592, 3724-3729) with comments explaining they are shared — i.e. they belong to no cluster, which is the symptom. This supports the ticket's question: the split genuinely separates the big concerns, but the residual shared helpers show the mixin scheme lacks a home for cross-cluster utilities, so they default back into the god-module.

*Recommendation:* Create a tiny engine/_shared.py (or fold into action_governance.py renamed engine/governance.py) for _agent_may + effective_researcher_eval_timeout, and move _op_span/_cadence_due there; keeps orchestrator.py shrinking monotonically.


### 4.2 Engine — cadence / monitoring / wrap-up mixins

Scope: `looplab/engine/`: strategy, research_cadence, novelty, speculation, ablation, confirm_phase, audit, resources, proposal_cues, train_monitor, asha_monitor, finalize, costs, signal_delivery.

**Reviewer assessment.** This cluster implements the engine's periodic/advisory subsystems as mixins over one Engine object, with a consistently applied replay-safety discipline (at_node idempotence gates, paid-attempt receipts before provider calls, fold-ignored DIAGNOSTIC events for background monitors). The architecture is deliberate and mostly well-documented, but the cluster has accreted heavy near-duplication across sibling features that grew independently: two ~190-line cross-run context builders, two watchdog monitors with copy-pasted resume/loop scaffolding, two ablation paths sharing a near-identical ~45-line tail, a developer-crash terminal+pause pair spelled five times, and a triplicated durable-usage append/verify protocol. speculation.py's 500-line _run_card_session and strategy.py's multi-subsystem sprawl are the main under-decomposition hot spots; speculation.py carries embedded reviewer comments acknowledging its unfixed perf/structure debt.

**Strengths worth preserving:**

- The signal-delivery registry (signal_delivery.py SIGNALS) and its source-scan test turn the classic 'signal folded but no longer injected' regression into a red test — the same registry discipline CLAUDE.md documents for other duck-typed seams, applied consistently here.
- Watchdog logic is factored into pure, unit-testable functions (training_log_digest, next_monitor_sleep, should_monitor_kill, asha_underperforming, extract_resource_curve) with the impure loop kept thin, and the shared claim_watchdog_kill correctly serializes the two monitors' kill race on the cooperative loop.
- Replay/crash-safety discipline is coherent across the cluster: at_node idempotence gates, paid-attempt receipts claimed BEFORE provider calls (research attempts, card_build_attempted, _claim_paid_finalize_step), and finalize.py's dual scope/finish_seq handshake all follow one recognizable pattern.
- Known gaps are honestly annotated with bounds and a closing recipe (e.g. the in-memory _last_hyp_merge_n cadence gap in research_cadence.py:539-548, the concept-snapshot re-purchase gap in strategy.py:703-711), which makes review and future fixes far cheaper than silent debt.
- Fail-closed engineering in resources.py (lease inode/symlink validation, count-only degradation when the memory inventory can't be joined losslessly) and costs.py (self-authenticating outbox records, never erasing conflicting evidence) is thorough and clearly reasoned inline.

#### EC-01 · HIGH · duplication · effort: medium

**Two ~190-line near-duplicate cross-run context builders (Strategist note vs Researcher advisory)**

*Locations:* `looplab/engine/strategy.py:139`, `looplab/engine/strategy.py:327`, `looplab/engine/proposal_cues.py:332`, `looplab/engine/proposal_cues.py:518`

*Evidence:* _cross_run_note_for_ctx (strategy.py:139-335) and _cross_run_advisory_text (proposal_cues.py:332-528) implement the same pipeline in parallel: gate on _cross_run_advisory + memory_dir; valid_live_direction check; the identical governance re-entry idiom (`if _governance is None: return project_governed_sources(base, lambda governance: self.<method>(state, _governance=governance), include_concepts=True, source_names=('concept_capsules.jsonl','lessons.jsonl','research_claims.jsonl'))`); load_claim_lessons/ConceptCapsuleStore/load_research_claims with observed_path_missing guard; row filtering by direction/task_id/excluded run_id; a v2 receipt dict with identical keys (scope_task, excluded_run, n_lessons, n_capsules, n_research, corpus_digest, render_digest built via sanitize_cross_run_projection + sha256); identical GovernanceLedgerUnavailable handler emitting {'v':2,'status':'unavailable','complete':False,'governance':exc.public_receipt()}; identical bare-except -> empty receipt + "". Only the middle (atlas summary vs context pack rendering) differs.

*Recommendation:* Extract a shared helper (e.g. engine/cross_run_context.py) that owns: the flag/direction gating, the governed source load + row scoping, and receipt construction (one build_receipt(scope_task, excluded_run, counts, corpus, rendered) function plus the unavailable/empty receipt shapes). Each caller keeps only its distinct projection/rendering middle section. This removes ~250 duplicated lines and, more importantly, prevents the two receipt schemas and scoping rules from drifting.

#### EC-02 · HIGH · under-decomposition · effort: large

**_run_card_session is a ~500-line async god-function with acknowledged perf debt**

*Locations:* `looplab/engine/speculation.py:1467`, `looplab/engine/speculation.py:1618`, `looplab/engine/speculation.py:1516`, `looplab/engine/speculation.py:1640`, `looplab/engine/speculation.py:1926`

*Evidence:* _run_card_session spans speculation.py:1467-1968: a while-True loop with two nested closures (_start_head_producer, _eval_one), and the triple gate recomputation `outer_rebuild/terminal_gate/budget_exhausted = ...` copy-pasted four times per iteration (lines 1640-1644, 1703-1708, 1824-1829, 1926-1932). One idle turn performs ~8 full `fold(self.store.read_all())` rebuilds; the code itself carries embedded review comments admitting this ('CODEX AGENT: one idle polling turn performs this full replay plus several more below... Cache one snapshot per observed tail', line 1618-1621) and a head-of-line-blocking fence issue (line 1516-1520). The whole file is 1968 lines for one feature.

*Recommendation:* Decompose the loop body into named phase methods (recover_sentinels, serve_raw_stage, drop_stale, serve_head, admit_evals, decide_exit) that each take and return one folded snapshot, so the state is folded once per turn and the exit-gate predicate exists in exactly one place. The two admitted-vs-terminal gate tuples should be one small dataclass computed once per fold. Then address the acknowledged fold-caching TODO.

#### EC-03 · HIGH · duplication · effort: small — **RESOLVED (2026-08-02)**

**Developer-crash terminal+pause event pair duplicated five times**

*Resolution:* `engine/node_build.py::developer_crash_records(node_id, generation, code,
pause_reason, *, terminal=True)` is the one spelling of the RECORDS — event types, order (terminal
first: a pause naming a node with no terminal reads as an operator freeze), field names and
defaults. All five sites build from it. `terminal=False` serves the recovery branch of
`_close_developer_sentinel_once`, where the node is already terminal and a second one would break
the one-terminal-per-node invariant.

The per-site APPEND discipline is deliberately NOT unified, because the differences are
load-bearing: the speculation sites append under one tail CAS, the fan-out queues its pause via
`_request_create_pause` for the main task (a worker-written EV_PAUSE races EV_RESUME for byte
position — CLAUDE.md invariant #1), and the two serial sites append sequentially. Doc 25 itself
marks unifying them onto CAS as a separate decision item, not preservation.

Pause wording stays each caller's — the sites describe genuinely different situations and an
operator reading the pause needs to know which — while everything a replay reads is fixed.

`tests/test_developer_crash_transaction.py` pins the record shape, the terminal-first order, the
pause/terminal identity match, the pause-only recovery mode, a source guard against a re-spelled
terminal (itself pinned against false negatives), and the worker-seam rule. Verified to have teeth:
reordering the pair fails two cases, and making the fan-out append its own EV_PAUSE fails the
worker-seam case.

*Locations:* `looplab/engine/speculation.py:644`, `looplab/engine/speculation.py:1351`, `looplab/engine/orchestrator.py:5233`, `looplab/engine/orchestrator.py:5453`, `looplab/engine/orchestrator.py:5665`

*Evidence:* The invariant-critical developer-crash transaction (EV_NODE_FAILED with reason='developer_crash' plus the EV_PAUSE circuit-breaker — appended via append_many under a tail CAS at the two speculation.py sites, as plain sequential appends at the two orchestrator sites 5453/5665, and as terminal + `_request_create_pause` at 5233 where the MAIN task appends the pause after the join) is hand-spelled in five places: speculation.py:644-659 (_create_precoded_node), speculation.py:1351-1364 and 1390-1396 (_close_developer_sentinel_once), and three orchestrator sites (5233/5453/5665). The pause reason strings already drift ('a Developer session crashed (LLM unreachable ...)', 'recovered a Developer crash before GPU dispatch', 'recovered a terminal Developer crash', 'crashed while building an injected node'). The CLAUDE.md invariant that a worker-written EV_PAUSE races EV_RESUME makes each copy a place where the transaction discipline can silently be gotten wrong.

*Recommendation:* Extract one `_developer_crash_records(node_id, generation, code, reason_text)` builder (or a full `_append_developer_crash(...)` helper with the tail-CAS retry) used by all five sites. Reason wording can stay a parameter; the event pair, ordering, and CAS discipline become single-sourced.

#### EC-04 · MEDIUM · mergeable-entities · effort: medium

**train_monitor and asha_monitor duplicate the resume-history scan and per-tick loop scaffold**

*Locations:* `looplab/engine/train_monitor.py:433`, `looplab/engine/asha_monitor.py:341`, `looplab/engine/train_monitor.py:451`, `looplab/engine/asha_monitor.py:365`

*Evidence:* The two watchdogs are documented siblings and already share claim_watchdog_kill/read_training_tail/cadence — good — but each hand-rolls: (a) an ~20-line resume scan (`prior_rows = await anyio.to_thread.run_sync(self.store.read_all, limiter=_watch_limiter())` then reversed() scan for its own event type with identical `isinstance(data.get('node_id'), int) and not isinstance(..., bool) and == node_id` and generation checks — train_monitor.py:433-449 vs asha_monitor.py:341-363); and (b) an identical per-tick skeleton (`while True: await anyio.sleep(...); if cancel.is_set(): return; try: tail = await to_thread(...); ... except cancelled: raise; except Exception: continue` — train_monitor.py:451-536 vs asha_monitor.py:365-458). The bool-guarded field validation idiom is copied verbatim.

*Recommendation:* Add two tiny shared helpers in train_monitor.py (which asha_monitor already imports from): `last_diagnostic_row(events, event_type, node_id, generation)` for the resume scan, and optionally a `watchdog_tick_loop(cadence, cancel, tick_fn)` scaffold. Full merger of the two monitors is NOT recommended (one is LLM-judged health, the other metric-rank; the split is documented), but the mechanical scaffolding should be single-sourced.

#### EC-05 · MEDIUM · duplication · effort: small

**Novelty reject/repropose/audit block duplicated between LLM and semantic gates**

*Locations:* `looplab/engine/novelty.py:440`, `looplab/engine/novelty.py:1224`

*Evidence:* _llm_novelty_gate (novelty.py:440-466) and the semantic branch of _apply_novelty_gate (novelty.py:1224-1252) repeat the same ~25-line sequence: build the duplicate-outcome string (`it FAILED ({dup.error_reason})` vs `it scored {dup.metric}`), capture original + idea_proposal_digest, call _repropose_with_feedback with a NOVELTY GATE hint, on BudgetExceeded append EV_NOVELTY_REJECTED with action='budget_exceeded' and re-raise, then compute action='reproposed'/'kept' from digest comparison and append EV_NOVELTY_REJECTED with kind='llm' vs kind='semantic'. Only the hint wording and one payload key (reason vs similarity) differ. _repropose_with_feedback itself is already shared, so this is the remaining un-extracted half.

*Recommendation:* Extract `_reject_and_repropose(state, idea, dup, kind, hint, extra_payload, repropose, researcher, prospective_node_id)` that owns the digest/action/audit/budget-exceeded protocol; both gates pass their kind-specific hint and payload fields.

#### EC-06 · MEDIUM · duplication · effort: medium

**_ablate and _ablate_code share a verbatim ~55-line refine_block child-construction tail**

*Locations:* `looplab/engine/ablation.py:145`, `looplab/engine/ablation.py:290`, `looplab/engine/ablation.py:104`, `looplab/engine/ablation.py:247`

*Evidence:* After their (also structurally parallel) probe loops, both methods repeat the same tail verbatim: _reserve_node_build with kind='refine_block' + parent_generations, None-check -> _discard_node_build_telemetry, idea = reservation.idea.model_copy(deep=True), _reset_developer_footprint, `self._implement(self._directed_idea(idea.model_copy(deep=True), state), parent)`, _finalize_developer_footprint, `_ablation_parent_current` re-check -> _fail_reserved_build('parent lifecycle changed while building'), _emit_node_created(operator='refine_block', ...), fold-membership check -> _fail_reserved_build('ablation node creation was rejected during replay'), then _emit_agent_report/_emit_hypothesis_ranked/_emit_foresight_selected (ablation.py:158-204 vs 306-346). The probe loops also duplicate the workdir naming, wall-clock accumulation, and superseded-flag dance (104-132 vs 254-274).

*Recommendation:* Extract `_build_refine_block_child(parent_id, generation, idea, state)` for the shared tail, and a `_probe(ablated_code_or_idea, workdir_suffix)` helper for the timed lifecycle-checked probe. The two entry points keep only their distinct impact computation and Idea construction.

#### EC-07 · MEDIUM · inconsistency · effort: small

**Strategist/concept cadence uses modulo gating that its sibling cadence explicitly fixed as a bug**

*Locations:* `looplab/engine/strategy.py:364`, `looplab/engine/strategy.py:380`, `looplab/engine/research_cadence.py:78`, `looplab/engine/orchestrator.py:3725`

*Evidence:* Three cadence idioms coexist: (1) _should_consult / _should_consult_concepts use `n % every == 0` (strategy.py:352-380); (2) deep research and reports use the since-last `_cadence_due(n, last, every)` gate, whose comment explicitly documents WHY modulo is wrong: 'a rung-0/seed batch that jumps the node count by k>1 must not step over the only multiple and skip the whole window' (research_cadence.py:78-83); (3) hypothesis merge uses an in-memory grown-by-2 baseline (research_cadence.py:549, documented KNOWN GAP). Under llm_parallel>1 the node count advances in batch-width strides, so with e.g. width 4 and strategist_every=5 the modulo can miss every multiple — starving the Strategist consult, coverage snapshots, AND the concept re-tag cadence — the exact failure class the research cadence was patched for.

*Recommendation:* Convert _should_consult/_should_consult_concepts to the same since-last _cadence_due pattern (last consult/snapshot at_node is already durable in strategy_history / coverage_snapshots), or document concretely why strategist starvation under batched builds is acceptable. Either way, one cadence idiom should be canonical.

#### EC-08 · MEDIUM · flat-code · effort: medium

**_set_complexity_hint is a 255-line linear cue assembler**

*Locations:* `looplab/engine/proposal_cues.py:58`, `looplab/engine/proposal_cues.py:313`

*Evidence:* One method (proposal_cues.py:58-313) linearly concatenates ~14 independent cue blocks (complexity, budget, time-budget, GPU contract, failure reflection, watchdog reflection, trust reflection, fault localization, feature engineering, prior note, research memo ref, cross-run advisory, cross-run pointer, concept authoring, concept slug reuse, sweep, novelty stance), each doing `hint += ...` plus `steering.append({...})` plus per-attr try/setattr. Every new signal grows this one function; the repo already has a registry pattern for exactly this shape (signal_delivery.SIGNALS).

*Recommendation:* Split each cue into a small method (or a list of (gate, render) callables) returning (hint_fragment, steering_entries); _set_complexity_hint becomes a loop that concatenates fragments and stamps the researcher once. This keeps byte-identical output while making cue addition/removal local, and lets test_signal_delivery reference cue functions instead of substrings.

#### EC-09 · MEDIUM · under-decomposition · effort: medium

**strategy.py mixes four loosely-related subsystems; _concept_coverage_snapshot alone is ~240 lines**

*Locations:* `looplab/engine/strategy.py:776`, `looplab/engine/strategy.py:1014`, `looplab/engine/strategy.py:1017`, `looplab/engine/strategy.py:634`, `looplab/engine/strategy.py:698`, `looplab/engine/strategy.py:717`

*Evidence:* The 'strategist cadence' mixin (1269 lines) actually contains: the consult/apply machinery, the cross-run note, coverage snapshots, the whole concept subsystem (classifier tagging, consolidation, edge assertion, hypothesis tagging, run-base seeding — with _concept_coverage_snapshot one 240-line method at 776-1014 nesting 4 try blocks), and the R1-c verifier tie-break (1017-1154), which is selection machinery, not strategist cadence. The at_node+projection-token idempotence predicate is also spelled three times (_already_covered_at at 634-638, and inline twice at 698-701 and 717-721) instead of once parametrized by snapshot list. Every other engine subsystem of this size got its own mixin file per the documented seventeen-file convention.

*Recommendation:* Split a ConceptCadenceMixin (concept snapshot/tagging/consolidation/run-base) and move _maybe_verify_ties/_verifier_soundness to their own small mixin (or eval_stages), leaving strategy.py the consult/apply/coverage core. Parametrize the at_node-idempotence predicate over the snapshot list. Break _concept_coverage_snapshot into tag-refresh / consolidation / edges / hypothesis-tag / coverage-summary steps.

#### EC-10 · MEDIUM · duplication · effort: medium

**Durable-usage append/verify/acknowledge protocol implemented three times in costs.py**

*Locations:* `looplab/engine/costs.py:426`, `looplab/engine/costs.py:317`, `looplab/engine/costs.py:556`

*Evidence:* The subtle sequence 'append EV_LLM_USAGE(_payload(usage_id, clean)); on exception re-read and check _event_usage_deltas(...)[usage_id]==clean; on success verify or trust; then _record + pending.pop + _forget_outbox (with the durable-but-unacknowledged conflict case)' exists in three near-copies: the sink closure inside bind_cost_accountants (costs.py:426-459), _drain_outbox (317-351), and the per-binding retry loop in reconcile_cost_accountants (556-591). Each copy makes slightly different verification choices (the sink trusts a successful append per the PERF-1 comment; the other two always rescan), so the protocol's correctness argument must be re-derived per site.

*Recommendation:* Extract one `_commit_usage_delta(engine, usage_id, clean, persisted_cache, *, trust_success)` returning (durable, ack_ok) and have all three sites call it. The intentional verification differences become one explicit flag instead of three divergent code paths.

#### EC-11 · LOW · flat-code · effort: small

**Twin ~18-line parallelism validation loops and a 200-line knob if-chain in _apply_strategy**

*Locations:* `looplab/engine/strategy.py:507`, `looplab/engine/strategy.py:526`, `looplab/engine/strategy.py:433`

*Evidence:* _apply_strategy (strategy.py:433-632) is a flat per-knob if-chain; its two concurrency loops (507-521 for max_parallel/eval_parallel, 526-544 for parallel_build/llm_parallel) are byte-near-identical: bool guard, non-integer-float guard, int() with bounds (0..1024 vs 0..64), max(1, value) assignment — differing only in the target attr, the bound, and the broker reconfigure call. The ops sub-dict block (479-489) repeats `if k in ops and may(k): self._attr = cast(ops[k])` five times.

*Recommendation:* Extract a `_settle_width(raw, upper)` -> Optional[int] validator used by both loops, and a small table for the ops knobs ((key, attr, cast)). The governance-sensitive policy/developer sections can stay explicit.

#### EC-12 · LOW · mergeable-entities · effort: small

**Mirrored producer pipelines: SpecBuildResult vs SpecRawStageResult async wrappers duplicate scaffolding**

*Locations:* `looplab/engine/speculation.py:1143`, `looplab/engine/speculation.py:1248`, `looplab/engine/speculation.py:51`, `looplab/engine/speculation.py:73`

*Evidence:* _produce_card_build (1143-1179) and _produce_raw_card_stage (1248-1299) repeat the same wrapper: to_thread.run_sync(functools.partial(worker,...), abandon_on_cancel=False); except Exception -> synthesize a failure result with `f"{type(exc).__name__}: {exc}"[:2_048]`; store the result on self; finally clear the inflight flag and `notify.send_nowait(...)` swallowing (WouldBlock, ClosedResourceError, BrokenResourceError). The failure-result construction (10–11 keyword fields against a 14-field dataclass) is itself duplicated inside _produce_raw_card_stage's except and _prepare_raw_card_stage's except (1231-1244 vs 1281-1292).

*Recommendation:* Extract a generic `_run_isolated_producer(worker, on_result, inflight_clear, notify_key)` coroutine, and one `SpecRawStageResult.failure(...)` classmethod so the 12-field failure payload is built in one place.

#### EC-13 · LOW · duplication · effort: small

**Parser-resolution wrapper-chain walk duplicated outside its canonical helper**

*Locations:* `looplab/engine/strategy.py:1121`, `looplab/engine/novelty.py:780`, `looplab/engine/lessons_distill.py:339`, `looplab/engine/lessons.py:284`

*Evidence:* The idiom `next((p for o in (researcher, getattr(r,'inner',None), getattr(r,'fallback',None), developer) if (p := getattr(o,'parser',None))), 'tool_call')` appears in _verifier_soundness (strategy.py:1121-1123) and _verified_failed_direction_reopen (novelty.py:780-785); the canonical spelling is _merge_prompt_opts itself (lessons_distill.py:331-342), which research_cadence.py:564-566 correctly delegates to — so two hand-rolled copies bypass the intended single lookup path. lessons.py:280-289 (reflect_client) walks the same researcher→inner→fallback→developer chain but resolves the LLM *client* rather than the parser — a third variant of the chain-walk idiom. Given the codebase's own warning about duck-typed wrapper chains (foresight __getattr__ proxy trap), each copy is a chance to miss a wrapper link.

*Recommendation:* Add `resolve_role_parser(*roles, default='tool_call')` next to the existing chain-walk in lessons.py (or agents/roles.py) and use it at all four sites.

#### EC-14 · LOW · dead-code · effort: small

**_acquire_gpu/_release_gpu are production-dead, kept only for tests**

*Locations:* `looplab/engine/resources.py:440`, `looplab/engine/resources.py:446`

*Evidence:* Repo-wide grep shows the single-GPU primitives are called only from tests/test_strategist.py:1231-1239; the in-tree comment admits 'The dispatcher itself uses the multi-GPU API and never relies on this non-blocking wrapper', and the historical call site the docs reference (evaluate.py::_acquire_gpu per command_eval.py:786 and docs/22) no longer exists. docs/23:1029 even lists replacing them as a planned step.

*Recommendation:* Port the two test call sites to _acquire_gpus/_release_gpus and delete the wrappers (or, if kept deliberately, move the assertions they support into a test helper). Low cost, removes a second API surface for the same pool.

#### EC-15 · LOW · excessive-logic · effort: small

**Acknowledged unfixed hot-path cost: invalid operator pin defeats the strategist short-circuit**

*Locations:* `looplab/engine/strategy.py:1194`, `looplab/engine/strategy.py:1202`

*Evidence:* An in-code review note (strategy.py:1194-1201, 'CLAUDE REVIEW: [PERF]') documents that a persistently-invalid set_strategy pin keeps pin_drift True forever because drift is computed from the raw (pre-validation) pin, so _strategy_ctx — O(nodes) operator_yields plus cross-run memory I/O under a governance lock when cross_run_advisory is on — runs on EVERY loop pass instead of only at the strategist cadence. The comment names the fix implicitly (validate before the cheap short-circuit) but the code was left as-is.

*Recommendation:* Cache the validated pin (keyed on the pending_strategy dict identity/digest) so a pin that validates to empty pin_fields clears pin_drift in the cheap pre-check; the note can then be deleted.


*Status (post-baseline):* Fixed on `master` by commit `c92b89f` (2026-08-01, immediately after this review's baseline): an `_invalid_pin_verdict` memo keyed on (pin JSON, card_driven_selection, policy registry) now short-circuits before `_strategy_ctx`, and the marker was rewritten as a past-tense why-comment — essentially this finding's own recommendation. The finding is retained as accurate at the baseline.

### 4.3 Engine — cross-run memory & knowledge

Scope: `looplab/engine/`: memory.py, lessons.py, claims.py, concept_registry.py, claim_key.py, stewards, governance_health.py, cross_run_index.py, task_facets.py, steward_invocation.py.

**Reviewer assessment.** The cross-run memory subsystem is functionally impressive — deterministic projections, fail-closed health receipts, careful lock ordering, and at-most-once paid-LLM protocols — but it has accreted into two god-modules (claims.py at 2896 lines spanning six distinct subsystems; memory.py at 1600 lines spanning five) and carries substantial structural duplication: three copy-pasted ~90-line steward drivers in lessons.py, two parallel governance-append implementations, two parallel at-most-once curation protocols, three coexisting claim-identity systems, and duplicated durable key-derivation that must stay bit-identical across modules. The receipt/health machinery, while deliberately paranoid, is implemented ad hoc roughly eight times with no shared validator abstraction, and the receipt-carrying list subclasses exist in three parallel flavors. Most individual pieces are well-commented and testable; the maintenance cost is concentrated in the module boundaries, not the logic.

**Strengths worth preserving:**

- Fail-closed evidence-health discipline is applied uniformly and honestly: quarantined/malformed rows are never laundered into 'exact absence', every bounded projection carries explicit omission counts, and receipts (source_complete, producer receipts, snapshot digests) flow end-to-end from durable stores to prompts — with warnings actually rendered to the agent (render_context_pack).
- Concurrency and lock-order discipline is exemplary: project_governed_sources centralizes the concept-global → claim-ledger → sorted-source lock order, every mutable shared store re-reads inside its interprocess lock, paid-LLM passes deliberately run unlocked with compare-and-swap tokens (LessonMemory.consolidate_lessons_file), and the reasoning is documented inline at each site.
- The at-most-once paid-invocation protocols (durable begun/terminal rows, input-digest-bound curation keys, crash-ambiguity handled as 'prior_attempt_incomplete_not_replayed') are rigorous against double-charging and replay divergence — rare care for LLM-cost correctness.
- cross_run_index.py is a model module: pure deterministic projection, byte-identical rebuild guarantee, receipted incremental cache with explicit skip reasons, TOCTOU fences on digest/read, and honest degraded-provenance notes.
- Load-bearing why-comments throughout (replay-safety notes, mega-review/CR references, named past bugs with their observed symptoms) make otherwise-subtle invariants auditable, and the mixin decomposition of LessonMemory (priors/distill/reconcile) preserved test monkeypatch seams with zero call-site churn.

#### EM-01 · HIGH · under-decomposition · effort: large — **RESOLVED (2026-08-02)**

**claims.py is a 2896-line god-module spanning six distinct subsystems** — now 843 lines across four

*Locations:* `looplab/engine/claims.py:81`, `looplab/engine/claims.py:965`, `looplab/engine/claims.py:1401`, `looplab/engine/claims.py:1726`, `looplab/engine/claims.py:2115`, `looplab/engine/claims.py:2436`, `looplab/engine/claims.py:2719`

*Evidence:* One module contains: (1) source-row validation + read-health receipts (~lines 81-460: _ClaimSourceRows, _valid_claim_source_row ~100 lines, _safe_* validators); (2) the operator claim-decision governance ledger (965-1385: record_claim_decision, record_observed_claim_decision, load_claim_decisions with its own CAS/idempotency/locking); (3) the durable D8 research_claims.jsonl store (1401-1575: record_research_claims ~135 lines, load_research_claims); (4) three claim-assessment projections (1726-2113: _fuzzy_merge_claims, _structured_assessments ~175 lines, claim_assessments); (5) the context pack + CR2a retrieval planner (2115-2716: build_context_pack ~145 lines, cross_run_retrieve ~280 lines including inline scope-receipt validation, intent classification, quota-swap and receipt assembly); (6) portfolio_atlas + prompt rendering (2719-2896). These interact through module-private helpers, so any change forces navigating all six. The module docstring itself acknowledges it 'owns the durable store, governance decisions, health-aware readers and live API/prompt consumers'.

*Recommendation:* Split along the already-visible seams: claims_health.py (row validation + read-health + receipt validators), claims_ledger.py (decision governance writes/replay), research_claims_store.py (D8 persistence), claims_assessments.py (the three projections), claims_retrieval.py (context pack + cross_run_retrieve + atlas + render). Each section is already comment-delimited; the split is mechanical and the back-compat import shim pattern (looplab/__init__.py _LAYOUT) already exists for exactly this.

*Resolution (2026-08-02, first module):* `engine/claims_health.py` — 970 lines, 60 names — is
extracted, and `claims.py` drops from 2,846 to 1,988. `claims.py` re-exports every name (the
`llm.py`/`agent.py` barrel), `_LAYOUT` carries the new module, and the private names `tools/`,
`cli/` and `serve/` import by their historical `engine.claims` path are unmoved.

The sections are NOT a clean DAG — an AST sweep found seven back-edges. Six are calls a later
section makes into an earlier one (fine within a module, deferred imports across). The seventh was
`_MAX_DECISION_METRIC`, declared in the governance section but read by the leaf as well as by two
sections above it; it moved down with the other bounds, which is what makes the leaf self-contained.

Guarded in `tests/test_claims.py`: the leaf may not import back into the subsystem (AST, so a
DEFERRED back-import is caught too — that one would not even raise), the barrel must re-export the
SAME objects rather than copies (a copy silently defeats monkeypatching through either path), and
the cross-package private names must still resolve. Verified to have teeth against all three.

*Resolution (2026-08-02, second module):* `engine/claims_retrieval.py` — 852 lines — is extracted
as the leaf's counterpart at the TOP of the subsystem: the context pack, the CR2a retrieval planner
and the portfolio atlas, i.e. everything that decides what a proposing agent SEES. That separation
is the point of doing this one next: retrieval quotas, the reserved caveat slot and the atlas are
selection-shaping policy, and they previously sat in the same file as the durable store that decides
what is TRUE. `claims.py` drops to 1,236 lines (from 2,846 before the split began).

The direction is inverted from `claims_health`'s: `claims.py` imports THIS module to re-export it,
so the one legal form for the five functions that need the ledger/store half (`build_context_pack`,
`_classify_intent`, `_retrieval_tokens`, `cross_run_retrieve`, `portfolio_atlas`) is a
function-local import. A module-level one is an import cycle at startup, and is guarded.

One trap is worth recording because it cost a debugging pass and is invisible to every ordinary
check: the module first took its leaf dependencies via `from claims_health import *`. Every name it
needs from the leaf is PRIVATE, and `import *` skips underscore names — so the module imported
cleanly, collected cleanly, and raised `NameError: _MAX_CONTEXT_CLAIMS` the first time a context
pack was actually built. A wildcard is now banned here by test, and a second guard walks the
module's compiled code objects (excluding `co_varnames`/`co_cellvars`/`co_freevars`, so a deferred
import still reads as bound) to catch any private global that resolves nowhere.

*Resolution (2026-08-02, third module):* `engine/claims_assessments.py` — 439 lines — carries the
three projections that fold the two independent durable stores (lesson outcomes, D8 research claims)
into one epistemic view: `supported` / `refuted` / `mixed` / `inconclusive` with the run and node
ids behind each. `contested` is only reachable *because* a research claim can oppose a lesson
verdict, which is the reason the two stores are read together here rather than separately. It sits
between the leaf and the planner — reads `claims_health`, is read by `claims_retrieval` through the
barrel — and takes the ledger's two legacy overlay-key helpers by deferred import.

Three helpers moved DOWN into the leaf as part of this, on the same rule that moved
`_MAX_DECISION_METRIC`: `_CLAIM_WORD`, `_string_list` and `_MAX_DECISION_SCOPE` are each read by
three of the four modules, so leaving them where they were first declared cost every consumer a
deferred import back into `claims.py`. `_CLAIM_WORD` is the load-bearing one — it defines what
counts as a claim word for BOTH the fuzzy-merge projection and the retrieval planner's intent
classifier, so a per-module copy could drift into making a claim retrievable by a query the merge
step considers a different statement. A test pins one definition each and identity across all four
modules.

`claims.py` is now 843 lines, down from 2,846 — the four modules total 3,124, the growth being
module docstrings that state each layer's direction.

*Still open:* the ledger and the D8 store. Both remain in `claims.py`, which at 843 lines is no
longer a god-module; splitting them further is optional rather than the finding.

#### EM-02 · HIGH · duplication · effort: medium

**Three near-identical ~90-line steward drivers copy-pasted in lessons.py**

*Locations:* `looplab/engine/lessons.py:939`, `looplab/engine/lessons.py:1031`, `looplab/engine/lessons.py:1114`

*Evidence:* store_concept_curation (939-1029), store_claim_curation (1031-1112), and store_task_facets (1114-1216) share an identical ~90-line skeleton: guard on _cross_run_curation, build diagnostic_key/diagnostic_provenance, take _curation_decision_lock, check _curation_attempt_already_resolved_locked, fast-path 'empty' append, fast-path client-None 'unavailable' append, _paid_curation_attempt_locked with propose→append(require_durable=True)→'error' terminal, and an outer except that writes a diagnostic 'error' row. They differ only in: log name, snapshot/has-input/propose functions, and the empty-proposals shape ({merges,splits,purges} vs {decisions} vs {task_id,facets}); facets adds two extra fast paths (already-governed, empty goal). ~270 lines where ~120 would do, and any protocol fix (e.g. a lock-ordering change) must be applied three times in step.

*Recommendation:* Extract a parameterized driver: _run_finalize_steward(log_name, kind, snapshot_fn, has_input_fn, propose_fn, empty_proposals, extra_fast_paths=()) and reduce the three methods to thin configurations. The identical exception/outcome vocabulary makes this a mechanical extraction.

#### EM-03 · MEDIUM · mergeable-entities · effort: large

**Two parallel at-most-once paid-curation protocols; the validator must understand four schema generations**

*Locations:* `looplab/engine/lessons.py:520`, `looplab/engine/lessons.py:619`, `looplab/engine/steward_invocation.py:167`, `looplab/engine/governance_health.py:370`, `looplab/engine/governance_health.py:476`

*Evidence:* The finalize path (lessons.py 520-937: _write_curation_claim/_read_curation_claim/_curation_decision_lock/_paid_curation_attempt/_append_curation_once — ~400 lines of claim-file protocol keyed by semantic curation_key with .curation_invocations/ scratch GC) and the on-demand HTTP/CLI path (steward_invocation.py run_steward_invocation — action_id-keyed durable begun/terminal rows) are two independently designed at-most-once protocols writing the SAME three ledgers (concept/claim/facets curation logs). As a result governance_health._validate_curation_row (370-473) is a ~100-line branch cascade over four coexisting row schemas (v2 semantic finalize rows, v1 begun/terminal HTTP rows, legacy run-keyed rows, oldest undiscriminated audit rows), and read_curation_rows (476-554) enforces two different sequencing disciplines in one loop. Also note this entire paid-curation transaction subsystem lives inside lessons.py/LessonMemory although it has nothing to do with lessons — it is governance infrastructure (~700 of lessons.py's 1334 lines).

*Recommendation:* Move the finalize claim/recovery protocol out of lessons.py into a curation_protocol.py module beside steward_invocation.py, and converge new writes on one protocol (the semantic-key v2 shape) so the validator's other branches become legacy-read-only code that can be isolated and eventually retired. The schema plurality is historical, not a requirement of new writes.

#### EM-04 · MEDIUM · duplication · effort: small

**Durable identity derivation (_curation_source_key / _facets_curation_key) duplicated between writer and validator**

*Locations:* `looplab/engine/lessons.py:541`, `looplab/engine/lessons.py:561`, `looplab/engine/governance_health.py:256`, `looplab/engine/governance_health.py:262`

*Evidence:* lessons.py LessonMemory._curation_source_key (541-551) computes 'source:v1:' + sha256({v:1, run_id, task_id, finish_seq}) and _facets_curation_key (561-569) computes 'facets:v2:' + sha256({v:2, kind:'facets', task_id}). governance_health.py independently reimplements both (256-265) for _validate_v2_curation_row, which rejects any row whose source_key does not match its recomputation (line 294-297). These are content-addressed durable identities: any drift between the two copies (field order, encoding flag, added field) makes every future curation ledger read raise GovernanceLedgerUnavailable on previously valid rows. Nothing ties the copies together except convention.

*Recommendation:* Move the two key functions to governance_health.py (the module already owning the schema constants) and have lessons.py import them; keep a source-scan test asserting a golden digest so an accidental change is a red test, not a silent ledger poisoning.

#### EM-05 · MEDIUM · inconsistency · effort: medium

**Two parallel governance-append implementations; the shared one is homed in the wrong module**

*Locations:* `looplab/engine/claims.py:1168`, `looplab/engine/concept_registry.py:883`, `looplab/engine/concept_registry.py:918`, `looplab/engine/task_facets.py:166`, `looplab/engine/steward_invocation.py:106`

*Evidence:* record_claim_decision (claims.py 1168-1234) hand-rolls its own locked append: _interprocess_lock, action_id idempotency scan via _decision_payload, expected_revision CAS, strict_fsync append, confirm_governance_durable on replay — the exact protocol _append_governance (concept_registry.py 883-990) already implements generically (same lock, same idempotency-before-CAS ordering, same durable-fsync/created-dir handling). Two implementations of one critical protocol can drift (e.g. record_claim_decision returns a sanitized projection on replay while _append_governance returns dict(existing) raw). Separately, _append_governance — the generic append primitive used by task_facets.py, lessons.py curation logs, and steward_invocation.py — lives in the concept-specific concept_registry.py and even special-cases ledger filenames internally (lines 918-921 and 969-977 branch on path.name in {'concept_aliases.jsonl','concept_splits.jsonl'}), so a generic-looking primitive secretly knows the concept ledgers by name.

*Recommendation:* Relocate _append_governance to governance_health.py, parameterize the ledger-specific readers instead of branching on filenames (read_rows is already the right hook — make it mandatory for policy ledgers), and port record_claim_decision onto it, keeping its sanitize-on-replay as a wrapper.

#### EM-06 · MEDIUM · inconsistency · effort: large

**Three coexisting claim-identity systems, each with its own decision-overlay resolution logic**

*Locations:* `looplab/engine/claims.py:2008`, `looplab/engine/claims.py:1733`, `looplab/engine/claims.py:1804`, `looplab/engine/claims.py:1889`, `looplab/engine/claims.py:2062`, `looplab/engine/claims.py:1303`, `looplab/engine/claim_key.py:144`

*Evidence:* claim_assessments maintains three selectable identity modes: lean normalize_statement grouping (2008-2112), opt-in fuzzy token-Jaccard merge (_fuzzy_merge_claims 1733-1801), and the structured claim_key signature (_structured_assessments 1804-1978). claim_key.py's own docstring describes the structured key as the 'full-CR' fix for three named failure modes of the lean/fuzzy paths, yet all three remain live behind flags. The cost is concrete: operator decisions must overlay correctly under every mode, so there are three separate resolution mechanisms — _decision_for with a five-candidate UID fallback chain (1889-1911), the lean path's ~30-line scoped/global-key logic with scope-consistency guards (2062-2093), and the loader's control-char-prefixed shadow namespaces _global_key/_scoped_key (1303-1320) that exist only to keep the lean projection correct. Every governance bugfix must be reasoned about three times.

*Recommendation:* Make structured the default projection, keep lean/fuzzy as explicitly-legacy read paths with a deprecation note, and once consumers migrate delete _fuzzy_merge_claims and the _scoped_key/_global_key shadow namespaces. Until then, add a table-of-modes comment at claim_assessments so reviewers know which overlay logic guards which mode.

#### EM-07 · MEDIUM · duplication · effort: small

**Lesson/research evidence-ingestion loops duplicated between structured and lean assessment paths**

*Locations:* `looplab/engine/claims.py:1846`, `looplab/engine/claims.py:1862`, `looplab/engine/claims.py:2023`, `looplab/engine/claims.py:2039`

*Evidence:* _structured_assessments (1846-1882) and the lean branch of claim_assessments (2023-2056) contain two nearly identical pairs of loops: for each lesson — add run_id/task_id to runs/scopes, _qualify_refs over _node_ids(evidence), route by _lesson_claim_stance into support/oppose; for each research claim — _indexable_research_claim guard, same run/scope registration, _research_verification verdict routing into support/unverified, verification-set update, sources update via _string_list(urls). ~35 lines duplicated verbatim except for the group-lookup call (_grp with signature vs _group with normalized statement). A stance-mapping or receipt bug must be fixed in both.

*Recommendation:* Extract an _ingest_evidence(groups_lookup, lessons, research_claims) helper taking the group-resolver as a parameter; both paths differ only in that resolver and the structured path's extra _ev weight bookkeeping (passable as a callback or handled by the group dict shape).

#### EM-08 · MEDIUM · duplication · effort: small

**The '_governance is None → recurse via project_governed_sources' pattern and the scope-filter block are copy-pasted across four/three call sites**

*Locations:* `looplab/engine/claims.py:1666`, `looplab/engine/claims.py:2462`, `looplab/engine/claim_steward.py:146`, `looplab/engine/concept_steward.py:181`, `looplab/engine/claims.py:1641`, `looplab/engine/claims.py:1697`, `looplab/engine/claims.py:2491`

*Evidence:* Four functions (atlas_for_memory, cross_run_retrieve, claim_curation_snapshot, concept_curation_snapshot) share the same '_governance is None → recurse via project_governed_sources with a self-invoking lambda' skeleton; atlas_for_memory and cross_run_retrieve additionally build source_names by None-checking lessons/research_claims/capsules (claim_curation_snapshot None-checks only lessons; concept_curation_snapshot passes a constant source_names). Separately, the task-scope filter (three _filter_claim_source_rows/_filter_capsule_rows calls comparing str(r.get('task_id')) == wanted) is repeated verbatim in claims_for_memory (1641-1646), atlas_for_memory (1697-1706), and cross_run_retrieve (2491-2498). The scope filter is an access boundary (the comment at 1698 notes a past leak when only one store was filtered) — exactly the kind of code that should have one spelling.

*Recommendation:* Add a governed_projection decorator/helper that handles the source_names derivation + recursion, and a _scope_all_sources(lessons, research, capsules, task_id) helper so the access-boundary filter has a single implementation.

#### EM-09 · MEDIUM · mergeable-entities · effort: medium

**Three parallel 'list subclass carrying a receipt' types with per-type copy/filter helpers and dynamic attribute stashing**

*Locations:* `looplab/engine/claims.py:134`, `looplab/engine/claims.py:820`, `looplab/engine/memory.py:650`, `looplab/engine/claims.py:1207`, `looplab/engine/claims.py:1619`

*Evidence:* _ClaimSourceRows (claims.py 134-139, with _claim_source_rows/_filter_claim_source_rows), _ClaimAssessmentRows (820-836, with _filter_claim_assessments), and _CapsuleRows (memory.py 650-670, with _capsule_rows/_filter_capsule_rows) are three independent implementations of 'a list that carries a health receipt through projections', each with its own inherit/merge rules. On top of that, callers stash extra attributes dynamically: record_claim_decision's validate path and locked_claim_evidence_snapshot set assessments.lessons_snapshot / research_claims_snapshot / decisions_snapshot on the returned list (claims.py 1207-1209, 1619-1621). Any plain list operation (slicing, comprehension, sorted()) silently drops the receipt and snapshots — which is why each type needs its own guarded filter helper, and why getattr(source, 'read_health', None) probes appear throughout.

*Recommendation:* Introduce one small generic Snapshot dataclass (rows + typed receipt + optional attachments) or a single shared ReceiptList base with an explicit .filter()/.map() API, and migrate the three types onto it; make the evidence-snapshot attachments explicit fields instead of monkey-set attributes.

#### EM-10 · MEDIUM · under-decomposition · effort: medium

**memory.py mixes five unrelated subsystems in 1600 lines**

*Locations:* `looplab/engine/memory.py:20`, `looplab/engine/memory.py:78`, `looplab/engine/memory.py:331`, `looplab/engine/memory.py:446`, `looplab/engine/memory.py:505`, `looplab/engine/memory.py:620`

*Evidence:* The module named for the 'episodic case library' (its docstring) actually contains: task fingerprinting/tokenizers (20-75); D2 lesson hygiene — consolidate/quarantine/harmonic retrieval/ranking (78-328); M6 comparative-pair selection + credit parsing (331-443); M4 auto-skill markdown writer (446-502); the JsonlCaseLibrary (505-617); and ~870 lines of concept-capsule machinery (620-1493: capsule validation/receipts, ConceptCapsuleStore, portfolio_concept_overview/_graph/_digest, profit signs/tendencies). The capsule subsystem alone is bigger than most engine modules and is imported piecemeal by claims.py, novelty, tools and the stewards (from looplab.engine.memory import ConceptCapsuleStore, _dedup_valid_capsules, _filter_capsule_rows, _portfolio_concept_overview_data — private names crossing module boundaries at claims.py:1663 and 2460).

*Recommendation:* Extract concept_capsules.py (validation + store + overview/graph/digest + profit signs) and lesson_hygiene.py (consolidate/filter/rank/parse); keep fingerprints + case libraries in memory.py. The private cross-module imports (_dedup_valid_capsules etc.) become public names of the new module, honestly reflecting their real API status. Wire through the existing back-compat shim.

#### EM-11 · MEDIUM · dead-code · effort: small

**Vector-backed CaseLibrary is dead in production — only tests use it**

*Locations:* `looplab/engine/memory.py:1496`, `looplab/engine/lessons.py:385`

*Evidence:* CaseLibrary (memory.py 1496-1600, ~105 lines: VectorStore-backed episodic store with Memora consolidation/expansion, _consolidate direction-aware metric merging, retain_if_improved) has zero callers under looplab/ — a repo-wide grep finds only tests (test_trust_knowledge.py, test_phase3_memory.py, test_memora.py) and a docstring mention in tools/memora.py. The engine's actual case store is JsonlCaseLibrary (lessons.py:385 store_case). Both classes claim the same I19/ADR-10 role in their docstrings, so a reader must discover by grep which one is real. The dead class still accretes maintenance (its _consolidate got a direction-comparability fix at some point).

*Recommendation:* Either wire CaseLibrary into a real consumer (if the Memora harmonic case path is still planned) or delete it and port its direction-comparability lesson into JsonlCaseLibrary docs; at minimum mark it clearly as test-fixture/unwired so its docstring stops claiming to be 'the top-system differentiator'.

#### EM-12 · MEDIUM · excessive-logic · effort: medium

**Ad-hoc hand-written receipt validators repeated ~8 times with no shared schema helper**

*Locations:* `looplab/engine/claims.py:102`, `looplab/engine/claims.py:652`, `looplab/engine/claims.py:767`, `looplab/engine/claims.py:542`, `looplab/engine/memory.py:684`, `looplab/engine/memory.py:726`, `looplab/engine/concept_steward.py:75`, `looplab/engine/claims.py:2537`

*Evidence:* The fail-closed receipt discipline (deliberate and documented) is implemented as ~8 independent hand-rolled validators, each repeating the same idioms — type(x) is not int / not isinstance(x, bool) guards, 0 <= v <= MAX bounds, arithmetic-consistency conjunctions (total == retained + omitted, complete == (omitted == 0)), and legacy-absent default-fill: _safe_claim_read_segment/_safe_claim_read_health (claims.py 102-131), _safe_research_source_summary (~75 lines, 652-726), _safe_claim_source_summary (767-804), _research_source_receipt (542-583), _valid_research_evidence_receipt (335-355), _capsule_concept_evidence_completeness/_capsule_completeness (memory.py 684-765), _concept_source_receipt (concept_steward.py 75-146), and cross_run_retrieve's inline scope_receipt validation (claims.py 2537-2559). Adding one field to any receipt touches its builder, its validator, its digest field-list, and every consumer's projection — with nothing enforcing they stay in sync except tests.

*Recommendation:* Keep the fail-closed semantics but extract a tiny declarative helper (field specs: bounded-int/bool + a consistency predicate list) that each receipt defines once and both builder and validator consume; the invariants are all expressible as (field types, bounds, equalities). This shrinks each validator to a spec table and makes builder/validator drift structurally impossible.

#### EM-13 · LOW · duplication · effort: small

**_valid_node_source and _node_ids duplicate the same numeric-string node-id parsing rules**

*Locations:* `looplab/engine/claims.py:266`, `looplab/engine/claims.py:472`

*Evidence:* _valid_node_source (266-295, the validation fence) and _node_ids (472-496, the reader) independently implement identical parsing rules: int-but-not-bool acceptance, negative rejection, string acceptance only when stripped length <= 24 and lstrip('-').isdigit(), int() with (ValueError, OverflowError) guard, negative-parsed rejection. The comments in each explain the same phantom-ref rationale. A future rule change (e.g. widening the 24-char bound) must be made twice or the fence and reader disagree — precisely the validator/reader drift the module elsewhere works hard to prevent.

*Recommendation:* Implement one _parse_node_id(value) -> Optional[int] used by both: the validator rejects the row when any element parses to None, the reader drops Nones. Behavior identical, single spelling.

#### EM-14 · LOW · dead-code · effort: small

**apply_concept_curation retained with zero production callers**

*Locations:* `looplab/engine/concept_steward.py:355`

*Evidence:* apply_concept_curation (355-414, ~60 lines: batch-apply merges/splits/purges through record_* writers with a partial-apply receipt) is documented as 'low-level compatibility helper for an already-reviewed batch; the steward never invokes it'. Repo-wide grep confirms the only callers are tests (tests/test_concept_steward.py). Since the module's core invariant is 'the steward only PROPOSES; the operator applies via typed single actions or HTTP CAS governance', a live batch-apply function that bypasses CAS/action_id (it passes neither expected_revision nor action_id to record_concept_alias) is not just dead weight — it is a footgun contradicting the invariant one import away.

*Recommendation:* Delete it (its tests exercise record_* behavior reachable directly), or if a batch path must survive, require expected_governance_revision/action_id parameters so it cannot bypass the CAS discipline the rest of the module enforces.

#### EM-15 · LOW · inconsistency · effort: small

**The unicode word-token regex and NFKC+casefold normalization re-declared in five places**

*Locations:* `looplab/engine/memory.py:35`, `looplab/engine/concept_registry.py:56`, `looplab/engine/claim_key.py:31`, `looplab/engine/claims.py:1726`, `looplab/engine/claims.py:2380`

*Evidence:* The identical pattern re.compile(r"[^\W_]+", re.UNICODE) is defined as _WORD_UNICODE (memory.py:35), _WORD (concept_registry.py:56), _WORD (claim_key.py:31), and _CLAIM_WORD (claims.py:1726); the NFKC-normalize-then-casefold-then-tokenize pipeline is likewise repeated in claims.py _retrieval_tokens (2380-2382), claim_key._analyze (117-118), and concept_registry.normalize_key (105). memory.py's legacy/universal duality is deliberate and documented, but the other four copies are simply the same tokenizer independently declared; a Unicode-handling fix would need five edits.

*Recommendation:* Export one WORD_RE (and a tokenize(text) helper doing NFKC+casefold+findall) from a low-level shared module (core or a small engine/_text.py) and import it; keep memory.py's legacy ASCII variant where it is since it is a versioned compatibility contract.


### 4.4 Events

Scope: `looplab/events/`: eventstore.py, replay.py, types.py, projections, span_index.py.

**Reviewer assessment.** The events package is the best-engineered subsystem in the repo: fold() has already been refactored from a 63-way if/elif chain into a uniform handler registry (replay.py:182-193, _HANDLERS at 3713) with an explicit cross-arm _FoldCtx, an enforced folded/diagnostic partition in types.py, and a coherent corruption model in eventstore.py. The dominant structural debt is that replay.py (5,563 lines) is a god-module in which the derived Card-ledger projection (~2,200 lines including _derive_cards at 818 lines) now rivals the fold itself, and that defensive-validation micro-patterns (hex-digest checks, bounded-int guards, request-queue purges, certificate invalidation) are copy-pasted rather than schema'd, with the log's own comments recording at least one real bug caused by exactly that drift.

**Strengths worth preserving:**

- The fold is already a handler registry, answering the decomposability question: one pure `(st, e, d, ctx) -> None` handler per event type, dispatched via `_HANDLERS.get` (replay.py:3713-3827), with the refactor's determinism argument documented in place (replay.py:182-193) and FoldCursor reusing the same handlers for incremental prefixes.
- types.py enforces a hard partition: every registered event type must be either in `replay._HANDLERS` or in `DIAGNOSTIC_EVENTS`, never both/neither (types.py:379-398), plus registry-guarded thread-append seams (`BACKGROUND_APPENDABLE`, `SETUP_THREAD_APPENDABLE`) with splice-neutrality tests — a typo'd event type can no longer silently no-op.
- The comment lifecycle is the model of projection unification the rest of the package should follow: one shared reducer `apply_comment_event` (comment_projection.py:104) is consumed by both `replay.fold` (replay.py:3691-3699) and the serve owner/reviewer APIs, so accept/fold semantics cannot diverge.
- eventstore.py's corruption model is precise and layered: torn final line (healable) vs mid-file complete-record divergence (fail-closed with `repair-log` recovery), dense-seq fencing, crash-atomic batch envelopes that pre-batch readers reject wholesale, and an incremental read cache whose consistency proof (full-prefix sha256) is explicit.
- Why-comments are genuinely load-bearing throughout: nearly every defensive guard cites the concrete crash/exploit it closes, often with the pinning test named (e.g. replay.py:1550-1556, 5185-5196), which makes the replay-safety invariants auditable line by line.

#### EV-01 · HIGH · under-decomposition · effort: large

**_derive_cards is an ~820-line god-function and the Card subsystem consumes ~40% of replay.py**

*Locations:* `looplab/events/replay.py:4746-5563`, `looplab/events/replay.py:2410-2903`, `looplab/events/replay.py:4070-4745`

*Evidence:* _derive_cards (replay.py:4746-5563) is one 818-line function with 9+ numbered phases (identity registration, hash/native bridging, merge-alias resolution with nested _canon closure, verdicts, drop overlays, build-reservation status, enrichment apply, ranking, operator overlays, selection-readiness blockers), containing 6 nested closures (_card_id, _register_card_identity, _record_registration, _record_action_owner, _node_parent_generations, _canon) and its own mini-state (12+ local dicts/sets). Together with its supporting helpers (_bounded_card_* at 2410-2730, card handlers at 2731-2939, _card_added_snapshot/_card_added_ownership/_card_action_freshness/_card_sidecar_subject at 4214-4745) the Card ledger occupies roughly 2,200 of replay.py's 5,563 lines (~40% of the file). It is invoked exactly once, as a pure post-pass from _finalize_fold (3848), and reads only folded state, so it has no reason to live in the fold module.

*Recommendation:* Extract the Card ledger into a sibling module (e.g. events/cards.py: fold-time bounded-receipt helpers + a derive_cards(st) post-pass) and decompose _derive_cards into its numbered phases as top-level pure functions taking/returning explicit small dataclasses (identity map, alias map, action-owner table). Keep _finalize_fold calling one entry point; behavior is unchanged because the pass is already pure over RunState.

#### EV-02 · MEDIUM · duplication · effort: medium

**Card action bounding is implemented twice: fold-admit (_bounded_card_action) and derive-time (_card_added_snapshot) re-validate the same fields with copy-pasted blocks**

*Locations:* `looplab/events/replay.py:2515-2576`, `looplab/events/replay.py:4214-4320`, `looplab/events/replay.py:2528-2542`, `looplab/events/replay.py:4250-4261`, `looplab/events/replay.py:2487-2512`, `looplab/events/replay.py:4227-4244`

*Evidence:* st.cards_added rows are produced only by _bounded_card_added_receipt/_bounded_card_action (operator/params/space/eval_profile/eval_timeout/concept_tags/parent_ids all bounded), yet _card_added_snapshot re-implements the identical decoding over those already-bounded rows: the eval_timeout try/float/isfinite block is byte-similar at 2528-2542 vs 4250-4261, the `space` list-of-finite-floats loop at 2498-2511 vs 4234-4243 (differing only heapq.nsmallest vs sorted()[:64] — a behavioral drift already), and the parent_ids dedupe/bound loop appears three times (2555-2561, 2622-2628, 4283-4289). Unlike span_index._append, which documents its deliberate double-validation, no comment claims this duplication is intentional.

*Recommendation:* Make _card_added_snapshot consume the receipt shape _bounded_card_action produces (single shared decoder for the action block: one function returning the normalized action dict + owns_action flag), or at minimum extract shared helpers for the eval_timeout, space and parent_ids coercions so the two stages cannot drift (they already differ on top-K selection strategy).

#### EV-03 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**The completion-certificate invalidation block is copy-pasted at 5 sites, and a comment records a real bug caused by one site drifting**

*Resolution:* `replay._invalidate_completion_certificates(st, ctx)` (over a smaller
`_clear_approval(st)`) is called from all five handlers — `_on_node_created`,
`_on_node_tombstoned`, `_on_node_reset`, `_on_node_abort` and `_on_resume_or_run_reopened`. The
`_on_node_tombstoned` partial variant is deliberately NOT folded in: it clears the subject half and
the grant half under two separate `in affected` conditions, which is a different rule; it now calls
`_clear_approval` for the half it does clear. `_on_node_abort`'s finished-run branch likewise uses
`_clear_approval` alone, because a FINISHED run's certificate is its result and only the grant
naming the aborted node is void.

`tests/test_completion_certificate_invalidation.py` asserts the shipped bug behaviourally rather
than by field inspection. The certificate deliberately names the metric LOSER, which makes the
confirm-override observable in `best_node_id`: while the certificate stands selection returns the
confirmed node, and the moment it is retired selection falls back to the metric winner. Each of the
five changes touches a THIRD node, so a surviving override is the stale certificate rather than the
subject merely becoming ineligible. Verified to have teeth by reintroducing the exact historical
half-clear (`confirmed_done` cleared, `ctx.best_confirmed` left) at the reopen site and again at the
new-candidate site: each fails exactly its own case with `assert 2 == 1`.

*Locations:* `looplab/events/replay.py:623-629`, `looplab/events/replay.py:1190-1196`, `looplab/events/replay.py:1341-1349`, `looplab/events/replay.py:3163-3174`, `looplab/events/replay.py:3298-3308`

*Evidence:* The 7-line sequence `st.confirmed_done = False; ctx.best_confirmed = None; st.approved = False; st.awaiting_approval = False; st.approval_subject = None; st.approval_generation = None; st.approved_node_id = None` appears verbatim in _on_node_created, _on_node_tombstoned, _on_node_reset, _on_resume_or_run_reopened, and _on_node_abort (plus partial variants in the tombstone/abort finished-run branches). The comment at 3163-3167 admits the failure mode: "every other invalidation site (node_reset, tombstone, new-candidate) pairs these two, and omitting the ctx clear here let an epoch-(N-1) certificate keep overriding epoch-N's metric winner" — i.e. one copy already drifted and shipped a selection bug before being fixed.

*Recommendation:* Extract `_invalidate_completion_certificates(st, ctx)` (and possibly a smaller `_clear_approval(st)`) and call it from all five handlers. This is a pure mechanical refactor of identical statements; the existing tests pin behavior.

#### EV-04 · MEDIUM · inconsistency · effort: medium

**Event-data admission is implemented three different ways; hex-digest validation alone is copy-pasted 4x within one handler and ~20x repo-wide**

*Locations:* `looplab/events/replay.py:277-312`, `looplab/events/replay.py:1984-2019`, `looplab/events/replay.py:4070-4107`, `looplab/events/replay.py:2820-2827`, `looplab/events/replay.py:4117-4120`

*Evidence:* _on_run_started validates four sha256-prefixed digest fields with four identical 6-line blocks (len==71, startswith 'sha256:', all-hex — replay.py:277-312); the same idiom recurs at 2820-2826 ('idea:v1:' digest), 4117-4120 (_digest_ref), and in 15+ other modules (core/models.py:661,667,952, engine/costs.py:194, search/speculation_quality.py:322, ...). Meanwhile the package already contains two other admission styles: declarative field tables (_COVERAGE_SNAPSHOT_STR/INT/FLOAT/LIST driving _coverage_snapshot_row, 1984-2019) and a recursive generic bounder (_bounded_card_enrichment, 4070-4107). The scalar guards `type(x) is int and 0 <= x <= (1 << 31) - 1` and `isinstance(v, bool) or not isinstance(v, int)` are each hand-rolled at dozens of call sites in replay.py.

*Recommendation:* Add a tiny validators module (e.g. core: valid_hex_digest(value, prefix), bounded_int(value, lo, hi), bounded_str(value, max_len)) and use the existing table-driven style (_coverage_snapshot_row) as the canonical pattern for new handlers. Collapse the four _on_run_started digest blocks into one loop over field names first — that alone removes ~30 lines with zero semantic risk.

#### EV-05 · MEDIUM · duplication · effort: medium — **RESOLVED (2026-08-02)**

**Four hand-synchronized implementations of the tolerant-JSONL-prefix scan (stop at first torn/corrupt line)**

*Resolution:* Two shared primitives in `events/eventstore.py`. `decode_jsonl_line(raw)` owns the
per-line rule as three explicit outcomes (record / blank-skip / `JsonlRecordInvalid`), and
`scan_jsonl_region(buf)` owns the buffer walk, returning `(records, consumed)` with each record as
`(obj, start, end)`. `_parse_jsonl_region` and `span_index._scan_light` are now thin projections
over the walk; `iter_jsonl`, `log_divergence`, `span_index._load_persisted` and the span-tail
fallback in `serve/routers/runs.py` share the line rule while keeping their own stop policies —
`log_divergence` deliberately continues PAST the bad line to count what the readers drop.
A fifth copy the review had not counted turned up in `serve/routers/runs.py::_scan_span_tail`,
whose own docstring claimed it "applies `iter_jsonl`'s torn-tail rules verbatim".

`tests/test_jsonl_prefix_scanner.py` is the shared equivalence test the extraction makes possible:
one corpus of adversarial buffers (torn tail, corrupt line, valid-JSON non-object, blank runs,
unicode) drives every reader and asserts they agree on the accepted records AND on the byte
watermark, plus a narrow source guard for a re-inlined stop. Verified to have teeth: relaxing
`decode_jsonl_line`'s non-object rejection fails 10 cases across all four readers at once.

*Locations:* `looplab/events/eventstore.py:311-333`, `looplab/events/eventstore.py:669-701`, `looplab/events/eventstore.py:539-573`, `looplab/events/span_index.py:188-219`, `looplab/serve/log_pages.py:190`

*Evidence:* The identical durability contract (yield complete newline-terminated JSON objects, stop at first torn/blank-unterminated/corrupt/non-dict line) is implemented four times: iter_jsonl (streaming), _parse_jsonl_region (buffer + consumed-offset, docstring: "applying iter_jsonl's EXACT durability rules"), log_divergence (re-walks all complete lines with its own accept predicate), and span_index._scan_light (docstring: "applying iter_jsonl's durability rules... matches iter_jsonl"), with serve/log_pages.py:190 extending the contract a fifth time. Each copy carries comments promising equivalence with the others; nothing but discipline keeps the stop conditions identical (log_divergence's predicate already went 'strictly weaker than read_all's stop condition' once, per its own comment at 549-554).

*Recommendation:* Extract one core scanner that yields (raw_line, start_offset, end_offset) with the stop-at-first-bad rule, and build iter_jsonl, _parse_jsonl_region, _scan_light and log_divergence's walk on top of it (each keeps its own payload handling: Event decode, light projection, divergence accounting). A shared equivalence test then covers all consumers at once.

#### EV-06 · MEDIUM · duplication · effort: medium — **RESOLVED (2026-08-02)**

**EventStore.append and append_many duplicate the entire ~55-line critical section**

*Resolution:* `EventStore._locked_append(build, *, expected_last_seq, require_lock,
require_durable)` owns the section; both public methods now contribute only a payload builder.
The recommendation's "pre-serialized payload" is a `build(cur)` CALLBACK instead: the seq is
only knowable inside the critical section, so a payload serialized against a tail read outside
it would carry a seq another writer already used. It returns `(payload_bytes,
last_logical_seq, result)` — the newline-terminated bytes, the highest logical seq they carry
(the LAST batch member, which is what an uncertain-sync reservation must fence), and the public
method's return value. Pinned by `tests/test_append_critical_section_parity.py`: a source scan
that fails if either public method re-grows a private critical section, plus a parity table that
runs each rule (divergence fail-closed, CAS, torn-tail heal, required-lock failure, durable
directory-entry publish, uncertain-sync fence) against BOTH appenders.

*Locations:* `looplab/events/eventstore.py:950-1015`, `looplab/events/eventstore.py:1040-1107`

*Evidence:* Both methods repeat verbatim: the lock acquisition (`self._append_lock, _interprocess_lock(...)`), reset/deletion asserts, `self.read_all()` + divergence raise, `_heal_torn_tail()`, `cur = max(self._seq, self._disk_last_seq())`, the expected_last_seq CAS raise, the open/write/flush/strict-or-best-effort-fsync/fstat block with the `accepted` flag and `_mark_uncertain_append` on BaseException, `_publish_dir_entry`, `self._seq = ...`, `_trusted_growth_stat = (...)` and the trailing `read_all()`. Only the payload construction (single Event line vs batch envelope) differs.

*Recommendation:* Extract a private `_locked_append(payload_bytes, last_logical_seq, expected_last_seq, require_lock, require_durable)` that owns the critical section, called by both public methods with their pre-serialized payload. This shrinks ~110 lines to ~65 and makes future durability changes single-site.

#### EV-07 · MEDIUM · excessive-logic · effort: medium

**Acknowledged-but-unfixed O(n²) read cost for cross-process readers in EventStore.read_all**

*Locations:* `looplab/events/eventstore.py:1142-1160`, `looplab/events/eventstore.py:628-665`

*Evidence:* read_all validates externally-observed growth by re-reading and re-hashing the ENTIRE consumed prefix on every call that sees new bytes not written by this store (eventstore.py:1151-1158). The in-code review note (1142-1150) states this is O(log-size) per external append — O(n²) over a run for a reader-only process such as the UI server tailing the engine — "the exact quadratic cost this cache exists to avoid", and that prefix_anchor_from_handle's bounded head/tail windows (628-665) were designed for this check but are not used here. The tradeoff (proof vs detector) is documented as deliberate, but the cost lands on the primary UI polling path for large logs.

*Recommendation:* Decide the tradeoff explicitly: either adopt the bounded anchor for cross-process growth validation (as the note proposes, accepting detector-not-proof semantics that seq-density fencing already backstops), or maintain a rolling incremental hash checkpoint so re-validation is O(new bytes). Remove the standing TODO-style comment once decided.

*Status (post-baseline):* Fixed on `master` by commit `c92b89f` (2026-08-01, immediately after this review's baseline): `read_all` now validates external growth with bounded head/tail windows on every poll and re-runs the full-prefix sha256 proof only on first external observation and each time the prefix doubles — amortized O(1) per appended byte; the review marker was replaced by a why-comment describing the shipped design. The finding is retained as accurate at the baseline.

#### EV-08 · MEDIUM · under-decomposition · effort: medium

**_on_node_created is a 226-line handler mixing node construction with ~100 lines of concept-envelope policy**

*Locations:* `looplab/events/replay.py:405-631`, `looplab/events/replay.py:506-615`

*Evidence:* _on_node_created handles: duplicate-terminal resurrection guard, parent-generation binding, aborted-intent materialization, Node construction with resource-glitch re-raise, then ~110 lines (506-615) of concept-mode decoding (raw receipts, delta/full/unsupported-mode discrimination, transitional 40a5a94 canonicalization, receipt protection across CLASSIFIER/OPERATOR/OFFLINE provenance, four-way branch updating five sidecar maps and four ctx sets), then holdout invalidation + certificate clearing + build-marker clearing. The concept block reads/writes st.node_concepts, st.node_concept_provenance, st.node_concepts_at_vocab, st.node_concept_deltas and four _FoldCtx sets — a self-contained sub-machine.

*Recommendation:* Extract the concept-envelope section into `_fold_node_concept_envelope(st, ctx, n, raw_idea, current, current_provenance)` living next to _on_node_concepts/_on_concept_tag_edited so all concept-membership writers sit together; _on_node_created then reads as lifecycle logic only.

#### EV-09 · LOW · mergeable-entities · effort: small

**Twin handlers and quadruplicated request-queue purges for the force_confirm/force_ablate/fork intent family**

*Locations:* `looplab/events/replay.py:3403-3423`, `looplab/events/replay.py:1160-1165`, `looplab/events/replay.py:1244-1254`, `looplab/events/replay.py:3284-3289`, `looplab/events/replay.py:1086-1095`

*Evidence:* _on_force_confirm and _on_force_ablate (3403-3423) are byte-identical except for the target lists (confirm_requests/confirm_request_generations vs ablate_requests/ablate_request_generations); _on_fork (3425-3436) is a third variant of the same shape. The corresponding purge idiom — filter nid out of `<x>_requests` and `<x>_request_generations` — is repeated for both queues in four handlers (_on_node_tombstoned 1160-1165, _on_node_reset 1244-1254, _on_node_abort 3284-3289, _requeue_partition_bound_results 1086-1095), i.e. 8 near-identical two-line filter pairs. The _advance_request_cursor extraction (3438-3469) shows the codebase already unified the done-side of this family after the hand-rolled copies grew "complementary holes"; the request-side purges remain unshared.

*Recommendation:* Parametrize a `_queue_forced_request(st, d, requests, generations)` helper for the two force handlers and a `_purge_node_requests(st, nid_or_set)` for the 4 purge sites, mirroring what _advance_request_cursor already did for cursors.

#### EV-10 · LOW · duplication · effort: medium

**Span-to-node attribution rule implemented three times across traceview and span_index, kept equivalent only by comments**

*Locations:* `looplab/events/traceview.py:1048-1065`, `looplab/events/traceview.py:914-931`, `looplab/events/traceview.py:489-517`, `looplab/events/span_index.py:388-421`

*Evidence:* The rule "a span's effective node is its own stamped node_id, else its trace ROOT's node_id (never a full ancestor walk)" is implemented in build_trace_view (root_nid map, 1048-1065), again in build_conversation (per-span `mine` comprehension with trace_nid fallback, 914-931, whose comment says "Attribute PER SPAN, exactly as build_trace_view does"), and a third time as the selection predicate in _bounded_node_trace_tail (489-517) whose comment warns "This must stay equivalent to the index's node_tids -> rows -> tail path"; span_index._rows_for_node (388-421) adds a fourth root-resolution for generation fencing. A past divergence is documented in the 914-931 comment (whole-trace keying dropped a node's turns from its own conversation).

*Recommendation:* Extract `effective_node_id(span, root_node_by_trace)` plus a shared `trace_root(spans)` helper into traceview and use them from build_trace_view, build_conversation, _bounded_node_trace_tail and span_index; add one equivalence test between the indexed and no-index selection paths.

#### EV-11 · LOW · duplication · effort: small

**Authoritative-provenance set spelled inline twice in _materialize_concept_deltas and again as a module constant**

*Locations:* `looplab/events/replay.py:1874-1879`, `looplab/events/replay.py:1947-1952`, `looplab/events/replay.py:4689-4695`

*Evidence:* The 4-member frozen set {AUTHORED, CLASSIFIER, OPERATOR, OFFLINE_HEURISTIC} appears as two inline set literals inside _materialize_concept_deltas (the Kahn pass at 1874-1879 and the cycle-fallback pass at 1947-1952), while _CARD_NODE_CONCEPT_PROVENANCE (4689) is the same set plus UNTRUSTED. Adding a new provenance tier requires editing three spellings; missing the second inline copy would make the cycle path disagree with the topo path on the same log.

*Recommendation:* Define one module-level _INHERITABLE_CONCEPT_PROVENANCE frozenset and reference it from both loops (and derive _CARD_NODE_CONCEPT_PROVENANCE from it | {UNTRUSTED}). Consider also collapsing the two parallel parent-reason loops (1843-1885 vs 1930-1957) into a shared per-parent classifier.

#### EV-12 · LOW · layering · effort: medium

**events/ package hosts non-event concerns: kNN/similarity math in digest.py and generic mutable-store JSONL utilities in eventstore.py**

*Locations:* `looplab/events/digest.py:16-64`, `looplab/events/eventstore.py:364-525`, `looplab/events/comment_projection.py:239-333`

*Evidence:* digest.py's numeric_params/knn_idw/param_distance (16-64) are generic ML-similarity primitives consumed by search/surrogate, search/panel, runtime/proxy, engine/novelty and tools/run_tools (find_analogous) — nothing event-related; the module's own docstring positions it as a "reuse hub" placed here only to stay importable without cycles. Likewise eventstore.py carries read_jsonl_lenient(_with_health)/write_jsonl_atomic/replace_jsonl_rows_atomic_preserving_quarantine (364-525), which serve the lessons/memory/claims MUTABLE stores in engine/ and trust/ and explicitly contrast themselves with the event log's semantics. comment_projection.py's comments_page/history_page cursors (239-333) are HTTP pagination mechanics used only by serve routers. All are pure and layering-legal (events imports only core), but they dilute the package's stated identity (event store + fold + projections) and make eventstore.py a 1,224-line multi-purpose module.

*Recommendation:* Move knn_idw/numeric_params/param_distance to core (e.g. core/similarity.py) and the mutable-store JSONL helpers to core (e.g. core/jsonlio.py), keeping re-export shims in the old locations (the repo already has the _LAYOUT meta-path shim pattern for exactly this). Low urgency; do it opportunistically when touching those helpers.

#### EV-13 · LOW · mergeable-entities · effort: medium

**Parallel legacy hypothesis board duplicates the Card event family end to end**

*Locations:* `looplab/events/replay.py:2314-2329`, `looplab/events/replay.py:2905-2939`, `looplab/events/replay.py:2390-2408`, `looplab/events/replay.py:4874-4877`, `looplab/events/types.py:233-256`

*Evidence:* Every card event has a hypothesis twin folded by a mirrored handler: _on_hypothesis_ranked vs _on_card_ranked (the latter's comment: "mirrors _on_hypothesis_ranked"), _on_hypothesis_merged vs _on_card_merged, _on_hypothesis_added vs _on_card_added, hypothesis_updated drop/abandon vs card_dropped — and _derive_cards must then zip both families ([(True, cards_added)] + [(False, hypotheses_added)] at 4874-4877, same for merges at 5014-5016, ranking fallback at 5371, abandon/delete overrides at 5156/5559). This is documented back-compat ("st.cards now SUBSUMES the removed st.hypotheses board"), so it is not a defect per se, but the compat layer is load-bearing inside the largest function in the package and every card change must be reasoned against the shadow family too.

*Recommendation:* Keep the fold handlers (old logs must replay), but isolate the hypothesis-shadow merging into an explicit adapter step at the top of the card derivation (normalize hypotheses_added/merged/ranking into synthetic card-shaped rows once), so the 800-line derivation reasons over one input family instead of interleaving both throughout.


### 4.5 Core

Scope: `looplab/core/`: models.py, config.py, llm.py + siblings, tracing, parsing, redaction, fences.

**Reviewer assessment.** looplab/core is a disciplined foundation layer with genuinely strong single-owner seams (fitness.SearchFitness, concepts.py, errors.py, llm_broker.py) and exceptional why-comment density, but two files have become god-modules: models.py (2181 lines mixing domain models with ~800 lines of card/idea digest+receipt machinery) and llm.py (an ~890-line client class with three parallel stream-reassembly implementations, one of which is production-dead). The layer's main systemic costs are copy-paste sibling modules (run_reset.py vs run_deletion.py), several bespoke bounded-JSON/redacting tree walkers that should share one core, and legacy code retained solely because tests pin it. The documented layering rule ("core imports nothing above itself") holds everywhere except one lazy import in config.py.

**Strengths worth preserving:**

- Single-owner seams are real, not aspirational: SearchFitness (core/fitness.py) demonstrably replaced ~6 inlined ranking copies, Node.robust_metric replaced ~12 call-site spellings, concepts.py owns concept identity for replay/search/serve alike, and errors.py + llm_broker.py cleanly broke the parse↔llm import cycle — each with the rationale written at the seam.
- Registry-guarded duck-typed contracts (PROMPT_KEYS in core/prompts.py, LLM_ROLE_KEYS/_ROLE_FIELDS in core/llm.py) backed by two-way source-scan tests turn the classic silent-rename failure into a red test.
- Why-comment discipline is exceptional and evidence-based: back-compat traps (Idea._coerce_eval_timeout's invariant-5 argument, LEGACY_CONFIG_SNAPSHOT_DEFAULTS with per-key commit dating and an explicit 'what this map is NOT' section) read like ADRs at the point of use.
- Untrusted-input handling is uniformly fail-closed at durable boundaries: redact.py/redact_persisted_text, advisory_payloads' receipt validators, comparison.py's refuse-to-invent contract, and the bounded tolerant readers in models.py all share the same posture of degrading rather than crashing replay.
- LLM transport resilience is unusually complete and each mechanism cites the live incident it fixes: keepalive-aware idle watchdogs, stream-stall degrade ratchets, reasoning/stream_options capability probing, empty-200 billing, saturating cost accounting, and the lane-fair concurrency broker.

#### CO-01 · MEDIUM · duplication · effort: medium

**run_reset.py and run_deletion.py are near-duplicate fence modules**

*Locations:* `looplab/core/run_reset.py:47-151`, `looplab/core/run_deletion.py:102-230`, `looplab/core/run_reset.py:71-76`, `looplab/core/run_deletion.py:127-131`

*Evidence:* The two modules implement the same durable writer-fence protocol twice: `_is_reparse` is duplicated verbatim (run_reset.py:47-50 vs run_deletion.py:102-105); the lstat/bounded-read/lstat file-identity check including an identical 6-field `identity = lambda info: (...)` appears in both `load_run_reset_marker` (run_reset.py:53-94) and `load_run_deletion_fence` (run_deletion.py:108-156); `publish_*` (encode, size-check, strict_atomic_write_text, read-back confirm), `assert_*_write_allowed`, and `clear_*` are structurally identical with only the schema key-set and error-class names differing. Both declare the same UUID operation-id regex and the same 64-hex generation regex and the same 8 KiB cap. ~180 of ~400 combined lines are copy-paste; this is fail-closed security-adjacent validation where silent divergence between the two copies (e.g. one gaining a TOCTOU fix the other misses) is the realistic failure mode.

*Recommendation:* Extract a shared `_fence.py` helper: `load_bounded_json_marker(path, schema_validator, max_bytes, error_cls)` (lstat-identity-checked bounded read), `publish_marker(...)` (encode+strict write+read-back confirm), and the shared `_is_reparse`/regex constants. Keep the two thin modules as the public schema owners so their distinct key-sets and error types stay explicit.

#### CO-02 · HIGH · under-decomposition · effort: large

**models.py is a god-module: card/idea identity-digest machinery (~800 lines) buried among domain models**

*Locations:* `looplab/core/models.py:426-980`, `looplab/core/models.py:1200-1451`, `looplab/core/models.py:579-679`, `looplab/core/models.py:442-576`, `looplab/core/models.py:1528-2182`

*Evidence:* models.py (2181 lines) contains at least five separable subsystems beyond the domain models: (1) versioned idea/card action digests and ownership receipts — `idea_proposal_digest` with its bespoke bounded-JSON walker `_complete`, `_card_action_digest` v1/v2/expanded-v1 plus three receipt constructors and `valid_card_action_digest` (lines 682-980); (2) footprint normalization + the Developer marker parser (442-576); (3) the closed steering-context vocabulary validator (579-679); (4) the Card provenance model family `CardConceptSource`/`CardIdentityProvenance`/`CardSelectionProvenance` + the 140-field-comment `Card` (1200-1451); (5) `RunState` with ~150 fields (1528-2182). The module already established the extraction pattern twice — concepts moved to concepts.py and comparison to fitness.py, each with an explicit re-export seam at models.py:27-34 — so the precedent and mechanism exist. RunState's flat ~150-field shape itself is documented as intentional (banner at 1531-1539) and is not the defect; the defect is that every card-identity change churns the same file as Node/Idea/RunState.

*Recommendation:* Extract the card/idea digest+receipt subsystem (682-980) and the Card provenance models (1200-1451) into a `core/cards.py` (or `core/card_identity.py`), and the footprint helpers (442-576) alongside, re-exporting through models.py exactly as concepts.py already does. Preserve comments verbatim per the project convention.

#### CO-03 · MEDIUM · dead-code · effort: medium

**Production-dead urllib-era streaming stack (~185 lines) kept alive only by tests**

*Locations:* `looplab/core/llm.py:572-657`, `looplab/core/llm_streaming.py:29-33`, `looplab/core/llm_streaming.py:236-275`, `looplab/core/llm_streaming.py:278-335`, `looplab/core/llm.py:44`

*Evidence:* `OpenAICompatibleClient._read_stream` (llm.py:572-657, ~86 lines) is explicitly documented as "LEGACY (urllib-era)... nothing in production calls this — only tests do". Grep confirms: the only callers are tests/test_openai_client.py:396,716. It exclusively drives four more helpers in llm_streaming.py — `_sse_chunks` (~47 lines), `_socket_watchdog` (~40), `_SSETail`, `_raw_socket` — none reachable from any live path (`_stream_raw_socket`/`_shutdown_pool_sockets` are the live ones). llm.py:44 also imports `urllib.request` solely so old tests can monkeypatch `llm.urllib.request.urlopen`. Net: ~185 lines of transport code plus the tests pinning it must be understood by anyone touching streaming, and the module docstrings must keep explaining which of two SSE paths is real.

*Recommendation:* Delete `_read_stream`, `_sse_chunks`, `_socket_watchdog`, `_SSETail`, `_raw_socket`, the `urllib.request` import, and their tests; port any behavior those tests uniquely cover (stall-kill, non-SSE fallback) onto `_accumulate_stream`/`_stream_with_idle_guard`, which already implement the same contracts.

#### CO-04 · MEDIUM · duplication · effort: medium

**Three parallel stream-reassembly loops in one client; fallback block triplicated**

*Locations:* `looplab/core/llm.py:523-570`, `looplab/core/llm.py:572-657`, `looplab/core/llm.py:893-1011`, `looplab/core/llm.py:548-559`, `looplab/core/llm.py:618-630`, `looplab/core/llm.py:965-997`, `looplab/core/llm.py:365-394`, `looplab/core/llm.py:929-957`

*Evidence:* `_accumulate_stream` (523-570), legacy `_read_stream` (572-657) and `complete_text_stream` (893-1011) each hand-roll SSE-delta accumulation. The tool-call slot-merge block (setdefault slot, id/name/arguments append) is duplicated near-identically at 548-559 and 618-630 (the SDK path first converts via tc.model_dump()). `complete_text_stream` separately re-implements `_sdk_chat`'s stream setup: the same `header_join = self.header_timeout + min(10.0, self.header_timeout)` computation (365 vs 929), the same own-and-close finally dance (386-394 vs 952-957), and the same `_is_stream_options_reject`/`_is_reasoning_reject` 400-retry pair (734-751 vs 973-981). Inside `complete_text_stream` the blocking-fallback block `delegated_to_fallback = True; text = self.complete_text(messages); if text: yield text; return` appears three times verbatim (965-970, 982-987, 992-997).

*Recommendation:* After deleting the legacy path (see dead-code finding), extract one `_open_bounded_stream()` helper (permit + bounded create + _streaming_body + close-on-exit) shared by `_sdk_chat` and `complete_text_stream`, one delta-merge helper for tool-call slots, and one `_fallback_to_blocking()` local for the triplicated block.

#### CO-05 · MEDIUM · under-decomposition · effort: medium

**OpenAICompatibleClient._post is a ~200-line multi-concern method inside an ~890-line class**

*Locations:* `looplab/core/llm.py:673-871`, `looplab/core/llm.py:195-1088`, `looplab/core/llm.py:429-520`

*Evidence:* `_post` (673-871) interleaves at least six concerns in one loop body: T7 cache lookup/deep-copy/zeroing (678-699), per-attempt stream/degrade decision (718-719), five exception-specific retry policies (726-804), empty-200/keepalive-stream classification (805-850), billing of empty-but-billable envelopes (843-846), and cache insertion with LRU eviction (864-870). `_bounded_create` + its inflight/teardown accounting (302-314, 403-427, 429-520) is another ~120 lines of intricate lock choreography. The class as a whole (195-1088) owns transport, retry, caching, degradation ratchets, pool teardown, cost accounting and tracing. Every concern is well-commented and incident-motivated, but the method's length means any new provider quirk is another branch in an already 6-way exception ladder.

*Recommendation:* Split `_post` into `_cache_get`/`_cache_put`, a `_classify_response(parsed, use_stream)` returning accept/retry/stall, and a retry-policy table keyed on exception type; move the cache into a small `_ResponseCache` class. Behavior-preserving mechanical extraction only — the comments move with the code.

#### CO-06 · MEDIUM · duplication · effort: small

**Two near-identical bounded redacting JSON-tree sanitizers (tracing vs advisory_payloads)**

*Locations:* `looplab/core/tracing.py:73-132`, `looplab/core/advisory_payloads.py:467-499`, `looplab/core/tracing.py:46-70`, `looplab/core/advisory_payloads.py:436-447`

*Evidence:* `tracing.sanitize_trace_value` (73-132) and `advisory_payloads._tree` (467-499) are structurally the same function written twice: recursive walk with depth cap (5), per-container item cap (64), shared mutable char-budget cell (`remaining[0]`/`budget[0]`), int bounded to ±2^63 else stringified, non-finite float stringified, `is_secret_key_name(key)` → "***", strings through the redactor with a cap. They differ only in constants and which redaction entry point they call (`_trace_text` vs `_text`, both thin wrappers over `redact_persisted_text`). Both files also carry their own budgeted-text helper (`_trace_text`+budget bookkeeping in `_trace_messages` vs `_text`). Both walkers enforce separately-maintained caps (tracing's `_TRACE_TREE_TOTAL_ITEMS_MAX` vs advisory's `_MAX_TREE_ITEMS` cell), so a redaction fix or cap change landing in one walker silently misses the other durable boundary.

*Recommendation:* Move one parameterized `bounded_redacted_tree(value, *, max_chars, max_items, max_depth, max_total_items)` into redact.py (which both already import) and have tracing and advisory_payloads call it with their own constants.

#### CO-07 · MEDIUM · layering · effort: small

**core→agents layering violation: Settings validation lazily imports agents.cli_agent**

*Locations:* `looplab/core/config.py:1358-1363`, `looplab/agents/cli_agent.py:21-22`, `looplab/core/task_kinds.py:9-14`

*Evidence:* CLAUDE.md's layering rule is "core imports nothing above itself", yet `Settings._check_trust_gate` does `from looplab.agents.cli_agent import PRESETS` (config.py:1358) — the only upward import in the whole core package (grep-confirmed). cli_agent itself imports core.models and core.validate at module scope, so the cycle is avoided only by the laziness; every `Settings(...)` construction now imports part of the agents layer as a side effect, and a future module-scope import in cli_agent's transitive closure that touches config would deadlock the import graph. The repo already has the right pattern for exactly this problem: task_kinds.py exists so "generated configs, CLI Genesis and web launch" share a kind vocabulary in core.

*Recommendation:* Move the developer-backend name registry (the PRESETS keys, not the preset bodies) into core — e.g. a `DEVELOPER_BACKENDS` tuple in task_kinds.py or a new core module — have cli_agent build PRESETS keyed off it, and validate against the core constant. A two-way source-scan test (the project's established registry pattern) keeps them in sync.

#### CO-08 · LOW · inconsistency · effort: small

**Six bespoke canonical-JSON→SHA-256 digest minters with no shared core**

*Locations:* `looplab/core/models.py:689-757`, `looplab/core/models.py:779-924`, `looplab/core/advisory_payloads.py:249-265`, `looplab/core/fitness.py:113-118`, `looplab/core/models.py:1148-1161`, `looplab/core/models.py:1184-1191`

*Evidence:* The same idiom — validate/bound a payload, `json.dumps(..., sort_keys=True, separators=(",", ":"), allow_nan=False)`, sha256, prefix — is written four times: `idea_proposal_digest` (with a 50-line private bounded walker `_complete`), `_card_action_digest` (with its own private `_number`/`_params`/`_space`/`_node_id` validators), `stable_advisory_ref`, and `verifier_evidence_digest`; two sibling minters use ad-hoc variants — `hypothesis_statement_digest` (sha256 over a normalized string) and `run_setup_key` (md5 over a joined argv string; md5 also appears in `hypothesis_id`). The frozen preimages themselves must not change (versioned identities), but each site also re-invents the bounding/validation scaffolding around the dump, and md5 coexists with sha256 across the family, so a reader must re-derive each one's guarantees from scratch.

*Recommendation:* Add one `canonical_json_digest(payload, *, prefix)` helper (dump+hash only, no bounding) and route the non-frozen call sites through it; leave the frozen v1/v2 preimage builders byte-identical but have them call the shared dump/hash tail. Document per-site why md5 remains where it does.

#### CO-09 · LOW · inconsistency · effort: small

**Eight subtly different 'usable finite number' predicates across core**

*Locations:* `looplab/core/fitness.py:31-41`, `looplab/core/comparison.py:144-154`, `looplab/core/profile.py:10-26`, `looplab/core/parse.py:23-41`, `looplab/core/llm.py:112-120`, `looplab/core/tracing.py:320-327`, `looplab/core/models.py:445-454`, `looplab/core/models.py:126-147`

*Evidence:* core carries at least eight scalar-coercion/finiteness predicates with slightly different contracts: `fitness.is_usable_metric` (isinstance int/float, not bool, finite), `comparison.finite_measurement` (`type(value) not in {int, float}` — rejects subclasses), `profile._is_number` (same intent, hand-rolled inf check), `parse.to_float/to_int` (which claims to be "The one spelling of scalar coercion previously re-implemented per module"), `llm._safe_token_count` (`type(value) is not int`, int64 bound), `tracing._token_int` (coercing, clamping), `models._resource_int` (int-or-integral-float, int31 bound) and `models.safe_lesson_node_count` (adds decimal-string parsing). Several differences are deliberate (strict durable readers vs lax telemetry) and individually documented, but there is no map of which contract applies where, and parse.py's "one spelling" claim is no longer true.

*Recommendation:* Not a merge-everything item — the strict/lax split is real. Consolidate the genuinely identical ones (profile._is_number ≈ is_usable_metric; tracing._token_int ≈ a clamping variant of llm._safe_token_count) into parse.py or fitness.py, and fix parse.py's stale 'one spelling' docstring to enumerate the intentional strict variants.

#### CO-10 · LOW · over-engineering · effort: small

**llm.py re-export shim freezes ~32 private helper names and its monkeypatch claim is subtly wrong**

*Locations:* `looplab/core/llm.py:59-69`, `looplab/core/llm_streaming.py:6-9`, `looplab/core/llm_streaming.py:123`, `looplab/core/llm_streaming.py:257`

*Evidence:* llm.py re-imports ~32 underscore-private names from the three split siblings so "tests and callers import/monkeypatch them THROUGH this module" and both paths "keep resolving to the SAME objects". The 'same objects' claim holds for reads, but the monkeypatch claim does not hold for intra-sibling calls: `_stream_with_idle_guard` calls `_stream_raw_socket` through llm_streaming's own namespace (llm_streaming.py:123), so patching `looplab.core.llm._stream_raw_socket` rebinds only llm.py's alias and never reaches the live call — the exact silent-no-op failure mode the project's registry-guard convention exists to prevent, here with no guard. The shim also permanently publishes private helpers (`_backoff`, `_err_body`, `_tool_call_slot`, ...) as de-facto API surface of core.llm. The split itself is documented and sound; the blanket private re-export is the accidental-complexity part.

*Recommendation:* Trim the re-export list to the names tests actually import (grep-driven), patch the remaining tests to import from the owning sibling, and correct the docstrings' monkeypatch claim (patch the sibling module for intra-module call sites). Alternatively add a small source-scan test asserting every re-exported name is referenced somewhere outside core/, so the list can shrink safely over time.

#### CO-11 · LOW · dead-code · effort: small

**Speculative/no-consumer projections retained in hot models: grouped_beliefs and selection_key**

*Locations:* `looplab/core/models.py:2079-2139`, `looplab/core/models.py:1302-1308`, `looplab/core/fitness.py:159-165`

*Evidence:* `RunState.grouped_beliefs()` is a 60-line projection whose own banner says it is "AVAILABLE for a future UI / lessons / verdict view — it currently has no production consumer"; grep confirms the only callers are tests/test_cards.py and docs. `SearchFitness.selection_key`'s docstring likewise states "retained as the plain-tuple reference (no non-test callers today...)" — though it is in fact still used internally by rank_promotion/ci_tie_set/best_ci, so only the 'reference' framing is stale, not the code. grouped_beliefs is true speculative generality: 60 lines plus 4 tests maintained for a consumer that may never come, inside the repo's largest module.

*Recommendation:* Either wire grouped_beliefs to its intended consumer or move it (and its tests) out of RunState into the events/ projection layer where the other derived views live; fix selection_key's stale 'no callers' note.

#### CO-12 · LOW · other · effort: small

**Stale load-bearing REVIEW comment in atomicio: 'zero production callers' is now false**

*Locations:* `looplab/core/atomicio.py:298-311`, `looplab/core/run_reset.py:116`, `looplab/core/run_deletion.py:186`, `looplab/serve/reset_route.py:326`

*Evidence:* strict_atomic_write_bytes carries a REVIEW(2026-07-16) comment whose point (2) asserts "ZERO PRODUCTION CALLERS: the actual paid-record writers ... never route through this helper, so the Windows write-through publication added for them protects nothing yet." Grep shows strict_atomic_write_text/bytes now have many production callers: core/run_reset.py:116, core/run_deletion.py:186, serve/reset_route.py:326, serve/routers/control.py:546,789, serve/routers/reports.py (4 sites), serve/deletion_transaction.py:167, search/speculation_quality.py:2471. In a codebase whose stated convention is "comments are load-bearing" and "stale docs are treated as a bug", a security-adjacent durability helper describing itself as unused misleads exactly the reviewer that comment was written for. Points (1) and (3) of the same comment (indeterminate postcondition, Windows race/leak) remain open and undocumented in the docstring proper.

*Recommendation:* Update the REVIEW comment: delete point (2), promote point (1)'s 'exception means INDETERMINATE' into the docstring contract, and file/point at an issue for point (3)'s Windows temp-dir leak.

#### CO-13 · LOW · flat-code · effort: small

**_check_trust_gate is a misnamed grab-bag validator for nine unrelated enum fields**

*Locations:* `looplab/core/config.py:1322-1372`

*Evidence:* The model_validator named `_check_trust_gate` validates trust_gate, merge_mode, novelty_mode, strategist_backend, eval_trust_mode, seed_mode, backend, developer_backend and llm_parser — nine independent closed-vocabulary fields — as a linear if-chain of hand-written `raise ValueError` blocks, each repeating the same "must be a|b|c, got {!r}" message format. New enum-ish fields keep being appended here (the comment trail shows three accretion waves).

*Recommendation:* Replace with a declarative `_ENUM_FIELDS = {"trust_gate": ("audit","gate","block"), ...}` table iterated by one loop (message format preserved), with the two lazy-registry cases (developer_backend, llm_parser) resolved via callables in the same table. Rename to `_check_enum_fields`.


### 4.6 Serve — non-router modules

Scope: `looplab/serve/`: run_commands.py, command_observation.py, engine_proc.py, reset/deletion, capability stores, TUI.

**Reviewer assessment.** The serve/ layer is a disciplined composition root (server.py) over an AppState bag plus a set of hardened, receipt-driven subsystems (durable commands, reset/deletion transactions, capability stores, bounded projections). Layering is clean — the engine never imports serve, and serve reuses events/ projections rather than re-folding its own — and the why-comment/registry discipline is exceptional. The dominant structural problems are concentration and repetition: run_commands.py has accreted into a 4,100-line god-module containing five separable subsystems and several giant flat dispatch chains, and the same defensive micro-machinery (canonical run-path validation, reparse/symlink checks, Windows-reserved names, no-replace durable renames, per-path lock registries, file-rewrite race fences) has been re-implemented 4–7 times across sibling modules with subtle variations.

**Strengths worth preserving:**

- Strict layering honored: serve/ composes canonical events/ projections (fold, comment_projection, traceview, digest) and never grows a second source of truth; the engine never imports serve — attention.py, log_pages.py and reviews routers are pure read projections over events.jsonl.
- command_observation.py is a well-engineered incremental observation index (copy-on-write immutable snapshots, blake2b probe fences for same-size rewrites, white-box ObservationMetrics) that demonstrably removed quadratic log re-parsing from the command hot path.
- Security allowlists are executable contracts: protocol.py CONTROL_EVENTS is a frozenset, run_commands asserts CONTROL_SPECS/CONTROL_DATA_FIELDS cover it exactly, and public_cards/reviews enforce completeness receipts at projection time so redaction can never silently invalidate a coverage claim.
- Why-comments are consistently load-bearing: almost every defensive branch names the exact race, CVE-class hazard, or pinning test it closes (e.g. the POSIX no-unlink rationale in sweep_stale_lifecycle_locks, the seq==0 sort-key note in routers/attention.py).
- server.py after the BACKLOG §4 split is a genuinely thin assembly module: middleware, auth, router order, static mounts — with historical re-export/patch seams documented rather than duplicated.

#### SC-01 · HIGH · under-decomposition · effort: large

**run_commands.py is a 4,103-line god-module spanning five separable subsystems**

*Locations:* `looplab/serve/run_commands.py:1`, `looplab/serve/run_commands.py:244-392`, `looplab/serve/run_commands.py:485-1259`, `looplab/serve/run_commands.py:1701-1994`, `looplab/serve/run_commands.py:1996-2069`, `looplab/serve/run_commands.py:3642-4103`

*Evidence:* One file contains: (1) OS process-identity probing incl. raw ctypes GetProcessTimes and /proc parsing (_process_alive/_process_identity, lines 244-392); (2) the ~775-line normalize_control payload validator (485-1259); (3) spawn-lease/quarantine/start-record sidecar machinery (~1345-1994); (4) the cross-process file-lock sequencer with msvcrt/fcntl branches (1996-2069); (5) RunCommandService's record store, reconciliation, and the ~460-line _execute worker state machine (3642-4103). These have different collaborators (hardware probing vs HTTP validation vs event-store CAS) and different test surfaces, but share one namespace and internal private helpers.

*Recommendation:* Split along the existing seams: process_identity.py (PID/identity probes), control_validation.py (normalize_control + the CONTROL_* tables), spawn_leases.py (claim/quarantine/start-record sidecars), and keep RunCommandService as the orchestrator. All helpers are already module-level functions or self-contained methods, so this is mostly mechanical with re-exports for test patch seams.

#### SC-02 · HIGH · flat-code · effort: large

**normalize_control is a ~775-line flat if/elif chain that re-spells the per-event registries three more times**

*Locations:* `looplab/serve/run_commands.py:485-1259`, `looplab/serve/run_commands.py:134-173`, `looplab/serve/run_commands.py:927-929`, `looplab/serve/run_commands.py:1057-1065`, `looplab/serve/run_commands.py:2706-2843`, `looplab/serve/run_commands.py:2901-2975`

*Evidence:* The file already has two per-event registries (CONTROL_SPECS, CONTROL_DATA_FIELDS with equality assertions), yet event-specific behavior is then implemented as three separate giant if/elif chains: normalize_control (485-1259, one branch per event type, some >100 lines), _collaboration_precondition (2706-2843, a second per-type chain re-checking cards/comments/nodes), and _decision (2901-2975, a third per-type chain). Field allowlists are spelled twice inside the same file: EV_BUDGET_EXTEND's field tuple at 142-144 is repeated verbatim at 928-929, and EV_INJECT_NODE's allowed set at 150-152 is re-declared as allowed_inject at 1058-1061 (already drifted: the second omits source_run/source_node because they were popped earlier — invisible coupling).

*Recommendation:* Extend ControlSpec into a real per-event strategy record: {event_type, engine_policy, postcondition, data_fields, normalize(fn), precondition(fn), decide(fn)}. The existing set-equality assertions then guarantee every event has all handlers, and the duplicate field tuples collapse into the single data_fields definition.

#### SC-03 · HIGH · duplication · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**Canonical run-path / run-id validation is implemented at least six different ways**

*Resolution (micro-helpers):* `core/pathsafe.py` now owns `is_reparse`, `WINDOWS_RESERVED` and
`filesystem_identity`; all EIGHT `_is_reparse` copies (the review found seven; `misc.py`'s
`_author_is_reparse` was a further one), all three duplicate reserved-name sets and all three
case/Unicode-identity copies now call it. `grep 'def _is_reparse\|def _author_is_reparse'
looplab/` returns nothing. Two of those copies were attribute-only and dropped the `S_ISLNK`
half — the drift the finding predicted; their callers happened to OR it in separately, so
converging removed a redundant double-check rather than fixing a live hole. **Still open:** the
six full `validate_run_child`-shaped validators, which carry per-caller HTTP error vocabularies.

*Locations:* `looplab/serve/appstate.py:142-202`, `looplab/serve/run_commands.py:1416-1451`, `looplab/serve/run_commands.py:2071-2097`, `looplab/serve/reset_route.py:924-949`, `looplab/serve/deletion_service.py:67-117`, `looplab/serve/launch.py:63-110`, `looplab/serve/scope_sources.py:242-260`

*Evidence:* The invariant 'a run is a canonical direct child of root, not a symlink/reparse/junction, with a regular in-run events.jsonl, not a reserved service name' is re-implemented in AppState.run_dir, RunCommandService.validate_paths + run_generation_if_present (its own lstat/reparse/resolve block), resolve_active_claims/resolve_spawn_claim ('canonical direct child' inline at 1627-1629/1934-1936), durable_reset_run's inline 25-line predicate, deletion_service._plain_run_path/_strict_existing_run, launch.safe_run_dir, and scope_sources._run_path. Supporting micro-helpers are also copy-pasted: _is_reparse exists 7 times (grep: core/run_reset.py:47, core/run_deletion.py:102, serve/deletion_transaction.py:58, serve/engine_proc.py:39, serve/scope_sources.py:130, serve/deletion_service.py:87, serve/reset_transaction.py:74), the Windows reserved-name set 4 times (run_commands.py:1153-1155, deletion_service.py:42-45, launch.py:36-40, scope_report.py:34-38), and the NFD-casefold/normcase filesystem-identity rule 3 times (run_commands.py:281-290, paid_work.py:148-155, core/run_deletion.py:50). Each copy differs slightly (some check junctions, some check st_file_attributes, some re-resolve strict/non-strict), so a hardening fix must be found and applied N times.

*Recommendation:* Create core/pathsafe.py with _is_reparse, WINDOWS_RESERVED, filesystem_identity(), and one parameterized validate_run_child(root, run_id, *, require_events, error_style) used by all seven call sites; keep the per-caller HTTP error mapping at the edges.

#### SC-04 · MEDIUM · duplication · effort: medium

**_PathLocks class is byte-identical (docstring included) in command_observation.py and log_pages.py; both files are parallel incremental log indexes**

*Locations:* `looplab/serve/command_observation.py:324-362`, `looplab/serve/log_pages.py:366-404`, `looplab/serve/command_observation.py:63-255`, `looplab/serve/log_pages.py:70-455`

*Evidence:* The ~40-line _PathLocks per-path LRU lock registry is copy-pasted verbatim between the two modules (grep confirms exactly these two occurrences). Beyond that, both modules implement the same incremental append-only-log index skeleton: an _Index dataclass with identity/metadata/revision/observed_size/valid_end/torn_tail, a _scan(handle, index, snapshot_size) that resumes from valid_end with stop-at-first-bad semantics, an OrderedDict LRU of MAX_INDEXED_RUNS=8, and rebuild fences for shrink/rewrite (probe signatures vs prefix anchors). They index different payloads (intents/acks vs byte-offset rows) but the scaffolding is ~60% overlapping.

*Recommendation:* Extract _PathLocks (and ideally the shared _Index lifecycle: identity/metadata fences, resume-from-valid_end scanning, LRU registry) into one serve/_log_index.py used by both; the payload-specific _apply_delta/_row_from stay per-module.

#### SC-05 · MEDIUM · duplication · effort: small

**ctypes no-replace durable rename duplicated between reset and deletion**

*Locations:* `looplab/serve/reset_route.py:53-99`, `looplab/serve/deletion_service.py:136-170`

*Evidence:* _durable_archive_move (reset_route) and _durable_no_replace_move (deletion_service) are near-identical ~45-line functions: same sibling-parent check, lexists no-replace guard, _windows_move_write_through on nt, ctypes renameat2 (linux, AT_FDCWD/RENAME_NOREPLACE) / renamex_np (darwin, RENAME_EXCL) declaration and call, same errno handling and strict_fsync_parent. Only the error strings differ.

*Recommendation:* Move one durable_no_replace_rename(source, destination, *, label) into core/atomicio.py next to _windows_move_write_through, which both already import.

#### SC-06 · MEDIUM · mergeable-entities · effort: large

**Reset and deletion form two parallel durable-transaction frameworks with duplicated receipt/fence machinery**

*Locations:* `looplab/serve/reset_transaction.py:1-324`, `looplab/serve/deletion_transaction.py:1-265`, `looplab/serve/reset_route.py:476-884`, `looplab/serve/deletion_service.py:371-654`, `looplab/serve/run_commands.py:2417-2529`

*Evidence:* reset_transaction.py and deletion_transaction.py each define: receipt-path derivation off srv.commands._sequence_path with the same lock-namespace check, _is_reparse + _regular_file/_regular_receipt, _validate_receipt with _RECEIPT_KEYS/immutable-field/phase tables (_IMMUTABLE_RECEIPT_FIELDS vs _IMMUTABLE_FIELDS), load-with-before/after-identity change detection, save with immutable-field enforcement, and marker/fence-to-receipt binding validators (validate_reset_binding vs _validate_fence_binding/_validate_fence_request). Their drivers (reset_route._reset_blocking, deletion_service.begin_or_resume_run_deletion) both roll a phase state machine forward under the same lock stack (commands.sequence → run_lifecycle_lock_http → engine_write_lock_http → config+events+span-index locks) and both re-spell the preflight ladder (active commands / spawn claim / finalize incomplete / liveness) that run_commands.reject_if_active and destructive_guard also implement — four spellings of the same quiescence checklist.

*Recommendation:* Extract a shared durable-operation kit: generic receipt store (validate/load/save with immutable fields + phase-transition table), fence-binding validator, and one preflight_quiescence(srv, rd, *, operation) helper. Reset/deletion keep their own phase enums and effects.

#### SC-07 · MEDIUM · under-decomposition · effort: medium

**_execute is a ~460-line worker state machine with an internally duplicated spawn-then-poll block**

*Locations:* `looplab/serve/run_commands.py:3642-4103`, `looplab/serve/run_commands.py:3826-3887`, `looplab/serve/run_commands.py:4004-4044`, `looplab/serve/run_commands.py:3813-3825`, `looplab/serve/run_commands.py:3976-4003`

*Evidence:* RunCommandService._execute handles: terminal short-circuit, reset fencing, intent verification, decision/append (with three per-event CAS variants), initial spawn, a startup poll loop, the main monitor loop with deadline sliding, mid-loop re-spawn, and final terminalization — in one function. The record-spawn-claim → Popen → record pid → poll-until-lock-or-postcondition sequence appears twice nearly verbatim (3826-3887 and 4017-4044), and the RESTART_AFTER_EXIT claim block also appears twice (3813-3825 and 3976-4003). A fix to one spawn path (e.g. the heartbeat added only in the second copy at 4037) does not automatically reach the other.

*Recommendation:* Extract _spawn_and_await_startup(rd, record, path) and _try_restart_claim(rd, record, path) helpers used by both occurrences, and split the monitor loop body from the admission phase.

#### SC-08 · MEDIUM · other · effort: small

**Unresolved embedded review marker acknowledging an O(events) full-log read on the per-command append path**

*Locations:* `looplab/serve/run_commands.py:3721-3728`, `looplab/serve/run_commands.py:3786-3790`, `looplab/serve/run_commands.py:3485-3486`

*Evidence:* A literal 'CLAUDE REVIEW: [PERF]' comment block sits in production code at 3721-3726 stating that self._events(rd) (EventStore.read_all(): parse + Event-validate every row) is executed purely to read events[-1].seq, bypassing the incremental observation index (self._observe(rd).latest_seq) that was built specifically to avoid re-parsing the whole log per command. Both call sites (3727, 3788) still do the full read. The marker is a leftover finding that was neither fixed nor converted to a normal why-comment/issue.

*Recommendation:* Replace both baselines with observation.latest_seq (the CAS expected_last_seq on append remains the correctness authority, as the comment itself notes), and remove the review-artifact comment.

*Status (post-baseline):* Fixed on `master` by commit `c92b89f` (2026-08-01, immediately after this review's baseline): both call sites now read `self._observe(rd).latest_seq` from the incremental observation index, `self._events(rd)` is gone, and the marker was replaced by a why-comment. The finding is retained as accurate at the baseline.

#### SC-09 · MEDIUM · mergeable-entities · effort: medium

**public_cards.py keeps three parallel per-field dispatch chains that must be edited in lockstep**

*Locations:* `looplab/serve/public_cards.py:630-684`, `looplab/serve/public_cards.py:879-930`, `looplab/serve/public_cards.py:945-1027`, `looplab/serve/public_cards.py:34-45`

*Evidence:* Every Card wire field is classified by membership in ~10 category sets (_TEXT_LIMITS, _REF_FIELDS, _INT_FIELDS, ...) and then dispatched through three separate if-chains: _field_value (projection), _field_projection_lossless (exactness verification, a full mirror of the projector), and _field_slice (loss counting). Complex fields additionally get paired projector/verifier functions (_cross_run/_cross_run_lossless, _steering/_steering_lossless, _card_identity/_card_identity_lossless, etc.). Adding one field requires touching _FIELDS, a category set, and up to three dispatch chains; the file's own comments record a bug this caused (matched_concept_outcome rows verified against the wrong key set, line 77-82).

*Recommendation:* Replace the category sets + three chains with a single per-field descriptor table mapping name -> {project, is_lossless, slice_units}; generic kinds (text/ref/int/list) become shared descriptor factories, complex fields keep bespoke pairs but registered in one place.

#### SC-10 · MEDIUM · inconsistency · effort: medium

**ShareStore duplicates ReviewStore's capability-link concept with weaker, inconsistent hardening**

*Locations:* `looplab/serve/assistant.py:289-411`, `looplab/serve/reviews.py:159-556`

*Evidence:* Both are one-file-per-capability bearer-token stores: sha256 token_hash (never the token), TTL bounds, revoked_at tombstones, public() views stripping the digest, resolve() with constant-time compare. ReviewStore adds a required interprocess lock, O_EXCL id reservation, abandoned-reservation healing, and a recovery/replay contract; ShareStore has only an in-process threading.Lock, no cross-process exclusion on create/revoke (two uvicorn workers can interleave revoke_session's read-modify-write), and no reservation protocol. Same concept, two implementations, materially different guarantees.

*Recommendation:* Extract the common capability-store core (digest, TTL validation, tombstone semantics, atomic publish, resolve) and have both stores parameterize it; ShareStore then inherits cross-process safety for free.

#### SC-11 · MEDIUM · inconsistency · effort: medium

**Event-log rewrite/race detection implemented six different ways across serve/**

*Locations:* `looplab/serve/command_observation.py:110-141`, `looplab/serve/log_pages.py:133-139`, `looplab/serve/routers/attention.py:96-134`, `looplab/serve/appstate.py:229-250`, `looplab/serve/appstate.py:390-420`, `looplab/serve/scope_sources.py:119-214`

*Evidence:* Six independent mechanisms detect 'the log was replaced/rewritten under me': blake2b sampled probe signatures (command_observation), mtime/ctime metadata + bounded prefix anchors (log_pages), before/after 5-tuple stat signatures with a retry loop (routers/attention), (ino,ctime,size,mtime,upto_seq,audience) cache keys (appstate.state_payload) and a different 5-tuple in trace_view._sig, and full inotify/FILE_BASIC_INFO ChangeTime capture (scope_sources). The signature tuples differ subtly (some include st_dev, some ctime_ns, some neither), each documents its own rationale, and a discovered weakness in one fence (e.g. the grow-after-rewrite hole command_observation patched at _refresh_locked) must be re-derived for each sibling independently.

*Recommendation:* Not full unification (the strength requirements genuinely differ), but a shared core file-identity toolkit: one canonical stat-signature function with named strength tiers (metadata / probe / descriptor-watch) so fixes to a tier propagate to every consumer.

#### SC-12 · LOW · duplication · effort: small

**Duplicated liveness/identity probe pairs and operator escape-hatch scaffolding inside run_commands**

*Locations:* `looplab/serve/run_commands.py:1515-1550`, `looplab/serve/run_commands.py:1552-1615`, `looplab/serve/run_commands.py:1617-1699`, `looplab/serve/run_commands.py:1926-1994`

*Evidence:* _claim_child_definitely_gone/_claim_child_exactly_alive (operating on a claim dict) and _execution_owner_definitely_gone/_execution_owner_exactly_alive (operating on a claim file) implement the same pid-state + identity-reuse decision twice. resolve_active_claims and resolve_spawn_claim repeat the same escape-hatch scaffold: exact confirmation phrase, minimum_age = max(5.0, startup_timeout*2+1) window, revalidate-owner-then-unlink, structured 409s.

*Recommendation:* One owner_liveness(row_or_path) pair taking a parsed claim dict (file loading as a thin adapter), and one guarded_claim_resolution(claims, phrase, revalidate) helper for both escape hatches.

#### SC-13 · LOW · duplication · effort: small

**Five near-identical cmd_*.json directory scanners in RunCommandService**

*Locations:* `looplab/serve/run_commands.py:2154-2195`, `looplab/serve/run_commands.py:2197-2232`, `looplab/serve/run_commands.py:2283-2317`, `looplab/serve/run_commands.py:2319-2338`, `looplab/serve/run_commands.py:2340-2381`

*Evidence:* _active_command_ids, _unresolved_equivalent, _pending_finalize_record, _active_record, and _unresolved_terminal_record each glob directory.glob("cmd_*.json"), apply per-path symlink policy (raise vs fail-closed-active, subtly different per function), _load the record, filter by status sets, and pick min/max by created_at/updated_at. ~150 lines of parallel scan loops whose symlink handling has already diverged.

*Recommendation:* One _scan_command_records(rd, *, on_symlink) generator yielding (path, record); each caller keeps only its filter and selection lambda.

#### SC-14 · MEDIUM · under-decomposition · effort: medium

**_reset_blocking is a ~410-line function nesting six lock scopes and all recovery branches inline**

*Locations:* `looplab/serve/reset_route.py:476-884`, `looplab/serve/reset_route.py:614-831`, `looplab/serve/reset_route.py:833-884`

*Evidence:* One function performs: receipt/marker discovery with a double-checked re-probe, cost flushing, ownership conflict resolution, marker-only recovery, quiescence validation, launch-evidence classification (a 5-value evidence vocabulary), receipt preparation/publication, archive roll-forward, spawn preclaim, Popen with uncertain-launch handling, and a completion poll — under a nesting of sequence → lifecycle → engine_write → (config, events-lock, span-guard) context managers reaching 6 levels of indentation. The phase logic itself is documented design, but as a single function every change must re-establish the whole lock-ordering context mentally.

*Recommendation:* Split into phase functions mirroring the receipt phases (discover_or_rejoin, restore_fence, admit_and_prepare, archive, launch, await_generation) each stating its lock preconditions in the signature/docstring; the driver keeps the lock nesting explicit and short.

#### SC-15 · LOW · under-decomposition · effort: medium

**tui.py Tui class mixes rendering, wizards, chat persistence, and a client-side command-recovery state machine; _reconcile_pending interleaves two protocols**

*Locations:* `looplab/serve/tui.py:178-1060`, `looplab/serve/tui.py:789-941`, `looplab/serve/tui.py:606-720`, `looplab/serve/tui.py:721-787`

*Evidence:* The ~880-line Tui class contains dashboard/table rendering, the genesis wizard, run-view chat, plan confirmation/application, and durable command staging/recovery. _reconcile_pending (789-941, ~153 lines) interleaves two distinct reconciliation protocols in one loop body: paid report-refresh receipts (805-859) and generic command records (860-941), each with its own transient/terminal error taxonomy; _control (721-787) and _apply_plan (606-720) repeat the staged-turn persist/print/status pattern.

*Recommendation:* Extract a CommandTracker (stage/persist/observe/reconcile one turn) used by _control, _apply_plan and _reconcile_pending, and split report-refresh reconciliation into its own method; rendering helpers can move to tui_format.

#### SC-16 · LOW · over-engineering · effort: small

**Micro over-engineering in deletion_service and run_commands helper wrappers**

*Locations:* `looplab/serve/deletion_service.py:214-215`, `looplab/serve/run_commands.py:86-87`, `looplab/serve/run_commands.py:93-129`

*Evidence:* deletion_service._storage_pending is a pure pass-through alias of _pending (def _storage_pending(receipt, code, message): return _pending(receipt, code, message)) used interchangeably with it, adding a second name for one behavior. run_commands._spec(event_type, policy, postcondition) merely calls ControlSpec(...) with the same arity, and every CONTROL_SPECS entry repeats the event type twice (as dict key and as ControlSpec.event_type), inviting key/field mismatch that nothing asserts.

*Recommendation:* Delete _storage_pending (or give it distinct behavior), drop _spec in favor of direct ControlSpec construction, and derive event_type from the mapping key (or assert key == spec.event_type).


### 4.7 Serve — routers

Scope: `looplab/serve/routers/`: reports, runs, control, boss, cross_run, assistant, misc, reviews, genesis, org, attention, collaboration.

**Reviewer assessment.** looplab/serve/routers/ is a 12.4k-line HTTP layer split from a former monolithic make_app, and the split preserved handler bodies verbatim rather than re-architecting: routers are correctly thin in places (control's reset delegates to reset_route.py, org delegates to deletion_service.py) but elsewhere entire durable-storage subsystems live inside router files — reports.py carries ~1400 lines of file-lease/fence/receipt machinery, control.py a ~640-line trace-clear state machine, runs.py a ~1000-line concept-lens ledger. The dominant systemic problem is that the (genuinely excellent) paid-work idempotency discipline was re-invented per endpoint: five-plus parallel claim/terminal/generation-fence protocols with byte-identical helper pairs, plus pervasive small-scale duplication (four _json_object router copies plus ~10 inline re-implementations, five bounded-JSON redactors, ~26 hand-built generation-conflict envelopes). Security/robustness engineering at the read boundaries is consistently strong; the maintenance risk is concentrated in duplication and under-extraction, not in correctness design.

**Strengths worth preserving:**

- Exceptional paid-work correctness discipline: every endpoint that spends money (report refresh, concept lens, scope reports, genesis, boss command) has explicit idempotency identities, durable claims before provider calls, honest 'indeterminate/ambiguous' states instead of silent rebilling, and generation fences against reset races — with the reasoning documented inline.
- Consistent security hardening at read boundaries: symlink/reparse rejection, bounded reads with truncation receipts, redact-before-truncate ordering, path-traversal guards, and allow-list projections (reviews.py, misc.py authoring, runs.py artifacts/agents_md) are applied nearly everywhere untrusted bytes cross the wire.
- Load-bearing why-comments are genuinely maintained: cache-locking rationale (attention.py; runs.py's _OP_STAGE_NAMES), splice/race analyses (collaboration.py _assert_still_current), and provenance of past fixes make the non-obvious invariants auditable.
- Bounded caches done right: attention projection cache, concept core/replay LRUs, summary cache, and scope revision caches all have explicit size ceilings, stat-identity invalidation, and documented race handling instead of unbounded dicts.
- The build_router(srv) convention with documented registration-order constraints (misc.py's catch-all ordering, __init__.py) keeps the app composition explicit and testable.

#### SR-01 · HIGH · inconsistency · effort: large

**Five parallel hand-rolled durable paid-work idempotency protocols across routers**

*Locations:* `looplab/serve/routers/boss.py:117-263`, `looplab/serve/routers/runs.py:306-397`, `looplab/serve/routers/runs.py:1061-1174`, `looplab/serve/routers/reports.py:313-1156`, `looplab/serve/routers/control.py:420-1062`, `looplab/serve/routers/control.py:1087-1198`, `looplab/serve/deletion_service.py:1`

*Evidence:* The same concept — durable claim before paid/destructive work, terminal receipt, generation fence, ambiguous/indeterminate reconciliation — is implemented independently at least five times: (1) boss.py report_refresh: `_report_refresh_ledger`/`_confirm_report_refresh_terminal`/`_record_report_refresh_failure`/`_run_report_refresh_worker` folding EV_REPORT_REFRESH_* events; (2) runs.py concept-lens: `_concept_lens_ledger` + a second stricter `_concept_lens_recovery_ledger` + `_confirm_concept_lens_terminal`/`_record_concept_lens_failure`/`_run_concept_lens_worker` folding EV_CONCEPT_LENS_* events; (3) reports.py scope actions: ~850 lines of file-based receipts, fences, OS-lock leases, retained-lease quarantine (`_write_scope_action_receipt`, `_read_reconciled_action`, `_ScopeActionLease`, `_write_scope_action_fence`); (4) control.py trace-clear write-ahead receipts (`_load_trace_clear_receipt`, `_apply_prepared_trace_clear`, `_supersede_trace_clear`); (5) control.py start records (`_reconcile_start`, `_inspect_keyed_start`) — plus the already-extracted deletion_service.py as a sixth variant. Some helpers are byte-near-identical copies: `_confirm_report_refresh_terminal` (boss.py:171-183) and `_confirm_concept_lens_terminal` (runs.py:391-398) have identical bodies (open r+b, strict_fsync, bool); the ledger folds share the same claims/terminals/unresolved shape keyed by 64-hex identity + generation.

*Recommendation:* Extract two shared services in looplab/serve/: an event-ledger paid-action protocol (claim event, terminal event, fsync-confirm, generation fence — parameterized by event types) covering report_refresh and concept-lens, and keep the file-ledger machinery of scope actions as its own module. Each new hand-rolled variant is a fresh set of crash-window bugs to re-find; the near-identical helper pairs prove the abstraction already exists implicitly.

#### SR-02 · HIGH · under-decomposition · effort: large

**reports.py is a god-module: a distributed-storage subsystem inside a router file**

*Locations:* `looplab/serve/routers/reports.py:154-1553`, `looplab/serve/routers/reports.py:2282-2475`, `looplab/serve/routers/reports.py:2570-3101`

*Evidence:* 3103 lines total. Lines ~154-1553 (~1400 lines) are module-level storage machinery with zero HTTP content: path confinement (`_validated_reports_dir`, `_confined_report_path`, `_confined_scope_root_path`), cross-platform byte-range file locks (`_open_scope_action_lease`, `_try_lock_scope_action_descriptor` with msvcrt/fcntl branches), lease markers, fences, receipt validation, and the prompt projection `_prior_learnings_index`. The endpoints themselves are enormous: `abandon_scope_report_action` is ~195 lines of nested marker/fence/lease case analysis; `generate_scope_report_ep` is ~530 lines containing five nested closures (`_stamp_scope_action_usage`, `_compute`, `_inputs_unchanged`, `_persist_terminal`, `_compute_durable`). serve/ already extracts comparable subsystems (deletion_service.py, reset_transaction.py, scope_report.py, scope_sources.py), so this file is the exception, not the pattern.

*Recommendation:* Move the lease/fence/receipt machinery to looplab/serve/scope_actions.py (or fold into scope_report.py), and `_prior_learnings_index` next to its consumers (it is a Genesis prompt projection, not a report route). The router should shrink to endpoint wiring plus the staleness GET.

#### SR-03 · HIGH · under-decomposition · effort: medium

**control.py trace-clear: ~640-line durable state machine as closures inside build_router**

*Locations:* `looplab/serve/routers/control.py:420-1062`, `looplab/serve/routers/control.py:820-1062`

*Evidence:* Seventeen nested helper closures (`_trace_clear_receipt_lstat` through `_apply_prepared_trace_clear`) plus the ~240-line `clear_node_trace` handler implement a complete write-ahead-receipt state machine (pending/succeeded/superseded, digest-CAS on spans.jsonl, recovery ownership) entirely inside `build_router`. The sibling destructive operations got dedicated modules — reset is one line delegating to `serve/reset_route.py::durable_reset_run` (control.py:413-418), deletion delegates to `deletion_service.py` (org.py:174-188) — so trace-clear is inconsistent with the codebase's own extraction pattern, and being closures it is untestable without building the whole app.

*Recommendation:* Extract to looplab/serve/trace_clear.py with the same shape as reset_route.py (`durable_clear_node_trace(srv, ...)`), leaving a one-line route.

#### SR-04 · MEDIUM · under-decomposition · effort: medium

**runs.py concept-lens subsystem (~1000 lines) with a triplicated generation-fence preamble**

*Locations:* `looplab/serve/routers/runs.py:224-441`, `looplab/serve/routers/runs.py:1176-1387`, `looplab/serve/routers/runs.py:1389-1493`, `looplab/serve/routers/runs.py:1495-1626`, `looplab/serve/routers/runs.py:1628-1740`

*Evidence:* The paid concept-lens feature spans module helpers (two ledger folds, identity/HMAC helpers, `_validated_derived_lens`) plus four endpoints. `derive_concept_lens`, `recover_concept_lens_receipt`, `abandon_recovered_concept_lens`, and `abandon_concept_lens` each repeat the same ~40-line preamble: validate expected_generation regex → `_materialize_concept_core` → enter `srv.commands.sequence(rd)` → `validate_paths` → compare `run_generation` twice (current vs expected, core vs current) with three near-identical hand-built 409 dicts per endpoint. runs.py is 2845 lines overall; this subsystem is a third of it and is conceptually independent of the read-model routes the file is named for.

*Recommendation:* Extract a `serve/concept_lens.py` service plus one `_assert_lens_generation(rd, core, expected)` helper for the preamble; the four endpoints become thin.

#### SR-05 · MEDIUM · duplication · effort: small

**_json_object body parser copy-pasted 4x at module level plus ~10 inline re-implementations**

*Locations:* `looplab/serve/routers/assistant.py:46-56`, `looplab/serve/routers/genesis.py:28-37`, `looplab/serve/routers/boss.py:452-479`, `looplab/serve/routers/org.py:18-25`, `looplab/serve/routers/control.py:222-227`, `looplab/serve/routers/control.py:293-298`, `looplab/serve/routers/misc.py:448-453`, `looplab/serve/routers/boss.py:845-849`

*Evidence:* Identical `try: await request.json() except (ValueError, UnicodeDecodeError) -> 400; isinstance dict check -> 400` exists as four module-level `_json_object` copies (genesis and assistant carry a docstring saying it 'mirrors routers/boss + control'; boss's docstring mirrors routers/control, and org's nested copy has none) and is additionally re-inlined in control.py (control, submit_command, resolve_activity_claims, resolve_start_claim, start_run, start_preflight), boss.py (chat_log_append, report_refresh), misc.py (put_settings, put_secret), and runs.py (`_concept_lens_json_body` adds only a byte cap). The comments themselves acknowledge the mirroring instead of sharing the function.

*Recommendation:* One `json_object(request, *, max_bytes=None)` helper in serve/protocol.py (or a new serve/http.py); the boss variant's extra field checks stay local.

#### SR-06 · MEDIUM · duplication · effort: medium

**Five implementations of bounded/redacted projection of untrusted JSON**

*Locations:* `looplab/serve/routers/genesis.py:45-86`, `looplab/serve/routers/misc.py:241-285`, `looplab/serve/routers/reviews.py:112-145`, `looplab/serve/routers/assistant.py:149-172`, `looplab/serve/routers/assistant.py:72-113`

*Evidence:* genesis.py `_bounded_evidence_value` and misc.py `_bounded_json_value` are near-identical ~40-line recursive walkers (shared budget list, depth cap, 32-item fanout, sorted keys, string cap + truncation flag) differing only in constants (budget 128 vs 96, depth 3 vs 2) and misc's secret-key masking; reviews.py `_scrub_json` is a third recursive scrubber (key-aware masking, collision suffixes, depth 40); assistant.py `_public_scope` and `_shared_message` are two more allow-list/redact projectors. The misc.py comment even names further siblings: `core/advisory_payloads._tree` and trust/cross_run's walk. Each copy independently re-derives the same redact-before-truncate and secret-key rules, so a fix in one (e.g. the 8d1bcda secret-key-classification fix noted in misc.py) does not propagate.

*Recommendation:* One configurable bounded-projection walker in core/redact.py (budget, depth, fanout, secret-key policy, truncation receipts as parameters); migrate the two near-identical copies first.

#### SR-07 · MEDIUM · mergeable-entities · effort: small

**cross_run.py: five concept-governance POSTs and two steward POSTs are the same endpoint modulo one function**

*Locations:* `looplab/serve/routers/cross_run.py:636-735`, `looplab/serve/routers/cross_run.py:823-877`, `looplab/serve/routers/cross_run.py:737-739`

*Evidence:* `concept_merge`, `concept_purge`, `concept_alias_clear`, `concept_split`, `concept_split_clear` each repeat verbatim: `memory_dir, portfolio_id = _portfolio(body.expected_portfolio_id)` → try record/clear helper with the same by/at/expected_revision/expected_governance_revision/action_id/require_existing kwargs → `except Exception: _raise_governance_error(exc)` → identical 5-key response envelope (~18 lines x5). `concept_steward` and `claim_steward` are likewise identical except for the revision-probe and steward function. Separately, `_iter_log` (line 737) is a wrapper whose body is exactly `yield from _read_curation_rows(path)` — pure indirection.

*Recommendation:* A `_governed_mutation(body, fn, result_key)` helper collapses the five endpoints to one-liners; parameterize the steward pair; delete `_iter_log`.

#### SR-08 · MEDIUM · other · effort: medium

**Twelve unresolved async-handler-blocks-event-loop defects, flagged in-code but unfixed**

*Locations:* `looplab/serve/routers/org.py:28-36`, `looplab/serve/routers/misc.py:466-471`, `looplab/serve/routers/boss.py:544-549`, `looplab/serve/routers/boss.py:598-603`, `looplab/serve/routers/boss.py:860-865`, `looplab/serve/routers/genesis.py:199-205`, `looplab/serve/routers/control.py:1335-1341`, `looplab/serve/routers/runs.py:1229-1235`, `looplab/serve/routers/runs.py:2780-2784`, `looplab/serve/routers/reports.py:2584-2590`, `looplab/serve/routers/assistant.py:853-858`, `looplab/serve/routers/runs.py:2112-2117`

*Evidence:* Twelve 'CLAUDE REVIEW: [PERF]' comments in the routers document `async def` handlers doing blocking work directly on the ASGI event loop: unbounded `fcntl.flock` in org project mutators and misc put_settings/put_secret; full event-log fold + fsync in boss chat/chat_log_append/report_refresh; command-sequencer + full `read_all()` in runs derive_concept_lens and start_run; global store lock + lease I/O in reports generate preflight; plus the span_io unbounded-file-scan DoS note (runs.py:2112). The correct fix pattern (anyio.to_thread.run_sync around the sequenced section) already exists in the same files (/control at control.py:229-276, submit_command, chat_compact), so this is inconsistent application of an established remedy, with every SSE tick on the worker stalling under contention.

*Recommendation:* Apply the existing to_thread offload pattern to the flagged handlers (a mechanical change per site), and bound or reject unindexed sids in span_io. Then delete the markers.

*Status (post-baseline):* Fixed on `master` by commit `c92b89f` (2026-08-01, immediately after this review's baseline): all flagged handlers now offload their blocking sections via `anyio.to_thread.run_sync` (the assistant SSE drain was inverted to a no-pool-hop loop drain), the span_io fallback scan is bounded to the index's coverage boundary, and every `CLAUDE REVIEW: [PERF]` marker was removed. Behavioural tests pin the fix. The finding is retained as accurate at the baseline.

#### SR-09 · MEDIUM · duplication · effort: small

**Generation-fence 409 envelopes hand-built ~26 times; comment-cursor error duplicated between reviews and collaboration**

*Locations:* `looplab/serve/routers/runs.py:1214-1221`, `looplab/serve/routers/runs.py:2696-2702`, `looplab/serve/routers/boss.py:876-882`, `looplab/serve/routers/control.py:916-922`, `looplab/serve/routers/reviews.py:410-429`, `looplab/serve/routers/collaboration.py:13-18`

*Evidence:* The `{"code": "run_generation_changed", "expected_generation": ..., "current_generation": ..., "message": ..., "remediation": ...}` 409 dict is hand-assembled 26 times across 9 serve files (11 in runs.py alone), each with slightly different prose — drift between copies is already visible (some include `or None` on current, some omit remediation). Similarly, collaboration.py has a `_cursor_error(exc)` helper for CommentCursorError, but reviews.py `review_comments` (lines 424-429) re-inlines the identical dict instead of importing it, and the `comment_filter_invalid` 400 dict is copied verbatim between collaboration.py:75-79 and reviews.py:411-415.

*Recommendation:* Add `generation_conflict(expected, current, *, message, remediation)` and share `_cursor_error` from one place (serve/protocol.py). This also stabilizes the wire contract the UI matches on.

#### SR-10 · MEDIUM · duplication · effort: small

**Attempt-fenced node-metrics read copy-pasted between owner and reviewer routes**

*Locations:* `looplab/serve/routers/runs.py:2039-2050`, `looplab/serve/routers/reviews.py:474-486`

*Evidence:* The three-way receipt decision — `receipt is None -> read only if attempt==0; receipt[0]==current_attempt -> read_node_metrics(since_wall_time=receipt[1]); else -> {}` — is duplicated line-for-line between `node_metrics` (runs.py) and `review_node_metrics` (reviews.py). The reviews.py comment explicitly says 'Fence on the attempt receipt exactly as the owner route (runs.py node_metrics) does', i.e. the invariant is maintained by comment discipline rather than shared code; a future receipt-format change must be fixed twice or the two surfaces silently diverge on which attempt's evidence they serve.

*Recommendation:* Extract `fenced_node_metrics(node_dir, current_attempt) -> dict` into serve/metrics_adapters.py (or core/node_evidence.py next to `metrics_attempt_receipt`); the deliberate difference (owner 409s on concurrent reset, reviewer returns empty) stays in the routes.

#### SR-11 · MEDIUM · duplication · effort: small

**Agentic emit-loop scaffolding duplicated between genesis and boss command router**

*Locations:* `looplab/serve/routers/genesis.py:334-378`, `looplab/serve/routers/boss.py:726-759`

*Evidence:* Both `_plan_agentic` (genesis) and `_route_with_tools` (boss) hand-build the same scaffolding around `drive_tool_loop`: an `emit_spec = {"type": "function", "function": {"name": "emit", ..., "parameters": Model.model_json_schema()}}` dict, a `box: dict = {}` result cell, a `_fin(args)` that filters kwargs to `Model.model_fields` and falls back to an empty model on junk, a fallback closure, and `max_turns=getattr(s, 'agent_max_turns', 0), time_budget_s=..., **loop_opts_from_settings(s)`. Only the pydantic model (_GenesisSpec vs _Plan), tools, and prompt text differ. Prompt strings are contracts (per CLAUDE.md) and must stay verbatim, but the mechanical scaffolding is pure duplication.

*Recommendation:* Add an `emit_loop(client, tools, messages, model_cls, settings, *, fallback, on_step)` helper in looplab/agents (next to drive_tool_loop) that owns emit_spec/box/_fin; both routers pass their exact prompts through unchanged.

#### SR-12 · MEDIUM · layering · effort: medium

**Router-to-router imports and side-effect late-binding seams couple the route modules**

*Locations:* `looplab/serve/routers/genesis.py:24-25`, `looplab/serve/routers/misc.py:591`, `looplab/serve/routers/runs.py:890`, `looplab/serve/routers/runs.py:925`, `looplab/serve/routers/reports.py:1582`, `looplab/serve/routers/reports.py:1410-1553`

*Evidence:* genesis.py imports `_defaults_backend_llm` from routers/control.py and `_prior_learnings_index` from routers/reports.py — private helpers of sibling routers, so router modules are no longer independent leaves. The other direction is handled by mutating srv inside build_router: misc.py sets `srv.list_tasks_fn = list_tasks`, runs.py sets `srv.list_runs_membership_fn` and `srv.list_runs_fn`, which reports.py's `_scope_run_ids` then reads (`srv.list_runs_membership_fn or srv.list_runs_fn`). These attributes exist only after the right build_router calls run, forming an implicit protocol on AppState that no type or registry guards. Both `_defaults_backend_llm` (a launch-policy predicate) and `_prior_learnings_index` (a prompt projection) are domain logic with no HTTP dependency living inside routers only for historical reasons.

*Recommendation:* Move `_defaults_backend_llm` to serve/launch.py, `_prior_learnings_index` to serve/scope_report.py, and make the run-summary/membership/tasks projections real AppState methods instead of build_router side effects.

#### SR-13 · LOW · dead-code · effort: small

**Orphaned endpoints and unused helpers**

*Locations:* `looplab/serve/routers/reports.py:418-419`, `looplab/serve/routers/genesis.py:94-133`, `looplab/serve/routers/runs.py:2825-2843`, `looplab/serve/routers/cross_run.py:56-59`

*Evidence:* `_scope_action_lease_marker_exists` (reports.py:418) has zero callers anywhere in looplab/ or tests/ (verified by repo-wide grep). `POST /api/research` (genesis.py) is referenced by no ui/src file and no TUI code — only tests exercise it; its function (LLM topic brief) is subsumed by the assistant and /api/genesis. `GET /api/runs/{run_id}/agents_md` (runs.py) likewise has no ui/src or TUI caller (grep for 'agents_md'/'AGENTS' in ui/src returns nothing) — only tests/test_server.py. `_portfolio_identity` (cross_run.py) is a compat wrapper used only by tests/test_cross_run_server.py, which could call `_resolved_portfolio_identity` directly.

*Recommendation:* Delete `_scope_action_lease_marker_exists` now. For /api/research and /agents_md, confirm no external API consumers, then remove or mark deprecated; fold `_portfolio_identity` into its test callers.

#### SR-14 · LOW · duplication · effort: small

**Dual-schema OpenAPI compatibility pattern duplicated between misc and runs config routes**

*Locations:* `looplab/serve/routers/misc.py:106-166`, `looplab/serve/routers/runs.py:141-209`

*Evidence:* misc.py defines `SettingsUpdateRequest` + `LegacySettingsUpdateRequest` (extra='allow', json_schema_extra={'not': {'required': ['settings']}}) + `_request_body_contract(*models)` producing an anyOf requestBody; runs.py re-implements the exact same trio as `RunConfigUpdateRequest` + `LegacyRunConfigUpdateRequest` + `_run_config_request_body_contract()`, including the same comment about preserving 400-vs-422 semantics. Both then re-inline the same expected_revision parse (misc `_expected_revision` vs runs' inline block at 2752-2759).

*Recommendation:* Share `_request_body_contract` and a `legacy_envelope(settings_model)` factory from one module; keep the differing revision regexes as parameters.

#### SR-15 · LOW · duplication · effort: small

**Boss LLM endpoints repeat an identical prologue/epilogue quartet**

*Locations:* `looplab/serve/routers/boss.py:557-586`, `looplab/serve/routers/boss.py:588-637`, `looplab/serve/routers/boss.py:639-684`, `looplab/serve/routers/boss.py:686-833`

*Evidence:* chat_compact, chat, suggest, and command all open with `rd = _run_dir(run_id); generation = srv.commands.run_generation(rd); body = await _json_object(request); msgs = body.get('messages') or []` and all close with the same 10-line epilogue: `except HTTPException as exc: sanitized = _sanitized_domain_http_exception(exc); if sanitized: raise sanitized; return JSONResponse({'ok': False, **_safe_boss_failure(exc)}, 200)` + the same bare-Exception soft-fail. Four copies of the error-shaping block in one file; assistant.py solved the same problem for its two turn endpoints by extracting `_begin_turn`/`_finish_turn` (documented in its module docstring), so the codebase already endorses the fix.

*Recommendation:* A `_boss_llm_call(rd, generation, fn)` wrapper (or decorator) owning the metered-client context and the two-except epilogue; each endpoint keeps only its prompt assembly.


*Status (post-baseline):* Partially addressed on `master` by commit `c92b89f`: chat/suggest/command now share an extracted `_boss_prologue` helper (run_generation fetch + fold + prompt assembly, off-thread), so the remaining duplication is chiefly the four-copy error-shaping epilogue.

### 4.8 Search

Scope: `looplab/search/`: policies, operators, concept analytics, card selection, speculation gate, wrappers.

**Reviewer assessment.** looplab/search/ is really three subsystems sharing a directory: (1) a small, high-quality core of pure, replay-safe selection primitives (policy.py's four policies + registry, operators, archive, best-of-N, the panel/surrogate/foresight researcher wrappers, hybrid_merge); (2) the PART-IV concept-analytics suite (concept_graph/coverage/lock_in/graded_novelty/research_targeting/taxonomy_dedup/novelty_recall), which is disciplined about purity and determinism but has accreted a 1,680-line god-module and three parallel synonym-merge/rename mechanisms; and (3) the Card-speculation admission machinery (card_selection + speculation_quality + scorer_fidelity + speculation_calibration, ~5,300 lines), a fail-closed receipt system whose validators mirror the engine's event writer byte-for-byte and whose whole-repo source hash — by its own inline admission — revokes every receipt on comment-only edits. The three 'pick what to try next' layers compose rather than compete (panel/foresight are mutually-exclusive idea-level wrappers wired in cli/__init__.py; card_selection is action-level and defers to the policy's forced gates), but the wrapper trio solves attribute delegation three different ways and the speculation gate is by far the largest maintenance liability in the package.

**Strengths worth preserving:**

- Purity/determinism discipline is real and consistently enforced: analytics use deterministic argmax/tie-breaks (_argmax in concept_graph, sorted-iteration fixes in project_hierarchy/project_lens), no I/O or LLM in fold-consumed paths, and replay-safety rationale is stated at each decision point.
- Fail-open advisory pattern is uniform across every LLM-dependent helper (foresight.rank, agent_merge, tag_text_llm, reexamine_failed_direction): malformed model output degrades to the deterministic prior behavior instead of blocking the loop, with NaN/inf edge cases explicitly neutralized (panel._predict, _sanitize_ranking).
- hybrid_merge.py is a genuinely shared core — one RRF retriever + one agent adjudicator reused by lesson consolidation, board dedup, novelty-recall diagnostics and the offline concept-consolidation fallback — exactly the deduplication the rest of the repo should imitate.
- Constant/registry discipline (KIND_*/META_* action vocabulary, the policy _REGISTRY, MAX_MATERIALIZED_CONCEPTS shared with replay) prevents the silent-typo no-op failure mode the event log is prone to, and comments like the META_RUNG note in card_selection show it being applied deliberately.
- Comments carry measured evidence (§21.x citations, 'verified:' claims, review provenance) that let a reader audit why each threshold, tie-break and lifecycle filter exists — rare and valuable in code this dense.

#### SE-01 · HIGH · over-engineering · effort: large

**Speculation-gate stack (~3,450 lines) is a byte-level mirror of the engine writer with a whole-repo source hash that revokes its own receipts**

*Locations:* `looplab/search/speculation_quality.py:1945-1985`, `looplab/search/speculation_quality.py:759-831`, `looplab/search/speculation_quality.py:480-586`, `looplab/search/speculation_quality.py:2568-2581`, `looplab/search/scorer_fidelity.py:1-557`, `looplab/search/speculation_calibration.py:1-267`

*Evidence:* speculation_quality.py (2,615) + scorer_fidelity.py (556) + speculation_calibration.py (267) exist solely to admit one Settings knob (speculation_depth>0). The validators re-implement the engine writer byte-for-byte: _validate_calibration_terminal hardcodes the exact 12-event finalization suffix and exact payload dicts (expected_types tuple at :759-772, expected_tail_data at :821-830); _validate_calibration_setup re-derives Engine._setup_phase's orjson config_hash/setup_manifest (:538-561). Any engine finalize/setup change breaks the gate in lockstep. speculation_implementation_digest (:1966) hashes RAW BYTES of every shipped .py file, and the module's own inline comment (:1961-1965) admits this 'makes comments, formatting and line-ending conversion revoke every previously issued receipt... turns review-only commits into an operational stop/resume outage and forces six fresh GPU calibration runs after documentation edits.' validated_speculation_gate_receipt (:2568) recomputes the ENTIRE gate — re-parsing all 6 run dirs (up to 64MB events each) and rerunning the 15-case scorer matrix — on every validation.

*Recommendation:* Adopt the fix the file's own comment proposes: a versioned semantic/runtime manifest (or explicit rollout protocol version) instead of raw-byte whole-package hashing. Extract the finalization/setup 'expected event shape' constants into the engine writer modules (finalize.py, orchestrator setup) and import them, so writer and validator share one spelling instead of two hand-synced copies. Cache/receipt the revalidation instead of full recomputation per check.

#### SE-02 · MEDIUM · mergeable-entities · effort: medium

**Three researcher-wrapper classes solve the same delegation problem three different ways, each with its own history of forwarding bugs**

*Locations:* `looplab/search/panel.py:55-69`, `looplab/search/surrogate.py:28-73`, `looplab/search/foresight.py:383-397`, `looplab/cli/__init__.py:365-402`

*Evidence:* PanelResearcher forwards parser/prompts/client via three explicit @property defs (panel.py:59-69, comment: 'a missing attr here silently shadowed the run's configured PromptStore / parser / client'); SurrogateResearcher uses a _fallback_telemetry property factory for last_hyp_priority/last_foresight/last_foresight_pick (surrogate.py:28-43, comment: 'a missing attr here silently dropped the panel's hypothesis_ranked / foresight_selected audit events'); ForesightPanelResearcher uses a catch-all __getattr__ (foresight.py:383-393), which CLAUDE.md documents as a trap (typo'd reads silently resolve to base; sets shadow reads). All three also mirror hints via forward_hints and inherit bounds/space_hint. Every wrapper has accreted patch-comments fixing the same class of forwarding bug independently. agents/roles.py already ships a WrapsDeveloper base for the developer side; the researcher side has no equivalent.

*Recommendation:* Introduce one WrapsResearcher base (parity with WrapsDeveloper) owning the delegation contract (parser/prompts/client/bounds/space_hint pass-through, hint forwarding, telemetry attrs) and have all three wrappers extend it, so a forwarding rule is fixed once.

#### SE-03 · MEDIUM · duplication · effort: small

**ASHA survivor-retirement logic duplicated between policy and card_selection, with no fidelity guard covering ASHA**

*Locations:* `looplab/search/policy.py:562-577`, `looplab/search/card_selection.py:856-868`

*Evidence:* The failed_children counting, has_live_child set, and retired = {pid for pid,c in failed_children.items() if c >= _ASHA_MAX_FAILED_PROMOTIONS and pid not in has_child} computation is copy-pasted between ASHAPolicy.next_actions and card_selection._asha_lane (the constant is shared, the algorithm is not). A semantics change to retirement in one silently desyncs the Card lane from the policy authority. Unlike the GreedyTree/Card parity, which the 15-case scorer_fidelity_gate checks continuously, SCORER_FIDELITY_CASE_NAMES (scorer_fidelity.py:33-49) contains only GreedyTree cases — ASHA lane parity has no runtime guard.

*Recommendation:* Extract a shared asha_retired_survivors(state) helper in policy.py and call it from both sites; consider adding an ASHA case to the fidelity matrix or a source-scan test.

#### SE-04 · MEDIUM · excessive-logic · effort: small

**card_score rebuilds the full concept projection per candidate; the code's own review comment says to hoist it but it never was**

*Locations:* `looplab/search/card_selection.py:764-770`, `looplab/search/card_selection.py:623-634`, `looplab/search/card_selection.py:1502-1512`, `looplab/search/card_selection.py:1532-1538`

*Evidence:* card_score calls _coverage_inputs(state) — which runs current_concept_projection over every node/membership/receipt — once per candidate Card, making one election O(cards × nodes·concepts). The inline comment at :765-768 states exactly this and prescribes the fix ('Compute explored+rename once per selection snapshot and pass that immutable scoring context through every candidate score') yet the code was left unhoisted. A smaller double-work case exists on the forced-SEED path, where _forced_card_actions itself calls eligible_cards (:599) before the forced-branch call (:1505); the forced (:1504) and candidates (:1533) calls sit on mutually exclusive control paths and never both execute in one election.

*Recommendation:* Compute (explored, rename) once in _selection_after_forced_gates and thread it into card_score via a scoring-context parameter; compute eligible_cards once per _speculative_selection invocation.

#### SE-05 · MEDIUM · dead-code · effort: small

**concept_projection.py carries an unreachable 'paired core commit' fallback lattice — the owner module shipped long ago**

*Locations:* `looplab/search/concept_projection.py:19-34`, `looplab/search/concept_projection.py:49-74`, `looplab/search/concept_projection.py:108-135`, `looplab/search/concept_projection.py:144-157`

*Evidence:* The module wraps its imports in try/except ModuleNotFoundError with the comment 'the owner lands in the paired core commit; keep this commit testable alone' and 'pragma: no cover - paired core commit removes this path'. looplab/core/concepts.py exists in-tree and is imported UNCONDITIONALLY by sibling modules (concept_graph.py:51, card_selection via this module), so the fallback branches — _fallback_resolve (rename hop-cap walker), the manual rename-normalization branch of _rename_projection (:108-125), the bare-string receipt compat of _materialization_receipt (:144-157), and the _normalized_id fallback (:53-56) — can never execute. No test removes the module to exercise them (grep of tests/ shows none). ~80 lines of dead dual-implementation that must be mentally diffed against core.concepts on every read.

*Recommendation:* Delete the try/except and all fallback branches; import looplab.core.concepts unconditionally as the sibling modules already do.

#### SE-06 · LOW · duplication · effort: small

**merged-alias-id resolution fragment duplicated verbatim twice inside card_selection.py**

*Locations:* `looplab/search/card_selection.py:1244-1252`, `looplab/search/card_selection.py:1376-1396`

*Evidence:* The comprehension merged_alias_ids = {alias for card in state.cards.values() for alias in (getattr(card, 'aliases', None) or []) if isinstance(alias, str) and alias} appears byte-identically in _counterfactual_owned_selection_state and _reserved_speculative_slots, each preceded by its own multi-line comment re-explaining the same fold behavior ('the fold collapses an alias INTO its canonical... Card.merged_into is never actually assigned'). The dead-card check is likewise split between _card_administratively_dead plus per-site alias-membership checks.

*Recommendation:* Extract a merged_alias_ids(state) helper (or a card_is_dead_or_merged(state, card_id) predicate) next to _card_administratively_dead and keep the fold-behavior comment in one place.

#### SE-07 · MEDIUM · layering · effort: small

**Hidden lazy circular dependency: search.speculation_quality ↔ engine, contradicting speculation_calibration's stated purpose**

*Locations:* `looplab/search/speculation_quality.py:1589-1592`, `looplab/search/speculation_quality.py:755`, `looplab/engine/orchestrator.py:1015-1026`, `looplab/search/speculation_calibration.py:1-8`

*Evidence:* speculation_calibration.py's docstring says the scope identity lives there specifically to 'avoid importing the engine from the quality layer (and the resulting import cycle)'. Yet speculation_quality.py lazily imports looplab.engine.orchestrator (SPECULATION_CALIBRATION_PROFILE_DIGEST/_SETTINGS at :1589) and looplab.engine.finalize (incomplete_finalize_scope at :755), while engine/orchestrator.py lazily imports search.speculation_quality (:1015, :1026). The cycle exists, merely deferred to call time — the calibration module dodged it for two constants while the quality module reintroduced it for three others (the two profile constants plus incomplete_finalize_scope). No other search module imports engine.

*Recommendation:* Move SPECULATION_CALIBRATION_PROFILE_DIGEST/_SETTINGS into speculation_calibration.py (which already exists exactly to own such source-scoped identity), and move/export incomplete_finalize_scope through events/ or core so the search→engine edge disappears.

#### SE-08 · LOW · duplication · effort: small

**Three near-identical bespoke JSON/finite-number helpers re-implemented across the package (and repo)**

*Locations:* `looplab/search/speculation_calibration.py:105-144`, `looplab/search/speculation_quality.py:281-314`, `looplab/search/speculation_quality.py:422-429`, `looplab/search/coverage.py:33-47`, `looplab/serve/launch.py:54`

*Evidence:* _canonical_json (sort_keys, allow_nan=False, compact separators, same except-tuple) is defined identically in speculation_calibration.py:134 and speculation_quality.py:300; serve/launch.py:54 carries a looser sibling under the same name (no allow_nan=False, default=str, never raises). _strict_json_value (calibration :105) and _projection_value (coverage :33) are two strict-JSON normalizers with subtly different tolerances. _finite_metric exists three times under one name with two behaviors: events/replay.py:645 and speculation_quality.py:422 both return float|None, while engine/memory.py:673 returns bool — a reader grepping the name still cannot assume one contract.

*Recommendation:* Move _canonical_json and a finite-float coercer into core (e.g. core/atomicio or a small core/jsonutil) and import them; rename the bool variant in engine/memory to avoid the name collision.

#### SE-09 · MEDIUM · under-decomposition · effort: large

**concept_graph.py is a god-module: data structure + two taggers + analytics + UI view projections + LLM consolidation in one 1,680-line file**

*Locations:* `looplab/search/concept_graph.py:96-240`, `looplab/search/concept_graph.py:512-709`, `looplab/search/concept_graph.py:793-1005`, `looplab/search/concept_graph.py:1123-1341`, `looplab/search/concept_graph.py:1437-1634`

*Evidence:* Five separable responsibilities coexist: (1) the ConceptGraph DAG class + curated dense-retrieval skeleton (:96-391); (2) taggers incl. a threaded parallel-batch LLM harness with ContextVar plumbing (tag_nodes_llm :530-709); (3) pure analytics (concept_coverage/concept_metrics/uncovered_regions :793-1368); (4) serve-facing view projections — project_hierarchy, project_lens, derive_lens (an LLM lens-minting endpoint helper), default_lenses, concept_touch_counts (:1123-1341), consumed by serve/concept_frame.py, serve/routers/runs.py and tools/run_tools.py; (5) LLM vocabulary consolidation + build_concept_map orchestration (:1437-1634). The Concept dataclass itself carries a DESIGN NOTE (:75-80) admitting the dual id-prefix/axes hierarchy encoding 'produced review #10, #11 and #12' bugs and should be unified. graded_novelty, lock_in and novelty_recall reach into its private helpers (_experiment_nodes, _node_text); research_targeting and taxonomy_dedup import only public names.

*Recommendation:* Split into concept_graph.py (structure + skeleton), concept_tagging.py (heuristic + LLM taggers), concept_analytics.py (coverage/metrics/alarms), and move the lens/hierarchy projections next to the other UI projections (events/ per the package map, or serve/). Promote _experiment_nodes/_node_text to public names since three sibling modules import them.

#### SE-10 · MEDIUM · inconsistency · effort: medium

**Concept/synonym merging implemented three different ways; per-run LLM consolidation bypasses the shared agent_merge core**

*Locations:* `looplab/search/concept_graph.py:1519-1563`, `looplab/search/hybrid_merge.py:199-257`, `looplab/engine/concept_registry.py:1-60`, `looplab/search/concept_graph.py:1574-1582`

*Evidence:* hybrid_merge.agent_merge is documented as 'the quality core shared by every place LoopLab used to merge similar items', yet consolidate_concepts' client path (:1519-1552) mints its own _Pair/_Out schema and 'You consolidate a... CONCEPT vocabulary' prompt instead of reusing agent_merge, using hybrid_merge only as the no-client fallback (:1554-1563). Meanwhile engine/concept_registry.py implements a third merge system (cross-run alias/purge/split ledgers with its own normalize_key). Rename-chain resolution alone has three spellings: consolidate_concepts._final (:1574-1582, transitive with cycle fail-safe), core.concepts.resolve_concept (bounded hop-cap used by concept_projection), and concept_registry's resolver. Per-run vs cross-run separation is deliberate and documented, but three chain-resolvers and two merge-adjudication prompts is drift surface.

*Recommendation:* Route consolidate_concepts' LLM path through agent_merge (kind='concepts') or at minimum share one rename-chain resolver (core.concepts.resolve_concept) across all three sites.

#### SE-11 · MEDIUM · other · effort: medium

**Selection-bearing card_score signals are self-reported by the competing proposer — acknowledged in shipped review comments but still live**

*Locations:* `looplab/search/card_selection.py:693-696`, `looplab/search/card_selection.py:778-781`

*Evidence:* Two 'CODEX AGENT:' comments in card_score's helpers flag active defects: (1) :693-696 — a Card's coverage bonus is computed from card.concept_tags, 'self-reported by the same Researcher competing for selection. A plausible new slug earns maximal exploration bonus before independent classification'; (2) :778-781 — 'provenance-free model self-confidence is known by the foresight module to be outcome-uncorrelated, yet it carries 65% of this active selection signal' (the Pearson≈0 / §21.12 evidence lives in foresight.py's own comments) (foresight = 0.65*confidence + 0.35*priority). Both comments prescribe fixes (require an independent concept-source receipt; admit only verifier-calibrated confidence) that were not applied, so the exploit/explore stance ranks partly on gameable, known-uncorrelated inputs.

*Recommendation:* Either implement the prescribed gating (verifier-calibrated confidence via ForesightPanelResearcher.verify_score plumbing; trusted post-classification tags for coverage) or demote these terms to tie-breaks, and convert the CODEX comments into tracked issues rather than shipped annotations.

#### SE-12 · LOW · over-engineering · effort: medium

**scorer_fidelity.py ships a 15-case unit-test suite (with its own fixture factories) as production code, re-executed on every gate and receipt revalidation**

*Locations:* `looplab/search/scorer_fidelity.py:83-457`, `looplab/search/speculation_quality.py:2068-2076`, `looplab/search/speculation_quality.py:2568`

*Evidence:* _node/_ready_card/_state (:83-162) are test-fixture builders duplicating the shape of tests/ factories; _merge_cases/_ablate_cases/_bandit_cases construct 15 hand-built RunStates and assert card_next_actions == GreedyTree.next_actions with self-raising AssertionErrors on matrix drift (:64-67, :453-456). This matrix is embedded into every gate receipt and recomputed both in speculation_quality_gate (:2070) and again inside validated_speculation_gate_receipt's full recompute (:2568) — a test suite run at runtime, twice per validation. The intent (receipt embeds proof the scorer matched at issue time) is legitimate, but 556 lines of fixtures maintained in production for a property the ordinary test suite also covers is heavy.

*Recommendation:* If receipt-embedded proof must stay, shrink the runtime matrix to a digest of the offline test result or a handful of forced-gate cases; keep the full 15-case matrix in tests/ where the fixtures belong.

#### SE-13 · LOW · dead-code · effort: small

**Dead helper: _explored_concepts is never called**

*Locations:* `looplab/search/card_selection.py:637-640`

*Evidence:* _explored_concepts(state) ('Exact current concepts allowed to affect the selection-bearing coverage score') just returns _coverage_inputs(state)[0]. Repo-wide grep (looplab/ + tests/) finds only the definition — card_score calls _coverage_inputs directly (:769), so the wrapper is unreferenced.

*Recommendation:* Delete it (or use it as the hoisted scoring-context accessor when fixing the per-candidate projection rebuild).

#### SE-14 · LOW · flat-code · effort: small

**The speculative-selection API threads 8-10 identical keyword parameters through five sibling entry points**

*Locations:* `looplab/search/card_selection.py:1402-1414`, `looplab/search/card_selection.py:1549-1561`, `looplab/search/card_selection.py:1587-1598`, `looplab/search/card_selection.py:1622-1631`, `looplab/search/card_selection.py:1679-1691`

*Evidence:* speculative_card_selection_set, speculative_card_actions, speculative_raw_actions, speculative_card_is_fresh and the private _speculative_selection each re-declare and forward the same parameter bundle (state, policy, max_nodes, scoring, excluded_card_ids, ignored_pending_node_ids, include_owned_card_id, include_owned_node_id, resource_envelope, consumed_inflight). Adding one parameter (as consumed_inflight evidently was — it is absent from speculative_card_actions and speculative_raw_actions but present elsewhere, an asymmetry easy to miss) means editing five signatures.

*Recommendation:* Introduce a frozen SpeculativeSelectionContext dataclass (session-owned ids, envelope, scoring) passed once; the entry points keep only their distinguishing arguments.

#### SE-15 · LOW · inconsistency · effort: small

**Duplicate k-NN prediction shims and colliding helper names across sibling modules**

*Locations:* `looplab/search/panel.py:19-41`, `looplab/search/surrogate.py:123-131`, `looplab/search/foresight.py:208-222`, `looplab/search/graded_novelty.py:40-52`, `looplab/search/novelty_recall.py:30-39`

*Evidence:* panel._predict and SurrogateResearcher._predict both wrap the shared knn_idw with a Euclidean-distance loop over key sets; the eligibility rules deliberately differ (subset vs full-dimension) and are documented, but the distance loop itself is a third copy alongside runtime/proxy.py:60-64. Separately, two unrelated functions both named _idea_text render an Idea differently (foresight.py:208 builds predictor-facing prose; graded_novelty.py:40 builds a lowercased tagger string) and novelty_recall adds _idea_full_text on top of concept_graph._node_text — three overlapping 'text of an experiment' renderers whose divergence is load-bearing (param names vs values) but discoverable only by reading all three.

*Recommendation:* Add a shared euclidean+knn_idw predict helper in events/digest.py taking an eligibility callable; rename one _idea_text and centralize the experiment-text renderers next to _node_text with explicit variants (names-only vs with-values).


### 4.9 Agents

Scope: `looplab/agents/`: roles.py, tool_loop.py, agent.py, cli_agent.py, unified_agent.py, strategist.py, deep_research.py.

**Reviewer assessment.** The agents package is a well-tested set of LLM personas (plain + tool-using Researcher, Developer wrappers, Strategist variants, DeepResearcher, pilot/triage) built around one shared loop (tool_loop.drive_tool_loop) and a facade module (agent.py) that intentionally preserves historical import/monkeypatch seams. The registry-guarded attr contracts (DEVELOPER_OUTPUT_ATTRS / RESEARCHER_HINT_ATTRS / forward_hints) are applied consistently and the two researcher variants share prompt fragments with byte-equality tests, so the classically dangerous duplication is under control. The real accretion points are elsewhere: drive_tool_loop has grown into a ~370-line, 26-parameter function fed by stringly-typed loop_opts dicts whose merge logic is duplicated (and has already caused a silently-swallowed TypeError bug per its own comments); roles.py is a god-module mixing prompts, registries, toy backends, a 137-line CUDA calibration blob, LLM backends and validation wrappers; and strategist.py's Strategy field set is manually synchronized across five parallel encodings with no registry test, contrary to the codebase's own registry-guarded-seam convention.

**Strengths worth preserving:**

- Registry-guarded duck-typed seams are applied consistently: DEVELOPER_OUTPUT_ATTRS/RESEARCHER_ACTION_ATTRS/RESEARCHER_HINT_ATTRS (roles.py:233-272) each have two-way source-scan tests, and forward_hints (roles.py:275-290) is the single owner of the wrapper-forwarding rule used by all four wrapper chains — a rename that used to fail silently now fails a red test.
- Prompt-assembly for the two Researcher variants is genuinely shared, not copy-pasted: _researcher_capability_suffix/_hypothesis_system_suffix/_UNTRUSTED_MEMORY_RULE/_state_brief are single-sourced, code-owned suffixes are appended AFTER PromptStore render so an override cannot bypass capability gates, and tests/test_prompt_capability_sync.py + test_prompt_injection_rule.py enforce byte-level sync — the one deliberate divergence (_IDEA_SPACE_PLAIN vs _IDEA_SPACE_TOOL) is named so grep surfaces the pair.
- drive_tool_loop is real consolidation, not speculative generality: it drives the Researcher, Strategist, pilot, crash-triage, DeepResearcher, genesis, best-of-N, novelty verdicts and reports (15+ call sites across the repo), and deep_research.py documents exactly which mechanics it stopped reimplementing when it folded onto the shared loop.
- Failure-containment discipline is uniform: every LLM-facing path degrades to a safe deterministic default (bounds-filled Idea, rule strategist, policy recommendation, minimal memo) while BudgetExceeded uniformly propagates as a hard stop — the same contract at every layer.
- cli_agent.py shows careful security/robustness engineering with load-bearing why-comments: untrusted prompt text moved out of the cmd.exe-reparsed argv path (_prompt_delivery), whole-process-tree kill on timeout/exception, reject-not-strip patch gating with an explicit 'git seed unavailable' audit marker.

#### AG-01 · HIGH · under-decomposition · effort: large

**drive_tool_loop is a 370-line, 26-parameter god-function fed by stringly-typed loop_opts dicts whose merge logic is duplicated and has already caused a real bug**

*Locations:* `looplab/agents/tool_loop.py:204`, `looplab/agents/tool_loop.py:339-575`, `looplab/agents/tool_loop.py:734-772`, `looplab/agents/agent.py:156-162`, `looplab/agents/strategist.py:753-758`, `looplab/agents/deep_research.py:110-131`, `looplab/agents/deep_research.py:279-297`

*Evidence:* drive_tool_loop (tool_loop.py:204-575) takes 26 parameters (4 positional + 22 keyword-only) and its single for-loop body inlines nine concerns: history compaction, plan re-injection, prose-stall forced emit, per-call JSON-args hardening, emit-validation bounce, cancellation stubs, tracing, the identical-result repeat ledger, stuck detection, and the emit_after/emit_force convergence machinery. Options travel as an untyped dict from loop_opts_from_settings and are **-spread at each call site. Both ToolUsingResearcher.__init__ (agent.py:156-162) and ToolUsingStrategist.__init__ (strategist.py:753-758) carry near-identical comments describing the SAME past bug: context_budget_chars arriving via both the ctor kwarg and loop_opts caused a double-keyword TypeError that the broad except silently swallowed, leaving 'the agentic Researcher DEAD in the default config' — fixed per-callsite with duplicated dict.setdefault merges instead of at the source. DeepResearcher meanwhile re-plumbs 9 of the same settings as individual ctor kwargs (deep_research.py:110-131, make_deep_researcher:287-297) precisely because the dict bundle can't express 'everything except self_plan and summary_client'.

*Recommendation:* Introduce a typed LoopOptions dataclass (built once by loop_opts_from_settings, with a .replace()-style override for DeepResearcher's two divergences) so a duplicate keyword is impossible by construction and every option has one declaration point; then extract the per-tool-call execution block (args hardening, execute, cap, repeat-note, hooks — roughly lines 414-510) and the three forced-emit salvage paths into named helpers, keeping drive_tool_loop as the turn-level skeleton.

#### AG-02 · MEDIUM · flat-code · effort: medium

**roles.py is a 1058-line god-module; the 137-line CUDA-probe calibration blob in its middle belongs to the speculation subsystem, not to role backends**

*Locations:* `looplab/agents/roles.py:1-58`, `looplab/agents/roles.py:214-298`, `looplab/agents/roles.py:305-533`, `looplab/agents/roles.py:325-461`, `looplab/agents/roles.py:643-808`, `looplab/agents/roles.py:816-1058`

*Evidence:* roles.py stacks six distinct responsibilities: (1) ~180 lines of prompt-fragment constants and suffix assemblers; (2) the Researcher/Developer Protocols plus three attr registries and forward_hints/collect_hint_cues; (3) the toy offline backends; (4) SPECULATION_CUDA_PROBE_* — a 137-line ctypes CUDA driver script embedded as a string constant plus its metric-key constants (lines 325-461), maintainer-calibration-only, consumed by looplab/search/speculation_quality.py and two tests, with zero relation to 'role backends'; (5) the LLM Researcher/Developer including the 80-line _state_brief prompt builder; (6) the WrapsDeveloper mixin + 160-line ValidatingDeveloper wrapper stack. A reader hunting the wrapper contract or the hint registry must navigate past toy objectives and CUDA ctypes bindings.

*Recommendation:* Move SPECULATION_CUDA_PROBE_* (and the calibration hooks on ToyResearcher/ToyObjectiveDeveloper if feasible) into a dedicated agents/calibration.py or next to search/speculation_quality.py, preserving comments verbatim and adding the old names to the import shim; consider a follow-up split of the wrapper stack (WrapsDeveloper/ValidatingDeveloper) into roles_wrappers.py with re-exports, mirroring the tool_loop split pattern already proven here.

#### AG-03 · MEDIUM · inconsistency · effort: medium

**Strategy field set is manually synchronized across five parallel encodings with no registry/source-scan guard, contrary to the codebase's own convention** — **RESOLVED (2026-08-02)**

*Resolution:* `tests/test_strategy_field_registry.py` walks the real encodings rather than
restating them (the TypedDict via AST, `_StrategyOut.model_fields`, and the unparsed bodies of
`_assemble_strategy` / `validate_strategy` / `engine/strategy.py`), and asserts each
silent-drop direction separately: every proposable field reaches the LLM schema, every schema field
is copied by assembly, every Strategy field survives the validator whitelist, and every one is
named by `_apply_strategy`. A `NOT_LLM_PROPOSABLE` set makes "the model must not set this" an
explicit statement per key (provenance, free-form dicts, the backend factory key, the two legacy
parallel aliases), and is itself guarded against stale or contradictory entries — a stale exemption
is how a genuinely-missing schema field would hide.

Verified to have teeth by adding a field to `_StrategyOut` and the TypedDict and forgetting it
everywhere else: the exact failure the finding describes, now three red assertions.

`NOVELTY_STANCES` / `CARD_SCORING_STANCES` are deliberately NOT collapsed. A card-scoring stance
and a novelty stance are separate dials that share a vocabulary today; merging them would couple
two knobs the Strategist sets independently. Their equality is pinned instead, so a future
divergence is a decision rather than a surprise.

*Locations:* `looplab/agents/strategist.py:74-104`, `looplab/agents/strategist.py:223-310`, `looplab/agents/strategist.py:542-567`, `looplab/agents/strategist.py:657-693`, `looplab/agents/strategist.py:460-477`, `looplab/agents/strategist.py:622-634`, `looplab/agents/strategist.py:42-43`

*Evidence:* The comment at strategist.py:74-78 itself documents that adding one Strategy field requires touching the Strategy TypedDict, _StrategyOut (the LLM schema), _assemble_strategy (field-by-field copy, lines 657-693), validate_strategy (field-by-field whitelist, lines 223-310), Engine._apply_strategy, and the _STRATEGIST_SYSTEM prose — and the knob descriptions are additionally duplicated in prose between _STRATEGIST_SYSTEM (460-477) and _strategist_brief (622-634). Grep of tests/ shows only behavioral tests (test_strategist.py, test_agent_control.py etc.), no two-way source scan like the ones guarding DEVELOPER_OUTPUT_ATTRS/RESEARCHER_HINT_ATTRS/PROMPT_KEYS — so a field added to _StrategyOut but forgotten in _assemble_strategy is silently dropped (exactly the failure class CLAUDE.md says registries exist to prevent). NOVELTY_STANCES and CARD_SCORING_STANCES (lines 42-43) are also two identical tuples where one is used in a single place.

*Recommendation:* Add a registry-style guard consistent with the repo convention: e.g. a STRATEGY_FIELDS tuple plus a test asserting _StrategyOut's model_fields, _assemble_strategy's copied keys, and validate_strategy's accepted keys all cover it (a source-scan test like tests/test_role_output_contract.py). Collapse CARD_SCORING_STANCES into NOVELTY_STANCES or document why they may diverge.

#### AG-04 · LOW · duplication · effort: small

**UnifiedAgent.choose_action and triage_crash duplicate ~40 lines of identical loop scaffolding**

*Locations:* `looplab/agents/unified_agent.py:199-252`, `looplab/agents/unified_agent.py:285-325`

*Evidence:* Both methods repeat the same sequence with only content differing: pilot-client None guard, render(system prompt), messages build, inline emit_spec dict, _finalize/_fallback closures that coerce-or-default, conditional bind_state on _pilot_tools, the call-time 'from looplab.agents.agent import drive_tool_loop' seam import (choose_action with the full six-line seam comment, triage_crash with a one-line pointer to it), drive_tool_loop(max_turns=self._agent_max_turns, time_budget_s=self._agent_time_budget_s, **self._loop_opts), and the identical except BudgetExceeded raise / except Exception -> _fallback tail. Only the schema, finalize coercion and default result differ.

*Recommendation:* Extract a private _pilot_emit(self, messages, emit_spec, finalize, fallback, state=None) helper owning bind_state, the seam import, the loop kwargs and the exception tail; both methods keep their prompts/schemas/coercions (prompt strings untouched).

#### AG-05 · LOW · mergeable-entities · effort: medium

**Four near-identical implementations of the 'append an emit-now nudge, parse_structured, degrade to default' salvage path**

*Locations:* `looplab/agents/agent.py:219-228`, `looplab/agents/deep_research.py:224-236`, `looplab/agents/roles.py:728-746`, `looplab/agents/strategist.py:717-722`

*Evidence:* ToolUsingResearcher._fallback (agent.py:219-228: messages + 'Emit the Idea now.' -> parse_structured -> default draft Idea), DeepResearcher._forced (deep_research.py:224-236: messages + 'Emit the memo now.' -> parse_structured -> '(deep research produced no memo)'), LLMResearcher.propose's 2-attempt retry-with-error-feedback loop (roles.py:728-746), and LLMStrategist.decide's parse-or-rule fallback (strategist.py:717-722) are structural clones of one 'forced structured parse with a safe default' pattern; all four re-state the ParseError handling, and two of them (DeepResearcher._forced, LLMStrategist.decide) also re-state the explicit BudgetExceeded-raise.

*Recommendation:* Add one helper in tool_loop or core.parse — forced_structured(client, messages, model_cls, parser, nudge, on_fail) — keeping each caller's nudge wording and default factory as arguments (prompt strings stay byte-identical); the four sites shrink to one call each.

#### AG-06 · LOW · duplication · effort: small

**The 4-line 'except BudgetExceeded: raise / except Exception: fallback' idiom is copy-pasted at 9+ sites in the package**

*Locations:* `looplab/agents/agent.py:282-288`, `looplab/agents/deep_research.py:193-199`, `looplab/agents/deep_research.py:231-236`, `looplab/agents/strategist.py:719-722`, `looplab/agents/strategist.py:796-799`, `looplab/agents/unified_agent.py:249-252`, `looplab/agents/unified_agent.py:322-325`, `looplab/agents/tool_loop.py:602-605`, `looplab/agents/tool_loop.py:630-633`, `looplab/agents/tool_loop.py:728-731`

*Evidence:* The same containment idiom (hard budget stop propagates; anything else degrades to a caller-specific fallback) appears verbatim around every drive_tool_loop/parse call in the package, each with a re-worded why-comment. It also recurs in engine/novelty.py and engine/crash_repair.py. Each instance is small, but the rule lives in ~15 copies and a new caller can (and must remember to) re-derive it.

*Recommendation:* Provide a tiny shared wrapper (e.g. tool_loop.resilient(fn, fallback) or a context manager) documented once with the budget-propagation rule; adopt opportunistically at new call sites rather than churning every existing comment-bearing site at once.

#### AG-07 · LOW · layering · effort: medium — **RESOLVED (2026-08-02)**

**Hidden agents<->search circular dependency: search imports agents at module level while roles._state_brief imports search inside the function body**

*Locations:* `looplab/agents/roles.py:571-573`, `looplab/agents/roles.py:588`, `looplab/search/foresight.py:36`, `looplab/search/panel.py:14`, `looplab/search/best_of_n.py:11`, `looplab/search/surrogate.py:17`, `looplab/search/speculation_quality.py:95`

*Evidence:* Five search modules import looplab.agents at module level (forward_hints, WrapsDeveloper, the speculation constants), while agents/roles.py::_state_brief reaches back into looplab.search.concept_projection (line 571) via a deferred function-level import — the cycle is held apart only by import placement, with no comment marking the deferral as cycle-breaking (unlike the seam imports, which are explained). CLAUDE.md's layering rules cover core/events/serve/engine but leave agents-vs-search undefined, so nothing stops the next module-level import from closing the loop into an ImportError.

*Recommendation:* Document the intended direction (search may depend on agents; agents may only reach search via deferred imports) in CLAUDE.md or a comment at roles.py:571, or move concept_projection's prompt-facing projector down to events/ (it projects folded state, like the digests roles.py already pulls from events.digest).

*Resolution:* the direction is stated in CLAUDE.md's Layering bullet AND enforced, because "nothing
states it" was the finding — a comment alone reproduces the same failure the next time. The
deferred import in `roles._state_brief` now says why it is deferred.
`tests/test_agents_search_direction.py` fails on any module-level `agents -> search` import, and
also pins the two facts the rule DEPENDS on: that a deferred import still exists (a rule guarding
nothing goes unnoticed when it breaks) and that `search -> agents` is still module-level in at
least three places (if that stops being true, the constraint should be revisited rather than worked
around). Verified to have teeth by hoisting the `concept_projection` import to module scope.

The alternative — moving `concept_projection` down to `events/` — is not taken here: it projects
folded state, so the move is defensible, but it is a relocation of a prompt-facing projector and
belongs with the events-layer read-model work rather than with a layering guard.

#### AG-08 · LOW · inconsistency · effort: small

**Two unrelated functions named run_phase in the same package**

*Locations:* `looplab/agents/agent.py:68`, `looplab/agents/strategist.py:191`

*Evidence:* agents/agent.py::run_phase(client, tools, messages, emit_spec, ...) is the tool-loop-with-handoff wrapper (a documented patch seam used by adapters/repo_developer.py), while agents/strategist.py::run_phase(state, n_seeds) -> str classifies the run into seed/explore/exploit/confirm (imported by engine/strategy.py). Same name, same package, disjoint semantics — grep for 'run_phase' returns 30+ mixed hits and a reader must disambiguate by signature.

*Recommendation:* Rename the strategist one to classify_run_phase (or run_phase_of) with a back-compat alias; it has few importers (engine/strategy.py and tests) so the rename is cheap, unlike the seam-laden agent.run_phase which must keep its name.

#### AG-09 · LOW · over-engineering · effort: small

**agent.py facade re-exports six private tool_loop names that nothing imports through it**

*Locations:* `looplab/agents/agent.py:34-38`

*Evidence:* The re-import block forwards 17 names 'because callers and tests import AND monkeypatch them THROUGH this module', but repo-wide grep shows _PLAN_TOOL_NAME, _REPEAT_NOTE, _TRUNC_NOTE, _plan_spec, _render_plan and _summarizer are never accessed via agent.* or 'from looplab.agents.agent import' anywhere (only _force_emit, _cap_tool_result, _flatten_transcript, _handoff_ctx and the public names are). The facade is documented and legitimate, but the blanket re-export of unused privates grows the two-path ambiguity (patching tool_loop._REPEAT_NOTE vs agent._REPEAT_NOTE would already diverge for constants, since re-imported strings are rebindings, not aliases).

*Recommendation:* Trim the re-export list to the names with verified external consumers (the four privates above plus the public API), noting in the comment that new tool_loop privates are NOT auto-forwarded.

#### AG-10 · LOW · duplication · effort: small

**The 4-cue tuple for prompt cues is duplicated as a literal in both researchers and is not covered by the registry scan**

*Locations:* `looplab/agents/roles.py:678-679`, `looplab/agents/agent.py:238-239`, `looplab/agents/roles.py:242-244`

*Evidence:* Both LLMResearcher.propose and ToolUsingResearcher.propose call collect_hint_cues with the identical inline tuple ("_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint") — a strict subset of RESEARCHER_HINT_ATTRS whose docstring promises 'both researchers honor the same cues'. tests/test_hint_forwarding.py scans setattr/forwarding sites, not these two read-side literals, so a new prompt cue added to the registry must be hand-added at both call sites and a one-sided edit silently desyncs the two prompts.

*Recommendation:* Hoist the tuple to a named module constant next to RESEARCHER_HINT_ATTRS (e.g. RESEARCHER_PROMPT_CUES) referenced by both propose() methods, and have the registry docstring/test point at it.


### 4.10 Tools

Scope: `looplab/tools/`: ~25 ToolProviders.

**Reviewer assessment.** looplab/tools/ is a well-disciplined collection of ~25 duck-typed ToolProviders with a genuinely minimal shared contract (_base.py fn_spec + RESULT_CAP) and a consistently enforced never-raise/soft-fail rule, strong path/secret/SSRF hardening, and unusually honest truncation/partial-source receipts. The structural problems concentrate in the two assistant-facing modules — machine_runs_tools.py (1659 lines mixing three providers with crash-recovery journaling and a command adapter, including a tools→serve layering violation its sibling modules explicitly forbid) and cross_run_tools.py (one ~856-line _execute function) — plus systematic re-implementation of two ceremonies (the permission decide/ask/deny gate ~6x, RESULT_CAP truncation ~7x) and two parallel stacks that should merge (three foreign-run reader wrappers; RepoTools vs RepoScoutTools; MemoryTools vs CrossRunTools over the same lessons ledger with contradictory scoping). The RunStateCache and edit_match extractions show the team already knows the consolidation pattern; it just hasn't been applied to the newer accretions.

**Strengths worth preserving:**

- The ToolProvider contract is deliberately minimal and uniformly honored: fn_spec is the single schema builder, every provider soft-fails from execute() with documented rationale, and result budgets are derived from the shared RESULT_CAP constant rather than free-standing magic numbers — the loop cap and provider budgets move together by construction.
- RunStateCache (_runcache.py) is a model consolidation: previously-duplicated fold-on-demand plumbing extracted once, LRU-bounded with an explicit rationale, and its PARTIAL-SOURCE divergence receipts are threaded through every foreign-run reader so a truncated log can never masquerade as a complete run — epistemic honesty enforced in infrastructure.
- Security engineering is thorough and consistently commented: path-traversal guards with symlink re-validation on resolved targets (reposcout grep/find), secret-name filtering applied to listings as well as reads, SSRF preflight + post-connect peer verification with proxy-awareness (web.py), ReDoS pattern caps, bounded reads/downloads with wall-clock deadlines, and credential redaction at every persisted/model boundary.
- patch.SurfacePolicy is a good example of merging three previously independent write gates into one value object while explicitly documenting (not erasing) the per-site semantic differences as constructor parameters — the opposite of a lossy 'simplification'.
- Truncation honesty as a design principle: nearly every tool distinguishes 'absent' from 'not searched/cut' ('this is NOT evidence of absence', resume markers with exact continuation lines, capped-at receipts), which directly targets the model-facing failure mode of treating a partial read as a completed negative search.

#### TO-01 · HIGH · under-decomposition · effort: medium

**cross_run_tools._execute is a single ~856-line dispatch function containing eight tool implementations**

*Locations:* `looplab/tools/cross_run_tools.py:473`, `looplab/tools/cross_run_tools.py:490`, `looplab/tools/cross_run_tools.py:899`, `looplab/tools/cross_run_tools.py:1113`, `looplab/tools/cross_run_tools.py:1329`

*Evidence:* `CrossRunTools._execute` runs from line 473 to line 1329 (~856 lines) as one flat `if name == ...` chain implementing all eight tools (cross_run_prior_attempts, cross_run_claims, cross_run_atlas, cross_run_concept_map, cross_run_search, similar_runs, find_concept_slugs, concept_card) inline, each 60-220 lines, with per-branch local imports, nested helper closures (`_receipt` defined twice, lines 829 and 988), and the partial-source/scope/claim warning boilerplate (`_partial_source_warning` + `_partial_scope_warning` + `partial_note` append sequences) repeated ~10 times across branches. The fuzzy slug-scoring block (exact-normalized=1.0, substring=0.9, SequenceMatcher, >=0.55 floor) is copy-pasted between find_concept_slugs (lines 1050-1060) and concept_card (lines 1171-1181) — the comment at 1167-1170 even says 'Scoring mirrors find_concept_slugs exactly', i.e. the duplication is known but not extracted. Contrast: RunControlTools in machine_runs_tools dispatches the same way but to one private method per verb.

*Recommendation:* Split each `if name == ...` branch into a private method (as RunControlTools already does), extract the fuzzy-score function (`_slug_score(query, slug) -> float`) shared by find_concept_slugs and concept_card, and extract a small receipt-builder helper that appends the source/scope/claim partial warnings so the ~10 hand-rolled sequences collapse to one call.

#### TO-02 · HIGH · over-engineering · effort: medium

**machine_runs_tools.py is a 1659-line god-module: 3 providers + crash-recovery fence + command adapter, with _subtree defined three times**

*Locations:* `looplab/tools/machine_runs_tools.py:92`, `looplab/tools/machine_runs_tools.py:288`, `looplab/tools/machine_runs_tools.py:1442`, `looplab/tools/machine_runs_tools.py:1485`, `looplab/tools/machine_runs_tools.py:1570`, `looplab/tools/machine_runs_tools.py:1197`, `looplab/tools/machine_runs_tools.py:1299`

*Evidence:* One module holds: `_TurnMutationFence` (~150 lines of assistant-turn crash-recovery journaling), `_RunCommandAdapter` (~230 lines; its `submit` alone is ~100 lines of conflict/uncertainty handling), plus three unrelated providers (MachineRunsTools read-only, RunLauncherTools, RunControlTools) and module-level rendering helpers. Concrete duplication inside: the parent-closure `_subtree` BFS is defined verbatim three times — as a closure in `_delete_node` (1442-1451), again in `_commit_delete_node_snapshot` (1485-1494), and inlined a third time in `_purge_node_snapshot` (1570-1578). The 'stale subject changed while awaiting permission' fence (read_all → tail seq → fold → compare attempt) is duplicated between `_reset_node` (1310-1332) and `_retag_node` (1362-1382). `_settings` (1197-1297) is a flat 100-line function containing three fully independent verbs (extend_budget / set_directive / set_trust_gate) already dispatched by name at line 1148 — the outer dispatch then re-dispatches inside. RunLauncherTools.specs embeds a ~70-line prompt (intentional per CLAUDE.md prompt-contract rule, but it inflates the module further).

*Recommendation:* Split the module: `_turn_fence.py` (_TurnMutationFence), `_run_command_adapter.py`, `run_launcher_tools.py`, `run_control_tools.py`. Extract `_subtree(state, root_id)` as one module-level function used by all three delete paths, extract the stale-node fence into a helper shared by _reset_node/_retag_node, and break _settings into three methods dispatched directly from execute().

#### TO-03 · MEDIUM · layering · effort: medium

**tools -> serve layering violation in machine_runs_tools, contradicting the rule other tools modules explicitly state**

*Locations:* `looplab/tools/machine_runs_tools.py:1276`, `looplab/tools/machine_runs_tools.py:1479`, `looplab/tools/machine_runs_tools.py:49`, `looplab/tools/write_tools.py:225`, `looplab/tools/mcp_tools.py:24`

*Evidence:* `machine_runs_tools._settings` imports `looplab.serve.run_files.run_config_write_lock` (line 1276) and `_commit_delete_node_snapshot` imports four PRIVATE serve names — `_engine_alive`, `_fresh_resume_launch_pending`, `_fresh_run_launch_pending`, `_run_lifecycle_lock` from `looplab.serve.engine_proc` (line 1479). Sibling modules state the opposite rule as a design invariant: write_tools.py:225-227 ('string-matched here rather than imported because tools must never import serve (layering)') and mcp_tools.py:24-27 ('Computed locally instead of importing looplab.serve.assistant.REPO_ROOT ... the tools layer must not depend on the serve layer'). The same module even duplicates serve logic specifically to AVOID this import — `_local_run_generation` (line 49) reimplements RunCommandService's first-event hash 'without a tools -> serve import' — while two other functions in the same file import serve directly, so the module is internally inconsistent about the rule, and the duplicated hash can silently drift from the serve-side canonical one.

*Recommendation:* Inject the serve dependencies the way `alive_fn` already is: pass lifecycle-lock / launch-pending / config-write-lock callables into RunControlTools' constructor from serve/assistant.py, and have the command service expose `run_generation` so `_local_run_generation` can be deleted. This restores the one-direction rule the package's own comments assert.

#### TO-04 · MEDIUM · duplication · effort: small

**The permission-gate ceremony (decide_action -> deny message -> approver -> approval_allows -> declined message) is re-implemented ~6 times**

*Locations:* `looplab/tools/write_tools.py:215`, `looplab/tools/shell_tools.py:162`, `looplab/tools/shell_tools.py:213`, `looplab/tools/concept_tools.py:131`, `looplab/tools/knowledge_tools.py:220`, `looplab/tools/machine_runs_tools.py:1067`, `looplab/tools/mcp_tools.py:186`

*Evidence:* Six providers each hand-roll the identical three-step authorization ritual: build an action dict, call `decide_action(mode, action)`, map 'deny' to a plan-mode refusal string, and on 'ask' call `self.approver(action) or "deny"` through `approval_allows`, returning a '(declined by the user: ...)' string. Sites: WriteTools._authorize (215-228), ShellTools.exec_argv (213-220) plus a second inline copy for kill_background (162-171), ConceptGovernanceTools._gate (131-142), KnowledgeWriteTools.execute inline (220-225), RunControlTools._gate (1067-1084, with generation capture interleaved), GatedMcpTools.execute (186-191). The bodies differ only in refusal wording and tool_kind; the `approver(action) or "deny"` idiom and `approval_allows` call recur at all six sites in near-identical form (mcp_tools uses the private `_approver`/`_mode` names; machine_runs_tools adds a None-guard on the approver).

*Recommendation:* Add `perm_modes.authorize(mode, approver, action, *, deny_msg=None) -> Optional[str]` (None = proceed, else the refusal string) and have all six sites delegate, keeping per-site wording via the parameter. Removes ~60 duplicated lines and guarantees future policy changes (e.g. remembered grants) apply everywhere at once.

#### TO-05 · MEDIUM · mergeable-entities · effort: medium

**Three near-identical foreign-run reader wrappers: SiblingRunTools, AllRunsTools, and MachineRunsTools' read half**

*Locations:* `looplab/tools/run_tools.py:649`, `looplab/tools/run_tools.py:762`, `looplab/tools/run_tools.py:821`, `looplab/tools/run_tools.py:903`, `looplab/tools/machine_runs_tools.py:735`, `looplab/tools/machine_runs_tools.py:751`

*Evidence:* All three classes hold the same composition (`self._runs = RunStateCache(run_root)`; `self._reader = RunTools(max_chars=...)`) and the same delegation shape: resolve run_id via cache, return '(no such run: ...)' on miss, fetch `source_note`, `self._reader.bind_state(st, None)`, then prefix-and-forward to the inner RunTools tool. Compare SiblingRunTools._read/_code (run_tools.py:762-789), AllRunsTools._read/_code (903-919), MachineRunsTools._read_run/_read_experiment/_read_logs (machine_runs_tools.py:735-767) — the bodies differ only in the scope check and the tool name. The `_list_runs` renderers are likewise triplicated, including the identical 'PARTIAL SOURCE (read incomplete; later results unknown)' receipt string in all three (the explanatory comment is pasted verbatim in two of them; AllRunsTools carries the receipt without it) (run_tools.py:755-758, 894-896; machine_runs_tools.py:729-732). The genuine differences (SiblingRunTools' fail-closed task_id boundary, AllRunsTools' no-filter policy, MachineRunsTools' liveness column) are small policy hooks on top of ~120 duplicated lines.

*Recommendation:* Extract a small base/mixin (e.g. `_ForeignRunReader` holding the cache+reader, `_delegate(run_id, tool, args, prefix)` and one `_run_line(...)` renderer with optional live/task columns); each class keeps only its scope predicate and specs. The task-boundary semantics stay where they are — only the plumbing merges.

#### TO-06 · MEDIUM · mergeable-entities · effort: medium

**Two parallel read-only repo-browsing providers: RepoTools (knowledge_tools) vs RepoScoutTools**

*Locations:* `looplab/tools/knowledge_tools.py:52`, `looplab/tools/knowledge_tools.py:86`, `looplab/tools/knowledge_tools.py:153`, `looplab/tools/reposcout.py:92`, `looplab/tools/reposcout.py:154`

*Evidence:* `RepoTools` (Researcher-facing: repo_grep/repo_list/repo_read over named mounts) re-implements what `RepoScoutTools` (boss/Developer-facing: grep/find_files/read_file over named roots) already provides: root-confined path resolution (RepoTools._resolve at knowledge_tools.py:86-100 vs _pathsafe.resolve_within + RepoScoutTools._resolve at reposcout.py:154-162), per-hit secret filtering (knowledge_tools.py:114-117 vs reposcout.py:556-558), .git exclusion (`_readable_repo_path` vs reposcout's `_looks_secret`/`_readable`), and pagination — RepoTools.repo_read even lazily imports `RepoScoutTools._paginate` (knowledge_tools.py:153-159) to reuse the window logic. RepoScoutTools already supports named multi-roots (`named_roots`, `_disp` prefixing) which is exactly RepoTools' mount model. The two evolved independently, so their guards drift: RepoTools caps repo_grep at 40 hits with no file budget while RepoScoutTools has a 4000-file budget, skip-dirs, and overlay awareness.

*Recommendation:* Make RepoTools a thin adapter over RepoScoutTools configured with named_roots (renaming the three tool names in specs and keeping its .git-internals filter), or delete it and expose RepoScoutTools with the repo_* aliases to the Researcher. One walker, one secret gate, one budget.

#### TO-07 · MEDIUM · inconsistency · effort: medium

**MemoryTools and CrossRunTools expose the same lessons.jsonl with contradictory scoping policy and two different tokenizers**

*Locations:* `looplab/tools/memory_tools.py:208`, `looplab/tools/memory_tools.py:18`, `looplab/tools/cross_run_tools.py:205`, `looplab/tools/cross_run_tools.py:28`, `looplab/adapters/tasks.py:460`, `looplab/adapters/tasks.py:475`

*Evidence:* adapters/tasks.py binds BOTH providers to the same run when memory_dir + cross_run_read_tools are set: CrossRunTools (line 460) and MemoryTools (line 475). CrossRunTools invests heavily in fail-closed scoping of lessons.jsonl rows — `_in_scope` (cross_run_tools.py:205-245) rejects rows with missing/mismatched `direction`, wrong task family, and the current run's own rows, with extensive comments about why unknown polarity must stay invisible. MemoryTools.search_lessons (memory_tools.py:208-224) reads the same lessons.jsonl with NO direction, task, or self-run filter — only lexical overlap over a bounded recent window — so the very rows CrossRunTools deliberately hides are retrievable one tool over in the same agent's toolset. The two also use different tokenizers for the same matching job: cross_run_tools `_WORD = [^\W_]+` Unicode-aware casefold (line 28) vs memory_tools `_WORD = [a-z0-9@._]+` ASCII lower (line 18), so the same query matches different lesson sets depending on which tool the model happens to call.

*Recommendation:* Either route MemoryTools.search_lessons through the same `_in_scope` predicate (bind_state it like CrossRunTools) or fold search_lessons/recall_notes into CrossRunTools as two more verbs; at minimum share one tokenizer helper so the two surfaces agree on what matches.

#### TO-08 · MEDIUM · duplication · effort: medium

**Seven-plus independent implementations of 'fit a tool result under RESULT_CAP with an honest marker'**

*Locations:* `looplab/tools/run_tools.py:34`, `looplab/tools/shell_tools.py:61`, `looplab/tools/env_inspect.py:342`, `looplab/tools/reposcout.py:54`, `looplab/tools/memory_tools.py:47`, `looplab/tools/mcp_tools.py:44`, `looplab/tools/concept_tools.py:243`

*Evidence:* Each provider re-derives a budget from RESULT_CAP (with independently chosen headroom: -400 in cross_run_tools/concept_tools/reposcout/shell_tools, -200 in env_inspect, -160 in mcp_tools, 'reserve=100' in memory_tools) and re-implements bounded rendering with its own marker text: run_tools `_clip` (tail-keep, '…[+N earlier chars truncated]'), shell_tools `_tail` (tail-keep, '…(truncated)…'), env_inspect `_clamp` (head-keep at line boundary), reposcout `_fit_rows` (drop rows, '(N more omitted to fit the result cap)'), memory_tools `_bounded_result` (drop rows, '[RESULT_WINDOW: ...]'), mcp_tools `_clip` (head-keep, '…[mcp reply truncated — {n} chars omitted]'), concept_tools inline `append_bounded` closure, plus run_tools' repeated `while visible: ... visible.pop()` loops (lines 247-257, 407-416, 551-562). Some head-vs-tail differences are deliberate (documented per site), but the row-dropping and line-boundary-cut variants are the same algorithm rewritten five ways, each with subtly different receipts a model must learn separately (mcp_tools.py:35-38 itself notes markers should match each other).

*Recommendation:* Add two shared helpers next to RESULT_CAP in core/context_budget or _base.py — `fit_rows(header, rows, receipt, cap)` (already exists as reposcout._fit_rows; promote it) and `clip(text, cap, *, keep='head'|'tail', note)` — and migrate the row-dropping and single-string sites onto them, keeping per-site marker wording as a parameter.

#### TO-09 · MEDIUM · layering · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**cross_run_tools/concept_tools depend on ~10 underscore-private engine helpers via lazy imports, so an engine rename fails silently at runtime**

*Locations:* `looplab/tools/cross_run_tools.py:337`, `looplab/tools/cross_run_tools.py:360`, `looplab/tools/cross_run_tools.py:412`, `looplab/tools/cross_run_tools.py:537`, `looplab/tools/cross_run_tools.py:1121`, `looplab/tools/cross_run_tools.py:453`

*Evidence:* CrossRunTools lazily imports private names from engine modules throughout: `_filter_claim_source_rows`, `_claim_source_rows`, `_safe_claim_source_summary`, `_safe_research_source_summary` (engine.claims), `_capsule_rows`, `_capsule_completeness`, `_capsule_fingerprint_scope_complete`, `_dedup_valid_capsules`, `_filter_capsule_rows`, `_capsule_source_summary`, `_portfolio_concept_overview_data` (engine.memory), plus `_TOMBSTONE` in concept_tools.py:224. Because the imports are lazy (intentional, to keep the import graph acyclic per concept_tools' docstring) AND `execute` swallows every exception into the generic '(cross-run tool unavailable)' string (cross_run_tools.py:453-471, deliberately hiding exception text), renaming any of these engine privates produces no import-time error — every affected cross-run tool starts answering 'unavailable'; tests/test_cross_run_tools.py does then fail on missing content, but the failure is opaque (the generic unavailable string, not a NameError) and no registry-style source-scan pins the private-name list. This is exactly the silent-rename failure class CLAUDE.md's registry-guarded seams exist to prevent, but these seams are not registry-guarded, and an underscore prefix normally licenses engine maintainers to rename freely.

*Recommendation:* Export a small public facade from engine (e.g. `engine.memory.capsule_views` / `engine.claims.claim_views` re-exporting the needed helpers under public names), or add a two-way source-scan test (like the existing registries) pinning the private names cross_run_tools/concept_tools import.

*Resolution (2026-08-02, guard arm):* `tests/test_cross_package_private_seams.py` declares the
whole surface — **26 edges**, more than either finding counted — as
`CROSS_PACKAGE_PRIVATE_IMPORTS` (consumer package -> provider module -> names) and checks it BOTH
ways: every declared name must still resolve (so renaming an engine private another package
imports is a red test naming both ends, instead of a tool that quietly answers "(cross-run tool
unavailable)"), and every edge in the tree must be declared (so a NEW private cross-package
dependency is a decision someone writes down rather than one autocomplete makes). A third check
fails on stale entries, so the debt can only shrink visibly.

Two entries are pinned by size to keep the promotion work prioritized by pressure rather than by
whoever trips over it first: `serve/` leans on nine `events.traceview` privates, and `tools/` +
`cli/` on seven `engine.memory` capsule read-model privates — XP-01's primary promotion candidate.

The **facade arm remains open**: this makes the breakage loud, it does not make the boundary
public. Promoting the capsule/claim read-model to public names is still the recommended fix, and
`_interprocess_lock` — imported by four packages outside `events` — is still a private name doing
a public job.

#### TO-10 · LOW · dead-code · effort: small

**Dead/unused surface: perm_modes.decide() has no production caller; VectorStore.delete/rebuild are never called; RunTools.parent is stored but never read**

*Locations:* `looplab/tools/perm_modes.py:232`, `looplab/tools/vectorstore.py:39`, `looplab/tools/vectorstore.py:246`, `looplab/tools/vectorstore.py:252`, `looplab/tools/run_tools.py:66`

*Evidence:* Verified by whole-repo grep including tests/: (1) `perm_modes.decide(mode, tool_kind)` — the kind-only 'compatibility helper' — is called only from tests/test_perm_modes.py; every production site uses `decide_action`. It is labeled compatibility, but nothing is left to be compatible with. (2) `VectorStore.rebuild` (protocol + InMemoryVectorStore implementation, vectorstore.py:246-256) has zero callers anywhere in the repo; `VectorStore.delete` has zero production callers (one test exercises it: tests/test_vectorstore.py:29); the module docstring admits the persistent-backend seam is 'a documented FUTURE seam ... not a config change today', so two of the four protocol methods are speculative. (3) `RunTools.bind_state` stores `self.parent = parent` (run_tools.py:66) but no code in the repo reads a RunTools `.parent` attribute — the parent parameter exists only to satisfy the bind_state signature. Each item is small, but together they are API surface a reader must reason about for nothing.

*Recommendation:* Delete `decide()` and retarget its test at `decide_action`; drop `delete`/`rebuild` from the Protocol (re-add with the first persistent backend) or mark them explicitly unused; stop storing `parent` in RunTools (accept-and-ignore, as MachineRunsTools does).

#### TO-11 · LOW · excessive-logic · effort: small

**concept_card/find_concept_slugs re-run full-portfolio canonicalization per call inside an already-huge module (excessive per-call work + duplicated governance plumbing)**

*Locations:* `looplab/tools/cross_run_tools.py:941`, `looplab/tools/cross_run_tools.py:1142`, `looplab/tools/cross_run_tools.py:405`

*Evidence:* find_concept_slugs and concept_card each independently reload all capsules (`_all_capsules` re-reads and re-dedups concept_capsules.jsonl per call, line 405-414), then build a full `canonicalize_concepts` map over every capsule (`canonical_by_capsule` at 943-947, `canonical_caps` at 1142-1146 — the same computation with a different container shape), and re-partition scope. `_scoped_capsules` similarly recomputes for every one of the other tools. Within one agent turn calling find_concept_slugs then concept_card (the documented workflow — the follow-up is prescribed in find_concept_slugs' rendered output and in concept_card's spec), the whole portfolio is re-canonicalized twice. There is no fingerprint cache analogous to RunStateCache even though the underlying files are the same governance snapshot the call already takes.

*Recommendation:* Cache the (capsules, canonical-sets) pair keyed by (capsule file sig, taxonomy governance_revision) on the provider instance — the revision is already fetched per call — and share the canonicalization structure between the two branches once they are extracted into methods.


### 4.11 Runtime + Adapters

Scope: `looplab/runtime/` and `looplab/adapters/`.

**Reviewer assessment.** The runtime package is battle-hardened with genuinely good single-choke-point discipline: run_argv owns process management (timeout/tree-kill/env-scrub/output caps) for all three execution paths, and the watchdog/salvage logic is carefully reasoned. However command_eval.run_command_eval has accreted into a ~265-line, 23-parameter (19 keyword) god-function with two hand-mirrored branches, and the two untrusted Docker tiers duplicate their `docker run` hardening argv by comment-enforced copy-paste. On the adapters side, the TaskAdapter seam and the registry-guarded optional hooks are solid, but adapters/tasks.py has become the whole agent/LLM composition root (about 450 of its 798 lines have nothing to do with task adapters), the five synthetic demo tasks are copy-paste quintuplets missing the direction validator two other adapters have, and there is one dead registry (METRIC_READERS) whose docstring falsely claims it is shared.

**Strengths worth preserving:**

- run_argv (runtime/sandbox.py:381) is a real universal choke point: timeouts (finite_timeout), tree-kill, secret-env scrubbing, output caps and the cidfile cleanup are single-homed and demonstrably shared by SubprocessSandbox, DockerSandbox, command_eval, deps and bg_tasks — with cross-referencing comments (e.g. docker_timed_out is 'the single home of the 124-vs-137 rule').
- validate_stages / materialized_stages (runtime/command_eval.py:489,542) is a genuinely shared single definition of 'a valid stage' used at authoring time (declare_stages tool), submit time (EvalSpec validators in repo_task.py:195-207) and consume time (engine _resolve_stages, repo_developer._materialized_stage_list) — the manifest handshake structurally cannot drift.
- Security containment in read_metric/host_score is thorough and consistently rationale-documented: workdir confinement of every candidate-controlled path, mtime freshness gates against stale-workdir metric promotion, size bounds against host OOM, and the authenticated `signals` channel that defeats forgeable stall/diverge sentinels (sandbox.py:849-868, command_eval.py:72-82).
- TASK_OPTIONAL_HOOKS (adapters/tasks.py:77) with its two-way source-scan test makes the duck-typed adapter seam rename-safe in both directions; probes I checked (onboard_command in repo_developer.py:597, host_grader in engine/orchestrator.py:1206) all resolve.
- Load-bearing why-comments throughout: nearly every defensive branch cites the concrete incident or review item that motivated it (e.g. the _covered_by empty-string trap in repo_write_tools.py:58-67), which materially lowers the cost of maintaining the defensive code.

#### RA-01 · HIGH · mergeable-entities · effort: medium

**adapters/tasks.py is two modules fused: task schema/registry + the entire agent composition root**

*Locations:* `looplab/adapters/tasks.py:604-798`, `looplab/adapters/tasks.py:513-601`, `looplab/adapters/tasks.py:427-510`, `looplab/adapters/tasks.py:365-424`, `looplab/adapters/tasks.py:1-9`

*Evidence:* The module's docstring says 'TaskAdapter seam (ADR-2) + a loader for tasks', but ~450 of 798 lines are LLM/agent wiring with no task-adapter content: make_roles is a 195-line deeply-branched factory (param-search guard, in-house LLMRepoDeveloper wiring, external CliAgentDeveloper wiring incl. opencode config, PromptStore, provider assembly, sweep-offer flag, ToolUsingResearcher wrap, BestOfNDeveloper wrap, per-role client rebinding), plus build_unified_agent (89 lines), _shared_providers (66 lines), build_strategist_tools, make_developer_factory, _make_abstractor, _memora_cache_path, _set_role_client. These import agents/, search/, tools/ heavily and are the de-facto composition root of the whole role system; the actual task machinery (normalize_task/validate_task/load_task/_KINDS) is lines 86-346.

*Recommendation:* Split the role/agent factory half into e.g. looplab/agents/factory.py (make_roles, build_unified_agent, build_strategist_tools, make_developer_factory, _shared_providers and helpers) and keep re-exports in adapters/tasks.py for the many existing importers (the module already does exactly this for make_llm_client at line 361, and the _LAYOUT meta-path shim shows the repo's established pattern for safe moves). Within make_roles, extract the three developer-backend branches (in-house repo dev, external CLI dev, best-of-N wrap) into named helpers.

#### RA-02 · HIGH · under-decomposition · effort: medium

**run_command_eval is a ~265-line god-function with 23 parameters (19 keyword) and two hand-mirrored eval branches**

*Locations:* `looplab/runtime/command_eval.py:736-1001`, `looplab/runtime/command_eval.py:825-930`, `looplab/runtime/command_eval.py:891-893`, `looplab/runtime/command_eval.py:952-954`, `looplab/runtime/command_eval.py:814`, `looplab/runtime/command_eval.py:848-852`

*Evidence:* One function does: setup phase, staged-pipeline loop (~105 lines incl. stage reuse, live-band spans, health watchdog, salvage, inter-stage check_fn), the single-command branch, metric read, drift cross-check, adapter-reader trust guard, declared+auto extra metrics, constraints, trials — behind 19 keyword parameters and three nested closures (_log, _sp, _bound). The two branches duplicate the stall-window resolution expression (`stall_timeout if stall_timeout is not None else _stall_window(...)` at 891-893 and 952-954), the docker timeout fold `to = to or (is_docker and docker_timed_out(rc))` (three occurrences: 814, 894, 955), and the authenticated-signal plumbing. The fragility already bit once: the `_sig` UnboundLocalError fix at 848-852 exists because the result expression at line 1001 reads a variable bound inside whichever branch happened to run.

*Recommendation:* Extract the staged loop into a _run_stages(ctx, stages, ...) helper returning (rc, out, err, to, sig, stage_results | early RunResult), and bundle the shared execution knobs (wrap, is_docker, grace, env, cancel, log_dir, tracer, stall settings, max_output_bytes) into a small context dataclass. That removes the cross-branch variable leakage and the triplicated timeout-fold/stall-window expressions.

#### RA-03 · MEDIUM · duplication · effort: medium — **RESOLVED (2026-08-02)**

**Docker `run` hardening argv is duplicated between DockerSandbox.run and make_docker_wrap, kept in sync only by comments**

*Resolution:* `runtime/sandbox.py::docker_run_argv(image, *, network, mount_root, workdir, runtime,
gpu_args, mem, cpus, env_args, extra_mounts)` returns the hardened prefix through the image, and
`require_docker_cli(what)` owns the presence check and its message. `DockerSandbox.run` and
`make_docker_wrap` compose on it and append only their own in-container command. Every flag's reason
is documented once in the builder. Behaviour-preserving: the only change is that `DockerSandbox`'s
`-e` pairs now precede `-v`/`-w` instead of following them, and ordering among pre-image `docker
run` options is not significant.

The duplicated timed-out `RunResult` tail this entry also lists (`sandbox.py:1077-1080` vs
`1150-1153`) is NOT merged: the two differ in what "timed out" means — the subprocess tier reads
`_run_argv`'s flag, the Docker tier ORs in `docker_timed_out(rc)` for the 124/137 exit codes — and
each carries a comment explaining its own nulling. Three lines of shared shape around two different
predicates is not the drift risk the argv was.

`tests/test_docker_hardening_parity.py` drives BOTH tiers with one configuration and asserts each
boundary flag (`--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 1024`,
`--network`, `--memory`, `--cpus`, `--runtime`) is present on each and lands BEFORE the image, that
the absent `--user` stays a documented decision, that a missing docker CLI refuses loudly on both,
and that neither tier re-spells `docker run` or a hardening flag itself. Verified to have teeth by
reproducing the historical drift — dropping mem/cpus on the solution tier alone fails exactly the
solution-tier cases.

*Locations:* `looplab/runtime/sandbox.py:1107-1153`, `looplab/runtime/command_eval.py:617-715`, `looplab/runtime/sandbox.py:1110-1113`, `looplab/runtime/command_eval.py:636-639`, `looplab/runtime/sandbox.py:1077-1080`, `looplab/runtime/sandbox.py:1150-1153`

*Evidence:* Both untrusted tiers assemble near-identical argv: docker-CLI presence check with the same error text ('trust_mode=... needs the docker CLI...'), `docker run --rm --network X`, `--runtime`, gpu_args from docker_gpu_argv, `--pids-limit 1024` (same 'fork-bomb guard (review C1)' comment twice), `--cap-drop ALL --security-opt no-new-privileges`, `--memory/--cpus`, `-v root:/work -w`, `-e` env forwarding via docker_gpu_env, and the in-container `timeout -k 5 <secs>` prefix. command_eval.py:671-677 literally says 'mirror sandbox.DockerSandbox.run so BOTH untrusted Docker tiers...' — and sandbox.py:1096-1097 records that the caps had already drifted once ('before this the solution.py path had NO memory/cpu bound and ran with default caps as root'). Additionally SubprocessSandbox.run and DockerSandbox.run duplicate the identical timed-out RunResult tail (metric/extra_metrics/trials nulled on timeout) at 1077-1080 vs 1150-1153.

*Recommendation:* Extract a shared builder in sandbox.py, e.g. docker_run_base(image, network, runtime, gpu_args, mem, cpus, mount_root, env) -> list[str] plus a require_docker_cli(context) helper, and have both DockerSandbox.run and make_docker_wrap compose on top of it. Security hardening flags should have exactly one home.

#### RA-04 · MEDIUM · dead-code · effort: small

**METRIC_READERS is dead code with a false docstring; metric-reader kinds are maintained in three parallel places**

*Locations:* `looplab/adapters/tasks.py:99-101`, `looplab/adapters/repo_task.py:173-187`, `looplab/runtime/command_eval.py:187-310`

*Evidence:* METRIC_READERS = {"stdout_json", "stdout_regex", "file_json", "file_regex", "host_score", "adapter", "auto"} has zero consumers anywhere in looplab/ or tests/ (repo-wide grep: only its own definition). Its comment claims 'Shared by normalize + the EvalSpec.metric.reader validator', but normalize_task only checks reader == "auto" and EvalSpec._valid_metric_kind (repo_task.py:179) hardcodes its own local _KINDS set with the same values minus 'auto'. read_metric's if-chain is a third hand-maintained enumeration of the same kinds. Adding a new reader kind today requires touching read_metric and _valid_metric_kind while the ostensible registry stays stale.

*Recommendation:* Either make METRIC_READERS the real single source (import it in _valid_metric_kind as METRIC_READERS - {"auto"}, and derive read_metric's dispatch from it) or delete it and fix the docstring. The repo's own registry-guarded-seam convention (CLAUDE.md) argues for the former.

#### RA-05 · MEDIUM · flat-code · effort: small

**read_metric is a 120-line flat if-chain with the security-critical workdir-confinement guard copy-pasted three times**

*Locations:* `looplab/runtime/command_eval.py:187-310`, `looplab/runtime/command_eval.py:209-215`, `looplab/runtime/command_eval.py:240-245`, `looplab/runtime/command_eval.py:292-297`

*Evidence:* Six reader kinds are handled in one linear if-chain, and the containment idiom `if not _is_within(X.resolve(), Path(workdir).resolve()): return None` wrapped in `try/except (OSError, ValueError)` appears verbatim three times (file_json/file_regex path, host_score predictions path, adapter module path). This is the guard that stops answer-key reads and arbitrary host-code exec; three hand-copies means a future fourth reader can plausibly forget it.

*Recommendation:* Extract one _confined(workdir, rel) -> Optional[Path] helper (resolve + _is_within + exception handling) used by all file-touching branches, and consider a {kind: reader_fn} dispatch table so a new reader kind must go through the table (and the confinement helper) rather than a new elif.

#### RA-06 · MEDIUM · mergeable-entities · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**Five synthetic task adapters are copy-paste skeletons, and the direction validator exists in only 2 of 9 task models**

*Resolution (the correctness half):* `core/models.validate_direction` is attached to all NINE
registered task models — verified by `tests/test_task_direction_validator.py`, which DISCOVERS the
models by import rather than listing them, so a new adapter that forgets it fails. `mlebench_real`
opts "auto" in explicitly (it resolves that from the grader before any comparison) rather than
every model loosening. **Still open:** the `SyntheticTaskBase` / `PerturbResearcher` collapse of
the five copy-paste skeletons, which the finding itself rates lower priority.

*Locations:* `looplab/adapters/toytask.py:17-45`, `looplab/adapters/regression.py:109-241`, `looplab/adapters/classification.py:80-140`, `looplab/adapters/timeseries.py:67-132`, `looplab/adapters/mlebench.py:146-284`, `looplab/adapters/repo_task.py:319-324`, `looplab/adapters/dataset_task.py:167-171`

*Evidence:* regression/classification/timeseries/mlebench each re-implement the same trio (toytask instead wires the shared ToyResearcher/ToyObjectiveDeveloper from agents/roles.py): a seeded-Random Researcher whose propose() is draft-random-params-else-perturb-parent (RegressionResearcher, ClassificationResearcher, TimeSeriesResearcher, MLEBenchResearcher, RepoParamResearcher in repo_task.py:234 differ only in the perturbation arithmetic), a Developer whose implement() is str.format over an embedded template, and a pydantic Task repeating kind/id/goal/direction/comparison_contract/seed + _data() + columns() + build_roles() + llm_roles(). regression.py additionally holds two near-identical task classes (RegressionTask/CodeRegressionTask share _data/columns and all data fields). Meanwhile the `direction` field validator ('silently treating typos as minimize flips the objective') exists ONLY in RepoTask (repo_task.py:319) and DatasetTask (dataset_task.py:167) — grep confirms the other seven of the nine registered task models accept direction="mxa" silently, causing exactly the objective-flip the validator's own comment warns about.

*Recommendation:* At minimum, hoist a shared direction validator (a mixin or Annotated Literal["min","max"] type in core) onto every task model — small change, real correctness payoff. Optionally extract a SyntheticTaskBase (common fields + columns/build_roles conventions) and a parameterized PerturbResearcher to collapse the five skeletons; these are stable demo tasks so this half is lower priority.

#### RA-07 · MEDIUM · under-decomposition · effort: medium

**LLMRepoDeveloper._run is a 185-line orchestration block with three spellings of the pipeline note and duplicated epilogue**

*Locations:* `looplab/adapters/repo_developer.py:735-919`, `looplab/adapters/repo_developer.py:843-866`, `looplab/adapters/repo_developer.py:619-634`, `looplab/adapters/repo_developer.py:656-682`, `looplab/adapters/repo_developer.py:907-918`

*Evidence:* _run handles base preload, system-prompt assembly (7 concatenated sections), repair-note assembly, the stages-phase decision tree (operator stages vs protected manifest vs declared vs carried-over parent manifest), the three-way stage_note construction (843-866), plan phase, per-step implement loop with error collection, an exception trap, and last_files/footprint bookkeeping — the latter duplicated byte-for-byte in the except path (908-912) and success path (914-918). 'Tell the model the actual pipeline' is implemented three separate ways: _repair_stage_note (619), the inline fresh-repo notes (848-866), and _stages_user's contract text (656-682). The class plus module-level prompt constants total ~950 lines even after the RepoWriteTools split.

*Recommendation:* Extract the fresh-repo stages/plan/implement orchestration (lines 806-899) into a _run_fresh(idea, write, system, user) method and a _stage_note(op_stages, declared, carried_over, manifest_protected) helper that unifies the three note builders; move the duplicated last_files/footprint epilogue into a finally block or a single _record_result(write, idea) call.

#### RA-08 · LOW · layering · effort: small

**runtime/ is a grab-bag: three of eight modules are not runtime-execution code**

*Locations:* `looplab/runtime/proxy.py:1-88`, `looplab/runtime/jupyter.py:1-63`, `looplab/runtime/notebook.py:1-33`, `looplab/search/surrogate.py:46-73`

*Evidence:* proxy.py (ProxyScorer) is a pure search policy over folded RunState — it imports only core.models and events.digest.knn_idw, no process/sandbox machinery — and is the direct sibling of search/surrogate.py's SurrogateResearcher (same knn_idw IDW core over the same (params->metric) history; the deliberate _numeric divergence is documented, but the two files living in different packages hides that relationship). jupyter.py is a jupyter-server-proxy launch spec for the serve UI (it even resolves ui/public/looplab.svg). notebook.py is an export renderer consumed only by cli/export_cmds.py. The remaining modules (sandbox, command_eval, deps, bg_tasks, plus __init__) are the actual runtime tier.

*Recommendation:* Move proxy.py to search/ (next to surrogate.py), notebook.py to the events/export or cli layer, and jupyter.py to serve/ — using the established _LAYOUT back-compat alias mechanism so old import paths keep resolving. Low urgency, but it makes the runtime package's contract ('process execution and sandboxing') true.

#### RA-09 · LOW · flat-code · effort: medium

**bg_tasks task records are raw dicts with a hand-rolled lock protocol re-explained at five call sites**

*Locations:* `looplab/runtime/bg_tasks.py:108-117`, `looplab/runtime/bg_tasks.py:178-223`, `looplab/runtime/bg_tasks.py:304-311`, `looplab/runtime/bg_tasks.py:320-327`, `looplab/runtime/bg_tasks.py:336-353`

*Evidence:* Each task is a dict {"proc","log","fh","cursor","cmd","cwd","timed_out","deadline_lock","deadline"} (+ later "closed") indexed by string keys from six methods. The subtle F10 concurrency contract — poll() reaps and frees the PID, so every reaping poll must hold deadline_lock, while pre-checks must read returncode only — is enforced by convention and re-documented in comment blocks at _enforce_deadline (185-192), the sweep (149-156), read (305-311), list (322), and kill (337-353). The invariant lives in prose, not structure.

*Recommendation:* Introduce a small _BgTask class owning proc/log/fh/cursor/deadline_lock with methods like locked_poll(), reap(), enforce_deadline() so the lock discipline is encoded once; the five comment blocks collapse into one docstring. Behavior-preserving refactor.

#### RA-10 · LOW · duplication · effort: small

**mle-bench registry/data-dir resolution and is_prepared are triplicated across the three mlebench modules**

*Locations:* `looplab/adapters/mlebench_real.py:41-69`, `looplab/adapters/mlebench_prep.py:42-51`, `looplab/adapters/mlebench_grade.py:32-34`

*Evidence:* The idiom `registry if not data_dir else registry.set_data_dir(Path(data_dir).resolve())` appears three times (mlebench_real._competition, mlebench_prep._registry, inline in mlebench_grade.grade), and is_prepared(competition_id, data_dir) is defined twice with identical bodies (mlebench_real.py:66-69 and mlebench_prep.py:47-51). mlebench_real.py even claims _competition is 'The single place the registry/data-dir resolution lives' — untrue given the other two copies.

*Recommendation:* Have mlebench_prep and mlebench_grade import _competition (and is_prepared) from mlebench_real, or move both helpers into a tiny shared _mlebench_registry helper module; then the 'single place' comment becomes true. Note mlebench_real.py already imports is_prepared-adjacent code lazily, so no import-weight concern.


### 4.12 CLI, Trust, top-level modules

Scope: `looplab/cli/`, `looplab/trust/`, `looplab/__init__.py`, bench.py, sweep.py.

**Reviewer assessment.** The CLI package and trust package are both well-documented and defensively engineered, but they sit at opposite ends of a decomposition spectrum: trust/ is a set of small, single-purpose modules with unusually rich precision/recall provenance comments, while looplab/cli has re-accreted two god-units after its documented package split — inspect_cmds.py (1701 lines, ~25 commands spanning read-only diagnostics, paid LLM stewardship and durable cross-run governance writes, under a docstring that still claims 'read-only') and run_cmds.run() (347 lines mixing config resolution, Genesis, a maintainer-only calibration envelope, snapshot publication and lifecycle triage). The dominant defect class is copy-paste within the CLI: the replay-critical prior-run triage appears three times, the paid-steward command skeleton three times, the memory-dir stat/governed-snapshot dance twice (plus a simpler third variant), the late-binding monkeypatch shim five times, and a read-only RunTools builder five times across four packages. The feared rot points were checked and are clean: all 200 _LAYOUT shim entries resolve to real modules, and no CLI command re-implements an events/ fold/digest/traceview projection (timings is a genuinely distinct spans aggregation).

**Strengths worth preserving:**

- The back-compat meta-path shim is in verifiably good shape: all 200 _LAYOUT entries resolve to existing modules (checked programmatically), alias and canonical name share one module object, and the map is guarded by a two-way test (tests/test_package_layout.py) — the classic shim-rot failure mode is structurally prevented.
- trust/confirm.py deliberately extracts robust_selection as the single pure winner-selection step shared with engine/confirm_phase.py, with an explicit 'so the two selections can never drift' contract — exactly the anti-duplication discipline the CLI lacks.
- The trust detectors (leakage.py, reward_hack.py, critic.py) carry exceptional inline provenance: every regex documents its false-positive/false-negative history and explicitly labels ACCEPTED RECALL GAPs as precision-over-recall decisions, making gate-safety auditable from the source alone.
- CLI paid-call safety is centralized well where it matters most: _run_cli_steward funnels all three steward commands through one durable at-most-once transaction (action-id fencing, ambiguous-outcome fail-closed), and _engine_singleton gives every mutating command the same fail-closed single-writer lock with an explicit operator escape hatch.
- run()'s maintainer note freezes the typed-flag surface and routes all new knobs through -s/--set Settings parity — an unusually explicit guard against CLI flag proliferation and settings drift.
- sweep.py and bench.py are small, dependency-light, and document their determinism/replay contracts (sorted-grid enumeration, seed derivation, the load-bearing json default=) precisely where generated code depends on them.

#### CT-01 · HIGH · under-decomposition · effort: medium

**inspect_cmds.py is a 1701-line god-module whose header lies about its contents**

*Locations:* `looplab/cli/inspect_cmds.py:1-8`, `looplab/cli/inspect_cmds.py:1016-1073`, `looplab/cli/inspect_cmds.py:1146-1196`, `looplab/cli/inspect_cmds.py:1262-1291`, `looplab/cli/inspect_cmds.py:1076-1354`

*Evidence:* The module docstring says "Read-only inspection commands: replay / timings / inspect / tensorboard", but the file holds ~25 commands across three unrelated domains: (a) run diagnostics (replay/timings/inspect/tensorboard/speculation-gate), (b) PART-IV concept diagnostics (concept-coverage/lock-in/board-dedup/research-targets/novelty-recall/lesson-guard/asset-brief), and (c) cross-run GOVERNANCE — durable writes (concept-merge, concept-split, claim-decide, task-facets-set) and three PAID LLM steward invocations (concept-steward, claim-steward, task-facets) with at-most-once ledger fencing. concept-coverage --persist additionally appends EV_NODE_CONCEPTS events under engine.lock. The governance commands share almost nothing with the fold-and-print diagnostics except the typer app.

*Recommendation:* Split into three command-group modules mirroring the existing package pattern (e.g. inspect_cmds.py, concept_cmds.py, governance_cmds.py), re-exporting through looplab/cli/__init__ like the other groups, and rewrite the module docstrings to state which commands mutate what. This is the same split the package already performed once (run/export/inspect/ui), so the mechanism and back-compat seam are proven.

#### CT-02 · HIGH · under-decomposition · effort: medium

**run() is a 347-line command function doing at least seven distinct jobs**

*Locations:* `looplab/cli/run_cmds.py:282-629`, `looplab/cli/run_cmds.py:390-412`, `looplab/cli/run_cmds.py:471-511`, `looplab/cli/run_cmds.py:543-556`, `looplab/cli/run_cmds.py:557-627`

*Evidence:* run() spans lines 282-629: flag validation, unified-file loading, settings merge, TWO speculation-gate-calibration validation blocks (~70 lines at 390-412 and 471-511 plus the fresh-dir check at 543-556), Genesis authoring, task validation, missing-path preflight, snapshot publication under run_config_write_lock, four-way lifecycle triage (finalization-pending / pending-finalize / finished-reopen / paused-resume), and driving the engine. The numbered step comments (# 1. … # 4.) are already the seams of the missing functions. The calibration envelope alone contributes ~100 lines of one-purpose validation that most readers of run() never need.

*Recommendation:* Extract per-phase helpers: _resolve_task_and_settings(...), _validate_calibration_envelope(task, settings, out), _publish_snapshots(out, task_dict, settings), and _triage_prior_run(prior, prior_events, eng) (the latter shared with resume — see the duplication finding). run() becomes a ~60-line pipeline, and the calibration lane becomes independently testable.

#### CT-03 · MEDIUM · duplication · effort: medium

**Prior-run lifecycle triage duplicated across run / resume / finalize**

*Locations:* `looplab/cli/run_cmds.py:557-627`, `looplab/cli/run_cmds.py:707-732`, `looplab/cli/run_cmds.py:793-848`

*Evidence:* run() (557-627) and resume() (707-732) both compute pending_finalize_scope = incomplete_finalize_scope(prior_events); finalization_pending = scope is not None or prior.finalization_pending(); then branch identically on (finalization_pending | stop_requested-with-error | finished | paused) with byte-identical echo strings ("run has an incomplete terminal projection — completing its existing wrap-up", "run has a pending finalize — wrapping it up (report / cross-run lessons / cost)") and the same EV_RESUME append. finalize() (793-848) carries a third variant of the same predicate cluster plus _pending_finalize(). This is a replay-critical decision (which event, if any, to append before re-entering the loop) implemented three times; a fix to one branch (as the history of these comments shows happens often) must be manually mirrored.

*Recommendation:* Extract one shared classifier, e.g. classify_prior_run(prior, prior_events) -> Literal["finalization_pending","pending_finalize","finished","paused","live","fresh"] plus a small act-on-it helper that appends EV_RUN_REOPENED/EV_RESUME and echoes. run/resume/finalize each keep only their surface-specific differences (run reopens, resume waits for handoff, finalize CAS-appends run_abort).

#### CT-04 · MEDIUM · mergeable-entities · effort: small

**Three paid-steward commands are ~70% copy-paste of each other**

*Locations:* `looplab/cli/inspect_cmds.py:1076-1143`, `looplab/cli/inspect_cmds.py:1199-1259`, `looplab/cli/inspect_cmds.py:1294-1354`, `looplab/cli/inspect_cmds.py:1112-1113`, `looplab/cli/inspect_cmds.py:1329-1330`

*Evidence:* concept-steward, task-facets and claim-steward share an identical skeleton: reject deprecated --apply before any paid call, _governance_cli_read preflight over a curation log, Settings() + `if model: settings.llm_model = model` (including the SAME two-line why-comment about model_copy writing a phantom attr, duplicated verbatim at 1113 and 1330), _run_cli_steward(memory_dir, kind, action_id, prepare=lambda: _make_llm_client(settings), invoke=...), per-proposal printing, and _echo_cli_invocation; the --json early-exit and curation_is_empty check exist in concept-steward and claim-steward only (task-facets tests `if not facets` and has no JSON mode). The core transaction is already extracted (_run_cli_steward), but ~40 lines of framing per command remain triplicated, as does the 4-line except-(GovernanceLedgerUnavailable|EventStoreLockError)/except-ValueError block that additionally appears verbatim in concept-merge (1032-1036), concept-split (1067-1071), claim-decide (1191-1195) and task-facets-set (1286-1290).

*Recommendation:* Add a second-tier helper (e.g. _steward_command(kind, memory_dir, action_id, model, apply, preflight, invoke, render)) that owns the --apply rejection, model override, preflight and invocation echo; and a @_governance_errors decorator/context manager for the repeated except block in the four deterministic governance writes.

#### CT-05 · MEDIUM · duplication · effort: small

**Memory-dir stat-resolution + governed-snapshot boilerplate duplicated (plus a simpler third variant)**

*Locations:* `looplab/cli/inspect_cmds.py:898-927`, `looplab/cli/inspect_cmds.py:1372-1391`, `looplab/cli/inspect_cmds.py:1587-1643`

*Evidence:* cross-run-concepts, cross-run-digest and claims each define a local _snapshot(); cross-run-concepts (898-927) and claims (1587-1643) do the full same dance: p.stat().st_mode; S_ISREG -> (p, p.parent); S_ISDIR -> (p/"<canonical file>", p); compute `canonical = path.absolute() == (base/name).absolute()`; then call project_governed_sources(base, _project, include_concepts=..., source_names=..., source_paths=...) with the file-vs-dir split threaded through — ~25 lines each, differing only in the canonical filename (concept_capsules.jsonl vs lessons.jsonl) and the projection body. cross-run-digest's _snapshot (1372-1391) is a simpler dir-only variant (no S_ISREG branch, fixed source_names). The partial-source WARNING rendering that follows is also near-duplicated across cross-run-concepts (937-944), cross-run-search (1436-1452), atlas (1505-1526) and claims (1672-1685).

*Recommendation:* Extract a resolve_memory_source(p, canonical_name) -> (path, base, source_names, source_paths) helper and a render_source_warnings(receipt) formatter; each command keeps only its _project body.

#### CT-06 · MEDIUM · duplication · effort: small

**Read-only RunTools builder copy-pasted five times across four packages**

*Locations:* `looplab/trust/verify.py:389-401`, `looplab/cli/inspect_cmds.py:392-402`, `looplab/engine/lessons_distill.py:181-185`, `looplab/engine/novelty.py:431-433`, `looplab/serve/report.py:117-119`

*Evidence:* The 5-line pattern `rt = RunTools(); rt.bind_state(state, None); return CompositeTools([rt])` wrapped in try/except-return-None exists five times: trust/verify.py::_verify_tools, cli/inspect_cmds.py::_run_tools_for (whose docstring literally says "mirrors trust.verify._verify_tools"), engine/lessons_distill.py::_reflect_tools, engine/novelty.py (inline), serve/report.py. Two of the five copies document their kinship with a "mirrors …" comment instead of sharing code (trust/verify.py and inspect_cmds; the novelty.py inline copy also degrades to `idea` rather than None), so a change to the degrade-to-None contract or the bind_state(state, parent) signature must be found by grep in five places.

*Recommendation:* Add one helper in looplab/tools (e.g. tools/run_tools.py::readonly_run_tools(state) -> Optional[CompositeTools]) and point all five callers at it. tools/ is importable from trust, engine, serve and cli without layering violations.

#### CT-07 · MEDIUM · duplication · effort: small

**Five hand-written late-binding monkeypatch shims with identical bodies**

*Locations:* `looplab/cli/run_cmds.py:49-63`, `looplab/cli/export_cmds.py:21-26`, `looplab/cli/inspect_cmds.py:385-389`, `looplab/bench.py:23-29`

*Evidence:* The pattern `def X(*args, **kwargs): from looplab import cli; return cli.X(*args, **kwargs)` — the seam that keeps monkeypatch.setattr("looplab.cli._engine"/… ) working — is written out five times: run_cmds._engine, run_cmds.make_llm_client, export_cmds.make_llm_client, inspect_cmds._make_llm_client, bench._engine. Each carries its own paragraph re-explaining the same freeze-at-import hazard. The seam itself is intentional (documented in CLAUDE.md-adjacent docstrings); the quintuplication is not.

*Recommendation:* One factory in the cli package, e.g. `def _late(name): def call(*a, **k): from looplab import cli; return getattr(cli, name)(*a, **k); return call`, then `_engine = _late("_engine")` etc., with the why-comment written once at the factory.

#### CT-08 · MEDIUM · inconsistency · effort: small

**config.snapshot.json loaded three different ways with three failure semantics**

*Locations:* `looplab/cli/run_cmds.py:219-238`, `looplab/cli/run_cmds.py:661-664`, `looplab/cli/run_cmds.py:820-823`, `looplab/cli/inspect_cmds.py:405-427`

*Evidence:* run_cmds has the strict loader _settings_from_config_snapshot (BadParameter on any corruption), but resume (661-664) and finalize (820-823) each duplicate the same 4-line `settings = Settings(); snap = run_dir/"config.snapshot.json"; if snap.exists(): settings = _settings_from_config_snapshot(snap)` prologue; and inspect_cmds independently re-implements snapshot loading as _settings_for_run with SILENT fallback to ambient Settings on any exception (its docstring justifies the ambient fallback for diagnostics, but it re-parses the JSON itself instead of composing the shared loader). Three call shapes for the same file means the "which settings does this command actually run with" question has three answers depending on entry point.

*Recommendation:* One `load_run_settings(run_dir, *, strict: bool) -> Settings` in the cli package (or core/appconfig): strict=True raises BadParameter (run/resume/finalize), strict=False degrades to ambient (diagnostics). Kill the two inline duplicates.

#### CT-09 · MEDIUM · mergeable-entities · effort: medium

**trust/verify.py vs trust/verifier.py: two near-namesake verifier modules with overlapping machinery**

*Locations:* `looplab/trust/verify.py:371-374`, `looplab/trust/verify.py:404-462`, `looplab/trust/verifier.py:218-260`, `looplab/trust/lesson_guard.py:70-88`

*Evidence:* verify.py (D8 memo-claim verifier) and verifier.py (advisory criteria scorer) are separate modules whose names differ by two letters and whose internals overlap: near-identical output models (_VerdictOut{verdicts,notes} vs _Verdicts{verdicts,rationales}), the same agentic_struct(client, tools, msgs, Model, parser=…, loop_opts={"max_turns": 15}, fallback=parse_structured) invocation, an overlapping verdict vocabulary ('unclear' is shared; verifier.py's ordinal strong_no..strong_yes scale maps 'supported'/'unsupported' only as normalization aliases), and verify.py builds its own read-only RunTools while verifier.py accepts a caller-supplied `tools` parameter. lesson_guard.py adds a third _evidence_text whose comment says it "mirrors trust/verify.py::_evidence_text". The two modules do serve different purposes (evidence-identity checking vs repeated ordinal sampling), so a full merge is wrong — but the naming and the duplicated LLM-judging plumbing are a real navigation/maintenance hazard: grep for "verifier" lands in both, and a change to the judge-call contract (max_turns, fallback, parser) must be made in 2-3 places.

*Recommendation:* Rename one module (e.g. verify.py -> memo_verify.py, keeping a _LAYOUT/back-compat alias as the repo already does for renames) and extract the shared judge-call helper (structured-judge invocation with agentic fallback) into one place both import.

#### CT-10 · LOW · inconsistency · effort: medium

**Three finding-dict vocabularies for the same 'trust flag' concept, adapted inline at the consumer**

*Locations:* `looplab/trust/leakage.py:45-63`, `looplab/trust/reward_hack.py:224-336`, `looplab/trust/critic.py:16`, `looplab/trust/harden.py:78-89`, `looplab/engine/evaluate.py:785-795`

*Evidence:* leakage detectors return {"detector": …, "leak": bool, …}; reward_hack returns [{"signal", "detail", "method", "confidence"}] and ExploitSuite.scan returns [{"signal", "detail"}]; critic returns [{"issue", "detail"}]. engine/evaluate.py then normalizes them by hand: `sigs.append({"signal": "data_leakage:" + f["signal"], …})` and `sigs.append({"signal": "critic:" + c["issue"], …})` while reward_hack rows pass through unchanged. The signal namespace ("data_leakage:", "critic:") — which is_hard_signal keys gating decisions on — is thus assembled at the call site rather than owned by the detectors. This is contained (one consumer) but means any new consumer of the trust detectors must re-invent the same mapping (critic.py's module docstring does name the `critic:hardcoded_metric` gate signal, but the mapping itself lives only in evaluate.py).

*Recommendation:* Define one lightweight finding shape (signal, detail, method, confidence, plus optional detector-specific fields) in trust/, have each detector emit its already-namespaced signal (data_leakage:fit_on_test, critic:hardcoded_metric), and reduce evaluate.py to concatenation. The dict-based events stay wire-compatible.

#### CT-11 · LOW · dead-code · effort: small

**Dead `state` parameter in _persist_node_concepts**

*Locations:* `looplab/cli/inspect_cmds.py:108-134`, `looplab/cli/inspect_cmds.py:554-563`

*Evidence:* _persist_node_concepts(store, state, raw_tags, …) unconditionally rebinds its second argument at line 134 (`state = fold(events)`) — correctly, per the comment about re-folding inside the mutation transaction — so the value the caller passes is never read. The caller _persist_exact computes `current = fold(current_events)` (line 544) — that fold is still needed by `_retro_tag_finished` — but passes it (line 556) for nothing.

*Recommendation:* Drop the parameter (or rename the local) so the signature stops implying the caller's fold matters; removes the pointless pass (the caller's fold stays — `_retro_tag_finished` consumes it; ~15 direct test call sites also pass a fold).

#### CT-12 · LOW · over-engineering · effort: small

**Trust-package library surfaces with no production consumer across multiple review cycles**

*Locations:* `looplab/trust/cv.py:19-62`, `looplab/trust/reward_hack.py:161-221`, `looplab/trust/verify.py:52-56`, `looplab/trust/harden.py:124-146`

*Evidence:* Verified by repo-wide grep: cv.py's kfold_indices/purged_walk_forward/consistent_cv/Evaluator are imported only by tests/test_cv_confirm.py; reward_hack.calibrate_detector + SEED_CALIBRATION_CORPUS only by tests/test_reward_hack.py; verify._source_ref only by tests/test_phase4_verify.py (its own docstring admits it is a test facade). Each is documented as an intentional seam — but docs/17 (2026-07-11, three weeks before this review) already listed the cv splitters as "tested, no live caller", and no adapter has arrived. harden's LLM-hacker plug is similarly unused, and its fallback rule name `exploit_{abs(hash(code)) % 10**6}` (harden.py:125) is nondeterministic across processes (salted str hash), so the same LLM-found exploit would persist under different names in the durable suite.

*Recommendation:* Not deletion-on-sight (the seams are documented), but set a decision point: wire the cv splitters behind a temporal adapter or move them to a docs/example; if calibrate_detector is the operator's harness, expose it (a `looplab calibrate-detector` subcommand is ~15 lines); replace hash() with a content digest (hashlib) in _derive-pattern naming.

#### CT-13 · LOW · duplication · effort: small

**Run-dir existence check re-implemented inline four times despite _require_run_dir**

*Locations:* `looplab/cli/__init__.py:138-146`, `looplab/cli/run_cmds.py:640-643`, `looplab/cli/run_cmds.py:762-764`, `looplab/cli/run_cmds.py:781-783`, `looplab/cli/run_cmds.py:925-927`

*Evidence:* _require_run_dir exists precisely to turn a missing events.jsonl into a clear exit-2, yet resume, stop, finalize and repair-log each re-write the `if not (run_dir / "events.jsonl").exists(): typer.echo(...); raise typer.Exit(2)` block inline with slightly different wording; stop/finalize then construct EventStore themselves — exactly what _require_run_dir returns. resume also builds a throwaway EventStore at 648 solely for _require_healthy_log and a second one inside _engine.

*Recommendation:* Use _require_run_dir (optionally with a `hint:` message parameter for the resume-specific guidance) in all four; have it optionally run the health check too, since every mutating caller pairs the two.

#### CT-14 · LOW · duplication · effort: small

**Optional-LLM-client construction pattern repeated across six diagnostics**

*Locations:* `looplab/cli/inspect_cmds.py:448-453`, `looplab/cli/inspect_cmds.py:574-579`, `looplab/cli/inspect_cmds.py:649-656`, `looplab/cli/inspect_cmds.py:724-728`, `looplab/cli/inspect_cmds.py:786-792`, `looplab/cli/inspect_cmds.py:806-812`

*Evidence:* The block `settings = _settings_for_run(run_dir, model); try: client = _make_llm_client(settings) except Exception as e: typer.echo(f"(no LLM endpoint: {e}; …fallback…)")` appears in _concept_map_for, concept-coverage, asset-brief, board-dedup, novelty-recall and lesson-guard, differing only in the fallback message. board-dedup even runs it twice per invocation (once inside _concept_map_for, once for hypothesis tagging).

*Recommendation:* One helper `_optional_client(run_dir, model, fallback_note) -> (settings, client|None)`; commands keep only their message.

#### CT-15 · LOW · under-decomposition · effort: medium

**_engine builder: 191 lines with duplicated ForesightPanelResearcher wiring in two branches**

*Locations:* `looplab/cli/__init__.py:302-493`, `looplab/cli/__init__.py:377-382`, `looplab/cli/__init__.py:396-401`

*Evidence:* _engine assembles the whole object graph — profile validation, calibration lane, researcher-wrapper selection (surrogate/foresight/panel × unified/non-unified), onboarder, strategist, deep researcher, report writer, proxy scorer, embedder, lesson abstractor — in one function. The ForesightPanelResearcher constructor call with its five getattr-defaulted kwargs (k, tools, min_confidence, verify_score, verify_samples) is written twice, once in the non-unified branch (377-382) and once in the unified branch (396-401); the guard conditions differ by one clause. A drift between the two copies would silently change unified-vs-plain behavior.

*Recommendation:* Extract at least _wrap_researcher(researcher, developer, settings, ftools) returning the wrapped pair, with the Foresight ctor written once; consider further splitting strategist/deep-research/report construction into small builders. Keep the function's name and signature — ~10 tests patch looplab.cli._engine.


### 4.13 Cross-package / whole-tree analysis

Scope: import graph, cross-package duplication, dead top-level code, registries, tests.

**Reviewer assessment.** The documented layering holds remarkably well for a ~123k-line tree: events imports only core, the engine never touches serve, and process spawning/liveness is centralized in serve/engine_proc. The real cross-package problems are concentrated at two seams: the cross-run knowledge subsystem (engine/memory + claims + concept_registry), whose private underscore functions are consumed wholesale by tools/ and serve/ without any of the registry guards the project otherwise applies religiously, and the serve composition style (giant build_router closures wired together by late-bound srv.*_fn attributes). Cross-package duplication is mostly disciplined (jsonl reading, redaction, atomic writes are genuinely shared), with one systemic exception — the file-identity stat-tuple reimplemented in 10+ modules — plus smaller drift in metric formatting. The nine registry constants should not be unified into one mechanism; only their test scanners share extractable boilerplate.

**Strengths worth preserving:**

- Layering discipline is real, not aspirational: a full import-graph scan found only one upward import out of core (config.py:1358), zero events→anything-above-core edges, and zero engine→serve edges — engine mixins even carry 'never serve' notes in their module docstrings.
- The registry + two-way source-scan test pattern (event types, role outputs, hints, prompt keys, signals, background-appendable events) converts classically-silent duck-typing renames into red tests — an unusually rigorous seam-guarding discipline.
- The back-compat meta-path finder in looplab/__init__.py aliases old flat paths to the same module object (so monkeypatching either path works), carefully restores __spec__ so importlib.reload keeps working, and is kept honest by tests/test_package_layout.py.
- Low-level helpers are genuinely reused across packages rather than reimplemented: read_jsonl_lenient/iter_jsonl, core/atomicio, core/redact, events/digest's node_metric/top-k — cli/inspect_cmds even documents WHY it picks read_jsonl_lenient over iter_jsonl for corrupt-span tolerance.
- Load-bearing why-comments at append sites, cache keys and lock acquisitions make the replay/idempotency invariants auditable in place — most files explain the failure mode a guard exists for, not just what the code does.

#### XP-01 · HIGH · layering · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**tools/cross_run_tools.py consumes 12 private (_-prefixed) engine functions (~20 import sites) across the package boundary**

*Locations:* `looplab/tools/cross_run_tools.py:232`, `looplab/tools/cross_run_tools.py:337`, `looplab/tools/cross_run_tools.py:348`, `looplab/tools/cross_run_tools.py:412`, `looplab/tools/cross_run_tools.py:424`, `looplab/tools/cross_run_tools.py:821`, `looplab/tools/cross_run_tools.py:928`, `looplab/tools/cross_run_tools.py:1121`, `looplab/serve/run_files.py:13`

*Evidence:* cross_run_tools.py lazily imports _capsule_fingerprint_scope_complete, _capsule_rows, _dedup_valid_capsules, _claim_source_rows, _filter_claim_source_rows, _filter_claim_assessments, _capsule_source_summary, _filter_capsule_rows, _portfolio_concept_overview_data from engine.memory/engine.claims/engine.concept_registry — private names of a 1600-line (memory.py) and 2896-line (claims.py) module used from a lower-layer package. tools/ also imports engine at 26 sites total (knowledge_tools.py:270, concept_tools.py:209-365) while engine imports tools back (15 sites), a package cycle held together only by function-local imports. Unlike every other duck-typed seam in this codebase (BACKGROUND_APPENDABLE, DEVELOPER_OUTPUT_ATTRS, PROMPT_KEYS...), this private cross-package surface has no registry or source-scan guard, so an engine-internal rename that looks safe (underscore = private) silently breaks the cross-run tools. serve/run_files.py:13 similarly imports events.eventstore._interprocess_lock at module level.

*Recommendation:* Promote the functions cross_run_tools actually needs into a public read-model API (drop the underscore, add to engine/memory's public surface or a dedicated cross-run read-model module) so the boundary is explicit; alternatively guard the private-import list with the same registry+source-scan discipline used for the other seams. Rename _interprocess_lock to a public name since four packages outside events (serve, cli, engine, tools) depend on it.

#### XP-02 · MEDIUM · duplication · effort: medium

**File-identity signature tuple (dev, ino, ctime_ns, size, mtime_ns) hand-rolled in 10+ modules across five packages**

*Locations:* `looplab/tools/_runcache.py:54`, `looplab/serve/appstate.py:236`, `looplab/serve/appstate.py:390`, `looplab/serve/routers/runs.py:799`, `looplab/serve/routers/attention.py:97`, `looplab/serve/command_observation.py:99`, `looplab/serve/log_pages.py:133`, `looplab/serve/scope_sources.py:134`, `looplab/engine/train_monitor.py:227`, `looplab/core/run_deletion.py:54`, `looplab/events/span_index.py:610`, `looplab/events/traceview.py:45`

*Evidence:* At least three functions literally named _file_identity exist (core/run_deletion.py:54, engine/train_monitor.py:227, serve/scope_sources.py:134) plus inline tuples in _runcache.sig, appstate._sig/_state_cache key, the runs-router _summary_cache key, attention's signature, log_pages._metadata_signature, span_index and traceview.trace_file_revision. Each site re-derives the same 'mtime seconds are blind to same-size same-second rewrites; include inode/device and ctime' insight with its own multi-line why-comment, and the field sets already drift (train_monitor uses (dev, ino) only; log_pages splits identity vs metadata; runs.py caches 4 fields, appstate 5).

*Recommendation:* Add one core helper (e.g. core/atomicio.file_identity(stat) returning the 5-tuple, with the canonical why-comment) and use it everywhere; sites that intentionally need fewer fields can document the subset against the shared definition.

*Resolution (2026-08-02):* `core/atomicio.file_identity` shipped with the canonical why-comment
(one bullet per field, each naming the swap it catches). The four sites using the exact 6-tuple —
`core/run_reset`, `core/run_deletion`, `serve/reset_transaction`, `serve/routers/control` — now
call it; the deliberate subsets (`train_monitor`, `log_pages`, `artifacts`) each state which
fields they omit and why, against that definition.

#### XP-03 · MEDIUM · layering · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**tools/machine_runs_tools.py is a serve-side component living in tools/, forming a tools<->serve cycle**

*Locations:* `looplab/tools/machine_runs_tools.py:1276`, `looplab/tools/machine_runs_tools.py:1479`, `looplab/serve/assistant.py:495`, `looplab/serve/assistant.py:547`

*Evidence:* machine_runs_tools.py (1659 lines) lazily imports serve.run_files.run_config_write_lock and serve.engine_proc (spawn/liveness) — the only two upward tools→serve imports in the tree — while serve/assistant.py imports MachineRunsTools/RunLauncherTools/RunControlTools back. Its only non-test consumers are serve/assistant.py; the cycle is avoided purely by function-local imports. The package map defines tools/ as 'agent-facing tools' and serve/ as the top layer, so a run-mutating assistant backend that needs serve's config-lock and engine-spawn machinery sits one layer below its own dependencies.

*Recommendation:* Move machine_runs_tools (or at least its mutation/spawn paths) into serve/, or extract run_config_write_lock and the liveness/spawn contract into a serve-independent module both can import downward.

*Resolution (2026-08-02, inversion arm):* `RunControlTools` — the class that owns the mutating
delete path — now takes its serve-side primitives as an explicit `RunLifecycleFns` dataclass
argument, and `tools/` names `serve` in exactly ONE place: that argument's lazy default. The
dependency is an argument of the component that needs it rather than an upward reach, and a caller
(a test, a different host) can substitute it. Behaviour is unchanged for every existing caller,
because the default resolves the same implementations.

`tests/test_cross_package_private_seams.py` pins both halves: an injected provider is used verbatim,
the default still resolves all five callables, and NO `looplab.serve` import may appear in `tools/`
outside that one provider. Verified to have teeth by scattering a second lazy import back in.

*Still open (the downward-extraction arm):* the five primitives cannot simply move to a lower layer
as-is — `_run_lifecycle_lock`, `_fresh_resume_launch_pending` and `_fresh_run_launch_pending`
transitively need `_run_lifecycle_key`, `_run_lifecycle_locks(_guard)`, `_run_lifecycle_lock_path`,
`_engine_liveness`, `_launch_claim_is_fresh` and `_run_launch_marker_path`. That is the whole
run-lifecycle/launch-liveness subsystem, not four helpers; moving half of it would split the grace
constants across two modules, which is worse than the cycle. It wants its own change.

#### XP-04 · LOW · layering · effort: small — **RESOLVED (2026-08-02)**

**core/config.py imports looplab.agents at validation time, violating the documented 'core imports nothing above itself' rule**

*Locations:* `looplab/core/config.py:1358`

*Evidence:* Settings validation runs `from looplab.agents.cli_agent import PRESETS as _DEV_PRESETS` to validate developer_backend. The inline comment justifies validating against the authoritative registry and keeping the import lazy, but not the direction: core (the bottom layer) now executes agents-package code on every Settings construction, and an import-time error in agents/cli_agent.py breaks all config loading. This is the single upward import out of core in the whole tree.

*Recommendation:* Move the closed set of developer-backend keys into core (e.g. core/task_kinds or a small constants module) and have agents/cli_agent assert its PRESETS match it (same two-way pattern the other registries use), restoring the documented layering without losing the fail-loud validation.

*Resolution:* `core.config.DEVELOPER_BACKENDS` is the closed set; `agents/cli_agent.py` asserts at
IMPORT time that its PRESETS are covered, so the authority over "which backends exist" is inverted
rather than lost. `core/` now imports nothing above itself anywhere in the tree.

`tests/test_developer_backend_registry.py` checks both directions, because each fails silently on
its own: a preset missing from the set makes a REAL backend unconfigurable (Settings rejects it as a
typo), and a set entry with no preset is worse — Settings accepts it and `adapters/tasks.py` wires
the DEFAULT developer instead, the exact silent downgrade the original validation existed to stop.
A third test asserts the layering rule for the WHOLE `core/` package rather than just the one import
that broke it. Verified to have teeth against all three: an unlisted preset, a preset-less set
entry, and a fresh upward import from `core/parse.py`.

#### XP-05 · MEDIUM · under-decomposition · effort: large

**13 closure-based build_router(srv) factories, three exceeding 1300 lines, with hand-rolled DI via late-bound srv.*_fn attributes**

*Locations:* `looplab/serve/routers/runs.py:700`, `looplab/serve/routers/reports.py:1555`, `looplab/serve/routers/control.py:192`, `looplab/serve/routers/assistant.py:116`, `looplab/serve/routers/runs.py:890`, `looplab/serve/routers/runs.py:925`, `looplab/serve/routers/misc.py:591`, `looplab/serve/routers/reports.py:1582`, `looplab/serve/routers/genesis.py:211`

*Evidence:* Every router is one giant factory closure: runs.py build_router is 2146 lines, reports.py 1549, control.py 1327, assistant.py 757 — endpoints, caches (concept_core_cache, _summary_cache) and helpers are all nested functions capturing srv, so no endpoint can be imported or unit-tested in isolation. Cross-router dependencies are wired by mutating the server object at build time (runs.py sets srv.list_runs_fn / srv.list_runs_membership_fn, misc.py sets srv.list_tasks_fn; reports.py:1582 and genesis.py:211 read them at request time), an implicit stringly-attribute contract with no static guarantee the producer router was mounted first.

*Recommendation:* Extract endpoint bodies to module-level functions taking srv (or a typed protocol) explicitly and register them in a thin build_router; replace the late-bound srv.*_fn attributes with an explicit shared-services object constructed before any router, so the inter-router contract is typed and import-checkable.

#### XP-06 · MEDIUM · under-decomposition · effort: large

**The largest module in each package still contains one 500-800-line function**

*Locations:* `looplab/engine/orchestrator.py:483`, `looplab/engine/orchestrator.py:1494`, `looplab/events/replay.py:4746`, `looplab/serve/run_commands.py:485`, `looplab/search/speculation_quality.py:2051`

*Evidence:* The 16-mixin Engine split is documented and intentional, but the residual accretion points are single functions: Engine.__init__ is 770 lines (orchestrator.py:483), _run_with_llm_broker 540 lines (1494); replay.py's _derive_cards is an 818-line fold sub-projection (4746); run_commands.normalize_control is a 775-line if-chain normalizing every control-event shape (485); speculation_quality_gate is 390 lines (2051). These functions cross multiple responsibilities (e.g. __init__ does config resolution, wiring, seam registration and validation) and are the highest-churn merge-conflict surfaces in the repo.

*Recommendation:* Split by responsibility, keeping replay-determinism: Engine.__init__ into named _init_* steps; normalize_control into a per-event-type dispatch table of small normalizers (the CONTROL_EVENTS registry already enumerates the types); _derive_cards into per-event helper functions composed by one driver.

#### XP-07 · MEDIUM · layering · effort: medium

**search/speculation_quality.py reaches up into engine and binds calibration receipts to a hash of every .py file in the package**

*Locations:* `looplab/search/speculation_quality.py:755`, `looplab/search/speculation_quality.py:1589`, `looplab/search/speculation_quality.py:1955`, `looplab/search/speculation_quality.py:1976`

*Evidence:* speculation_quality.py (search layer) lazily imports engine.finalize.incomplete_finalize_scope and engine.orchestrator's SPECULATION_CALIBRATION_PROFILE_* constants — upward imports into the orchestrator from a policy package. Its implementation digest rglobs every looplab/*.py and hashes raw bytes into the receipt manifest (1955-1985), so a comment-only or docs-adjacent edit anywhere in the codebase revokes every issued calibration receipt; the module's own inline comment (≈1961) states this 'turns review-only commits into an operational stop/resume outage and forces six fresh GPU calibration runs after documentation edits' and recommends a versioned semantic manifest instead — i.e. the code ships with an acknowledged unresolved design defect.

*Recommendation:* Move the calibration-profile constants into search (or a shared core module) so engine imports them downward, and implement the comment's own recommendation: pin receipts to an explicit rollout/protocol version plus exact hashes of only execution-affecting files.

#### XP-08 · LOW · dead-code · effort: small

**Seven verified-dead functions, including a ~60-line unused locking context manager**

*Locations:* `looplab/engine/claims.py:1577`, `looplab/engine/governance_health.py:557`, `looplab/serve/scope_report.py:601`, `looplab/serve/routers/reports.py:418`, `looplab/search/concept_graph.py:1008`, `looplab/search/concept_graph.py:1020`, `looplab/search/card_selection.py:637`

*Evidence:* Repo-wide grep (looplab/, tests/, docs/, ui/) finds zero references to: claims.locked_claim_evidence_snapshot (a ~60-line ExitStack/lock context manager whose presence misleadingly implies a locking protocol that nothing exercises), governance_health.claim_governance_snapshot, scope_report.build_digest, reports._scope_action_lease_marker_exists, concept_graph._normalized_rename_map and _canon_set, card_selection._explored_concepts. Several are one-line delegation wrappers left behind by refactors (e.g. _normalized_rename_map just calls normalized_concept_renames). adapters/kaggle_dl.check_auth also has no code callers but is a documented operator command in docs/MLEBENCH.md:54, so it is NOT dead.

*Recommendation:* Delete the seven functions; if locked_claim_evidence_snapshot documents an intended future locking protocol, move that intent to docs/an ADR rather than shipping dead lock machinery.

#### XP-09 · LOW · inconsistency · effort: small

**Metric formatting implemented three ways with divergent output semantics**

*Locations:* `looplab/events/digest.py:202`, `looplab/serve/tui_format.py:28`, `looplab/serve/scope_report.py:83`

*Evidence:* digest.fmt_num renders None as '?' and uses %.4g; tui_format.fmt_metric renders None/NaN as '—', switches to exponent form outside [1e-3, 1e6) and takes a precision arg (documented as 'the Python twin of util.js fmt'); scope_report._fmt_metric renders None as '—' with %.5g. The same best-metric value can therefore print differently in agent-facing digests, the TUI, and cross-run scope reports, and a formatting fix must be found and applied three times (four counting ui/'s util.js twin).

*Recommendation:* Keep one canonical formatter (tui_format's is the most complete) in core or events and have the other call sites delegate, parameterizing the None sentinel if the '?' vs '—' distinction is intentional.

#### XP-10 · LOW · duplication · effort: small

**Registry sprawl verdict: the 9 registries should stay separate, but their 6 guard tests copy-paste the source-scan skeleton**

*Locations:* `tests/test_role_output_contract.py:26`, `tests/test_prompt_keys.py:21`, `tests/test_event_types.py:83`, `tests/test_hint_forwarding.py:49`, `tests/test_signal_delivery.py:21`, `tests/test_background_appendable.py:1`

*Evidence:* BACKGROUND_APPENDABLE/SETUP_THREAD_APPENDABLE (frozensets of event types), TASK_OPTIONAL_HOOKS/DEVELOPER_OUTPUT_ATTRS/RESEARCHER_ACTION_ATTRS/RESEARCHER_HINT_ATTRS (attr-name tuples), PROMPT_KEYS (override file keys), SIGNALS (SignalRoute dataclasses) and CONTROL_EVENTS (HTTP allow-list) guard different seam kinds with type-appropriate shapes — a uniform runtime registry mechanism would add abstraction without value since the per-seam scan heuristics ARE the value. What is duplicated is the test-side skeleton: each guard test independently reimplements 'rglob looplab/*.py, read with BOM-tolerant encoding, regex/AST-extract names, build {name: files}' (test_role_output_contract._scan, test_prompt_keys._call_keys, test_event_types' ast walk, test_hint_forwarding's ast walk, etc.), including repeated per-file gotchas like the utf-8-sig BOM note.

*Recommendation:* Do NOT unify the registries themselves. Extract a small shared tests helper (e.g. tests/_source_scan.py with iter_sources() and scan(pattern)->dict) so the six guard tests share the file-walking/decoding logic while keeping their bespoke extraction heuristics.

#### XP-11 · MEDIUM · duplication · effort: medium

**Test suite mass-duplicates Engine construction and role stubs; conftest is only 30 lines**

*Locations:* `tests/conftest.py:1`, `tests/test_ablation.py:18`, `tests/test_agent_control.py:52`, `tests/test_build_recovery.py:32`, `tests/test_card_selection_integration.py:90`, `tests/test_end_to_end.py:26`

*Evidence:* 29 test files each define a private `_engine(...)` factory around Engine(...); there are 153 direct Engine( constructions across 78 files (204 counting subclass-named stub engines), and 17 files define their own scripted Researcher/Developer stub classes (12 distinct `class _*Developer` names, 13 distinct `class _*Researcher` names — with _Researcher/_BatchResearcher/_SeqResearcher re-defined in 11/6/4 files respectively). CLAUDE.md acknowledges the symptom — 'Engine tests construct Engine(...) directly (~100 call sites) — keep its keyword API stable' — i.e. the production constructor API is frozen specifically because the test-side factory was never centralized.

*Recommendation:* Add a tests/factories.py (or conftest fixtures) with a canonical make_engine(run_dir, **overrides) plus the common scripted-role stubs; migrate opportunistically. This directly reduces the cost of ever evolving Engine's keyword API.

#### XP-12 · LOW · layering · effort: small

**Generic KNN/IDW numeric estimator lives in the events package and drags runtime/search into depending on it**

*Locations:* `looplab/events/digest.py:1`, `looplab/runtime/proxy.py:20`, `looplab/search/surrogate.py:25`, `looplab/search/panel.py:16`

*Evidence:* events/ is documented as event-log projections ('files-as-truth'), yet digest.py exports knn_idw and numeric_params — pure numeric estimation helpers with no event-log dependency — consumed by runtime/proxy.py (the sole reason runtime imports events at all), search/surrogate.py and search/panel.py. The one legitimate runtime→events edge in the import graph exists only to reach a math function.

*Recommendation:* Move knn_idw/numeric_params to core (e.g. core/fitness or a small core/numeric module) and re-export from events.digest for compatibility; runtime then imports nothing above core.


### 4.14 UI (React control plane)

Scope: `ui/src/` + `ui/test/`.

**Reviewer assessment.** The UI is a hand-rolled (no framework beyond React) event-sourced control plane with an unusually strong correctness culture: generation-fenced mutations, idempotency-keyed durable command envelopes in sessionStorage, allow-listed payload validation, and ~90 pure-model test files. The architecture note in util.js ("mega-refactor P5.2") records the deliberate extraction of api.js/format.js/layout.js, alongside a real pure-model layer (timelineModel, runIndex, assistantRecovery, …), which largely worked. The remaining structural debt is concentrated in six 1.6k-2.4k-line god files (api.js, panels.jsx, RunView.jsx, AssistantBar.jsx, Inspector.jsx, Dock.jsx) and in one large duplication: the durable run-command lifecycle state machine is implemented twice, nearly line-for-line, in Dock and AssistantBar. API paths used by the UI were cross-checked against looplab/serve/routers/* (commands/jobs/settings-schema/deletions/report_refresh/concepts-lens/scope-report all verified) — no client/server route mismatches found.

**Strengths worth preserving:**

- Rigorous, uniformly applied mutation-safety discipline: every write is generation-fenced with idempotency keys, durable sessionStorage envelopes with strict allow-listed keys (api.js RUN_ENVELOPE_KEYS/LOCK_KEYS), and fail-closed recovery UIs — an unusually honest treatment of lost-response ambiguity for a web UI.
- Pure-model extraction with deep test coverage: ~50 dependency-free .js model modules (timelineModel, runIndex, mergeIntent, assistantRecovery, runStartOverRecovery, conceptViewModel, settingsModel, ...) each paired with focused tests in ui/test (~90 files), keeping protocol logic testable outside React.
- usePoll is a genuinely shared, well-designed polling primitive (serialized ticks, alive() fencing, abortable requests, opt-in visibility pause) that demonstrably replaced copy-pasted setInterval effects per its P5.2 note.
- Client/server API surface is consistent: every path the UI constructs (commands, jobs, deletions, concepts/lens recovery, scope-report actions, report_refresh, reviews, comments) has a matching route in looplab/serve/routers/* — no orphaned endpoints found; review-mode read-only enforcement is centralized in api.js (assertNotReviewMutation + reviewReadPath + _authHeaders) rather than scattered per component.
- Comments are load-bearing and honest, frequently citing prior arch-review findings and explaining replay/idempotency rationale inline — matching the repo-wide convention and making the dense recovery code auditable.

#### UI-01 · HIGH · duplication · effort: large

**Durable run-command lifecycle state machine implemented twice (Dock vs AssistantBar)**

*Locations:* `ui/src/Dock.jsx:803-1338`, `ui/src/AssistantBar.jsx:793-1217`, `ui/src/api.js:448-606`

*Evidence:* Dock and AssistantBar each contain a ~350-line near-identical command lifecycle implementation with pairwise-parallel functions: persistTransport/persistDirect, protocolTransportState/protocolDirect, acceptTransportRecord/acceptDirectRecord, unavailableTransport/unavailableDirect, failTransport/failDirectObservation, runTransport/executeDirect, onCheckTransport/checkDirect, onRetryTransport/retryDirect, storageTransportFailure/localStorageFailure, plus two byte-similar restore-on-mount effects (Dock.jsx:907-934 vs AssistantBar.jsx:1127-1184) and two identical poll loops (1s initial, 1.5s reschedule, transientFailures<3, 750*2^n backoff capped at 6000ms: Dock.jsx:1270-1313 vs AssistantBar.jsx:1185-1217). The storage layer beneath (api.js saveCommandTransport/loadCommandTransport/locks) is already parameterized by source 'dock'|'assistant' — only the React state machine above it was copy-pasted with renames. assistantCommand.js holds only small fragments of the assistant side.

*Recommendation:* Extract one useDurableRunCommand(source, runId, {labels, onToast}) hook (or a pure state-machine module driven by both components) over the already-shared api.js envelope layer; the DIRECT/TRANSPORT_INTENTS action tables and label maps become the only per-surface inputs.

#### UI-02 · HIGH · over-engineering · effort: large

**api.js re-accreted into a 2,216-line god-module of 8 distinct concerns**

*Locations:* `ui/src/api.js:1-2217`, `ui/src/api.js:53-606`, `ui/src/api.js:1384-1483`, `ui/src/api.js:1788-2098`

*Evidence:* One module contains: the fetch client + auth/review/prefix plumbing (_authHeaders, _throw, apiUrl, reviewReadPath), ~550 lines of sessionStorage command-envelope/lock/launch-transport persistence (lines 53-606), a WHATWG SSE parser (1384-1483), the command submit/await/retry protocol (663-960), the report-refresh intent store (962-1114), the scope-report paid-action reconciliation saga (~300 lines, 1788-2098), the CONTROL action map, cross-run Atlas payload sanitizers (projectCrossRunValue, 1685-1760), and every endpoint function. The header comment says it was split out of util.js 'bodies verbatim', but it has since grown well beyond an API client.

*Recommendation:* Split along the already-visible seams: commandStorage.js (envelopes/locks/launch transports), eventStream.js (parser + fetchEventStream), commandProtocol.js (submit/await/retry/jobAwait), scopeReportActions.js (the paid-action saga). util.js barrel keeps importers unchanged, matching the P5.2 precedent.

#### UI-03 · HIGH · mergeable-entities · effort: large

**RunView.jsx is a 2,000-line god-component; start-over recovery saga and repeated page-shell markup should be extracted**

*Locations:* `ui/src/RunView.jsx:177-1999`, `ui/src/RunView.jsx:650-903`, `ui/src/RunView.jsx:1318-1506`, `ui/src/RunView.jsx:461-511`

*Evidence:* One component owns: route/fence state, the Start-over destructive-operation recovery saga (~250 lines: persistStartOverPhase/executeStartOver/finishStartOverHandoff/retryStartOver plus 4 coordinating effects at 852-903), merge-intent capture, pane layout with a manual focus-owner switchyard (workspaceFocusOwnerRef, 461-511), a config resource fetch, historical snapshot loading, hub dropdown menus, timeline wiring, and toast state. Additionally, six early-return screens (1318-1339, 1340-1379, 1380-1407, 1408-1439, 1440-1461, 1484-1506) each re-declare the same topbar/brand markup ('<div className="topbar run-head"><span className="brand">…') six times with small variations.

*Recommendation:* Extract useStartOverRecovery(runId, generation) as a hook (it already has a pure-model sibling in runStartOverRecovery.js), and a RunShell({children, banner}) wrapper for the six early-return screens. Merge-intent handling can also move next to mergeIntent.js as a hook.

#### UI-04 · MEDIUM · under-decomposition · effort: medium

**panels.jsx: 19 panels in one 2,351-line module; ConfigPanel (~490 lines) and the Card kanban (~700 lines) are components-within-a-module needing their own files**

*Locations:* `ui/src/panels.jsx:562-1048`, `ui/src/panels.jsx:1319-1953`, `ui/src/RunView.jsx:76-97`

*Evidence:* panels.jsx exports OverviewPanel, ResearchPanel, TrustPanel, SensitivityPanel, FailuresPanel, QueuePanel, ParetoPanel, DataQualityPanel, ConfigPanel, AuthoringPanel, MemoryPanel, RegistryPanel, GpuPanel, HyperImportancePanel, CrossRunPanel, HypothesisBoard (+_CardKanban/_CardKanbanCard/_HypothesisFallback), ComparePanel, EventExplorer, ArtifactsPanel. ConfigPanel alone spans 562-1048 with its own mutation-fencing/reconcile machinery; the Card board (1319-1953) contains an optimistic-control mini-framework (cardControlReflected/_cardWithOptimisticControls/sentEditRef pruning). RunView deliberately funnels all of these through one lazy chunk (loadPanels), so file layout is free to change without affecting bundling.

*Recommendation:* Split ConfigPanel and the Card board (with _HypothesisFallback) into their own modules re-exported through panels.jsx; the single-chunk lazy strategy in RunView is preserved by keeping panels.jsx as the barrel.

#### UI-05 · MEDIUM · under-decomposition · effort: large

**AssistantBar.jsx: 2,031-line component mixing 3 view layouts, session lifecycle, stream+recovery, share management, and the duplicated command machine**

*Locations:* `ui/src/AssistantBar.jsx:165-2031`, `ui/src/AssistantBar.jsx:1926-2012`, `ui/src/AssistantBar.jsx:501-618`, `ui/src/AssistantBar.jsx:1267-1406`

*Evidence:* Beyond the duplicated direct-command machine (see separate finding), the component inlines: openSession (~120 lines with a nested exact-recovery async state machine, 501-618), runLLM streaming with two concurrent fallback poll loops (1267-1406), and two ~55/30-line share/unshare async handlers written directly inside JSX onClick props (1926-1979, 1981-2012) — control flow with clipboard fallbacks, tombstones, and fence refs living inside the render tree.

*Recommendation:* Lift share/unshare into named handlers or a useAssistantShare hook; extract session management (openSession/newChat/delSession/tombstones) into a useAssistantSessions hook; the three view layouts can then be sibling components receiving one shared controller.

#### UI-06 · MEDIUM · inconsistency · effort: large

**At least seven independent implementations of the load/last-good/stale/retry resource pattern**

*Locations:* `ui/src/RunList.jsx:41-64`, `ui/src/panels.jsx:103-147`, `ui/src/panels.jsx:2101-2115`, `ui/src/panels.jsx:295-304`, `ui/src/RunView.jsx:569-608`, `ui/src/Inspector.jsx:133-301`, `ui/src/App.jsx:39-61`

*Evidence:* The same concept — fetch with deadline, keep last-good data, mark stale on failure, expose retry — is re-derived as RunList.useResource, panels.usePanelResource (with in-flight lock + poll), panels.useNodeResource, TrustPanel's inline configResource effect, RunView's configResource effect (retrying flag variant), Inspector's ~170-line detailResource machine (scope-fenced flights with supersede/mapLastGood/onSettled), and App's ReviewRoute effect; ConceptView, CollabPanel, ResearchAtlas, OwnerAuth and RunCompare each add further variants (grep for status:'loading' hits 13 files). Each differs subtly in abort handling, stale semantics, and pending labels, so fixes (e.g. the out-of-order-response fences several of them add) don't propagate.

*Recommendation:* Promote one shared hook family into hooks.js — usePanelResource already has the right shape (lock, last-good, retry, optional poll); Inspector's supersede/mapLastGood needs are the superset to design for. Migrate incrementally, starting with the three trivial variants (TrustPanel, RunView config, ReviewRoute).

#### UI-07 · MEDIUM · duplication · effort: small

**~15 copy-pasted 'CONTROL.x → commandFeedback(labels) → onToast' blocks across panels/RunView/Inspector**

*Locations:* `ui/src/panels.jsx:306-313`, `ui/src/panels.jsx:443-450`, `ui/src/panels.jsx:208-217`, `ui/src/panels.jsx:1913-1924`, `ui/src/panels.jsx:1985-2019`, `ui/src/RunView.jsx:1079-1125`, `ui/src/Inspector.jsx:67-80`, `ui/src/Inspector.jsx:575-586`

*Evidence:* The identical 8-10-line try/await CONTROL.<cmd>/commandFeedback({success,noop,executing,failure})/onToast/catch-toast wrapper appears in TrustPanel.quarantine, QueuePanel.cancel (both wrapping CONTROL.nodeAbort with only label text differing), ResearchPanel.steer, _CardKanban.addCard vs _HypothesisFallback.add (near-identical addHypothesis blocks), _HypothesisFallback.abandon/del, four branches of RunView.onNodeAction (plus the merge-confirm handler at 1248), Inspector.ResetBtn.doReset, and StagePipeline.rerun.

*Recommendation:* Add a small helper (e.g. submitCommand(promise, labels, onToast) returning the feedback) next to commandFeedback in api.js and replace the copies; label objects stay at call sites.

#### UI-08 · MEDIUM · flat-code · effort: medium

**Dock.jsx carries ~300 lines of pure narration data as two hand-synced parallel registries (NARR + NARR_VALID)**

*Locations:* `ui/src/Dock.jsx:46-170`, `ui/src/Dock.jsx:182-255`, `ui/src/Dock.jsx:266-297`, `ui/src/Dock.jsx:628-653`

*Evidence:* NARR (94 event-type render entries) and NARR_VALID (64 validator entries) are separate objects keyed by the same event types and must be updated in tandem; the file's own comments record past drift ('the duplicate keys here were dead: the later definitions always won. arch-review §5 P3'). GROUPS/TYPE2GROUP/GROUP_GLYPH/TYPE_GLYPH add two more per-type tables. All of this is pure data + pure functions (eventNarration, kindOf) living inside a 1,605-line component file, while the sibling pure module timelineModel.js already exists and is unit-tested.

*Recommendation:* Merge NARR/NARR_VALID into one table of {validate?, render} entries and move it (with GROUPS/kindOf/eventNarration) into timelineModel.js or a new narration.js so it gains direct unit-test coverage and Dock returns to being a component.

#### UI-09 · MEDIUM · under-decomposition · effort: large

**useRunState: one 330-line useEffect interleaving three connection state machines; Inspector.Trace is a ~510-line function**

*Locations:* `ui/src/hooks.js:96-448`, `ui/src/Inspector.jsx:1409-1918`, `ui/src/AssistantBar.jsx:501-618`

*Evidence:* useRunState's single effect implements the owner SSE stream (backoff ramp, cursor validation), the terminal lifecycle probe (its own delay ramp + visibility handling), and the review poll loop, coordinated through ~20 mutable closure variables (stopped, timer, pollTimer, lastSeq, terminalMode, ownerEnded, reviewTerminal, reviewPollRunning, ...). Inspector's Trace function (1409-1918) combines view toggling, the clear-trace confirm/busy/verify/blocked recovery phases, polling, and rendering in one closure.

*Recommendation:* Extract the connection logic into a plain (non-React) state-machine object with injected timers/fetchers — the same pattern the codebase already uses for pure models — leaving useRunState as a thin subscription wrapper; split Trace's clear-recovery phases into a useTraceClear hook.

#### UI-10 · LOW · dead-code · effort: small

**Dead component: Inspector.Agent is never rendered**

*Locations:* `ui/src/Inspector.jsx:2128-2145`

*Evidence:* function Agent({ n }) renders an agent_report KV grid + validation-check table, but no JSX in the repo references <Agent (verified by grep across ui/src and ui/test; only AgentReport is used, at Inspector.jsx:1875-1916). The file's top comment explains the old 'Reasoning / LLM / Agent' tab split was replaced by the single Trace tab, and TABS contains no 'Agent' entry — this is a leftover.

*Recommendation:* Delete the Agent function (AgentReport stays).

#### UI-11 · LOW · inconsistency · effort: small

**runApiPath is documented as 'one constructor for every owner-style per-run endpoint' but ~15 endpoints in the same file bypass it**

*Locations:* `ui/src/api.js:14-24`, `ui/src/api.js:685`, `ui/src/api.js:798`, `ui/src/api.js:825`, `ui/src/api.js:1571-1573`, `ui/src/api.js:1610-1639`, `ui/src/api.js:2184-2189`

*Evidence:* The comment on runApiPath (line 14) states the identity-boundary rationale, yet getRunGeneration, submitRunCommand, getRunCommand, retryRunCommand, resetRun, assignRun, renameRun, runComments, commentHistory, createRunReview/list/revoke, spanDetail and others inline `/api/runs/${encodeURIComponent(runId)}/…` template literals in the same module. All still encode the id, so this is a consistency/documentation debt rather than a safety bug — but the invariant the comment promises is unenforced.

*Recommendation:* Convert the inline sites to runApiPath/runNodeApiPath (mechanical change), or soften the comment; a simple grep-based test could then enforce it like the repo's other registry-guard tests.

#### UI-12 · LOW · duplication · effort: small

**Four coexisting request-timeout wrappers**

*Locations:* `ui/src/requestDeadline.js:1`, `ui/src/api.js:743-775`, `ui/src/api.js:1509-1510`, `ui/src/AssistantBar.jsx:60`

*Evidence:* deadlineRequest (requestDeadline.js), commandFetch's own AbortController+Promise.race deadline (api.js:743, justified by a comment about body-read lifetime), deadlineGet (api.js:1509, a thin deadlineRequest wrapper), and AssistantBar's boundedRequest (a one-line deadlineRequest.promise alias) all implement 'fetch with a deadline'. Components pick among them ad hoc.

*Recommendation:* Keep deadlineRequest and commandFetch (distinct semantics), fold boundedRequest into deadlineRequest usage, and document when each applies.

#### UI-13 · LOW · duplication · effort: small

**Three independent toast implementations**

*Locations:* `ui/src/RunView.jsx:643-649`, `ui/src/AssistantBar.jsx:431`, `ui/src/Settings.jsx:1`

*Evidence:* RunView.showToast (5s timer, .toast div), AssistantBar.flash (5s timer, mountedRef guard, .cmdbar-toast variants including run-change suppression logic at 1569), and Settings' own toast state each re-implement the timer-reset pattern; toastTimerDiscipline.test.js exists to police the subtle clear-previous-timer bug that this duplication invites.

*Recommendation:* One useToast hook (timer reset, unmount guard) with presentation left to callers.

#### UI-14 · LOW · over-engineering · effort: small

**Speculative Card kanban lanes acknowledged unreachable by their own comment**

*Locations:* `ui/src/panels.jsx:1329-1345`

*Evidence:* _CARD_COLUMNS defines 'speculating' and 'built-awaiting-commit' lanes with an inline comment stating 'these speculative lanes are unreachable from the production Card projection… project a bounded speculative owner state before advertising these lanes'; _CARD_OPTIONAL_STATUSES hides them unless occupied, so today they are dead configuration carried in the render path.

*Recommendation:* Either implement the owner-state projection that feeds them or drop the two lanes until the backend publishes those statuses (the extra-lane fallback in _cardLanes already handles unknown statuses generically).


## 5. Remediation status ledger (2026-08-02, HEAD `41813bd`)

Between the baseline `756ad13` and `master` HEAD `41813bd`, 43 commits landed. A dedicated
status pass re-checked every one of the 188 findings against the current tree (one checker per
section, re-running the findings' greps and attributing changes via `git log`/`git diff`).
Headline: **4 findings fixed, 1 partially fixed, 183 open**. All five remediations trace to one
commit — `c92b89f` («perf: stop blocking the ASGI event loop, and make two hot paths
sub-quadratic»), which closed the whole `[PERF]`-marker backlog. The other ~42 commits are
behavioral work (mobile/UI hardening, settings/attention/assistant features, test re-pointing)
that performed **no structural extractions** — and §5.3 shows several flagged god-modules grew
meaningfully in the same window.

**Updates through `a077d86` (2026-08-02).** Commits kept landing while this ledger was
maintained; each was checked against it. `70b6a5d` fixes a live/persisted digest-source mismatch
*inside* the speculation gate without touching SE-01's structural claim (the whole-repo raw-byte
source hash). Then `a077d86` became the **first structural commit to act on this document**: it
executed §3's deletion list (the seven zero-reference helpers plus `METRIC_READERS`, ~97 lines
of code removed, with the §3 rows flipped to DELETED in the same change — the §6.8 ledger-upkeep rule in
action). That flips **XP-08, RA-04 and SE-13 to fixed and SR-13 to partial**. Immediately after,
`cea97c3` executed the first slice of §6.1's safety kernel — in its post-validation form
(`core/pathsafe.py` with a stat-taking `is_reparse`, `WINDOWS_RESERVED`,
`filesystem_identity`; `atomicio.file_identity`) — collapsing all **eight** `_is_reparse`
copies (the review's seven plus `misc.py`'s identically-bodied sibling), the four
reserved-name sets and the fs-identity rule, and porting a first tranche of stat-tuple sites.
That flips **SC-03 and XP-02 to partial**, bringing the running total to **7 fixed, 4 partial,
177 open**.

### 5.1 Fixed / partially fixed

| Finding | Status | What happened |
|---|---|---|
| EC-15 | **fixed** | Re-confirmed on HEAD: commit c92b89f (2026-08-01) added the _invalid_pin_verdict memo (strategy.py:1213/1228) keyed on the pin verdict so an invalid pin short-circuits before _strategy_ctx off-cadence, and the 'CLAUDE REVIEW: [PERF]' marker was rewritten as a past-tense why-comment (no marker remains). No regression since. |
| EV-07 | **fixed** | Re-confirmed on HEAD: fixed by c92b89f. read_all now uses the two-arm design — bounded head/tail windows (_read_prefix_windows) on every poll, full-prefix sha256 proof only on first external observation and each prefix doubling (_full_verified_bytes gate) — amortized O(1) per appended byte; the [PERF] marker is gone (replaced by a why-comment) and tests/test_eventstore_cache.py gained the pinning tests. |
| SC-08 | **fixed** | Confirmed fixed on HEAD by c92b89f (per the doc's post-baseline note): no 'CLAUDE REVIEW' marker remains anywhere in serve/, both decision baselines now read self._observe(rd).latest_seq (run_commands.py 3764 and 3824) and the full-log self._events(rd) read is gone. |
| SR-08 | **fixed** | Re-confirmed the post-baseline note: fixed by c92b89f ('perf: stop blocking the ASGI event loop...', with f3586c9 for the settings/secret/project lock sites). Zero 'CLAUDE REVIEW' markers remain anywhere in looplab/, anyio.to_thread offloads are present in every flagged router (org 5, misc 4, boss 12, genesis 2, control 8, runs 6, reports 1, assistant 3), and the runs.py span read path is bounded via events/span_index. No regression observed. |
| XP-08 | **fixed** | `a077d86` (2026-08-02) deleted all seven zero-reference helpers (locked_claim_evidence_snapshot, claim_governance_snapshot, build_digest, _scope_action_lease_marker_exists, _normalized_rename_map/_canon_set, _explored_concepts). |
| RA-04 | **fixed** | `a077d86` deleted the dead METRIC_READERS registry with the false "shared" docstring (the duplicated reader-kind enumerations in read_metric/_valid_metric_kind remain, tracked under RA-05). |
| SE-13 | **fixed** | `a077d86` deleted the unreferenced _explored_concepts wrapper. |
| SR-13 | **partial** | `a077d86` deleted _scope_action_lease_marker_exists; the /api/research and agents_md endpoints and the _portfolio_identity wrapper remain (pending the external-consumer check). |
| SC-03 | **partial** | `cea97c3` (2026-08-02) unified the `_is_reparse`×8, reserved-name×4 and fs-identity×3 micro-helpers into `core/pathsafe.py`; the 6+ full canonical run-path validators (`validate_run_child` and the per-surface ladders) remain — §6.1's remaining slice. |
| XP-02 | **partial** | `cea97c3` added the canonical `atomicio.file_identity` and ported a first tranche of sites (fences, train_monitor, log_pages, scope_sources, paid_work); inline identity tuples remain in appstate, _runcache, routers/runs, span_index/traceview. |
| SR-15 | **partial** | Confirmed the doc's post-baseline note on HEAD: c92b89f extracted _boss_prologue (boss.py:619), now shared by chat (649), suggest (692) and command (740), removing the prologue duplication for those three. The four-copy error-shaping epilogue remains (_sanitized_domain_http_exception + _safe_boss_failure blocks at 589-594 chat_compact, 662-667 chat, 719-724 suggest, 814/853 command), and chat_compact still keeps its own prologue. |

### 5.2 Still open — everything else (183 findings)

Every finding not listed in §5.1 remains open essentially as written; the flagged structures
were re-located by symbol on HEAD (line numbers drift by up to ~100 lines in the files the
recent commits touched). Per-section outcome of the status pass:

| § | Scope | Post-baseline commits in scope | Outcome |
|---|---|---|---|
| 4.1 | engine — execution spine | only `c92b89f` (strategist memo, +5 lines) and `e3f3a56` (credential plumbing) touched the engine spine | all 14 open |
| 4.2 | engine — cadence/monitoring/wrap-up | `c92b89f` fixed EC-15; speculation/proposal_cues/novelty/ablation/monitors/costs untouched | 14 open, EC-15 fixed |
| 4.3 | engine — cross-run memory & knowledge | `git log 756ad13..HEAD` over the whole 4.3 scope is EMPTY — line counts match the review byte-for-byte | all 15 open |
| 4.4 | events | only `eventstore.py` changed (`c92b89f`, the EV-07 two-arm redesign); replay.py and the projections untouched | 12 open, EV-07 fixed |
| 4.5 | core | `e3f3a56`/`9275736` ADDED ~400 lines to the flagged god-modules (`llm.py` grew to 1,865 lines) | all 13 open |
| 4.6 | serve — non-router | behavior/feature commits; `run_commands.py` grew to 4,164 lines; `cea97c3` unified the path-safety micro-helpers | 14 open, SC-08 fixed, SC-03 partial |
| 4.7 | serve — routers | `c92b89f`/`f3586c9` perf offloads; `a077d86` deleted one dead helper; router god-modules grew (`misc.py` +1.2k lines) | 12 open, SR-08 fixed, SR-13 + SR-15 partial |
| 4.8 | search | untouched until `a077d86` deleted two dead helpers (`_explored_concepts`, `_normalized_rename_map`/`_canon_set`) | 14 open, SE-13 fixed |
| 4.9 | agents | one commit (`e3f3a56`) touched cli_agent/tool_loop with credential plumbing unrelated to the findings | all 10 open |
| 4.10 | tools | `927dfee` added +867 lines to `write_tools.py` (undo/destructive fencing); `e3f3a56` hardened transports — neither addressed a flagged structure | all 11 open |
| 4.11 | runtime + adapters | `e3f3a56` (client binding, `git_subprocess_env`); `a077d86` deleted METRIC_READERS | 9 open, RA-04 fixed |
| 4.12 | cli/trust/misc | one commit (`e3f3a56`) rerouted client construction through `make_llm_client_for`; the flagged duplications persist | all 15 open |
| 4.13 | cross-package | perf/behavior fixes until `a077d86` (§3 deletions) and `cea97c3` (`file_identity` + pathsafe) | 10 open, XP-08 fixed, XP-02 partial |
| 4.14 | ui | 25 `fix(ui)` commits, all behavioral/mobile/security; none of the recommended extractions happened | all 14 open |

### 5.3 Size drift since the baseline (the accretion is live)

The same window that fixed the `[PERF]` backlog also demonstrated §1's re-accretion pattern in
fast-forward — the largest flagged files grew, none shrank:

| File | Baseline `756ad13` | HEAD `41813bd` | Delta |
|---|---|---|---|
| `ui/src/panels.jsx` | 2,351 | 4,042 | **+1,691** |
| `looplab/serve/routers/misc.py` | 737 | 1,927 | **+1,190** |
| `ui/src/AssistantBar.jsx` | 2,031 | 2,841 | **+810** |
| `looplab/tools/write_tools.py` | 411 | 1,211 | **+800** |
| `ui/src/Settings.jsx` | 519 | 1,277 | **+758** |
| `ui/src/api.js` | 2,216 | 2,790 | **+574** |
| `looplab/core/llm.py` | 1,549 | 1,865 | **+316** |
| `looplab/serve/settings_store.py` | 314 | 500 | **+186** |
| `looplab/serve/run_commands.py` | 4,103 | 4,164 | **+61** |
| `looplab/engine/orchestrator.py` | 5,880 | 5,890 | +10 |

This is not a criticism of those commits — they are behavior and security work the product
needed — but it is direct evidence for §6.8: without a ratchet, the god-modules absorb every
feature by default.

## 6. Architectural resolution proposals

§4's per-finding recommendations are local fixes. This section proposes the **target designs**
that resolve the finding *clusters* — what the tree should look like so the same debt does not
re-accrete. Each proposal names its driving findings, the concrete module layout, the migration
path, and the invariant it must preserve. §6 absorbs the cluster-level P1 items of §2 (the five
P1 singles — EV-03/EV-05/EV-06 in §6.2, RA-03/RA-06 in §6.1 — are folded into the kernels so
nothing from P1 is orphaned); every proposal is behavior-preserving unless explicitly marked as
a **decision item**.

> *Validation note (2026-08-02).* This section was itself adversarially validated against
> `master` HEAD `a077d86` by nine independent design reviewers (one per subsection plus a
> cross-cutting pass) checking facts, naming collisions, layering, seam preservation and
> feasibility; their three blockers and ~60 corrections are folded into the text below. The
> recurring lessons: name-check the tree before coining a module (`core/_pathsafe.py`,
> `core/validate.py`, `serve/paid_work.py` and `events/readmodel.py` all already exist), splits
> are preserved by **re-export barrels** (the `llm.py`/`agent.py` pattern — the `_LAYOUT` shim
> only aliases flat pre-split names), and every extraction must carry its monkeypatch seams and
> its CLAUDE.md/docs/diagram updates in the same change.

### 6.1 A shared safety kernel in `core/` (resolves most of T1)

The highest-risk duplication class is small hand-copied *safety* helpers. Consolidate them into
core, reusing the homes that already exist rather than coining near-collisions:

| Home | Contents | Replaces (findings) |
|---|---|---|
| `core/pathsafe.py` — the existing `core/_pathsafe.py` promoted to a public name (`_pathsafe` kept as an import alias for its six current importers; comments move verbatim) | its current tool-provider guards (`looks_secret`/`resolve_within`/…) **plus** the run-path family: `is_reparse(info: os.stat_result)` (stat-taking, matching all seven copies — callers lstat once and reuse the result inside a TOCTOU bracket), `WINDOWS_RESERVED`, `filesystem_identity(name)` (NFD-casefold/normcase), and `validate_run_child(root, run_id, *, require_events, check_conflict, reserved_prefixes)` returning a typed verdict / raising one core exception with a reason enum — **each serve caller maps reasons onto its own pinned HTTP status/code set at the edge** (core must not shape HTTP errors; fastapi is an optional extra and the refusal-code sets are security-test-pinned) | 7× `_is_reparse`, 4× reserved-name set (content-identical, so that merge is behavior-preserving), 3× fs-identity rule, the shared core of the 6+ run-path validators (SC-03); the fence-module twins consume it (CO-01). Surface-specific checks (launch's name-conflict + deletion-fence probe, deletion's uniform-404 policy) stay at the edges |
| `core/atomicio.py` (extend) | `file_identity(stat) -> 5-tuple` with the canonical why-comment; `durable_no_replace_rename(src, dst, *, label)` (the ctypes renameat2/renamex_np/Windows write-through mover — both current copies already import `_windows_move_write_through` from here) | the ~6 exact-shape stat tuples port mechanically; deliberate-subset sites (train_monitor's 2-tuple, scope_sources' st_mode-for-ctime, log_pages' metadata pair, runs.py's 4-field key) keep their shapes but document the subset against the canonical definition; the four fence/receipt identity tuples (run_reset/run_deletion/reset_transaction/deletion_transaction) port **only under an equality test pinning tuple contents and order** — they are durable-identity brackets (XP-02); the mover pair (SC-05) |
| `core/parse.py` (extend — it already claims the scalar-coercion charter) + a new `core/digests.py` | `bounded_int(v, lo, hi)` / `bounded_str(v, max_len)` beside the existing coercers; `valid_hex_digest(v, prefix)` and `canonical_json_digest(payload, *, prefix)` in `digests.py`. (Deliberately **not** `core/validators.py` — `core/validate.py` already exists as the unrelated ADR-7 CLI-agent output validator, and a one-letter sibling name is exactly the silent-typo trap this codebase documents) | ~20 hex-digest blocks and dozens of scalar guards (EV-04); the **four canonical-JSON minters'** dump+hash scaffolding (CO-08 — `idea_proposal_digest`, `_card_action_digest`, `stable_advisory_ref`, `verifier_evidence_digest`, whose dump kwargs are byte-identical). **Excluded:** `hypothesis_statement_digest` (sha256 over a normalized string) and `run_setup_key` (md5 over a joined argv string) are frozen non-JSON durable identities that must not be rerouted; five word-token regex copies (EM-15) also consolidate here or in a tiny `core/_text.py` |
| `core/redact.py` (extend) | `bounded_redacted_tree(value, *, max_chars, max_items, max_depth, max_total_items, per_string_cap, strict_int_typing, guard_exceptions, drop_empty_keys, secret_policy)` — the divergent axes of the two existing walkers become explicit policy parameters | the tracing/advisory walker pair (CO-06) — noting advisory's sanitized projections feed `stable_advisory_ref` digest preimages, so the advisory-side port lands behind a golden-digest test; of the router projectors (SR-06) only the two near-identical walkers (genesis/misc) port now — `_scrub_json` may later adopt the core with its key-redaction policy parameterized, and the assistant **allow-list** projectors stay bespoke (an allow-list is not a bounded walk) |

Two further P1 single-spelling extractions land alongside the kernel: the shared Docker
hardening argv builder + `require_docker_cli` in `runtime/sandbox.py`, composed by both
untrusted tiers (RA-03); and one shared `direction` validator (an `Annotated Literal["min",
"max"]` type in core) applied to **all nine** registered task models (RA-06). Frozen digest
preimages keep their byte-exact builders and only route the final dump+hash through the shared
tail — identity stability is the invariant. The CLAUDE.md `core/` package-map row (and any
docs/guide page describing these helpers) is updated in the same change that lands the kernel.

### 6.2 One spelling for the engine's append protocols (ES-02, EC-03, ES-07, ES-06, ES-11 + EV-03/EV-05/EV-06)

The engine's replay-safety discipline is uniform in *intent* but hand-spelled per site. Add a
small protocol layer used by every append path:

- `events/eventstore.py::retry_tail_cas(store, plan, *, attempts=64, on_exhaust)` — the one
  read→check→append(`expected_last_seq`)→retry loop. The `plan` callback returns either an
  abort result or a **zero-arg append closure** the helper invokes — this covers the site that
  must route its CAS append through the single `_emit_node_created` emitter and lets each site
  keep its own lock wrapping. Lock scope is load-bearing and stays per-site:
  `_reserve_node_build` calls the helper *inside* its existing `with self._id_lock:` (serial id
  reservation), while the three narrow-lock sites keep the scan/fold/plan outside the lock as
  their comments prescribe. Exhaustion behavior (`RuntimeError` vs `return False`) becomes the
  explicit `on_exhaust` argument instead of accidental divergence.
- `_commit_built_node(reservation, code, files, ...)` — the shared post-build epilogue
  (parent-refetch guard → `_emit_node_created` → landed-check → developer-error sentinel →
  telemetry consumption), **homed in `orchestrator.py` beside its three callers**. It cannot
  move to `node_build.py`: the epilogue's re-folds are module-global `fold` calls that tests
  monkeypatch through the orchestrator namespace, `node_build.py`'s own docstring documents the
  three creation paths as "DELIBERATELY NOT MOVED" for that reason, and a reverse top-level
  import would cycle. The per-path divergences become explicit parameters/hooks (landed-check
  predicate, `materialize_abort` first-terminal branch, files/deleted source, generation
  payload, telemetry gating and pooled-roles) so the extraction reviews as parameterized
  unification, not textual dedupe. The two fold-monkeypatch test files are re-verified to still
  intercept in the same change.
- `append_developer_crash(...)` — one records-builder/appender for the
  `node_failed(reason='developer_crash')` + pause transaction, parameterized by reason text,
  pause routing (queued vs direct), CAS discipline (tail-CAS `append_many` at the speculation
  sites vs today's plain sequential appends at the orchestrator sites — unifying the latter
  onto CAS would be an improvement, not preservation: a marked decision item), and the
  speculation sites' `_create_paused = True` side effect. The pause-only recovery branch of
  `_close_developer_sentinel_once` (which restores a lost pause with *no* terminal) either
  stays hand-rolled or uses an omit-terminal mode. The worker-seam rule is not a trusted
  boolean: the orchestrator's why-comment block (why a worker-written `EV_PAUSE` races
  `EV_RESUME` for byte position) moves verbatim into the helper, which **asserts** queued
  routing whenever invoked off the main task, mirroring the existing membership-assert pattern.
- `core/models.py::DEVELOPER_ERROR_PREFIX` + `is_developer_error(code)` — producer and all six
  consumers import it; a two-way source-scan test pins it (ES-11).
- `_eval_admission_current(state, node, generation, max_es)` shared by the serial and parallel
  dispatch branches (ES-06).
- The events-layer P1 singles land here too: `_invalidate_completion_certificates(st, ctx)`
  called from all five fold handlers (EV-03); one tolerant-JSONL-prefix scanner under the four
  hand-synchronized copies (EV-05); and `_locked_append` extracted under
  `EventStore.append`/`append_many` (EV-06).
- A tail-keyed `fold_cached` memo to retire the per-iteration re-fold apologies (ES-12) —
  specified precisely, because appends reach the log from outside the Engine (serve
  cross-process, eval worker threads, the background research task), so invalidation **cannot**
  hook append sites. Contract: on every call compare (first-event object identity, tail seq,
  snapshot length, **fold-callable identity**) against the freshly read snapshot (the
  `_ack_commands` cursor technique); store key+state as one atomically swapped tuple; each
  converted call site passes its own module-global `fold` so per-module patch seams keep
  resolving. The enabling precondition is `RunState`'s documented "never mutated except by
  replay.fold" contract (a cache hit returns a shared instance). Adopting it **amends CLAUDE.md
  invariant #4's wording in the same change** (the memo preserves the invariant's purpose —
  any log movement invalidates — but the current text forbids caching derived state outright).

### 6.3 Finish the Card subsystem's move out of the two god-modules (ES-01, EV-01, CO-02)

The Card feature currently lives as three accretions inside other subsystems' files: the
write-side ledger in `orchestrator.py` (~1,000 lines), the read-side derivation in `replay.py`
(~2,200 lines), and identity/digest machinery in `core/models.py` (~800 lines). Target layout:

- `core/cards.py` — Card identity: the digest/receipt family and the provenance models,
  re-exported through `models.py` via the existing compatibility-seam pattern. Unlike
  `concepts.py` (a true leaf), the card family has dependencies pointing back into `models.py`
  — so `cards.py` also absorbs the leaf helpers it needs (`durable_idea_payload`,
  `hypothesis_id` and its statement normalizers), with `models.py` re-exporting all of them;
  otherwise the re-export import cycles at load.
- `engine/card_ledger.py` — a **seventeenth mixin** (making the Engine span eighteen files)
  holding the reservation/receipt write side — ES-01's nineteen-helper cluster,
  `_canonical_card_id` … `_mirror_hypothesis_card_merges`. The cluster contains six
  module-global `fold` call sites (one, `_reserve_node_build`, on the default creation path),
  so the mixin binds its own `fold` from `looplab.events.replay` like every other extracted
  mixin, and the fold-monkeypatching tests (`test_creation_runaway_guard`,
  `test_gpu_resources`, `test_hypothesis_merge`) are extended to patch
  `looplab.engine.card_ledger.fold` **in the same change** and re-verified to intercept — the
  repo already shipped exactly this breakage once during the research_cadence extraction. The
  orchestrator's fold-seam comment stays true for the three creation functions that remain.
- `events/cards.py` — the bounded-receipt fold helpers plus `derive_cards(state)` decomposed
  into its numbered phases as pure top-level functions; `_finalize_fold` keeps calling one
  entry point. `replay.py` keeps `_derive_cards` (plus the `_FoldCtx`/handler names that four
  test files import) as compatibility re-exports — the `_LAYOUT` shim cannot help with
  intra-package symbol moves. A normalization adapter converts the legacy hypothesis-board
  events into card-shaped rows **once at the top** so the derivation reasons over one input
  family — the behavior-preserving half of EV-13 (the shadow-family *retirement* stays a P5
  decision item).

Invariants: the fold stays deterministic and order-tolerant (the pass is already pure over
`RunState`); old logs must replay byte-identically — the existing golden-replay tests are the
pin. Each extraction commit also updates the CLAUDE.md package map (the mixin count/list —
"SEVENTEEN files … sixteen mixins" goes stale — plus the `events/` and `core/` rows), the
affected docs/guide pages, and `docs/infographic/agent-architecture.html` (which references the
Card subsystem ~14 times), per the repo's same-change rule.

### 6.4 A durable paid-work service for `serve/` (SR-01, SR-02, SR-03, SC-06)

Five-plus hand-rolled implementations of "claim → do paid/destructive work → terminal receipt →
generation fence → crash reconciliation" is the layer's biggest bug surface. Target:

- `serve/paid_ledger.py` — the **operation-idempotency (claim/terminal) ledger** half,
  explicitly **composing the existing `serve/paid_work.py`** (whose docstring already owns the
  other half of this boundary: the generation-bound metering lease and cost reconciliation).
  Both docstrings state the split: *paid_work = generation-bound metering/attribution lease;
  paid_ledger = per-operation claim→terminal receipt ledger that wraps it*. Port boss
  `report_refresh` and the runs concept-lens first: their fsync-confirm helpers are identical
  and their ledger folds share a shape — but the protocol is parameterized by identity field
  names, digest binding and **conflict policy** (report_refresh is first-terminal-wins; the
  lens binds `request_digest` into identity and drops conflicted identities fail-closed, and
  keeps its strict recovery fold as its own strategy). The lens port includes de-closuring the
  `build_router` helpers by threading `srv` explicitly (the boss/reset_route pattern). The
  module **asserts its parameterized claim/terminal event types against an explicit
  allow-list** (DIAGNOSTIC_EVENTS plus the documented serve-appendable folded exceptions) at
  the append site, mirroring the BACKGROUND_APPENDABLE pattern — otherwise a generic serve-side
  append helper becomes the unguarded path around engine invariant #1.
- `serve/durable_op.py` — the **file-ledger** kit shared by reset and deletion (generic receipt
  store with immutable-field/phase tables, fence-binding validator, one
  `preflight_quiescence(srv, rd, *, operation)`); reset/deletion keep their own phase enums
  (reset's unordered frozenset vs deletion's ordered monotonic tuple).
- Extract `serve/trace_clear.py` (mirroring `reset_route.py`'s shape — feasible: the seventeen
  closures capture only `srv`, module constants and each other) and `serve/scope_actions.py`
  (the ~1,400 lines of module-level lease/fence machinery out of `routers/reports.py` **plus**
  the five request-scoped closures of the ~530-line `generate_scope_report_ep`, decomposed into
  functions taking `(reports_dir, scope_type, scope_id, action_id, …)`). This shrinks both
  routers substantially rather than to pure wiring; `control.py`'s start-record reconciliation
  (SR-01's fifth variant) is its own follow-up port.
- `run_commands.py`: split into `spawn_leases.py` / `control_validation.py` + the
  `RunCommandService` orchestrator, with the PID identity probes folded into the existing
  `serve/engine_proc.py` (already the serve-layer process-plumbing module) rather than a third
  process module. `run_commands.py` stays as the re-export barrel for the moved names
  (`normalize_control`, `CONTROL_SPECS`, a derived `CONTROL_DATA_FIELDS` compat mapping —
  tests import them directly). Grow `ControlSpec` into a real per-event strategy record
  `{event_type, engine_policy, postcondition, data_fields, normalize, precondition, decide}` —
  the existing set-equality assertions then *prove* every control event has all handlers; the
  per-event `decide` hooks compose with the existing shared cross-event epilogue rather than
  replacing it. Each extraction updates the CLAUDE.md serve/ package-map row and the
  docs/guide/concepts.md module table in the same change (the process diagram has no
  serve-internal nodes and is unaffected).

### 6.5 Re-modularize the cross-run knowledge subsystem (EM-01, EM-03, EM-05, EM-10, EM-06)

- Split `claims.py` along the section boundaries EM-01 identifies (three of which are
  banner-commented): `claims_health.py` (row validation + receipts — the shared base module the
  other four import), `claims_ledger.py` (decision governance), `research_claims_store.py` (D8
  persistence), `claims_assessments.py` (projections, absorbing `claims_for_memory`),
  `claims_retrieval.py` (context pack + retrieve + atlas, absorbing `atlas_for_memory`). Split
  `memory.py` into `concept_capsules.py` + `lesson_rows.py` (named outside the `lessons_*`
  mixin namespace — it is a pure-function module, not another LessonMemory mixin); fingerprints,
  the M6/M4 helpers and the case libraries stay in `memory.py`. **Compatibility is the
  re-export barrel, not the `_LAYOUT` shim**: `claims.py`/`memory.py` keep re-exporting every
  moved name (the documented `llm.py`/`agent.py` split pattern) so importers and monkeypatch
  seams keep resolving — internal call sites that tests patch keep routing through the barrel
  or late imports; the new modules are additionally registered in `_LAYOUT` because the
  two-way package-layout audit requires it.
- Move the paid-curation transaction out of `lessons.py` as a **`CurationProtocolMixin`** in
  `curation_protocol.py` (verbatim method moves, following the documented
  `lessons_priors`/`distill`/`reconcile` mixin convention) — the protocol members are
  LessonMemory methods that durability tests monkeypatch as *instance attributes*, so a
  free-function extraction would silently detach those seams. **Decision item (EM-03):**
  converging new writes onto the v2 semantic-key shape is a durable-protocol migration of a
  live paid at-most-once path (the v1 begun/terminal generation is written today by the
  HTTP/CLI steward invocations, whose begun-row crash fence and request-digest binding the v2
  finalize shape does not provide for operator retries) — it needs its own at-most-once design
  and an owner; only after it lands can the v1 branch of the row validator become
  legacy-read-only.
- Relocate `_append_governance` to `governance_health.py`, parameterizing the per-ledger
  readers **and** the global-revision hook (or lazy-importing
  `concept_governance_global_revision` — never top-level: `concept_registry` imports
  `governance_health` at module top, so a top-level back-import cycles), and deciding whether
  the `ConceptGovernance*` exception types move with it. Port `record_claim_decision` onto it
  with the semantics parameterized, not assumed: the idempotency-payload extractor (claim rows
  share none of the concept fields the current extractor reads), the revision derivation
  (logical deduped claim count vs physical row count), a persist-continuation hook so the
  `validate_evidence` path keeps its `project_governed_sources(claim_locked=True)` lock nesting
  around the append, and the `Claim*` exception types the HTTP layer maps (an HTTP contract —
  its tests move in the same change). Alternatively scope the port to sharing only the
  lock/append/fsync/durability core.
- One generic receipted-snapshot type (rows + typed receipt + explicit attachments) replacing
  the three list subclasses (EM-09), and one declarative receipt-spec helper for the ~8
  hand-rolled validators (EM-12) — with §6.1's caveat applied: specs must reproduce today's
  exact per-version field sets, bounds and legacy-absent defaults (golden fixtures pin them);
  digest field-lists and consumer projections remain separate contracts the spec does not
  unify.
- **Decision item (EM-06), rescoped:** the *product* default is already the structured claim
  key (`Settings.cross_run_structured_claims=True`); what remains is (a) whether to flip the
  deliberately-frozen bare-library defaults (EngineOptions and the function signatures), which
  requires revising the pinned options-divergence test and the configuration.md row while
  keeping the resume-compat map entry, and (b) deprecating the lean/fuzzy read paths
  (`_fuzzy_merge_claims`, the `_global_key`/`_scoped_key` shadow namespaces, the CLI fuzzy
  flag).
- Each split/relocation commit updates docs/guide/concepts.md's module map (pinned by
  `test_package_layout`), the relevant docs/guide pages and the CLAUDE.md engine package-map
  row in the same change.

### 6.6 Guard the cross-package seams like the in-package ones (T4)

- Export a public read-model facade from the engine for tools/serve consumers — e.g. one
  `engine/knowledge_views.py` (**not** `read_models.py`: `events/readmodel.py` already owns
  that term for the per-run SQLite artifact) re-exporting the capsule/claim views under public
  names, plus a public home for `concept_registry._TOMBSTONE` — with a two-way source-scan test
  pinning the list (XP-01, TO-09). Additionally promote `events.eventstore._interprocess_lock`
  to a public `interprocess_lock` (back-compat alias kept): it is the single most-imported
  cross-package underscore name (serve, cli, engine, tools). With those, engine-internal
  underscore names stop crossing into tools/serve.
- Move the developer-backend key registry into core (the `core/task_kinds.py` pattern) and have
  `agents/cli_agent.PRESETS` assert against it; also derive `cli/__init__.py`'s hardcoded
  `_DEV_BACKENDS` tuple from it and cover `appconfig.py`'s template string in the source-scan
  test, so all four spellings pin to one source (CO-07/XP-04).
- Inject the serve dependencies `machine_runs_tools` needs (config-write lock, liveness/spawn
  probes) as constructor callables — with **fail-closed semantics**: the lifecycle-fence
  callables are required arguments (or, when absent, the mutation verbs refuse with an explicit
  "(run control unavailable: no lifecycle fence bound)"), never no-op; only the read-side
  `alive_fn` keeps its degrade-to-False display behavior. The existing `alive_fn` precedent
  degrades open, which is fine for liveness display and unsafe for the destructive-rewrite
  fences (TO-03/XP-03).
- Move the speculation-calibration **profile-builder block** (not just two constants: the
  settings-defaults walker, the ~105-line overrides literal, the coverage guards and the
  schema-string digest — ~170 lines of import-time machinery) into
  `search/speculation_calibration.py`, accepting that the module gains `core.config`/orjson
  imports and rewriting its two "spelled literally to avoid importing core.config" comments in
  the same change; `orchestrator.py` keeps back-compat re-exports (the existing
  `SPECULATION_CALIBRATION_VARIANT_FIELDS` precedent) for the cli and five test importers. The
  digest value is location-independent, so the move cannot invalidate receipts. Export
  `incomplete_finalize_scope`'s pure helper cluster through `events/` (it keys on
  `events/types.py` constants, so core is not a viable home; `engine/finalize.py` keeps a
  re-export), deleting the lazy search↔engine cycle (SE-07/XP-07).
- Add the missing `STRATEGY_FIELDS` registry + source-scan test (AG-03); document the
  agents↔search import direction in CLAUDE.md (AG-07); extract the shared scan skeleton into
  `tests/_source_scan.py` so the **seven-plus** guard tests (including
  `test_task_adapter_contract`) and the three this section adds stop copy-pasting it (XP-10).
  Each new registry/module lands together with its CLAUDE.md updates (registry enumeration,
  package-map rows) in the same change.
- **Decision item (SE-01):** replace the whole-repo raw-byte speculation-gate hash with a
  versioned semantic manifest (explicit protocol version + hashes of execution-affecting files
  only), and extract the expected finalize/setup event-shape constants into a
  **downward-importable home** — `events/` beside the event-type registry, or
  `speculation_calibration.py` — with the *engine writer* importing them. (Never the reverse:
  having the search-side validator import engine writer modules would re-create the exact
  search→engine edge the previous bullet deletes.)

### 6.7 UI: one command machine, one resource hook, smaller files (UI-01…UI-08, UI-13)

- `useDurableRunCommand(source, runId, {labels, onToast})` — one hook (or pure state-machine
  module) over the already-shared `api.js` envelope layer; Dock and AssistantBar keep only
  their action tables and labels (UI-01 — a T1 member; it ships in §6.9 step 1, not
  opportunistically).
- Split `api.js` along its visible seams (`commandStorage.js`, `eventStream.js`,
  `commandProtocol.js`, `scopeReportActions.js`) — with **`api.js` itself remaining the compat
  barrel** re-exporting the extracted modules: 14 src files and 12 test files import
  `./api.js`/`../src/api.js` directly (only ~20 go through `util.js`), so a util-only barrel
  strands them — the same stranded-test failure mode the repo's contract-change rule warns
  about. Extract the fetch client beneath the four modules first to avoid an
  api↔commandProtocol import cycle (UI-02).
- Promote one shared resource-hook family — by **growing `usePanelResource`** (which already
  has the right shape) toward Inspector's superset (supersede/mapLastGood/onSettled), or by
  migrating `RunList.jsx` first so its exported hook vacates the `useResource` name — and
  migrate the seven variants incrementally (UI-06). One `useToast` (UI-13). A
  `withCommandToast(promise, labels, onToast)` helper for the ~15 CONTROL blocks (UI-07) —
  named to avoid the near-collision with the existing `submitRunCommand` (a different protocol
  layer).
- Move the narration tables into **`narration.js`** with one `{validate?, render}` table
  (UI-08), imported only by Dock — *not* into `timelineModel.js`: that module sits inside the
  review-route bundle closures whose budgets `ui/scripts/check-bundle.mjs` deliberately keeps
  owner-only Dock bytes out of (budgets are "calibrated downward — do not raise them").
  `dockNarration.test.js` is re-pointed (or Dock keeps an `eventNarration` re-export) in the
  same change.
- Extract `useStartOverRecovery` + a `RunShell` wrapper for the six early-return screens from
  RunView (UI-03); split **three** residents out of `panels.jsx`, keeping it as the lazy-chunk
  barrel: ConfigPanel, the Card board complex, and `AuthoringPanel` with its
  authoring-operation recovery helpers — AuthoringPanel grew ~32→~830 lines in the §5.3 drift
  window, so a two-way split no longer leaves a barrel (UI-04).

### 6.8 Guardrails against re-accretion (the meta-problem)

Every god-module in §4 regrew *after* a documented split, so structural fixes need enforcement:

1. **A size-regression source-scan test** in the spirit of the existing registry guards,
   covering **both trees**: file-level line budgets for `looplab/**/*.py` *and*
   `ui/src/**/*.{js,jsx}` (plain line counting — the headline offenders are UI files, which the
   existing looplab-only scan pattern would miss), plus a Python-only ast rule for new
   functions over ~200 lines outside an allow-list. §4 supplies the hotspot *list*; budgets
   seed from the actual line counts at the commit that lands the test and only ratchet DOWN
   from there (seeding from §4's baseline numbers would arrive red by up to +1,691 lines). The
   failure message points at doc 25. The ratchet is not hypothetical: in the single day after
   this review's baseline, `panels.jsx` grew 2,351→4,042 lines and `routers/misc.py` 737→1,927
   (§5.3).
2. **Marker hygiene rule in CLAUDE.md**: a diagnosed-defect marker (`CODEX AGENT:` and
   successors) in `looplab/` must be resolved when a change **alters the behavior** of its
   function — by fixing it, filing a tracked issue, or rewriting it as a marker-free
   why-comment where the content is accepted-limitation documentation (the `c92b89f`
   precedent). Verbatim code moves stay exempt (the repo's comments-move-verbatim convention),
   and test-rationale comments are out of scope. The precedent: all 15 baseline `[PERF]`
   markers were closed within hours of being catalogued here — 13 by `c92b89f`, the two
   settings-lock markers by `f3586c9`.
3. **Ledger upkeep**: when a change fixes a doc-25 finding, flip its entry in §5 in the SAME
   change — `a077d86` already demonstrated the workflow by flipping §3's rows to DELETED in the
   deletion commit itself.
4. **Test-side factories**: add `tests/factories.py` (`make_engine(run_dir, **overrides)` plus
   the common scripted-role stubs) and migrate opportunistically (XP-11; 153 direct `Engine(`
   constructions across 78 test files — CLAUDE.md's own "~100 call sites" is a stale
   undercount) — this is what makes the frozen `Engine(...)` keyword API evolvable at all, and
   it de-risks every §6.2/§6.3 extraction.

### 6.9 Suggested sequencing

0. Land the two §6.8 enablers **first**: `tests/factories.py` (it de-risks every §6.2/§6.3
   extraction, so it cannot come after them) and the §6.8.1 size ratchet (the accretion it
   guards against is measurably live *now*, §5.3). Marker hygiene and ledger upkeep (§6.8.2/3)
   land with the first flagship split.
1. §6.1 + §6.2 next (small, mechanical, each removes a live drift risk; no API changes), plus
   the UI `useDurableRunCommand` hook — the last unshipped T1 member.
2. §6.6 seam guards — cheap, and they make the §6.4–§6.5 moves safe to review (§6.3's safety
   pin is the golden-replay tests, as stated there).
3. §6.3 (Cards) and §6.4 (serve paid-work) as the two flagship splits; each is a bounded series
   of behavior-preserving commits with existing tests as pins.
4. §6.5 and §6.7 opportunistically, module by module — behind the `claims.py`/`memory.py` and
   `api.js` **re-export barrels** respectively (the `llm.py`/P5.2 precedent; the `_LAYOUT` shim
   and `util.js` keep resolving *through* the barrels, they are not the split mechanism).
5. Decision items get owners rather than patches: EM-06 (bare-library structured-key defaults +
   lean/fuzzy deprecation), EM-03 (v2 write-shape convergence), SE-01 (versioned manifest +
   event-shape constants), the developer-crash CAS unification (§6.2), EV-13's shadow-family
   retirement, plus §2's remaining P5 list.

Every extraction/split commit updates the CLAUDE.md package map (including the mixin count/list
and the registry enumeration), the relevant docs/guide page, and — where subsystems the diagram
shows are affected — `docs/infographic/agent-architecture.html`, in the same change; and every
commit that fixes a finding flips its §5 row. That rule is what keeps this document, CLAUDE.md
and the tree telling the same story.
