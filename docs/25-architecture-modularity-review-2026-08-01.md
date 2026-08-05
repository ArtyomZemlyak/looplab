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
remediation ledger in **§5** (running total as of `2d96bed`, §5.4: 43 resolved, 13 partial, 132 open — and
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
  *(resolved 2026-08-04 — the constants had already moved down; the last upward import,
  `incomplete_finalize_scope`'s five-function pure cluster, now lives in `events/finalize_scope.py`.)*
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

*Diagnosed (2026-08-02) — it is GIL starvation of the command-monitor thread, not a logic bug.*
Reproduced deterministically: run the failing test in a loop with one CPU-burning thread per core in
the same process. It fails within ~6 attempts, always with the same signature — the record sits at
`status: "executing"` for the full 60 s poll.

What the instrumentation showed, in order, because two of the three readings were misleading and the
record of a wrong turn is worth as much as the answer:

1. Every clause of the `finished_and_stopped` postcondition is SATISFIED at the moment the test
   gives up — `finished`, engine stopped, `stop_reason="aborted"`, `stop_requested`, no incomplete
   finalize scope, `attached` with a matching intent seq and digest. Evaluating `_postcondition`
   against the ON-DISK record at that instant returns **True**.
2. A first measurement suggested the postcondition returned False. That was an artifact: it was
   evaluated against the record as projected over HTTP, which omits
   `attached_semantic_payload_digest`, so `_attached_finalize_intact` fails closed. The monitor uses
   the on-disk record and is unaffected. Recorded because the same mistake is easy to repeat.
3. A second hypothesis — that the worker thread holds a pre-attach copy of the record, since the
   monitor loop never reloads it — is also WRONG: logging the worker's own dict shows `attached`
   and the digest both present.

The actual cause is scheduling. Logging every 200th loop iteration shows only `iter=1` across a
25 s stall, and a `faulthandler` dump catches the worker inside `_heartbeat_execution`'s
`os.utime`. The monitor polls at `poll_interval` (10 ms) and does real work each pass — a full
`_observe` (read + fold) plus file stats — all of which release and re-acquire the GIL. With
CPU-bound threads saturating the interpreter, it is starved to a handful of iterations over tens of
seconds. In the real suite the "burn threads" are simply other tests' work in the same pytest
process, which is why this is load-dependent, appears in unrelated subsystems, and always passes in
isolation.

*Recommendation, now that the mechanism is known:* raising ceilings again is still the wrong fix,
but so is treating it as a test bug. The durable record's status should not depend on a background
thread winning a scheduling race when any reader can already prove the postcondition holds. The
worker's own terminalization path (`run_commands.py`, around the `sequence(rd)` re-load before a
`timed_out` write) already carries a comment acknowledging that "a completion arriving at the
deadline could be promoted to succeeded by GET" — so the read path is understood to be capable of
promoting. Making a GET evaluate a satisfied postcondition and promote would remove this entire
flake family, and would also be correct behaviour for a real operator polling a finalize while the
box is loaded. Left unimplemented here deliberately: it touches the at-most-once command protocol,
and it deserves its own change with the same byte-level differential treatment as EM-02/EC-01.

### T9 — The UI suite's source-regex idiom had silently retired 30 assertions (added 2026-08-02, RESOLVED)

The `ui/test/*.test.js` files check many properties by matching a regex against the `.jsx`/`.css`
source. It is a cheap idiom for things a jsdom render cannot see (dep lists, layer priorities,
which branch calls the mutating API), and the repo uses it deliberately. It also has a failure mode
that is the browser-side twin of the HTTP-contract drift in CLAUDE.md: **when the production code
gets safer, the anchor stops matching, and the assertion is retired rather than reported.**

Thirty tests were red this way. The distribution is the point — this is not sloppiness, it is the
idiom working against itself:

* **13 anchors broke on a HARDENING.** The node-mutation gate was renamed *and widened* to cover
  lost run authority; the re-tag editor's remount key gained the run generation (so a draft opened
  before a start-over cannot submit against the rebuilt node); the stale-detail latch moved from a
  predicate flag into `finish`/`cancel`, which newly fences the FAILURE path too; `useDialogFocus`
  calls gained explicit layer priorities; a recovery POST gained `acknowledged_live_share_ids`. In
  every case the assertion stopped running at the exact moment the property it guarded got stronger.
* **6 fixtures predated a required envelope field.** `useAttention`'s pages, `useAttentionDeadline`'s
  pages, the settings resource's `credential`, the shared chat's `meta.live`/`expires_at`. A
  protocol-invalid payload is dropped WHOLE by design, so the FIRST assertion threw and every
  fail-closed case below it — disproportionately the security-relevant ones — never ran.
* **5 pinned a behaviour that had been deliberately improved.** A composer that froze on an unknown
  write outcome now offers a non-mutating "Check command"; a retry no longer blanks the error the
  operator is reading; a standalone `attempt=` is now a valid node-lifecycle fence rather than a
  dangling half-link.
* **4 were pure cosmetics** — an attribute added, a line wrapped, a parameter renamed.
* **2 were real defects**, found only because fixing the anchor let the test run: `role="status"` on
  a `<ul>` erased its own `<li>` list semantics (serious axe violation, on the warning list the
  operator reads when a source is stale), and RunList reflected server `message`/`remediation` into
  an alert unbounded, uncoerced, and with an empty-string fallback — so a malformed body rendered
  `[object Object]`, and an envelope carrying neither field rendered a blank alert.

*Resolution:* all 30 fixed, each by re-pointing the anchor at the property rather than the
spelling, and by asserting the improvement that broke it. Where a count or an exact array was the
anchor (`4` route-level mains, an exact effect dep list, a `z-index` literal), it was replaced by
the relation that must hold — every route-main focusable and named; the deps that carry the meaning
with additions permitted; backdrop < drawer < modal overlay — so the next legitimate change reports
the values instead of a bare regex miss.

*All 30 fixed; the UI suite is 648/648.* The last one, `inspectorDetailResource`, held two stale
behaviours at once: that a retry blanks the surface back to "Loading…" (it does not — the alert
stays and its button relabels to "Retrying…" and disables, so the operator keeps the message they
were reading), and that "focus recovery" means focus on the alert CONTAINER. It is focus RETENTION:
after the retry fails, focus stays on the Retry button inside the re-rendered alert. Requiring the
container would yank focus off the control the operator just used. The assertion now names the
button and separately forbids the failure that matters — focus falling back to `document.body`.

*Recommendation:* the idiom is worth keeping, but a source-regex assertion should anchor on the
smallest thing that carries the meaning, and any assertion that can pass vacuously — an `indexOf`
that returns -1, a slice whose end anchor moved, a `[^>]*` that cannot cross a line — needs a
companion check that the scan saw anything at all. Several tests in this file already do this
(`'a source anchor moved; this test reads nothing'`); it should be the default.

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

*Resolution (2026-08-02):* extracted with the recommended signature —
`Engine._consume_batch_proposal(state, width) -> (ideas, telemetry, dropped)` — plus
`_record_dropped_batch_cards(dropped)` for the node-less-card loop, which was a THIRD copy the
finding's location list did not separate out (twice in `run`, once in `_stage_card_creates`).

The reset deliberately stays at the call sites, contrary to the recommendation's parenthetical. The
two orderings are not interchangeable: `run` clears after its reservations are durable so a crash
mid-batch still replays the same reservations, while `_stage_card_creates` clears in a `finally`
because it must not leak into a later repair/legacy build. Folding them together would have made one
of those two orderings wrong. Instead the helper SNAPSHOTS both lists, which is what makes deferring
the reset safe at all.

Two rules in the hand-written reading were load-bearing and silent when wrong, and are now pinned:
telemetry is PADDED to align 1:1 with the ideas (`zip` truncates to the shortest, so a short list
drops the tail of the batch — ideas proposed and gated, then never built), and the results are copied
rather than aliased.

`tests/test_batch_proposal_consume.py` (23) drives both helpers against a minimal host and pins the
padding rules, the snapshot identity, the reason default/truncation, junk-row tolerance, and that
both call sites go through the shared helpers. One test's premise was wrong on the first pass — it
asserted the snapshot survives the caller's RESET, but the reset rebinds and an alias survives a
rebind; it now asserts the identity directly and clears the producer's buffer in place, which is the
hazard a copy actually removes.

A near-miss earned a permanent guard. The first application inserted the new helpers between
`@staticmethod` and `def _node_id_ceiling(`, which re-decorated the NEW function and silently demoted
`_node_id_ceiling` to an instance method — surfacing as "takes 2 positional arguments but 3 were
given" across 57 tests, and only that loudly because that helper is called everywhere. A quieter
neighbour would have failed on one path. `test_the_staticmethods_around_the_new_helpers_are_still_staticmethods`
now checks the decorators directly.

#### ES-09 · LOW · duplication · effort: small

**_apply_control_overrides contains two copy-pasted parallelism-override loops**

*Locations:* `looplab/engine/orchestrator.py:2697-2711`, `looplab/engine/orchestrator.py:2712-2726`

*Evidence:* Two 15-line for-loops over ("max_parallel", "eval_parallel") and ("parallel_build", "llm_parallel") are byte-identical except for the bound (0..1024 vs 0..64) and the target attribute (self._eval_parallel vs self._llm_parallel): same bool-exclusion, same float-integrality check, same int coercion, same try/except tuple.

*Recommendation:* One helper `_override_width(bo, keys, bound, current) -> int` called twice; the legacy-first/canonical-last ordering is preserved by the keys tuple.

*Resolution (2026-08-03):* `engine/widths.py::settle_width(raw, upper)` is the one rule, with
`EVAL_WIDTH_MAX` / `LLM_WIDTH_MAX` naming the two bounds so a call site reads as the axis it settles
rather than as a magic number. All FOUR loops use it — both in `_apply_control_overrides` (ES-09,
operator `budget_extend` controls) and both in `_apply_strategy` (EC-11, Strategist decisions). The
legacy-first/canonical-last ordering is untouched: it lives in the key tuple, not in the validator.

Each of the four rules inside is load-bearing and fails in a DIFFERENT direction if a copy drifts,
and none fails loudly — a rejected width just leaves the running envelope alone, which looks exactly
like a control that was never sent:

* a bool is not a width (`True` is an `int` subclass, so a JSON `true` would serialize the run);
* a non-integral float is refused rather than truncated (2.5 -> 2 is a guess about intent);
* the bound is a REFUSAL, not a clamp (a clamped 100_000 would look accepted and reshape the run);
* a LIVE zero settles to serial 1 and never means AUTO — AUTO belongs to launch-time `Settings`,
  which can read the hardware and the settled eval width; a mid-run zero has no such context.

One behaviour was tightened rather than moved: `_apply_strategy` reconfigured the LLM broker with the
RAW value while assigning `_llm_parallel` the settled one. Those agree today (the broker applies the
identical rule internally), but only because two spellings happened to match; it now passes the
settled width.

The per-lane allocation map in `_apply_strategy` deliberately keeps its OWN, stricter rule: strict
`int` only, and all-or-nothing across the map (one bad lane rejects the whole allocation rather than
silently allocating the rest). Folding it into `settle_width` would loosen a validator whose job is
to be all-or-nothing; `tests/test_width_settling.py` pins that as a decision, not an oversight.

The ops sub-dict block EC-11 also mentions, and the `_apply_strategy` if-chain's remaining
governance-sensitive sections, are left explicit as the finding itself recommends.

#### ES-10 · LOW · inconsistency · effort: medium

**Four GPU-probe implementations with two different nvidia-smi parsers**

*Locations:* `looplab/engine/orchestrator.py:417-442`, `looplab/core/hardware.py:22-44`, `looplab/core/hardware.py:251`, `looplab/engine/resources.py:153-196`

*Evidence:* GPU discovery exists in four forms: orchestrator._detect_gpu_ids (CVD tokens -> torch.cuda.device_count -> counting `nvidia-smi -L` output lines), core/hardware.detect_gpus (nvidia-smi CSV query with comma-in-name repair), core/hardware.effective_gpu_inventory (ctypes CUDA Driver API with UUID/PCI identity), and core/hardware.detect_gpu (first GPU name, kept for back-compat). Two independent nvidia-smi invocation/parsing styles exist (`-L` line counting vs CSV query), and resources.detect_gpu_inventory even contains a defensive cross-check comment ("_detect_gpu_ids derives the same count. A mismatch means one of the probes changed...", resources.py:166-169) — evidence the duplication is a known hazard being papered over with a runtime consistency check.

*Recommendation:* Make core/hardware the single probe owner: _detect_gpu_ids's count should derive from detect_gpus() (falling back to torch), eliminating the `-L` parser and the cross-probe mismatch failure mode that detect_gpu_inventory currently guards against.

*Resolution (2026-08-04) — the second parser is gone; the fail-closed guard stays, because it defends something else.*

`orchestrator._detect_gpu_ids` keeps its ladder (CUDA_VISIBLE_DEVICES → torch → inventory) but its
last step now reads `len(core.hardware.detect_gpus())` instead of counting `nvidia-smi -L` output
lines. `core/hardware` is the sole owner: `query_nvidia_smi` is documented as the one
launcher+CSV-splitter, and `detect_gpus` adds the comma-in-a-GPU-name repair — a GPU whose model name
contains a comma makes a fixed-position CSV read grab a name fragment instead of a memory figure. The
`-L` counter never needed that repair, which is exactly why it was able to sit beside the real probe
looking correct.

**The cross-check in `detect_gpu_inventory` is NOT removed.** The finding reads it as a symptom of
the duplication, and its comment did say "_detect_gpu_ids derives the same count" — but what it
actually guards is the `logical_ids` list arriving from a CALLER, which may be stale or forged.
Nothing about single-sourcing the nvidia-smi parse makes that argument go away, and an empty mapping
is what stops an independently forged reservation escaping the operator's visibility fence through
logical-id fallback. Both branches derive from `cuda_visible_device_tokens`, so the case the comment
literally describes was already impossible; the guard earns its place on the other one.

Pinned by three tests in `tests/test_gpu_resources.py` (40 → 43): the ordinal probe's count tracks
the shared inventory (both a populated box and an empty one), a probe that raises degrades to "no
GPUs" rather than out of `Engine.__init__`, and a source scan that no module outside `core/hardware`
invokes the binary itself. Teeth-tested against 2 breaks, both biting.

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

*Resolution (2026-08-03):* the first option, with `action_governance.py` renamed to
`engine/shared.py` and given a `SharedEngineMixin` — an EIGHTEENTH member of the split, composed last
in the MRO so a concern mixin can still specialize a member exactly as it could when they sat on the
Engine body.

`_agent_may` moved out of `EvalDispatchMixin` and now sits with its own sibling
`effective_researcher_eval_timeout`, which is the pairing that mattered: the timeout helper CALLS the
gate to decide whether a researcher override is even an action, so splitting them across two modules
is how a reader ends up believing there are two governance systems. `_op_span` and `_cadence_due`
moved off `orchestrator.py`, each of which carried a comment explaining that it is shared — which was
the symptom the finding named, not a resolution.

The module's docstring states the BAR for entering it, because the obvious failure mode of "a home
for shared things" is becoming a second god-module: called from more than one cluster AND no state of
its own. A helper used once still belongs where it is used.

Two things the finding's evidence overstated, checked against the code: `_cadence_due` was already a
`staticmethod(cadence_due)` delegating to `engine/cadence.py` (doc 25 EC-07), so only the NAME needed
a home, not the rule; and the flat `looplab.action_governance` alias was retired rather than kept,
since nothing imported it by that path.


### 4.2 Engine — cadence / monitoring / wrap-up mixins

Scope: `looplab/engine/`: strategy, research_cadence, novelty, speculation, ablation, confirm_phase, audit, resources, proposal_cues, train_monitor, asha_monitor, finalize, costs, signal_delivery.

**Reviewer assessment.** This cluster implements the engine's periodic/advisory subsystems as mixins over one Engine object, with a consistently applied replay-safety discipline (at_node idempotence gates, paid-attempt receipts before provider calls, fold-ignored DIAGNOSTIC events for background monitors). The architecture is deliberate and mostly well-documented, but the cluster has accreted heavy near-duplication across sibling features that grew independently: two ~190-line cross-run context builders, two watchdog monitors with copy-pasted resume/loop scaffolding, two ablation paths sharing a near-identical ~45-line tail, a developer-crash terminal+pause pair spelled five times, and a triplicated durable-usage append/verify protocol. speculation.py's 500-line _run_card_session and strategy.py's multi-subsystem sprawl are the main under-decomposition hot spots; speculation.py carries embedded reviewer comments acknowledging its unfixed perf/structure debt.

**Strengths worth preserving:**

- The signal-delivery registry (signal_delivery.py SIGNALS) and its source-scan test turn the classic 'signal folded but no longer injected' regression into a red test — the same registry discipline CLAUDE.md documents for other duck-typed seams, applied consistently here.
- Watchdog logic is factored into pure, unit-testable functions (training_log_digest, next_monitor_sleep, should_monitor_kill, asha_underperforming, extract_resource_curve) with the impure loop kept thin, and the shared claim_watchdog_kill correctly serializes the two monitors' kill race on the cooperative loop.
- Replay/crash-safety discipline is coherent across the cluster: at_node idempotence gates, paid-attempt receipts claimed BEFORE provider calls (research attempts, card_build_attempted, _claim_paid_finalize_step), and finalize.py's dual scope/finish_seq handshake all follow one recognizable pattern.
- Known gaps are honestly annotated with bounds and a closing recipe (e.g. the in-memory _last_hyp_merge_n cadence gap in research_cadence.py:539-548, the concept-snapshot re-purchase gap in strategy.py:703-711), which makes review and future fixes far cheaper than silent debt.
- Fail-closed engineering in resources.py (lease inode/symlink validation, count-only degradation when the memory inventory can't be joined losslessly) and costs.py (self-authenticating outbox records, never erasing conflicting evidence) is thorough and clearly reasoned inline.

#### EC-01 · HIGH · duplication · effort: medium — **RESOLVED (2026-08-02)**

**Two ~190-line near-duplicate cross-run context builders (Strategist note vs Researcher advisory)**

*Locations:* `looplab/engine/strategy.py:139`, `looplab/engine/strategy.py:327`, `looplab/engine/proposal_cues.py:332`, `looplab/engine/proposal_cues.py:518`

*Evidence:* _cross_run_note_for_ctx (strategy.py:139-335) and _cross_run_advisory_text (proposal_cues.py:332-528) implement the same pipeline in parallel: gate on _cross_run_advisory + memory_dir; valid_live_direction check; the identical governance re-entry idiom (`if _governance is None: return project_governed_sources(base, lambda governance: self.<method>(state, _governance=governance), include_concepts=True, source_names=('concept_capsules.jsonl','lessons.jsonl','research_claims.jsonl'))`); load_claim_lessons/ConceptCapsuleStore/load_research_claims with observed_path_missing guard; row filtering by direction/task_id/excluded run_id; a v2 receipt dict with identical keys (scope_task, excluded_run, n_lessons, n_capsules, n_research, corpus_digest, render_digest built via sanitize_cross_run_projection + sha256); identical GovernanceLedgerUnavailable handler emitting {'v':2,'status':'unavailable','complete':False,'governance':exc.public_receipt()}; identical bare-except -> empty receipt + "". Only the middle (atlas summary vs context pack rendering) differs.

*Recommendation:* Extract a shared helper (e.g. engine/cross_run_context.py) that owns: the flag/direction gating, the governed source load + row scoping, and receipt construction (one build_receipt(scope_task, excluded_run, counts, corpus, rendered) function plus the unavailable/empty receipt shapes). Each caller keeps only its distinct projection/rendering middle section. This removes ~250 duplicated lines and, more importantly, prevents the two receipt schemas and scoping rules from drifting.

*Resolution (2026-08-02):* `engine/cross_run_context.py` now owns what both builders duplicated —
the flag/direction gate, the governance re-entry idiom, the governed source load, the row-scoping
predicate, the bounded corpus digest, and the v2 receipt (including its unavailable shape). Each
caller keeps only its distinct middle: an atlas summary for the Strategist, a rendered context pack
for the Researcher.

The receipt is why this mattered more than the line count. It is what an auditor reads to decide
whether "no cross-run evidence opposed this" meant *nothing opposed it* or *the store could not be
read* — and two hand-maintained copies of that schema drift silently until the two agents shaping
the same run stop being comparable. One consequence fell out immediately: the two builders scoped
RESEARCH rows through separately-written predicates that happened to agree; they now share
`visible_row_predicate`.

*Verified behaviour-preserving.* A 70-scenario harness (7 seedings × 5 run states × on/off) captured
both builders' rendered text AND both receipts before and after: 42,672 bytes, byte-identical.

*The teeth pass is the part worth recording.* Five deliberate breaks of the new shared module were
run against the existing 64 cross-run tests, and TWO passed silently:

* dropping `research_claims.jsonl` from `CROSS_RUN_SOURCE_NAMES` — the builder still returns text,
  now governed by a ledger that never saw one of the stores it is projecting;
* digesting the raw projection instead of the sanitized one — the module's own docstring calls a raw
  hash "a credential oracle and an identity for bytes the model never received", and nothing checked
  it.

Neither is visible in rendered output, which is exactly why centralizing them needed new guards
rather than inherited ones. `tests/test_cross_run_context.py` — 14 tests; all seven breaks (the five
above plus receipt key-order and a digest leaking into the unavailable receipt) now fail loudly.

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

*Resolution (2026-08-04) — the resume scan is shared by THREE sites; the tick-loop scaffold is declined, and its one load-bearing line is guarded instead.*

**`last_lifecycle_row(rows, event_type, node_id, generation)`** in `engine/train_monitor.py`. The
finding names two copies; there was a third — `asha_monitor.latest_train_verdict`, which does the
identical scan for `EV_TRAIN_MONITOR_ALERT` so the ASHA judge can see the health verdict. All three
now call it and keep only their own interpretation of the row they get back (a status string, an
endpoint/resource flag pair with a legacy fallback, a sanitized verdict dict).

The copied idiom is worth single-sourcing because of what it defends against, not its length: rows
are UNTRUSTED append-only data, and `isinstance(True, int)` is True in Python, so a payload carrying
`node_id: true` matches a plain `== node_id` test against node 1 and hands a watchdog ANOTHER
lifecycle's history as its own. The scan also deliberately returns the newest matching row even when
its contents are unusable — walking further back would answer a resuming watchdog with a verdict it
has already moved past.

**The `watchdog_tick_loop(cadence, cancel, tick_fn)` scaffold is declined**, and the finding's own
"optionally" is why it was worth re-deciding rather than mechanically applying. The shared skeleton
is 7 lines wrapped around 60 and 160 lines of unrelated body, and three things fight the extraction:
the training monitor's cadence MUTATES per tick (the observer self-paces via the LLM's
`recheck_after_s` and a healthy run backs off) while ASHA's is fixed; both bodies `return` from the
enclosing coroutine to stop watching after claiming a kill, which a `tick_fn` would have to signal
back out of band; and both use `continue` throughout, which changes meaning inside a callback. The
result would be two rewritten loop bodies to share seven lines — more risk than the duplication.

What the scaffold WOULD have enforced is guarded directly instead:
`test_every_watchdog_tick_loop_reraises_cancellation_before_swallowing` walks both loops' handlers
and asserts each re-raises `anyio.get_cancelled_exc_class()` BEFORE its blanket
`except Exception: continue`. That blanket clause exists so a transient disk/LLM/tracer hiccup skips
one tick rather than disabling the watcher for the rest of a long eval — but anyio delivers
cancellation as an exception, so without the earlier clause it swallows the cancel and the watchdog
keeps looping against a finished node, holding its task group open. That is the real failure mode
the shared scaffold was protecting against, and a guard catches it without rewriting either body.

Pinned by five new tests in `tests/test_train_monitor.py` (34 → 39): the bool-`node_id` /
bool-`generation` traps, newest-match-even-when-unusable, wrong type/node/generation and empty logs,
a structural guard that all three sites use the helper and none contains `reversed(` any more, and
the cancellation guard above. Teeth-tested against 6 breaks, all biting.

#### EC-05 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**Novelty reject/repropose/audit block duplicated between LLM and semantic gates**

*Locations:* `looplab/engine/novelty.py:440`, `looplab/engine/novelty.py:1224`

*Evidence:* _llm_novelty_gate (novelty.py:440-466) and the semantic branch of _apply_novelty_gate (novelty.py:1224-1252) repeat the same ~25-line sequence: build the duplicate-outcome string (`it FAILED ({dup.error_reason})` vs `it scored {dup.metric}`), capture original + idea_proposal_digest, call _repropose_with_feedback with a NOVELTY GATE hint, on BudgetExceeded append EV_NOVELTY_REJECTED with action='budget_exceeded' and re-raise, then compute action='reproposed'/'kept' from digest comparison and append EV_NOVELTY_REJECTED with kind='llm' vs kind='semantic'. Only the hint wording and one payload key (reason vs similarity) differ. _repropose_with_feedback itself is already shared, so this is the remaining un-extracted half.

*Recommendation:* Extract `_reject_and_repropose(state, idea, dup, kind, hint, extra_payload, repropose, researcher, prospective_node_id)` that owns the digest/action/audit/budget-exceeded protocol; both gates pass their kind-specific hint and payload fields.

*Resolution (2026-08-02):* extracted as recommended — `NoveltyGateMixin._reject_and_repropose`
(`looplab/engine/novelty.py`) now owns the whole protocol, and both gates pass only what legitimately
differs: their hint (a prompt string, so kept VERBATIM at the call sites), `kind=`, and one payload
key (`reason` for llm, `similarity` for semantic). The audit dict is built ONCE, before the
re-propose, so the budget-exceeded exit and the normal exit describe the same rejection — one of the
two ways the copies had already drifted.

The valuable half was never the ~25 duplicated lines; it was `novelty_rejected.action`, the only
place the log records whether a paid re-proposal actually changed the idea (`reproposed`), whether
the Researcher handed the same one back (`kept`), or whether the budget ran out mid-gate
(`budget_exceeded`). A drifted copy keeps gating correctly and quietly stops recording why, and
nothing downstream goes red. `tests/test_novelty_rejection_audit.py` (17 tests) pins that: each
audited outcome, the conservative digest rule (a digest that degrades to `None` reads as `kept`, in
BOTH orderings — a plain `!=` would call it a change), the binding describing the ORIGINAL proposal,
the budget stop auditing exactly once with the same binding, and both gates still routing through the
helper with their own kind/payload/hint. A grep guard pins `except BudgetExceeded:` at exactly two
sites (the helper and `_repropose_with_feedback`) so a re-inlined copy is a red test.

Teeth-tested against six deliberate breaks (drop the budget audit; loosen the digest guard; drop the
payload merge; bind the replacement instead of the original; swap the semantic gate's `kind`; drop
the llm gate's `reason`). The digest-guard break was SILENT on the first draft — the test patched the
digest to `None` on both sides, where a plain `!=` still answers `kept`; it now drives the one-sided
degradations that actually distinguish the two rules.

#### EC-06 · MEDIUM · duplication · effort: medium

**_ablate and _ablate_code share a verbatim ~55-line refine_block child-construction tail**

*Locations:* `looplab/engine/ablation.py:145`, `looplab/engine/ablation.py:290`, `looplab/engine/ablation.py:104`, `looplab/engine/ablation.py:247`

*Evidence:* After their (also structurally parallel) probe loops, both methods repeat the same tail verbatim: _reserve_node_build with kind='refine_block' + parent_generations, None-check -> _discard_node_build_telemetry, idea = reservation.idea.model_copy(deep=True), _reset_developer_footprint, `self._implement(self._directed_idea(idea.model_copy(deep=True), state), parent)`, _finalize_developer_footprint, `_ablation_parent_current` re-check -> _fail_reserved_build('parent lifecycle changed while building'), _emit_node_created(operator='refine_block', ...), fold-membership check -> _fail_reserved_build('ablation node creation was rejected during replay'), then _emit_agent_report/_emit_hypothesis_ranked/_emit_foresight_selected (ablation.py:158-204 vs 306-346). The probe loops also duplicate the workdir naming, wall-clock accumulation, and superseded-flag dance (104-132 vs 254-274).

*Recommendation:* Extract `_build_refine_block_child(parent_id, generation, idea, state)` for the shared tail, and a `_probe(ablated_code_or_idea, workdir_suffix)` helper for the timed lifecycle-checked probe. The two entry points keep only their distinct impact computation and Idea construction.

*Resolution (2026-08-04) — both extractions done; `ablation.py` 346 → 332 lines with the ~55-line tail spelled once.*

* **`_build_refine_block_child(parent, parent_id, generation, idea, state)`** — everything from the
  reservation onward. `_ablate` and `_ablate_code` now end on the same single line, keeping only
  what genuinely differs: how they score (numeric-param delta vs code-block delta with a `None`
  marker for "removing this BROKE the pipeline, so it is maximally essential") and how they build
  the `Idea`. `_ablate` also keeps its own extra currency check, because only it makes an LLM
  `propose` call before building the idea and therefore has a window the other mode does not.
* **`_timed_ablation_probe(source, workdir, parent_id, generation) -> (result, seconds, current)`** —
  the timed, lifecycle-checked probe. The wall-clock is RETURNED rather than accumulated inside the
  helper, deliberately: it is budgeted on the `ablate` event (P1-2), and a caller that forgets to
  sum it is then visibly wrong at the call site instead of silently spending outside
  `max_eval_seconds`.

`_write_assets` stayed at the call sites. `_ablate` stages the workdir BEFORE asking its probe
developer to implement the ablated idea, and folding it into the helper would have moved that
staging to after an LLM call — a real reordering bought for nothing but symmetry.

The tail is where the duplication actually mattered. It carries three abandon paths — reservation
refused, parent superseded mid-build, creation rejected during replay — and each has to do TWO
things: drop the developer telemetry (or it leaks onto whichever node is created next) and, for two
of the three, fail the reservation it already holds (or the card stays `building` forever). Half of
one pair going missing in one copy is precisely the drift a second copy hides.

Pinned by four new tests in `tests/test_ablation.py` (2 → 6): neither mode may contain
`_reserve_node_build`/`_emit_node_created`/`_fail_reserved_build` any more, the tail has exactly
three abandon paths and an AST walk proves each is preceded by the discard (and two by the failure),
the probe helper returns a 3-tuple while neither loop re-grows its own `time.monotonic()`, and
behaviourally the `ablate` event still carries non-negative probe seconds. Teeth-tested against 5
breaks, all biting.

#### EC-07 · MEDIUM · inconsistency · effort: small — **RESOLVED (2026-08-02)**

**Strategist/concept cadence uses modulo gating that its sibling cadence explicitly fixed as a bug**

*Locations:* `looplab/engine/strategy.py:364`, `looplab/engine/strategy.py:380`, `looplab/engine/research_cadence.py:78`, `looplab/engine/orchestrator.py:3725`

*Evidence:* Three cadence idioms coexist: (1) _should_consult / _should_consult_concepts use `n % every == 0` (strategy.py:352-380); (2) deep research and reports use the since-last `_cadence_due(n, last, every)` gate, whose comment explicitly documents WHY modulo is wrong: 'a rung-0/seed batch that jumps the node count by k>1 must not step over the only multiple and skip the whole window' (research_cadence.py:78-83); (3) hypothesis merge uses an in-memory grown-by-2 baseline (research_cadence.py:549, documented KNOWN GAP). Under llm_parallel>1 the node count advances in batch-width strides, so with e.g. width 4 and strategist_every=5 the modulo can miss every multiple — starving the Strategist consult, coverage snapshots, AND the concept re-tag cadence — the exact failure class the research cadence was patched for.

*Recommendation:* Convert _should_consult/_should_consult_concepts to the same since-last _cadence_due pattern (last consult/snapshot at_node is already durable in strategy_history / coverage_snapshots), or document concretely why strategist starvation under batched builds is acceptable. Either way, one cadence idiom should be canonical.

*Resolution (2026-08-02):* `engine/cadence.py` now owns `cadence_due(n, last, every)` and
`cadence_marks(records)`; `_should_consult` and `_should_consult_concepts` use them, and
`Engine._cadence_due` is kept as `staticmethod(cadence_due)` so its existing `self.`-callers are
untouched. The module was extracted rather than the method reused because both gates are unit-tested
against a stand-in `self`, which cannot resolve an Engine method.

Each of the three consumers passes its OWN durable marks — `state.strategy_history`,
`state.coverage_snapshots`, `state.concept_coverage_snapshots` — because they advance independently
and must not be able to satisfy each other's window. That is also what makes the gate resume-safe:
`last` comes from the folded log, not process memory, unlike the hypothesis-merge baseline this
finding lists as its third idiom (still a documented KNOWN GAP).

Three deliberate breaks were caught: reverting to modulo, taking the EARLIEST mark instead of the
latest (which leaves the window permanently open — a paid LLM call per node for the concept
snapshot), and accepting a negative `at_node` (which overshoots the interval and fires early).

Two existing tests encoded the modulo semantics directly — `fire(12) is False`, `fire0(4) is False`.
They were rewritten to assert the since-last property rather than edited until green, and a new
`test_a_batch_stride_cannot_step_over_the_concept_cadence_window` pins the case the finding names:
a width-4 stride over 4, 8, 12, 16, 24 hits no multiple of 10, so a modulo gate fires zero times and
the since-last gate fires at 12 and again at 24.

**Behaviour note:** a consult that fails without recording its event leaves the mark unadvanced, so
the next decision point retries rather than waiting for the next multiple. That is the same
treatment the deep-research cadence gives an unrecorded attempt, and for the concept path the
paid-retry exposure is the KNOWN GAP already documented on `_maybe_snapshot_concept_coverage`.

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

*Resolution (2026-08-04) — one protocol, one flag; the three call sites are 60 lines of ladder shorter.*

`_commit_usage_delta(engine, usage_id, clean, *, trust_success) -> (durable, acknowledged, error)`
in `engine/costs.py`, over a small `_delta_is_durable(engine, usage_id, clean)`. All three sites —
the accountant sink inside `bind_cost_accountants`, `_drain_outbox`, and the per-binding retry loop
in `reconcile_cost_accountants` — now call it, and the per-site bookkeeping that legitimately
differs (`_record` + `pending.pop` for the two binding-aware sites, `persisted[usage_id] = clean`
for the two cache-carrying ones) stays at the call sites where a reader can see it.

Three return values rather than the recommended two. `error` has to come back because the sink
reports it to its caller and is the only site that can tell an append failure apart from the outbox
failure it may have to report instead (`append_error = outbox_error or exc`); raising from the
helper would have forced the other two sites into a try/except they do not otherwise need.

The finding's real point was that "each copy makes slightly different verification choices, so the
protocol's correctness argument must be re-derived per site". That difference is now ONE keyword,
and it is stated at every call site — `trust_success` has no default, so a fourth caller cannot
inherit a verification policy by omission. The argument for each value is local:

* **the sink passes `True`** — it minted this id moments ago (`secrets.token_hex(16)`,
  collision-guarded against `pending`), so no other telemetry can own the identity and a committed
  append IS the durable winner. PERF-1: the rescan it skips was O(K²) across a run and ran for every
  paid call while holding the engine cost lock.
* **`_drain_outbox` passes `False`** — the record it is replaying was written by a PRIOR process, so
  the first-writer argument is not the current process's to make.
* **reconcile's retry passes `False`** — it is retrying an id whose first append already failed, so
  a bare success is not the evidence it is in the sink.

The half both branches share is the one that was easy to get wrong in triplicate: `EventStore.append`
can COMMIT and then surface an error, so an exception is never evidence of absence — only a re-read
is — and an unreadable log answers "not durable", because acknowledgement ERASES the outbox record a
later process would retry from.

Pinned by five tests in `tests/test_llm_cost_ledger.py` (23 → 27, one is a two-way parametrized
case): a structural guard that exactly one call site trusts a bare success AND that it is the sink
(both directions — the other two are asserted not to), commit-then-raise is durable under both flag
values, a commit that never landed returns the append's own exception object, an unreadable log
never acknowledges, and durable-but-unacknowledged stays distinguishable from a clean success.
Teeth-tested against 6 breaks, all biting.

#### EC-11 · LOW · flat-code · effort: small

**Twin ~18-line parallelism validation loops and a 200-line knob if-chain in _apply_strategy**

*Locations:* `looplab/engine/strategy.py:507`, `looplab/engine/strategy.py:526`, `looplab/engine/strategy.py:433`

*Evidence:* _apply_strategy (strategy.py:433-632) is a flat per-knob if-chain; its two concurrency loops (507-521 for max_parallel/eval_parallel, 526-544 for parallel_build/llm_parallel) are byte-near-identical: bool guard, non-integer-float guard, int() with bounds (0..1024 vs 0..64), max(1, value) assignment — differing only in the target attr, the bound, and the broker reconfigure call. The ops sub-dict block (479-489) repeats `if k in ops and may(k): self._attr = cast(ops[k])` five times.

*Recommendation:* Extract a `_settle_width(raw, upper)` -> Optional[int] validator used by both loops, and a small table for the ops knobs ((key, attr, cast)). The governance-sensitive policy/developer sections can stay explicit.

*Resolution (2026-08-03):* `engine/widths.py::settle_width(raw, upper)` is the one rule, with
`EVAL_WIDTH_MAX` / `LLM_WIDTH_MAX` naming the two bounds so a call site reads as the axis it settles
rather than as a magic number. All FOUR loops use it — both in `_apply_control_overrides` (ES-09,
operator `budget_extend` controls) and both in `_apply_strategy` (EC-11, Strategist decisions). The
legacy-first/canonical-last ordering is untouched: it lives in the key tuple, not in the validator.

Each of the four rules inside is load-bearing and fails in a DIFFERENT direction if a copy drifts,
and none fails loudly — a rejected width just leaves the running envelope alone, which looks exactly
like a control that was never sent:

* a bool is not a width (`True` is an `int` subclass, so a JSON `true` would serialize the run);
* a non-integral float is refused rather than truncated (2.5 -> 2 is a guess about intent);
* the bound is a REFUSAL, not a clamp (a clamped 100_000 would look accepted and reshape the run);
* a LIVE zero settles to serial 1 and never means AUTO — AUTO belongs to launch-time `Settings`,
  which can read the hardware and the settled eval width; a mid-run zero has no such context.

One behaviour was tightened rather than moved: `_apply_strategy` reconfigured the LLM broker with the
RAW value while assigning `_llm_parallel` the settled one. Those agree today (the broker applies the
identical rule internally), but only because two spellings happened to match; it now passes the
settled width.

The per-lane allocation map in `_apply_strategy` deliberately keeps its OWN, stricter rule: strict
`int` only, and all-or-nothing across the map (one bad lane rejects the whole allocation rather than
silently allocating the rest). Folding it into `settle_width` would loosen a validator whose job is
to be all-or-nothing; `tests/test_width_settling.py` pins that as a decision, not an oversight.

The ops sub-dict block EC-11 also mentions, and the `_apply_strategy` if-chain's remaining
governance-sensitive sections, are left explicit as the finding itself recommends.

#### EC-12 · LOW · mergeable-entities · effort: small

**Mirrored producer pipelines: SpecBuildResult vs SpecRawStageResult async wrappers duplicate scaffolding**

*Locations:* `looplab/engine/speculation.py:1143`, `looplab/engine/speculation.py:1248`, `looplab/engine/speculation.py:51`, `looplab/engine/speculation.py:73`

*Evidence:* _produce_card_build (1143-1179) and _produce_raw_card_stage (1248-1299) repeat the same wrapper: to_thread.run_sync(functools.partial(worker,...), abandon_on_cancel=False); except Exception -> synthesize a failure result with `f"{type(exc).__name__}: {exc}"[:2_048]`; store the result on self; finally clear the inflight flag and `notify.send_nowait(...)` swallowing (WouldBlock, ClosedResourceError, BrokenResourceError). The failure-result construction (10–11 keyword fields against a 14-field dataclass) is itself duplicated inside _produce_raw_card_stage's except and _prepare_raw_card_stage's except (1231-1244 vs 1281-1292).

*Recommendation:* Extract a generic `_run_isolated_producer(worker, on_result, inflight_clear, notify_key)` coroutine, and one `SpecRawStageResult.failure(...)` classmethod so the 12-field failure payload is built in one place.

*Resolution (2026-08-03, partial by design):* The two genuinely duplicated pieces are single-sourced;
the generic producer coroutine is NOT, and deliberately.

`speculation.py::producer_error_text(exc, prefix="")` and `notify_producer(notify, key)` replace five
and three hand-written copies respectively. Both carry a rule that a copy can drop silently: the error
text is CAPPED because it lands in a durable result an operator reads (an unbounded provider
traceback repr turns one failed proposal into an unreadable log line) and keeps the type name in
front because `str(exc)` is empty for several provider errors; the notification swallow covers all
THREE anyio teardown errors, each of which means the consumer is already gone or saturated while the
main task re-scans the durable slots anyway — letting one escape would tear down the task group, i.e.
cancel live evaluations, over a hint nobody needed.

The `_run_isolated_producer` wrapper is not extracted: the two producers' lifecycles genuinely
differ (one clears a KEY from a set and discards a superseded result, the other clears a bool flag
and additionally discards role telemetry) and their result types are different dataclasses. Forcing
them into one shape would mean threading three callbacks through it — more machinery than the
duplication it removes. The `SpecRawStageResult.failure(...)` classmethod is likewise left open: the
two payload sites differ in which optional fields they carry, and a classmethod defaulting the rest would
hide that. Recorded as a deliberate partial rather than dropped.

#### EC-13 · LOW · duplication · effort: small

**Parser-resolution wrapper-chain walk duplicated outside its canonical helper**

*Locations:* `looplab/engine/strategy.py:1121`, `looplab/engine/novelty.py:780`, `looplab/engine/lessons_distill.py:339`, `looplab/engine/lessons.py:284`

*Evidence:* The idiom `next((p for o in (researcher, getattr(r,'inner',None), getattr(r,'fallback',None), developer) if (p := getattr(o,'parser',None))), 'tool_call')` appears in _verifier_soundness (strategy.py:1121-1123) and _verified_failed_direction_reopen (novelty.py:780-785); the canonical spelling is _merge_prompt_opts itself (lessons_distill.py:331-342), which research_cadence.py:564-566 correctly delegates to — so two hand-rolled copies bypass the intended single lookup path. lessons.py:280-289 (reflect_client) walks the same researcher→inner→fallback→developer chain but resolves the LLM *client* rather than the parser — a third variant of the chain-walk idiom. Given the codebase's own warning about duck-typed wrapper chains (foresight __getattr__ proxy trap), each copy is a chance to miss a wrapper link.

*Recommendation:* Add `resolve_role_parser(*roles, default='tool_call')` next to the existing chain-walk in lessons.py (or agents/roles.py) and use it at all four sites.

*Resolution (2026-08-03):* `agents/roles.py` now owns the walk once —
`role_wrapper_chain(researcher, developer)` returns the four-slot
`researcher → inner → fallback → developer` tuple (`None` holes included, so callers keep using
plain None-tolerant `getattr`), with `resolve_role_parser`, `resolve_role_prompts` and
`resolve_role_client` on top of it. All four sites delegate: `strategy.py::_verifier_soundness`,
`novelty.py::_verified_failed_direction_reopen`, `lessons_distill.py::_merge_prompt_opts` and
`lessons.py::reflect_client`. `research_cadence.py` already delegated through `_merge_prompt_opts`
and is unchanged.

The three resolvers are NOT the same predicate, and the differences are the load-bearing part:

* the parser takes the first TRUTHY `.parser` — an empty parser name is an unset field, not a choice,
  and passing `""` into the structured-output layer is worse than the documented `tool_call` default;
* prompts take the first NON-NONE `.prompts` — an empty PromptStore is a wired store that overrides
  nothing, and skipping past it would walk on into a wrapper that was never configured at all;
* the client additionally requires `hasattr(c, "complete_text")`, because toy backends carry a
  `client` attribute that is not an LLM client; returning one turns "no LLM wired, skip this advisory
  step" into an AttributeError inside run-end distillation.

Covered by `tests/test_role_chain_resolution.py` (23 tests), including that a wrapper carrying no
parser of its own is walked THROUGH — the failure mode the finding names, and one that reports
nothing downstream because the fallback is a valid default rather than an error.

#### EC-14 · LOW · dead-code · effort: small

**_acquire_gpu/_release_gpu are production-dead, kept only for tests**

*Locations:* `looplab/engine/resources.py:440`, `looplab/engine/resources.py:446`

*Evidence:* Repo-wide grep shows the single-GPU primitives are called only from tests/test_strategist.py:1231-1239; the in-tree comment admits 'The dispatcher itself uses the multi-GPU API and never relies on this non-blocking wrapper', and the historical call site the docs reference (evaluate.py::_acquire_gpu per command_eval.py:786 and docs/22) no longer exists. docs/23:1029 even lists replacing them as a planned step.

*Recommendation:* Port the two test call sites to _acquire_gpus/_release_gpus and delete the wrappers (or, if kept deliberately, move the assertions they support into a test helper). Low cost, removes a second API surface for the same pool.

*Resolution (2026-08-03):* `_acquire_gpu`/`_release_gpu` are deleted and
`tests/test_strategist.py::test_gpu_pool_auto_max_parallel_and_distinct_pinning` now drives
`_acquire_gpus`/`_release_gpus` — the API the dispatcher actually uses.

The port is not purely mechanical, and that is the point. `_acquire_gpu` carried its own
`if eval_parallel <= 1: return None` branch, which is a SECOND copy of the "an unspecified footprint
pins a device only in parallel mode" rule that really lives in
`resources.py::_resource_request_for_node`. A second answer to an admission question is worse than
none: the old assertion could keep passing off the dead wrapper while the path the dispatcher takes
disagreed. The serial-no-pin half of the test now asserts against `_resource_request_for_node`
directly.

The stale pointer in `runtime/command_eval.py` (`evaluate.py::_acquire_gpu`, a call site that no
longer exists) now names `engine/resources.py::_acquire_gpus`.

Covered by `tests/test_role_chain_resolution.py`'s last two tests (the wrappers stay gone; the
pinning rule stays in admission) plus the ported strategist test.

#### EC-15 · LOW · excessive-logic · effort: small — **RESOLVED (2026-08-02)**

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

*Verified behaviour-preserving, not merely asserted.* A digest harness over a fixed corpus
(`tests/test_claims.py::test_the_claim_projections_are_unchanged_by_the_split`) exercises all four
flag combinations of `claim_assessments`, five cap values of `build_context_pack`, the render path
and the atlas — and produces the SAME digest when run against the pre-split tree
(`bfd1659b~1`). 181,897 bytes of output, byte-identical. That is what justifies calling a
2,846 -> 843 line redistribution behaviour-preserving; the module's own docstrings claim
"pure/deterministic", which is what makes a digest a legitimate tripwire rather than a flake
generator. Two companion guards keep it honest: one pins determinism within a process, the other
proves the corpus actually reaches `mixed`/`supported`/`refuted`/`inconclusive`, exercises the
governance overlay, and demonstrates that an operator-rejected claim is projected (auditable) but
never reaches a context pack.

*Still open:* the ledger and the D8 store. Both remain in `claims.py`, which at 843 lines is no
longer a god-module; splitting them further is optional rather than the finding.

#### EM-02 · HIGH · duplication · effort: medium — **RESOLVED (2026-08-02)**

**Three near-identical ~90-line steward drivers copy-pasted in lessons.py**

*Locations:* `looplab/engine/lessons.py:939`, `looplab/engine/lessons.py:1031`, `looplab/engine/lessons.py:1114`

*Evidence:* store_concept_curation (939-1029), store_claim_curation (1031-1112), and store_task_facets (1114-1216) share an identical ~90-line skeleton: guard on _cross_run_curation, build diagnostic_key/diagnostic_provenance, take _curation_decision_lock, check _curation_attempt_already_resolved_locked, fast-path 'empty' append, fast-path client-None 'unavailable' append, _paid_curation_attempt_locked with propose→append(require_durable=True)→'error' terminal, and an outer except that writes a diagnostic 'error' row. They differ only in: log name, snapshot/has-input/propose functions, and the empty-proposals shape ({merges,splits,purges} vs {decisions} vs {task_id,facets}); facets adds two extra fast paths (already-governed, empty goal). ~270 lines where ~120 would do, and any protocol fix (e.g. a lock-ordering change) must be applied three times in step.

*Recommendation:* Extract a parameterized driver: _run_finalize_steward(log_name, kind, snapshot_fn, has_input_fn, propose_fn, empty_proposals, extra_fast_paths=()) and reduce the three methods to thin configurations. The identical exception/outcome vocabulary makes this a mechanical extraction.

*Resolution (2026-08-02):* the three drivers now share
`LessonMemory._run_finalize_steward` and differ only in data — log name, snapshot, propose call, and
the empty shape of that steward's proposals. `lessons.py` drops from 1,334 to 1,300 lines; the line
saving is modest because the driver carries the protocol's why-comments, but the point was never the
line count. It was that a lock-ordering change, a new terminal or a receipt field had to be applied
three times IN STEP, or the three ledgers would disagree about what happened during one finalize.

One structural difference survives as a parameter rather than being flattened away: facets are
once-per-TASK, so an already-governed task short-circuits inside the lock before any provider call.
That is `fast_paths`, an ordered tuple evaluated under the decision lock.

*Verified behaviour-preserving.* A harness drives all three stewards through every terminal —
proposed, empty input, empty proposal, unavailable client, provider error, no task id — plus the
replay of each, and compares the LEDGER BYTES they write, not the strings they return. Run against
the pre-extraction tree it produced identical output (22,728 bytes, 24 scenarios). That mattered
concretely: the extraction moved row construction into a shared `row()` helper, which changes JSON
key ORDER, and the byte comparison is what proves nothing digests a row.

Two lessons from building the guard, both worth recording because they are the failure mode this
campaign keeps finding:

* the first harness used plausible-looking proposal payloads with the wrong field names
  (`{"concept": …}` for `{"from_concept": …}`), so every case labelled `proposed` actually settled
  as `empty`. A differential over that would have "proved" byte-identity for a path neither side
  ran. The terminal assertion caught it.
* the first version of the teeth check found that three of five deliberate protocol breaks passed
  silently — including dropping `require_durable=True` from the paid terminal, which is the one
  property whose loss is invisible until a crash replays a paid provider call. The guard now pins
  fast-path ORDER (an already-governed task must win over an empty goal) and durability in BOTH
  directions, so the paid terminal cannot lose its fsync and the no-op finalize path cannot gain one.

`tests/test_finalize_steward_driver.py` — 35 tests; all five deliberate breaks now fail loudly.

#### EM-03 · MEDIUM · mergeable-entities · effort: large

**Two parallel at-most-once paid-curation protocols; the validator must understand four schema generations**

*Locations:* `looplab/engine/lessons.py:520`, `looplab/engine/lessons.py:619`, `looplab/engine/steward_invocation.py:167`, `looplab/engine/governance_health.py:370`, `looplab/engine/governance_health.py:476`

*Evidence:* The finalize path (lessons.py 520-937: _write_curation_claim/_read_curation_claim/_curation_decision_lock/_paid_curation_attempt/_append_curation_once — ~400 lines of claim-file protocol keyed by semantic curation_key with .curation_invocations/ scratch GC) and the on-demand HTTP/CLI path (steward_invocation.py run_steward_invocation — action_id-keyed durable begun/terminal rows) are two independently designed at-most-once protocols writing the SAME three ledgers (concept/claim/facets curation logs). As a result governance_health._validate_curation_row (370-473) is a ~100-line branch cascade over four coexisting row schemas (v2 semantic finalize rows, v1 begun/terminal HTTP rows, legacy run-keyed rows, oldest undiscriminated audit rows), and read_curation_rows (476-554) enforces two different sequencing disciplines in one loop. Also note this entire paid-curation transaction subsystem lives inside lessons.py/LessonMemory although it has nothing to do with lessons — it is governance infrastructure (~700 of lessons.py's 1334 lines).

*Recommendation:* Move the finalize claim/recovery protocol out of lessons.py into a curation_protocol.py module beside steward_invocation.py, and converge new writes on one protocol (the semantic-key v2 shape) so the validator's other branches become legacy-read-only code that can be isolated and eventually retired. The schema plurality is historical, not a requirement of new writes.

#### EM-04 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**Durable identity derivation (_curation_source_key / _facets_curation_key) duplicated between writer and validator**

*Locations:* `looplab/engine/lessons.py:541`, `looplab/engine/lessons.py:561`, `looplab/engine/governance_health.py:256`, `looplab/engine/governance_health.py:262`

*Evidence:* lessons.py LessonMemory._curation_source_key (541-551) computes 'source:v1:' + sha256({v:1, run_id, task_id, finish_seq}) and _facets_curation_key (561-569) computes 'facets:v2:' + sha256({v:2, kind:'facets', task_id}). governance_health.py independently reimplements both (256-265) for _validate_v2_curation_row, which rejects any row whose source_key does not match its recomputation (line 294-297). These are content-addressed durable identities: any drift between the two copies (field order, encoding flag, added field) makes every future curation ledger read raise GovernanceLedgerUnavailable on previously valid rows. Nothing ties the copies together except convention.

*Recommendation:* Move the two key functions to governance_health.py (the module already owning the schema constants) and have lessons.py import them; keep a source-scan test asserting a golden digest so an accidental change is a red test, not a silent ledger poisoning.

*Resolution (2026-08-02):* `governance_health.py` owns `curation_source_key` and
`facets_curation_key` (its private spellings kept as aliases for its own validators);
`LessonMemory._curation_source_key` / `_facets_curation_key` delegate. The module that VALIDATES an
identity now also derives it, which is the only arrangement that cannot drift.

The failure mode is what makes this worth more than its line count. These are durable
content-addressed identities, not cache keys: `_validate_v2_curation_row` recomputes `source_key`
and rejects any row that does not match. A drift between the two copies — a field dropped, a key
order changed, `ensure_ascii` flipped — would not break the write that introduced it. It would
retroactively invalidate every receipt already on disk, surfacing much later as
`GovernanceLedgerUnavailable` on reads that used to work.

`tests/test_curation_identity.py` adds 11 tests. The golden digests are computed from the
PRE-extraction derivation at HEAD, so they prove the move changed nothing rather than merely pinning
whatever the new code happens to produce. Three deliberate breaks were caught: dropping `finish_seq`
from the identity, coercing an absent `finish_seq` to zero (which makes "never finished" and
"finished at seq 0" the same source), and letting an empty task id mint a shared facets key — which
would serve one task's paid overlay to another.

#### EM-05 · MEDIUM · inconsistency · effort: medium

**Two parallel governance-append implementations; the shared one is homed in the wrong module**

*Locations:* `looplab/engine/claims.py:1168`, `looplab/engine/concept_registry.py:883`, `looplab/engine/concept_registry.py:918`, `looplab/engine/task_facets.py:166`, `looplab/engine/steward_invocation.py:106`

*Evidence:* record_claim_decision (claims.py 1168-1234) hand-rolls its own locked append: _interprocess_lock, action_id idempotency scan via _decision_payload, expected_revision CAS, strict_fsync append, confirm_governance_durable on replay — the exact protocol _append_governance (concept_registry.py 883-990) already implements generically (same lock, same idempotency-before-CAS ordering, same durable-fsync/created-dir handling). Two implementations of one critical protocol can drift (e.g. record_claim_decision returns a sanitized projection on replay while _append_governance returns dict(existing) raw). Separately, _append_governance — the generic append primitive used by task_facets.py, lessons.py curation logs, and steward_invocation.py — lives in the concept-specific concept_registry.py and even special-cases ledger filenames internally (lines 918-921 and 969-977 branch on path.name in {'concept_aliases.jsonl','concept_splits.jsonl'}), so a generic-looking primitive secretly knows the concept ledgers by name.

*Recommendation:* Relocate _append_governance to governance_health.py, parameterize the ledger-specific readers instead of branching on filenames (read_rows is already the right hook — make it mandatory for policy ledgers), and port record_claim_decision onto it, keeping its sanitize-on-replay as a wrapper.

*Resolution (2026-08-05) — PARTIAL, the middle clause only:* `read_rows` is now the sole way a caller
selects a strict reader. Both filename branches inside `_append_governance` are gone (the reader
selection and the torn-tail separator), and the four concept call sites pass `_read_alias_rows` /
`_read_split_rows` explicitly. The primitive four subsystems import as generic no longer names a
concept ledger.

Safe because the two revision derivations are the SAME computation, not merely similar:
`_ledger_revision` returns `max([len(rows), *explicit], default=0)` over `_read_alias_rows(path)`, and
the `strict_rows` branch computes that expression over the rows `read_rows` just returned. Passing
the reader therefore changes which line computes the CAS revision, not its value — and collapses two
reads of the ledger into one inside the same lock.

The separator clause needed no replacement either: these two ledgers now reach it with a reader, so
`read_rows is None` already excludes them and the filename clause could only ever have agreed with it.

**What teeth-testing changed here, and it is the point of the entry.** Deleting the branches moved a
STRUCTURAL guarantee ("this path implies a strict reader") into a call-site CONVENTION ("this caller
passes one") — and breaking the convention failed no test at all. The guarantee only survived because
`_ledger_revision` keeps its own filename dispatch, a third copy the finding does not list. Two guards
now pin what the branch used to: the primitive contains no ledger filename, and every
`_append_governance` call in `concept_registry` passes `read_rows`. Both were verified to fail when
broken.

NOT done, and deliberately: the relocation and the `record_claim_decision` port. `_append_governance`
still depends on concept-specific machinery — `concept_governance_global_revision`,
`ConceptGovernanceConflict`, `_idempotency_payload`, `_validate_expected_revision` — so moving it to
`governance_health.py` means injecting or relocating those too, and `record_claim_decision` is a
durable CAS protocol on operator policy where a behaviour-preserving port needs its own evidence
rather than a shared one. `_ledger_revision`'s dispatch stays for the same reason: it is reached from
`concept_governance_revision(memory_dir, kind)`, which legitimately knows the two ledgers, and it now
carries the fail-closed guarantee that the deleted branches used to duplicate.

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

*Resolution (2026-08-02):* extracted as recommended —
`claims_assessments._ingest_evidence(lessons, research_claims, resolve, *, weigh=None)`. `resolve(row)`
is the group lookup (scope+polarity signature vs normalized statement) and `weigh` is the structured
path's `_ev` bookkeeping, taken as the callback the recommendation offered. Both projections are now
one call each.

Three rules lived inside the duplicated walk, and all three fail SILENTLY:

* **A neutral lesson still registers its run and scope.** "Noted" takes no stance but does prove the
  claim was seen there; skipping the registration shrinks the breadth a reader judges by, and nothing
  reports a smaller number as wrong.
* **Unsupported research is `unverified`, never `oppose`.** "Not established" is not counter-evidence
  — merging the two lets an uncited claim read as actively refuted.
* **The verification receipt carries method AND verdict.** Two claims "supported" by replication and
  by a web search are not the same evidence.

`tests/test_evidence_ingestion.py` (19) drives the fold directly for each of those, plus junk/
unresolvable rows, the non-indexable producer-receipt guard, the `weigh` hook's optionality, and an
end-to-end cross-check that the structured and lean projections put the same rows in the same
buckets. Teeth-tested against five breaks.

The peer split of `claims.py` also means `claims.py` must RE-EXPORT every name the assessments module
owns (`test_claims.py::test_the_assessments_barrel_re_exports_the_same_objects`) — the new helper
needed adding there, which that guard caught immediately. Second time this session a guarded
post-split barrel contract has caught an omission; it is doing its job.

#### EM-08 · MEDIUM · duplication · effort: small — **PARTIALLY RESOLVED (2026-08-02)**

**The '_governance is None → recurse via project_governed_sources' pattern and the scope-filter block are copy-pasted across four/three call sites**

*Locations:* `looplab/engine/claims.py:1666`, `looplab/engine/claims.py:2462`, `looplab/engine/claim_steward.py:146`, `looplab/engine/concept_steward.py:181`, `looplab/engine/claims.py:1641`, `looplab/engine/claims.py:1697`, `looplab/engine/claims.py:2491`

*Evidence:* Four functions (atlas_for_memory, cross_run_retrieve, claim_curation_snapshot, concept_curation_snapshot) share the same '_governance is None → recurse via project_governed_sources with a self-invoking lambda' skeleton; atlas_for_memory and cross_run_retrieve additionally build source_names by None-checking lessons/research_claims/capsules (claim_curation_snapshot None-checks only lessons; concept_curation_snapshot passes a constant source_names). Separately, the task-scope filter (three _filter_claim_source_rows/_filter_capsule_rows calls comparing str(r.get('task_id')) == wanted) is repeated verbatim in claims_for_memory (1641-1646), atlas_for_memory (1697-1706), and cross_run_retrieve (2491-2498). The scope filter is an access boundary (the comment at 1698 notes a past leak when only one store was filtered) — exactly the kind of code that should have one spelling.

*Recommendation:* Add a governed_projection decorator/helper that handles the source_names derivation + recursion, and a _scope_all_sources(lessons, research, capsules, task_id) helper so the access-boundary filter has a single implementation.

*Resolution (2026-08-02, scope half):* the ACCESS BOUNDARY half is done —
`claims_health.py::scope_cross_run_sources(*, task_id, lessons, capsules, research)` returns the
scoped triple, and all three joining readers (`claims_for_memory`, `atlas_for_memory`,
`cross_run_retrieve`) call it. This was the half worth doing first: the comment that used to sit at
the atlas site recorded the leak literally — filtering research rows and forgetting the others
returned another task's lessons and capsules in the same payload, and the response looked complete,
just wider than it should be.

Two contract decisions are pinned rather than left implicit. A store passed as `None` comes back as
`None`, because "capsules were not read" and "this task has no capsules" are different claims and the
caller renders them differently. And a BLANK `task_id` filters NOTHING: the callers guard on
`if scope_task` before calling, so a helper that silently emptied everything on a blank scope would
turn a missing argument into a plausible "this task has no cross-run history".

`tests/test_cross_run_scope_boundary.py` (21) covers the three-store leak, exact-match only (case,
whitespace, prefix and numeric near-misses all out), unattributed rows, read-health survival through
the filter, the None/blank contract, a narrow grep guard over the two joining modules, and an
end-to-end atlas read proving a foreign task's lesson text and concept id are absent from the
rendered payload. Teeth-tested against five breaks.

The `_governance is None → recurse via project_governed_sources` half of this finding is NOT done and
stays open: it is a different shape (a recursion/decorator over four functions with differing
`source_names` derivations) and does not gate an access boundary.

Worth noting for the next collapse in this area: dropping `_filter_claim_source_rows` from
`claims.py`'s import list broke 37 tests, because `claims.py` RE-EXPORTS it as a guarded post-split
import contract (`tests/test_claims.py` asserts the re-export set). The import is back, now labelled.

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

#### EM-11 · MEDIUM · dead-code · effort: small — **RESOLVED (2026-08-02)**

**Vector-backed CaseLibrary is dead in production — only tests use it**

*Locations:* `looplab/engine/memory.py:1496`, `looplab/engine/lessons.py:385`

*Evidence:* CaseLibrary (memory.py 1496-1600, ~105 lines: VectorStore-backed episodic store with Memora consolidation/expansion, _consolidate direction-aware metric merging, retain_if_improved) has zero callers under looplab/ — a repo-wide grep finds only tests (test_trust_knowledge.py, test_phase3_memory.py, test_memora.py) and a docstring mention in tools/memora.py. The engine's actual case store is JsonlCaseLibrary (lessons.py:385 store_case). Both classes claim the same I19/ADR-10 role in their docstrings, so a reader must discover by grep which one is real. The dead class still accretes maintenance (its _consolidate got a direction-comparability fix at some point).

*Recommendation:* Either wire CaseLibrary into a real consumer (if the Memora harmonic case path is still planned) or delete it and port its direction-comparability lesson into JsonlCaseLibrary docs; at minimum mark it clearly as test-fixture/unwired so its docstring stops claiming to be 'the top-system differentiator'.

*Resolution (2026-08-02):* marked, not deleted — and the marking is now enforced. `CaseLibrary`'s
docstring opens with UNWIRED, names `JsonlCaseLibrary` as the store a run actually reaches, and says
what wiring it in would require (the durability contract it does not have: whole-file reload,
quarantine-preserving rewrite, retain-on-improvement across runs). `JsonlCaseLibrary`'s docstring
points back, and carries the direction-comparability lesson from `CaseLibrary._consolidate` — two
cases are only comparable when their objective direction matches, which the JSONL store sidesteps by
keying on `task_id`.

Deleting it would have deleted the only coverage of Memora consolidation/expansion, which is still
the intended direction for this path; the finding's own minimum ask was the marking.

`tests/test_case_store_wiring.py` keeps both claims true in BOTH directions: it fails if anything
under `looplab/` constructs `CaseLibrary` (with the message saying what to do about it), and it
fails if nothing constructs `JsonlCaseLibrary` — because that would mean the engine has no case store
and cross-run recall is silently off. A first attempt at the "someone wired it in" break was silent:
assigning the class is not constructing it, and the scan looks for calls. The break was sharpened to
a real construction.

#### EM-12 · MEDIUM · excessive-logic · effort: medium

**Ad-hoc hand-written receipt validators repeated ~8 times with no shared schema helper**

*Locations:* `looplab/engine/claims.py:102`, `looplab/engine/claims.py:652`, `looplab/engine/claims.py:767`, `looplab/engine/claims.py:542`, `looplab/engine/memory.py:684`, `looplab/engine/memory.py:726`, `looplab/engine/concept_steward.py:75`, `looplab/engine/claims.py:2537`

*Evidence:* The fail-closed receipt discipline (deliberate and documented) is implemented as ~8 independent hand-rolled validators, each repeating the same idioms — type(x) is not int / not isinstance(x, bool) guards, 0 <= v <= MAX bounds, arithmetic-consistency conjunctions (total == retained + omitted, complete == (omitted == 0)), and legacy-absent default-fill: _safe_claim_read_segment/_safe_claim_read_health (claims.py 102-131), _safe_research_source_summary (~75 lines, 652-726), _safe_claim_source_summary (767-804), _research_source_receipt (542-583), _valid_research_evidence_receipt (335-355), _capsule_concept_evidence_completeness/_capsule_completeness (memory.py 684-765), _concept_source_receipt (concept_steward.py 75-146), and cross_run_retrieve's inline scope_receipt validation (claims.py 2537-2559). Adding one field to any receipt touches its builder, its validator, its digest field-list, and every consumer's projection — with nothing enforcing they stay in sync except tests.

*Recommendation:* Keep the fail-closed semantics but extract a tiny declarative helper (field specs: bounded-int/bool + a consistency predicate list) that each receipt defines once and both builder and validator consume; the invariants are all expressible as (field types, bounds, equalities). This shrinks each validator to a spec table and makes builder/validator drift structurally impossible.

*Resolution (2026-08-04):* The LEAF is shared; the spec table is deliberately NOT built, and the
reason is worth recording because the finding's premise is half right.

What reading the eight validators shows is that the repeated part is one line, not the validator. The
consistency predicates the recommendation proposes to tabulate are the actual content: they are
domain logic with load-bearing comments, and `_concept_source_receipt`'s carries ten lines explaining
why it must read BOTH source axes — a bug someone already fixed once, where comparing against
`partial_capsules == 0` alone reported a readable-but-incomplete receipt as unreadable. Expressing
that as a row in a spec table would hide precisely the part a reader needs. "Field types, bounds,
equalities" describes the shape of these predicates but not their meaning.

What IS shared is the guard on a single count field, and it had DIVERGED — which is a real defect the
finding did not name:

* `claims_health` and `memory` spelled it `type(value) is int` — rejecting every `int` subclass.
* `concept_steward` spelled it `isinstance(value, int) and not isinstance(value, bool)` — rejecting
  `bool` specifically and ACCEPTING any other `int` subclass.

One concept, two rules. They agree on everything JSON can produce and disagree only on an in-process
`int` subclass, so no shipped receipt ever distinguished them — which is exactly why it survived.
`core/receipts.bounded_receipt_count(value, maximum)` is now the single answer, and the STRICT
spelling wins for the reason the fold uses it on untrusted data: a receipt is durable evidence, an
`int` subclass can override the comparisons the bound is expressed in, and a bound a value can talk
its way past is not a bound. Nothing constructs receipt counts as a subclass, so `concept_steward`
tightens without any reachable behaviour change.

The guard test is scoped to the named validator FUNCTIONS via AST, not to their files. A file-wide
scan was the first attempt and was wrong three ways: it flagged unbounded coercions that are not
receipt counts, matched `int)` inside `fingerprint)`, and reported its own explanatory comment. A
guard that cries wolf collects exemptions until it guards nothing — the same trap EV-04's first draft
fell into one finding earlier.

Still open under this finding: the builder/validator drift the recommendation's last sentence is
really about. Nothing yet forces a receipt's WRITER and its READER to agree on the field set; that is
a registry problem (the shape CLAUDE.md's other duck-typed seams solve) rather than a helper problem,
and it is not addressed here.

#### EM-13 · LOW · duplication · effort: small

**_valid_node_source and _node_ids duplicate the same numeric-string node-id parsing rules**

*Locations:* `looplab/engine/claims.py:266`, `looplab/engine/claims.py:472`

*Evidence:* _valid_node_source (266-295, the validation fence) and _node_ids (472-496, the reader) independently implement identical parsing rules: int-but-not-bool acceptance, negative rejection, string acceptance only when stripped length <= 24 and lstrip('-').isdigit(), int() with (ValueError, OverflowError) guard, negative-parsed rejection. The comments in each explain the same phantom-ref rationale. A future rule change (e.g. widening the 24-char bound) must be made twice or the fence and reader disagree — precisely the validator/reader drift the module elsewhere works hard to prevent.

*Recommendation:* Implement one _parse_node_id(value) -> Optional[int] used by both: the validator rejects the row when any element parses to None, the reader drops Nones. Behavior identical, single spelling.

*Resolution (2026-08-03):* `claims_health.py::_parse_node_id(value) -> Optional[int]` is the one rule;
`_valid_node_source` rejects the row when any element parses to None, `_node_ids` drops them. The
24-character bound moved to `_MAX_NODE_ID_TEXT` alongside it. Both names are re-exported through the
`claims.py` barrel, as the barrel contract requires.

One deliberate tightening rather than a pure move: the reader used `isinstance(x, int)` (after a bool
guard) where the fence used `type(value) is int`, so an int SUBCLASS other than bool would have been
kept by the reader and quarantined by the fence. The shared rule takes the fence's stricter spelling —
the fail-closed direction, and unreachable in practice since these rows come from JSONL.

Covered by `tests/test_shared_identity_rules.py`, whose central test asserts the invariant directly:
over a mixed corpus, `_valid_node_source([v])` is true exactly when `_node_ids([v])` keeps `v`.

#### EM-14 · LOW · dead-code · effort: small

**apply_concept_curation retained with zero production callers**

*Locations:* `looplab/engine/concept_steward.py:355`

*Evidence:* apply_concept_curation (355-414, ~60 lines: batch-apply merges/splits/purges through record_* writers with a partial-apply receipt) is documented as 'low-level compatibility helper for an already-reviewed batch; the steward never invokes it'. Repo-wide grep confirms the only callers are tests (tests/test_concept_steward.py). Since the module's core invariant is 'the steward only PROPOSES; the operator applies via typed single actions or HTTP CAS governance', a live batch-apply function that bypasses CAS/action_id (it passes neither expected_revision nor action_id to record_concept_alias) is not just dead weight — it is a footgun contradicting the invariant one import away.

*Recommendation:* Delete it (its tests exercise record_* behavior reachable directly), or if a batch path must survive, require expected_governance_revision/action_id parameters so it cannot bypass the CAS discipline the rest of the module enforces.

*Resolution (2026-08-03):* Deleted. It bypassed the CAS discipline entirely — neither
`expected_revision` nor `action_id` reached the `record_*` writers — while the module's stated
invariant is that the steward only PROPOSES and the operator applies through a typed single action or
owner HTTP governance. Dead code that contradicts the invariant one import away is worse than dead
code.

Two of its three tests pinned WRITER properties and are re-pointed at the writers themselves: that
merges/splits/purges land (with an empty target meaning PURGE, not a merge into nothing), and that a
cycle-closing merge is rejected at record time while the store stays usable. The third pinned the
batch applier's own conflicting-source rule and went with it — batch semantics no caller had.

#### EM-15 · LOW · inconsistency · effort: small

**The unicode word-token regex and NFKC+casefold normalization re-declared in five places**

*Locations:* `looplab/engine/memory.py:35`, `looplab/engine/concept_registry.py:56`, `looplab/engine/claim_key.py:31`, `looplab/engine/claims.py:1726`, `looplab/engine/claims.py:2380`

*Evidence:* The identical pattern re.compile(r"[^\W_]+", re.UNICODE) is defined as _WORD_UNICODE (memory.py:35), _WORD (concept_registry.py:56), _WORD (claim_key.py:31), and _CLAIM_WORD (claims.py:1726); the NFKC-normalize-then-casefold-then-tokenize pipeline is likewise repeated in claims.py _retrieval_tokens (2380-2382), claim_key._analyze (117-118), and concept_registry.normalize_key (105). memory.py's legacy/universal duality is deliberate and documented, but the other four copies are simply the same tokenizer independently declared; a Unicode-handling fix would need five edits.

*Recommendation:* Export one WORD_RE (and a tokenize(text) helper doing NFKC+casefold+findall) from a low-level shared module (core or a small engine/_text.py) and import it; keep memory.py's legacy ASCII variant where it is since it is a versioned compatibility contract.

*Resolution (2026-08-03):* New `looplab/core/text.py` exports `WORD_RE`, `normalize_text(text)`
(NFKC → casefold) and `tokenize(text)` (that, then `findall`). Five modules now share the compiled
object — `engine/memory.py`, `engine/concept_registry.py`, `engine/claim_key.py`,
`engine/claims_health.py` and `tools/cross_run_tools.py`, the last of which the finding had not
listed. Each imports it under its own existing private alias
(`from looplab.core.text import WORD_RE as _CLAIM_WORD`), so no new public name appears in a
barrel-guarded namespace.

`normalize_text` is exposed separately because two callers need BOTH the tokens and the normalized
string — `claim_key` matches the "n't" contraction against the string after tokenizing it, and
`cross_run_tools._slug_norm` filters it to alphanumerics — and re-deriving the pipeline there would
have been another copy. Two sites keep a bare `.casefold()` deliberately (an ASCII cue match), and
`memory.py::_WORD_ASCII` stays exactly where it is: it is the VERSIONED pre-unicode fingerprint mode
a running portfolio must still match, not a copy of this rule.

Covered by `tests/test_shared_identity_rules.py`, including a repo-wide source scan that fails on any
re-declaration of the pattern, and identity (not equality) assertions per module — a separately
compiled twin would pass an equality check today and drift on the next unicode fix.


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

*Resolution (2026-08-04) — the "at minimum" path, deliberately; and the drift the finding suspected is REAL and measured.*

Three coercions are now shared: `_bounded_card_action_space` (already existed — the snapshot simply
was not calling it), plus new `_bounded_card_eval_timeout(value) -> (timeout, valid)` and
`_bounded_card_parent_ids(value) -> list[int]`. Admission and the derive-time snapshot both go
through all three.

**The measured drift.** The snapshot sliced its top-64 window BEFORE filtering keys
(`sorted(space.items(), key=str)[:64]`, then discard unusable ones), while admission filters first
and then takes the 64 lexically smallest. On a space whose earliest-sorting keys are unusable, the
snapshot's window is eaten by keys it then throws away: admission kept **64** usable keys, the
snapshot decoded **14** of the same input. Not reachable today — only `st.cards_added` rows reach
`_card_added_snapshot`, and those were bounded on the way in — but nothing enforced that, and a
divergence inside the fold is silent by construction.

**Why not the fuller refactor.** Making the snapshot consume the receipt shape wholesale would fold
the `owns_action` derivation into the bounding pass, and `owns_action` is not a bounding fact: it
decides whether a row CLAIMS an action, which drives card ownership and selection. It also has a
deliberate asymmetry the bound does not share — an explicit `eval_timeout: null` CLEARS a timeout and
so does not by itself make the row an owner, while admission records the same null as a legitimate
value. Merging the two would have to reproduce that distinction inside a function whose job is
"shrink untrusted data", which is how the two stages would end up coupled for a second time.

`_card_replay_node_id` replaced the snapshot's inline `_coerce_node_id(...)` + range check; verified
equivalent across bools, floats, strings, negatives and both `2**31` boundaries before substituting.

`tests/test_events_replay.py` 170 → 175: both stages bound a space identically (the drift case),
both reject an unusable timeout including the `isinstance(True, int)` trap, an explicit null survives
both stages while staying distinguishable from absent and from invalid, parent ids bound/dedupe
identically, and a structural guard that neither stage carries an inline `math.isfinite` ladder any
more. Teeth-tested against 6 breaks, all biting.

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

*Resolution (2026-08-04):* The hex-digest half is done. `core/jsonutil.valid_digest_ref(value, *,
prefix="")` now answers it once, at 16 call sites across `events`, `core`, `engine`, `search`, `tools`
and `serve`.

It lives in `jsonutil.py` rather than a new validators module because it is the READER of the format
`canonical_json_digest` in that same file WRITES. The two are one contract: a verifier that drifts
from its minter accepts refs nothing issued, or rejects refs that were. Splitting them across modules
is what let ~20 copies exist in the first place.

Two of the copies were not merely duplicated, they were WEAKER, and the shared predicate fixes both:

* `replay._digest_ref` had no `isinstance` guard, so a non-string reached `.startswith` and raised
  `AttributeError` inside the fold — which takes down every replay of the run, not one field. Both
  call sites pass an already-bounded `str` today, so it was latent rather than live.
* `lessons.py`'s curation-claim check tested `len` and character membership with no type guard at
  all, so a 64-element LIST of hex characters satisfied both and was accepted as a digest.

Three deliberate NON-conversions, which is the part a blind sweep would have got wrong:

* `serve/routers/boss._normalize_report_generation` accepts `A-F` as well as `a-f`. It normalizes a
  generation a client typed into an HTTP request and lowercases it afterwards — input normalization,
  not identity checking. Its sibling twelve lines up is fold-side and lowercase-only. Merging the two
  would have silently made the fold accept two spellings of one digest.
* `serve/reviews.py` and `serve/assistant.py` validate 12- and 32-hex REVIEW LINK IDS, and
  `engine/costs.py` a 32-hex `usage_id` (`uuid4().hex`). Random identifiers, not digests.

The guard is a source scan, and its first draft had the same bug the finding describes: it exempted
those files WHOLESALE, so a genuinely re-derived 64-hex predicate added beside an unrelated random-id
check would have passed. Exemptions now match on the reason (an uppercase alphabet, or a non-64
length in the two-line window) rather than the filename; a teeth-test that injects a re-derived copy
into `costs.py` confirms it is caught.

The finding's OTHER two halves — the scalar guards (`type(x) is int and 0 <= x <= (1 << 31) - 1`,
`isinstance(v, bool) or not isinstance(v, int)`) and adopting the `_coverage_snapshot_row` table style
for new handlers — are NOT done here and stay open. They are a larger change with real semantic risk
per site, unlike the digest predicate, which is one exact shape with a differential check available.

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

#### EV-07 · MEDIUM · excessive-logic · effort: medium — **RESOLVED (2026-08-02)**

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

*Resolution (2026-08-03):* Both extracted exactly as recommended.
`replay.py::_queue_forced_request(st, d, requests, generations)` backs `_on_force_confirm` and
`_on_force_ablate`; `_purge_node_requests(st, drop)` backs all four purge sites
(requeue-on-partition, tombstone, reset, abort).

The purge is the one that mattered. Each queue is TWO lists — a legacy bare-id list and a
generation-stamped record list — and they must move together, ASYMMETRICALLY so: the engine's
`_pending_forced_*` readers consult the stamped list FIRST, so a record left behind wins over an id
the caller believed it had removed, and the request fires against a lifecycle that no longer exists.
`_on_node_reset` in particular spelled its two queues twelve lines apart, which is precisely how a
partial purge hides in review.

`_on_fork` is left as its own handler: it queues a whole RECORD (with `from_node_id` and a defaulted
generation) rather than an id/generation pair, so it shares the shape but not the data.

Covered by `tests/test_replay_queue_and_producer_seams.py` (20), including the generation CAS, the
legacy queued-before-create arm, and a source guard that the purge exists in exactly one place.

#### EV-10 · LOW · duplication · effort: medium

**Span-to-node attribution rule implemented three times across traceview and span_index, kept equivalent only by comments**

*Locations:* `looplab/events/traceview.py:1048-1065`, `looplab/events/traceview.py:914-931`, `looplab/events/traceview.py:489-517`, `looplab/events/span_index.py:388-421`

*Evidence:* The rule "a span's effective node is its own stamped node_id, else its trace ROOT's node_id (never a full ancestor walk)" is implemented in build_trace_view (root_nid map, 1048-1065), again in build_conversation (per-span `mine` comprehension with trace_nid fallback, 914-931, whose comment says "Attribute PER SPAN, exactly as build_trace_view does"), and a third time as the selection predicate in _bounded_node_trace_tail (489-517) whose comment warns "This must stay equivalent to the index's node_tids -> rows -> tail path"; span_index._rows_for_node (388-421) adds a fourth root-resolution for generation fencing. A past divergence is documented in the 914-931 comment (whole-trace keying dropped a node's turns from its own conversation).

*Recommendation:* Extract `effective_node_id(span, root_node_by_trace)` plus a shared `trace_root(spans)` helper into traceview and use them from build_trace_view, build_conversation, _bounded_node_trace_tail and span_index; add one equivalence test between the indexed and no-index selection paths.

*Resolution (2026-08-04) — and the copies had ALREADY diverged. This was a live bug, not just duplication.*

`trace_root_node_id(spans)` and `effective_node_id(span, trace_root_nid)` in `events/traceview.py`;
`build_trace_view` and `build_conversation` both go through them.

**The divergence, measured.** ROOT means "parent not present in this trace" — a true `parent_id is
None` span OR an ORPHAN whose parent is missing. The orphan case is the normal LIVE shape, and
`build_conversation`'s own comment says so: an operation span is written only on CLOSE and
`create_node` closes at node END, so for the whole life of a node its trace has no root on disk and
every span in it is an orphan. `build_trace_view` used `_tree(...)[0]`, whose root set INCLUDES
orphans. `build_conversation` derived attribution from its structural `root`, which requires
`parent_id is None` strictly. On a trace holding an orphan (node 7, earlier) and a later true root
(node 9), those pick different spans — so a span carrying no `node_id` of its own was attributed to
node 7 in the trace view and node 9 in the conversation. Same span, two nodes, depending on which
view the operator opened. The comment above that line asserted the two behaved "exactly" the same.

**What did NOT get merged, and why.** `build_conversation` keeps its structural `root`/`first`: they
name the stage and stand in as a band container, and that role genuinely wants the strict
`parent_id is None` span. Collapsing the two reintroduces the bug in one direction or breaks stage
naming in the other — so the split is now explicit, with a comment at each, instead of one variable
serving both meanings. `_bounded_node_trace_tail` and `SpanIndex.node_tids` are a different rule
again (ANY span in the trace carrying the id — a deliberate selection SUPERSET, re-filtered per span
afterwards) and are left alone; the finding's requested equivalence test covers them instead.

Pinned by five tests in `tests/test_span_index.py` (35 → 40): the two views agree on an orphan-headed
trace, per-span stamping still beats the trace root (asserted on the trace VIEW, because a lone tool
span with no generation parent produces no conversation TURN and so cannot witness it there), the
attribution root accepts an orphan while the structural root does not, an unstamped root leaves its
node-idless spans `unscoped` rather than bleeding a later span's id onto them, a structural guard
that neither view re-derives `_tree(...)[0]`, and the indexed-vs-unindexed equivalence the finding
asked for — run on the orphan-headed shape that broke. Teeth-tested against 5 breaks, all biting.

#### EV-11 · LOW · duplication · effort: small

**Authoritative-provenance set spelled inline twice in _materialize_concept_deltas and again as a module constant**

*Locations:* `looplab/events/replay.py:1874-1879`, `looplab/events/replay.py:1947-1952`, `looplab/events/replay.py:4689-4695`

*Evidence:* The 4-member frozen set {AUTHORED, CLASSIFIER, OPERATOR, OFFLINE_HEURISTIC} appears as two inline set literals inside _materialize_concept_deltas (the Kahn pass at 1874-1879 and the cycle-fallback pass at 1947-1952), while _CARD_NODE_CONCEPT_PROVENANCE (4689) is the same set plus UNTRUSTED. Adding a new provenance tier requires editing three spellings; missing the second inline copy would make the cycle path disagree with the topo path on the same log.

*Recommendation:* Define one module-level _INHERITABLE_CONCEPT_PROVENANCE frozenset and reference it from both loops (and derive _CARD_NODE_CONCEPT_PROVENANCE from it | {UNTRUSTED}). Consider also collapsing the two parallel parent-reason loops (1843-1885 vs 1930-1957) into a shared per-parent classifier.

*Resolution (2026-08-03):* `replay.py::_INHERITABLE_CONCEPT_PROVENANCE` is the single spelling, read
by BOTH passes of `_materialize_concept_deltas`, and `_CARD_NODE_CONCEPT_PROVENANCE` is now DERIVED
(`_INHERITABLE_CONCEPT_PROVENANCE | {UNTRUSTED}`) rather than re-listed — so a new tier added to the
inheritable set cannot be forgotten on the display side.

The risk this closes is a replay-determinism break, not merely an edit-in-three-places chore: the two
passes are the Kahn topological walk and the CYCLE FALLBACK over the same log. If they disagree about
which tiers carry an exact membership statement, one event log folds to different concept memberships
depending only on whether the node graph happened to contain a cycle, with no error raised anywhere.

The two parallel parent-reason loops are left as they are: they compute different things (one
materializes memberships, the other only accumulates reasons for nodes it cannot resolve), and
merging them would mean threading a mode flag through both — recorded here rather than silently
dropped.

Covered by `tests/test_shared_identity_rules.py` (first four tests), including a source guard that
BOTH passes still read the constant and that no inline set literal came back.

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

*Resolution (2026-08-03):* `looplab/core/fence.py` owns the PROTOCOL;
`run_reset.py` and `run_deletion.py` stay the schema owners. `load_bounded_json_marker` performs the
lstat / reject-non-regular / reject-oversized / bounded-read / re-lstat / identity-compare / decode
sequence and hands the decoded object to the owner's validator; `publish_bounded_json_marker` does
encode + size check + strict atomic write + read-back confirm through the owner's own loader. The
UUID and 64-hex shapes and the 8 KiB cap are declared once; the two modules alias them under their
existing public names so importers are unaffected.

Each owner keeps what is genuinely its own: its error classes, its key-set, and the decisions that
differ. The deletion fence also binds `run_key` to the exact directory being asked about (it lives
BESIDE the run, so a fence copied next to another run must be malformed rather than authoritative),
and it REFUSES to overwrite a live fence where the reset marker republishes — replacing one would
hand ownership to a second deleter mid-operation.

**The drift the finding predicted had already happened.** `load_run_deletion_fence` re-derived
`atomicio.file_identity` as a local six-field lambda while importing the canonical helper two lines
above, so a change to the canonical tuple would have left the deletion fence comparing a weaker
identity with nothing to notice. That copy is gone.

`tests/test_fence_protocol.py` proves the protocol as BEHAVIOUR on BOTH fences (absent is `None`;
undecodable, malformed, extra-key, oversized and non-regular all fail CLOSED into the owner's
storage error), and pins the parts a shape-only test would miss: the read-back lstat actually
refuses a marker replaced under it, the oversized marker is refused WITHOUT being opened (asserting
only "it raised" would stay green with the pre-read guard deleted, because the post-read guard still
catches it — after doing the very read the first guard exists to prevent), and the two fences keep
distinct error types. Teeth-verified against 13 separate breakages.

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

*Resolution (2026-08-03):* Deleted, along with one more the finding did not list: `_parse_chat_body`,
whose only caller was `_read_stream`. Also gone are the `urllib.request` import that existed solely
so those tests could monkeypatch `llm.urllib.request.urlopen`, the four dead re-exports through
`llm.py`, and the two module docstrings' explanations of which of two SSE paths was real.

Neither contract needed porting, which was checked rather than assumed:

* **stall-kill** is already exercised on the live path by
  `test_stream_idle_guard_kills_keepalive_trickle`, against a stream that blocks until the watchdog
  shuts its socket — the same mechanism, on the transport that actually runs.
* **non-SSE fallback** was a shape only the urllib transport could produce (a response that iterates
  raw lines and never sends `data:`). On the SDK path a non-streaming endpoint behind a streaming
  request yields nothing, and `_post`'s empty-body classification owns that case; the degenerate
  reassembly is pinned directly.

`tests/test_llm_streaming_surface.py` states both halves — the surface is gone AND the live path
still owns what the surface carried — because a dead-code deletion whose contracts leave with it is
a regression wearing a cleanup's clothes. It also cross-references the covering stall test by name,
so renaming that test away is caught here rather than silently leaving the contract unexercised.
Teeth-verified against 6 breakages.

Two of the assertions had to be sharpened before they bit, and both failure modes are worth naming:
a `_raw_socket` substring check flags the SURVIVING `_stream_raw_socket`, and `"shutdown" in source`
stays green when `sock.shutdown(...)` becomes `sock.close()` — which is precisely the regression,
since close() does not unblock a recv() wedged in the kernel.

*Follow-up (2026-08-03):* a THIRD test depended on the removed `urllib.request` import —
`test_complete_text_stream_bails_on_a_keepalive_stall` in `tests/test_assistant_mega_review.py`.
It was missed because the reference sweep that cleared the deletion was read through `head -20` and
this file fell below the cut; the lesson is that a truncated grep is not a clearance.

The test is now re-pointed rather than deleted, because its PROPERTY is real and was not being
tested: it scripted the stall by patching `llm.urllib.request.urlopen`, which the openai-SDK path
never calls, so the patch was inert and what actually made it pass was the client failing for
unrelated reasons. The stall is injected at the live seam instead — a stream of content-free
keepalive chunks over a socket the watchdog can shut down — so the idle guard is the thing under
test.

It is also drained on a worker thread with a hard join. The regression here is a HANG: with the idle
clock reset by keepalives the generator never ends, and an in-line `list(...)` wedges the whole
suite instead of reporting a failure. Teeth-verified — flipping `_chunk_has_content` to always-true
now fails in ~5s with "the keepalive stall was never detected; the stream ran forever".

#### CO-04 · MEDIUM · duplication · effort: medium

**Three parallel stream-reassembly loops in one client; fallback block triplicated**

*Locations:* `looplab/core/llm.py:523-570`, `looplab/core/llm.py:572-657`, `looplab/core/llm.py:893-1011`, `looplab/core/llm.py:548-559`, `looplab/core/llm.py:618-630`, `looplab/core/llm.py:965-997`, `looplab/core/llm.py:365-394`, `looplab/core/llm.py:929-957`

*Evidence:* `_accumulate_stream` (523-570), legacy `_read_stream` (572-657) and `complete_text_stream` (893-1011) each hand-roll SSE-delta accumulation. The tool-call slot-merge block (setdefault slot, id/name/arguments append) is duplicated near-identically at 548-559 and 618-630 (the SDK path first converts via tc.model_dump()). `complete_text_stream` separately re-implements `_sdk_chat`'s stream setup: the same `header_join = self.header_timeout + min(10.0, self.header_timeout)` computation (365 vs 929), the same own-and-close finally dance (386-394 vs 952-957), and the same `_is_stream_options_reject`/`_is_reasoning_reject` 400-retry pair (734-751 vs 973-981). Inside `complete_text_stream` the blocking-fallback block `delegated_to_fallback = True; text = self.complete_text(messages); if text: yield text; return` appears three times verbatim (965-970, 982-987, 992-997).

*Recommendation:* After deleting the legacy path (see dead-code finding), extract one `_open_bounded_stream()` helper (permit + bounded create + _streaming_body + close-on-exit) shared by `_sdk_chat` and `complete_text_stream`, one delta-merge helper for tool-call slots, and one `_fallback_to_blocking()` local for the triplicated block.

*Resolution (2026-08-03):* Most of this finding was DISCHARGED BY CO-03 rather than refactored: the
legacy `_read_stream` is gone, so the three reassembly loops are two, and the duplicated tool-call
slot-merge (548-559 vs 618-630) had its second copy inside the deleted function. What remained is
done here.

* `_header_join()` — the `header_timeout + min(10.0, header_timeout)` budget, computed once instead
  of at both streaming entry points. This is the budget that makes a black-holed request fail over
  near `header_timeout` rather than minutes later on the ~180s idle timeout, so two expressions for
  it is precisely how one gets loosened alone.
* `_fallback_to_blocking()` — the block that appeared three times verbatim inside
  `complete_text_stream`. It is a nested GENERATOR delegated to with `yield from`, because the
  caller is one; the `return` stays at each call site so the stream's termination remains visible
  where it matters.

`_open_bounded_stream()` was NOT extracted. The two remaining creates differ in the ways that
matter — `_sdk_chat` holds ONE broker slot continuously across the header wait AND the body and
passes `counted=True` so `_bounded_create` does not take a second, while `complete_text_stream` owns
its Stream across a `yield` so a consumer that cancels mid-answer still closes the socket. Both
arrangements carry incident comments explaining why the simpler nesting was wrong. Merging them
would either re-introduce the double-count or hide the close-on-cancel ownership, so the shared part
(the budget) was extracted and the divergent part left explicit.

**What the teeth pass established about the accounting flag.** `delegated_to_fallback` reads as the
guard that stops a delegated stream being billed on top of the fallback's own call — but all three
delegation sites sit under `if not pieces:`, so at each of them `stream_completed` is False and
`pieces` is empty, and `(stream_completed or bool(pieces))` is already False. The clause changes no
arithmetic today; it is a guard against a fourth site that delegates AFTER yielding content. A
behavioural test therefore passes with the clause deleted, and would keep passing right up until
someone adds the site that needs it — so `tests/test_llm_stream_setup.py` pins the INVARIANT the
guard rests on (every delegation site is under `not pieces`) and says why. All 9 teeth bite.

#### CO-05 · MEDIUM · under-decomposition · effort: medium

**OpenAICompatibleClient._post is a ~200-line multi-concern method inside an ~890-line class**

*Locations:* `looplab/core/llm.py:673-871`, `looplab/core/llm.py:195-1088`, `looplab/core/llm.py:429-520`

*Evidence:* `_post` (673-871) interleaves at least six concerns in one loop body: T7 cache lookup/deep-copy/zeroing (678-699), per-attempt stream/degrade decision (718-719), five exception-specific retry policies (726-804), empty-200/keepalive-stream classification (805-850), billing of empty-but-billable envelopes (843-846), and cache insertion with LRU eviction (864-870). `_bounded_create` + its inflight/teardown accounting (302-314, 403-427, 429-520) is another ~120 lines of intricate lock choreography. The class as a whole (195-1088) owns transport, retry, caching, degradation ratchets, pool teardown, cost accounting and tracing. Every concern is well-commented and incident-motivated, but the method's length means any new provider quirk is another branch in an already 6-way exception ladder.

*Recommendation:* Split `_post` into `_cache_get`/`_cache_put`, a `_classify_response(parsed, use_stream)` returning accept/retry/stall, and a retry-policy table keyed on exception type; move the cache into a small `_ResponseCache` class. Behavior-preserving mechanical extraction only — the comments move with the code.

*Resolution (2026-08-04) — `_post` is 199 → 83 lines; five concerns are now units with their own tests.*

Behaviour-preserving mechanical extraction, comments moved verbatim with the code
(`looplab/core/llm.py`):

* `_cache_get(ck)` / `_cache_put(ck, body)` — T7 lookup + recency bump + hit-usage zeroing, and
  insertion + LRU eviction. Both no-op on an uncacheable key or a disabled cache, so `_post` no
  longer branches on either.
* `_retry_or_raise(exc, attempt, use_stream)` — the six-way exception ladder, in the SAME order (the
  stream_options-before-reasoning order is load-bearing and now says so in the docstring). Two
  outcomes only: RETURN means "another attempt", with the backoff already served and the returned
  flag carrying the SSE ratchet; every other path RAISES the `LLMError` the ladder raised. `_post`'s
  six `except` clauses collapse to one, over `(openai.APIError, json.JSONDecodeError)` — both
  families, because a keepalive-only body escapes the SDK's decoder as a raw `JSONDecodeError` that
  is *not* an `APIError`, and narrowing the catch to the SDK base would let it abort the run.
* `_keepalive_stall(parsed, use_stream)` — the empty-200 classifier, now a `staticmethod` and a pure
  function of the parsed body.
* `_account_keepalive_stall(parsed)` — billing a stalled-but-billable envelope.

The point of the split is testability, not line count. The keepalive-stall table has the most
expensive failure mode in the method — a false "stall" both regenerates minutes of reasoning tokens
and ratchets the client permanently off SSE — and it was previously reachable only through a full
fake transport, so the two rows with real incident history (`reasoning` present, `finish_reason`
present) were sampled rather than pinned.

`tests/test_llm_post_decomposition.py` (34) pins the units: the 9-row keepalive table, the billing
pair, twelve retry-policy rows (backoff served vs not, the stream-only degrade ratchet, `Retry-After`
positive/capped/non-positive, throttle-403 vs hard-403, the reject ORDER, the decode retry, the
unclassified-SDK-error catch-all), the cache accessors (deep copy, zeroed counters, uncacheable
no-op, LRU victim), and three structural guards — `_post` keeps exactly one `except`, that `except`
still names both families, and `_retry_or_raise` ends in a `raise` so an unclassified error can never
return `None` and read as "retry, not stalled". Teeth-tested against 15 breaks, all of which bite.
The pre-existing end-to-end `_post` tests in `test_openai_client.py` are unchanged and still pass —
they are the behaviour-preservation evidence.

*Completed (2026-08-04) — the two remaining recommendations.*

**The retry-policy table now exists**: `OpenAICompatibleClient._RETRY_POLICY`, an ORDERED tuple of
`(types, handler-name)` pairs dispatched top-down by `_retry_or_raise`, with each former ladder rung
its own `_policy_*` method. A list rather than a dict because both properties a dict would lose are
load-bearing — SUBCLASS dispatch (`APITimeoutError` must reach the `APIConnectionError` handler;
`RateLimitError`/`InternalServerError` share one) and ORDER (a `stream_options` rejection also
matches `_is_reasoning_reject`'s generic keys, so BadRequest must be tried before anything that could
claim it). The tail entry is `(None, "_policy_unclassified")`: whatever the SDK grows next still
becomes a clean `LLMError`, because only `LLMError` triggers the role layer's retry+fallback. The
table is registry-guarded in BOTH directions, the discipline CLAUDE.md applies to the other
duck-typed seams — every declared handler must exist, and every `_policy_*` method must be declared,
so a handler renamed out of the table is a red test rather than a rung that silently stops running.

**The cache is a class**: `_ResponseCache` owns the `OrderedDict`, its bound (`max_entries`), the
lock that pairs each read-modify-write, and the deep copies in BOTH directions. The earlier objection
— that a wrapper would have to re-expose a dict surface for its callers — was a reason to update six
test call sites, not a reason to leave three loose attributes on the client. It exposes `get` (copy +
recency bump), `put` (copy + evict), `peek` (the stored entry, no copy, no bump — so a test's own
look cannot change which entry the next eviction takes) and `__len__`/`__contains__`/`__iter__`.
`_cache_lock` and `_cache_max` are gone from the client; `self._cache is None` still means caching is
disabled.

Six extra tests cover the class directly (both copy directions, a miss answering `None` rather than
raising, `peek` not refreshing recency) and four cover the table (order, catch-all tail, the two-way
registry, and the dispatcher holding no policy of its own). The teeth harness grew 15 → 23 breaks,
all biting.

**Still deliberately NOT done:** the finding also names `_bounded_create` + its inflight/teardown
accounting as "another ~120 lines". Measured at 92 lines today and left alone — unlike `_post` it is
ONE concern (bounded creation with pool teardown under concurrent siblings), so splitting it would
scatter the lock choreography rather than separate concerns. `__init__` (123 lines) is likewise
config normalization only, and is not named by the finding.

#### CO-06 · MEDIUM · duplication · effort: small

**Two near-identical bounded redacting JSON-tree sanitizers (tracing vs advisory_payloads)**

*Locations:* `looplab/core/tracing.py:73-132`, `looplab/core/advisory_payloads.py:467-499`, `looplab/core/tracing.py:46-70`, `looplab/core/advisory_payloads.py:436-447`

*Evidence:* `tracing.sanitize_trace_value` (73-132) and `advisory_payloads._tree` (467-499) are structurally the same function written twice: recursive walk with depth cap (5), per-container item cap (64), shared mutable char-budget cell (`remaining[0]`/`budget[0]`), int bounded to ±2^63 else stringified, non-finite float stringified, `is_secret_key_name(key)` → "***", strings through the redactor with a cap. They differ only in constants and which redaction entry point they call (`_trace_text` vs `_text`, both thin wrappers over `redact_persisted_text`). Both files also carry their own budgeted-text helper (`_trace_text`+budget bookkeeping in `_trace_messages` vs `_text`). Both walkers enforce separately-maintained caps (tracing's `_TRACE_TREE_TOTAL_ITEMS_MAX` vs advisory's `_MAX_TREE_ITEMS` cell), so a redaction fix or cap change landing in one walker silently misses the other durable boundary.

*Recommendation:* Move one parameterized `bounded_redacted_tree(value, *, max_chars, max_items, max_depth, max_total_items)` into redact.py (which both already import) and have tracing and advisory_payloads call it with their own constants.

*Resolution (2026-08-02):* done — `core/redact.py::bounded_redacted_tree(value, budget, items, *,
max_items, max_depth, str_cap, key_cap)`. The two mutable cells are passed IN rather than created
inside, because `advisory_payloads` charges URLs and verdict rows against the same page budget; a
private copy would let the page double-spend.

The recommendation understated the problem: the two walkers differed in BEHAVIOUR, not only in
constants, so the collapse had to choose. A differential harness over a 41-value corpus (nested,
deep, wide, secret keys, oversized strings, big ints, non-finite floats, unicode, control characters,
bytes, an `IntEnum`, an opaque object, a mapping whose `items()` raises) measured it, run against a
`git worktree` of the pre-refactor tree:

* **the TRACE boundary is byte-identical, 0/41** — that half is provably behaviour-preserving;
* **the advisory boundary changes in exactly 7 places**, all four adopted divergences and nothing
  else.

Every divergence had tracing safer, so tracing's semantics won, each named in the docstring:

1. **A hostile mapping degrades instead of RAISING.** This is a defect fix, not a tightening: a
   `dict` subclass whose `items()` throws took the advisory projection down with a `RuntimeError`,
   where tracing already answered `"<mapping unavailable>"`. A redaction boundary that can raise is
   one that can drop a whole payload.
2. **A key that redacts to empty is dropped**, not emitted as `""` — two of them collide into one
   JSON member and silently discard a value.
3. **Depth is cut at `>= max_depth`**, the earlier of the two cuts, with the marker charged to the
   budget like any other emitted text.
4. **An `int` SUBCLASS stays an int** (`isinstance`, not `type(...) is`), so an `IntEnum` keeps the
   number a reader is trying to compare instead of becoming a string.

Only `str_cap` and `key_cap` remain per-caller, and both are load-bearing: a trace value spends its
whole remaining budget on one string (a span is already the bounded record), while an advisory
payload caps each at 2 000 so one oversized early row cannot starve the rows below it.

`tests/test_bounded_redacted_tree.py` (34) pins the redaction properties, both budgets, the cyclic
and depth cuts, the scalar table, each caller's declared cap, and — the point of the whole finding —
that the two boundaries now AGREE on what a value becomes. Teeth-tested against five breaks: the
mapping guard narrowed, secret masking limited to depth 0, the depth cap effectively removed, the
budget cell copied instead of shared, and the advisory per-string cap dropped.

#### CO-07 · MEDIUM · layering · effort: small — **RESOLVED (2026-08-02)**

**core→agents layering violation: Settings validation lazily imports agents.cli_agent**

*Locations:* `looplab/core/config.py:1358-1363`, `looplab/agents/cli_agent.py:21-22`, `looplab/core/task_kinds.py:9-14`

*Evidence:* CLAUDE.md's layering rule is "core imports nothing above itself", yet `Settings._check_trust_gate` does `from looplab.agents.cli_agent import PRESETS` (config.py:1358) — the only upward import in the whole core package (grep-confirmed). cli_agent itself imports core.models and core.validate at module scope, so the cycle is avoided only by the laziness; every `Settings(...)` construction now imports part of the agents layer as a side effect, and a future module-scope import in cli_agent's transitive closure that touches config would deadlock the import graph. The repo already has the right pattern for exactly this problem: task_kinds.py exists so "generated configs, CLI Genesis and web launch" share a kind vocabulary in core.

*Recommendation:* Move the developer-backend name registry (the PRESETS keys, not the preset bodies) into core — e.g. a `DEVELOPER_BACKENDS` tuple in task_kinds.py or a new core module — have cli_agent build PRESETS keyed off it, and validate against the core constant. A two-way source-scan test (the project's established registry pattern) keeps them in sync.

*Resolution (2026-08-02):* DUPLICATE of **XP-04**, and already closed by it — two review lanes found
the same upward import from different angles. `core/config.py::DEVELOPER_BACKENDS` is the closed set,
`agents/cli_agent.py` asserts at IMPORT time that its PRESETS are covered by it, and
`tests/test_developer_backend_registry.py` checks both directions plus the layering rule for the
whole `core/` package. Nothing further to do here; see XP-04 for the reasoning and the teeth tests.

#### CO-08 · LOW · inconsistency · effort: small

**Six bespoke canonical-JSON→SHA-256 digest minters with no shared core**

*Locations:* `looplab/core/models.py:689-757`, `looplab/core/models.py:779-924`, `looplab/core/advisory_payloads.py:249-265`, `looplab/core/fitness.py:113-118`, `looplab/core/models.py:1148-1161`, `looplab/core/models.py:1184-1191`

*Evidence:* The same idiom — validate/bound a payload, `json.dumps(..., sort_keys=True, separators=(",", ":"), allow_nan=False)`, sha256, prefix — is written four times: `idea_proposal_digest` (with a 50-line private bounded walker `_complete`), `_card_action_digest` (with its own private `_number`/`_params`/`_space`/`_node_id` validators), `stable_advisory_ref`, and `verifier_evidence_digest`; two sibling minters use ad-hoc variants — `hypothesis_statement_digest` (sha256 over a normalized string) and `run_setup_key` (md5 over a joined argv string; md5 also appears in `hypothesis_id`). The frozen preimages themselves must not change (versioned identities), but each site also re-invents the bounding/validation scaffolding around the dump, and md5 coexists with sha256 across the family, so a reader must re-derive each one's guarantees from scratch.

*Recommendation:* Add one `canonical_json_digest(payload, *, prefix)` helper (dump+hash only, no bounding) and route the non-frozen call sites through it; leave the frozen v1/v2 preimage builders byte-identical but have them call the shared dump/hash tail. Document per-site why md5 remains where it does.

*Resolution (2026-08-03):* `core/jsonutil.py::canonical_json_digest(value, *, prefix="", cap=None)`
on top of the `canonical_json` extracted for SE-08, plus `DIGEST_TEXT_CAP` naming the 131_072-byte
preimage budget that two of the minters had spelled as a literal.

The split is exactly the one the finding asked for: the **preimages stay owned by their call sites**
— each is a versioned, frozen wire contract, and merging the bounding walkers would be a wire change
— while the **tail is shared**, because that is the part where two spellings of "canonical" produce
two digests for one logical value and a receipt written by one reader silently stops verifying for
the other. Byte-identity is pinned by a test that keeps the pre-extraction spelling and compares.

Three sites route through it (`idea_proposal_digest`, `_card_action_digest`, `stable_advisory_ref`);
`verifier_evidence_digest` hashes `canonical_json` directly instead, because it is the only one that
must RAISE rather than fail closed — its snapshot is assembled from already-validated scalars, so an
unencodable value there is a bug in that module, and a silent `None` would hide it.

The cap is per-caller and stays that way: the two agent-output minters pass it, `stable_advisory_ref`
deliberately does not (its callers pass an already-sanitized, deliberately small projection, so a
size refusal could only drop a well-formed advisory). For `card_action_digest` the cap turns out to be
a BACKSTOP — every field has its own bound, which refuses with the name of the wrong field long
before a byte budget that can only say "too big" — and the test says so rather than implying the cap
is the working limit.

**md5, per site, all three kept.** `hypothesis_id` uses it for a 6-hex DISPLAY suffix that
disambiguates two slugs, with the sha256 identity right above it — and it is a frozen join key, so
changing it orphans every ledger entry and capsule that joined on it. `run_setup_key` compares against
a key already written into a durable `run_setup_finished` event, so changing it makes every in-flight
run re-run a completed setup; there is also no attacker who both controls the local argv and benefits
from a collision that makes their OWN setup be skipped. `vectorstore.hash_embed` uses it as a
token→bucket function that must be stable across processes, which Python's salted `hash()` is not.
Each now says so in place.

#### CO-09 · LOW · inconsistency · effort: small

**Eight subtly different 'usable finite number' predicates across core**

*Locations:* `looplab/core/fitness.py:31-41`, `looplab/core/comparison.py:144-154`, `looplab/core/profile.py:10-26`, `looplab/core/parse.py:23-41`, `looplab/core/llm.py:112-120`, `looplab/core/tracing.py:320-327`, `looplab/core/models.py:445-454`, `looplab/core/models.py:126-147`

*Evidence:* core carries at least eight scalar-coercion/finiteness predicates with slightly different contracts: `fitness.is_usable_metric` (isinstance int/float, not bool, finite), `comparison.finite_measurement` (`type(value) not in {int, float}` — rejects subclasses), `profile._is_number` (same intent, hand-rolled inf check), `parse.to_float/to_int` (which claims to be "The one spelling of scalar coercion previously re-implemented per module"), `llm._safe_token_count` (`type(value) is not int`, int64 bound), `tracing._token_int` (coercing, clamping), `models._resource_int` (int-or-integral-float, int31 bound) and `models.safe_lesson_node_count` (adds decimal-string parsing). Several differences are deliberate (strict durable readers vs lax telemetry) and individually documented, but there is no map of which contract applies where, and parse.py's "one spelling" claim is no longer true.

*Recommendation:* Not a merge-everything item — the strict/lax split is real. Consolidate the genuinely identical ones (profile._is_number ≈ is_usable_metric; tracing._token_int ≈ a clamping variant of llm._safe_token_count) into parse.py or fitness.py, and fix parse.py's stale 'one spelling' docstring to enumerate the intentional strict variants.

*Resolution (2026-08-03):* one merge, one map, one correction — and one of the two proposed merges
was declined on inspection.

`profile._is_number` was rule-identical to `fitness.is_usable_metric`, character-different only, so it
is now an alias. The profiler-specific REASONS stay written down above it, because the shared
predicate cannot carry them: the consequence of accepting `10**400` is that
`profile_column`'s `sum(nonnull)/len(nonnull)` raises OverflowError and takes the leakage front-end
down with it, which is a fact about the profiler, not about the rule.

`tracing._token_int` and `llm._safe_token_count` were NOT merged, and the finding's "≈ a clamping
variant" understates the gap. `_safe_token_count` refuses anything that is not exactly `int` — an
integral float from a provider is a bug it must not round away, because the value lands in the
durable cost ledger. `_token_int` coerces (`int(value or 0)`) and never raises, because tracing must
not be able to perturb the operation it observes. On the input that distinguishes them — a provider
reporting `3.7` — one answers 0 and the other 3, and both are right for their caller. A shared
"clamping variant" would have to be parameterized on the very thing that differs.

`parse.to_float`'s "the one spelling of scalar coercion previously re-implemented per module" is now
scoped to what it actually owns (COERCING parse of wire TEXT), and a contract map above it names the
six strict readers and what each refuses. That map is the deliverable: the danger here was never line
count, it was a reader importing the nearest predicate and getting a durable bug — a metric that
accepts `"3.5"`, or a comparison claim that trusts a subclass which overrides `__lt__`.
`tests/test_digest_and_number_contracts.py` drives those two disagreements rather than asserting the
prose.

#### CO-10 · LOW · over-engineering · effort: small

**llm.py re-export shim freezes ~32 private helper names and its monkeypatch claim is subtly wrong**

*Locations:* `looplab/core/llm.py:59-69`, `looplab/core/llm_streaming.py:6-9`, `looplab/core/llm_streaming.py:123`, `looplab/core/llm_streaming.py:257`

*Evidence:* llm.py re-imports ~32 underscore-private names from the three split siblings so "tests and callers import/monkeypatch them THROUGH this module" and both paths "keep resolving to the SAME objects". The 'same objects' claim holds for reads, but the monkeypatch claim does not hold for intra-sibling calls: `_stream_with_idle_guard` calls `_stream_raw_socket` through llm_streaming's own namespace (llm_streaming.py:123), so patching `looplab.core.llm._stream_raw_socket` rebinds only llm.py's alias and never reaches the live call — the exact silent-no-op failure mode the project's registry-guard convention exists to prevent, here with no guard. The shim also permanently publishes private helpers (`_backoff`, `_err_body`, `_tool_call_slot`, ...) as de-facto API surface of core.llm. The split itself is documented and sound; the blanket private re-export is the accidental-complexity part.

*Recommendation:* Trim the re-export list to the names tests actually import (grep-driven), patch the remaining tests to import from the owning sibling, and correct the docstrings' monkeypatch

*Resolution (2026-08-03):* The false claim is corrected; the re-export list is deliberately KEPT.

The docstrings now say what the shim actually gives you: `looplab.core.llm._X`, the flat
`looplab.llm._X` and the owning sibling's `_X` all READ the same object, which is what keeps existing
imports and direct calls working — and that this is NOT a monkeypatch seam. Rebinding the barrel
alias replaces only that alias; a sibling calling `_X` through its own module globals keeps calling
the original, so the patch is a silent no-op. `llm_streaming`'s own docstring now names the affected
helpers and says to patch that module instead.

Trimming the list was considered and rejected. `CLAUDE.md` documents the barrel as "every split name
re-exported through `llm.py`", the re-export costs nothing at runtime, and a grep of the suite shows
NO test patches a private llm name through the barrel today — the tests only read through it, which
the shim supports correctly. Trimming would therefore buy churn against a documented convention
rather than safety. `tests/test_core_contracts.py` pins that: the read-identity holds for every
shared name, the affected set really is non-empty (`_stream_raw_socket` is in it), and a test that
ever starts patching through the barrel fails immediately. claim (patch the sibling module for intra-module call sites). Alternatively add a small source-scan test asserting every re-exported name is referenced somewhere outside core/, so the list can shrink safely over time.

#### CO-11 · LOW · dead-code · effort: small

**Speculative/no-consumer projections retained in hot models: grouped_beliefs and selection_key**

*Locations:* `looplab/core/models.py:2079-2139`, `looplab/core/models.py:1302-1308`, `looplab/core/fitness.py:159-165`

*Evidence:* `RunState.grouped_beliefs()` is a 60-line projection whose own banner says it is "AVAILABLE for a future UI / lessons / verdict view — it currently has no production consumer"; grep confirms the only callers are tests/test_cards.py and docs. `SearchFitness.selection_key`'s docstring likewise states "retained as the plain-tuple reference (no non-test callers today...)" — though it is in fact still used internally by rank_promotion/ci_tie_set/best_ci, so only the 'reference' framing is stale, not the code. grouped_beliefs is true speculative generality: 60 lines plus 4 tests maintained for a consumer that may never come, inside the repo's largest module.

*Recommendation:* Either wire grouped_beliefs to its intended consumer or move it (and its tests) out of RunState into the events/ projection layer where the other derived views live; fix selection_key's stale 'no callers' note.

*Resolution (2026-08-03):* Moved, not wired — there is still no production consumer, and inventing
one would be worse than leaving the view available. `events/belief_projection.py::grouped_beliefs(st)`
now holds it, with the other derived views, and `RunState` no longer carries it. `tests/test_cards.py`
follows the move.

Kept computation-identical. The verdict roll-up stays a per-member LABEL MAX rather than a call to
`_evidence_verdict` over the unioned evidence — the two agree, and the label max is what the existing
tests pin. What changed is that the choice is now a choice: on `RunState` it was forced, because
calling the helper would have crossed the core → events layer the wrong way.

`selection_key`'s "no non-test callers today" note is corrected. It is the soundness-blind,
metric-first leader that `rank_promotion` sorts by and that `ci_tie_set`/`best_ci` both start from;
the stale note invited exactly the deletion that would break them. A test asserts the live caller
count so the note cannot go stale silently again.

#### CO-12 · LOW · other · effort: small

**Stale load-bearing REVIEW comment in atomicio: 'zero production callers' is now false**

*Locations:* `looplab/core/atomicio.py:298-311`, `looplab/core/run_reset.py:116`, `looplab/core/run_deletion.py:186`, `looplab/serve/reset_route.py:326`

*Evidence:* strict_atomic_write_bytes carries a REVIEW(2026-07-16) comment whose point (2) asserts "ZERO PRODUCTION CALLERS: the actual paid-record writers ... never route through this helper, so the Windows write-through publication added for them protects nothing yet." Grep shows strict_atomic_write_text/bytes now have many production callers: core/run_reset.py:116, core/run_deletion.py:186, serve/reset_route.py:326, serve/routers/control.py:546,789, serve/routers/reports.py (4 sites), serve/deletion_transaction.py:167, search/speculation_quality.py:2471. In a codebase whose stated convention is "comments are load-bearing" and "stale docs are treated as a bug", a security-adjacent durability helper describing itself as unused misleads exactly the reviewer that comment was written for. Points (1) and (3) of the same comment (indeterminate postcondition, Windows race/leak) remain open and undocumented in the docstring proper.

*Recommendation:* Update the REVIEW comment: delete point (2), promote point (1)'s 'exception means INDETERMINATE' into the docstring contract, and file/point at an issue for po

*Resolution (2026-08-03):* Point (1) is promoted into the docstring, where a caller reads it: an
exception from `strict_atomic_write_bytes` means INDETERMINATE, not "not written" — a
`strict_fsync_parent` failure AFTER the replace raises while the destination already holds the NEW
bytes, so a writer that rolls back on exception can leave a visible record it will never reconcile.
Callers owning a paid-work claim need a recovery probe, not a bare rollback.

Point (2) is deleted rather than corrected in place: the helper now has ten calling modules across
`core/`, `search/` and `serve/` (reset, deletion, reports, trace-clear, assistant, control). The
comment does not repeat the old phrase, so a grep for it does not land on a note explaining that it
is false.

Point (3) is RETAINED under a `STILL OPEN:` heading — the Windows parent-publication race and the
orphaned `.{name}.{rand}.tmp` directory are real, Windows-only, and unreachable from this repo's
POSIX CI. A test asserts both the promoted contract and that the retained gaps were not lost with
the deleted point.int (3)'s Windows temp-dir leak.

#### CO-13 · LOW · flat-code · effort: small

**_check_trust_gate is a misnamed grab-bag validator for nine unrelated enum fields**

*Locations:* `looplab/core/config.py:1322-1372`

*Evidence:* The model_validator named `_check_trust_gate` validates trust_gate, merge_mode, novelty_mode, strategist_backend, eval_trust_mode, seed_mode, backend, developer_backend and llm_parser — nine independent closed-vocabulary fields — as a linear if-chain of hand-written `raise ValueError` blocks, each repeating the same "must be a|b|c, got {!r}" message format. New enum-ish fields keep being appended here (the comment trail shows three accretion waves).

*Recommendation:* Replace with a declarative `_ENUM_FIELDS = {"trust_gate": ("audit","gate","block"), ...}` table iterated by one loop (message format preserved), with the two lazy-registry cases (developer_backend, llm_parser) resolved via callables in the same table. Rename to `_check_enum_fields`.

*Resolution (2026-08-03):* Done as recommended. `Settings._ENUM_FIELDS` is the table and
`_check_enum_fields` the one loop; the refusal message keeps the `"<field> must be a|b|c, got <v>"`
shape (`llm_parser` loses a stray "one of", the only wording change). The two lazy cases are
callables in the same table: `developer_backend` resolves `DEVELOPER_BACKENDS` and `llm_parser` the
parser registry through a small `_parser_names()` helper, both deferred so core neither executes
agents-package code on every `Settings()` (doc 25 XP-04) nor drags the parser module into
`config`'s import.

The stale `_check_trust_gate` references in `core/parse.py` and two `config.py` comments are
re-pointed at the new name.

What the table buys is not brevity. A new enum-ish field added to `Settings` and forgotten here fails
SILENTLY — an out-of-set value does not raise, it falls through whatever consumes the field as a
no-op, which is how a mis-cased `LOOPLAB_NOVELTY_MODE=LLM` turned the novelty gate off with no
diagnostic. `tests/test_core_contracts.py` pins the covered field set exactly, so adding a field
without a vocabulary is a failing test.


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

*Resolution (2026-08-04):* `serve/_log_index.py` exists and owns the copy-pasted halves —
`PathLocks` (moved verbatim, docstring included; both copies deleted) and `validated_index_bound`,
the LRU-bound guard both constructors spelled identically down to the message. Neither index carries
its own definition any more, which `tests/test_serve_module_seams.py` asserts by scanning the
package for a second `PathLocks` class.

**The shared `_Index` lifecycle is extracted too** (2026-08-04): `LogIndexCursor` in the same module
holds the six fields both dataclasses declared — `identity`, `metadata`, `revision`, `observed_size`,
`valid_end`, `torn_tail` (`command_observation` spelled the last one `stopped_tail`) — plus the two
operations both performed on them: `note_scanned(valid_end, snapshot_size, torn)` at the end of a
scan and `mint_revision()` when a rewritten prefix must fail outstanding client cursors closed. Both
`_Index` classes now subclass it and declare only their own payload. `generation` gains a `None`
default purely because a dataclass cannot put a required field after the base's defaulted ones; every
construction site passes it explicitly.

`note_scanned` takes all three together on purpose: a `valid_end` advanced without its
`observed_size` lets the next poll skip the bytes between them, and a stale `torn_tail` stops the
re-scan a writer filling reserved bytes in place depends on. As three separate assignments that was
three chances to update two of them.

**What is still NOT merged is the SCAN, and the "~60% overlapping scaffolding" estimate does not
survive a read.** Measured field by field, the two payloads diverge on everything the cursor does not
cover, including the fences the finding calls common:

* the rewrite fence is a bounded content PROBE in `command_observation` (`probe_signature`, hashed
  sentinel windows) and a prefix ANCHOR in `log_pages` (`anchor`, a two-part boundary fingerprint) —
  different data, computed at different points, compared under different conditions;
* `_metadata` deliberately differs and each carries its own why-comment: `(mtime_ns, 1)` because
  Windows reports a transient ctime skew between `fstat` and `Path.stat` for the same open file, vs
  `(mtime_ns, ctime_ns)` where that skew is not in play;
* `log_pages._Index` additionally keys on a durable `generation` the other has no analogue for;
* the two `_scan` bodies share only the readline skeleton. Beyond it they enforce different row
  grammars (`event_sequence_continues` over decoded events vs strictly increasing `seq` over byte
  rows), different limits (`log_pages` bounds a single row at `MAX_SOURCE_ROW_BYTES` and reports
  `source_tail_limited`), and different accumulators.

A parameterized scanner over that would take a per-row callback, a per-module state object and a
per-module limit table — an abstraction with two implementations and no third caller, which is
harder to read than either original. The LRU registry dict is likewise left alone: in
`command_observation` its `_lock` also guards the short per-index memo sections
(`materialized_revision`, `folded_revision`), so folding the dict into a registry object would
either narrow that lock or drag the memos along with it.

Pinned by `tests/test_serve_module_seams.py` (29): the single-definition scan, both indexes using the
shared registry and bound, the identical rejection table (including `bool`, which is an `int`
subclass and would otherwise build a one-entry cache), one-lock-per-path exclusion, an unrelated path
NOT stalling behind a held one, the liveness rule that a referenced lock is never evicted, both
payloads subclassing the cursor AND still owning a payload of their own (so the merge cannot quietly
go too far), `note_scanned` moving all three fields, `mint_revision` rotating, and two fresh cursors
not sharing a revision — a class-attribute default instead of `default_factory` would make every
index in the process answer to the same client cursor. Teeth-tested with SR-12 against 17 breaks,
all biting.

#### SC-05 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**ctypes no-replace durable rename duplicated between reset and deletion**

*Locations:* `looplab/serve/reset_route.py:53-99`, `looplab/serve/deletion_service.py:136-170`

*Evidence:* _durable_archive_move (reset_route) and _durable_no_replace_move (deletion_service) are near-identical ~45-line functions: same sibling-parent check, lexists no-replace guard, _windows_move_write_through on nt, ctypes renameat2 (linux, AT_FDCWD/RENAME_NOREPLACE) / renamex_np (darwin, RENAME_EXCL) declaration and call, same errno handling and strict_fsync_parent. Only the error strings differ.

*Recommendation:* Move one durable_no_replace_rename(source, destination, *, label) into core/atomicio.py next to _windows_move_write_through, which both already import.

*Resolution (2026-08-02):* done exactly as recommended —
`core/atomicio.py::durable_no_replace_rename(source, destination, *, label)`. Both wrappers stay,
because each owns a DOMAIN rule with an operator-facing message the mechanics never had ("Replay
archives must remain in the run directory" vs "deletion quarantine must be a sibling of the run");
only the ~45 lines of ctypes moved, and `label` renders the two callers' differing error wording.

`tests/test_durable_no_replace_rename.py` (11) pins the three properties the function exists for: an
occupied destination is REFUSED rather than replaced (a plain `os.rename` would destroy whatever a
concurrent operation just created there), the parent is fsynced before the call returns (so a receipt
saying "moved" is never ahead of the filesystem), and a missing `renameat2` raises `ENOTSUP` rather
than falling back to a replacing rename — a silently downgraded durability guarantee is worse than a
failed operation.

One teeth-test came back SILENT and changed the test's claim rather than the code: weakening
`lexists` to `exists` is unobservable, because `RENAME_NOREPLACE` refuses a dangling symlink in the
kernel anyway. The docstring and the test now say what is actually true — the flag is the guarantee,
`lexists` is the early clean error — and the teeth case was replaced with one that drops BOTH (the
flag AND the check), which reddens three tests.

The consumer suites also caught a real miss: `deletion_service` uses `strict_fsync_parent` elsewhere,
so collapsing its atomicio import list broke `begin_or_resume_run_deletion` with a `NameError` that
the new unit tests could not see.

Side effect worth recording: this RETIRES a cross-package private seam.
`tests/test_cross_package_private_seams.py` went red because `serve/` no longer imports
`core.atomicio._windows_move_write_through` — the two callers used to reach past the package boundary
to assemble the primitive themselves, and now call one public function. The registry entry was
dropped; the declared private-seam surface is one name smaller.

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

*Resolution (2026-08-04) — the duplication is gone; `_execute` is 487 → 381 lines.*

Three phases now live outside `_execute` (`serve/run_commands.py`):

* **`_try_restart_claim(rd, path, record) -> bool`** — the RESTART_AFTER_EXIT replacement claim. The
  two copies were byte-identical after de-wrapping, so this one is a pure move. `False` means it
  terminalized and the caller returns.
* **`_spawn_under_claim(rd, path, record, command_id, *, restarting) -> (terminalized, pid)`** — the
  lease → Popen → persist-the-PID sequence, including both failure taxonomies. Returns the pid so the
  caller can do its own record bookkeeping.
* **`_terminalize_expired(rd, path, record, command_id, spec)`** — the post-loop deadline exit, the
  method's sixth lock scope and four more early returns.

The copies had really drifted, and unifying them settled three points:

1. The certain-failure remediation read `"…/retry endpoint (same intent)"` at admission and
   `"…/retry endpoint"` in the monitor. Adopted the admission wording: the retry serves the same
   durable intent on both paths, so dropping the clause was an accident, not a distinction.
2. `self._record_spawn_claim(rd, command_id, pid)` — the post-Popen half of the lease — was written
   identically at both call sites and moved INTO the helper. The durable order is unchanged (claim
   before record); only the in-memory field assignment now follows it.
3. The `record["engine_pid"]` divergence (admission guards `if pid is not None`, the monitor did not)
   stays at the call sites deliberately, so a reader sees it. It is benign — `_quarantine_spawn_claim`
   reads the pid off the CLAIM row, which `_record_spawn_claim` writes on both paths — and quietly
   picking one semantics would have been a behaviour change the finding did not ask for.

The heartbeat asymmetry the finding names (`_heartbeat_execution` present in the monitor's startup
poll, absent from the admission one) was checked and is NOT a live defect: reclaim requires POSITIVE
evidence that the owning process exited (`_claim_execution`'s `_execution_owner_definitely_gone`), and
`_active_command_ids` says so explicitly — "age protects only ambiguous/live owners from heartbeat
pauses". Nothing reads the claim file's mtime for a decision today. The two startup poll loops
therefore stay separate: unlike the spawn sequence they are genuinely different (the admission one
also checks `_domain_failure`, distinguishes ENSURE_RUNNING, and has a slow-start `else` branch that
extends to the absolute deadline), and merging them would need three flags to reproduce both.

Pinned by `tests/test_command_worker_spawn_phases.py` (16): the lease-before-Popen ordering as an
observed call order, the certain-vs-uncertain claim rules in both directions (a certain failure
releases the lease, an uncertain one keeps it and is non-retryable), the `restarting` wording, the
restart-claim won/lost/uncertain outcomes, and structural guards that `_execute` calls neither
`_spawn` nor `_claim_restart_spawn` directly and still routes BOTH sites of each through the helper.
Teeth-tested against 12 breaks, all biting.

*Completed (2026-08-04) — "split the monitor loop body from the admission phase", the recommendation's
second half.*

`_execute` is now a 22-line spine: terminal short-circuit → `_admit` → `_monitor`, with the
worker-failure handler and the execution-claim release around them.

* **`_admit(rd, path, record, command_id) -> (spec, record)`** (227 lines) is everything that runs
  under the per-run SEQUENCER. `(None, record)` means it already terminalized and there is nothing to
  watch. No status enum was needed — `spec` is the value the monitor needs anyway, and its absence is
  the stop signal.
* **`_monitor(rd, path, record, command_id, spec)`** (144 lines) is the observation loop plus the
  deadline exit. It runs OUTSIDE the sequencer and re-takes it only for the moments that must be
  serialized. `event_type` is re-derived from the record rather than threaded through: it is only
  used to NAME the operation in an error message, and `_admit` read it from the same field.

The sequencer became a plain `with self.sequence(rd):` covering `_admit`'s whole body. `_execute`
used to hand-roll `__enter__`/`__exit__` with a `sequence_held` flag its `finally` had to re-check —
one more thing to get right in a 460-line function, and now a `with` block releases on every early
return by construction.

`tests/test_command_worker_spawn_phases.py` grew to 23: the process-starting guards apply to all
three phases (so a third copy cannot hide in any of them), each helper is called once per phase, each
phase has its own size ceiling (so re-merging them is a red test rather than a slow drift back), and
the admission body is asserted to be exactly ONE lock scope with no `__enter__`/`sequence_held` left
in the spine. Two source-scanning guards elsewhere were re-pointed at `_monitor`
(`test_run_command_service.py`'s domain-progress slide assertion) rather than deleted. Teeth harness
12 → 14 breaks, all biting.

*Regression found and fixed (2026-08-04) — the two-value return became a contract, and one exit
missed it.*

Turning `_execute`'s inline admission into a function that RETURNS made every exit part of a tuple
contract the compiler does not check. One of the eighteen exits kept its bare `return`: the
ENSURE_RUNNING success inside the admission startup poll loop, reached whenever the engine this
command started folds the intent and acks before the short startup window elapses — the fast local
case. It handed `None` to `spec, record = self._admit(...)`, which raised `TypeError`, which the
spine's catch-all recorded as `command_worker_failed` OVER the `succeeded` status that exit had just
written. It surfaced as an intermittent full-suite failure in
`test_monitor_reensures_dead_preexisting_driver_and_heartbeats_long_pause` (an operator flipping the
driver dead between the intent append and the liveness check takes exactly this path), which is why
it read as a wall-clock flake rather than as the logic bug it was.

Two fixes, and the second is the one that generalizes:

* `return None, record` at that exit (landed independently and concurrently as `f6f9fe41`, whose
  comment is the one in the tree), plus an AST guard that EVERY `ast.Return` in `_admit` is a
  2-tuple — so the next exit added to the admission phase cannot repeat this.
* The spine's crash handler re-reads the record before writing. A record that is already terminal on
  disk is the durable answer and is left alone (this handler exists to make a crash observable, never
  to demote a command whose effect landed), and a genuine crash is now reported against what
  admission PERSISTED, so `event_seq`/`baseline_seq` survive — a failure record that dropped them
  reads as a command that never appended an intent, the one state operators are told not to
  auto-retry.

`tests/test_command_worker_spawn_phases.py` 23 → 28. The admission case is driven at BOTH altitudes:
once at the phase boundary, where the tuple contract actually lives and a bare `return` raises before
anything has been re-read, and once through the whole spine, because the crash guard would otherwise
mask exactly this regression end-to-end. Teeth harness 14 → 21 breaks, all biting.

#### SC-08 · MEDIUM · other · effort: small — **RESOLVED (2026-08-02)**

**Unresolved embedded review marker acknowledging an O(events) full-log read on the per-command append path**

*Locations:* `looplab/serve/run_commands.py:3721-3728`, `looplab/serve/run_commands.py:3786-3790`, `looplab/serve/run_commands.py:3485-3486`

*Evidence:* A literal 'CLAUDE REVIEW: [PERF]' comment block sits in production code at 3721-3726 stating that self._events(rd) (EventStore.read_all(): parse + Event-validate every row) is executed purely to read events[-1].seq, bypassing the incremental observation index (self._observe(rd).latest_seq) that was built specifically to avoid re-parsing the whole log per command. Both call sites (3727, 3788) still do the full read. The marker is a leftover finding that was neither fixed nor converted to a normal why-comment/issue.

*Recommendation:* Replace both baselines with observation.latest_seq (the CAS expected_last_seq on append remains the correctness authority, as the comment itself notes), and remove the review-artifact comment.

*Status (post-baseline):* Fixed on `master` by commit `c92b89f` (2026-08-01, immediately after this review's baseline): both call sites now read `self._observe(rd).latest_seq` from the incremental observation index, `self._events(rd)` is gone, and the marker was replaced by a why-comment. The finding is retained as accurate at the baseline.

*Resolution (2026-08-02):* re-verified against the live tree, not just the commit message: no
`CLAUDE REVIEW` marker remains anywhere in `serve/run_commands.py`, and both decision baselines read
`self._observe(rd).latest_seq`. Nothing left to do — recorded here so the finding is counted closed
rather than sitting open behind a status note.

#### SC-09 · MEDIUM · mergeable-entities · effort: medium

**public_cards.py keeps three parallel per-field dispatch chains that must be edited in lockstep**

*Locations:* `looplab/serve/public_cards.py:630-684`, `looplab/serve/public_cards.py:879-930`, `looplab/serve/public_cards.py:945-1027`, `looplab/serve/public_cards.py:34-45`

*Evidence:* Every Card wire field is classified by membership in ~10 category sets (_TEXT_LIMITS, _REF_FIELDS, _INT_FIELDS, ...) and then dispatched through three separate if-chains: _field_value (projection), _field_projection_lossless (exactness verification, a full mirror of the projector), and _field_slice (loss counting). Complex fields additionally get paired projector/verifier functions (_cross_run/_cross_run_lossless, _steering/_steering_lossless, _card_identity/_card_identity_lossless, etc.). Adding one field requires touching _FIELDS, a category set, and up to three dispatch chains; the file's own comments record a bug this caused (matched_concept_outcome rows verified against the wrong key set, line 77-82).

*Recommendation:* Replace the category sets + three chains with a single per-field descriptor table mapping name -> {project, is_lossless, slice_units}; generic kinds (text/ref/int/list) become shared descriptor factories, complex fields keep bespoke pairs but registered in one place.

*Resolution (2026-08-04) — TWO chains collapsed into `_FIELD_KINDS`; the third was never a per-field chain.*

`_FieldKind(project, lossless)` pairs each field's projector with its exactness verifier, and
`_FIELD_KINDS` is built from the existing category SETS via shared factories (`_text_kind`,
`_ref_kind`, `_int_kind`, `_nonneg_int_kind`, `_float_kind`, `_positive_float_kind`,
`_ref_list_kind`, `_int_list_kind`, `_bool_kind`, `_mapping_kind`, `_named_scalars_kind`), with the
eleven complex fields registered as explicit pairs. `_field_value` and `_field_projection_lossless`
are each three lines of table lookup.

The verifier is the half that made this worth doing. It decides whether the completeness RECEIPT
claims a field came through exactly, so a verifier that drifts from its projector does not corrupt
the wire data — it LIES about it, and this module's own comments record exactly that happening once
(`matched_concept_outcome` rows verified against the wrong key set). The two halves now sit on one
line together instead of fifty lines apart, and a field cannot acquire a projector without a
verifier.

**`_field_slice` is left alone: the finding miscounts it as a third per-field chain.** It dispatches
on the RAW VALUE's type (str → characters, list → items, dict → entries, else → values) with two
name-keyed special cases for dicts whose loss partition is a key SET rather than a length. Folding a
`slice_units` column into the descriptors would push per-field data into a function that is
deliberately type-driven, and the two special cases would still need naming somewhere.

Verified by golden master rather than by inspection: every field in `_FIELDS` × a 31-value corpus
(None/bools/ints/floats/NaN/inf/2^31 boundaries/empty and oversized strings/lists/dicts/tuples) run
through BOTH chains before and after — **1426 rows, zero differences**.

Pinned by six tests in `tests/test_card_public_projection.py` (31 → 37): every published field has
both halves, neither function may contain a category-set or per-name branch again, an unregistered
field is skipped AND never certified exact, and per generic kind a clean value round-trips exactly
while a clipped/sliced/rejected one is never certified. Teeth-tested against 5 breaks, 4 biting — the
fifth (dropping `len(raw) <= _MAX_ITEMS` from a list verifier) turned out to be a NON-break: `_refs`
already slices to the bound, so `_refs(raw) == list(raw)` fails on a long list anyway and the guard
is redundant in the original expression, which is preserved verbatim.

#### SC-10 · MEDIUM · inconsistency · effort: medium

**ShareStore duplicates ReviewStore's capability-link concept with weaker, inconsistent hardening**

*Locations:* `looplab/serve/assistant.py:289-411`, `looplab/serve/reviews.py:159-556`

*Evidence:* Both are one-file-per-capability bearer-token stores: sha256 token_hash (never the token), TTL bounds, revoked_at tombstones, public() views stripping the digest, resolve() with constant-time compare. ReviewStore adds a required interprocess lock, O_EXCL id reservation, abandoned-reservation healing, and a recovery/replay contract; ShareStore has only an in-process threading.Lock, no cross-process exclusion on create/revoke (two uvicorn workers can interleave revoke_session's read-modify-write), and no reservation protocol. Same concept, two implementations, materially different guarantees.

*Recommendation:* Extract the common capability-store core (digest, TTL validation, tombstone semantics, atomic publish, resolve) and have both stores parameterize it; ShareStore then inherits cross-process safety for free.

*Evidence RE-VERIFIED (2026-08-03).* `ShareStore` has grown from the 122 lines the review measured
to 484 (`serve/assistant.py:935-1419`) — a peer hardened it substantially with `_safe_dir_locked`,
`_safe_record_path_locked`, `_validated_record`, `_prune_locked` and `revoke_token`. That made it
worth checking whether the finding had aged out before acting on it. It had not: `ShareStore.__init__`
held `threading.Lock()` and nothing else, while `ReviewStore` acquired `_interprocess_lock`
(`serve/reviews.py:211,220`) on top of a per-path process lock.

*Resolution (2026-08-04) — the GUARANTEE gap is closed; the shared-core extraction is not attempted.*

`ShareStore` now matches its sibling's locking contract exactly (`serve/assistant.py`):

* a module-level per-path process lock (`_SHARE_STORE_LOCKS`, keyed on the normcased absolute
  `<root>/.shares` path) so two `ShareStore` objects over one directory contend on ONE lock;
* `_store_lock()`, which takes that process lock with a bounded timeout and then a **required,
  non-blocking** `_interprocess_lock` on `<store>/.lock`, and raises the store's own retryable 503
  rather than yielding if either cannot be had — no thread-only fallback;
* the same MUTATIONS-only split `ReviewStore` uses: `create` / `revoke_token` / `revoke_session` take
  `_store_lock()`; `resolve` / `active_for_session` / `active_summary_by_session` keep the plain
  thread lock, because a read cannot corrupt the store and locking it would turn cross-worker
  contention into 503s on the HTTP read path.

That removes the concrete loss the finding named: two uvicorn workers can no longer interleave
`revoke_session`'s read-modify-write and leave live a link the owner was told was dead.

Pinned by `tests/test_share_store_cross_process.py` (12 tests): each mutator's guards are read out of
the AST, the reader split is asserted in both directions, the sibling's own split is re-derived from
`reviews.py` so a change to one is visibly a change to both, the per-path sharing is proved with two
instances, contention is proved to raise a 503, and the interprocess failure path is pinned
structurally (every handler in `_store_lock` raises, none yields, `required=True`, `blocking=False`)
because the same-interpreter contention test raises before that block is ever reached.

**Deliberately NOT done:** the recommendation's *shared capability-store core*. Extracting mint /
revoke / resolve across two security-relevant stores is a much larger change than the guarantee gap,
and a partial extraction that leaves one path on the weaker primitive is worse than the duplication.
The duplication remains; the divergence in guarantees does not. Remaining differences, for whoever
takes the extraction: `ReviewStore` also has `O_EXCL` id reservation, abandoned-reservation healing,
and a recovery/replay contract that `ShareStore` has no analogue for.

#### SC-11 · MEDIUM · inconsistency · effort: medium

**Event-log rewrite/race detection implemented six different ways across serve/**

*Locations:* `looplab/serve/command_observation.py:110-141`, `looplab/serve/log_pages.py:133-139`, `looplab/serve/routers/attention.py:96-134`, `looplab/serve/appstate.py:229-250`, `looplab/serve/appstate.py:390-420`, `looplab/serve/scope_sources.py:119-214`

*Evidence:* Six independent mechanisms detect 'the log was replaced/rewritten under me': blake2b sampled probe signatures (command_observation), mtime/ctime metadata + bounded prefix anchors (log_pages), before/after 5-tuple stat signatures with a retry loop (routers/attention), (ino,ctime,size,mtime,upto_seq,audience) cache keys (appstate.state_payload) and a different 5-tuple in trace_view._sig, and full inotify/FILE_BASIC_INFO ChangeTime capture (scope_sources). The signature tuples differ subtly (some include st_dev, some ctime_ns, some neither), each documents its own rationale, and a discovered weakness in one fence (e.g. the grow-after-rewrite hole command_observation patched at _refresh_locked) must be re-derived for each sibling independently.

*Recommendation:* Not full unification (the strength requirements genuinely differ), but a shared core file-identity toolkit: one canonical stat-signature function with named strength tiers (metadata / probe / descriptor-watch) so fixes to a tier propagate to every consumer.

*Resolution (2026-08-03):* The tier vocabulary lives beside the canonical tuple in
`core/atomicio.py`: `file_identity` (same file AND unchanged — every way the bytes could have been
swapped) and the new `same_file_entry` (replacement only — growth keeps the answer, a new inode
changes it). Not full unification, as the finding says: a site needing something between the two
declares it AGAINST these definitions.

Converted the sites that were spelling a weaker tuple for NO stated reason, and two were bugs, not
style:

* `serve/appstate.py`'s state-cache key omitted `st_dev`. Inode numbers are unique per DEVICE, so
  two runs whose logs shared an inode across filesystems could collide on one key — and what that
  key guards is a whole projected `RunState`.
* `serve/routers/attention.py`'s three hand-spelled 5-tuples were `file_identity` minus
  `st_file_attributes`, so on Windows a log that gained a reparse point compared EQUAL and the cached
  projection was served for a different file.

The two sites that already documented themselves as deliberate subsets (`log_pages._metadata`,
`command_observation._metadata`) were left alone — that is exactly the behaviour the convention
asks for. `scope_sources._file_identity` was a documented variant that never named what it varied
FROM; its docstring now does.

**The finding undercounted.** It says "six different ways"; an AST sweep for tuples built out of
`st_*` reads finds ~27 across the tree. Several are legitimately the weak replacement-only tier, so
converting them all needs per-site judgement rather than a sweep — but at least one carries the same
defect just fixed above: `tools/_runcache.py:63` spells `file_identity`'s fields reordered and
without `st_file_attributes`.

`tests/test_file_identity_tiers.py` therefore pins the two tiers as BEHAVIOUR (growth keeps
`same_file_entry`; a same-size in-place rewrite defeats it but not `file_identity`), pins both fixed
bugs, and turns the remainder into a LEDGER: the count of unconverted hand-rolled signatures cannot
grow without the test going red, and lowering it is the work. That is a bounded, visible backlog
instead of an unbounded claim of coverage. Teeth-verified against 5 breakages.

The guard is AST-based, not grep: a regex over stat field names flagged thirty INCIDENTAL reads (an
`st_size` check next to an `st_mtime` sort key), which is not a competing definition. The question is
whether a TUPLE is being built out of stat fields.

It also walks the tree through `tests/_source_scan.iter_sources()` rather than its own `rglob` — the
`test_no_guard_test_re_derives_the_walk` guard caught this file doing exactly that, for the SECOND
time in this campaign (CT-10's grep guard was the first). Two occurrences is a pattern, not a slip:
the reflex when writing a tree-wide guard is to reach for `rglob`, and the shared helper exists
because at least one tracked file carries a UTF-8 BOM that a fresh walk decodes differently.

#### SC-12 · LOW · duplication · effort: small

**Duplicated liveness/identity probe pairs and operator escape-hatch scaffolding inside run_commands**

*Locations:* `looplab/serve/run_commands.py:1515-1550`, `looplab/serve/run_commands.py:1552-1615`, `looplab/serve/run_commands.py:1617-1699`, `looplab/serve/run_commands.py:1926-1994`

*Evidence:* _claim_child_definitely_gone/_claim_child_exactly_alive (operating on a claim dict) and _execution_owner_definitely_gone/_execution_owner_exactly_alive (operating on a claim file) implement the same pid-state + identity-reuse decision twice. resolve_active_claims and resolve_spawn_claim repeat the same escape-hatch scaffold: exact confirmation phrase, minimum_age = max(5.0, startup_timeout*2+1) window, revalidate-owner-then-unlink, structured 409s.

*Recommendation:* One owner_liveness(row_or_path) pair taking a parsed claim dict (file loading as a thin adapter), and one guarded_claim_resolution(claims, phrase, revalidate) helper for both escape hatches.

*Resolution (2026-08-03) — the probe pair. The escape-hatch scaffold is NOT yet done.*

`_owner_definitely_gone(row)` and `_owner_exactly_alive(row, *, own_process_counts=False)` are the
one decision; the four old names are now thin carriers, with file loading (including the legacy
bare-PID line) as the adapter the recommendation asks for.

The decision was worth unifying because of what it gates: whether a SECOND worker may take over
durable command state. Two copies of that rule are two chances to answer "the owner is gone" about a
process that is merely suspended, and the asymmetry it encodes is easy to get subtly wrong in a
copy — "definitely gone" is the destructive direction and treats every ambiguity (unknown liveness,
unreadable pid, incomparable identity token) as NOT gone, while "exactly alive" treats the same
ambiguities as NOT alive.

`own_process_counts` is the single genuine difference and is now a named argument rather than a
second function: an EXECUTION claim written by this very server process counts as live even where
creation identity is unavailable, because its worker context may still be running; a SPAWN claim
gets no such fallback.

One behaviour TIGHTENED, in the fail-closed direction: the claim-dict path had no pid shape check and
handed `row.get("pid")` straight to `process_alive`. It now refuses a non-int/bool/non-positive pid
before probing, so a malformed claim can never read as "definitely gone".

Still open: the second half — `resolve_active_claims` / `resolve_spawn_claim` repeat the same
escape-hatch scaffold (confirmation phrase, `minimum_age` window, revalidate-then-unlink, structured
409s). That is a separate extraction and has not been done.

#### SC-13 · LOW · duplication · effort: small

**Five near-identical cmd_*.json directory scanners in RunCommandService**

*Locations:* `looplab/serve/run_commands.py:2154-2195`, `looplab/serve/run_commands.py:2197-2232`, `looplab/serve/run_commands.py:2283-2317`, `looplab/serve/run_commands.py:2319-2338`, `looplab/serve/run_commands.py:2340-2381`

*Evidence:* _active_command_ids, _unresolved_equivalent, _pending_finalize_record, _active_record, and _unresolved_terminal_record each glob directory.glob("cmd_*.json"), apply per-path symlink policy (raise vs fail-closed-active, subtly different per function), _load the record, filter by status sets, and pick min/max by created_at/updated_at. ~150 lines of parallel scan loops whose symlink handling has already diverged.

*Recommendation:* One _scan_command_records(rd, *, on_symlink) generator yielding (path, record); each caller keeps only its filter and selection lambda.

*Resolution (2026-08-03):* `RunCommandService._scan_command_records(rd, *, on_symlink)` is the one
walk; all five scanners keep only their filter and their min/max selection.

The finding's own observation — "whose symlink handling has already diverged" — is the reason this
mattered more than the line count. A symlinked `cmd_*.json` is an attempt to make one run's command
file point at another's, so a scanner that forgets the check reads a record it does not own and
answers a liveness question about the wrong run. The two surviving policies are now NAMED, because
they are opposites and each is right for its caller:

* `"refuse"` — the four scanners that answer a question about a SPECIFIC record raise 409, because a
  planted link means the answer cannot be trusted at all;
* `"unreadable"` — `_active_command_ids`, a fail-CLOSED liveness census feeding a destructive
  mutation's safety check. Refusing there would let a planted link BLOCK the check; counting the link
  as active TRIPS it, which is the safe direction. The record is not read.

A third policy survives deliberately and is pinned by a test: the cross-run stranded-restart sweep
SKIPS a linked record, because a 409 over one planted link would abort the sweep for every other run.

Covered by `tests/test_command_record_scan.py` (14), including both policies end to end and a guard
that the refusal message appears in exactly the two places that should own it (the walk, and `_path`
for a single named record).

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

*Resolution (2026-08-03):* All three. `deletion_service._storage_pending` is deleted and its five
call sites use `_pending` directly — it was `return _pending(...)`, a second name for one behaviour
used interchangeably with the first, so a reader had to check whether they differed.

`run_commands._spec` is gone and the registry is now `_CONTROL_POLICIES` (event type -> policy,
postcondition) with `CONTROL_SPECS` derived by stamping the KEY onto each spec. The event type is
therefore spelled once per entry rather than twice, which makes the mismatch structurally impossible
instead of merely unasserted. That mismatch would not have raised anywhere: it would have surfaced as
a control running under the WRONG engine policy — a NO_SPAWN intent waking a dead engine, or an
ENSURE_RUNNING command quietly never spawning one. The registry's existing
`set(CONTROL_SPECS) == set(CONTROL_EVENTS)` assertion is unchanged.

Covered by `tests/test_registry_and_alias_seams.py`. SC-12 and SC-13 (the liveness-probe pairs and
the five `cmd_*.json` scanners) are separate findings and remain open.


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

#### SR-03 · HIGH · under-decomposition · effort: medium — **RESOLVED (2026-08-02)**

**control.py trace-clear: ~640-line durable state machine as closures inside build_router**

*Locations:* `looplab/serve/routers/control.py:420-1062`, `looplab/serve/routers/control.py:820-1062`

*Evidence:* Seventeen nested helper closures (`_trace_clear_receipt_lstat` through `_apply_prepared_trace_clear`) plus the ~240-line `clear_node_trace` handler implement a complete write-ahead-receipt state machine (pending/succeeded/superseded, digest-CAS on spans.jsonl, recovery ownership) entirely inside `build_router`. The sibling destructive operations got dedicated modules — reset is one line delegating to `serve/reset_route.py::durable_reset_run` (control.py:413-418), deletion delegates to `deletion_service.py` (org.py:174-188) — so trace-clear is inconsistent with the codebase's own extraction pattern, and being closures it is untestable without building the whole app.

*Recommendation:* Extract to looplab/serve/trace_clear.py with the same shape as reset_route.py (`durable_clear_node_trace(srv, ...)`), leaving a one-line route.

*Resolution (2026-08-02):* `looplab/serve/trace_clear.py` owns the whole machine —
`durable_clear_node_trace(srv, run_id, nid, body, *, known_engine_liveness)` plus the seventeen
helpers as module functions. `routers/control.py` keeps the route docstring (it is the OpenAPI
description) and a two-line delegate, and drops ten now-unused imports; the file goes 1691 → 1064
lines. The only edits to the moved bodies are mechanical: `srv` threaded explicitly where it was
captured, `srv.run_dir` in place of the captured `_run_dir`, and `known_engine_liveness` passed in
so the router's fail-closed liveness verdict keeps ONE definition and ONE patch seam across its
four call sites.

Verified by a 45-case byte-level differential (validation ladder, every fence refusal, the receipt
path hazards, and each recovery terminal) run against a `git worktree` of the pre-extraction tree:
**byte-identical**. The harness has teeth — five deliberate breaks (complete-without-the-source-check,
drop the recovery re-confirm, resolve liveness locally instead of through the router helper, skip the
sibling-pending scan, alter the snapshot bytes) were each caught.

The point of the finding was not the line count but the sentence *"being closures it is untestable
without building the whole app"*, so the extraction ships `tests/test_trace_clear_service.py` — 39
tests that drive the state machine against a stub `srv` with no ASGI app, no engine, and no run.
That instrument reaches the states HTTP tests cannot construct: a write-ahead record whose process
died before the replacement, one that died after it, one whose trace has since moved to a third
state, and one whose recorded counts contradict a recomputation even though both digests still
match. Each pins the same property — an unconfirmed or unreconstructable outcome never authorizes
another deletion. Six independent breaks in the production module were each caught by exactly the
test that guards the property they broke.

#### SR-04 · MEDIUM · under-decomposition · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**runs.py concept-lens subsystem (~1000 lines) with a triplicated generation-fence preamble**

*Locations:* `looplab/serve/routers/runs.py:224-441`, `looplab/serve/routers/runs.py:1176-1387`, `looplab/serve/routers/runs.py:1389-1493`, `looplab/serve/routers/runs.py:1495-1626`, `looplab/serve/routers/runs.py:1628-1740`

*Evidence:* The paid concept-lens feature spans module helpers (two ledger folds, identity/HMAC helpers, `_validated_derived_lens`) plus four endpoints. `derive_concept_lens`, `recover_concept_lens_receipt`, `abandon_recovered_concept_lens`, and `abandon_concept_lens` each repeat the same ~40-line preamble: validate expected_generation regex → `_materialize_concept_core` → enter `srv.commands.sequence(rd)` → `validate_paths` → compare `run_generation` twice (current vs expected, core vs current) with three near-identical hand-built 409 dicts per endpoint. runs.py is 2845 lines overall; this subsystem is a third of it and is conceptually independent of the read-model routes the file is named for.

*Recommendation:* Extract a `serve/concept_lens.py` service plus one `_assert_lens_generation(rd, core, expected)` helper for the preamble; the four endpoints become thin.

*Resolution (2026-08-02, the preamble):* `_assert_lens_generation(srv, rd, *, core_generation,
expected_generation, stale_message, stale_remediation, prepared_message)` replaces the three
hand-built copies inside `recover_concept_lens_receipt`, `abandon_recovered_concept_lens` and
`abandon_concept_lens`. It returns the VALIDATED run dir alongside the generation, because
`validate_paths` may re-resolve the path and a sequenced section that kept using the pre-validation
one would be fencing one directory while writing to another. Only the prose differs per endpoint and
it is client-visible, so it is passed in rather than flattened.

The two checks are not redundant and the helper keeps both: the first says the CALLER is stale (a tab
acting on a generation the run has moved past), the second says the SERVER's own projection is (the
run moved between materializing the concept core and taking the sequencer). `derive_concept_lens`
deliberately does NOT use the helper — it is the paying path and reports a missing identity as its
own `run_generation_unavailable` code, because for a caller about to spend money "the run has no
identity" and "the run moved" are different problems with different fixes.

`tests/test_lens_generation_fence.py` adds 7 tests; four deliberate breaks — drop the prepared
check, allow an empty generation to match, discard the validated dir, read the generation before
validating the paths — were each caught. The empty-generation break is instructive: it is dead code
today because `expected_generation` is regex-validated as 64 hex upstream, and the test that gives
it teeth had to say so, pinning the fence's behaviour if that validation is ever relaxed rather than
pretending the clause was already load-bearing.

**Still open:** the ~1,000-line concept-lens subsystem still lives inside `routers/runs.py`; the
`serve/concept_lens.py` service extraction is a separate change.

#### SR-05 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**_json_object body parser copy-pasted 4x at module level plus ~10 inline re-implementations**

*Locations:* `looplab/serve/routers/assistant.py:46-56`, `looplab/serve/routers/genesis.py:28-37`, `looplab/serve/routers/boss.py:452-479`, `looplab/serve/routers/org.py:18-25`, `looplab/serve/routers/control.py:222-227`, `looplab/serve/routers/control.py:293-298`, `looplab/serve/routers/misc.py:448-453`, `looplab/serve/routers/boss.py:845-849`

*Evidence:* Identical `try: await request.json() except (ValueError, UnicodeDecodeError) -> 400; isinstance dict check -> 400` exists as four module-level `_json_object` copies (genesis and assistant carry a docstring saying it 'mirrors routers/boss + control'; boss's docstring mirrors routers/control, and org's nested copy has none) and is additionally re-inlined in control.py (control, submit_command, resolve_activity_claims, resolve_start_claim, start_run, start_preflight), boss.py (chat_log_append, report_refresh), misc.py (put_settings, put_secret), and runs.py (`_concept_lens_json_body` adds only a byte cap). The comments themselves acknowledge the mirroring instead of sharing the function.

*Recommendation:* One `json_object(request, *, max_bytes=None)` helper in serve/protocol.py (or a new serve/http.py); the boss variant's extra field checks stay local.

*Resolution (2026-08-02):* `looplab/serve/http.py` owns it —
`json_object(request, subject="request body", *, absent_is_empty=False)` plus a
`json_object_bytes` sibling for the routes that must read the body themselves. Sixteen call sites
across assistant/genesis/boss/org/misc/runs/control and `reset_route` now go through it; the four
module-level copies (three of which carried a docstring naming which OTHER router they mirrored) are
gone, and 138 lines went with them. It landed in a new module rather than `serve/protocol.py`
because protocol.py has no third-party imports today and the TUI client depends on that.

Two things legitimately differ per route, so they are arguments rather than variants: the **subject
noun** in the message (``"control body"``, ``"settings payload"``, …), which is client-visible
contract; and whether an **absent** body means ``{}`` or a 400. Everything else collapsed, including
one thing that had already drifted — `routers/misc.py`'s settings and secret writers caught
`Exception` ("malformed JSON is a client error, never a server traceback") while the other twelve
caught only `(ValueError, UnicodeDecodeError)`. The broad catch is right and is now the parser's
rule, so the next copy cannot be narrower by accident. `CancelledError` is a `BaseException` and
still propagates.

Two sites deliberately did NOT fold and are named in the guard test: `/api/start` and its preflight
answer with a structured ``{"code": "invalid_launch_request", "field_errors": {}}`` body the launch
client parses by shape, and the author-file PUT, which is raw UTF-8 text and never JSON. The
concept-lens route keeps its own UTF-8 decode beside its byte cap — both are that route's reading
policy, and `json.loads` would otherwise have accepted BOM-marked UTF-16/32 that endpoint never did.

Nothing in the suite asserted any of these 400 strings, which is exactly what makes collapsing
copies risky, so `tests/test_serve_json_body.py` pins the parser's decision table (18 tests) and
adds a grep-level guard that fails if a router starts reading `request.json()`/`request.body()`
itself again. Five deliberate breaks — drop the object check, ignore `absent_is_empty`, hardcode the
subject, narrow the catch, forgive emptiness everywhere — were each caught.

One regression fell out on the way, and the existing guard caught it: the first version imported
`HTTPException` at module scope, which `serve/server.py` pulls in through its router re-exports —
so `make_app` stopped answering "pip install looplab[ui]" and raised an ImportError traceback
instead. `test_event_types.py::test_server_reexports_and_cli_stay_friendly_without_ui_extra` failed
on it. The 400 is now built in a `_bad_request` helper that imports fastapi lazily on the failure
path, the same reason `serve/engine_proc.py` imports it inside its functions.

#### SR-06 · MEDIUM · duplication · effort: medium

**Five implementations of bounded/redacted projection of untrusted JSON**

*Locations:* `looplab/serve/routers/genesis.py:45-86`, `looplab/serve/routers/misc.py:241-285`, `looplab/serve/routers/reviews.py:112-145`, `looplab/serve/routers/assistant.py:149-172`, `looplab/serve/routers/assistant.py:72-113`

*Evidence:* genesis.py `_bounded_evidence_value` and misc.py `_bounded_json_value` are near-identical ~40-line recursive walkers (shared budget list, depth cap, 32-item fanout, sorted keys, string cap + truncation flag) differing only in constants (budget 128 vs 96, depth 3 vs 2) and misc's secret-key masking; reviews.py `_scrub_json` is a third recursive scrubber (key-aware masking, collision suffixes, depth 40); assistant.py `_public_scope` and `_shared_message` are two more allow-list/redact projectors. The misc.py comment even names further siblings: `core/advisory_payloads._tree` and trust/cross_run's walk. Each copy independently re-derives the same redact-before-truncate and secret-key rules, so a fix in one (e.g. the 8d1bcda secret-key-classification fix noted in misc.py) does not propagate.

*Recommendation:* One configurable bounded-projection walker in core/redact.py (budget, depth, fanout, secret-key policy, truncation receipts as parameters); migrate the two near-identical copies first.

*Resolution (2026-08-04) — the walker the finding asks for ALREADY existed; the two copies just were not on it.*

`core/redact.py::bounded_redacted_tree` was built for CO-06 and is used by `core/tracing.py` and
`core/advisory_payloads.py`. It already carries the stricter union of those two prior copies. What it
lacked was the one thing the serve routers needed — a truncation receipt — so both had kept a walker
of their own. It now takes an optional `truncated` out-cell, and `genesis._bounded_evidence_value`
and `misc._bounded_json_value` are thin wrappers that keep only their own CONSTANTS (nodes 128/96,
depth 3/2, fanout 32, string cap 500, key cap 80).

**The copies had already drifted, on exactly the rule that matters at a redaction boundary.** Given
`{"api_key": "tok-abcd"}`, genesis DROPPED the key and reported `truncated=True`; misc MASKED it as
`"***"` and reported `truncated=False`. Same payload, two endpoints, two answers — and the dropping
one told the operator nothing about why a field had vanished. Masking wins: "this field exists and is
a secret" is strictly more useful than a silently absent key, it is what every other projector in the
codebase already did, and it is not truncation because nothing was dropped for SIZE.

Two further behaviours the routers gain by sharing:

* **A hostile mapping degrades instead of raising.** Both called `.items()` unguarded, so a `dict`
  subclass whose iteration throws took the response down as a 500. The shared walker answers
  `<mapping unavailable>`.
* **A key that redacts to a colliding or empty name is dropped AND reported**, rather than silently
  overwriting the earlier member.

The character budget is DERIVED (`nodes x string cap`) rather than chosen, so it stays non-binding
and the original node-only bound is preserved exactly.

**Not migrated, deliberately.** `reviews._scrub_json` is a different job: an UNBOUNDED key-aware
scrubber with deterministic collision suffixes, whose contract is "copy everything, mask secrets" —
bounding it would silently truncate a reviewer's evidence. `assistant._public_scope` and
`_shared_message` are allow-list projectors, not recursive walkers. The finding's own advice was to
migrate the two near-identical copies first, and those are the two.

`tests/test_bounded_redacted_tree.py` 38 → 45: the receipt reports each omission/shortening kind
(parametrized), masking a secret is NOT a cut, an exhausted budget is reported, neither router walks
itself any more, both agree on a credential-named key, and a hostile mapping no longer reaches either
endpoint as a 500. Teeth-tested against 7 breaks, all biting.

#### SR-07 · MEDIUM · mergeable-entities · effort: small — **RESOLVED (2026-08-02)**

**cross_run.py: five concept-governance POSTs and two steward POSTs are the same endpoint modulo one function**

*Locations:* `looplab/serve/routers/cross_run.py:636-735`, `looplab/serve/routers/cross_run.py:823-877`, `looplab/serve/routers/cross_run.py:737-739`

*Evidence:* `concept_merge`, `concept_purge`, `concept_alias_clear`, `concept_split`, `concept_split_clear` each repeat verbatim: `memory_dir, portfolio_id = _portfolio(body.expected_portfolio_id)` → try record/clear helper with the same by/at/expected_revision/expected_governance_revision/action_id/require_existing kwargs → `except Exception: _raise_governance_error(exc)` → identical 5-key response envelope (~18 lines x5). `concept_steward` and `claim_steward` are likewise identical except for the revision-probe and steward function. Separately, `_iter_log` (line 737) is a wrapper whose body is exactly `yield from _read_curation_rows(path)` — pure indirection.

*Recommendation:* A `_governed_mutation(body, fn, result_key)` helper collapses the five endpoints to one-liners; parameterize the steward pair; delete `_iter_log`.

*Resolution (2026-08-02):* all three. `_governed_mutation(body, record, result_key)` owns the
portfolio resolve, the `_raise_governance_error` funnel and the five-key envelope; merge / purge /
alias-clear / split / split-clear each shrink to the registry call that is genuinely theirs.
`_run_steward(kind, ..., probe=, steward=)` parameterizes the pair. `_iter_log` — a wrapper whose
body was `yield from _read_curation_rows(path)` — is deleted and its one caller reads directly.

The envelope is the part worth sharing rather than the line count: `revision` and
`governance_revision` are what a client CASes on next, so a copy that dropped one would break the
NEXT fenced write, at a call site with no visible connection to the endpoint that dropped it.

Five deliberate breaks were tried and four were caught by the existing suite. The fifth was not, and
is the one the driver exists for: moving the health probe AFTER the durable paid-call claim left
every assertion green. A steward handed a guessed taxonomy returns confident proposals about a
portfolio that does not exist, and the operator has already paid for them — so
`test_an_unhealthy_projection_refuses_the_steward_before_any_paid_call` now pins the ordering by
asserting the invocation is never reached, not merely that the response is an error.

#### SR-08 · MEDIUM · other · effort: medium — **RESOLVED (2026-08-02)**

**Twelve unresolved async-handler-blocks-event-loop defects, flagged in-code but unfixed**

*Locations:* `looplab/serve/routers/org.py:28-36`, `looplab/serve/routers/misc.py:466-471`, `looplab/serve/routers/boss.py:544-549`, `looplab/serve/routers/boss.py:598-603`, `looplab/serve/routers/boss.py:860-865`, `looplab/serve/routers/genesis.py:199-205`, `looplab/serve/routers/control.py:1335-1341`, `looplab/serve/routers/runs.py:1229-1235`, `looplab/serve/routers/runs.py:2780-2784`, `looplab/serve/routers/reports.py:2584-2590`, `looplab/serve/routers/assistant.py:853-858`, `looplab/serve/routers/runs.py:2112-2117`

*Evidence:* Twelve 'CLAUDE REVIEW: [PERF]' comments in the routers document `async def` handlers doing blocking work directly on the ASGI event loop: unbounded `fcntl.flock` in org project mutators and misc put_settings/put_secret; full event-log fold + fsync in boss chat/chat_log_append/report_refresh; command-sequencer + full `read_all()` in runs derive_concept_lens and start_run; global store lock + lease I/O in reports generate preflight; plus the span_io unbounded-file-scan DoS note (runs.py:2112). The correct fix pattern (anyio.to_thread.run_sync around the sequenced section) already exists in the same files (/control at control.py:229-276, submit_command, chat_compact), so this is inconsistent application of an established remedy, with every SSE tick on the worker stalling under contention.

*Recommendation:* Apply the existing to_thread offload pattern to the flagged handlers (a mechanical change per site), and bound or reject unindexed sids in span_io. Then delete the markers.

*Status (post-baseline):* Fixed on `master` by commit `c92b89f` (2026-08-01, immediately after this review's baseline): all flagged handlers now offload their blocking sections via `anyio.to_thread.run_sync` (the assistant SSE drain was inverted to a no-pool-hop loop drain), the span_io fallback scan is bounded to the index's coverage boundary, and every `CLAUDE REVIEW: [PERF]` marker was removed. Behavioural tests pin the fix. The finding is retained as accurate at the baseline.

#### SR-09 · MEDIUM · duplication · effort: small — **PARTIALLY RESOLVED (2026-08-02)**

**Generation-fence 409 envelopes hand-built ~26 times; comment-cursor error duplicated between reviews and collaboration**

*Locations:* `looplab/serve/routers/runs.py:1214-1221`, `looplab/serve/routers/runs.py:2696-2702`, `looplab/serve/routers/boss.py:876-882`, `looplab/serve/routers/control.py:916-922`, `looplab/serve/routers/reviews.py:410-429`, `looplab/serve/routers/collaboration.py:13-18`

*Evidence:* The `{"code": "run_generation_changed", "expected_generation": ..., "current_generation": ..., "message": ..., "remediation": ...}` 409 dict is hand-assembled 26 times across 9 serve files (11 in runs.py alone), each with slightly different prose — drift between copies is already visible (some include `or None` on current, some omit remediation). Similarly, collaboration.py has a `_cursor_error(exc)` helper for CommentCursorError, but reviews.py `review_comments` (lines 424-429) re-inlines the identical dict instead of importing it, and the `comment_filter_invalid` 400 dict is copied verbatim between collaboration.py:75-79 and reviews.py:411-415.

*Recommendation:* Add `generation_conflict(expected, current, *, message, remediation)` and share `_cursor_error` from one place (serve/protocol.py). This also stabilizes the wire contract the UI matches on.

*Resolution (2026-08-02, the comment half):* `serve/http.py` now owns `comment_cursor_error(exc)`
and `comment_filter_invalid()`, shared by `routers/collaboration.py` and `routers/reviews.py`. The
reviewer surface was re-inlining both envelopes rather than importing the helper that already
existed next door, which is the shape of divergence that matters here: a reviewer able to filter or
paginate more loosely than the owner surfaces comments the owner's own view excludes.

The cursor split is contract, not cosmetics — 400 says the cursor was never valid, 409 says it was
valid for a run state that has since moved, and only the second is worth re-fetching page one for.

**Still open:** the `generation_conflict` sweep over the ~26 hand-built `run_generation_changed`
409s. Three of them were already collapsed by SR-04's `_assert_lens_generation`.

#### SR-10 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**Attempt-fenced node-metrics read copy-pasted between owner and reviewer routes**

*Locations:* `looplab/serve/routers/runs.py:2039-2050`, `looplab/serve/routers/reviews.py:474-486`

*Evidence:* The three-way receipt decision — `receipt is None -> read only if attempt==0; receipt[0]==current_attempt -> read_node_metrics(since_wall_time=receipt[1]); else -> {}` — is duplicated line-for-line between `node_metrics` (runs.py) and `review_node_metrics` (reviews.py). The reviews.py comment explicitly says 'Fence on the attempt receipt exactly as the owner route (runs.py node_metrics) does', i.e. the invariant is maintained by comment discipline rather than shared code; a future receipt-format change must be fixed twice or the two surfaces silently diverge on which attempt's evidence they serve.

*Recommendation:* Extract `fenced_node_metrics(node_dir, current_attempt) -> dict` into serve/metrics_adapters.py (or core/node_evidence.py next to `metrics_attempt_receipt`); the deliberate difference (owner 409s on concurrent reset, reviewer returns empty) stays in the routes.

*Resolution (2026-08-02):* `serve/metrics_adapters.py::fenced_node_metrics(node_dir,
current_attempt)` owns the three-way receipt decision; both routes call it and keep only what
genuinely differs — the owner 409s on a concurrent reset, the reviewer returns an empty series
because a read-only observer has no way to resolve an error. It lives in `serve/` rather than beside
`metrics_attempt_receipt` in `core/` because it needs `read_node_metrics`, and `core` may not import
`serve`.

`tests/test_shared_serve_projections.py` adds 11 tests. Six deliberate breaks were each caught; two
are worth naming. Dropping the `since_wall_time` window still returns a plausible non-empty series —
just the PREVIOUS attempt's curve under the current attempt's label — and swapping the receipt tuple
`(attempt, started_at)` passes a plausible integer as a wall-time, which silently empties the window
for every live node. Neither raises.

The extraction also retired a patch seam, exactly as the CLAUDE.md contract note warns: three tests
in `test_review_capabilities.py` patched `reviews_router.read_node_metrics`, which no longer exists
there. They are re-pointed at `serve/metrics_adapters.read_node_metrics` and RE-VERIFIED rather than
just made green — the attempt-fence test still fails when the fence is broken (two of the three
breaks; the third, "read unwindowed for any attempt", is caught by the new tests instead, because
that test's no-receipt case only exercises attempt zero).

#### SR-11 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**Agentic emit-loop scaffolding duplicated between genesis and boss command router**

*Locations:* `looplab/serve/routers/genesis.py:334-378`, `looplab/serve/routers/boss.py:726-759`

*Evidence:* Both `_plan_agentic` (genesis) and `_route_with_tools` (boss) hand-build the same scaffolding around `drive_tool_loop`: an `emit_spec = {"type": "function", "function": {"name": "emit", ..., "parameters": Model.model_json_schema()}}` dict, a `box: dict = {}` result cell, a `_fin(args)` that filters kwargs to `Model.model_fields` and falls back to an empty model on junk, a fallback closure, and `max_turns=getattr(s, 'agent_max_turns', 0), time_budget_s=..., **loop_opts_from_settings(s)`. Only the pydantic model (_GenesisSpec vs _Plan), tools, and prompt text differ. Prompt strings are contracts (per CLAUDE.md) and must stay verbatim, but the mechanical scaffolding is pure duplication.

*Recommendation:* Add an `emit_loop(client, tools, messages, model_cls, settings, *, fallback, on_step)` helper in looplab/agents (next to drive_tool_loop) that owns emit_spec/box/_fin; both routers pass their exact prompts through unchanged.

*Resolution (2026-08-02):* `agents/tool_loop.py::emit_loop(...)` owns the emit spec, the result
cell, the finalizer and the settings-driven limits; it is re-exported through `agents/agent.py` like
every other loop seam. Both routers pass their prompts through verbatim — those are contracts and
none of their bytes moved.

The degradation behaviour is the part worth having once. These loops end by rendering a card to a
human, so a junk emit must yield a USABLE empty model rather than an exception that loses the whole
turn including everything the model just read; and unknown keys are filtered by the HELPER rather
than left to each caller's model config, so a hallucinated field cannot reach a model that permits
extras. The fallback now takes `(messages, emitted)` and its answer becomes the result, which is
what let the genesis planner stop writing into a mutable cell of its own.

`tests/test_emit_loop.py` adds 17 tests. Five deliberate breaks were each caught, and the first two
attempts at them were SILENT — which is the useful part of the record. Dropping the unknown-key
filter is invisible against a model that ignores extras by default, and narrowing the junk guard to
`ValueError` is invisible until the model emits a non-mapping (a bare scalar or a list makes
`.items()` raise `AttributeError`). Both tests were strengthened to the cases that actually
distinguish, rather than left as green assertions that proved nothing.

The helper resolves `drive_tool_loop` through `agents/agent.py` at CALL time rather than binding
`tool_loop`'s own global, and the fifth break is exactly that line. `agent.drive_tool_loop` is THE
documented monkeypatch seam (CLAUDE.md; `tests/test_prompt_injection_rule.py` asserts it by name),
and the first version of this helper quietly retired it for the two surfaces it serves — which
surfaced as `test_genesis_prior_reports_are_redacted_untrusted_user_json` failing, i.e. the test
that checks an untrusted prior report cannot reach a system prompt. A refactor that silently
disconnects the injection tests from the loops they guard is worse than the duplication it removes.

#### SR-12 · MEDIUM · layering · effort: medium

**Router-to-router imports and side-effect late-binding seams couple the route modules**

*Locations:* `looplab/serve/routers/genesis.py:24-25`, `looplab/serve/routers/misc.py:591`, `looplab/serve/routers/runs.py:890`, `looplab/serve/routers/runs.py:925`, `looplab/serve/routers/reports.py:1582`, `looplab/serve/routers/reports.py:1410-1553`

*Evidence:* genesis.py imports `_defaults_backend_llm` from routers/control.py and `_prior_learnings_index` from routers/reports.py — private helpers of sibling routers, so router modules are no longer independent leaves. The other direction is handled by mutating srv inside build_router: misc.py sets `srv.list_tasks_fn = list_tasks`, runs.py sets `srv.list_runs_membership_fn` and `srv.list_runs_fn`, which reports.py's `_scope_run_ids` then reads (`srv.list_runs_membership_fn or srv.list_runs_fn`). These attributes exist only after the right build_router calls run, forming an implicit protocol on AppState that no type or registry guards. Both `_defaults_backend_llm` (a launch-policy predicate) and `_prior_learnings_index` (a prompt projection) are domain logic with no HTTP dependency living inside routers only for historical reasons.

*Recommendation:* Move `_defaults_backend_llm` to serve/launch.py, `_prior_learnings_index` to serve/scope_report.py, and make the run-summary/membership/tasks projections real AppState methods instead of build_router side effects.

*Resolution (2026-08-04) — one of the two router-to-router edges is closed; the other two items are
measured and deferred with reasons.*

**Done:** `_defaults_backend_llm` now lives in `serve/launch.py`, as recommended. No re-export in
`routers/control.py` — that router does not call it, so a shim there would only re-create the
coupling in the other direction. `tests/test_serve_module_seams.py` asserts that NO router imports a
sibling router, with the one remaining edge named explicitly in the assertion so removing it is a
one-line change to the expected list rather than a new test.

The move surfaced a stale comment worth recording: the docstring claimed the predicate was "shared by
/api/start (authoritative — the one funnel every launch goes through) and the genesis card", and
`routers/genesis.py` claimed that "delegating to the SAME shared predicate means the card can never
disagree with what /api/start actually spawns". Neither is true as written — /api/start stopped
calling it and applies the rule itself in `launch.py::_resolve_settings` over its own already-layered
settings. The two spellings still AGREE (both defer to `engine/genesis.py::default_backend` and to
the same `model_fields_set` "chosen" test), so the guarantee holds; the comments now say why rather
than asserting a call graph that no longer exists, and both spellings sit in one file where a reader
can check them against each other. `cli/run_cmds.py`'s pointer at the old location is updated, and a
guard test fails on any surviving `routers.control._defaults_backend_llm` reference.

**`_prior_learnings_index` — done (2026-08-04), by extracting the store it depends on.** It reads
eleven private helpers and three constants of the scope-report STORE, so it could never move alone.
That store is now `serve/scope_report_store.py` — 103 names, ~1 400 lines that used to sit ahead of
`build_router` in `routers/reports.py` and contain no HTTP at all. `routers/genesis.py` imports the
STORE; `routers/reports.py` star-imports it so `reports.<name>` keeps resolving for its own
`build_router` and for the tests that spell it that way. **No router imports another router any
more**, and the guard test's expected list is now empty. Its home is not `serve/scope_report.py`,
which documents itself as free of run-root/store details so it stays unit-testable with plain dicts.

The monkeypatch hazard was real and was handled head-on rather than hoped away. A star import BINDS
BY VALUE, so patching `reports.<name>` no longer reaches the store's own global lookup — and the
first run after the move failed exactly there: ten receipt/fence-recovery tests went green-to-red
because their injected write failure stopped arriving. Two of the four seams
(`strict_atomic_write_text`, `_read_scope_action_lease_marker`) are genuinely read from BOTH modules
— the store writes receipts and fences, the router writes the report record — so
`tests/test_report.py::_patch_store` patches the name wherever it exists, and a guard test asserts
that both modules really do call `strict_atomic_write_text`, so the helper cannot silently degrade
into patching one. All 16 sites were re-pointed and re-verified.

Pinned by `tests/test_serve_module_seams.py` (33): no router imports a router (empty list), genesis
and reports resolving to the SAME `_prior_learnings_index` object, the store holding no `APIRouter`
or route decorator, `__all__` matching what the module actually defines (a name added without
declaring it stops resolving through the star import and takes its tests with it), and the
both-modules write seam. Teeth-tested against 6 breaks, all biting.

**The `srv.list_*_fn` late-binding — done (2026-08-04) for the two side-effect-free projections.**
`run_summaries` and `run_membership` are `serve/run_projections.py`, exposed as real
`AppState.run_summaries()` / `AppState.run_membership()` methods; `routers/reports.py` calls the
method instead of reading `srv.list_runs_membership_fn or srv.list_runs_fn` back off the bag, and the
attribute is gone from `AppState` entirely so there is only one way to reach the projection. The
per-run fold cache stays on `AppState.summary_cache`, where the reset/delete paths already invalidate
it — a cache local to the new module would keep serving generation A's summary after a reset replaced
the log. `run_projections.py` imports no router, so the graph stays acyclic.

`srv.list_runs_fn` and `srv.list_tasks_fn` REMAIN attributes, deliberately: unlike the two above,
those are the route bodies themselves — `list_runs` overlays live engine-liveness facts (a lock probe
with a best-effort resume re-spawn, which is exactly why the membership projection was split out of
it in the first place), and `list_tasks` reads the on-disk catalogue relative to the repo. Promoting a
route body to an AppState method would move HTTP concerns into the state bag, which is the opposite
of what this finding asks for.

#### SR-13 · LOW · dead-code · effort: small — **RESOLVED (2026-08-02)**

**Orphaned endpoints and unused helpers**

*Locations:* `looplab/serve/routers/reports.py:418-419`, `looplab/serve/routers/genesis.py:94-133`, `looplab/serve/routers/runs.py:2825-2843`, `looplab/serve/routers/cross_run.py:56-59`

*Evidence:* `_scope_action_lease_marker_exists` (reports.py:418) has zero callers anywhere in looplab/ or tests/ (verified by repo-wide grep). `POST /api/research` (genesis.py) is referenced by no ui/src file and no TUI code — only tests exercise it; its function (LLM topic brief) is subsumed by the assistant and /api/genesis. `GET /api/runs/{run_id}/agents_md` (runs.py) likewise has no ui/src or TUI caller (grep for 'agents_md'/'AGENTS' in ui/src returns nothing) — only tests/test_server.py. `_portfolio_identity` (cross_run.py) is a compat wrapper used only by tests/test_cross_run_server.py, which could call `_resolved_portfolio_identity` directly.

*Recommendation:* Delete `_scope_action_lease_marker_exists` now. For /api/research and /agents_md, confirm no external API consumers, then remove or mark deprecated; fold `_portfolio_identity` into its test callers.

*Resolution (2026-08-02):* `_scope_action_lease_marker_exists` is already gone from `master` (a
repo-wide grep finds no definition and no caller). `_portfolio_identity` — a two-field compat
wrapper whose only callers were two lines of `tests/test_cross_run_server.py` — is deleted and those
tests now call `_resolved_portfolio_identity` directly.

`POST /api/research` and `GET /api/runs/{id}/agents_md` are marked `deprecated=True` with the reason
in their docstrings, NOT deleted. A repo-wide grep confirms neither appears in `ui/src` or the TUI,
which is the whole of what this repository can see — they are public HTTP routes, and the OpenAPI
deprecation flag is precisely the mechanism for giving a caller this repository cannot enumerate
notice before removal. Deleting them on the strength of a first-party grep would be treating
"I cannot see a consumer" as "there is no consumer".

#### SR-14 · LOW · duplication · effort: small — **RESOLVED (2026-08-02)**

**Dual-schema OpenAPI compatibility pattern duplicated between misc and runs config routes**

*Locations:* `looplab/serve/routers/misc.py:106-166`, `looplab/serve/routers/runs.py:141-209`

*Evidence:* misc.py defines `SettingsUpdateRequest` + `LegacySettingsUpdateRequest` (extra='allow', json_schema_extra={'not': {'required': ['settings']}}) + `_request_body_contract(*models)` producing an anyOf requestBody; runs.py re-implements the exact same trio as `RunConfigUpdateRequest` + `LegacyRunConfigUpdateRequest` + `_run_config_request_body_contract()`, including the same comment about preserving 400-vs-422 semantics. Both then re-inline the same expected_revision parse (misc `_expected_revision` vs runs' inline block at 2752-2759).

*Recommendation:* Share `_request_body_contract` and a `legacy_envelope(settings_model)` factory from one module; keep the differing revision regexes as parameters.

*Resolution (2026-08-02):* `serve/http.py::request_body_contract(*models)` is now the single
builder, used by `PUT /api/settings`, `PUT /api/secrets` and `PUT /api/runs/{id}/config`;
`runs.py`'s second copy is deleted. It sits beside `json_object` deliberately — these are the two
halves of one decision. The route publishes strict schemas through `openapi_extra` and keeps
parsing the body itself, because declaring the union as a Pydantic body parameter would turn the
established malformed-JSON **400** into FastAPI's **422**, silently changing a contract clients
already handle. That reasoning was written out twice, once per copy, and is now stated once.

Three deliberate breaks — always emit `anyOf`, never emit it, mark the body optional — were each
caught. The `anyOf` shape is what makes the legacy bare-mapping body DISCOVERABLE rather than
merely tolerated: the handler accepts it either way, so publishing it is the difference between a
documented contract and something a client finds by guessing.

The `legacy_envelope` factory half is NOT done: the two legacy models differ in more than their
revision regex (`misc` excludes the reserved `settings` key through `json_schema_extra`), and
folding them would have meant inventing a parameterization neither caller asked for.

#### SR-15 · LOW · duplication · effort: small — **PARTIALLY RESOLVED (2026-08-02)**

**Boss LLM endpoints repeat an identical prologue/epilogue quartet**

*Locations:* `looplab/serve/routers/boss.py:557-586`, `looplab/serve/routers/boss.py:588-637`, `looplab/serve/routers/boss.py:639-684`, `looplab/serve/routers/boss.py:686-833`

*Evidence:* chat_compact, chat, suggest, and command all open with `rd = _run_dir(run_id); generation = srv.commands.run_generation(rd); body = await _json_object(request); msgs = body.get('messages') or []` and all close with the same 10-line epilogue: `except HTTPException as exc: sanitized = _sanitized_domain_http_exception(exc); if sanitized: raise sanitized; return JSONResponse({'ok': False, **_safe_boss_failure(exc)}, 200)` + the same bare-Exception soft-fail. Four copies of the error-shaping block in one file; assistant.py solved the same problem for its two turn endpoints by extracting `_begin_turn`/`_finish_turn` (documented in its module docstring), so the codebase already endorses the fix.

*Recommendation:* A `_boss_llm_call(rd, generation, fn)` wrapper (or decorator) owning the metered-client context and the two-except epilogue; each endpoint keeps only its prompt assembly.


*Status (post-baseline):* Partially addressed on `master` by commit `c92b89f`: chat/suggest/command now share an extracted `_boss_prologue` helper (run_generation fetch + fold + prompt assembly, off-thread), so the remaining duplication is chiefly the four-copy error-shaping epilogue.

*Resolution (2026-08-03):* the epilogue is now `_boss_failure_response(exc)`, and the three
request-path endpoints each close with two lines instead of ten. The two `except` arms collapsed into
one, since both did the same thing once the shaping moved out.

The extraction is worth having because neither half is visible from a call site. A DOMAIN
HTTPException (a run-generation conflict) is re-RAISED sanitized — it is the client's to act on,
since the run moved under the request, and swallowing it into a 200 would tell the UI the turn merely
failed and invite a retry against the same stale generation. Everything else soft-fails as 200
`ok:false`, because "no model configured" and "endpoint unreachable" are the ordinary offline case
for these routes, with `_safe_boss_failure` keeping the provider's URLs and credentials out of the
body.

The finding counts FOUR copies; only three were the same thing. `command` computes on a background
job thread and returns a plain dict, so it cannot raise into a client at all — its conflict has to
come back as a DATA row the UI renders. `_background_http_failure` is its named counterpart, and
folding the two would either lose the conflict there or make the request path swallow it. The test
pins both behaviours and the fact that they are deliberately separate.

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

#### SE-03 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**ASHA survivor-retirement logic duplicated between policy and card_selection, with no fidelity guard covering ASHA**

*Locations:* `looplab/search/policy.py:562-577`, `looplab/search/card_selection.py:856-868`

*Evidence:* The failed_children counting, has_live_child set, and retired = {pid for pid,c in failed_children.items() if c >= _ASHA_MAX_FAILED_PROMOTIONS and pid not in has_child} computation is copy-pasted between ASHAPolicy.next_actions and card_selection._asha_lane (the constant is shared, the algorithm is not). A semantics change to retirement in one silently desyncs the Card lane from the policy authority. Unlike the GreedyTree/Card parity, which the 15-case scorer_fidelity_gate checks continuously, SCORER_FIDELITY_CASE_NAMES (scorer_fidelity.py:33-49) contains only GreedyTree cases — ASHA lane parity has no runtime guard.

*Recommendation:* Extract a shared asha_retired_survivors(state) helper in policy.py and call it from both sites; consider adding an ASHA case to the fidelity matrix or a source-scan test.

*Resolution (2026-08-02):* extracted as `search/policy.py::asha_expansion(state) -> (has_live_child,
retired)`, called by both `ASHAPolicy.next_actions` and `card_selection._asha_lane`. It returns BOTH
sets rather than just the retired one, because the Card lane uses `has_live_child` independently for
its `legal_survivors` filter — returning only half would have left the other half duplicated.

Took the source-scan option rather than the fidelity-matrix one: adding an ASHA case to
`SCORER_FIDELITY_CASE_NAMES` changes a runtime gate's case count and schema, which is a bigger
behavioural change than this finding warrants. `tests/test_asha_expansion_parity.py` (12) instead
pins the semantics directly — live-vs-failed, the retry cap, a live child rescuing a capped survivor,
per-parent counting, and a failed MERGE counting against every parent it names — plus a scan
asserting `_ASHA_MAX_FAILED_PROMOTIONS` is read nowhere in `search/` outside the helper. One test
pins the finding's own premise (the matrix is still GreedyTree-only), so if an ASHA case is ever
added, that test is the reminder the scan can be relaxed.

Teeth-tested by re-inlining the Card lane's copy, by collapsing the live/failed distinction so a
PENDING child counts as a failure, and by dropping the multi-parent fan-out so a failed merge is
charged to only its first parent.

#### SE-04 · MEDIUM · excessive-logic · effort: small

**card_score rebuilds the full concept projection per candidate; the code's own review comment says to hoist it but it never was**

*Locations:* `looplab/search/card_selection.py:764-770`, `looplab/search/card_selection.py:623-634`, `looplab/search/card_selection.py:1502-1512`, `looplab/search/card_selection.py:1532-1538`

*Evidence:* card_score calls _coverage_inputs(state) — which runs current_concept_projection over every node/membership/receipt — once per candidate Card, making one election O(cards × nodes·concepts). The inline comment at :765-768 states exactly this and prescribes the fix ('Compute explored+rename once per selection snapshot and pass that immutable scoring context through every candidate score') yet the code was left unhoisted. A smaller double-work case exists on the forced-SEED path, where _forced_card_actions itself calls eligible_cards (:599) before the forced-branch call (:1505); the forced (:1504) and candidates (:1533) calls sit on mutually exclusive control paths and never both execute in one election.

*Recommendation:* Compute (explored, rename) once in _selection_after_forced_gates and thread it into card_score via a scoring-context parameter; compute eligible_cards once per _speculative_selection invocation.

*Resolution (2026-08-02, hoist half):* the per-candidate rebuild is gone. The election computes
`_coverage_inputs(state)` ONCE and threads it to `card_score` through an optional
`coverage_inputs=` keyword.

The keyword is optional, and the dispatch never forwards it to an EXTERNAL policy hook, because
`card_score(state, card, *, scoring=...)` is the published scoring-hook signature: a third-party
policy would raise `TypeError` on an unknown keyword, `_score_for_policy` swallows exceptions, and
the lane would then silently fall back to legacy actions with that policy's scoring never consulted
again. Nothing would go red. Only the built-in scorer, which we own, receives the snapshot.

The projection is an immutable view of a `state` that does not change while the lane is scored, so
recomputing it per candidate was not merely slow — every candidate was paying to derive an answer
identical by construction.

`tests/test_card_scoring_snapshot.py` (10) pins that it is built once at several lane sizes, that
scoring with the shared snapshot equals scoring without it for every card, that the FULL ordering is
unchanged (a re-rank below the cut is still a behaviour change), that the two-argument public hook
still works, and that the extra keyword never reaches an external hook.

The first "built once" test was SILENT against the undone hoist: its fixture had a seed prefix still
due, so `card_selection_set` returned before scoring anything and the counter only ever saw the
election's own call. The fixture now asserts the election actually selected something, and the teeth
break reddens four tests.

The finding's smaller second half — `eligible_cards` computed once per `_speculative_selection` — is
NOT done and stays noted here; the two calls sit on mutually exclusive control paths and never both
execute in one election, which the finding itself records.

#### SE-05 · MEDIUM · dead-code · effort: small — **RESOLVED (2026-08-02)**

**concept_projection.py carries an unreachable 'paired core commit' fallback lattice — the owner module shipped long ago**

*Locations:* `looplab/search/concept_projection.py:19-34`, `looplab/search/concept_projection.py:49-74`, `looplab/search/concept_projection.py:108-135`, `looplab/search/concept_projection.py:144-157`

*Evidence:* The module wraps its imports in try/except ModuleNotFoundError with the comment 'the owner lands in the paired core commit; keep this commit testable alone' and 'pragma: no cover - paired core commit removes this path'. looplab/core/concepts.py exists in-tree and is imported UNCONDITIONALLY by sibling modules (concept_graph.py:51, card_selection via this module), so the fallback branches — _fallback_resolve (rename hop-cap walker), the manual rename-normalization branch of _rename_projection (:108-125), the bare-string receipt compat of _materialization_receipt (:144-157), and the _normalized_id fallback (:53-56) — can never execute. No test removes the module to exercise them (grep of tests/ shows none). ~80 lines of dead dual-implementation that must be mentally diffed against core.concepts on every read.

*Recommendation:* Delete the try/except and all fallback branches; import looplab.core.concepts unconditionally as the sibling modules already do.

*Resolution (2026-08-02):* deleted as recommended — the `try/except ModuleNotFoundError`,
`_fallback_resolve`, the hand-rolled consolidation-map normalizer, the bare-string receipt
compatibility branch, `_RENAME_HOP_CAP`, and the `CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON` import that
existed only to seed the fallback's reason tuple. `looplab.core.concepts` is now imported the way
`concept_graph.py` already imported it.

The cost was never runtime — it was that reading this module meant mentally diffing two
implementations of an identity projection whose disagreements are SILENT: a rename resolved one way
here and another way in `core.concepts` changes which concepts a proposal inherits, with no error
anywhere. `tests/test_concept_projection_core_owner.py` (26) therefore pins the behaviours the dead
branches were shadowing rather than just asserting they are gone: rename chains, cycles,
unnormalizable ids, and — the one that matters most — that resolution agrees with
`core.concepts.resolve_concept` EXACTLY, reason string included, across a matrix of inputs.

The bare-recognized-reason branch gets its own test with its own argument, because on paper it is the
one place the deletion removes a behaviour: the fallback accepted a bare reason and synthesized
`status="unavailable"` around it. The core owner does not and must not — a receipt that lost its
status is not evidence the materialization was merely partial, and inventing one downgrades a hard
failure into a softer story the reader will believe.

Teeth-tested by re-admitting the bare reason string and by restoring the try/except lattice.

#### SE-06 · LOW · duplication · effort: small

**merged-alias-id resolution fragment duplicated verbatim twice inside card_selection.py**

*Locations:* `looplab/search/card_selection.py:1244-1252`, `looplab/search/card_selection.py:1376-1396`

*Evidence:* The comprehension merged_alias_ids = {alias for card in state.cards.values() for alias in (getattr(card, 'aliases', None) or []) if isinstance(alias, str) and alias} appears byte-identically in _counterfactual_owned_selection_state and _reserved_speculative_slots, each preceded by its own multi-line comment re-explaining the same fold behavior ('the fold collapses an alias INTO its canonical... Card.merged_into is never actually assigned'). The dead-card check is likewise split between _card_administratively_dead plus per-site alias-membership checks.

*Recommendation:* Extract a merged_alias_ids(state) helper (or a card_is_dead_or_merged(state, card_id) predicate) next to _card_administratively_dead and keep the fold-behavior comment in one place.

*Resolution (2026-08-03):* `card_selection.py::merged_alias_ids(state) -> frozenset[str]` sits next
to `_card_administratively_dead`, and both `_counterfactual_owned_selection_state` and
`_reserved_speculative_slots` call it. The fold-behaviour explanation now lives once, in the helper's
docstring, instead of being re-argued above each copy.

The explanation is the load-bearing part. The fold collapses a merged Card OUT of `state.cards` and
records only its id in the canonical's `.aliases` — `Card.merged_into` is never actually assigned —
so a merged id is proven merged by ALIAS MEMBERSHIP, not by a present `merged_into` row. That is what
lets the callers distinguish "legitimately merged away, skip it" from "absent with no merge receipt",
which is a corrupt or partial ownership chain that must make the whole counterfactual fail CLOSED
rather than be silently passed over.

The `card_is_dead_or_merged(state, card_id)` predicate the finding also offers is NOT added: the two
sites consume the set differently (one tests sibling ids inside a loop that must still append
unproven ids to `pairs`, the other tests excluded ids while counting reservations), so a combined
predicate would have to return more than a bool to serve both.

#### SE-07 · MEDIUM · layering · effort: small

**Hidden lazy circular dependency: search.speculation_quality ↔ engine, contradicting speculation_calibration's stated purpose**

*Locations:* `looplab/search/speculation_quality.py:1589-1592`, `looplab/search/speculation_quality.py:755`, `looplab/engine/orchestrator.py:1015-1026`, `looplab/search/speculation_calibration.py:1-8`

*Evidence:* speculation_calibration.py's docstring says the scope identity lives there specifically to 'avoid importing the engine from the quality layer (and the resulting import cycle)'. Yet speculation_quality.py lazily imports looplab.engine.orchestrator (SPECULATION_CALIBRATION_PROFILE_DIGEST/_SETTINGS at :1589) and looplab.engine.finalize (incomplete_finalize_scope at :755), while engine/orchestrator.py lazily imports search.speculation_quality (:1015, :1026). The cycle exists, merely deferred to call time — the calibration module dodged it for two constants while the quality module reintroduced it for three others (the two profile constants plus incomplete_finalize_scope). No other search module imports engine.

*Recommendation:* Move SPECULATION_CALIBRATION_PROFILE_DIGEST/_SETTINGS into speculation_calibration.py (which already exists exactly to own such source-scoped identity), and move/export incomplete_finalize_scope through events/ or core so the search→engine edge disappears.

*Resolution (2026-08-02, profile half):* the two profile constants moved into
`search/speculation_calibration.py` exactly as recommended, along with the derivation that produces
them (`_declared_settings_json_defaults`, the overrides map, the coverage and canonical-JSON
self-checks). Nothing in that block touches the engine — it reads `Settings`' DECLARED defaults from
`core` and this module's own variant-field set — so the module now IS what its docstring always
claimed. `speculation_quality` imports a sibling instead of the orchestrator, and
`engine/orchestrator.py` re-exports both names because the engine, the CLI and the tests all spell
them there.

The digest is a RECEIPT GATE, so the move was verified byte-identical rather than merely "still
passes": `sha256:5515fda7…` before and after. If it shifted, every calibration receipt issued before
the move would stop verifying and the gate would refuse legitimately calibrated runs with no error
saying why. `tests/test_calibration_profile_home.py` (12) pins that exact value, the engine's
re-export identity, profile coverage vs the variant fields, canonical snapshot JSON, and that the
derivation still ignores the launcher's environment (a `BaseSettings()` read would make a
source-OWNED profile depend on whose machine built it).

**The last `search` → `engine` edge is still there and is named rather than glossed:**
`speculation_quality` imports `engine.finalize.incomplete_finalize_scope`. That is a different move —
a cluster of event-log helpers (`_adjacent_claim`, `_scope_has_step`, `finalize_scope_quiescent`)
with five `serve` consumers — and `finalize.py` imports `engine.costs` at module scope, so relocating
it into `events/` needs its own change. One test asserts the edge count is exactly one and points at
that file, so when it goes the layer is provably clean.

Teeth-tested by changing the digest's schema string, by pointing the quality reader back at the
orchestrator, and by making the orchestrator re-derive the digest itself. A fourth attempt — leaking
a variant field into the profile — never reached the tests: the module's own coverage self-check
raises at import, which is a stronger guard than a test and worth recording as such.

One lesson from the verification, not the change. The first version of the environment-independence
test used `importlib.reload(speculation_calibration)`. It passed alone and in pairs, and reddened SIX
unrelated tests in `test_concept_lens_durability.py` on the full run — because a reload hands every
already-imported holder (here, `engine/orchestrator.py`'s re-exports) a stale object, and the damage
only shows up once enough of the suite has been imported. The property is now driven by re-deriving
under a poisoned environment, one function call away and with no shared state touched. Worth stating
plainly: a test that mutates module state mid-suite can cost a whole run, and the failure will point
anywhere but at the test.

#### SE-08 · LOW · duplication · effort: small

**Three near-identical bespoke JSON/finite-number helpers re-implemented across the package (and repo)**

*Locations:* `looplab/search/speculation_calibration.py:105-144`, `looplab/search/speculation_quality.py:281-314`, `looplab/search/speculation_quality.py:422-429`, `looplab/search/coverage.py:33-47`, `looplab/serve/launch.py:54`

*Evidence:* _canonical_json (sort_keys, allow_nan=False, compact separators, same except-tuple) is defined identically in speculation_calibration.py:134 and speculation_quality.py:300; serve/launch.py:54 carries a looser sibling under the same name (no allow_nan=False, default=str, never raises). _strict_json_value (calibration :105) and _projection_value (coverage :33) are two strict-JSON normalizers with subtly different tolerances. _finite_metric exists three times under one name with two behaviors: events/replay.py:645 and speculation_quality.py:422 both return float|None, while engine/memory.py:673 returns bool — a reader grepping the name still cannot assume one contract.

*Recommendation:* Move _canonical_json and a finite-float coercer into core (e.g. core/atomicio or a small core/jsonutil) and import them; rename the bool variant in engine/memory to 

*Resolution (2026-08-03):* New `core/jsonutil.py::canonical_json(value) -> bytes` replaces the two
byte-identical STRICT copies (`speculation_calibration`, `speculation_quality`). Every option in it
is load-bearing because both callers build a receipt PREIMAGE — the bytes a digest is taken over —
and two spellings of "canonical" produce two digests for one logical value, so a receipt written by
one reader stops verifying for the other with nothing to say why. `allow_nan=False` is the decisive
one: `json.dumps` emits bare `NaN`/`Infinity` by default, which no strict JSON reader accepts, so a
receipt could be minted over bytes nothing else can parse. It RAISES rather than falling back,
because a caller minting a receipt must find out before the receipt exists.

`serve/launch.py`'s copy is NOT merged — it is deliberately looser (`default=str`, no
`allow_nan=False`, never raises) because a launch payload is caller-shaped and its hash is a dedup
identity rather than a preimage. It is RENAMED to `_lenient_json_bytes`, which is the actual fix:
sharing one name for two contracts is what made the difference invisible.

`core/fitness.py::finite_metric(value) -> float | None` sits next to `is_usable_metric`, whose rules
it reuses verbatim, and both float-valued readers (`events/replay.py`,
`search/speculation_quality.py`) now alias it — so they are the SAME object, not equal twins that
drift on the next edit. `speculation_quality`'s local version additionally gains correct handling of
an arbitrary-precision JSON integer such as `10**400`, which its own `float()` call raised
`OverflowError` on outside its `except`.

`engine/memory.py::_finite_metric` — the BOOL variant — is renamed `_is_finite_metric`. That
collision was the sharpest edge in the finding: `if _finite_metric(x):` is valid under both old
spellings and means opposite things, since the float version is falsy for a legitimate metric of
`0.0`.

Two normalizers stay separate on purpose and a test records why:
`speculation_calibration._strict_json_value` RAISES on a non-JSON value (a receipt preimage that must
fail closed) while `coverage._projection_value` coerces to `str` (an analytics token that must never
fail a run). Merging them would either brick analytics or weaken a receipt.

Covered by `tests/test_json_and_metric_contracts.py` (30).avoid the name collision.

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

#### SE-13 · LOW · dead-code · effort: small — **RESOLVED (2026-08-02)**

**Dead helper: _explored_concepts is never called**

*Locations:* `looplab/search/card_selection.py:637-640`

*Evidence:* _explored_concepts(state) ('Exact current concepts allowed to affect the selection-bearing coverage score') just returns _coverage_inputs(state)[0]. Repo-wide grep (looplab/ + tests/) finds only the definition — card_score calls _coverage_inputs directly (:769), so the wrapper is unreferenced.

*Recommendation:* Delete it (or use it as the hoisted scoring-context accessor when fixing the per-candidate projection rebuild).

#### SE-14 · LOW · flat-code · effort: small

**The speculative-selection API threads 8-10 identical keyword parameters through five sibling entry points**

*Locations:* `looplab/search/card_selection.py:1402-1414`, `looplab/search/card_selection.py:1549-1561`, `looplab/search/card_selection.py:1587-1598`, `looplab/search/card_selection.py:1622-1631`, `looplab/search/card_selection.py:1679-1691`

*Evidence:* speculative_card_selection_set, speculative_card_actions, speculative_raw_actions, speculative_card_is_fresh and the private _speculative_selection each re-declare and forward the same parameter bundle (state, policy, max_nodes, scoring, excluded_card_ids, ignored_pending_node_ids, include_owned_card_id, include_owned_node_id, resource_envelope, consumed_inflight). Adding one parameter (as consumed_inflight evidently was — it is absent from speculative_card_actions and speculative_raw_actions but present elsewhere, an asymmetry easy to miss) means editing five signatures.

*Recommendation:* Introduce a frozen SpeculativeSelectionContext dataclass (session-owned ids, envelope, scoring) passed once; the entry points keep only their distinguishing arguments.

*Resolution (2026-08-03):* `SpeculativeSelectionContext` (frozen dataclass) + the module-level
all-default instance `NO_SPECULATIVE_CONTEXT` now carry the session half of the query, and the four
public entry points plus `_speculative_selection` keep only their distinguishing arguments. Every
call site was converted — **29** that passed at least one bundled keyword; the rest already passed
none and take the default untouched.

The split, as designed:

* SESSION-owned, identical across every call in one producer/consumer session — `scoring`,
  `excluded_card_ids`, `ignored_pending_node_ids`, `resource_envelope`, `consumed_inflight`.
* PER-CALL subject, still an ordinary argument — `include_owned_card_id` / `include_owned_node_id`,
  and the freshness predicate's `card_id` / `node_id`. Burying the subject in the session would hide
  the one thing that distinguishes the entry points from each other.

The asymmetry the finding spotted is preserved, not "fixed" on the way past: `consumed_inflight`
stays unset at the two ELECTION call sites because election runs before the consumer admits an
attempt, while the freshness gate runs after it. The `()` default reproduces the old behaviour
byte-for-byte, and the difference is now one visible unset field at a caller instead of a missing
keyword in two of five signatures.

`tests/test_speculative_selection_context.py` pins both halves. The asymmetry half is pinned
STRUCTURALLY as well as behaviourally: `_speculative_selection` may read `consumed_inflight` only
inside the branch guarded by a named owned subject, so election — which passes no subject — cannot
see consumer admissions BY CONSTRUCTION. A "let's make the lanes consistent" edit has to lift a read
out from under that guard, and the test catches that even where a behavioural probe over one state
would not. Teeth-verified against five separate breakages (re-declared session field, unfrozen
context, subject migrated into the session, election reading the field, freshness losing it).

#### SE-15 · LOW · inconsistency · effort: small

**Duplicate k-NN prediction shims and colliding helper names across sibling modules**

*Locations:* `looplab/search/panel.py:19-41`, `looplab/search/surrogate.py:123-131`, `looplab/search/foresight.py:208-222`, `looplab/search/graded_novelty.py:40-52`, `looplab/search/novelty_recall.py:30-39`

*Evidence:* panel._predict and SurrogateResearcher._predict both wrap the shared knn_idw with a Euclidean-distance loop over key sets; the eligibility rules deliberately differ (subset vs full-dimension) and are documented, but the distance loop itself is a third copy alongside runtime/proxy.py:60-64. Separately, two unrelated functions both named _idea_text render an Idea differently (foresight.py:208 builds predictor-facing prose; graded_novelty.py:40 builds a lowercased tagger string) and novelty_recall adds _idea_full_text on top of concept_graph._node_text — three overlapping 'text of an experiment' renderers whose divergence is load-bearing (param names vs values) but discoverable only by reading all three.

*Recommendation:* Add a shared euclidean+knn_idw predict helper in events/digest.py taking an eligibility callable; rename one _idea_text and centralize the experiment-text renderers next to _node_text with explicit variants (names-only vs with-values).

*Resolution (2026-08-03):* both halves, with one deliberate narrowing.

**The distance, not the predictor.** `core/numeric.euclidean(a, b, keys)` joins `knn_idw` there
(XP-12 moved it out of `events/digest.py`, so that is where a shared numeric primitive belongs now),
and all three predictors call it. What did NOT happen is the "eligibility callable" the finding
suggests: the three rules — full-bounds dimensionality, target-subspace containment, any shared key —
are each documented at their call site as what that predictor MEANS by "comparable", and folding them
behind a parameter would hide the one thing about them worth seeing. Sharing the arithmetic while
leaving the rules in place is what stops them drifting on the distance while claiming to differ on
eligibility; a test pins each rule where it lives.

**Four renderers, four names.** `foresight._idea_text` is now `_idea_prose` (predictor-facing prose)
and `graded_novelty._idea_text` is `_idea_tag_text` (lowercased structural tagger surface). The
engine's `_idea_text` keeps its name: it is a METHOD and a documented patch seam — `orchestrator.py`
and `eval_stages.py` both name it — and it never collided with these two.

The renderers were NOT physically moved next to `_node_text`; the MAP was written there instead.
Moving them would mean `concept_graph` importing for `foresight`'s prompt-shaping and
`novelty_recall`'s paraphrase judge, which puts three consumers' concerns in one module to save a
grep. What a reader needed was to know the other three exist and why they differ — including the
non-obvious one, that `novelty_recall` carries param VALUES precisely because `_node_text` drops
them: two nodes differing only by `temperature=0.02` vs `0.05` would otherwise read identical and be
judged duplicates, when a value tweak is a VARIANT.


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

#### AG-03 · MEDIUM · inconsistency · effort: medium — **RESOLVED (2026-08-02)**

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

#### AG-04 · LOW · duplication · effort: small — **RESOLVED (2026-08-02)**

**UnifiedAgent.choose_action and triage_crash duplicate ~40 lines of identical loop scaffolding**

*Locations:* `looplab/agents/unified_agent.py:199-252`, `looplab/agents/unified_agent.py:285-325`

*Evidence:* Both methods repeat the same sequence with only content differing: pilot-client None guard, render(system prompt), messages build, inline emit_spec dict, _finalize/_fallback closures that coerce-or-default, conditional bind_state on _pilot_tools, the call-time 'from looplab.agents.agent import drive_tool_loop' seam import (choose_action with the full six-line seam comment, triage_crash with a one-line pointer to it), drive_tool_loop(max_turns=self._agent_max_turns, time_budget_s=self._agent_time_budget_s, **self._loop_opts), and the identical except BudgetExceeded raise / except Exception -> _fallback tail. Only the schema, finalize coercion and default result differ.

*Recommendation:* Extract a private _pilot_emit(self, messages, emit_spec, finalize, fallback, state=None) helper owning bind_state, the seam import, the loop kwargs and the exception tail; both methods keep their prompts/schemas/coercions (prompt strings untouched).

*Resolution (2026-08-02):* `UnifiedAgent._pilot_emit(messages, emit_spec, finalize, fallback, *,
state=None, bind_state=True)` owns what the two methods shared — the optional `bind_state`, the
call-time seam import with its six-line comment, the loop kwargs, and the containment boundary.
Each caller keeps its prompt, schema and coercion, and each keeps a one-line note saying what ITS
fallback means (the policy recommendation; the safe "attempt repair" action), so the why stays at
the site that knows.

It is also AG-06's first adopter: the containment tail is now `tool_loop.resilient`, which is
exactly the "adopt opportunistically at new call sites" that finding recommends.

`bind_state` is a FLAG rather than an inference from `state is not None`, because the two callers
genuinely differ — the pilot binds unconditionally, triage only when handed a run state — and
collapsing it would silently change which tools are reachable.

*The teeth pass needed two rounds and that is the point.* Three deliberate breaks were tried; two
initially passed. Collapsing the flag into `state is not None` was invisible because both existing
cases agreed with it, and a caller that simply stops passing `bind_state` still reads as one
`_pilot_emit(` call to a structural count. The guards now include the distinguishing case (`state`
is None but binding is still requested) and drive `triage_crash` itself. All three breaks fail
loudly.

#### AG-05 · LOW · mergeable-entities · effort: medium

**Four near-identical implementations of the 'append an emit-now nudge, parse_structured, degrade to default' salvage path**

*Locations:* `looplab/agents/agent.py:219-228`, `looplab/agents/deep_research.py:224-236`, `looplab/agents/roles.py:728-746`, `looplab/agents/strategist.py:717-722`

*Evidence:* ToolUsingResearcher._fallback (agent.py:219-228: messages + 'Emit the Idea now.' -> parse_structured -> default draft Idea), DeepResearcher._forced (deep_research.py:224-236: messages + 'Emit the memo now.' -> parse_structured -> '(deep research produced no memo)'), LLMResearcher.propose's 2-attempt retry-with-error-feedback loop (roles.py:728-746), and LLMStrategist.decide's parse-or-rule fallback (strategist.py:717-722) are structural clones of one 'forced structured parse with a safe default' pattern; all four re-state the ParseError handling, and two of them (DeepResearcher._forced, LLMStrategist.decide) also re-state the explicit BudgetExceeded-raise.

*Recommendation:* Add one helper in tool_loop or core.parse — forced_structured(client, messages, model_cls, parser, nudge, on_fail) — keeping each caller's nudge wording and default factory as arguments (prompt strings stay byte-identical); the four sites shrink to one call each.

*Resolution (2026-08-04) — three of the four; the fourth is a different shape and keeps its own.*

`core/parse.py::forced_structured(client, messages, model, parser, *, nudge, then, on_fail)`. The
agentic Researcher's forced emit, the deep Researcher's forced memo and the Strategist's
parse-or-rule decision now each call it once. Nudge wording stays a caller argument — prompt strings
are contracts and must not drift into a shared default — and the Strategist passes none at all,
because its call is the PRIMARY one rather than a forced re-emit after a failure.

`LLMResearcher.propose` is NOT migrated. It is a two-attempt retry LOOP that folds the parse error
back into the prompt before re-asking, and that re-prompt is the point: without it the retry is
byte-identical and deterministically re-fails. Collapsing it into a single forced parse would delete
the only thing that makes the second attempt worth making.

`then` runs INSIDE the guarded region, because two callers transform the parsed model there
(`.to_idea()`, `_assemble(...)`) and a transform that raises must degrade with everything else rather
than escape past the very salvage that exists to keep the run alive.

**The exception posture is what actually justified the extraction.** It depends on a fact visible at
no call site: `BudgetExceeded` is deliberately not an `LLMError`, so unlike a transport failure it
passes straight through `parse_structured` instead of arriving as a `ParseError`. A hard budget stop
must therefore END the run, while everything else degrades. Two of the three sites re-stated that
re-raise; the third (`ToolUsingResearcher._fallback`) caught only `ParseError` and got the same
effect by accident, because `parse_structured` converts `LLMError` on its way out. That accident was
load-bearing in the wrong place: `_fallback` is also the `drive_tool_loop(fallback=…)` callback, where
a raise has no handler at all. It now degrades on everything but a budget stop, which is the contract
`propose`'s own comment already claimed for it.

The Strategist needed a private `_RULE_FALLBACK = object()` sentinel rather than `None`: `None` is a
LEGITIMATE `decide` result ("no strategy change"), so it cannot double as "the parse failed" without
collapsing two different outcomes.

`tests/test_parse_llm.py` 20 → 26. Teeth-tested against 6 breaks — a budget stop degrading, the
transform escaping the guard, the nudge always appended, the sentinel collapsed to `None`, and a site
parsing directly again — all biting. (The transform break had to be written twice: the first attempt
left the second `try` under the original handlers, so it preserved the semantics it was meant to
break and reported a false green.)

#### AG-06 · LOW · duplication · effort: small — **RESOLVED (2026-08-02)**

**The 4-line 'except BudgetExceeded: raise / except Exception: fallback' idiom is copy-pasted at 9+ sites in the package**

*Locations:* `looplab/agents/agent.py:282-288`, `looplab/agents/deep_research.py:193-199`, `looplab/agents/deep_research.py:231-236`, `looplab/agents/strategist.py:719-722`, `looplab/agents/strategist.py:796-799`, `looplab/agents/unified_agent.py:249-252`, `looplab/agents/unified_agent.py:322-325`, `looplab/agents/tool_loop.py:602-605`, `looplab/agents/tool_loop.py:630-633`, `looplab/agents/tool_loop.py:728-731`

*Evidence:* The same containment idiom (hard budget stop propagates; anything else degrades to a caller-specific fallback) appears verbatim around every drive_tool_loop/parse call in the package, each with a re-worded why-comment. It also recurs in engine/novelty.py and engine/crash_repair.py. Each instance is small, but the rule lives in ~15 copies and a new caller can (and must remember to) re-derive it.

*Recommendation:* Provide a tiny shared wrapper (e.g. tool_loop.resilient(fn, fallback) or a context manager) documented once with the budget-propagation rule; adopt opportunistically at new call sites rather than churning every existing comment-bearing site at once.

*Resolution (2026-08-02):* `tool_loop.resilient(attempt, fallback, *, on_error=None)` states the
rule once, and the fifteen existing sites are deliberately UNCHANGED — which is this finding's own
recommendation ("adopt opportunistically … rather than churning every existing comment-bearing
site"), and the right call under CLAUDE.md's load-bearing-comments rule. Each of those comments
records why THAT fallback is safe (e.g. "`_fallback` is itself resilient … so it can't re-raise the
transport error"); replacing them with a bare call would trade fifteen small duplications for
fifteen lost explanations.

The value is therefore the WRITTEN RULE plus a guard, not a line saving. The asymmetry is easy to
get backwards and expensive in both directions: swallowing `BudgetExceeded` keeps a run billing past
the ceiling an operator set to stop it, while propagating a transport blip crashes a run and loses
every node already evaluated. `tests/test_agent_containment_rule.py` pins both directions plus three
properties a bare try/except tends to miss — a contained failure is offered to an observer, a
PROPAGATING budget stop is NOT (or telemetry counts a deliberate stop as an agent error), a broken
observer cannot escalate a contained failure, and a failing FALLBACK is not swallowed (there is no
safe value left, and hiding it hands the caller a silent `None`).

11 tests; all four deliberate breaks fail loudly.

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

*Resolution (2026-08-03):* Renamed to `strategist.classify_run_phase`; `agents/agent.py::run_phase`,
the tool-loop-with-handoff patch seam, keeps its name. `engine/strategy.py` is the only importer and
follows it.

The back-compat alias the recommendation offers is deliberately NOT added: it would preserve exactly
the collision the rename exists to remove — a grep for `run_phase` would still return both. A
repo-wide grep confirms no other consumer, and a test asserts the alias stays absent.

#### AG-09 · LOW · over-engineering · effort: small

**agent.py facade re-exports six private tool_loop names that nothing imports through it**

*Locations:* `looplab/agents/agent.py:34-38`

*Evidence:* The re-import block forwards 17 names 'because callers and tests import AND monkeypatch them THROUGH this module', but repo-wide grep shows _PLAN_TOOL_NAME, _REPEAT_NOTE, _TRUNC_NOTE, _plan_spec, _render_plan and _summarizer are never accessed via agent.* or 'from looplab.agents.agent import' anywhere (only _force_emit, _cap_tool_result, _flatten_transcript, _handoff_ctx and the public names are). The facade is documented and legitimate, but the blanket re-export of unused privates grows the two-path ambiguity (patching tool_loop._REPEAT_NOTE vs agent._REPEAT_NOTE would already diverge for constants, since re-imported strings are rebindings, not aliases).

*Recommendation:* Trim the re-export list to the names with verified external consumers (the four privates above plus the public API), noting in the comment that new tool_loop privates are NOT auto-forwarded.

*Resolution (2026-08-03):* trimmed to exactly the four, with the rule written into the comment.
`_force_emit` (tests/test_agentic_retrieval.py), `_cap_tool_result` (tests/test_deep_research_loop.py),
`_flatten_transcript` and `_handoff_ctx` (tests/test_phase_handoff.py — and `_handoff_ctx` is read by
`run_phase` in this module) stay; `_PLAN_TOOL_NAME`, `_REPEAT_NOTE`, `_TRUNC_NOTE`, `_plan_spec`,
`_render_plan` and `_summarizer` are gone.

The finding's parenthetical is the whole reason this is worth doing rather than cosmetic: for a
module-level CONSTANT the re-import is a rebinding, not an alias, so `tool_loop._REPEAT_NOTE` and
`agent._REPEAT_NOTE` are already two objects. Forwarding one by default hands a future caller that
ambiguity for nothing — the failure mode is a monkeypatch that appears to take and changes nothing
that runs.

Note CO-10 went the other way for `llm.py`, and both are right: that barrel's re-exports have
documented consumers and a test that patches THROUGH it, so the list is load-bearing there.

#### AG-10 · LOW · duplication · effort: small

**The 4-cue tuple for prompt cues is duplicated as a literal in both researchers and is not covered by the registry scan**

*Locations:* `looplab/agents/roles.py:678-679`, `looplab/agents/agent.py:238-239`, `looplab/agents/roles.py:242-244`

*Evidence:* Both LLMResearcher.propose and ToolUsingResearcher.propose call collect_hint_cues with the identical inline tuple ("_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint") — a strict subset of RESEARCHER_HINT_ATTRS whose docstring promises 'both researchers honor the same cues'. tests/test_hint_forwarding.py scans setattr/forwarding sites, not these two read-side literals, so a new prompt cue added to the registry must be hand-added at both call sites and a one-sided edit silently desyncs the two prompts.

*Recommendation:* Hoist the tuple to a named module constant next to RESEARCHER_HINT_ATTRS (e.g. RESEARCHER_PROMPT_CUES) referenced by both propose() methods, and have the registry docstring/test point at it.

*Resolution (2026-08-03):* `roles.py::RESEARCHER_PROMPT_CUES` sits next to `RESEARCHER_HINT_ATTRS`
and both `LLMResearcher.propose` and `ToolUsingResearcher.propose` reference it.

The gap this closes is specific: `tests/test_hint_forwarding.py` scans the WRITE side (setattr and
wrapper forwarding), so the two read-side literals were unguarded — a cue added to the registry and
to only one `propose()` makes the agentic path and the plain path ask the model different questions,
with no test to notice and no error at runtime. The constant's docstring also records why it is a
strict SUBSET: `_digest_cap` is a numeric cap, `_hyp_order` orders the board inside `_state_brief`,
and `_novelty_stance` / `_steering_context` / `_cross_run_advisory_receipt` are read structurally
rather than concatenated as prose.

Covered by `tests/test_agent_and_adapter_seams.py`, which pins the subset relation, the exact
membership, and that neither `propose()` re-inlines the literal.


### 4.10 Tools

Scope: `looplab/tools/`: ~25 ToolProviders.

**Reviewer assessment.** looplab/tools/ is a well-disciplined collection of ~25 duck-typed ToolProviders with a genuinely minimal shared contract (_base.py fn_spec + RESULT_CAP) and a consistently enforced never-raise/soft-fail rule, strong path/secret/SSRF hardening, and unusually honest truncation/partial-source receipts. The structural problems concentrate in the two assistant-facing modules — machine_runs_tools.py (1659 lines mixing three providers with crash-recovery journaling and a command adapter, including a tools→serve layering violation its sibling modules explicitly forbid) and cross_run_tools.py (one ~856-line _execute function) — plus systematic re-implementation of two ceremonies (the permission decide/ask/deny gate ~6x, RESULT_CAP truncation ~7x) and two parallel stacks that should merge (three foreign-run reader wrappers; RepoTools vs RepoScoutTools; MemoryTools vs CrossRunTools over the same lessons ledger with contradictory scoping). The RunStateCache and edit_match extractions show the team already knows the consolidation pattern; it just hasn't been applied to the newer accretions.

**Strengths worth preserving:**

- The ToolProvider contract is deliberately minimal and uniformly honored: fn_spec is the single schema builder, every provider soft-fails from execute() with documented rationale, and result budgets are derived from the shared RESULT_CAP constant rather than free-standing magic numbers — the loop cap and provider budgets move together by construction.
- RunStateCache (_runcache.py) is a model consolidation: previously-duplicated fold-on-demand plumbing extracted once, LRU-bounded with an explicit rationale, and its PARTIAL-SOURCE divergence receipts are threaded through every foreign-run reader so a truncated log can never masquerade as a complete run — epistemic honesty enforced in infrastructure.
- Security engineering is thorough and consistently commented: path-traversal guards with symlink re-validation on resolved targets (reposcout grep/find), secret-name filtering applied to listings as well as reads, SSRF preflight + post-connect peer verification with proxy-awareness (web.py), ReDoS pattern caps, bounded reads/downloads with wall-clock deadlines, and credential redaction at every persisted/model boundary.
- patch.SurfacePolicy is a good example of merging three previously independent write gates into one value object while explicitly documenting (not erasing) the per-site semantic differences as constructor parameters — the opposite of a lossy 'simplification'.
- Truncation honesty as a design principle: nearly every tool distinguishes 'absent' from 'not searched/cut' ('this is NOT evidence of absence', resume markers with exact continuation lines, capped-at receipts), which directly targets the model-facing failure mode of treating a partial read as a completed negative search.

#### TO-01 · HIGH · under-decomposition · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**cross_run_tools._execute is a single ~856-line dispatch function containing eight tool implementations**

*Locations:* `looplab/tools/cross_run_tools.py:473`, `looplab/tools/cross_run_tools.py:490`, `looplab/tools/cross_run_tools.py:899`, `looplab/tools/cross_run_tools.py:1113`, `looplab/tools/cross_run_tools.py:1329`

*Evidence:* `CrossRunTools._execute` runs from line 473 to line 1329 (~856 lines) as one flat `if name == ...` chain implementing all eight tools (cross_run_prior_attempts, cross_run_claims, cross_run_atlas, cross_run_concept_map, cross_run_search, similar_runs, find_concept_slugs, concept_card) inline, each 60-220 lines, with per-branch local imports, nested helper closures (`_receipt` defined twice, lines 829 and 988), and the partial-source/scope/claim warning boilerplate (`_partial_source_warning` + `_partial_scope_warning` + `partial_note` append sequences) repeated ~10 times across branches. The fuzzy slug-scoring block (exact-normalized=1.0, substring=0.9, SequenceMatcher, >=0.55 floor) is copy-pasted between find_concept_slugs (lines 1050-1060) and concept_card (lines 1171-1181) — the comment at 1167-1170 even says 'Scoring mirrors find_concept_slugs exactly', i.e. the duplication is known but not extracted. Contrast: RunControlTools in machine_runs_tools dispatches the same way but to one private method per verb.

*Recommendation:* Split each `if name == ...` branch into a private method (as RunControlTools already does), extract the fuzzy-score function (`_slug_score(query, slug) -> float`) shared by find_concept_slugs and concept_card, and extract a small receipt-builder helper that appends the source/scope/claim partial warnings so the ~10 hand-rolled sequences collapse to one call.

*Resolution (2026-08-02, the known duplication):* the fuzzy slug scoring is now one module-level
`_slug_score(normalized_query, slug)` plus a `_SLUG_MATCH_FLOOR` constant, shared by
`find_concept_slugs` and `concept_card`.

This was the piece the code itself admitted — the second copy carried the comment "Scoring mirrors
find_concept_slugs exactly", i.e. the correspondence was maintained by hand. It is worth fixing
ahead of the larger split because the two callers decide DIFFERENT things from the same number:
which slugs to offer an agent, and which existing card an agent's spelling resolves to. Drift
between them makes a slug findable by a query that cannot then open its card — an agent chasing a
concept it was just told exists.

Extracting it surfaced a second reason the copies existed: `difflib` was imported PER BRANCH, so a
module-level helper could not see it. It is hoisted, and the two now-shadowed local imports removed.
That also explains why the duplication survived — the natural place to put a shared helper did not
have the import it needed, and the failure mode was the generic `(cross-run tool unavailable)`
swallow rather than an ImportError.

*Resolution (2026-08-02, the split and the receipt):* `_execute` is now a seven-line dispatcher over
one private method per verb — `_tool_cross_run_prior_attempts` … `_tool_concept_card` — the shape
`RunControlTools` already used. The governance snapshot the re-entry resolved is passed EXPLICITLY
to the handler rather than read from an enclosing scope, and `_TOOL_NAMES` gates the lookup so a
model-supplied `name` cannot reach a same-spelled attribute of the class.

The warning boilerplate collapsed into `_partial_warnings(source=, scope=, claims=)`. Each
`_partial_*_warning` already returns "" for a complete view, so all fifteen call sites were
re-deriving that same `is not True` emptiness test by hand, in two spellings
(`view.get("source_complete") is not True` and a pre-computed `not source_complete`). These lines
are the sentence that tells a reader an ABSENCE is not proof, so a site with the polarity backwards
produces no error at all — it produces a confident-looking answer with the caveat missing. Four
sites keep a genuine extra condition (`want != "own"` for a scope that has no cross-run
denominator, `capsule_scope_uncertain`) and now express it by passing `None` for that view.

Verified with a 209-case byte-level differential (every tool × bound/unbound × role × argument
shape, over both a complete and a deliberately partial portfolio) against a worktree of the
pre-split tree: **byte-identical**. `tests/test_cross_run_tool_dispatch.py` adds 14 tests for what
the differential cannot see, and six deliberate breaks — drop the empty filter, flip the claim
guard, swap the warning order, drop the registry gate, stop threading governance, print the scope
caveat unconditionally — were each caught by exactly the test guarding that property.

One process note worth keeping: the teeth loop timed out mid-run and its restore step never
executed, so `handler(args, None)` (break T5) sat in the tree while later edits were made on top of
it. The differential would have caught it at the next comparison; what actually caught it first was
a NEW unit test failing for a reason its own change could not explain. Verification steps that
mutate the tree need their restore to be a separate, unconditional command.

Verified against the exact expression both branches used, over 120 query/slug pairs spanning
separator and case variants, leaf-vs-path matches, unicode and empty input: zero mismatches. Four
guards in `tests/test_cross_run_tools.py` pin one definition, the two-ratio max (concept keys are
PATHS, so a bare technique name shares no prefix with its full slug), and the empty-query guard —
without which the empty string is a substring of every slug and a blank query resolves to an
arbitrary card at 0.9. All three deliberate breaks fail loudly.

*Still open:* the 856-line `if name == ...` chain itself, and the ~10 hand-rolled partial-source /
partial-scope warning sequences.

#### TO-02 · HIGH · over-engineering · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**machine_runs_tools.py is a 1659-line god-module: 3 providers + crash-recovery fence + command adapter, with _subtree defined three times**

*Locations:* `looplab/tools/machine_runs_tools.py:92`, `looplab/tools/machine_runs_tools.py:288`, `looplab/tools/machine_runs_tools.py:1442`, `looplab/tools/machine_runs_tools.py:1485`, `looplab/tools/machine_runs_tools.py:1570`, `looplab/tools/machine_runs_tools.py:1197`, `looplab/tools/machine_runs_tools.py:1299`

*Evidence:* One module holds: `_TurnMutationFence` (~150 lines of assistant-turn crash-recovery journaling), `_RunCommandAdapter` (~230 lines; its `submit` alone is ~100 lines of conflict/uncertainty handling), plus three unrelated providers (MachineRunsTools read-only, RunLauncherTools, RunControlTools) and module-level rendering helpers. Concrete duplication inside: the parent-closure `_subtree` BFS is defined verbatim three times — as a closure in `_delete_node` (1442-1451), again in `_commit_delete_node_snapshot` (1485-1494), and inlined a third time in `_purge_node_snapshot` (1570-1578). The 'stale subject changed while awaiting permission' fence (read_all → tail seq → fold → compare attempt) is duplicated between `_reset_node` (1310-1332) and `_retag_node` (1362-1382). `_settings` (1197-1297) is a flat 100-line function containing three fully independent verbs (extend_budget / set_directive / set_trust_gate) already dispatched by name at line 1148 — the outer dispatch then re-dispatches inside. RunLauncherTools.specs embeds a ~70-line prompt (intentional per CLAUDE.md prompt-contract rule, but it inflates the module further).

*Recommendation:* Split the module: `_turn_fence.py` (_TurnMutationFence), `_run_command_adapter.py`, `run_launcher_tools.py`, `run_control_tools.py`. Extract `_subtree(state, root_id)` as one module-level function used by all three delete paths, extract the stale-node fence into a helper shared by _reset_node/_retag_node, and break _settings into three methods dispatched directly from execute().

*Resolution (2026-08-02, the three named duplications):* all three are done; the file split is still
open (see below).

`_node_subtree(state, root_id)` replaces the three verbatim copies of the descendant walk. Those
copies were not free to disagree: the purge re-runs the walk and compares its answer against the
approved scope, refusing on a mismatch — so a copy that drifted would not have produced a wrong
deletion, it would have produced a permanent refusal of a correct approval, which reads like data
corruption. The extraction also gave the walk a place to say WHY it is a fixpoint sweep and not a
recursive descent: the node graph is a DAG, a merge node has several parents, and a child joins the
subtree as soon as ANY parent is inside it.

`_node_lifecycle_unchanged(store, node_id=, expected_tail=, generation=)` replaces the fence
`_reset_node` and `_retag_node` each spelled out. The property it defends is that a confirm card can
stay open indefinitely while another control resets or tombstones its subject, and the fence is the
whole log TAIL rather than just the node — the operator approved an action against a run they were
shown, and a sibling append changed that run.

`_settings` — a flat 100-line chain re-dispatching on the same `name` the outer dispatch had just
matched — is now `_tool_extend_budget` / `_tool_set_directive` / `_tool_set_trust_gate`, reached
directly. Splitting them made the odd one out visible in its own docstring: the trust gate is the
only settings verb that is NOT command-backed, because it also mirrors `config.snapshot.json` so a
later resume re-enters with the new gate.

`tests/test_node_subtree_and_fence.py` adds 13 tests. Five deliberate breaks — require ALL parents
instead of any, drop the fixpoint sweep for a single pass, drop the tail fence, allow a tombstoned
subject, ignore the generation — were each caught. The single-pass break is the one worth naming:
iteration order over `state.nodes` is not topological, so a one-pass version is right on most
inputs and wrong when a descendant is visited before its parent joins.

**Still open:** the module is 1,721 lines and still holds `_TurnMutationFence`, `_RunCommandAdapter`
and three unrelated providers. That split is a separate change with its own verification.

#### TO-03 · MEDIUM · layering · effort: medium — **PARTIALLY RESOLVED (2026-08-02)**

**tools -> serve layering violation in machine_runs_tools, contradicting the rule other tools modules explicitly state**

*Locations:* `looplab/tools/machine_runs_tools.py:1276`, `looplab/tools/machine_runs_tools.py:1479`, `looplab/tools/machine_runs_tools.py:49`, `looplab/tools/write_tools.py:225`, `looplab/tools/mcp_tools.py:24`

*Evidence:* `machine_runs_tools._settings` imports `looplab.serve.run_files.run_config_write_lock` (line 1276) and `_commit_delete_node_snapshot` imports four PRIVATE serve names — `_engine_alive`, `_fresh_resume_launch_pending`, `_fresh_run_launch_pending`, `_run_lifecycle_lock` from `looplab.serve.engine_proc` (line 1479). Sibling modules state the opposite rule as a design invariant: write_tools.py:225-227 ('string-matched here rather than imported because tools must never import serve (layering)') and mcp_tools.py:24-27 ('Computed locally instead of importing looplab.serve.assistant.REPO_ROOT ... the tools layer must not depend on the serve layer'). The same module even duplicates serve logic specifically to AVOID this import — `_local_run_generation` (line 49) reimplements RunCommandService's first-event hash 'without a tools -> serve import' — while two other functions in the same file import serve directly, so the module is internally inconsistent about the rule, and the duplicated hash can silently drift from the serve-side canonical one.

*Recommendation:* Inject the serve dependencies the way `alive_fn` already is: pass lifecycle-lock / launch-pending / config-write-lock callables into RunControlTools' constructor from serve/assistant.py, and have the command service expose `run_generation` so `_local_run_generation` can be deleted. This restores the one-direction rule the package's own comments assert.

*Status (2026-08-02):* the injection seam landed — `RunLifecycleFns` injected by serve, with the
lazy serve import kept as the deliberate default fallback (`e4722db`; resolved per call so read-only
assistant sessions never pay for the server package). Same seam as XP-03; see there for what remains.

#### TO-04 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**The permission-gate ceremony (decide_action -> deny message -> approver -> approval_allows -> declined message) is re-implemented ~6 times**

*Locations:* `looplab/tools/write_tools.py:215`, `looplab/tools/shell_tools.py:162`, `looplab/tools/shell_tools.py:213`, `looplab/tools/concept_tools.py:131`, `looplab/tools/knowledge_tools.py:220`, `looplab/tools/machine_runs_tools.py:1067`, `looplab/tools/mcp_tools.py:186`

*Evidence:* Six providers each hand-roll the identical three-step authorization ritual: build an action dict, call `decide_action(mode, action)`, map 'deny' to a plan-mode refusal string, and on 'ask' call `self.approver(action) or "deny"` through `approval_allows`, returning a '(declined by the user: ...)' string. Sites: WriteTools._authorize (215-228), ShellTools.exec_argv (213-220) plus a second inline copy for kill_background (162-171), ConceptGovernanceTools._gate (131-142), KnowledgeWriteTools.execute inline (220-225), RunControlTools._gate (1067-1084, with generation capture interleaved), GatedMcpTools.execute (186-191). The bodies differ only in refusal wording and tool_kind; the `approver(action) or "deny"` idiom and `approval_allows` call recur at all six sites in near-identical form (mcp_tools uses the private `_approver`/`_mode` names; machine_runs_tools adds a None-guard on the approver).

*Resolution (2026-08-02):* added as `perm_modes.authorize(mode, approver, action, *, denied,
declined)` — None to proceed, a string to return to the model — plus `refusal_for(decision, …)` for
the two sites that legitimately need the DECISION itself. All six providers now go through it.

`denied` and `declined` stay at the call sites because the model reads them: each refusal names which
capability is off and how to turn it on, and prompt-visible strings are contracts.

This is not a tidiness finding, and the tests say so. One of the six copies had already been caught
checking `deny` alone and killing a process-global background task in the DEFAULT `ask` mode with no
approval at all (arch-review §3 P0-6) — plan-mode deny does not satisfy ask-mode approval semantics,
and a gate that forgets the second half still LOOKS gated to every reader and to every test that only
tries plan mode. So `tests/test_permission_ceremony.py` (34) runs the whole mode × decision × verdict
matrix against the helper AND asserts, provider by provider, that each one refuses an UNAPPROVED ask
— not just that it refuses in plan mode. It also pins that a provider with no approver at all
declines (being unable to ask is not permission), that the approver is called exactly once with the
exact action, and a grep guard that `approval_allows(` appears nowhere in `tools/` outside
`perm_modes.py`.

Two sites keep `decide_action` + `refusal_for` rather than the one-shot `authorize`, each for a real
reason stated inline: `machine_runs_tools._gate` captures the run generation BETWEEN the deny
short-circuit and the approval round-trip, so its mutation fence describes the run before the user
was asked; and `shell_tools.exec_argv` reads the decision again afterwards, where an `inline`
read-only git peek is deliberately not recorded in `self.applied`. The second was caught by the
existing `test_shell_tools`/`test_git_tools` suites after a first pass dropped the variable — the
kind of dependency a mechanical collapse loses silently if the consumers are not re-run.

Teeth-tested against five breaks: a deny-only gate (the P0-6 regression itself, which reddens 23
tests), a truthiness verdict check that authorizes `"allow_onc"`, a missing approver reading as
permission, the generation captured after the approval, and one provider re-inlining the ceremony.

*Original recommendation:* Add `perm_modes.authorize(mode, approver, action, *, deny_msg=None) -> Optional[str]` (None = proceed, else the refusal string) and have all six sites delegate, keeping per-site wording via the parameter. Removes ~60 duplicated lines and guarantees future policy changes (e.g. remembered grants) apply everywhere at once.

#### TO-05 · MEDIUM · mergeable-entities · effort: medium

**Three near-identical foreign-run reader wrappers: SiblingRunTools, AllRunsTools, and MachineRunsTools' read half**

*Locations:* `looplab/tools/run_tools.py:649`, `looplab/tools/run_tools.py:762`, `looplab/tools/run_tools.py:821`, `looplab/tools/run_tools.py:903`, `looplab/tools/machine_runs_tools.py:735`, `looplab/tools/machine_runs_tools.py:751`

*Evidence:* All three classes hold the same composition (`self._runs = RunStateCache(run_root)`; `self._reader = RunTools(max_chars=...)`) and the same delegation shape: resolve run_id via cache, return '(no such run: ...)' on miss, fetch `source_note`, `self._reader.bind_state(st, None)`, then prefix-and-forward to the inner RunTools tool. Compare SiblingRunTools._read/_code (run_tools.py:762-789), AllRunsTools._read/_code (903-919), MachineRunsTools._read_run/_read_experiment/_read_logs (machine_runs_tools.py:735-767) — the bodies differ only in the scope check and the tool name. The `_list_runs` renderers are likewise triplicated, including the identical 'PARTIAL SOURCE (read incomplete; later results unknown)' receipt string in all three (the explanatory comment is pasted verbatim in two of them; AllRunsTools carries the receipt without it) (run_tools.py:755-758, 894-896; machine_runs_tools.py:729-732). The genuine differences (SiblingRunTools' fail-closed task_id boundary, AllRunsTools' no-filter policy, MachineRunsTools' liveness column) are small policy hooks on top of ~120 duplicated lines.

*Recommendation:* Extract a small base/mixin (e.g. `_ForeignRunReader` holding the cache+reader, `_delegate(run_id, tool, args, prefix)` and one `_run_line(...)` renderer with optional live/task columns); each class keeps only its scope predicate and specs. The task-boundary semantics stay where they are — only the plumbing merges.

*Resolution (2026-08-04):* `run_tools.py::ForeignRunReader` is the base; all three providers inherit
it. It owns the composition (`RunStateCache` + the inner `RunTools`), `_state`, `_delegate(run_id,
tool, args, *, prefix, missing)` and `_partial_suffix(run_id)`. Six methods across the three classes
(`SiblingRunTools._read/_code`, `AllRunsTools._read/_code`,
`MachineRunsTools._read_experiment/_read_logs`) are now two lines each, and the PARTIAL-SOURCE receipt
has exactly one spelling in the tree.

The task boundary stayed where it is, as a policy hook: `_scope_denial(run_id, st)` defaults to no
filter and `SiblingRunTools` overrides it with the fail-closed same-task rule. `_delegate` consults it
BEFORE binding the reader, which matters — a denial issued after the delegate would already have read
the foreign run's text.

**The `_list_runs` renderers were NOT merged**, and the finding's "identical receipt string" is the
only part of them that actually was identical. The three listings answer different questions and carry
different columns: siblings render task-scoped rows with a `best=#id` pointer, `AllRunsTools` adds the
`[task]` column its cross-task audience needs, and `MachineRunsTools` renders from its `summaries()`
projection (shared with the assistant's @run-mention expansion) with a LIVE column and a goal excerpt.
A single renderer with optional live/task/goal columns and three header strings would be a
three-branch function serving three callers — so only the receipt moved.

Pinned by `tests/test_foreign_run_reader.py` (21): the shared composition, the miss wording per
surface, the source receipt present on a truncated read and ABSENT on a complete one (a receipt on
every read trains the model to ignore it), one spelling of the listing receipt across all three,
the sibling boundary on the DIRECT read, the unbound reader failing closed, the two cross-task readers
deliberately applying none, the scope hook running before any content is read, and structural guards
that no provider re-spells the bind-and-receipt dance or re-declares its own cache. Teeth-tested
against 13 breaks, all biting.

#### TO-06 · MEDIUM · mergeable-entities · effort: medium

**Two parallel read-only repo-browsing providers: RepoTools (knowledge_tools) vs RepoScoutTools**

*Locations:* `looplab/tools/knowledge_tools.py:52`, `looplab/tools/knowledge_tools.py:86`, `looplab/tools/knowledge_tools.py:153`, `looplab/tools/reposcout.py:92`, `looplab/tools/reposcout.py:154`

*Evidence:* `RepoTools` (Researcher-facing: repo_grep/repo_list/repo_read over named mounts) re-implements what `RepoScoutTools` (boss/Developer-facing: grep/find_files/read_file over named roots) already provides: root-confined path resolution (RepoTools._resolve at knowledge_tools.py:86-100 vs _pathsafe.resolve_within + RepoScoutTools._resolve at reposcout.py:154-162), per-hit secret filtering (knowledge_tools.py:114-117 vs reposcout.py:556-558), .git exclusion (`_readable_repo_path` vs reposcout's `_looks_secret`/`_readable`), and pagination — RepoTools.repo_read even lazily imports `RepoScoutTools._paginate` (knowledge_tools.py:153-159) to reuse the window logic. RepoScoutTools already supports named multi-roots (`named_roots`, `_disp` prefixing) which is exactly RepoTools' mount model. The two evolved independently, so their guards drift: RepoTools caps repo_grep at 40 hits with no file budget while RepoScoutTools has a 4000-file budget, skip-dirs, and overlay awareness.

*Recommendation:* Make RepoTools a thin adapter over RepoScoutTools configured with named_roots (renaming the three tool names in specs and keeping its .git-internals filter), or delete it and expose RepoScoutTools with the repo_* aliases to the Researcher. One walker, one secret gate, one budget.

*Resolution (2026-08-04):* Done — `RepoTools` composes a `RepoScoutTools` and no longer walks the
tree itself. `repo_grep` → `_grep` per mount, `repo_list` → `_find_files`, `repo_read` →
`_read_file`. One walker, one secret gate, one budget.

The gap was real and the drift was one-directional — `RepoScoutTools` is uniformly the stricter
walker, so every difference was a guard `RepoTools` LACKED:

* `repo_grep` walks with no file budget at all, where `_grep` stops after 4 000 files and says so;
* it descends `_SKIP_DIRS` and hidden directories that `_grep` prunes (`node_modules`, `.mypy_cache`,
  venvs, checkpoints);
* it has no per-file size skip, where `_grep` skips anything over 2 MB;
* it caps at 40 hits with no receipt, where `_grep` clamps to ≤200 and emits `(capped at N hits)` —
  so an overflowing `repo_grep` reads as an exhaustive search.

`repo_read` already delegated its pagination (`RepoScoutTools._paginate`), which is why the M9
full-file-then-paginate fix reached it; it now delegates the whole read.

This is a path-restriction surface, so three things had to survive the merge — each a place where
"just delegate it" would have quietly changed what the Researcher can see, and each now pinned:

1. **Glob semantics differ.** `repo_list` matches a bare `*.py` RECURSIVELY (`retrieval.glob_files`
   is rglob-shaped); `_find_files` runs a pathlib glob where `*.py` is one level. `_recursive_glob`
   rewrites `*.py` → `**/*.py` and leaves an already-recursive or path-scoped pattern alone —
   handing the pattern over unchanged would silently stop showing every subdirectory file.
2. **Root selection differs.** `_grep` takes ONE root; `repo_grep` searches every mount. It now
   emits one BLOCK per mount, each carrying that mount's own `(capped at N hits)` /
   `(stopped after 4000 files…)` receipt. Merging the hit lines into a single cut is precisely what
   let a partial answer read as exhaustive, so a merged list would have re-created the defect while
   removing the duplication.
3. **`<repo>/<path>` resolution is RepoTools-only.** `RepoScoutTools._resolve` knows `default_root`
   and CWD, not named mounts, so `a/x.py` under mounts `a` and `b` resolves only through
   `RepoTools._resolve` — kept, and handed the scout an ABSOLUTE path it re-confines. `named_roots`
   makes `_disp` render the same `<name>/<rel>` labels this tool always emitted.

The `.git` filter is also deliberately NOT delegated: `_pathsafe.looks_secret` does not know `.git`,
so the scout's own gate would hand back a credentialed clone's `.git/config`. `_readable_repo_path`
still runs first, and the existing security regression test covers it.

One difference became a PARAMETER rather than being swallowed. `_grep` pruned every dotted directory
because one of the scout's roots is `~/`, where `.cache`/`.venv` dwarf the repo — but a Researcher
grepping ONE mounted repo wants `.github/workflows`, which is ordinary source. `_grep` gained
`skip_hidden=True` (default, so the scout's own contract is unchanged) and `RepoTools` passes
`skip_hidden=False`. `.git` sits in `_SKIP_DIRS` and is pruned in both modes, so the credential
surface is closed either way. This also makes the divergence `_find_files` already documented
("HIDDEN entries are yielded") explicit instead of a silent difference between two walkers.

Pinned by `tests/test_repo_reader_adapter.py` (17): no walker of its own, an overflowing grep
carrying a receipt, noise dirs pruned, `.github` still visible, the scout still pruning dotted dirs
BY DEFAULT, the glob-translation table, the named-mount round trip, per-mount receipts (one capped
mount must not swallow another's complete answer), the `.git` refusal, escape refusal, and the M9
page-marker contract. The seven pre-existing `test_repo_tools.py` tests are unchanged and still pass.
Teeth-tested against 12 breaks, all biting.

#### TO-07 · MEDIUM · inconsistency · effort: medium

**MemoryTools and CrossRunTools expose the same lessons.jsonl with contradictory scoping policy and two different tokenizers**

*Locations:* `looplab/tools/memory_tools.py:208`, `looplab/tools/memory_tools.py:18`, `looplab/tools/cross_run_tools.py:205`, `looplab/tools/cross_run_tools.py:28`, `looplab/adapters/tasks.py:460`, `looplab/adapters/tasks.py:475`

*Evidence:* adapters/tasks.py binds BOTH providers to the same run when memory_dir + cross_run_read_tools are set: CrossRunTools (line 460) and MemoryTools (line 475). CrossRunTools invests heavily in fail-closed scoping of lessons.jsonl rows — `_in_scope` (cross_run_tools.py:205-245) rejects rows with missing/mismatched `direction`, wrong task family, and the current run's own rows, with extensive comments about why unknown polarity must stay invisible. MemoryTools.search_lessons (memory_tools.py:208-224) reads the same lessons.jsonl with NO direction, task, or self-run filter — only lexical overlap over a bounded recent window — so the very rows CrossRunTools deliberately hides are retrievable one tool over in the same agent's toolset. The two also use different tokenizers for the same matching job: cross_run_tools `_WORD = [^\W_]+` Unicode-aware casefold (line 28) vs memory_tools `_WORD = [a-z0-9@._]+` ASCII lower (line 18), so the same query matches different lesson sets depending on which tool the model happens to call.

*Recommendation:* Either route MemoryTools.search_lessons through the same `_in_scope` predicate (bind_state it like CrossRunTools) or fold search_lessons/recall_notes into CrossRunTools as two more verbs; at minimum share one tokenizer helper so the two surfaces agree on what matches.

*Resolution (2026-08-04) — both halves, via one shared predicate. This CHANGES what a bound agent sees.*

`trust/cross_run.py` now owns `LessonScope` (the visibility predicate) and `scope_terms` (the
tokenizer). `MemoryTools` gained a `bind_state` hook and filters `search_lessons` through
`self._scope.allows(row)`; `CrossRunTools._in_scope` delegates to the same object.

The predicate moved to `trust/` rather than onto either provider because it is a trust boundary, not
tool plumbing, and because a predicate that lives on one of two peers is one refactor away from being
"the other one's business" again. `CrossRunTools`'s five loose scope attributes (`_bound`, `_task_id`,
`_run_id`, `_direction`, `_scope_terms`) became read-only PROPERTIES over `self._scope` — a dozen call
sites in that file read them, and a plain copy would be the same drift in miniature.

Behaviour, stated plainly: a BOUND agent's `search_lessons` no longer returns rows with unknown or
opposite `direction`, rows from a foreign task family without a strict goal-fingerprint overlap, or
this run's own rows. An UNBOUND provider — the CLI/human audit path — stays portfolio-wide, which is
the direction that would LOSE evidence if narrowed, and is exactly how `CrossRunTools` already
behaved.

One rule stayed on the provider rather than moving: a CAPSULE additionally needs a complete persisted
fingerprint before foreign-task visibility is granted (`_capsule_fingerprint_scope_complete`). That is
a fact about how capsules are written — capped or legacy-unknown fingerprints exist — and not part of
the general row predicate. It is now expressed as one guard ahead of the shared call instead of being
tangled into the middle of it.

`recall_notes` is deliberately NOT filtered. `meta_notes.jsonl` carries no direction or task-family
provenance to scope on, so applying this predicate would hide every note rather than the unsafe ones.
A test pins that as a decision instead of an oversight.

`tests/live/scenarios.py::memory_recall` seeded its lesson without `direction`, which a bound reader
now hides — the fixture was updated to persist it, matching what both production lesson writers do.
`docs/guide/memory.md`'s retrieval table states the scoping.

Pinned by `tests/test_lesson_scope.py` (20): the unbound/bound split, the polarity table (missing,
empty, `"minimize"`, `"MIN"`, `0`, `["min"]` all hidden), the self-run fence, the two-salient-terms
overlap rule, the namespaced-token rule (provenance must not DILUTE the denominator and make an
on-topic row look unrelated), a sparse bound state binding closed rather than degrading open, both
providers answering identically end to end, the empty-result message disclosing that a scope ran, the
capsule rule in both directions, and one tokenizer across both modules — asserted over the AST,
because both modules now describe the old ASCII pattern in their comments and a substring scan
matches its own explanation. Teeth-tested against 15 breaks, all biting.

#### TO-08 · MEDIUM · duplication · effort: medium

**Seven-plus independent implementations of 'fit a tool result under RESULT_CAP with an honest marker'**

*Locations:* `looplab/tools/run_tools.py:34`, `looplab/tools/shell_tools.py:61`, `looplab/tools/env_inspect.py:342`, `looplab/tools/reposcout.py:54`, `looplab/tools/memory_tools.py:47`, `looplab/tools/mcp_tools.py:44`, `looplab/tools/concept_tools.py:243`

*Evidence:* Each provider re-derives a budget from RESULT_CAP (with independently chosen headroom: -400 in cross_run_tools/concept_tools/reposcout/shell_tools, -200 in env_inspect, -160 in mcp_tools, 'reserve=100' in memory_tools) and re-implements bounded rendering with its own marker text: run_tools `_clip` (tail-keep, '…[+N earlier chars truncated]'), shell_tools `_tail` (tail-keep, '…(truncated)…'), env_inspect `_clamp` (head-keep at line boundary), reposcout `_fit_rows` (drop rows, '(N more omitted to fit the result cap)'), memory_tools `_bounded_result` (drop rows, '[RESULT_WINDOW: ...]'), mcp_tools `_clip` (head-keep, '…[mcp reply truncated — {n} chars omitted]'), concept_tools inline `append_bounded` closure, plus run_tools' repeated `while visible: ... visible.pop()` loops (lines 247-257, 407-416, 551-562). Some head-vs-tail differences are deliberate (documented per site), but the row-dropping and line-boundary-cut variants are the same algorithm rewritten five ways, each with subtly different receipts a model must learn separately (mcp_tools.py:35-38 itself notes markers should match each other).

*Recommendation:* Add two shared helpers next to RESULT_CAP in core/context_budget or _base.py — `fit_rows(header, rows, receipt, cap)` (already exists as reposcout._fit_rows; promote it) and `clip(text, cap, *, keep='head'|'tail', note)` — and migrate the row-dropping and single-string sites onto them, keeping per-site marker wording as a parameter.

*Resolution (2026-08-04):* Done as recommended. `tools/_base.py` (beside the `RESULT_CAP` re-export)
now holds `fit_rows(header, rows, *, receipt, cap, omitted)` and
`clip(text, cap, *, keep, note, reserve, line_boundary)`. Migrated: `reposcout._fit_rows`,
`memory_tools._bounded_result`, `run_tools._clip`, `shell_tools._tail`, `mcp_tools._clip`,
`env_inspect._clamp` — each keeps its own thin named wrapper, so its call sites and its per-site
comment trail are untouched and the marker wording stays where the reader expects it.

`header` accepts a string (`reposcout` owns its trailing newline) or a sequence of lines
(`memory_tools` builds a header list), which was the only structural difference between the two
row-droppers.

`clip`'s four options are four REAL differences, not knobs added to force a merge, and the docstring
says which caller each is for:

* `keep` — a log or command stream is read tail-first (the end holds the error and the final metric
  line), so its marker goes in FRONT; a reply or listing is head-kept.
* `line_boundary` — `env_inspect` cuts back to the last newline, because a half-hit reads as a
  complete one.
* `reserve` — whether the marker is charged AGAINST the cap. Most callers pass a cap that already
  carries headroom; `mcp_tools` is handed the loop's RAW `RESULT_CAP` and must reserve, since a reply
  landing EXACTLY on the cap is one the loop's own marker also skips — a cut answer
  byte-indistinguishable from a complete one.
* `note` — the per-site receipt, formatted with `{n}` = characters dropped.

One behaviour was made uniform rather than preserved: the dropped-character count now describes the
RESULT, not the intended budget. A line-boundary cut gives back more than `cap - budget`, and
`env_inspect`'s marker carried no count at all, so nothing regressed — but a future caller that asks
for `{n}` on a boundary cut now gets the true number.

Pinned by `tests/test_bounded_tool_results.py` (26): the fit/overflow/receipt-survival table, the
marker being SIZED before the fit is decided (a marker appended after is exactly what pushes a
receipt past the cap), a cap too small for any row still answering honestly, both clip directions,
the line-boundary rule in both directions, `reserve` on and off, the dropped-count semantics, and
per-provider guards that each wrapper delegates and has no fitting loop of its own. `memory_tools`
gets a BEHAVIOURAL guard instead of a structural one — it keeps a `[RESULT_TRUNCATED]` backstop, so a
reader that stopped fitting rows would fall through to a blunt mid-row cut while a structural check
still saw the shared call on the way past. A separate guard re-derives that every provider's budget
is still `RESULT_CAP - <headroom>` rather than a free-standing constant, which is the other half of
the finding. Teeth-tested against 18 breaks, all biting.

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

*Resolution (2026-08-03):* all three, exactly as recommended. Each was small; each cost something
more specific than its line count, which is what the replacement comments record.

`perm_modes.decide` was labelled "compatibility" with nothing left to be compatible with, and it was
not merely redundant — it answered the same question with a COARSER rule. All `write` was `inline`
under `acceptEdits`, whereas `decide_action` first demotes a write with no recovery receipt to
CONSEQUENTIAL, which asks. A future provider reaching for the shorter-looking name would have
silently widened its own permissions. Its five tests moved onto `decide_action` over CONCRETE
REGISTERED identities (grouped by risk, since a kind alone does not determine the answer — precisely
why the shortcut had to go), and gained the case the old one could not express: `acceptEdits` asking
for an edit it could not undo.

`VectorStore.delete`/`rebuild` left the Protocol. On a Protocol, speculative methods are worse than
dead code: the seam exists to state what a LanceDB/Qdrant backend must implement, so it was wrong in
both directions — asking a real backend for machinery nothing calls, while the shapes a persistent
store genuinely needs (durable open/close, index compaction) are absent because nobody has written
one. `InMemoryVectorStore` keeps both as its own API.

`RunTools.parent` is no longer stored. The parameter stays in the signature — it is contractual
(`tools/_base.py`: a provider implementing `bind_state` without it raises TypeError at dispatch) —
so this is accept-and-ignore, as MachineRunsTools already does. Storing a value nobody reads implied
a back-reference these read-only tools do not have.

#### TO-11 · LOW · excessive-logic · effort: small

**concept_card/find_concept_slugs re-run full-portfolio canonicalization per call inside an already-huge module (excessive per-call work + duplicated governance plumbing)**

*Locations:* `looplab/tools/cross_run_tools.py:941`, `looplab/tools/cross_run_tools.py:1142`, `looplab/tools/cross_run_tools.py:405`

*Evidence:* find_concept_slugs and concept_card each independently reload all capsules (`_all_capsules` re-reads and re-dedups concept_capsules.jsonl per call, line 405-414), then build a full `canonicalize_concepts` map over every capsule (`canonical_by_capsule` at 943-947, `canonical_caps` at 1142-1146 — the same computation with a different container shape), and re-partition scope. `_scoped_capsules` similarly recomputes for every one of the other tools. Within one agent turn calling find_concept_slugs then concept_card (the documented workflow — the follow-up is prescribed in find_concept_slugs' rendered output and in concept_card's spec), the whole portfolio is re-canonicalized twice. There is no fingerprint cache analogous to RunStateCache even though the underlying files are the same governance snapshot the call already takes.

*Recommendation:* Cache the (capsules, canonical-sets) pair keyed by (capsule file sig, taxonomy governance_revision) on the provider instance — the revision is already fetched per call — and share the canonicalization structure between the two branches once they are extracted into methods.

*Resolution (2026-08-03):* exactly that pair, split across the two things that invalidate at
different times. `_all_capsules` memoizes the READ on the capsule file's `file_identity` alone;
`_capsule_snapshot` memoizes the canonical MAP on the taxonomy revision, over the rows that read
returned. `similar_runs`, `find_concept_slugs` and `concept_card` all consume it, so the documented
workflow — call one, then the other on a slug it returned — canonicalizes the portfolio once.

`file_identity` rather than a hand-rolled (size, mtime): the portfolio store rewrites by
`os.replace`, so a same-size same-second rewrite is invisible without dev/ino. That is the case the
test drives, because it is the one a weaker key would silently get wrong — and the wrong outcome here
is an agent answering a concept question from a superseded portfolio while saying nothing about it.

Two shapes the implementation is committed to, both tested. The map is keyed by object IDENTITY, so
the read must hand back the SAME row objects — a caller that filters rows from a second
`_all_capsules()` call would get a KeyError on every lookup, which is how the first attempt failed.
And the revision map holds ONE entry: a long-lived provider would otherwise grow a full copy of the
portfolio's concept sets per governance edit.

The finding's `_scoped_capsules` note is covered by the same change — it goes through
`_all_capsules`, so it stops re-reading too.


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

*Resolution (2026-08-03):* Split as recommended. `looplab/agents/factory.py` holds the composition
root (`make_roles`, `build_unified_agent`, `build_strategist_tools`, `make_developer_factory`,
`_shared_providers`, `_make_abstractor`, `_memora_cache_path`, `_set_role_client`, `_agent_model`);
`adapters/tasks.py` (795 -> 361 lines) keeps the task schema/registry and re-exports every moved
name, so `from looplab.adapters.tasks import make_roles` and any patch of that name keep working —
they resolve to the SAME objects, which is the property that makes the move invisible.

Two things the split had to get right, both now pinned:

* the `make_llm_client` re-export block rode along with the agent half on the first cut and had to be
  put back. It is `adapters/tasks.py`'s documented back-compat surface and `cli/__init__.py` imports
  through it.
* the layering asymmetry. Every `agents`/`search`/`tools` import in the moved code was already
  FUNCTION-LOCAL, and must stay that way: `search` imports `agents` at module scope, so a
  module-level `looplab.search` import in `agents/factory.py` closes the cycle into an ImportError at
  startup. `TaskAdapter` is TYPE_CHECKING-only for the same reason in the other direction —
  `adapters.tasks` re-exports FROM the factory. A subprocess test imports the factory FIRST, because
  a cycle shows up in only one order.

The split surfaced a pre-existing dead store: `shared = resolve_llm_target(settings)` in
`build_unified_agent`, read by nothing — every comparison below it is stage-vs-ROLE. Removed, with
the note kept so it is not re-added as if it were needed.

The `make_roles` developer-backend extraction is NOT done. `make_roles` is 193 lines and its three
branches are genuinely distinct wirings, but they interleave with the shared provider/prompt setup
rather than sitting as three separable blocks; splitting them needs its own pass with room to verify
each backend, and doing it badly would scatter the wiring instead of naming it. Recorded so the
remaining half is visible rather than assumed done. Teeth-verified against 6 breakages.

Two REGISTRY guards caught the split, which is what they exist for, and both needed a real update
rather than a green-making edit:

* `tests/test_cross_package_private_seams.py` — the back-compat re-export pulls five
  private-by-convention names (`_agent_model`, `_make_abstractor`, `_memora_cache_path`,
  `_set_role_client`, `_shared_providers`) across a package boundary. Declared, with the reason:
  dozens of call sites and tests already spell them as `looplab.adapters.tasks._make_abstractor`, so
  the re-export has to carry them or the split stops being invisible.
* `tests/test_task_adapter_contract.py` — the `params` hook's consumer probe moved with the
  composition root, so the hook read as orphaned. `agents/factory.py` added to the consumer scan.

#### RA-02 · HIGH · under-decomposition · effort: medium

**run_command_eval is a ~265-line god-function with 23 parameters (19 keyword) and two hand-mirrored eval branches**

*Locations:* `looplab/runtime/command_eval.py:736-1001`, `looplab/runtime/command_eval.py:825-930`, `looplab/runtime/command_eval.py:891-893`, `looplab/runtime/command_eval.py:952-954`, `looplab/runtime/command_eval.py:814`, `looplab/runtime/command_eval.py:848-852`

*Evidence:* One function does: setup phase, staged-pipeline loop (~105 lines incl. stage reuse, live-band spans, health watchdog, salvage, inter-stage check_fn), the single-command branch, metric read, drift cross-check, adapter-reader trust guard, declared+auto extra metrics, constraints, trials — behind 19 keyword parameters and three nested closures (_log, _sp, _bound). The two branches duplicate the stall-window resolution expression (`stall_timeout if stall_timeout is not None else _stall_window(...)` at 891-893 and 952-954), the docker timeout fold `to = to or (is_docker and docker_timed_out(rc))` (three occurrences: 814, 894, 955), and the authenticated-signal plumbing. The fragility already bit once: the `_sig` UnboundLocalError fix at 848-852 exists because the result expression at line 1001 reads a variable bound inside whichever branch happened to run.

*Recommendation:* Extract the staged loop into a _run_stages(ctx, stages, ...) helper returning (rc, out, err, to, sig, stage_results | early RunResult), and bundle the shared execution knobs (wrap, is_docker, grace, env, cancel, log_dir, tracer, stall settings, max_output_bytes) into a small context dataclass. That removes the cross-branch variable leakage and the triplicated timeout-fold/stall-window expressions.

*Partially resolved (2026-08-03).* The two duplicated EXPRESSIONS are done; the `_run_stages`
extraction and the context dataclass are not, and that is recorded rather than assumed.

* `_timed_out(to, rc, is_docker)` replaces the triplicated docker-timeout fold. This is
  correctness-relevant, not cosmetic: under the container tier the deadline is enforced by coreutils
  `timeout` INSIDE the container, so the host subprocess exits normally and `run_argv` reports
  `to=False` — the timeout appears only as exit 124/137. A run site that omits the fold reports a
  timed-out eval as an ordinary non-zero failure, which sends the Developer to repair code that
  never got to finish.
* `_stall_window_for(stall_timeout, budget, stall_cap)` replaces the duplicated window derivation.
  The staged pipeline and the single-command path must agree, or a staged run kills healthy long
  stages the serial path would have let run.

`tests/test_command_eval_folds.py` pins the semantics AND that every run site goes through the
helpers, because the failure mode is a NEW site added without the fold — which no behavioural test
of the existing sites would catch.

**Not done:** `_run_stages` + the execution-knob dataclass. That is the half that removes the
cross-branch variable leakage (the `_sig` UnboundLocalError fix the finding cites as evidence), and
it needs room to verify the staged and single-command branches against each other rather than a
mechanical lift. Left explicit so the remaining work is visible.

One process note, because it cost a cycle: `runtime/command_eval.py` carries a UTF-8 BOM, so a
scripted edit that reads it as plain `utf-8` hands `ast.parse` a leading U+FEFF and dies before
writing. Third occurrence in this campaign — `encoding="utf-8-sig"` on read plus writing the BOM
back explicitly is the spelling that works.

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

#### RA-04 · MEDIUM · dead-code · effort: small — **RESOLVED (2026-08-02)**

**METRIC_READERS is dead code with a false docstring; metric-reader kinds are maintained in three parallel places**

*Locations:* `looplab/adapters/tasks.py:99-101`, `looplab/adapters/repo_task.py:173-187`, `looplab/runtime/command_eval.py:187-310`

*Evidence:* METRIC_READERS = {"stdout_json", "stdout_regex", "file_json", "file_regex", "host_score", "adapter", "auto"} has zero consumers anywhere in looplab/ or tests/ (repo-wide grep: only its own definition). Its comment claims 'Shared by normalize + the EvalSpec.metric.reader validator', but normalize_task only checks reader == "auto" and EvalSpec._valid_metric_kind (repo_task.py:179) hardcodes its own local _KINDS set with the same values minus 'auto'. read_metric's if-chain is a third hand-maintained enumeration of the same kinds. Adding a new reader kind today requires touching read_metric and _valid_metric_kind while the ostensible registry stays stale.

*Recommendation:* Either make METRIC_READERS the real single source (import it in _valid_metric_kind as METRIC_READERS - {"auto"}, and derive read_metric's dispatch from it) or delete it and fix the docstring. The repo's own registry-guarded-seam convention (CLAUDE.md) argues for the former.

*Resolution (2026-08-02):* both options, in that order, and the second is what actually closes it.
A peer took the delete option first (`a077d86` removed the dead constant and its false "shared"
docstring), which fixed the lie but left the finding's real complaint — the kinds enumerated in three
parallel places — untouched.

Closed under **RA-05**: `runtime/command_eval.py::METRIC_READERS` is now a live `{kind: reader_fn}`
dispatch table that `read_metric` dispatches through, and `repo_task.EvalSpec._valid_metric_kind`
validates against `set(METRIC_READERS)` rather than its own local copy. The name is back, but as the
registry-guarded seam CLAUDE.md's convention asks for: a reader that exists but is unlisted is
unconfigurable, and a listed kind with no reader is a red test rather than a spec that validates at
submit and then returns no metric forever.

#### RA-05 · MEDIUM · flat-code · effort: small — **RESOLVED (2026-08-02)**

**read_metric is a 120-line flat if-chain with the security-critical workdir-confinement guard copy-pasted three times**

*Locations:* `looplab/runtime/command_eval.py:187-310`, `looplab/runtime/command_eval.py:209-215`, `looplab/runtime/command_eval.py:240-245`, `looplab/runtime/command_eval.py:292-297`

*Evidence:* Six reader kinds are handled in one linear if-chain, and the containment idiom `if not _is_within(X.resolve(), Path(workdir).resolve()): return None` wrapped in `try/except (OSError, ValueError)` appears verbatim three times (file_json/file_regex path, host_score predictions path, adapter module path). This is the guard that stops answer-key reads and arbitrary host-code exec; three hand-copies means a future fourth reader can plausibly forget it.

*Recommendation:* Extract one _confined(workdir, rel) -> Optional[Path] helper (resolve + _is_within + exception handling) used by all file-touching branches, and consider a {kind: reader_fn} dispatch table so a new reader kind must go through the table (and the confinement helper) rather than a new elif.

*Resolution (2026-08-02):* both halves done. `_confined(workdir, rel)` is the one containment guard,
and `read_metric` is now a `METRIC_READERS` dispatch table over five reader functions, so `read_metric`
itself is two lines.

**The extraction surfaced a real latent bug, present identically in all three hand-copies.** They
caught `(OSError, ValueError)`, but `Path.resolve()` also raises `RuntimeError("Symlink loop from
…")` — and the candidate can CREATE that loop inside its own workdir at eval time (`ln -s b a;
ln -s a b`, then a `file_json` spec naming `a`). Verified against the pre-refactor function: it
raised out of `read_metric` and took down the RUN, where every other malformed-spec branch fails the
node. `_confined` catches it. This is the concrete form of the finding's own argument — three copies
means one place to fix it and two places to forget.

The table also closes **RA-04**'s remaining half. `repo_task.EvalSpec._valid_metric_kind` now
validates against `set(METRIC_READERS)` instead of its own local copy, so the reader kinds are
enumerated ONCE. A new reader is unconfigurable until it is registered, and registering it is what
routes it past `_confined`.

`tests/test_metric_reader_confinement.py` (29) pins traversal, absolute paths, symlinks out (and
symlinks that legitimately stay in), the NUL and symlink-loop refusals, that every registered
path-touching reader routes through the guard, that `_is_within` is spelled in exactly three places
(its definition, `_confined`, and host_score's INVERSE labels-must-be-outside assertion), the
validator/table agreement, and that an unknown kind still falls through to no-metric rather than a
KeyError. The adapter case asserts a marker file is NOT written — that branch `runpy`s what it is
given, so a missed guard there is code execution, not a wrong number.

Teeth-tested against five breaks: the adapter skipping the guard, string-prefix containment instead
of `.resolve()`, the validator drifting back to a local copy, the narrowed except (the symlink-loop
crash), and host_score's inverse assertion degrading to a silent None.

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

*Resolution (2026-08-03):* all three moved, exactly as recommended — `proxy.py` to `search/` beside
`surrogate.py`, `notebook.py` to `events/` beside the other exporters, `jupyter.py` to `serve/`. The
`_LAYOUT` map was updated so the flat aliases (`looplab.proxy`, …) resolve to the new homes and still
return the SAME module object, which is what keeps monkeypatching through either path working.

The finding calls this low urgency, and the folder placement was. The IMPORT EDGE was not: with
`proxy.py` gone, `runtime` no longer imports `events` at all — that single edge existed only to reach
a k-NN function (XP-12, resolved with it) — so the package now imports nothing above `core`, and the
docstring that already claimed "process execution" is true rather than aspirational.

Two things worth naming because they fail silently rather than loudly:

* `jupyter.py` is reached by NAME from installed metadata, not by import, so the
  `jupyter_serverproxy_servers` entry point in `pyproject.toml` had to move with it. Left behind, the
  only symptom would have been a missing Launcher tile in a JupyterHub deploy — nothing in the test
  suite, which imports the module directly, would have noticed.
* `proxy.py` keeps its own param coercion, which accepts numeric STRINGS where `numeric_params` must
  not. Now that they are in the same package the temptation to "unify" them is closer, so a test
  drives the disagreement instead of relying on the comment.

`tests/test_package_contracts.py` pins the runtime membership, the no-imports-above-core rule, the
`_LAYOUT` entries and the entry point.

#### RA-09 · LOW · flat-code · effort: medium

**bg_tasks task records are raw dicts with a hand-rolled lock protocol re-explained at five call sites**

*Locations:* `looplab/runtime/bg_tasks.py:108-117`, `looplab/runtime/bg_tasks.py:178-223`, `looplab/runtime/bg_tasks.py:304-311`, `looplab/runtime/bg_tasks.py:320-327`, `looplab/runtime/bg_tasks.py:336-353`

*Evidence:* Each task is a dict {"proc","log","fh","cursor","cmd","cwd","timed_out","deadline_lock","deadline"} (+ later "closed") indexed by string keys from six methods. The subtle F10 concurrency contract — poll() reaps and frees the PID, so every reaping poll must hold deadline_lock, while pre-checks must read returncode only — is enforced by convention and re-documented in comment blocks at _enforce_deadline (185-192), the sweep (149-156), read (305-311), list (322), and kill (337-353). The invariant lives in prose, not structure.

*Recommendation:* Introduce a small _BgTask class owning proc/log/fh/cursor/deadline_lock with methods like locked_poll(), reap(), enforce_deadline() so the lock discipline is encoded once; the five comment blocks collapse into one docstring. Behavior-preserving refactor.

*Resolution (2026-08-05):* `_BgTask` now owns the record and the discipline. The dict is gone —
`__slots__` fields instead of string keys, so a misspelled read is an `AttributeError` at the call
site rather than the `None` that `.get("timed_ouy")` used to return and that would have read as "not
timed out".

`locked_reap_and_poll()` is the F10 rule as code: close the fd if the child is gone, then read its
status, both under `deadline_lock`. The three callers that needed it (`read`, `list`,
`_sweep_deadlines`) say so in one line each, and the five comment blocks collapse to one docstring.
`status_row()` absorbs the status projection `read` and `list` both spelled out.

Three polls deliberately stay outside that helper, each fencing itself for its own reason:
`_enforce_deadline`'s non-blocking try, `kill`'s fence around the tree-kill, and `_evict_finished`'s
non-blocking retention sweep. The guard therefore checks that every reaping poll is fenced ONE WAY OR
THE OTHER rather than that all of them use the helper — the first draft asserted the stricter rule and
was wrong about `_enforce_deadline`.

The non-reaping pre-check is unchanged and still reads `.returncode`: it runs outside the lock, and
`poll()` there could reap a child mid-`_kill_tree` and free its PID for reuse.

Test-side: 13 tests constructed or read these records as dicts and were re-pointed in the same change
(the CLAUDE.md contract-change rule), including two that build a synthetic task to drive
`_enforce_deadline` directly — those now build a real `_BgTask`, so they exercise the shipped shape.

#### RA-10 · LOW · duplication · effort: small

**mle-bench registry/data-dir resolution and is_prepared are triplicated across the three mlebench modules**

*Locations:* `looplab/adapters/mlebench_real.py:41-69`, `looplab/adapters/mlebench_prep.py:42-51`, `looplab/adapters/mlebench_grade.py:32-34`

*Evidence:* The idiom `registry if not data_dir else registry.set_data_dir(Path(data_dir).resolve())` appears three times (mlebench_real._competition, mlebench_prep._registry, inline in mlebench_grade.grade), and is_prepared(competition_id, data_dir) is defined twice with identical bodies (mlebench_real.py:66-69 and mlebench_prep.py:47-51). mlebench_real.py even claims _competition is 'The single place the registry/data-dir resolution lives' — untrue given the other two copies.

*Recommendation:* Have mlebench_prep and mlebench_grade import _competition (and is_prepared) from mlebench_real, or move both helpers into a tiny shared _mlebench_registry helper module; then the 'single place' comment becomes true.

*Resolution (2026-08-03):* `mlebench_prep` and `mlebench_grade` now import `_competition` (and
`is_prepared`) from `mlebench_real`, so its "the single place the registry/data-dir resolution lives"
comment is true — and the comment now carries its own provenance so the claim is enforced rather than
asserted.

The `.resolve()` is the part that mattered. `registry.set_data_dir` keys the whole competition layout
off that path, so a relative `--data-dir` resolved by one caller and left relative by another points
at two different trees whenever the process cwd differs. The symptom is "not prepared", or a grade
computed against the wrong answers — never an error.

Covered by `tests/test_agent_and_adapter_seams.py`, which drives the resolver against a stub registry
under a changed cwd rather than only scanning source, and asserts `prep.is_prepared is
real.is_prepared` by identity. Note mlebench_real.py already imports is_prepared-adjacent code lazily, so no import-weight concern.


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

*Resolution (2026-08-03):* Split three ways along the recommended lines, every command body moved
VERBATIM by line range so no comment or prompt string changed:

* `inspect_cmds.py` (1701 → 211 lines) — run diagnostics only: `replay`, `speculation-gate`,
  `timings`, `inspect`, `tensorboard`.
* `concept_cmds.py` (new) — the Part IV concept/novelty diagnostics.
* `governance_cmds.py` (new) — everything that WRITES cross-run memory or spends money on a steward,
  plus the read-only portfolio views over the same sources.

Each header now states its own mutation contract, which is the half of the finding that was really
about the docstring: the governance header names its three classes (durable writes through
`_governed_write`, paid stewards fenced at-most-once by `--action-id`, and fail-closed reads through
`_governance_cli_read`) instead of sitting under a "read-only inspection" claim.

`_make_llm_client` and `_settings_for_run` moved UP to `looplab/cli/__init__.py` rather than into
either group, because both groups that reach an endpoint need them. Each group imports them into its
own namespace, so `monkeypatch.setattr(<group>, "_make_llm_client", …)` still works — and now names
the module that actually calls it. Twelve such patch sites across seven test files were re-pointed
and re-verified rather than shimmed: adding back-compat re-exports on `inspect_cmds` would have kept
the imports working while silently turning every one of those seams into a no-op.

**The split surfaced a live defect.** `_concept_map_for` and `concept_coverage` each bound
`_settings, client = _optional_client(...)` — the underscore that means "deliberately unused" — and
then read plain `settings.llm_parser` inside the `client is not None` branch. There is no
module-level `settings`, so the AGENTIC path (the DEFAULT for these commands) raised
`NameError: name 'settings' is not defined` the moment an endpoint was actually reachable. Every
offline test takes the `client is None` branch, so nothing had gone red. Both are fixed.

`tests/test_cli_command_groups.py` pins the boundary as a per-group command inventory (so a new
command has to be placed on purpose), that all 25 still reach the live typer app, that each header
keeps its mutation contract, and that no group is a god-module again. The `NameError` is pinned via
`symtable` — the interpreter's OWN scope analysis, not a hand-rolled approximation that would miss
comprehension scopes or closure cells — so the guard covers the whole class, not the two known
sites. Teeth-verified: restoring either underscore turns three assertions red.

#### CT-02 · HIGH · under-decomposition · effort: medium

**run() is a 347-line command function doing at least seven distinct jobs**

*Locations:* `looplab/cli/run_cmds.py:282-629`, `looplab/cli/run_cmds.py:390-412`, `looplab/cli/run_cmds.py:471-511`, `looplab/cli/run_cmds.py:543-556`, `looplab/cli/run_cmds.py:557-627`

*Evidence:* run() spans lines 282-629: flag validation, unified-file loading, settings merge, TWO speculation-gate-calibration validation blocks (~70 lines at 390-412 and 471-511 plus the fresh-dir check at 543-556), Genesis authoring, task validation, missing-path preflight, snapshot publication under run_config_write_lock, four-way lifecycle triage (finalization-pending / pending-finalize / finished-reopen / paused-resume), and driving the engine. The numbered step comments (# 1. … # 4.) are already the seams of the missing functions. The calibration envelope alone contributes ~100 lines of one-purpose validation that most readers of run() never need.

*Recommendation:* Extract per-phase helpers: _resolve_task_and_settings(...), _validate_calibration_envelope(task, settings, out), _publish_snapshots(out, task_dict, settings), and _triage_prior_run(prior, prior_events, eng) (the latter shared with resume — see the duplication finding). run() becomes a ~60-line pipeline, and the calibration lane becomes independently testable.

*Resolution (2026-08-03):* Four of the five recommended extractions landed; `run()`'s BODY went from
347 to 210 lines (the remaining 65 lines of the function are its frozen `--flag` signature and
docstring, which are not work it does).

* `_pin_offline_speculation_profile(settings, *, calibration, genesis, goal)` — the pre-Genesis
  profile guard, which has to run before any client construction.
* `_calibration_envelope_task_dict(task, settings)` — the restricted envelope, returning the
  canonical dump its evidence needs. Every clause is still collected before raising, so an operator
  sees the whole envelope at once rather than fixing it one rejection per invocation.
* `_assert_calibration_dir_is_fresh(out, prior_events)` — the zero-prior-anything requirement.
* `_publish_run_snapshots(out, task_dict, settings)` — both snapshots in ONE reset-aware config
  transaction, including the deliberate `except OSError: pass` around the task snapshot.
* the prior-run triage is CT-03's shared `classify_prior_run` (below).

`_resolve_task_and_settings(...)` was deliberately NOT extracted, and this is a decision rather than
an omission: steps 1–3 read 21 of `run`'s typer parameters, so the helper would take 21 arguments —
the precise shape RA-02 flags as a defect three findings later. Turning a readable prologue into a
21-argument call would move the problem, not fix it. The lane that made `run` hard to read was the
maintainer-only calibration envelope, and that is gone.

`tests/test_cli_lifecycle_triage.py` bounds the body length (measured on the body, so a regression
cannot hide behind the option surface), asserts `run` calls each helper rather than inlining it, and
pins the two behaviours that a tidy-up would quietly drop: the envelope reports every violation at
once, and an unwritable task snapshot does not lose the config snapshot that already landed.

#### CT-03 · MEDIUM · duplication · effort: medium

**Prior-run lifecycle triage duplicated across run / resume / finalize**

*Locations:* `looplab/cli/run_cmds.py:557-627`, `looplab/cli/run_cmds.py:707-732`, `looplab/cli/run_cmds.py:793-848`

*Evidence:* run() (557-627) and resume() (707-732) both compute pending_finalize_scope = incomplete_finalize_scope(prior_events); finalization_pending = scope is not None or prior.finalization_pending(); then branch identically on (finalization_pending | stop_requested-with-error | finished | paused) with byte-identical echo strings ("run has an incomplete terminal projection — completing its existing wrap-up", "run has a pending finalize — wrapping it up (report / cross-run lessons / cost)") and the same EV_RESUME append. finalize() (793-848) carries a third variant of the same predicate cluster plus _pending_finalize(). This is a replay-critical decision (which event, if any, to append before re-entering the loop) implemented three times; a fix to one branch (as the history of these comments shows happens often) must be manually mirrored.

*Recommendation:* Extract one shared classifier, e.g. classify_prior_run(prior, prior_events) -> Literal["finalization_pending","pending_finalize","finished","paused","live","fresh"] plus a small act-on-it helper that appends EV_RUN_REOPENED/EV_RESUME and echoes. run/resume/finalize each keep only their surface-specific differences (run reopens, resume waits for handoff, finalize CAS-appends run_abort).

*Resolution (2026-08-03):* `run_cmds.classify_prior_run(prior, prior_events)` is now the single
answer, over `terminal_projection_incomplete(state, events)` — the disjunction all three commands
needed and all three spelled out. `announce_wrap_up(kind)` owns the two byte-identical notices and
returns whether the caller may lift the run at all.

`run` and `resume` call the classifier; `finalize` uses `terminal_projection_incomplete` in the
three places it had its own variant. The surface differences stay where they belong — `run` reopens
a finished dir with `run_reopened` so the loop processes a new budget, `resume` appends the
universal `resume`, `finalize` still CAS-appends `run_abort` — because those genuinely differ;
collapsing them would be the wrong deduplication.

The ladder ORDER is the contract and is pinned rung by rung in
`tests/test_cli_lifecycle_triage.py`: an incomplete terminal projection outranks everything, a stop
newer than the finish (or a finish whose reason is `error`) is a pending finalize that must be
RESPECTED rather than lifted, and only then the liftable states. Teeth-verified — reordering any
rung, dropping the scope half of the predicate, making a wrap-up state liftable, or re-spelling a
shared notice each turns exactly the matching assertion red. The notice guard joins Python's
implicit adjacent-literal concatenation first, so a re-spelled copy cannot hide by being wrapped
across two lines.

#### CT-04 · MEDIUM · mergeable-entities · effort: small

**Three paid-steward commands are ~70% copy-paste of each other**

*Locations:* `looplab/cli/inspect_cmds.py:1076-1143`, `looplab/cli/inspect_cmds.py:1199-1259`, `looplab/cli/inspect_cmds.py:1294-1354`, `looplab/cli/inspect_cmds.py:1112-1113`, `looplab/cli/inspect_cmds.py:1329-1330`

*Evidence:* concept-steward, task-facets and claim-steward share an identical skeleton: reject deprecated --apply before any paid call, _governance_cli_read preflight over a curation log, Settings() + `if model: settings.llm_model = model` (including the SAME two-line why-comment about model_copy writing a phantom attr, duplicated verbatim at 1113 and 1330), _run_cli_steward(memory_dir, kind, action_id, prepare=lambda: _make_llm_client(settings), invoke=...), per-proposal printing, and _echo_cli_invocation; the --json early-exit and curation_is_empty check exist in concept-steward and claim-steward only (task-facets tests `if not facets` and has no JSON mode). The core transaction is already extracted (_run_cli_steward), but ~40 lines of framing per command remain triplicated, as does the 4-line except-(GovernanceLedgerUnavailable|EventStoreLockError)/except-ValueError block that additionally appears verbatim in concept-merge (1032-1036), concept-split (1067-1071), claim-decide (1191-1195) and task-facets-set (1286-1290).

*Recommendation:* Add a second-tier helper (e.g. _steward_command(kind, memory_dir, action_id, model, apply, preflight, invoke, render)) that owns the --apply rejection, model override, preflight and invocation echo; and a @_governance_errors decorator/context manager for the repeated except block in the four deterministic governance writes.

*Resolution (2026-08-03):* `inspect_cmds._steward_command(memory_dir, kind, action_id, *, apply,
apply_refusal, model, preflight, invoke, request)` now owns the four-step paid-steward preamble, and
the `_governed_write()` context manager owns the refusal block for the four deterministic governance
writes. `concept-steward`, `task-facets` and `claim-steward` call the first; `concept-merge`,
`concept-split`, `claim-decide` and `task-facets-set` call the second. What legitimately differs
stays at the call site: the refusal wording (each names the command to use instead), the preflight
body, the invocation and the per-command rendering.

Two details drove the shape. The preamble's SEQUENCE is the contract, not the reuse: `--apply` is
refused before anything costs money; the audit preflight runs before a provider client exists, so
corrupting the ledger cannot become a way to spend money; and the model override goes through
`apply_llm_model_override` rather than `settings.llm_model = model` — assignment validation is off on
`Settings`, so a direct write lands a phantom attribute and the flag silently does nothing. The
client stays a THUNK (`prepare=lambda: …`) so a replayed action id never constructs one. On the write
side the two `except` arms are different KINDS of failure and must not collapse: a ledger/lock error
is redacted (an unreadable governance store must not leak filesystem shape into CLI output), while a
`ValueError` is the operator's own bad argument and its text is the whole diagnosis.

The duplicated two-line why-comment about the phantom attribute now lives once, in
`apply_llm_model_override`'s caller-facing docstring in `_steward_command`.

Covered by `tests/test_steward_command_framing.py` (21 tests): the preamble order, the no-preflight
path, the thunk, the redact-vs-echo split, an unexpected exception NOT being swallowed, plus source
guards that every call site goes through the shared helpers and that the
`(GovernanceLedgerUnavailable, EventStoreLockError)` pair appears in exactly two places.

#### CT-05 · MEDIUM · duplication · effort: small

**Memory-dir stat-resolution + governed-snapshot boilerplate duplicated (plus a simpler third variant)**

*Locations:* `looplab/cli/inspect_cmds.py:898-927`, `looplab/cli/inspect_cmds.py:1372-1391`, `looplab/cli/inspect_cmds.py:1587-1643`

*Evidence:* cross-run-concepts, cross-run-digest and claims each define a local _snapshot(); cross-run-concepts (898-927) and claims (1587-1643) do the full same dance: p.stat().st_mode; S_ISREG -> (p, p.parent); S_ISDIR -> (p/"<canonical file>", p); compute `canonical = path.absolute() == (base/name).absolute()`; then call project_governed_sources(base, _project, include_concepts=..., source_names=..., source_paths=...) with the file-vs-dir split threaded through — ~25 lines each, differing only in the canonical filename (concept_capsules.jsonl vs lessons.jsonl) and the projection body. cross-run-digest's _snapshot (1372-1391) is a simpler dir-only variant (no S_ISREG branch, fixed source_names). The partial-source WARNING rendering that follows is also near-duplicated across cross-run-concepts (937-944), cross-run-search (1436-1452), atlas (1505-1526) and claims (1672-1685).

*Recommendation:* Extract a resolve_memory_source(p, canonical_name) -> (path, base, source_names, source_paths) helper and a render_source_warnings(receipt) formatter; each command keeps only its _project body.

*Resolution (2026-08-02):* `inspect_cmds.resolve_memory_source(memory_dir, canonical_name, *,
missing_is_dir)` returns exactly the recommended `(path, base, source_names, source_paths)`, and all
three commands keep only their `_project` body. Three now-orphaned `import stat` lines went with it.

The dance is not cosmetic: the file-vs-directory split decides which governed SOURCE the read
declares. A source declared by NAME gets `project_governed_sources`' health/quarantine bookkeeping
applied to a store it recognises; one declared by PATH gets the same locking with no such claim.
Getting that backwards does not fail — it silently changes what "complete" means in the receipt the
operator reads.

`missing_is_dir` is the one behaviour that legitimately differed and is now named rather than
implied: `cross-run-concepts` treats a non-existent argument as a directory so its refusal can name
the capsule file the operator expected, while `claims` refuses outright because it has no useful
answer about a directory that is not there. `cross-run-digest` stays directory-ONLY, which is now an
explicit check on the resolved base rather than a differently-shaped `stat` call.

The warning half was taken narrower than proposed. A `render_source_warnings(receipt)` formatter
would have flattened four DIFFERENT operator-facing sentences into one, and those strings are what an
operator reads to know a count is a lower bound. What was actually duplicated is the extraction, so
`quarantined_claim_counts(claim_source)` owns that and each site keeps its own wording — a receipt
read with the wrong nesting reports 0 quarantined rows and turns "these counts are lower bounds" into
a confident exact answer, in three commands at once.

`tests/test_memory_source_resolution.py` (20) pins canonical-by-name vs other-file-by-path, relative
paths absolutised before they cross into the governance layer, symlink following, a FIFO refused
rather than treated as a directory, the per-command missing-path rule, every receipt shape, and grep
guards that `S_ISREG`/`S_ISDIR`/`rows_quarantined` each appear in exactly one place. Teeth-tested
against five breaks.

#### CT-06 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**Read-only RunTools builder copy-pasted five times across four packages**

*Locations:* `looplab/trust/verify.py:389-401`, `looplab/cli/inspect_cmds.py:392-402`, `looplab/engine/lessons_distill.py:181-185`, `looplab/engine/novelty.py:431-433`, `looplab/serve/report.py:117-119`

*Evidence:* The 5-line pattern `rt = RunTools(); rt.bind_state(state, None); return CompositeTools([rt])` wrapped in try/except-return-None exists five times: trust/verify.py::_verify_tools, cli/inspect_cmds.py::_run_tools_for (whose docstring literally says "mirrors trust.verify._verify_tools"), engine/lessons_distill.py::_reflect_tools, engine/novelty.py (inline), serve/report.py. Two of the five copies document their kinship with a "mirrors …" comment instead of sharing code (trust/verify.py and inspect_cmds; the novelty.py inline copy also degrades to `idea` rather than None), so a change to the degrade-to-None contract or the bind_state(state, parent) signature must be found by grep in five places.

*Recommendation:* Add one helper in looplab/tools (e.g. tools/run_tools.py::readonly_run_tools(state) -> Optional[CompositeTools]) and point all five callers at it. tools/ is importable from trust, engine, serve and cli without layering violations.

*Resolution (2026-08-02):* done as recommended — `tools/run_tools.py::readonly_run_tools(state)`, with
all five callers pointed at it. The four named wrappers (`trust.verify._verify_tools`,
`inspect_cmds._run_tools_for`, `lessons_distill._reflect_tools`, `serve.report._report_tools`) keep
their names and their per-site "why" docstrings — those are load-bearing, and `_reflect_tools` is
called from `lessons_reconcile` too — but their bodies are now two-line delegates. The two "mirrors
…" comments are gone: the kinship is expressed in code.

The five lines were never the point. The contracts they each re-derived were: `bind_state(state,
parent)` takes a SECOND argument (`tools/_base.py` — a provider that implements the hook and omits it
raises `TypeError` at dispatch), and a build failure degrades to `None` rather than raising because
every caller has a plain non-agentic path. `tests/test_readonly_run_tools.py` (11 tests) pins both,
plus a narrow grep guard on lone-`RunTools` composites (multi-provider composites are a different
thing and stay put) and a source check that each wrapper is still a delegate.

The novelty gate's inline copy degraded differently — it keeps its proposal instead of making a plain
call — and that stayed, now as an explicit `if tools is None: return idea`. Teeth-testing proved WHY
it has to be explicit: deleting the guard was SILENT against a stub client, because the try/except
below catches the failure either way. Against a real client `agentic_struct(client, None, …)` does
not raise — it pays for a round-trip with no tools bound, i.e. exactly the blind, summary-only
adjudication that `novelty_mode="llm"` exists to replace. The test now asserts `agentic_struct` is
never REACHED.

Four deliberate breaks in total (drop `parent` from `bind_state`; narrow the except so `MemoryError`
escapes; delete the novelty guard; re-inline one wrapper's copy).

#### CT-07 · MEDIUM · duplication · effort: small — **RESOLVED (2026-08-02)**

**Five hand-written late-binding monkeypatch shims with identical bodies**

*Locations:* `looplab/cli/run_cmds.py:49-63`, `looplab/cli/export_cmds.py:21-26`, `looplab/cli/inspect_cmds.py:385-389`, `looplab/bench.py:23-29`

*Evidence:* The pattern `def X(*args, **kwargs): from looplab import cli; return cli.X(*args, **kwargs)` — the seam that keeps monkeypatch.setattr("looplab.cli._engine"/… ) working — is written out five times: run_cmds._engine, run_cmds.make_llm_client, export_cmds.make_llm_client, inspect_cmds._make_llm_client, bench._engine. Each carries its own paragraph re-explaining the same freeze-at-import hazard. The seam itself is intentional (documented in CLAUDE.md-adjacent docstrings); the quintuplication is not.

*Recommendation:* One factory in the cli package, e.g. `def _late(name): def call(*a, **k): from looplab import cli; return getattr(cli, name)(*a, **k); return call`, then `_engine = _late("_engine")` etc., with the why-comment written once at the factory.

*Resolution (2026-08-02):* done, as `looplab/core/latebind.py::late_bound(module, name)` — `core`
rather than the cli package, because `looplab/bench.py` needs the same shim and importing
`looplab.cli` at its module scope would pull the whole Typer command surface into a module that today
has no CLI dependency at all. `late_bound` names its target by STRING and imports inside the call, so
`core` gains no dependency either (and it imports nothing above itself).

Four sites are now one-liners (`_engine = late_bound("looplab.cli", "_engine")`, …). The fifth,
`inspect_cmds._make_llm_client`, is NOT identical and stays a function: it COMPOSES the seam, handing
the builder to `make_llm_client_for` as a factory argument. It now passes `late_bound(...)` as that
factory, and its docstring says why it differs.

The five paragraphs became one, at the factory, explaining the actual hazard: `from looplab.cli
import _engine` binds the object that exists at import time, so a test patching `looplab.cli._engine`
patches an attribute the frozen copy no longer reads — and the command then drives the REAL engine
against a test that believed it was offline, passing, having tested nothing.

`tests/test_cli_shared_indirection.py` pins call-time resolution at all five sites, verbatim argument
forwarding, and a grep guard that no module hand-writes `from looplab import cli` inside a forwarding
function again. Teeth-tested by freezing the shim, by re-inlining one, and — after a first attempt at
the diagnostics break turned out to be call-time resolution in disguise — by a genuine module-scope
`from looplab.cli import make_llm_client as _FROZEN_FACTORY`.

#### CT-08 · MEDIUM · inconsistency · effort: small — **RESOLVED (2026-08-02)**

**config.snapshot.json loaded three different ways with three failure semantics**

*Locations:* `looplab/cli/run_cmds.py:219-238`, `looplab/cli/run_cmds.py:661-664`, `looplab/cli/run_cmds.py:820-823`, `looplab/cli/inspect_cmds.py:405-427`

*Evidence:* run_cmds has the strict loader _settings_from_config_snapshot (BadParameter on any corruption), but resume (661-664) and finalize (820-823) each duplicate the same 4-line `settings = Settings(); snap = run_dir/"config.snapshot.json"; if snap.exists(): settings = _settings_from_config_snapshot(snap)` prologue; and inspect_cmds independently re-implements snapshot loading as _settings_for_run with SILENT fallback to ambient Settings on any exception (its docstring justifies the ambient fallback for diagnostics, but it re-parses the JSON itself instead of composing the shared loader). Three call shapes for the same file means the "which settings does this command actually run with" question has three answers depending on entry point.

*Recommendation:* One `load_run_settings(run_dir, *, strict: bool) -> Settings` in the cli package (or core/appconfig): strict=True raises BadParameter (run/resume/finalize), strict=False degrades to ambient (diagnostics). Kill the two inline duplicates.

*Resolution (2026-08-02):* done exactly as recommended — `looplab/cli/__init__.py::load_run_settings(
run_dir, *, strict)`, alongside the other shared CLI helpers. The strict loader moved there with it;
`run_cmds`' three call sites are now `load_run_settings(run_dir, strict=True)` and `inspect_cmds`'
re-implementation is `strict=False` on the same function instead of a second parse.

`strict` names the two semantics that are actually legitimate, and the docstring says why each is:
strict is for the LIFECYCLE commands, where a corrupt snapshot is an operator-facing input error and
falling back to ambient would silently drop run-only flags (require_approval, trust_mode, confirm_*,
eval_trust_mode, backend, …) — e.g. finishing a paused, not-yet-approved run without any approval.
Lenient is for read-only diagnostics, where the snapshot supplies endpoint/model provenance and an
unreadable one must not stop someone reading a partially-written run.

One behaviour was clarified rather than preserved verbatim: an ABSENT snapshot is ambient Settings
under both modes. That matches what all three call shapes already did (each guarded with
`if snap.exists()`), and the one caller that genuinely requires the file —
`_pending_finalization_inputs` — keeps its own check, which names BOTH snapshots in one message.

`tests/test_cli_shared_indirection.py` pins both modes against five corruption shapes, the
absent-file rule, that recovery still refuses a missing snapshot by name, and a grep guard on the
PARSE (`settings_from_snapshot` outside `cli/__init__.py`) rather than on the filename — commands
legitimately still name `config.snapshot.json` to write it, to check it exists, and to print it
verbatim. Teeth-tested by making strict degrade silently and by flipping one lifecycle call to
lenient.

#### CT-09 · MEDIUM · mergeable-entities · effort: medium

**trust/verify.py vs trust/verifier.py: two near-namesake verifier modules with overlapping machinery**

*Locations:* `looplab/trust/verify.py:371-374`, `looplab/trust/verify.py:404-462`, `looplab/trust/verifier.py:218-260`, `looplab/trust/lesson_guard.py:70-88`

*Evidence:* verify.py (D8 memo-claim verifier) and verifier.py (advisory criteria scorer) are separate modules whose names differ by two letters and whose internals overlap: near-identical output models (_VerdictOut{verdicts,notes} vs _Verdicts{verdicts,rationales}), the same agentic_struct(client, tools, msgs, Model, parser=…, loop_opts={"max_turns": 15}, fallback=parse_structured) invocation, an overlapping verdict vocabulary ('unclear' is shared; verifier.py's ordinal strong_no..strong_yes scale maps 'supported'/'unsupported' only as normalization aliases), and verify.py builds its own read-only RunTools while verifier.py accepts a caller-supplied `tools` parameter. lesson_guard.py adds a third _evidence_text whose comment says it "mirrors trust/verify.py::_evidence_text". The two modules do serve different purposes (evidence-identity checking vs repeated ordinal sampling), so a full merge is wrong — but the naming and the duplicated LLM-judging plumbing are a real navigation/maintenance hazard: grep for "verifier" lands in both, and a change to the judge-call contract (max_turns, fallback, parser) must be made in 2-3 places.

*Recommendation:* Rename one module (e.g. verify.py -> memo_verify.py, keeping a _LAYOUT/back-compat alias as the repo already does for renames) and extract the shared judge-call helper (structured-judge invocation with agentic fallback) into one place both import.

*Resolution (2026-08-03, completed 2026-08-04).* Both halves are done: the duplicated judge-call
plumbing first, then the rename.

`looplab/trust/judge.py::structured_judge(client, msgs, model, *, parser, tools=None)` is now the one
judge-call contract: agentic through `agentic_struct` when tools are supplied with the plain parse as
its FALLBACK, plain `parse_structured` otherwise, and `JUDGE_MAX_TURNS = 15` as a constant instead of
two literals. Both verifiers call it. What each keeps is its own FAILURE policy, because those are
deliberately different — `verify_memo` falls back to its deterministic verdicts, `verify` drops the
sample and averages the rest — so the helper owns no `except` at all.

**One piece of the finding's evidence is wrong.** The two `_evidence_text` helpers share a NAME, not
an implementation: `verify.py`'s takes a CLAIM dict plus a frozen source map and emits bounded
REDACTED JSON, because a memo claim can cite external URLs that must never reach the model
unredacted; `lesson_guard.py`'s takes a distilled LESSON record and renders node outcomes as prose
for a judge prompt. Merging them would drag a redaction contract into a path that has no URLs. The
comment claiming one "mirrors" the other was itself the defect — it sent readers looking for a
duplication that is not there — and is corrected to say what actually differs.

**The rename (2026-08-04) — done, and the constraint the earlier deferral named is what it is built
on.** `trust/verify.py` is now `trust/memo_verify.py`; `trust/verifier.py` keeps its name. A grep for
"verifier" no longer lands in two files whose names differ by two letters and whose purposes a reader
had to open them to distinguish.

Both retired spellings — the dotted `looplab.trust.verify` and the flat pre-split
`looplab.verify` — resolve through a new `_RENAMED` map in `looplab/__init__.py`, checked BEFORE the
`_LAYOUT` lookup so a retired name can never fall through and rebuild the path it used to live at. It
routes through the SAME `_CompatLoader` as every other alias, which is the entire point: old and new
are ONE module object.

That identity is the contract, not importability. These modules are patch seams —
`engine/research_cadence.py` documents monkeypatching `verify_memo` to intercept the live call, and
several tests do — so the obvious shim (a `verify.py` that re-exports with `from … import *`) is the
one implementation that must not be used: a star-import binds by VALUE, so the import succeeds, the
patch appears to apply, and the original function still runs. Nothing raises. The teeth harness
includes exactly that wrong fix, and it is caught.

`_LAYOUT` keeps its own contract — canonical module STEM -> package — so `verify` left it and
`memo_verify` took its place; both of its two-way registry guards (`test_every_layout_entry_exists_at_
its_canonical_path`, `test_no_module_missing_from_layout`) still hold. Six in-repo call sites, four
test modules and seven comments were repointed, and a source scan now fails if any file names the
retired path again — reintroducing the navigation cost from the other side, by sending a reader to a
file that is not there.

Pinned by four tests in `tests/test_structured_judge.py`: both retired spellings ARE the canonical
module object (parametrized), a patch through a retired spelling actually reaches the live module
(exercised, not inferred from identity), the canonical `__spec__.name` survives an alias import (the
restamping hazard the flat aliases already guard, which silently no-ops `importlib.reload`), and the
no-stale-path source scan. Teeth-tested against 5 breaks — the star-import shim, each alias dropped,
the `_RENAMED` hook removed, and an alias pointed at the wrong module — all biting.

#### CT-10 · LOW · inconsistency · effort: medium

**Three finding-dict vocabularies for the same 'trust flag' concept, adapted inline at the consumer**

*Locations:* `looplab/trust/leakage.py:45-63`, `looplab/trust/reward_hack.py:224-336`, `looplab/trust/critic.py:16`, `looplab/trust/harden.py:78-89`, `looplab/engine/evaluate.py:785-795`

*Evidence:* leakage detectors return {"detector": …, "leak": bool, …}; reward_hack returns [{"signal", "detail", "method", "confidence"}] and ExploitSuite.scan returns [{"signal", "detail"}]; critic returns [{"issue", "detail"}]. engine/evaluate.py then normalizes them by hand: `sigs.append({"signal": "data_leakage:" + f["signal"], …})` and `sigs.append({"signal": "critic:" + c["issue"], …})` while reward_hack rows pass through unchanged. The signal namespace ("data_leakage:", "critic:") — which is_hard_signal keys gating decisions on — is thus assembled at the call site rather than owned by the detectors. This is contained (one consumer) but means any new consumer of the trust detectors must re-invent the same mapping (critic.py's module docstring does name the `critic:hardcoded_metric` gate signal, but the mapping itself lives only in evaluate.py).

*Recommendation:* Define one lightweight finding shape (signal, detail, method, confidence, plus optional detector-specific fields) in trust/, have each detector emit its already-namespaced signal (data_leakage:fit_on_test, critic:hardcoded_metric), and reduce evaluate.py to concatenation. The dict-based events stay wire-compatible.

*Resolution (2026-08-03):* `looplab/trust/findings.py` owns the shape (`finding(...)`) and the two
gate namespaces (`LEAKAGE_NS`, `CRITIC_NS`). `leakage.code_leakage_findings(src)` and
`critic.critic_findings(idea, code, submission_file=...)` emit already-namespaced rows; `evaluate.py`
concatenates. `code_leakage_scan`/`critique` keep their own richer shapes for callers that want the
line numbers or the raw issues, so no existing test moved.

The reason this is worth doing is narrower than "three vocabularies". Those namespaced strings are
what `events/replay.py::is_hard_signal` keys GATING on — `critic:hardcoded_metric` excludes a node
from selection, every other `critic:` issue stays advisory, and leakage names gate via the
fail-closed default. So the prefix a detector's output landed in decided whether a run could be
gated, and it was minted at the consumer, three files from the detector that knew what it found. A
second consumer would have re-derived the mapping, and drift between the two would be a silent
change to what gates.

`tests/test_trust_finding_namespaces.py` therefore pins the JOIN in both directions — the detectors
emit exactly the strings the gate recognises, AND the gate's classification of those strings is
unchanged — rather than either side alone. It also carries a tree-wide grep guard, because the
defect is not specific to `evaluate.py`: any consumer that string-builds a gate namespace has
re-created the split. Teeth-verified against 10 breakages, including both directions of the gate
drift (a broad critic warning starting to gate, and leakage evidence stopping).

One fixture had to be corrected: `metric = 0.99` does not trip `critic:hardcoded_metric` — the rule
requires a QUOTED literal (`{"metric": 0.99}`) with nothing computing it — so the first version of
the gating test asserted against an empty signal set and would have proved nothing.

#### CT-11 · LOW · dead-code · effort: small

**Dead `state` parameter in _persist_node_concepts**

*Locations:* `looplab/cli/inspect_cmds.py:108-134`, `looplab/cli/inspect_cmds.py:554-563`

*Evidence:* _persist_node_concepts(store, state, raw_tags, …) unconditionally rebinds its second argument at line 134 (`state = fold(events)`) — correctly, per the comment about re-folding inside the mutation transaction — so the value the caller passes is never read. The caller _persist_exact computes `current = fold(current_events)` (line 544) — that fold is still needed by `_retro_tag_finished` — but passes it (line 556) for nothing.

*Recommendation:* Drop the parameter (or rename the local) so the signature stops implying the caller's fold matters; removes the pointless pass (the caller's fold stays — `_retro_tag_finished` consumes it; ~15 direct test call sites also pass a fold).

*Resolution (2026-08-03):* the parameter is gone, and the docstring now says why it will not come
back: the helper re-folds INSIDE the mutation transaction because it needs the tail seq for the CAS
anyway, so a caller-supplied state could only ever be the stale one. The caller's own fold stays —
`_retro_tag_finished` consumes it — and all 15 test call sites were re-pointed rather than left
passing a value into a signature that no longer has a slot for it.

#### CT-12 · LOW · over-engineering · effort: small

**Trust-package library surfaces with no production consumer across multiple review cycles**

*Locations:* `looplab/trust/cv.py:19-62`, `looplab/trust/reward_hack.py:161-221`, `looplab/trust/verify.py:52-56`, `looplab/trust/harden.py:124-146`

*Evidence:* Verified by repo-wide grep: cv.py's kfold_indices/purged_walk_forward/consistent_cv/Evaluator are imported only by tests/test_cv_confirm.py; reward_hack.calibrate_detector + SEED_CALIBRATION_CORPUS only by tests/test_reward_hack.py; verify._source_ref only by tests/test_phase4_verify.py (its own docstring admits it is a test facade). Each is documented as an intentional seam — but docs/17 (2026-07-11, three weeks before this review) already listed the cv splitters as "tested, no live caller", and no adapter has arrived. harden's LLM-hacker plug is similarly unused, and its fallback rule name `exploit_{abs(hash(code)) % 10**6}` (harden.py:125) is nondeterministic across processes (salted str hash), so the same LLM-found exploit would persist under different names in the durable suite.

*Recommendation:* Not deletion-on-sight (the seams are documented), but set a decision point: wire the cv splitters behind a temporal adapter or move them to a docs/example; if calibrate_detector is the operator's harness, expose it (a `looplab calibrate-detector` subcommand is ~15 lines); replace hash() with a content digest (hashlib) in _derive-pattern naming.

*Resolution (2026-08-03):* the DEFECT is fixed and the decision point is now a test rather than a
note.

`harden`'s LLM-found rule naming used `abs(hash(code))`, and `str.__hash__` is SALTED per
interpreter. That is not cosmetic: the name goes into a DURABLE rule suite later runs read back, so
the same exploit landed under a new name on every process — the suite accumulated one rule per
sighting and `ExploitSuite.add`'s idempotence could never fire. `_exploit_digest` is a sha256 prefix;
a test drives the same exploit twice and asserts exactly one rule, and another runs the digest in two
subprocesses with different `PYTHONHASHSEED`s, which is the failure the old code actually had.

The seams themselves are KEPT, and the "decision point" is
`tests/test_trust_seam_status.py::UNCONSUMED_TRUST_SEAMS` — a declared list with a reason per entry,
guarded in both directions: a surface that gains a production caller fails the test and has to leave
the list, and a NEW unconsumed surface has to be added to it deliberately. That is the part worth
having. docs/17 had already recorded "tested, no live caller" for the cv splitters three weeks before
this review restated it, which is what a note in a document does; a list the suite checks cannot go
quietly stale the same way.

Two of the finding's suggestions were NOT taken. Wiring the cv splitters "behind a temporal adapter"
would mean inventing the adapter they are waiting for — the splitters are not the blocked part. And a
`looplab calibrate-detector` subcommand is a new public CLI surface, offered on the finding's own
conditional ("IF calibrate_detector is the operator's harness"), which nothing in the repo confirms;
adding a command nobody asked for is a worse outcome than an unwired function that says why it is
unwired.

#### CT-13 · LOW · duplication · effort: small

**Run-dir existence check re-implemented inline four times despite _require_run_dir**

*Locations:* `looplab/cli/__init__.py:138-146`, `looplab/cli/run_cmds.py:640-643`, `looplab/cli/run_cmds.py:762-764`, `looplab/cli/run_cmds.py:781-783`, `looplab/cli/run_cmds.py:925-927`

*Evidence:* _require_run_dir exists precisely to turn a missing events.jsonl into a clear exit-2, yet resume, stop, finalize and repair-log each re-write the `if not (run_dir / "events.jsonl").exists(): typer.echo(...); raise typer.Exit(2)` block inline with slightly different wording; stop/finalize then construct EventStore themselves — exactly what _require_run_dir returns. resume also builds a throwaway EventStore at 648 solely for _require_healthy_log and a second one inside _engine.

*Recommendation:* Use _require_run_dir (optionally with a `hint:` message parameter for the resume-specific guidance) in all four; have it optionally run the health check too, since every mutating caller pairs the two.

*Resolution (2026-08-03):* both parameters, as recommended. `_require_run_dir(run_dir, *, hint=…,
healthy=False)` is the single prologue and `_require_healthy_log` moved next to it in
`cli/__init__.py`. Five commands now call it: `resume` / `stop` / `finalize` / `approve` with
`healthy=True`, `repair-log` without.

The user-visible part was the drift that mattered: `stop` and `finalize` printed a bare
`no run found at <dir>` — no `(no events.jsonl)`, no guidance — so the same operator mistake got a
less actionable answer depending on which verb was typed. `hint:` is what let the check be shared
without flattening that advice into one generic sentence.

Two things stay deliberately outside the flag:

* `run` still calls `_require_healthy_log` directly. It creates the run dir, so there is no run to
  *require* yet — folding it in would mean a `healthy=` on a path that must not exist-check at all.
* `repair-log` must NOT pass `healthy=True`. A mid-file corruption is its INPUT; failing closed on
  one would make the only documented recovery path unreachable exactly when it is needed. The
  opt-out is now a named decision with a test on it rather than an omission.

`tests/test_cli_shared_prologues.py` covers it, including driven cases (a real corrupted log is
refused with `healthy=True` and repaired without it) rather than source assertions alone.

#### CT-14 · LOW · duplication · effort: small

**Optional-LLM-client construction pattern repeated across six diagnostics**

*Locations:* `looplab/cli/inspect_cmds.py:448-453`, `looplab/cli/inspect_cmds.py:574-579`, `looplab/cli/inspect_cmds.py:649-656`, `looplab/cli/inspect_cmds.py:724-728`, `looplab/cli/inspect_cmds.py:786-792`, `looplab/cli/inspect_cmds.py:806-812`

*Evidence:* The block `settings = _settings_for_run(run_dir, model); try: client = _make_llm_client(settings) except Exception as e: typer.echo(f"(no LLM endpoint: {e}; …fallback…)")` appears in _concept_map_for, concept-coverage, asset-brief, board-dedup, novelty-recall and lesson-guard, differing only in the fallback message. board-dedup even runs it twice per invocation (once inside _concept_map_for, once for hypothesis tagging).

*Recommendation:* One helper `_optional_client(run_dir, model, fallback_note) -> (settings, client|None)`; commands keep only their message.

*Resolution (2026-08-03):* `_optional_client(run_dir, model, fallback, *, unavailable="no LLM
endpoint")` with exactly that signature, used by the FIVE diagnostics that degrade — concept-map,
concept-coverage, board-dedup's hypothesis tagging, novelty-recall and asset-brief. Each keeps only
its fallback wording.

`lesson-guard` is the sixth site in the finding and deliberately did NOT move: it is LLM-only and
`raise typer.Exit(1)`s instead of degrading. That is a different CONTRACT, not a different message —
folding it in would turn "I could not check this" into "checked, nothing found", which is the one
answer a guard must never give. A test pins the exclusion so it reads as a decision.

#### CT-15 · LOW · under-decomposition · effort: medium

**_engine builder: 191 lines with duplicated ForesightPanelResearcher wiring in two branches**

*Locations:* `looplab/cli/__init__.py:302-493`, `looplab/cli/__init__.py:377-382`, `looplab/cli/__init__.py:396-401`

*Evidence:* _engine assembles the whole object graph — profile validation, calibration lane, researcher-wrapper selection (surrogate/foresight/panel × unified/non-unified), onboarder, strategist, deep researcher, report writer, proxy scorer, embedder, lesson abstractor — in one function. The ForesightPanelResearcher constructor call with its five getattr-defaulted kwargs (k, tools, min_confidence, verify_score, verify_samples) is written twice, once in the non-unified branch (377-382) and once in the unified branch (396-401); the guard conditions differ by one clause. A drift between the two copies would silently change unified-vs-plain behavior.

*Recommendation:* Extract at least _wrap_researcher(researcher, developer, settings, ftools) returning the wrapped pair, with the Foresight ctor written once; consider further splitting strategist/deep-research/report construction into small builders. Keep the function's name and signature — ~10 tests patch looplab.cli._engine.

*Resolution (2026-08-03):* `_wrap_with_foresight_panel(researcher, settings, ftools)` writes the
constructor once, and `_foresight_panel_applies(settings, researcher)` writes the guard once.
`_engine` keeps its name and signature, so the ~10 tests that patch `looplab.cli._engine` are
untouched.

The one-clause difference between the two guards is gone rather than preserved: the non-unified
branch tested `backend == "llm"` and the unified branch did not, because `_unified` already implies
it. Folding the clause into the shared predicate is equivalent and says plainly that the two paths
were never checking different things. `tests/test_cli_lifecycle_triage.py` pins that exactly one
`ForesightPanelResearcher(` call exists, that `_engine` no longer re-derives the guard, and — as
behaviour, not shape — that the guard still yields to an explicitly-configured `researcher_panel > 1`
so opting into the k-NN panel is never silently overridden by the foresight default.

The further split of strategist/deep-research/report construction was not done; the duplicated
constructor was the defect, and those are each written once already.


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

#### XP-02 · MEDIUM · duplication · effort: medium — **RESOLVED (2026-08-02)**

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

*Resolution (2026-08-04):* Both halves done; the first half of the finding was already partly stale.

**Layering.** The calibration-profile constants had already moved to `search/speculation_calibration.py`
and the orchestrator already imports them DOWNWARD, so that half needed nothing. One upward import
remained — `engine.finalize.incomplete_finalize_scope`. It was not alone: it sits in a cluster of five
pure functions over an event list (`_adjacent_claim`, `_finalize_begun`, `_scope_has_step`,
`finalize_scope_quiescent`, `incomplete_finalize_scope`) that read event types and nothing else. That
cluster is a READ SIDE, not orchestration, so it moved verbatim to `looplab/events/finalize_scope.py`,
which `search` may import downward. `engine/finalize.py` re-exports all five, so the existing engine
and `serve/run_commands.py` import sites — and every monkeypatch seam through them — resolve to the
SAME objects; `_LAYOUT` gained the module. A test now pins the direction for this module specifically,
alongside the package-wide `tests/test_agents_search_direction.py`.

**Digest.** The comment's own recommendation is implemented, but not as it was written. "Exact hashes
of only execution-affecting FILES" cannot be decided per file — every shipped `.py` can affect
execution, which is exactly why the rglob was total. The separable axis is not which files but which
BYTES: the manifest now hashes `ast.dump(ast.parse(raw))` per file, so comments, blank lines, line
endings and rewrapping vanish while everything that can change what the process does survives. The
manifest stays total, so nothing is silently excluded.

Two decisions worth recording. Docstrings are deliberately KEPT execution-affecting — a tool's
docstring is its agent-facing description, so editing one really can change a run; that is a narrower
claim than "review-only", and the test says so. A file that does not parse falls back to its raw
bytes, which can only over-revoke, never under-revoke.

The schema is bumped to `looplab.speculation-implementation/v2` rather than reused: the same tree now
hashes differently, so receipts issued under v1 are revoked ONCE by this change. That is correct, and
it is the last time a comment edit will do it.

One defect was found in this fix's own first draft and is worth naming, because the test that caught
it is the one worth keeping: hashing the parsed tree while still recording `"bytes": len(raw)` put the
byte-for-byte sensitivity straight back into the manifest through the OTHER field, and a test that
only exercised `_semantic_source` passed anyway. The row is now minted by one `_manifest_entry`
helper whose every field derives from the same bytes, and the guard asserts on the ROW.

#### XP-08 · LOW · dead-code · effort: small — **RESOLVED (2026-08-02)**

**Seven verified-dead functions, including a ~60-line unused locking context manager**

*Locations:* `looplab/engine/claims.py:1577`, `looplab/engine/governance_health.py:557`, `looplab/serve/scope_report.py:601`, `looplab/serve/routers/reports.py:418`, `looplab/search/concept_graph.py:1008`, `looplab/search/concept_graph.py:1020`, `looplab/search/card_selection.py:637`

*Evidence:* Repo-wide grep (looplab/, tests/, docs/, ui/) finds zero references to: claims.locked_claim_evidence_snapshot (a ~60-line ExitStack/lock context manager whose presence misleadingly implies a locking protocol that nothing exercises), governance_health.claim_governance_snapshot, scope_report.build_digest, reports._scope_action_lease_marker_exists, concept_graph._normalized_rename_map and _canon_set, card_selection._explored_concepts. Several are one-line delegation wrappers left behind by refactors (e.g. _normalized_rename_map just calls normalized_concept_renames). adapters/kaggle_dl.check_auth also has no code callers but is a documented operator command in docs/MLEBENCH.md:54, so it is NOT dead.

*Recommendation:* Delete the seven functions; if locked_claim_evidence_snapshot documents an intended future locking protocol, move that intent to docs/an ADR rather than shipping dead lock machinery.

#### XP-09 · LOW · inconsistency · effort: small

**Metric formatting implemented three ways with divergent output semantics**

*Locations:* `looplab/events/digest.py:202`, `looplab/serve/tui_format.py:28`, `looplab/serve/scope_report.py:83`

*Evidence:* digest.fmt_num renders None as '?' and uses %.4g; tui_format.fmt_metric renders None/NaN as '—', switches to exponent form outside [1e-3, 1e6) and takes a precision arg (documented as 'the Python twin of util.js fmt'); scope_report._fmt_metric renders None as '—' with %.5g. The same best-metric value can therefore print differently in agent-facing digests, the TUI, and cross-run scope reports, and a formatting fix must be found and applied three times (four counting ui/'s util.js twin).

*Recommendation:* Keep one canonical formatter (tui_format's is the most complete) in core or events and have the other call sites delegate, parameterizing the None sentinel if the '?' vs '—' distinction is intentional.

*Resolution (2026-08-03):* `core/fitness.format_metric(value, *, absent, precision, exponent,
absent_nan)`, with `tui_format`'s rule as the DEFAULT (it was the most complete, as the finding says)
and the other two delegating with explicit keywords. It lives beside `is_usable_metric` /
`finite_metric` because "what counts as a metric" and "how a metric reads" are the same question
asked twice.

The '?' vs '—' distinction IS intentional and is now named: the digest is PROMPT text, and a model
must not be able to read a dash as a value. Two further differences turned out to be real and are
parameters rather than accidents — the digest keeps `%.4g` with no exponent switch because that
output is frozen prompt text, and it passes `absent_nan=False` because `fmt_params` routes raw param
VALUES through the same helper, where turning a NaN into "unknown" would hide that the parameter
itself diverged.

One behaviour changed, deliberately: the scope report used to print the bare text `nan` for a
diverged metric in a table that already spells absence `—`.

The `ui/src/format.js::fmt` twin the finding counts as a fourth stays a copy — it cannot import
Python — but a test now pins the five decisions it must keep in step. That test also RECORDS a
divergence rather than papering over it: `%g` and JS `Number.toString` disagree on rendering the same
result (`1e+06` vs `1000000`, `1.00e-07` vs `1.00e-7`). It predates the extraction on both sides and
is display-only, so it is documented instead of "fixed" — changing either would move numbers an
operator is already used to reading.

#### XP-10 · LOW · duplication · effort: small

**Registry sprawl verdict: the 9 registries should stay separate, but their 6 guard tests copy-paste the source-scan skeleton**

*Locations:* `tests/test_role_output_contract.py:26`, `tests/test_prompt_keys.py:21`, `tests/test_event_types.py:83`, `tests/test_hint_forwarding.py:49`, `tests/test_signal_delivery.py:21`, `tests/test_background_appendable.py:1`

*Evidence:* BACKGROUND_APPENDABLE/SETUP_THREAD_APPENDABLE (frozensets of event types), TASK_OPTIONAL_HOOKS/DEVELOPER_OUTPUT_ATTRS/RESEARCHER_ACTION_ATTRS/RESEARCHER_HINT_ATTRS (attr-name tuples), PROMPT_KEYS (override file keys), SIGNALS (SignalRoute dataclasses) and CONTROL_EVENTS (HTTP allow-list) guard different seam kinds with type-appropriate shapes — a uniform runtime registry mechanism would add abstraction without value since the per-seam scan heuristics ARE the value. What is duplicated is the test-side skeleton: each guard test independently reimplements 'rglob looplab/*.py, read with BOM-tolerant encoding, regex/AST-extract names, build {name: files}' (test_role_output_contract._scan, test_prompt_keys._call_keys, test_event_types' ast walk, test_hint_forwarding's ast walk, etc.), including repeated per-file gotchas like the utf-8-sig BOM note.

*Recommendation:* Do NOT unify the registries themselves. Extract a small shared tests helper (e.g. tests/_source_scan.py with iter_sources() and scan(pattern)->dict) so the six guard tests share the file-walking/decoding logic while keeping their bespoke extraction heuristics.

*Resolution (2026-08-03):* `tests/_source_scan.py` with exactly that API — `iter_sources()`,
`iter_trees()` and `scan(pattern) -> {name: {file, …}}` — and the registries untouched, as the
finding directs. The per-seam extraction heuristics stay in their own files; only the walk moved.

The scope is wider than the six named, because the survey found **fifteen** tests rglob-ing the
package, and the copies had already diverged on the detail that decides whether a scan runs at all:
**four different decodings** were in use (`utf-8` and `utf-8-sig`, each with and without
`errors="replace"`). That is not cosmetic. At least one tracked file carries a BOM, and `ast.parse`
on a plain-`utf-8` read of it raises `SyntaxError: invalid non-printable character U+FEFF` — so a
scanner written with the wrong spelling does not miss a finding quietly, it dies on an unrelated
file. **Three of the fifteen still parsed with plain `utf-8`** and were one BOM away from that.
`utf-8-sig` + `errors="replace"` is the one spelling that works for both scan kinds, and it is now
the only one.

Two smaller things the shared walk fixes rather than preserves: the results are SORTED (an unsorted
rglob reports the same offenders in a different order per filesystem, which reads as a flapping
test), and `iter_trees` sets `filename=` so a SyntaxError names the file it came from.

Two of the finding's six were miscounted and are deliberately left alone:
`tests/test_background_appendable.py` does no source walk at all, and `tests/test_signal_delivery.py`
reads a handful of NAMED files — a different, correct shape, since its point is that one specific
wiring line exists in one specific file. A test pins that exclusion so it reads as a decision.

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

*Resolution (2026-08-03):* `core/numeric.py`, re-exported from `events/digest.py` — the recommended
shape, done together with RA-08 because they are the same defect seen from two sides: the estimator
was in the wrong package, and `runtime/proxy.py`'s import of it was the wrong edge.

`param_distance` deliberately did NOT move. It is the exact metric the novelty gate uses to say two
experiments are "near", which is a statement about a run rather than arithmetic — a projection
concern, and the reason `digest.py` still exists as more than a shim.

The re-export is asserted to be the SAME object (`digest.knn_idw is numeric.knn_idw`), not a
convenience wrapper: several modules and tests still import through `events.digest`, and a
re-implementation there could drift from the one the predictors call. The zero-distance short-circuit
that scans the whole top-k — the subtle part, and the one a "simplification" would lose — is driven
by a test at its new home rather than left to the comment.


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

*Resolution (2026-08-03):* `api.js::submitCommand(promise, labels, onToast)` is the other half of the
presentation contract `commandFeedback` already owned: `commandFeedback` explains a RECORD,
`submitCommand` explains an ATTEMPT — including the attempt that never produced a record because the
transport threw. It toasts exactly once on every outcome and RETURNS the feedback, because the two
things a call site still has to do are gate a success-only side effect (clearing an input draft) and
roll an optimistic update back on anything else.

The transport arm is deliberately an `error` feedback, never a success: a network failure that
returned `success` would clear the operator's draft. `labels.transport` covers the surfaces that
WITHHOLD the thrown message (the Inspector reset menu says only "… could not be submitted. Try
again."); everything else keeps the `${failure}: ${message}` shape the panels already used.

Converted: `TrustPanel.quarantine`, `QueuePanel.cancel`, `ResearchPanel.steer`, `_CardKanban.addCard`,
`_HypothesisFallback.add`/`.abandon` (panels.jsx) and `ResetBtn.doReset` / `StagePipeline.rerun`
(Inspector.jsx). Deliberately NOT converted: the sites that interleave a mount/generation check
BETWEEN the await and the feedback (`ConfigPanel.restart`, `setEvalCeiling`, the card-board
mutations) — a helper that takes the promise cannot run a staleness check in the middle, and
threading one in would buy the de-duplication back at the price of the guard those sites exist for.
`RunView.onNodeAction` is already de-duplicated behind its own `checkedCommand`, which has a
different contract (it THROWS so one outer catch covers a whole switch).

Covered by `ui/test/submitCommand.test.js` (14 tests): one toast per outcome including the throw, the
transport arm never reporting success, `labels.transport` honoured on a throw and ignored when the
server answered, no rethrow out of an event handler, plus source guards that the call sites use it.

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

*Resolution (2026-08-03):* Deleted. `AgentReport` (the component actually rendered by the Trace tab)
is untouched; the removal is verified against the whole of `ui/src` and `ui/test` rather than by
reading `TABS`, because a JSX reference can live anywhere.

#### UI-11 · LOW · inconsistency · effort: small

**runApiPath is documented as 'one constructor for every owner-style per-run endpoint' but ~15 endpoints in the same file bypass it**

*Locations:* `ui/src/api.js:14-24`, `ui/src/api.js:685`, `ui/src/api.js:798`, `ui/src/api.js:825`, `ui/src/api.js:1571-1573`, `ui/src/api.js:1610-1639`, `ui/src/api.js:2184-2189`

*Evidence:* The comment on runApiPath (line 14) states the identity-boundary rationale, yet getRunGeneration, submitRunCommand, getRunCommand, retryRunCommand, resetRun, assignRun, renameRun, runComments, commentHistory, createRunReview/list/revoke, spanDetail and others inline `/api/runs/${encodeURIComponent(runId)}/…` template literals in the same module. All still encode the id, so this is a consistency/documentation debt rather than a safety bug — but the invariant the comment promises is unenforced.

*Recommendation:* Convert the inline sites to runApiPath/runNodeApiPath (mechanical change), or soften the comment; a simple grep-based test could then enforce it like the repo's other registry-guard tests.

*Resolution (2026-08-03):* Converted, not softened. Nineteen inline sites now go through
`runApiPath` — fourteen in `api.js` (`getRunGeneration`, `submitRunCommand`, `getRunCommand`,
`retryRunCommand`, `resetRun`, `assignRun`, `renameRun`, `saveRunConfig`, the three review-link
calls, `runComments`, `commentHistory`, `spanDetail`) plus five outside it (`timelineModel`,
`AssistantBar`, `RunCompare`, `RunView` x2, `panels`). The suffix stays explicit at each call site,
which is what the constructor's own comment asks for; only the identity boundary is centralised.

`ui/test/runApiPathBoundary.test.js` is the grep guard the finding asked for, plus the behaviour it
protects: a run id carrying `#`, `/`, `%2F` and `?` must come back as ONE encoded path segment with
no fragment and no query. That is not hypothetical — run ids are filesystem names, so `#` truncates
the request path and a literal `%2F` decodes into a second segment that reaches a different route.
Every converted site already encoded correctly, so nothing was broken; the point is that the next
author copies whichever neighbour they land on.

#### UI-12 · LOW · duplication · effort: small

**Four coexisting request-timeout wrappers**

*Locations:* `ui/src/requestDeadline.js:1`, `ui/src/api.js:743-775`, `ui/src/api.js:1509-1510`, `ui/src/AssistantBar.jsx:60`

*Evidence:* deadlineRequest (requestDeadline.js), commandFetch's own AbortController+Promise.race deadline (api.js:743, justified by a comment about body-read lifetime), deadlineGet (api.js:1509, a thin deadlineRequest wrapper), and AssistantBar's boundedRequest (a one-line deadlineRequest.promise alias) all implement 'fetch with a deadline'. Components pick among them ad hoc.

*Recommendation:* Keep deadlineRequest and commandFetch (distinct semantics), fold boundedRequest into deadlineRequest usage, and document when each applies.

*Resolution (2026-08-03):* `deadlineRequest` and `commandFetch` kept, as the finding says; the choice
between all four shapes is now written down in `requestDeadline.js`, where they live.

`boundedRequest` was **moved rather than inlined**, which is a deliberate deviation from "fold into
deadlineRequest usage". It has 20 call sites in `AssistantBar`, none of which need the handle;
spelling `deadlineRequest(read, ms).promise` at each would be longer at every site and would put the
12s default back into 20 places. The problem was never that the alias existed — it was that it was a
PRIVATE one-liner, which is why `CollabPanel` had already grown its own (`boundedLinkRequest`, with
the same bound spelled `12_000` instead of `12000`). One exported `boundedRequest` plus a shared
`DEFAULT_REQUEST_TIMEOUT_MS` removes the drift without moving the verbosity to the callers.

`CollabPanel` keeps its named local because its three call sites genuinely want the handle (they
cancel a stale list/create on a newer one) — but the bound now comes from the shared constant. The
guard test enforces exactly that distinction: a local wrapper is fine, a local NUMBER is not.

`commandFetch` is pinned as deliberately NOT built on the primitive. It bounds a durable COMMAND
submission where the body read is part of the operation, and its timeout must surface as a typed
`COMMAND_REQUEST_TIMEOUT` the command lifecycle can classify; collapsing it would turn a submission
timeout into a generic `TimeoutError` the retry path cannot act on.

#### UI-13 · LOW · duplication · effort: small

**Three independent toast implementations**

*Locations:* `ui/src/RunView.jsx:643-649`, `ui/src/AssistantBar.jsx:431`, `ui/src/Settings.jsx:1`

*Evidence:* RunView.showToast (5s timer, .toast div), AssistantBar.flash (5s timer, mountedRef guard, .cmdbar-toast variants including run-change suppression logic at 1569), and Settings' own toast state each re-implement the timer-reset pattern; toastTimerDiscipline.test.js exists to police the subtle clear-previous-timer bug that this duplication invites.

*Recommendation:* One useToast hook (timer reset, unmount guard) with presentation left to callers.

*Resolution (2026-08-03):* `ui/src/useToast.js` exports `useToast(ms = 5000) -> [toast, show,
clearToast]`, used by `RunView` (5s), `AssistantBar` (5s) and `Settings` (2.5s). Presentation stays at
each call site — the three surfaces render different elements with different classes, and that
difference is real. Only the timing discipline is shared.

The differences between the three copies were not features; they were the two guards each copy
happened to remember. RESET the pending timer on every new notice, or the second toast inherits the
first's remaining time and can vanish almost immediately. Do not clear state after UNMOUNT, because a
5-second timer outlives a closed drawer. RunView's copy had no unmount teardown at all and the
Assistant bar's lived in an unrelated effect; both now get both guards. `clearToast` is the Assistant
bar's early-retire path (a run change makes the previous run's notice misleading, not merely stale)
and it cancels the pending timer too, so a notice for the NEW run is not cut short by the old one's
window.

`ui/test/toastTimerDiscipline.test.js` is re-pointed from "each site remembers" to the stronger
invariant the consolidation buys: exactly ONE arming site, in `useToast.js`, and no surface may grow
a second. Its clear-before-arm check now walks back to the enclosing function body — two weaker
spellings (a file-wide `includes`, then a bounded forward match) both passed while the reset was
missing, because the early-retire path and the unmount teardown each call `clearTimeout` nearby.

UI-12's request-timeout wrappers are a separate finding and remain open.

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

### 5.4 Third reconciliation (2026-08-02, HEAD `2d96bed`)

Roughly 110 further commits landed in the following hours — about 45 of them structural commits
**executing this document**, most tagged with finding IDs in their subjects and each updating
its §4 entry in the same change (the §6.8.3 ledger-upkeep rule, working as designed; the
per-finding `*Resolution:*` paragraphs in §4 are the authoritative detail — this section is the
roll-up). The header marks in §4 were normalized in this pass so every non-open finding carries
one. Running total: **43 resolved, 13 partially resolved, 132 open.**

Newly resolved since §5.1's table: ES-07, ES-11; EC-01, EC-03, EC-05, EC-07; EM-01 (claims.py
2,896 → 831 lines across five modules), EM-02, EM-04, EM-11; EV-03, EV-05, EV-06; CO-07;
SC-05; SR-03, SR-05, SR-07, SR-10, SR-11, SR-13, SR-14; SE-03, SE-05; AG-03, AG-04, AG-06,
AG-07; TO-04; RA-03, RA-05; CT-06, CT-07, CT-08; XP-02, XP-04. Newly partial: ES-02
(telemetry-consume step extracted), EM-08 (scope-boundary half), TO-01, TO-02, TO-03/XP-03
(injection seam with a deliberate lazy default), TO-09/XP-01 (the guard arm:
`tests/test_cross_package_private_seams.py` pins all 26 private cross-package edges two-way),
RA-06 (direction validator on all nine models), SR-04 (generation-fence preamble), SR-09 (the
comment-error half).

The sprint also validated §6's corrected designs in practice: §6.1's kernel is largely shipped
(`core/pathsafe.py`, `atomicio.file_identity` + `durable_no_replace_rename`,
`core/latebind.py`), §6.2's protocol layer mostly shipped (`retry_tail_cas`-equivalent loops
gone via `ES-07`'s helper, `developer_crash_records`, `DEVELOPER_ERROR_PREFIX`,
`_invalidate_completion_certificates`, the JSONL scanner, `_locked_append`), §6.4's
`serve/http.py` and `serve/trace_clear.py` exist as proposed, §6.5's claims split completed
with behavior-preservation proven by test, and §6.6's registries landed (STRATEGY_FIELDS, the
agents↔search direction note, the core `DEVELOPER_BACKENDS` set ending the single upward core
import). Two new themes were added by the executing sessions: **T8** (non-deterministic
full-suite failures diagnosed as GIL starvation — a live defect class, §2) and **T9** (the UI
suite's source-regex idiom had silently retired 30 assertions — found, restored, and closed the
same day, §2).

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
