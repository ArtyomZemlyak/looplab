# 50 · Whole-tree architecture review (2026-09-02)

> Reviewed baseline: `421695bfa3b62fefded441772c2fe5db47327d4b` (`master`, 2026-09-01 14:20 UTC).
> Snapshot: 333 production modules under `looplab/` (~200k physical lines, 113.7k code lines),
> 627 `test_*.py` files (9,806 test functions, ~226k lines), 165 files / ~59k lines of React
> control plane under `ui/src` with 189 `node --test` files, 49 numbered design records plus the
> guide, 137 registered event types, 218 `Settings` fields, 52 CLI commands, ~140 HTTP routes.

**Status: a findings ledger, not a record of fixes.** What the review DID do is mint 44 `OPEN[…]`
markers, each at the site it is about and each carrying a falsifier `tests/test_open_item_index.py`
re-derives from the tree on every run — so a row that ships goes red instead of sitting here. That
44 is a fact about the review, not a running total: closing an item DELETES its marker and its §8
row, so the live index is `grep -rn 'OPEN\['` and never a number written in this document. It is the review the user asked for on 2026-09-02: every level, every surface, with
proposals. Where a finding is already tracked by an `OPEN[…]`/`DECLINED[…]` marker in the tree it
says so and does not re-open it. Line numbers are never cited (the claim-pin guard refuses them);
every site is `path::symbol` and was resolved against the baseline. Counts carry the command that
produced them so they can be re-derived — they WILL drift, quote the instant.

This document is complementary to [doc 25](25-architecture-modularity-review-2026-08-01.md) (the
2026-08-01 structural ledger, 188 findings, since reconciled) and [doc 40](40-mega-review-2026-08-13.md)
(the 4-day correctness window, closed). Neither was re-litigated: what they closed stays closed,
and a finding here that touches one of their sites says which.

---

## 0. Scope and method

Seventeen parallel scope reviewers, each reading its assigned files IN FULL (files over 1,000 lines
chunk by chunk) — all seventeen reported, after two rate-limit interruptions that cost the review
about six hours of wall clock — plus one cross-package pass (import graph, layering, registries, test census,
exception posture, comment density, churn) done by the synthesising session. Every high-severity
and every structural claim in §3 was re-derived against the working tree during synthesis; where a
reviewer's claim could not be re-derived it is marked PLAUSIBLE or dropped.

| § | Scope | Files / lines |
|---|---|---|
| 3.1 | ES1 — engine execution spine, creation/selection side (`orchestrator`, `node_build`, `card_reservation`, `speculation*`, `options`, `shared`, `widths`, `cadence`, `signal_delivery`, `genesis`, `workspace*`) | 14 / ~13.5k |
| 3.2 | ES2 — engine evaluation/repair side (`evaluate`, `eval_dispatch`, `eval_stages`, `crash_repair`, `triage`, `failure_diagnosis`, `repair_verify`, `repair_judgment`, `metric_salvage`, `resources`, `audit`, `costs`, `finalize`, `holdout`, `champion_caveats`, `eval_contract`, `comparability`) | 17 / ~13.7k |
| 3.3 | EM — watchdogs, cadences, strategy, cues (`train_monitor`, `asha_monitor`, `research_cadence`, `concept_cadence`, `strategy`, `novelty`, `proposal_cues`, `confirm_phase`, `ablation`, `verifier_tiebreak`, `task_facets` + `agents/strategist`) | 14 / ~14.1k |
| 3.4 | EK — cross-run memory, lessons, claims, concepts, paid governance (22 engine modules + `trust/cross_run`, `trust/memo_verify`) | 24 / ~13k |
| 3.5 | EV — events (store, fold, card ledger, projections, span index, exporters) | 19 / ~16k |
| 3.6 | CO — core (models, config, LLM client, tracing, cards, I/O primitives, redaction, fences) | 47 / ~21k |
| 3.7 | RA — runtime + adapters | 27 / ~16.4k |
| 3.8 | TO — tools | 32 / ~16k |
| 3.9 | AG — agents + trust + cli + top-level | 42 / ~17.6k |
| 3.10 | SE — search | 26 / ~12.6k |
| 3.11 | SC — serve control-plane core (command lifecycle, control validation, assistant, engine process, composition, auth, settings, TUI) | 29 / ~17.5k |
| 3.12 | SD — serve destructive transactions and read-side services | 24 / ~14k |
| 3.13 | SR — serve routers (the HTTP surface) | 12 / ~14.7k |
| 3.14 | U1 — the six largest React components | 6 / ~19k |
| 3.15 | U2 — every other component + the API client family | ~47 / ~17k |
| 3.16 | U3 — the pure UI models + `ui/test` | ~112 / ~25k |
| 3.17 | DX — documentation architecture, CLAUDE.md, the diagram, the doc guards | — |
| 2 | XP — cross-package / whole-tree | — |

What could NOT be measured in this environment, stated so nobody reads absence as a clean bill:
the `runs/` corpus that every measured claim in CLAUDE.md quotes is absent from the checkout, so
none of those corpus figures were re-derived; the clone is SHALLOW (50 commits, from 2026-08-30),
so churn and first-appearance figures cover the last three days only; `ui/node_modules` is absent,
so the UI test suite and the bundle budget were not run; no LLM endpoint, so nothing live.

---

## 1. The tree in numbers

Every figure below was derived at the baseline with the command in the right-hand column
(scripts in the session's scratchpad; the one-liners are reproducible verbatim).

| Measure | Value | How |
|---|---|---|
| Production Python | 333 modules; 113,705 code / 35,435 comment / 36,257 docstring lines (prose share **38.7 %**; engine 47 %, runtime 58 %, core 49 %, serve 24 %) | `tokenize` census per package |
| Files over 1,000 lines | 59 (9 over 3,000: `orchestrator` 6,590, `replay` 4,422, `routers/runs` 3,827, `evaluate` 3,598, `run_commands` 3,430, `speculation_quality` 3,367, `train_monitor` 3,244, `card_ledger` 3,221, `command_eval` 3,037) | `wc -l` |
| UI | 53 `.jsx` + 112 `.js`; 4 files over 3,000 lines (`AssistantBar` 4,469, `panels` 3,288, `RunView` 3,140, `Inspector` 3,042) | `wc -l` |
| Tests | 627 files, 9,806 test functions (12,552 collected), 226k lines = **1.13×** production Python; full offline suite **12,467 passed / 7 failed / 80 skipped in 25 min 38 s** in this environment (§3.18) | AST census; `pytest --durations` |
| Source-text pins in tests | 158 positive + 126 negative `assert "<lit>" [not] in <source>` across 110 files; 174 files read production source; 36 files use `tests/_source_scan.py` | AST census |
| `Engine(` construction sites in tests | 209 direct vs 89 `make_engine(` | AST census |
| Import edges (intra-`looplab`) | 1,052 module-level + **650 function-local** (38 %) — engine 354, serve 201, tools 109, cli 107 | AST walk |
| Runtime module-level cycles (TYPE_CHECKING excluded) | **1** — the nine-module `cli` package (`cli/__init__.py` imports its groups at the bottom under `noqa: E402`; each group imports the app back). If every deferred import were hoisted: 8 cycles, the largest 28 `serve` modules | Tarjan SCC |
| Cross-package imports of `_private` names | 123 (serve←events 29, tools←engine 21, engine←events 20, engine←runtime 11); `events/eventstore.py::_interprocess_lock` alone is imported by 27 modules | AST walk |
| `except Exception`/`BaseException` handlers | **743**; **460** (62 %) neither re-raise, log, record nor assign — engine 143, serve 112, tools 55, core 48 | AST census |
| `# noqa` annotations | 702, of which 634 `BLE001` — with NO linter configured anywhere in the tree | `grep -c` |
| `Engine` shape | 20 mixins, 489 methods, **772** distinct `self.<attr>` names touched in `engine/`; 208 assigned in `Engine.__init__`, **91** assigned only outside it, **47** assigned from more than one mixin file; 141 `getattr(self, "…")` reads | AST census |
| Registries named in CLAUDE.md | 39, all resolve to a definition; 1–16 test files each | grep |
| Public vocabularies | 137 `EV_*` event types (30 diagnostic, 5 background-appendable); 31 control events + 10 collaboration; 218 `Settings` fields (all mentioned in the configuration guide); 14 `FAILURE_REASONS`; 8 signals; 19 prompt keys vs 37 `render(` sites; 52 CLI commands (2 not in the CLI reference; 21 help strings cite dead `§21.x/§22.x` section numbers); 140 route decorators | import + grep |
| Compat shim | 288 `_LAYOUT` rows + 3 `_RENAMED`; production code imports 0 old flat paths (42 residual mentions are strings/comments, e.g. the `looplab.server` logger name); tests import 5, deliberately | regex |
| Docs | 49 numbered records + guide/audit/reference (~4.6 MB of markdown); `mkdocs build --strict` passes; 9 pages outside the nav; CLAUDE.md **232,919 bytes / 657 lines** (~58k tokens per agent turn) | `mkdocs`, `wc` |
| Churn (last 50 commits only — shallow clone) | `orchestrator.py` 14 touches, `research_cadence.py` 9, `docs/guide/architecture.md` 9, `card_reservation.py` 8, `novelty.py` 7, `speculation.py` 6, the infographic 5 | `git log --name-only` |
| Packages outside every map | `looplab/maintenance/` (2 modules, 524 lines) and `looplab/judgebench/` (6 modules, 2,794 lines, its own `python -m` entry and guide page) | `ls`, grep CLAUDE.md |

---

---

## 2. Cross-cutting findings (XP)

These are the findings that no single scope owns: either the same defect shape recurs in several
packages, or the property is a whole-tree one (layering, exception posture, vocabularies, tests).
Each names the per-scope rows in §3 that carry the evidence. Severity here is the severity of the
CLASS; the per-scope rows carry their own.

### XP-01 — Four paid or filesystem-heavy code paths still run synchronously on the engine's event loop
**Severity** high · **Kind** correctness · **Confidence** CONFIRMED (driven in two scopes).
The 2026-08-30 offload of the proposal lanes (`orchestrator.py::_await_batch_proposal`,
`novelty.py::_capture_proposal_events`) fixed ONE of at least four synchronous holds on the loop.
Still on it: (a) every paid cadence in `orchestrator.py::_run_cadences` — the Strategist consult
(unbounded turns under shipped defaults), the concept re-tag/consolidation pass, the verifier
tie-break, the report refresh — a plain `def` called from `_run_with_llm_broker` (EM-01); (b) the
three paid repair-path calls inside `evaluate.py::_evaluate` — `_triage_crash`, `_repair`,
`_repair_critic` — each a multi-turn blocking HTTP session with no `await` and no worker hop, driven
to **0 loop ticks during a 0.3 s call** with a 5 ms ticker (ES2-01); (c) the FS-heavy eval steps
(`workspace.py::materialize`'s repo copy, `_write_node_files`, the reuse closure, and the trust
scan INSIDE `_write_lock`) (ES2-04); (d) `failure_diagnosis.py::evidence_citation_resolves`'s stat
of a maybe-absent path on the loop in the watchdog tick (EM-08). While any of these holds, the
watchdog ticks, kill claims, operator abort, sibling terminals and GPU refills all wait — the same
bill the propose offload was measured for (median 10.8 min, 59 idle GPU-minutes per the backlog).
The comment justifying (b) by ContextVar lane propagation is false: anyio worker threads inherit the
caller's context (verified). **Proposal**: the `_await_batch_proposal` shape (offload + capture
sink + main-task publish) applied to each site, with a loop-liveness twin of
`tests/test_propose_does_not_freeze_the_loop.py` per site; for (b) first make the Developer's
per-call outputs a RETURN value, because the freeze is what currently serializes concurrent
repairs on the shared developer instance.

### XP-02 — "Fail the node, never the run" has two live holes, and one eval child's raise cancels every sibling
**Severity** high · **Kind** correctness · **Confidence** CONFIRMED.
`runtime/command_eval.py::run_command_eval` raises `ValueError` for an `adapter` reader in
`metrics`/`constraints` AFTER the eval has run — a spec `EvalSpec._readers_usable` accepts at
submit and `core/models.py`'s `EXTRA_METRIC_CHANNELS` comment claims it refuses (RA-01; the
suite pins the raise as intended). Any such raise, and any `OSError`/ENOSPC from `_materialize`,
`_write_node_files` or a `store.append`, escapes `_evaluate` into the run-scoped task group: the
three callers (`orchestrator.py::_dispatch_evals` ×2, `speculation.py::_card_eval_one`) are
`try/finally` with no `except`, and only `GpuPinUnenforceable` gets a per-node terminal (ES2-02).
Siblings are cancelled mid-training with no terminal, the GPU hours are re-spent on resume, and the
run exits with a traceback. **Proposal**: a containment funnel at the three callers writing
`node_failed reason="engine_error"` under `CancelScope(shield=True)` + `_write_lock` (the
`gpu_unpinnable` shape) and a run pause naming the exception; refuse `adapter` in the secondary
reader slots at submit; a mutation test that raising from any reader still yields a node terminal.

### XP-03 — The exception posture is "contain and continue", and nothing measures containment
**Severity** high · **Kind** correctness posture · **Confidence** CONFIRMED (census).
743 handlers catch `Exception`/`BaseException` in production; **460 (62 %)** neither re-raise,
log, record nor assign — engine 143, serve 112, tools 55, core 48. 634 `# noqa: BLE001`
annotations decorate them while no linter is configured anywhere in the tree (CLAUDE.md: "no
ruff/black"), so the annotations are cargo that documents nothing. Containment is the house style
and most instances are argued locally, but the review found the cost at the seams: the
`_AshaStub` incident (an AttributeError swallowed into a silent watchdog), `verifier.py::verify`
swallowing `BudgetExceeded` at a SELECTION site while its sibling `memo_verify` re-raises it
(AG-01), `run_projections.py::run_summaries` dropping a whole run from `/api/runs` on a fold error
(SD-10), `concept_map.py::derive_reference_concepts` returning `[]` for an outage AND for "no blind
spots" (SE-04), `lessons.py::store_case` discarding a refused case while finalize marks the step
done (EK-05), 19 `except Exception` in `finalize.py::finalize_run` (13 of them `pass`/`return
False`). **Proposal**: (1) adopt ruff with `BLE001` and turn the existing `noqa`s into a reviewed
allow-list; (2) one `contain(span, reason)` helper that stamps a `contained` attribute on the
enclosing span so containment becomes countable in `looplab timings` and the judge bench; (3) an
AST guard that every broad `except` around a paid call re-raises `BudgetExceeded` first (AG
proposal 2); (4) triage the 460 silent handlers by package, starting with the 143 in `engine/`.

### XP-04 — Cross-run READ models key on the directory name while every WRITER keys on `run_uid`
**Severity** high · `Kind` correctness · **Confidence** CONFIRMED (four live reproductions).
`memory_cascade.py` documents that run NAMES are reused "on half the corpus" (`demo`, `baseline`,
two checkouts sharing one store) and keys the purge on `run_uid` for that reason; the writers do
too. Seven readers do not: `lessons_reconcile.py::reconcile_lessons` retires another incarnation's
lessons and re-buys the reflect batch (EK-01), `concept_capsules.py::_dedup_valid_capsules` and
`_portfolio_concept_overview_data` drop one incarnation and flip the portfolio to
`source_complete=False` — which withholds tendencies, blocks steward splits/purges and prints
"PARTIAL" on every surface (EK-02), `claims_health.py::_research_source_summary`/`_qualify_refs`
make the D8 claim source `producer_receipt_known=False`, demoting every one-sided verdict to
`inconclusive` and refusing every ratification (EK-03), plus `claims_retrieval.py::portfolio_atlas`
and `concept_shelf.py::run_concept_index`. `trust/cross_run.py::LessonScope.is_current_run`
already spells the correct rule. **Proposal**: one `core` helper `run_ref(row)` (uid, else
`legacy:<run_id>`) used by every reader; a two-incarnation fixture in each store's test file.

### XP-05 — The untrusted-text boundary is applied to the operator's own memory and not to the surfaces that matter more
**Severity** high · **Kind** correctness (trust) · **Confidence** CONFIRMED.
Every persisted memory field the agent tools return is prefixed `UNTRUSTED_*`, and the two
Researcher prompts carry `_UNTRUSTED_MEMORY_RULE`; the label stops there. The always-on assistant
holds auto-run tools (`finalize_run`, `resume_run`, `extend_budget`, `write_file`, `git_commit`,
…) and splices candidate-authored stdout/traces and `@run:` summaries into its turn with no guard
sentence, including on unattended standing-watch wake-ups — while the Boss route beside it wraps
the same bytes in `UNTRUSTED_RUN_EVIDENCE` + `BOSS_EVIDENCE_GUARD` (SC-01). The Strategist, whose
answer sets `eval_parallel`/`policy`/`timeout`, reads a `UNTRUSTED_MEMORY_SUMMARY=`-labelled note
with no rule (AG-02, 3 of 9 in-scope system prompts carry one); the crash triage and repair
critic read stderr verbatim; `concept_tagging.py::tag_text_llm` interpolates the proposer's own
unbounded rationale into an ADMISSION input under a comment that promises an envelope the code
does not build (SE-02); web, arXiv and MCP results — and a remote MCP server's self-description
spliced into the tool SCHEMA — arrive unmarked (TO-06); `inject_node.origin` is client-supplied
provenance the fold and the DAG render as a verified seed with a metric (SC-05). **Proposal**: one
`core/evidence.py` envelope (label + guard sentence) used by the Boss, the assistant, the tool
results and the tagger; one prompt assembler `agents/prompting.py::system_prompt(...)` so the
rule is a property of every prompt rather than of two; a test derived from `PROMPT_KEYS` that
every assembled system prompt whose user turn splices tool output/stderr/memo prose carries it.

### XP-06 — Six vocabularies decide behaviour with no registry, in a tree whose house rule is the registry guard
**Severity** medium · **Kind** structural · **Confidence** CONFIRMED.
CLAUDE.md guards 39 registries and explains why ("a typo'd literal silently no-ops"). The review
found the same seam shape unguarded six times: the ten engine-minted terminal reasons
(`gpu_unavailable`, `superseded`, `card_dropped`, …) are bare literals at every write and read
site, with two divergent ignore-sets in `replay.py`/`attention.py` both listing a `"cancelled"`
nothing mints (ES2-03); the eight stage-row statuses (`reused|ok|fail|timeout|needs_failed|…`)
are spelled in 13 files with the `RunResult` docstring naming three (RA-08); the command-status
vocabulary (`accepted/executing/succeeded/noop/failed/rejected/timed_out`) is spelled six times
across Python and JS and is absent from `serve/protocol.py`, whose docstring calls itself their
home (SC-06); `policy.py`'s `KIND_*`/`META_*` constants are decorative — `card_selection.py` reads
`"_rung"`/`"_promoted"` as literals and the engine reads the meta keys as literals 15 times and as
constants 0 (SE-03); event payload keys exist only as 205 `(handler, key)` `.get()` pairs across
103 fold handlers, with 65 of 137 event types carrying no describing comment (EV-03); permission
is decided by `perm_modes._ACTION_RISK` while `ToolCapability.approval/effect` is recorded and
consumed by nothing (TO-10). **Proposal**: one registry + two-way AST guard per vocabulary, in
the `tests/test_card_build_skip_reasons.py` shape; `EVENT_PAYLOAD_KEYS` and a generated event-log
page for the payload contract.

### XP-07 — Layering is held by deferral and only a third of it is machine-checked
**Severity** medium · **Kind** structural · **Confidence** CONFIRMED.
650 of 1,702 intra-`looplab` import edges (38 %) are function-local — engine 354, serve 201,
tools 109, cli 107. With `TYPE_CHECKING` blocks excluded there is exactly ONE runtime module-level
cycle (the nine-module `cli` package, held together by bottom-of-file imports under `noqa: E402`
and each group importing the app back), but hoisting the deferred imports would collapse the
graph into 8 cycles, the largest 28 `serve` modules. The stated rules are guarded for `runtime`
purity, the `agents→search` direction, and the private-name seams; nothing guards `core`
purity, `events` purity, `engine↛serve`, `tools↛serve` (one declared debt), `events↛search`, or
`adapters` (whose rule is stated nowhere and has one upward edge into `engine`, RA-10). Today the
measured matrix satisfies every stated rule except the declared `tools→serve` one; the cost is
that it holds by convention. Also 123 cross-package imports of `_private` names —
`events/eventstore.py::_interprocess_lock` alone reaches 27 modules — registered as debts in
`tests/test_cross_package_private_seams.py` but not as a plan. **Proposal**: one AST layering
guard over the package matrix with the deferred-import allowance explicit per edge; promote the
top private seams to public API (`_interprocess_lock` → `core/jsonlio.py`).

### XP-08 — `Engine` has 772 attribute names, 91 of them minted outside `__init__`, and nothing declares any of them
**Severity** high · **Kind** structural · **Confidence** CONFIRMED (census).
20 mixins, 489 methods, **772** distinct `self.<attr>` names touched across `engine/`: 208 assigned
in `Engine.__init__`, **91** assigned only elsewhere (lazily minted state such as `_eval_inflight`,
`_card_scoring`, `_landlock_cache`), **47** assigned from more than one mixin file
(`_create_paused` from orchestrator AND speculation, `_eval_parallel` from orchestrator AND
strategy, `_pending_batch_dropped` from three files), 214 `getattr(self, "…")` reads with
defaults. No `__slots__`, no typed state, and 143 silent `except Exception` handlers in the same
package that would absorb the AttributeError a typo produces — the documented `_AshaStub`
incident. `evaluate.py::_evaluate` alone is a **1,898-line method** reading 51 engine attributes
(ES2-10); four test files `inspect.getsource(_evaluate)` to find things in it. **Proposal**: per-
cluster typed state records (`EvalState`, `CardState`, `WatchdogState`, …) declared once, plus an
AST guard "every `self._x` read in `engine/` has exactly one declaring site" — cheaper than a
split and it makes the 91 lazy attributes visible; then the `EvalAttempt` phase object ES2 proposes.

### XP-09 — Two packages are outside every map, and the map's own counts have drifted
**Severity** medium · **Kind** docs · **Confidence** CONFIRMED.
`looplab/maintenance/` (2 backfill scripts, 524 lines, fronted by `cli/maintenance_cmds.py`) and
`looplab/judgebench/` (6 modules, 2,794 lines, its own `python -m looplab.judgebench` entry and a
guide page) appear in neither CLAUDE.md's package map (0 and 1 mentions), the `looplab/__init__.py`
package list, nor `_LAYOUT`; `tests/test_package_layout.py` audits only `_LAYOUT.values()`
packages, so they are outside the layout guard; CLAUDE.md's cli-group list omits
`maintenance_cmds`. Smaller drifts the scopes found: "five search modules import `agents`" (four;
SE-13), "the 35 named per-event rules" (44 slots / 36 functions; SC-16), "`_MONITOR_LOOK_TURNS=6`"
(9; EM-11), "many tests use old flat paths" (5 imports in 3 files; AG-10), "the ONE structured-
judge invocation both verifiers share" (four callers; AG-12), the triage prompt's "five kinds"
against a six-member registry (AG-06), `events/types.py` citing `concept_graph.py::tag_text_llm`
after the doc-25 move (SE-13). **Proposal**: add the two rows; extend the layout audit to every
package directory; move every count in prose behind a `CLAIM[…]` pin or delete it.

### XP-10 — The test suite is larger than the product and its coverage is uneven in a measurable way
**Severity** medium · **Kind** tests · **Confidence** CONFIRMED.
627 files / 9,806 tests / 226k lines (1.13× production Python). 284 source-text pin assertions
across 110 files; 174 files read production source; 36 use `tests/_source_scan.py`. 209 direct
`Engine(` constructions against 89 `make_engine(`. The three largest React components
(AssistantBar, RunView, RunList — 10.6k lines) are never mounted by any test; their coverage is a
vite compile check plus pins (U1-01). Three routes have no HTTP test and nothing asserts the route
SET is covered (SR-09); `_mcp_transport.py` has zero tests (TO-14); no test drives a WORK-role
repair-stop receipt (EM-04), two run incarnations sharing a name (EK), a `sequence()` re-entry
(SC-02), or Replay on a flag-refusing filesystem (SD-01). The full offline suite did not finish in
25 minutes under review load; CI runs it on two platforms with no lint and no type-check.
**Proposal**: a pin-budget guard (the count may not grow); a shared jsdom mount harness and one
gate-flip test per large component; a route-coverage manifest derived from `app.routes`;
fast/slow markers with a CI time budget.

### XP-11 — Public surfaces without a stated, versioned contract
**Severity** medium · **Kind** surface · **Confidence** CONFIRMED.
The HTTP API (140 routes) is unversioned, declares `response_model` on 22, hand-parses 39 bodies,
and 110 of its 140 templates are absent from `docs/guide/ui.md` (SR-10); its refusal-code
vocabulary is unstated and inconsistent — six `HTTPException(500)` sites answer an unreadable
snapshot where every sibling answers 503, one reflecting an `OSError` text with a host path
(SR-05). The event log has no per-type payload contract (EV-03). The task document ignores unknown
keys on every model but one, `validate_stages` silently drops a typo'd `needs`/`expect`/`timeout`
/`role`, and a `stdout_regex` reader with no `pattern` validates (RA-02/03/04). 52 CLI commands:
21 help strings cite `§21.x`/`§22.x` section numbers of documents that no longer carry them (one
cited section, `§21.18`, appears in zero docs), 20 mention internal "PART IV/V" phases, 24
`typer.Exit(1)` sites contradict the documented "1 = crashed" contract, and 2 destructive commands
are not in the CLI reference (AG-03/13). Four `LOOPLAB_*` env knobs and the MCP config surface
are not `Settings` fields and appear in no guide (SC-15, TO-07). **Proposal**: a generated
`docs/guide/api-reference.md` from `app.openapi()` under `mkdocs --strict`; a refusal-code table
in `serve/http.py` with a guard; `extra="forbid"` + closed key sets on the task schema; a CLI
contract registry from which the reference, the exit-code table and the group guard derive.

### XP-12 — Comment density is 38.7 % and the measurements the comments carry do not re-derive here
**Severity** medium · **Kind** docs · **Confidence** CONFIRMED.
35,435 comment lines + 36,257 docstring lines against 113,705 code lines (engine 47 %, runtime
58 %, core 49 %); `train_monitor.py` is 29 % comment lines carrying 56 "measured" claims and its own
inline retractions; `evaluate.py` is 64 % prose; `log_tools.py`'s module docstring is 262 lines of
measurement ledger; the 17 evaluation-spine files carry 110 dated measurement notes and 128 run-name
citations. Every one of those figures cites a `runs/` corpus that is not in the checkout, so none
is re-derivable by the suite. CLAUDE.md is 232,919 bytes / 657 lines (~58k tokens) and is loaded
into every agent turn. The comments ARE load-bearing (this review found the contradictions
because the rationale was written down), but the tree has already grown a better home for the
measurements — numbered docs with `CLAIM[…]` pins and symbol citations — and the scopes list
seventeen comments that no longer describe the code beside them (EM-10/11/17, ES2-11, RA-11,
SE-09/10/13, SC-12/16, EV-11, AG-12, U1-15). **Proposal** (DX section carries the detail): rules
stay in code; dated measurements move to `docs/` behind pins; CLAUDE.md keeps the map, the
invariants and the conventions and points at the record.

### XP-13 — Small helpers re-spelled per module
**Severity** low · **Kind** structural · **Confidence** CONFIRMED.
`_finite_number` ×4 with four different contracts (bool rejection, a 64-char string cap, a
`fallback` argument, `math.isfinite` vs `value == value`), `_safe_text` ×4 (three one-line wrappers
over `cross_run_text`/`redact_persisted_text`, one over `_claim_text`), `_text` ×7, `_clip` ×3,
`_read_bounded` ×3, `_digest` ×4; the "plain direct-child run id" validator in 3 serve modules
plus a seventh weakest spelling in `control.py::resolve_start_claim` (SD-13, SR-08); the
`cmd_`+sha256 command-id rule in 3 places (TO-09); `FILE_ATTRIBUTE_REPARSE_POINT` re-derived in 8
serve files against the `pathsafe` single-spelling rule (SC-11); the metric-subject mode read
spelled 4× (ES2-17); two watchdog loops that are near-copies (EM-05). **Proposal**:
`core/numeric.py::finite_number` with the strictest contract; `core/redact.py::prompt_safe_text`;
one `appstate.plain_run_path`; `core` homes for `run_generation_token`/`command_id_for`.

### XP-14 — The compat shim is tests-only and its justification no longer re-derives
**Severity** low · **Kind** structural · **Confidence** CONFIRMED.
288 `_LAYOUT` rows + 3 `_RENAMED`; production imports 0 old flat paths (the 42 residual matches
are strings, comments and the `looplab.server` logger name); tests import 5 (3 files) and patch 9
strings (4 files), all `looplab.server`/`looplab.calibration`. `test_package_layout.py`
parametrizes 2×288 cases and its exhaustiveness test makes the map grow with the tree for seven
consumer files. **Proposal**: keep `_RENAMED` (real seams); retarget the seven test files; either
shrink `_LAYOUT` to the stems still spelled or generate it; fix the CLAUDE.md sentence.

---

## 3. Per-scope ledgers

Each scope: what it is in five lines, its findings ordered by severity (Sev: H/M/L; Conf:
C = CONFIRMED by tracing or driving, P = PLAUSIBLE), then the two or three moves with the most
leverage. Sites are `path::symbol`. "Tracked" means an existing `OPEN[…]`/`DECLINED[…]` marker
already carries the item and it is not re-opened here.

### 3.1 ES1 — engine execution spine, creation/selection side

**Shape.** `orchestrator.py` (6,590 lines; `class Engine` 6,086 lines / 118 methods /
`__init__` 764 lines) plus the two Card-lane mixins `card_reservation.py` (2,160) and
`speculation.py` (2,919), and the small pure modules that are the good pattern here (`widths.py`,
`cadence.py`, `speculation_gate.py`, `options.py` with 127 knobs 1:1 with `Settings`, `shared.py`,
`node_build.py`, `signal_delivery.py`, `workspace*.py`). The outer loop `_run_with_llm_broker`
(304 lines; 12 `break`s + 1 `return`, 22 `continue`s, 6 `read_all()`s, 3 folds) re-folds per turn
and hands work to `_handle_create_actions` (370 lines) and the six-phase `_run_card_session`.
Three fold seams coexist (`orchestrator.fold`, `card_reservation._fold` routing through it,
`speculation.fold` — a direct import nobody patches). Tests are mostly behaviour-driving with a
heavy AST-guard layer (~270 usages) and ~34 substring pins.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| ES1-01 | H | C (driven) | `orchestrator.py::_handle_create_actions` (serial lane), `::_serve_forced_requests` (fork/inject), `::_rerun_node`, `::_create_node_scoped` | The 2026-08-29/31 offloads moved only the two propose lanes; the Developer call and the fork/inject/reset proposes still run synchronously on the loop, with no in-flight guard — and `_occupancy_paced_creates` delivers work to exactly this lane while evals burn. Driven: a fork served with an adopted eval → **0 loop ticks** during a 0.2 s paid call; width 2 with node 0 in flight → Developer on the loop thread, 0 ticks. The backlog maps this as "the whole create handler … five-point change" but minted no marker for want of a falsifier. | One `_offload_node_build(fn)` on the proposal pool for the serial lane, fork/inject and rerun (the own-node worker seam already licenses their appends; pause drains through `_request_create_pause`); the driven ticker test is the falsifier. |
| ES1-02 | M | C (driven) | `speculation.py` (`from looplab.events.replay import fold`, 14 calls) vs `card_reservation.py::_fold` | The Card session folds through a name no test patches; `_fold_current`'s docstring claims a documented patch seam (0 tests). Patching `orch.fold` with a sentinel does not reach `_fold_current`. The 40-line argument in `card_reservation._fold` applies unaddressed to the 2,919-line module beside it. | Promote `_fold` to `shared.py` and route `speculation.py` through it; extend the driven seam test to assert interceptions from `looplab.engine.speculation`. |
| ES1-03 | M | C (trace) | `orchestrator.py::_settle_proposal_width` (`_evals_inflight()` clause), `speculation.py::_run_card_session` phase order | After one `gpus:2` proposal re-pins a 2-GPU run to width 1, widening back needs a turn with NO eval in flight — the instant F1f/the occupancy pace never provide on exactly the runs where production works. The docstring's justification is about the legacy dispatcher; where it applies the clause is unnecessary. `test_proposal_derived_width.py` (25 tests) never names inflight. | Drop the clause when the session is the dispatcher (or re-derive width at `_card_phase_admit_evals`' fill loop); drive with `_eval_inflight={(0,0)}` + a `gpus:1` board after a re-pin. |
| ES1-04 | M | C (driven) | `orchestrator.py::_build_role_pairs` (`except Exception: break`, no log) → `speculation.py::_start_head_producer` → `_election_excluded_card_ids` | A transient provider hiccup at pair construction closes the head as `producer_failed` (the "producer RAN and gave up" word), with no error text or log, and `card_build_producer_failed` then excludes the Card from speculative election for the rest of the run — routing it through the loop-blocking serial lane (ES1-01). Driven: raising factory → 1 pair, producer pair None, 0 log records. | Log with the exception; add `producer_unavailable` to `CARD_BUILD_SKIP_REASONS`; make `_start_head_producer` return False (retry next turn) when no pair could be BUILT. |
| ES1-05 | M | C (driven) | `workspace_seed.py::seed_repo_tree` (`except Exception: tracked = None` → `copytree`) | A wedged `git ls-files` (`TimeoutExpired`, 120 s wall on mounts measured at 105–950 ms per lstat) takes the "git missing" branch and deep-copies every untracked checkpoint/dataset into every node workdir; only the `workspace_seeded` row betrays it. No test covers the timeout path (the fingerprint sibling has one). | Catch `TimeoutExpired` separately: WARNING + `mode="copytree:git_timeout"`; fail closed for an explicit `tracked` mode. |
| ES1-06 | L/M | C (driven) | `orchestrator.py::systemic_failure_stop_reason`, `_NON_EVIDENCE_FAILURE_REASONS` (= `{"superseded"}`) | The docstring says operator aborts are excluded, but the second operator stop affordance fails the pending node `card_dropped`, which counts; three early drops end the run as "systemic failure: … environment, dependencies or data" at the shipped threshold 3. | A registry `ENGINE_CLOSED_FAILURE_REASONS` (`superseded`, `card_dropped`, `aborted`, `proposal_rejected`, `parent_unavailable`, …) consumed here and AST-guarded against the engine's `reason=` literals (see ES2-03 — same registry). |
| ES1-07 | L | C (reading) | `orchestrator.py::_create_injected_node` (raises for EVERY `None` from `_reserve_node_build`), `::_serve_forced_requests` (receipt-first) | A lost CAS / pause / slot race after `inject_done` is spent consumes the operator's inject as `materialization_failed`, a receipt that reads like a malformed request. | Reserve (cheap, under `_id_lock`) BEFORE appending `inject_done`; only the Developer call stays at-most-once. |
| ES1-08 | L | C | six comment/doc drifts | "the thirteen `break`s" ×2 (AST: 12 + 1 return); "Four gates" (5 callers); `speculation.py`'s "every selection-affecting event is written by the main task" (the legacy parallel worker appends `card_auto_dropped`); "documented patch seam" (0 tests); three un-indexed TODO/CODEX notes (`_serve_raw_card_stage`, `_produce_card_build`, the build-barrier note); 21 event types the scope writes appear in neither the infographic nor any guide page. | Fix the sentences in the same change as ES1-02/04; mint the markers; add the 21 types to the diagram's `B` map. |
| ES1-09 | L/M | C | `orchestrator.py::_dispatch_evals` (three `hasattr(self, …)` probes) | Real scheduling is gated on duck-typed probes that exist for test stubs; a renamed `ResourceSchedulingMixin` method silently skips GPU reservation — fail-OPEN, the opposite of the house rule. | Assert the three names on `Engine` at import (the `shared.py` precedent) or give the stubs the methods. |
| ES1-10 | L | P | `speculation.py::_produce_requested_card` (`kind == "debug"` branch) | Dead since F5 unless a pre-F5 log holds a `debug` Card; the attach gate documents its own unreachability, this branch does not. | Annotate as the attach gate is, or route through the same refusal. |
| ES1-11 | L | C | `localize.py::localize` | Walks and reads every `.py` under the editable roots with no cap (one caller, behind an off-by-default knob). | A file/byte ceiling before anyone turns `localize_faults` on. |
| ES1-12 | L | C | `orchestrator.py::_apply_control_overrides` | A budget control during calibration is refused with a bare `RuntimeError` about operator input. | `ConfigRefusal`. |

Tracked, not re-opened: `node-commit-epilogue-triplicated`, `loop-local-tail-gated-refold`,
`offload-sink-guards-scan-one-file`, `isolated-producer-wrapper-not-extracted`,
`eval-lanes-admit-without-reserving-time`, `first-propose-runs-with-every-gpu-idle`, and the
declined `backfill-receipt-unwired`.

**Top moves.** (1) Finish the offload — one helper, one driven ticker test (ES1-01). (2) One fold
seam (ES1-02). (3) Split `orchestrator.py` along the NON-fold boundaries: `engine/reentry.py`
(the invariant-#6 re-entry envelope, ~600 lines as functions over `RunState` + a frozen record),
`engine/setup_phase.py` (`_setup_phase`, `_dirty_inputs`, `_setup_manifest`, `_env_fingerprint`),
and `EngineConfig = resolve_engine_config(...)` for the 764-line `__init__` — the `fold` seam
constrains only the callers of the module global, and these three clusters fold not at all.

### 3.2 ES2 — engine evaluation/repair side

**Shape.** 17 files, 15,155 lines; `evaluate.py` is 3,598 lines of which 64 % is prose, and
`EvaluateMixin._evaluate` is **one 1,898-line method** (20 appends, 15 `_write_lock` blocks, 4
folds, 45 engine attributes) holding admission, the attempt loop, salvage, the repair ladder,
reuse/rollback and the terminal write. Around it the pure rule libraries doc 25 ES-03 extracted
(`repair_judgment`, `repair_verify`, `failure_diagnosis`, `triage`, `metric_salvage` — every one
"a named function with a truth table"), the dispatcher (`eval_dispatch::_run_eval`, 289 lines),
`eval_stages` (reuse/rollback + the two LLM-judge factories), `crash_repair`, and the leaves
(`resources`, `costs`, `finalize::finalize_run` 339 lines, `champion_caveats`, `eval_contract`,
`comparability`, `holdout`, `audit`). ~90 test files; the core is behaviour-driven through a real
`_evaluate`. All eight node terminals are lexically under `_write_lock` as CLAUDE.md claims
(re-verified), `trust/critic.py` is pure regex, and `CostAccountant.add` swallows sink errors
correctly — three suspected findings refuted.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| ES2-01 | H | C (driven) | `evaluate.py::_evaluate` → `self._triage_crash(…)`, `self._repair(…)` ×2, `self._repair_critic(…)` | Three paid, multi-turn HTTP sessions run with no `await` and no worker hop on the engine loop; median 116–276 s per triage, one recorded case 88.3 min. Driven with a 5 ms ticker: **0 loop ticks** during a 0.3 s `_triage_crash` and during a 0.3 s `_repair`. The comment justifying the direct call by ContextVar propagation is false (worker threads inherit the context — verified). Every surface calls this "the EVAL-BLOCKING thread"; it is the loop. | `await anyio.to_thread.run_sync(partial(...))` at the four sites + a loop-liveness twin of the propose test; FIRST make `Developer.repair` return its change set (or use a per-eval developer from `_role_pool`), because the freeze is what serializes concurrent repairs on the shared instance today. |
| ES2-02 | H | C (trace) | `orchestrator.py::_dispatch_evals` (serial + `_eval_in_slot`), `speculation.py::_card_eval_one` — `try/finally`, no `except`; the run-scoped `eval_tg` | Any raise in `_evaluate` other than `GpuPinUnenforceable` (whose handler comment names the blast radius) propagates into the run-scoped task group: `_materialize` (`rmtree` + copy), `_write_node_files`, every `store.append` (ENOSPC/corruption), six folds. Siblings are cancelled at their checkpoint with no terminal, GPU hours re-spent on resume, the run exits with a traceback. | A containment funnel at the three callers (or one `except Exception` band in `_evaluate`) writing `node_failed reason="engine_error"` under `CancelScope(shield=True)` + `_write_lock`, then a run pause naming the exception. |
| ES2-03 | M | C | writers in `evaluate.py::_evaluate` (`gpu_unavailable`, `gpu_unpinnable`, `proxy_skipped`, `superseded`, `card_dropped`, `aborted`, `developer_crash`, `idea_rejected`, watchdog `monitor_broken`/`asha_underperforming`); readers `replay.py::_FAILURE_SPIKE_IGNORED_REASONS`, `attention.py::_IGNORED_FAILURE_REASONS`, `card_ledger.py::_card_debuggable_leaf_ids` | Ten engine-authored terminal reasons have no registry (`FAILURE_REASONS` covers the classifier's 14 only); the two ignore-sets are spelled independently (attention adds `frozen`) and both list `"cancelled"`, which no writer mints. | `core/models.py::ENGINE_TERMINAL_REASONS` (disjoint from `FAILURE_REASONS`, asserted), readers import it, an AST guard over every `EV_NODE_FAILED` append's `"reason"` constant. |
| ES2-04 | M | C | `evaluate.py::_evaluate` → `_materialize`, `_write_node_files` ×2, `snapshot_training_logs`, `_resolved_stages` ×4/attempt, salvage readers, `_safe_reuse_start`/`_rollback_start`, and `_trust_scan_signals` → `_audit_workdir_writes` INSIDE the terminal's `_write_lock` | Synchronous filesystem work on the loop (the repo measured a missed lookup at 105–950 ms and the largest workdir at 1,017 MB/144 files; `_repair_inputs` at 1.5 ms was offloaded, the seed copy was not); the audit walk under the lock stalls every writer in the process. | Offload the four FS-heavy steps; move the trust scan OUT of the lock (it commits by digest, so the appends can follow under a second acquisition). |
| ES2-05 | M | C | `crash_repair.py::_repair_error_context` (`not_learning` branch) reached with the DIAGNOSED reason | The directive tells the Developer "the live watchdog KILLED this stage … the live judge named the IMPLEMENTATION" — false on both counts when the reason came from the diagnostician on a `check_failed` stage (nothing killed; the head sentence is the diagnosis lead). | A new sentence keyed on `_reason_source == REASON_SOURCE_TRIAGE` (prompt strings are contracts); pin both variants. |
| ES2-06 | M | C | `eval_dispatch.py::_do_run_setup` (`raise RuntimeError("run_setup failed …")`) | A failed `-r requirements.txt` is the textbook `EnvironmentRefusal`, so the operator gets the 42-frame traceback the marker type exists to remove. | `raise EnvironmentRefusal(...)` (subclasses `RuntimeError`); add to `test_cli_refusals.py`. |
| ES2-10 | M | C | `evaluate.py::EvaluateMixin._evaluate` (1,898 lines) | Doc 25 extracted the pure rules; the DRIVER kept growing by 50–150-line blocks inside the `while True`; six test files `inspect.getsource(_evaluate)` at seven sites to find things in it. | An `EvalAttempt` object along the phases the comments already name: `admit` → `run_attempt` → `settle_outcome` → `salvage` → `decide_repair` → `apply_repair` → `write_terminal`; every append and lock stays where it is (AST-asserted). |
| ES2-12 | M | C | `costs.py::_ROOT_ATTRS`/`_CHILD_ATTRS`/`find_cost_accountants` | Billing reachability is a hand-maintained attribute list; the docstring records three "spent and unbilled" incidents; the only guard is a one-way positive pin. | Derive the accountant-bearing attributes from a fully-built default engine and assert `find_cost_accountants` finds them all. |
| ES2-07 | L | C | `evaluate.py::_auto_pause_provider_failure` (the only concurrent-task writer of `EV_PAUSE`) | A run-global folded latch written from an eval child, outside every registry and invariant clause; defended by a site docstring and one test. | Register it (a `RUN_LATCH_APPENDABLE` singleton or a sentence in invariant #1). |
| ES2-08 | L | C | `evaluate.py::_eval_intervention_seen` vs `replay.py::_control_generation_matches` | A third spelling of the generation rule with different semantics from the fold (unstamped controls bind to generation 0 only; raw `!=` on `node_id`). No live divergence for stamped int controls. | Call `coerce_node_id` + the fold's rule; delete the inline parse. |
| ES2-09 | L | P | `evaluate.py::_repair_salvaged_cause`/`_commit_salvaged_cause_fix` | The salvage-cause path lacks the stuck rung the attempt loop calls load-bearing, so a "(developer stuck: …)" answer can be committed as `node.code` on an evaluated node. | Hoist the two-rung ladder into one named function both paths call. |
| ES2-11 | L | C | `metric_salvage.py` (`NEVER_SALVAGED_REASONS` comment), `audit.py::_redact` docstring | The exit-0 divergence race the comment describes is closed one layer down (`run_argv` forces `rc=-1`); "the ONE funnel for all six persisted tails" is now ~12 strings via 7 call sites. | Reword to current reachability. |
| ES2-13 | L | C | `finalize.py::finalize_run` (`"llm_cost_refresh_failed"` ×2, `"stewards"`, `"concept_curation"`, `"claim_curation"`, `"task_facets"`), `_STEWARD_RECEIPT_OUTCOMES` | Five live step names are outside `events/finalize_protocol.py` (the vocabulary the quiet-suffix gate shares); the steward outcome set is a reader-side guess; 19 `except Exception` in one 339-line function. | Move the names into the protocol module (AST-derived from `_mark_finalize_step` sites); producers import the outcome set. |
| ES2-14 | L | C | `evaluate.py::_evaluate(node_id, limiter, max_es)` | All 22 call sites pass a fresh `CapacityLimiter(1)`; the parameter bounds nothing and invites tuning. | Drop or rename. |
| ES2-15 | L | C | `node_repaired` (~22 fields), `node_failed`, `stage_rollback`, `full_retrain_charged`, `repair_critic_verdict`; `_MAX_DEP_ROUNDS`=6, `_UNPARSEABLE_REPAIR_LIMIT`=3, `INERT_REPAIR_LIMIT`=2 | Row schemas exist only as inline comments; no key-set test; the three bounds are in no guide. | `NODE_REPAIRED_FIELDS`/`NODE_FAILED_FIELDS` registries with a payload-⊆-registry test and one catalog table in `docs/guide/concepts.md`. |
| ES2-16 | L | C | `tests/test_repair_judgment.py` (9 `getsource` pins), `test_watchdog_stage_scope.py`, `test_repair_stop_decision.py` (5) | Comment-satisfiable substring pins guard the repair loop's control flow while `tests/_source_scan.py` is already used one file over. | Convert positive pins to `called_names`/`names_read`. |
| ES2-17 | L | C | `metric_subject` mode read ×4; 27 module-level imports after the first `def` in `evaluate.py`; `triage.py::_dir_fingerprint`/`_shallow_fingerprint` share a verbatim git-HEAD block | Hygiene. | One `_metric_subject_mode` property; an import block; a `_git_head` helper. |
| ES2-18 | L | C | `metric_salvage.py::SalvagedMetric.__post_init__` (`SALVAGE_SOURCES`/`_CONDITIONS`/`_PRODUCERS`) | The runtime registry check is tested by nothing; a typo'd slug raises inside `except Exception → None` wrappers and degrades to "not salvaged". | A parametrized construction test. |

Tracked: `repair-unmet-five-unpatched-shapes`, `monitor-fault-has-no-outcome-label`,
`judge-bench-covers-two-judges-of-four`, `judge-bench-cannot-see-a-post-exit-stage-failure`.

**Top moves.** (1) Offload the repair path and make the Developer's outputs a return value
(ES2-01). (2) Per-child containment with an `engine_error` terminal (ES2-02). (3) The
`EvalAttempt` split (ES2-10) with the terminal-reason registry (ES2-03) landing first.

### 3.3 EM — watchdogs, cadences, strategy, cues

**Shape.** 14 modules / 14,083 lines: the two per-eval timer watchdogs (`train_monitor.py`
3,244 — 29 % comment lines carrying 56 "measured" claims and its own inline retractions;
`asha_monitor.py` 1,038), the node-count cadence chain driven from `orchestrator.py::_run_cadences`
(coverage + concept snapshots, run-base seed, verifier tie-break, Strategist consult, serial deep
research, report, hypothesis merge), the novelty gate and the proposal-cue registry inside the
offloaded proposal lanes, the confirm/ablation phases, the Strategist agent, and the pure pacing
rules in `cadence.py`. The trust posture is strong and STATABLE: kill/repair/stop decisions are
pure predicates with truth-table tests, every watchdog row is in `DIAGNOSTIC_EVENTS` and asserted
at its append site, `_proposal_authority_seq` excludes them wholesale, the ASHA judge can only
narrow the rank test. Settings ↔ docs reconcile 63/63 in this scope. 24 test files / 533 tests,
predominantly behaviour-driven.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| EM-01 | H | C (mechanism) | `orchestrator.py::_run_cadences` (plain `def`, called from `_run_with_llm_broker`) → `strategy.py::_maybe_consult_strategist` → `agents/strategist.py::ToolUsingStrategist.decide` → `drive_tool_loop`; `concept_cadence.py::_refresh_concept_tags`/`_tag_hypothesis_concepts` (up to 20 + 60 sequential tags); `verifier_tiebreak.py::_maybe_verify_ties`; `research_cadence.py::_run_deep_research` (manual/strategist triggers); `ablation.py::_ablate` | None of these awaits; the Strategist loop is unbounded under shipped defaults (`agent_max_turns=0`, `agent_time_budget_s=0.0`); `verifier_tiebreak.py` states "a blocking LLM call here matches the established pattern". `at_creation_boundary` (ON) made these gates due WHILE evaluations burn; `_run_cadences` is not in the backlog's four-door map. | Hop each paid cadence body through `to_thread.run_sync` under the capture-sink discipline of `novelty._offload_under_proposal_sink` (they write FOLDED rows); stop-gap: a finite `LoopOptions` for the consult; test as the propose test does. |
| EM-02 | M | C | `agents/strategist.py::RuleStrategist._decide_machinery` (`"asha" in avail and ctx.phase == "explore"`), `search/policy.py::policy_fills_width` | The rule fallback for EVERY LLM failure picks `asha` in explore with no width term, so an endpoint hiccup at width ≥ 2 selects the schedule the same module's brief tells the model "cannot keep the slots busy"; the repo's own measurement of that shape is 5.94 of 8.03 starved GPU-hours. Pinned at width 1 only. | `and policy_fills_width("asha", ctx.eval_parallel)` (or emit `eval_parallel: 1` beside it); a truth table over width ∈ {1, 2}. |
| EM-03 | M | P | `train_monitor.py::should_monitor_repair` (`trajectory_vetoes_kill(...) and not citation_authenticates(...)`), `failure_diagnosis.py::evidence_citation_resolves` | The one deterministic conjunct is lifted when `fault == "implementation"` and a locator resolves to an EXISTING workdir path — which the judge, holding `list_dir`/`find_files`, fully controls; a confident wrong `implementation` on a descending curve buys a full re-train. | Authenticate against the judge's OWN tool ledger (a path it actually opened/grepped), and for `implementation` require the cited line to hold an assignment or numeric literal. |
| EM-04 | M | C (pure logic) | `train_monitor.py::TrainingMonitorMixin._monitor_training` (`trajectory_veto`, `kill_role_withheld` keyed on `not stop_decided`) | A repair-stop on a WORK role writes `repair_decided: true, stop_decided: true, kill: true` AND `kill_role_withheld: "work"`; a repair-stop admitted through the citation bypass writes `trajectory_veto: true` beside `kill: true` — both receipts predate `should_monitor_repair` and were never re-keyed; durable-record rot. | Key both on `acted = stop_decided or repair_decided`; drive a WORK-role repair-stop. |
| EM-05 | M | C | `train_monitor.py::_monitor_training` vs `asha_monitor.py::_monitor_asha` | Near-copies: prior-row recovery, the `_judge` closure with a 14-line docstring duplicated verbatim ("it has to be stated twice…"), the `llm_capped`/`cancelled_before_call` branches, the `_redact` idiom, the `stop_decided`/`kill` triple, the shielded append. The 2026-08-30 fix landed twice. | Free functions `watchdog_judge_tick(...)` and `recover_last_row(...)` (the `monitor_log_tools` precedent, so no MRO can hide one from a stub). |
| EM-06 | M | C | `train_monitor.py` (importers: evaluate, eval_stages, failure_diagnosis, asha_monitor, orchestrator, judgebench; 20 test files) | Five modules in one file — verdict schema + prompts, the loss-trajectory measurement (~600 pure lines), the declared-contract reader, log plan/roles/floors/tool builders (~800 lines), the pure gates, the ~900-line mixin loop; external importers use only the pure halves. | `engine/loss_trajectory.py`, `engine/eval_log_plan.py`, `engine/monitor_gates.py`; re-export through the shim; a split-guard test. |
| EM-07 | L | C | `events/types.py` (`EV_TRAIN_MONITOR_ALERT` note) vs the writer | 10 of the 17 keys written are absent from the schema note (`repair_decided`, `evidence_*`, `kill_role_withheld`, `projected_overrun_s`, …); `ui/src/attentionModel.js` reads one of them. | A `TRAIN_MONITOR_ALERT_KEYS` tuple beside the constant; writer keys ⊆ it by AST. |
| EM-08 | L | C | `train_monitor.py::_monitor_training` (`_citation_resolved = evidence_citation_resolves(...)` after the worker returns) | A stat of a maybe-absent path on the loop, in a loop whose own docstring records why the tool build was moved into the worker (105–950 ms per missed lookup). | Compute inside `_judge` and return `(verdict, resolved)`. |
| EM-09 | L | C | `train_monitor.py` (`DECLINED[…]` citing `docs/47-early-stop-blind-classes-2026-08-20.md`) | The doc is `48-…`; the index guard checks only that a `measured:` clause contains `docs/`. | Resolve `docs/<path>` in a DECLINED body against the tree; fix the citation. |
| EM-10 | L | C | six sites (`train_monitor.py`/`asha_monitor.py` module docstrings, `proposal_cues.py::_cue_watchdog_reflection`, `orchestrator.py`, `__init__.py::_LAYOUT`, `types.py`) | Still describe the kills as opt-in / off by default; `Settings.train_monitor_kill` and `asha_live_kill` default `True`. | One sentence per site separating product from library default, or a `CLAIM[…]` pin on the two Settings lines. |
| EM-11 | L | C | `train_monitor.py::_MONITOR_LOOK_TURNS` (= 9) vs `tools/log_tools.py` docstring and CLAUDE.md's tools row (6) | The 128 MiB ceiling's derivation still cites the old budget. | Cite the constant, not the literal. |
| EM-12 | L | C/P | `train_monitor.py::_MONITOR_SAME_DIGEST_RETRIES` vs `_monitor_training` (`last_digest` committed only at the end of the parsed branch) | A deterministic raise between the parsed verdict and the commit lands in the per-tick `except: continue` with the digest uncommitted; the next tick re-sends the identical prompt, bounded only by `_MAX_MONITOR_LLM_CALLS` (200). | Commit or count in a `finally`. |
| EM-13 | L | C | `concept_cadence.py::_should_consult_concepts` (`concept_retag_every … or self.strategist_every`) vs `cadence.py`'s "0 means off" rule | Dead for every built engine (`ge=1` + clamp), live only on a `__new__`-built one where it re-couples the map to the Strategist interval. | Delete the fallback. |
| EM-14 | L | C | `research_cadence.py::_maybe_merge_hypotheses` (per CARD, `not selection_ready and _pure_belief`) vs `::_admissible_beliefs` (per BELIEF) | Two "open board" populations claiming to be one; `is_pure_belief`'s own docstring calls `selection_ready` the wrong predicate. | One `RunState.open_pure_beliefs()` accessor; drop the conjunct. |
| EM-15 | L | C | `asha_monitor.py` imports `_MONITOR_LOOK_TURNS`/`_normalize_monitor_confidence` (also `judgebench/score.py`) | The ASHA judge inherits a turn budget justified by code tools it does not have. | Public names; `ASHA_LOOK_TURNS = 6` with its own derivation. |
| EM-16 | L | C | `docs/guide/*.md`, the infographic | 0 guide pages for the four confirm events (`confirm_done`: 0 files anywhere under `docs/`); 6 scope events absent from the diagram (`asha_verdict` among them — the row that decides an early kill). | One `B`-map entry per type; a confirm-phase paragraph in `concepts.md`. |
| EM-17 | L | C | `research_cadence.py`/`strategy.py` module headers | Layering claims stale ("only core, events and stdlib" — imports `agents`; "…search, agents" — also `trust`, `governance_health`). Permitted edges, wrong sentences. | Replace prose with the AST guard used for the search/agents edge. |
| EM-18 | L | C | `test_watchdog_stage_scope.py`, `test_monitor_log_tools_wiring.py`, `test_ablation.py` | Three positive text pins guarding properties other tests drive. | Convert or delete. |

Tracked: `asha-inert-on-this-task-family`, `f1i-b-serial-deep-research-gate`,
`first-propose-runs-with-every-gpu-idle`, `monitor-fault-has-no-outcome-label`,
`judge-bench-covers-two-judges-of-four`, `strategist-developer-field`,
`concept-skeleton-matches-no-run`, `classifier-rewrites-authored-membership`, `overrun-grace-bar`,
`crash-predictability-unmeasured`, `duplicate-receipt-lands-on-one-lane-of-three`,
`next-experiments-never-reach-proposal`.

**Top moves.** (1) Cadence offload behind a capture sink (EM-01). (2) Split `train_monitor.py`
along its importer map (EM-06). (3) One watchdog tick helper (EM-05) so EM-04/08/12 land once.

### 3.4 EK — cross-run memory, lessons, claims, concepts, paid governance

**Shape.** 22 engine modules (13,097 lines) plus the trust/tools/cli/serve surfaces that read
them, in a clean three-tier shape the doc-25 splits mostly achieved: writers that leave a run at
finalize (`lessons_distill::write_reflection_note`, `lessons::store_case`/`store_research_claims`
/`store_concept_capsule`, and the two at-most-once paid protocols `curation_protocol` and
`steward_invocation` sharing three ledgers); strict policy ledgers (`concept_registry`, claim
decisions, `governance_health.read_governance_rows`) that fail closed and CAS every write; and
read models carrying health receipts through every projection. Layering holds (0 `serve`
imports). The metric-trust boundary is real: every durable claim grounded in a node passes
`memory.py::unreliable_metric_ids`, driven through a real run by
`tests/test_trust_gates_reach_the_ledger.py`. The dominant defect class is IDENTITY (XP-04);
the second is store schemas stated nowhere once; the third is governed reads holding every
store's write lock. 897 tests in 38 files.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| EK-01 | H | C (driven) | `lessons_reconcile.py::LessonReconcileMixin.reconcile_lessons` (`o.get("run_id") != state.run_id`, nested `_is_stale`) | "This run's lessons" is decided by directory NAME; a lesson from a previous incarnation of `runs/demo` cannot match the new run's `evidence_sig`, is judged stale, RETIRED under the lock, and the reflect batch is re-bought. Driven: surviving statements `[]`. The existing test uses a different NAME. | Filter and `_is_stale` through `LessonScope.of(state).is_current_run(row)`; a two-incarnation test. |
| EK-02 | H | C (driven) | `concept_capsules.py::_dedup_valid_capsules` (`by_run[rid]` on `run_id`), `_portfolio_concept_overview_data`, `claims_retrieval.py::portfolio_atlas`; writer `ConceptCapsuleStore.add` replaces by `run_uid` | Two valid capsules from two incarnations coexist on disk (store health: complete), every reader keeps ONE, counts the other as a duplicate and reports `source_complete: False` — which withholds tendencies (`build_context_pack`), forbids steward splits/purges and prints "PARTIAL" everywhere. Driven: `n_runs=1 source_complete=False dup_rows=1`. | Dedup/aggregate on `(run_uid or "legacy:"+run_id)`; make the reader's rule the SAME expression as `_reload`'s. |
| EK-03 | H | C (driven) | `claims_health.py::_research_source_summary` (`groups.setdefault(run_id …)`), `_qualify_refs` (`f"{run_id}:{n}"`), `claims_assessments.py::_ingest_evidence` | Two incarnations' complete v3 row sets collapse into one group → `producer_receipt_known=False` → `source_complete=False` → every `supported`/`refuted` demoted to `inconclusive`, every `ratified` refused, refs from two runs merged into one support count. Driven: `rows=2 receipt_known=False`. `run_uid` appears in 0 lines of `tests/test_claims.py`. | Group receipts and qualify refs by incarnation. |
| EK-04 | M | C (driven) | `memory.py::JsonlCaseLibrary._add_locked` (legacy branch) | A uid-less case write's `replace_if` narrows by task/direction only — not by `_case_scale` and not by "row has no uid" — so one legacy finalize erases every uid-keyed contribution of its group across all comparability partitions, and `store_case` ignores the return value. Driven: 3 rows → `[('legacy', 0.9, None)]`. | Legacy `replace_if` adds `not row.get("run_uid") and _case_scale(row) == scale`; or refuse and disclose. |
| EK-05 | M | C (mechanism) | `lessons.py::LessonMemory.store_case` (`lib.add(...)` result discarded) vs `store_concept_capsule` (raises); `memory.py::valid_case_record` caps `goal` at 4,000 | A goal over 4,000 chars (CLAUDE.md's goal guidance produces multi-KB goals) never gets a case row while finalize marks `FINALIZE_STEP_CASE` done — the "silent loss" the capsule writer's comment forbids one function up. | Bound `goal`/`rationale` at the WRITER with a receipt and raise on rejection. |
| EK-06 | M | C | `lessons_distill.py::distill_skill_body` (feeds `code[:4000]`), `memory.py::write_auto_skill`, `tools/skills.py::use_skill` (returns the body verbatim) | The one cross-run sink written from unredacted code and read back verbatim: `node_created.code` is deliberately outside `Engine._redact`, but a skill card is a SHARED artifact mounted into every later Researcher's toolset; neither write nor read applies `cross_run_text`. `_redact_persisted` also never applies `redact_env_values`. | Redact at `write_auto_skill` and on `use_skill`; add `redact_env_values` to `_redact_persisted`. |
| EK-07 | M | C | `serve/memory_cascade.py::claim_keep_reason`/`_tasks_curated_by_other_runs`/`merged_concept_ids`/`capsule_keep_reason` | The keep-predicates read steward PROPOSALS (`concept_curation_log.jsonl`) and never applied policy (`concept_aliases.jsonl`), and the claim predicate assumes per-`task_id` curation while the finalize claim steward's input is portfolio-wide (`claims_for_memory` with no `scope_task`) — a claim a steward's `input_digest` covered can be deleted, an unratified proposal protects forever. | Key claim protection on "a curation row's `input_digest` covered this row"; read `load_concept_aliases` for capsule protection. |
| EK-08 | M | P | `governance_health.py::project_governed_sources` (callback inside `_concept_governance_transaction` → global thread lock + interprocess locks on every store, blocking, no timeout); consumers `claims_retrieval.cross_run_retrieve`, `atlas_for_memory`, every governed agent tool, `routers/cross_run.py` | A read that builds a `HybridRetriever` or a whole atlas holds the same locks every finalize writer needs; the in-process `threading.Lock` serializes all governed reads across the FastAPI threadpool and every agent tool call. | Snapshot BYTES (or `file_identity` + cached parse) under the locks, compute outside; keep revision labels from the snapshot. |
| EK-09 | M | P | `lessons.py::append_lessons(hygiene=True)` → `lesson_hygiene._agentic_merge_lessons` → `search/hybrid_merge.consolidate` | Every finalize of any run re-clusters the ENTIRE shared store and re-sends every ≥2-member cluster to the model; a cluster already declined is re-sent forever (the CAS drops the rewrite, not the spend). | A `(bucket, cluster digest) → verdict` memo keyed on `lessons_file_token`; or scope to buckets this run touched. |
| EK-10 | L | C | `concept_registry.py::concept_governance_snapshot`, `governance_health._read_governance_locked`, `claims.py::claim_governance_revision` | The alias ledger is strictly re-read 4×, the split ledger 3×, the claim ledger 2× per snapshot, inside EK-08's lock. | One `GovernanceLedger.load(memory_dir)`. |
| EK-11 | L | C | `memory.py::task_fingerprint`, `lessons.py`, `novelty.py`, `concept_capsules.py::prior_capsules`, `cross_run_index.py::run_facts`/`build_index` | 7 `TODO … §21.20.x` comments and a 2026-07-17 design note carry no marker; CLAUDE.md's claim that the section "no longer exists" is false (`docs/17` still holds `##### 21.20.13`). | Mint markers with `present:` proofs; correct the sentence. |
| EK-12 | L | C | `docs/guide/memory.md` "The types" table vs the writers (`lessons_distill::reflect_lessons`, `lessons_reconcile::comparative_lessons`) and `claims_health::_valid_claim_source_row` | The lessons row is spelled by three writer literals plus one validator; the docs omit ten fields; cases documented as "one active row per (task, direction)" — false since partitioning by `comparability`; "steward proposals: nothing reads these" — false (`concept_tidy`, `memory_cascade`); `memory.py`'s docstring describes the UNWIRED `CaseLibrary` (0 constructor sites in `looplab/`). Filenames literal in 22/30/16 places. | One schema registry per store; a test diffing the docs table against each validator's field set. |
| EK-13 | L | C | `concept_tidy.py::pending_curation_work`, `ratify_concept_merges` | Cumulative history reported as "pending"; a ratification receipt appended per finalize once any proposal exists — `concept_ratification_log.jsonl` grows O(all proposals) per finalize. | Subtract applied sources; write a receipt only when the applied/skipped set changed. |
| EK-14 | L | C | `lessons_distill.py::write_reflection_note` (`_interprocess_lock(...)`, default `required=False`) vs every sibling store write (`required=True`) | On a filesystem without advisory locks the dedup+append silently runs unlocked. | `required=True` + disclosure. |
| EK-15 | L | C | `cli/governance_cmds.py` (no `concept-alias-clear`/`concept-split-clear`), `concept_merge_cmd`/`concept_split_cmd` (no CAS/`action_id`) | The documented undo that the ratification stage's safety argument rests on is not a CLI command; CLI concept writes have no idempotency. | Add the two commands; accept `--action-id`/`--expected-revision`. |
| EK-16 | L | C | `core/concepts.py::normalize_concept_id`, `engine/concept_registry.py::normalize_key`, `tools/cross_run_tools.py::_slug_norm` | Three normalizations; `canonicalize_concepts` validates the raw slug but not the alias TARGET, so an operator alias with a space is emitted as a canonical concept the shelf/UI then drop. | `prepare_concept_alias` refuses a source/target failing `valid_concept_id`; one `core` owner for "is this a concept id". |

Tracked: `auto-skill-promotion-run-end-only`, `distilled-pair-ledger-leaks-on-long-runs`.

**Top moves.** (1) One run identity for read models (XP-04). (2) A store schema registry. (3)
Reads never hold write locks across computation (EK-08).

### 3.5 EV — events (store, fold, card ledger, projections, span index, exporters)

**Shape.** 19 files / 16,057 lines; layering clean (38 `from looplab.core`, 20 intra-package,
0 above). Three tiers: store (`eventstore.py` + the `core` I/O it re-exports), fold (`replay.py`
4,422 + `card_ledger.py` 3,221 + `comment_projection.py`), projections/exporters. The registry is
an exact partition: 137 constants = 107 folded + 30 diagnostic, guarded by `test_event_types.py`;
0 constants have zero readers. The invariants hold where driven: six permutations of three
independent node blocks fold canonically byte-identical; `FoldCursor.snapshot() == fold()` at all
14 prefixes of a cards+reset+concepts log; first-terminal-wins and the generation fences carry 151
tests. `derive_cards` is a 15-phase pipeline with a 200-shuffle permutation test; the store has a
real 4-process race test. `replay.py` is five handler families in one file sharing a 19-slot
`_FoldCtx`.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| EV-01 | M | C (driven) | `replay.py::_record_repair_ledger` (`_REPAIR_LEDGER_MAX = 200`, first-come); readers `RunState.repair_candidates`, `lessons_reconcile.py::self_pair_repair_account`, `cli/inspect_cmds.py::repair_candidates` | The cross-node repair ledger silently drops every row after the first 200 with no omission receipt; a 261-row log folds to 200 with node 1's only row gone, the CLI prints "repair rows: 200" as complete, and a cross-run lesson reads a missing row as "no cause recorded". Real runs hold 2,345 repairs on one node. | Bound per node (newest N) + `repair_ledger_omitted` in the `card_enrichment_omissions` shape; `self_pair_repair_account` distinguishes omitted from none. |
| EV-02 | M | C | `replay.py` (103 handlers; families: control/advisory 1,195 lines, node lifecycle 1,126, concept machine 655, selection/trust 396, card receipts 352); `_FoldCtx`; `fold` and `FoldCursor.extend` re-spell the dispatch | Doc 25 moved `derive_cards` out and the file grew back past its pre-split size; a concept-materialization change and a control-cursor change land in one module and one ctx. | An `events/fold/` package with per-family `HANDLERS` dicts merged into `replay._HANDLERS`, one `_dispatch`, per-family ctx records; `fold` stays in `replay.py` for the seam; verify by the corpus digest. |
| EV-03 | M | C | `events/types.py`, every `_on_*` handler, `docs/guide/concepts.md`, the infographic | No per-type payload contract: 65 of 137 constants have no describing comment, 2 state a shape; the fold reads 205 distinct `(handler, key)` pairs (`_on_run_started` 29); 15 types are named in no doc, `concepts.md` names 53/137, the diagram 59/137; four constants are filed under the wrong section header with apology comments; payload versioning is ad hoc (`ownership_receipt.v`, `proposal_ref.v`, `verifier_group_scored.v`) while `Event.v` is 1 everywhere. | `EVENT_PAYLOAD_KEYS` registry guarded both directions by AST; a generated `docs/guide/event-log.md`; move the four misfiled constants. |
| EV-04 | M | C (driven) | 18 handlers aliasing raw `Event.data` into public `RunState` journals (`_on_hypothesis_ranked` `{**d}`, `_on_novelty_*`, `_on_coverage_snapshot`, `_on_llm_cost` `dict(raw)`, `_on_strategy_decision`, …) vs `_on_data_provenance`'s stated rule and `_on_concept_coverage_snapshot`'s bounded conversion | Two contradictory admission rules in one file; none of these fields is in `INTERNAL_CARD_STATE_FIELDS` and `_public_state_value` strips 12 raw keys, so every row rides `/state` and SSE. Driven: 3,000 ~4 KB rows → `st.novelty_events[0] is events[11].data`, 15.3 MB wire, `snapshot()` 14 ms — copying is negligible, the wire is not. | One bounded-row admission per journal (the `_coverage_snapshot_row` table style) with a cap and an `_omitted` receipt; a one-table statement of REQUESTS (copied) vs PROJECTIONS (bounded). |
| EV-05 | L | C (measured) | `eventstore.py::EventStore.__init__` (`_scan_last_seq` → full `read_all`, then `log_divergence` → second full parse); 23 construction sites in `serve/` (per legacy `/control` POST, 7 in `routers/runs.py`) | Two full passes per construction: 1,186 ms on a 41 MB/12,001-event log, a fresh-store single append 827 ms, ~29 ms/MB — 150–350 ms per click on real 5–12 MB logs; `_finalize_incomplete`'s docstring records removing exactly this cost from its own path. | Derive the divergence receipt from the same walk; one store per path keyed by `file_identity` on the serve side. |
| EV-06 | L | C (driven) | `replay.py::_on_card_dropped` (registered for `EV_CARD_DROPPED` AND `EV_CARD_AUTO_DROPPED`), `card_ledger.py::_drop_author` | Authority is read off `dropped_by`, not the event TYPE the split was minted for; an engine-typed row carrying `dropped_by: "operator"` is reopenable and returns to `selection_ready`. Exposure: a foreign/hand-edited log or a future call site. | Stamp `dropped_by = "engine"` when `e.type == EV_CARD_AUTO_DROPPED`; one test. |
| EV-07 | L | C (driven) | `types.py::BACKGROUND_APPENDABLE` ("(b) the fold handles it ORDER-TOLERANTLY"), `replay.py::_on_llm_cost`, `test_background_appendable.py` (asserts selection only) | `llm_usage` spliced before vs after a legacy `llm_cost` summary folds `llm_cost.cost` 1.0 vs 11.0; property (b) is proven for selection only. | Narrow the registry comment; assert `st.llm_cost` in the splice test except the stated legacy interaction. |
| EV-08 | L | C | `tests/test_event_types.py::_emitted_string_literals` (matches `.append(` only); 7 `append_many` sites, 0 literal tuple heads today | The emitted-literal guard cannot see `append_many` and says so in a CODEX note. | Extend the scanner to tuple heads; delete the note. |
| EV-09 | L | C | `replay.py::_on_hypothesis_added`, `::_on_concept_consolidation` (CODEX), `span_index.py::_load_persisted` ("THE RESIDUAL"), three test-file CODEX notes | Six self-declared open items with no marker; `grep 'OPEN\[' looplab/events/` → exactly one. | Tag or close each; the consolidation note is a design decision — `DECLINED[…]` with a number or `OPEN[…]`. |
| EV-10 | L | C | `replay.py::_on_node_reset` and `::_requeue_partition_bound_results` | Two hand-maintained ~27-field "begin a new lifecycle" lists that already disagree (`agent_report`, a dead `generalization_gap` write); a third partial copy on the `node_created` re-emit path. Driven: no live consequence (finalize recomputes). | `Node.begin_lifecycle(keep_code)` naming the field set once; a residue-equality test. |
| EV-11 | L | C | `digest.py::trust_reflection`/`watchdog_reflection` (lazy `replay` import "to avoid a cycle"), `replay.py::_on_research_completed`/`_on_report_generated` (lazy `advisory_payloads`) | No cycle is possible under the layering rule (`replay` does not import `digest`; `core` imports `events` in 0 files); the stated reason is false. | Hoist, or state the real reason (import weight). |
| EV-12 | L | P | `traceview.py::TRACE_PROJECTION_SCHEMA` (= 2, "bump together with `span_index._SCHEMA`") and `span_index.py::_SCHEMA` (= 12) | Two schema numbers kept in step by a comment; the persisted index header is stated only in code. | Derive `_SCHEMA` from `(TRACE_PROJECTION_SCHEMA, INDEX_LAYOUT_VERSION)`; document the header in doc 08. |

Tracked: `score-backfill-fold-drops-backfilled-marker`; doc 25 EV-04's scalar-guard residue.

**Top moves.** (1) Make the payload contract statable (EV-03). (2) Split `replay.py` by handler
family (EV-02). (3) Bounded journals with omission receipts (EV-04, EV-01).

### 3.6 CO — core

**Shape.** 47 modules / 20,976 lines; four files carry 47 % (`config.py` 2,836, `models.py` 2,549,
`llm.py` 2,357, `tracing.py` 2,181). Layering holds (0 imports above `core`; `latebind.py`
imports by string at call time). The re-export seams are identity seams, not copies (60 `cards`
names + 13 `concepts` names resolve on `models` to the SAME objects). `Settings` = 218 flat
fields; the configuration table covers all 218 with **0 wrong defaults and 0 env-name mismatches**
(derived by parsing the rows); 13 enum + 1 member field validated; 43 fields carry bounds;
`LEGACY_CONFIG_SNAPSHOT_DEFAULTS` = 67 rows naming live fields. The LLM layer is one client plus
three principled siblings; the broker's five lanes are a closed vocabulary. The durable-write
primitives are tiered on purpose and `file_identity` includes `st_ctime_ns`. `hardware.py` no
longer contradicts the resource fence (fixed, with a pin). The posture is unusually behavioural
(real socketpairs, scripted SDK, real `lstat` under-reporting).

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| CO-01 | M | C (probed) | `core/appconfig.py::build_settings`, `core/config.py::Settings.model_config` (`extra="ignore"`) | A config file's `settings:` block silently drops unknown keys (`{"max_node": 30}` → `max_nodes == 8`, no diagnostic) while `--set` refuses them; the YAML file is the documented primary launch surface. | Refuse `set(layer) - set(Settings.model_fields)` per layer with a `ConfigRefusal`; keep `extra="ignore"` on the model for snapshots. |
| CO-02 | M | C (probed) | `config.py::Settings._ENUM_FIELDS`, `llm.py::reasoning_body`, `OpenAICompatibleClient._policy_bad_request` | `llm_reasoning`/`llm_reasoning_style` are closed vocabularies that are not validated: `llm_reasoning="hgih"` is accepted, the provider 400s, `_is_reasoning_reject` matches and reasoning flips OFF for the client's lifetime — the exact class `_ENUM_FIELDS` exists for, on the field whose shipped default is `high`. | Add the two rows to `_ENUM_FIELDS`; update the pinned set and the docs. |
| CO-03 | M | C | `docs/guide/configuration.md` ("Web editors"), `tests/test_config_docs_sync.py::test_settings_catalogue_counts_and_profile_semantics_are_current`, `serve/settings_ui_schema.py` | **RED at the baseline**: the doc says "184 of the 217 direct `Settings` fields" while the constants say 185/218, and the paragraph (with its "176 catalogued keys" neighbour) is repeated FOUR times in consecutive lines — a merge artifact from `cc6a64e` (2026-08-30). | Fix the numbers, delete the three copies, derive the sentence from the constants. |
| CO-04 | M | C (probed) | `llm.py::bound_api_key_for`, `config.py::Settings._llm_credential_pair_trusted` | A directly constructed `Settings(llm_api_key=…)` is silently ignored (`bound_api_key_for` → `"local"`) unless a private flag only `preflight.py` and `settings_store.py` set is on; the 401 message then names an env variable the caller did not use. `tests/test_silent_misconfiguration.py` sets the private flag by hand. | Raise `LLMCredentialError` naming the resolver when the field is set and untrusted. |
| CO-05 | M | P (shallow git) | `config.py::LEGACY_CONFIG_SNAPSHOT_DEFAULTS`, `tests/test_config.py::test_every_product_on_divergence_is_grandfathered…` | The admission rule (post-2026-06-23 + paid/intervention/concurrency/selection + a pointable value) is prose; the only derivation-based guard walks the Settings-vs-EngineOptions divergence table and cannot see 35 ON fields EngineOptions lacks and 25 it also enables — among them `phase_handoff_summary`, `agent_auto_summary`, `research_verify`, `gpu_footprint_cue`, `researcher_tools`/`cross_run_tools`/`all_runs_tools`, `strategist_backend="agent"`, each paid or prompt-changing and carrying neither a row nor an in-place exemption. | A `LEGACY_EXEMPT: dict[str, str]` beside the map; a test asserting every ON/positive field is in exactly one of rows/exemptions/pre-snapshot. |
| CO-06 | L | C (measured) | `config.py` (`from looplab.core.llm import AGENT_STAGE_KEYS, DEFAULT_HEADER_TIMEOUT_S`), `llm.py` (guarded `import openai`) | Constructing the schema loads the openai SDK: `import looplab.core.config` 849 ms, 607 of it `core.llm`, 533 `openai`; every CLI command, `replay`/`timings` included, pays it. | A leaf `core/llm_vocab.py`; `_RETRY_POLICY` built lazily. |
| CO-07 | L | C | `llm.py` (cites a non-existent `tests/test_llm_reexport_seam.py`), `config.py::_SECRET_ENV_NAME` + `_check_llm_profiles` ("duplicated: layering forbids core importing runtime" — `SECRET_ENV` has lived in `core/envsafe.py` since the move), `models.py::Node.error_reason` (6 reasons; 14 exist), the `Settings` docstring ("six timeout knobs"; 15), `PROFILES["thorough"]["reflection_priors"]` ("no-op unless memory_dir is set"; it defaults ON) | Five load-bearing comments that no longer describe the code; the first is invisible to `citation_defects` because it carries no `::symbol`. | Fix; extend `citation_defects` to bare `tests/test_*.py` paths. |
| CO-08 | L | C | `Settings.eval_deadline_grace_s` (`ge=-1` admits (-1, 0), all read as AUTO), `Settings._apply_profile` (stores the raw spelling: `profile="THOROUGH"` snapshotted as a name `PROFILES` lacks) | Two bound/normalization nits. | A `v == -1 or v >= 0` validator; write the normalized profile back. |
| CO-09 | L | C | `core/tracing.py` (span model + caps + OTel bridge + the sync JSONL exporter with torn-tail healing and receipts + the async queue + fork quarantine + the untraced-call detector) | Five concerns in one 2,181-line file; the exporter half (~900 lines) depends on the span model only through `_span_jsonl_line`. | `core/trace_export.py` with re-exports. |
| CO-10 | L | C | `comparison.py::ComparisonContract._bind_contract_id`, `fence.py::publish_bounded_json_marker`, `run_deletion.py::run_deletion_snapshot_token`, `llm.py::_cache_key` | Four hand-spelled canonical-JSON encodings beside `jsonutil.canonical_json`, three of them digest/marker preimages (doc 25 SE-08's reason for unifying two). | Two can call `canonical_json` today; `run_deletion`'s token must keep its bytes and say so. |
| CO-11 | L | C (scope) | `llm_broker.py::BACKGROUND_LANE_PRODUCERS`, `tests/test_llm_broker.py` (`_ENGINE_DIR.glob("*.py")`) | The capped-lane registry guard scans `engine/` only while `in_llm_lane` is importable anywhere and an unknown lane maps to the uncapped `engine` lane silently. | Scan `looplab/**`. |
| CO-12 | L | C | `llm.py::LiteLLMClient` (implements `complete_tool`/`complete_text` only; roles call `chat`, `complete_text_stream`, `probe`; 0 production constructors) | The docstring says it "drops into the LLM roles like any other backend"; it cannot. | Implement `chat` or narrow the docstring. |
| CO-13 | L | C | `core/_pathsafe.py` (tool read/secret guards, imported by six production modules) vs `core/pathsafe.py` (filesystem identity) | Two unrelated modules one underscore apart, the "private" one the more used. | Rename to `core/toolpaths.py` through `_RENAMED`. |
| CO-14 | L | P | `llm_transient.py::_REASONING_REJECT_KEYS` (`extra_forbidden`, `unexpected keyword`, `unrecognized`), `llm.py::_policy_bad_request` | Any "unrecognized field" 400 is attributed to the reasoning toggle; `guided_json`/`response_format` are not special-cased as `stream_options` was, so one retry with the real offender attached, a misleading error, and reasoning permanently off. | Check for the guided-JSON keys before the reasoning branch. |
| CO-15 | L | C | `redact.py::is_secret_key_name` (`_SECRET_KEY_RE` with bare `token`/`secret`; an 8-name `_BENIGN_KEYS` allowlist), reached from `tracing.py::SpanHandle.set` | `input_tokens`, `output_tokens`, `tokens_per_second`, `max_new_tokens`, `tokenizer_path` are written as `"***"` on the span — the throughput numbers the trace exists to show. | A stem rule (`_tokens`/`token_count` benign) instead of a list. |
| CO-16 | L | C | `memory_window.py::read_memory_jsonl_window` vs `jsonlio.py::read_jsonl_lenient_with_health` | Two lenient JSONL readers with two health vocabularies over the same stores; `memory_window`'s own comment records the line rule diverging once. | Build the window on `jsonlio.scan_jsonl_region`. |
| CO-17 | L | C | `tests/test_config_docs_sync.py::test_every_settings_field_is_documented` (`f not in text`) | The docs-sync guard checks NAME presence anywhere in the file, never the DEFAULT the CLAUDE.md rule requires (`timeout` inside `llm_timeout` passes); 0 defaults are wrong today by discipline. | Parse the table rows and compare defaults (the ~40-line script exists in the scratchpad). |

Tracked: `atomicio-windows-parent-publication`, `researcher-questions-not-appended`,
`default-atomic-write-skips-parent-fsync`, `json-extraction-takes-the-first-object`,
`receipt-builder-reader-field-set-unguarded`, `replay-scalar-guards-hand-rolled`,
`run-path-validators-not-unified`, `unconverted-stat-signature-ledger`.

**Top moves.** (1) One registry per `Settings` field, derived not maintained (CO-02/05/17). (2)
Refuse unknown file keys at the loader (CO-01). (3) Decouple the schema from the transport
(CO-06).

### 3.7 RA — runtime (sandboxes, command evaluation, fences, deps) + adapters

**Shape.** 17,380 lines: `runtime/` (9,401; `command_eval.py` 3,037 with 69 top-level defs and
54 constants, `sandbox.py` 1,782 imported by 64 production modules) and `adapters/` (7,028;
`repo_developer.py` 1,919, `repo_task.py` 1,739). `runtime/` imports only `core` (verified);
`adapters/` has one upward edge into `engine`. The runtime side is strong where it says it is:
`sandbox.run_argv` is a real universal choke point, `validate_stages` is the one definition of a
stage and all 8 callers route through it, every metric-source path goes through `_confined`, the
metric record triad and `stage_identity` are closed-vocabulary acquit-only instruments, the three
fence rungs are stated with their residuals. The weak side is the SUBMIT SCHEMA (three open key
sets) and the WORKER BOUNDARY (the "fail the node, never the run" rule has two live exceptions
inside `run_command_eval` itself). 58 test files / 862 tests, unusually behavioural for boundary
code.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| RA-01 | H | C (driven) | `adapters/repo_task.py::EvalSpec._readers_usable`, `runtime/command_eval.py::run_command_eval` (the `raise ValueError("metrics/constraints readers must be built-in, not 'adapter'…")` and `validate_cross_check`), `engine/evaluate.py::_evaluate` | An `adapter` reader in `metrics`/`constraints` is ACCEPTED at submit and raises inside the eval worker AFTER the eval ran; the raise escapes exactly as the module's own comments describe for `TypeError` — no terminal, siblings cancelled, the run re-dies on every resume. `core/models.py`'s `EXTRA_METRIC_CHANNELS` comment says `EvalSpec.metrics` refuses `adapter`; the suite (`test_constraints_adapter_reader_rejected`) pins the raise as intended. | Refuse at submit in the same `readers()` walk; replace both raises with the node-failing shape (`reader_refused` on `RunResult`); re-point the test; a worker-boundary mutation test. |
| RA-02 | H | C (probed) | `runtime/command_eval.py::validate_stages` (builds `st` from known keys, never inspects the rest) vs `_validate_expect`'s closed `STAGE_EXPECT_KEYS` | `{"needs_files":…, "expects":…, "time_out":99, "roles":"training"}` validates with `err=None` and every declaration vanishes — declared inputs, outputs, wall and kill-eligibility; the manifest is MODEL-authored on every fresh repo node. No test covers it. | `STAGE_KEYS` closed beside `STAGE_EXPECT_KEYS`, refused with the same message shape (reaches the model through the `declare_stages` bounce). |
| RA-03 | M | C (probed) | `repo_task.py::EvalSpec`, `RepoTask`, `DataSpec`, `EditableSpec`, `ReferenceSpec` (no `model_config`); only `DeveloperCommandSpec` has `extra="forbid"` | `EvalSpec(command=…, tiemout=5, subject=[…], stage=[…])` validates and drops all three; the `_stages_valid` comment records this exact mechanism once made `cmd.stages` "silently vanish". The snapshot then records the operator's intent as the default. | `extra="forbid"` on all five with a `_grandfathered` strip-and-log validator; one test per model. |
| RA-04 | M | C (probed) | `command_eval.py::metric_spec_path_error` (asks only `READER_PATH_KEYS`/`READERS_REQUIRING_PATH`), `_read_stdout_regex`/`_read_file` | A `stdout_regex` reader with no `pattern` (or `patern`) validates and every node fails `no_metric` with nothing naming the cause — the failure the pathless-`file_json` refusal was added for, one reader over. | Generalize into `READER_KEYS = {kind: (required, allowed)}`; `metric_spec_error` refuses missing-required and unknown keys; extend the two-way reader guard. |
| RA-05 | M | C | `command_eval.py::_read_adapter` (`run_argv(argv, str(workdir), …, None, 64_000)` — `env=None`); `engine/resources.py::_fenced_env`; `adapters/repo_developer.py::LLMOnboarder.__call__` (emits `kind: adapter`) | The one reader that EXECs candidate-lineage code runs in the ENGINE's environment minus secret-named vars: unfenced, unlandlocked, unpinned (`CUDA_VISIBLE_DEVICES` absent), without the operator's `eval_env`. `run_argv`'s comment lists "the metric adapter" among what passes through the fence — it passes through the function, not the fence. | Add `env` to the reader signature, pass the eval env from both call sites, thread `cancel`; state the boundary in `tasks.md`. |
| RA-06 | M | C (reading) | `runtime/read_allowlist.py::mount_sources` (`"readwrite" if edit`), `repo_task.py::DataSpec._mount_edit_consistent` (coerces `edit:true` → `mount:false`), `engine/eval_dispatch.py::_data_binds` (dead `edit` branch), `read_fence.py::fence_inputs` | The two spellings of the read boundary disagree about `edit:true`: Landlock grants WRITE on the operator's ORIGINAL, Docker never binds it, and the bind branch that would is unreachable for validated specs; the two-spellings test compares membership, not mode. | `mount_sources` answers `read` unless `mount and edit`; delete the dead branch; compare modes in the test. |
| RA-07 | M | P | `command_eval.py::_run_stages` (checker consulted only after `rc == 0` and `expect.files` PASSED), `STAGE_CHECK_HARD_KINDS`, `epoch_floor_acquits` | A `FAIL crash:` verdict about a stage that exited 0, or `FAIL no_artifact_written:` about artifacts the engine just verified, still lands as `check_failed` (46.6 GPU-h recorded); the "out-of-band channel exists → use it" rule is applied to two of five physical kinds; the checker sees stdout only while HF/Lightning write to stderr. | Hoist `epoch_floor_acquits` into an acquit-only `stage_check_acquits(kind, rc, artifact_contract, health_fired)` table; hand the checker the combined stage log tail. |
| RA-08 | M | C | `command_eval.py::_run_stages` (mints `reused|ok|fail|timeout|needs_failed|env_unsupported|expect_failed|check_failed` as literals); `sandbox.py::RunResult.stages` docstring (3 of 8); 13 files spell them | No registry for a vocabulary CLAUDE.md guards elsewhere. | `STAGE_STATUSES` at the minting site + two-way AST guard; fix the docstring. |
| RA-09 | M | C | `command_eval.py` (seven concerns), `sandbox.py` (docker translation, JSON scanners, rlimit launcher, `run_argv`/`_tee_drain`/`_kill_tree`, health monitor, both tiers) | Doc 25 RA-02 decomposed the function, not the module; patch seams (`_violations` forwarders, `_run_argv`) exist only to survive the size. | `runtime/stage_manifest.py`, `metric_readers.py`, `stage_check.py`, `docker.py`, `process.py`, `stdout_json.py` behind `_RENAMED`. |
| RA-10 | L | C | `adapters/repo_developer.py::_validate_repair` (function-local `from looplab.engine.repair_verify import …`); CLAUDE.md "Layering" (0 mentions of `adapters`) | One upward edge held open by laziness; the layering rule for `adapters` is stated nowhere. | State it; move `repair_claimed_without_writing` to `core`; add the direction test. |
| RA-11 | L | C | seven sites | Comments contradicting code: the `EXTRA_METRIC_CHANNELS` refusal claim (RA-01); `run_argv`'s fence list (RA-05); `command_eval`'s docstring "four readers … `adapter` not here" (six, incl. `_read_adapter`); `RunResult.stages` (RA-08); `repo_task.py`'s "see `plans/`" (no such dir) and "the agent backend (opencode)" (one of three); `stage_identity.py::reuse_refusal` "the reporter drives" (0 callers); `mount_sources` vs `_data_binds`. | Fix at site; (a) and (f) mislead about a boundary. |
| RA-12 | L | C | `adapters/kaggle_dl.py::check_auth` (0 callers), `runtime/metric_inputs.py::unreadable_input_note` (0), `applied_params.py::applied_divergence_note` (0 prod / 1 test), `stage_identity.py::reuse_refusal` + `REUSE_REFUSALS` (0 prod / 9 tests), `_data_binds`' `edit` branch | Unreached code, one of it an instrument whose "decision half" has no consumer. | Delete the first two; wire or delete the notes; make `stage-dups` report `reuse_refusal` or mark test-only. |
| RA-13 | L | C | `docs/guide/tasks.md` | `host_score` (one of six reader kinds, with its own keys and submit rule): 0 mentions in `docs/guide/`; `onboard_command`/`onboard_timeout`: 0; the `eval.stages` row omits `role`; no per-kind reader key table; `task.snapshot.json` has no schema version stamp. | Add the rows; stamp the snapshot. |
| RA-14 | L | C (probed) | `repo_task.py::EvalSpec._valid_metric_kind` (`k not in _KINDS` on the raw value) | `{"kind": ["file_json"]}` raises `TypeError: unhashable type` at submit instead of the reader-table refusal; `spec_kind` exists for exactly this and is used one validator later. | `k = spec_kind(v)`. |
| RA-15 | L | P | `runtime/bg_tasks.py::BackgroundManager.start` (`open(log, "wb")` in the shared temp dir, default umask, up to 2 h × 32 files) | Assistant background-command OUTPUT lands world-readable on a shared JupyterHub node; the env is scrubbed, the output is not. | `os.open(..., 0o600)` or a `mkdtemp(0o700)` per manager. |

Tracked: `synthetic-task-adapters-copy-paste` (8 draft-then-perturb Researcher classes, 8
`_direction_valid` copies, 5 `gpu_capable` copies), `windows-tree-kill-is-not-atomic`.

**Top moves.** (1) One closed schema for the task document, stated once (RA-02/03/04/14 +
a `schema` stamp). (2) "Fail the node, never the run" as a statable, guarded rule at the worker
boundary (RA-01 + ES2-02). (3) Split `command_eval.py`/`sandbox.py` by concern behind the shim.

### 3.8 TO — tools

**Shape.** 32 modules / 16.8k lines + `adapters/repo_write_tools.py`. 28 classes offer `specs()`;
together they advertise **103 tool specs / 98 distinct names** across three real compositions
(the Researcher/Strategist stack, the owner assistant, the repo Developer) and two judge toolsets.
The authority split holds: mutating providers are gated by `perm_modes.authorize`, every
execution surface is bounded and stated, read providers refuse by `core/_pathsafe` or by NAME
(`log_tools` names a log, never a path). Docs/36 holds (no tool result decides engine-side except
the assistant's own operator-approved run-control appends). Layering: `tools→serve` at one
guarded site, `tools→engine` at 7 files function-local over 26 declared private edges. ~477 tests
in 21 files; the behavioural drive is tier-1 where it matters (`test_log_tools` spends the
receipts it asserts, `test_dev_probe` re-derives the unaudited-mutator set from a recording audit
hook). The `mcp` SDK is absent here, so `_mcp_transport.py` could not be imported.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| TO-01 | H | C (mechanism) / P (magnitude) | `tools/_runcache.py::RunStateCache` (`_cache_max = 32`, LRU), `run_tools.py::AllRunsTools._list_runs`, `SiblingRunTools._sibling_ids` (folds every run to read `task_id`), `machine_runs_tools.py::MachineRunsTools.summaries`, `serve/assistant.py::build_tools` (instance rebuilt per turn and per `@run` mention) | Every listing walks `run_ids()` in sorted order folding each; with N > 32 the LRU evicts the head while the tail is still folding, so the NEXT call misses on every run again (sequential-scan thrash); on the assistant the cache starts empty every turn. `ForeignRunReader` records `list_all_runs` at ~2,500 ms "warm, dominated by one fold per run" — the thrash measured without being recognised; "32 covers the working set" is false on the author's own 46–59-directory corpus. `test_runcache.py` pins the bound at 4 and never drives a scan larger than it. | One process-level cache keyed by run root, bounded by bytes; read `task_id` from `run_started`/`task.snapshot.json`; a test listing 2× the bound twice and asserting fold count == N. |
| TO-02 | M | C | `run_tools.py::RunTools._code` (`n.code[:max_chars]`), `DataTools._schema`/`_profile`, `knowledge_tools.py::read_note` (`[:4000]` = exactly `RESULT_CAP`, the one length the loop never marks), `RepoTools.repo_grep` (per-mount blocks joined with no fit) | Four reader surfaces cut at/under the cap with no marker — a cut answer is byte-identical to a complete one, the defect `mcp_tools._clip`'s comment records; `read_code` lists a repo node's `files` by NAME only and no reader can open them. Not in `test_bounded_tool_results.py`'s parametrisation. | Route through `clip(..., note=…)`/`fit_rows`; `read_code(node, start_line, lines)` pagination + `read_node_file`; an AST guard on `[:<name>]` slices on returned strings. |
| TO-03 | M | C | `machine_runs_tools.py::RunControlTools._tool_set_trust_gate` (appends `EV_TRUST_GATE_CHANGED` + mirrors the snapshot, bare `store.append`) vs `serve/routers/runs.py::_repair_trust_gate_event` (`expected_last_seq` CAS + lock + 4 retries); `_commit_delete_node_snapshot` appends `EV_NODE_TOMBSTONED` — the ONLY writer in the tree | The tools layer is a second writer of two folded events outside `CONTROL_EVENTS` (0 matches in `protocol.py`), with a different CAS discipline; invariant #1 says UI/CLI append only allow-listed control intents. The docstring names the fix; it is not indexed. | Register both as control events with precondition rows; make `set_trust_gate`/`delete_node` command-backed like their eight siblings; delete the dual write. |
| TO-04 | M | C | `tools/vectorstore.py::LLMEmbedder._call` (raw `urllib` POST, no counters, no client), `engine/costs.py::_CHILD_ATTRS` (comment claims a `CostAccountant`), `knowledge_tools.py::_build_index` (re-embeds every note/case on every `cases.jsonl` append) | Embedding spend is invisible to the durable `llm_usage` ledger — an unsigned residual in `looplab tokens` — and `costs.py` says the opposite. | A `CostAccountant` on the embedder (or route through `core/llm.py`); an embedding cache keyed `(model, sha256(text))`; fix the comment. |
| TO-05 | M | P (torch absent) | `tools/env_inspect.py::EnvInspectTools._gpu_info` (`torch.cuda.get_device_properties(i)` per device → `_lazy_init`, a persistent primary context on device 0 of the ENGINE process); `dev_probe.py` rule 4 directs the model here | The engine fences `CUDA_VISIBLE_DEVICES` for every child but not for itself; `_resolve` imports arbitrary installed modules in-process and the tool is composed unconditionally at four sites regardless of `trust_mode` ("safe in the trusted-local tier, sandboxed otherwise" is false). | Answer from `nvidia-smi`/NVML or a `run_argv` child; a test that patches `_lazy_init` to raise. |
| TO-06 | M | C | `tools/web.py`, `literature.py`, `mcp_tools.py::McpTools.execute_result` (results) and `_advertised_mcp_spec` (a remote server's `description[:400]` spliced verbatim into the tool SCHEMA) | The `UNTRUSTED_*` labelling the memory tools apply stops at the three network surfaces and at MCP; `tool_loop._run_tool_call` frames every result as a plain `role: tool`. | One `untrusted(text, source)` envelope in `_base.py`; a provenance test. |
| TO-07 | M | C | `mcp_tools.py::load_config` (`LOOPLAB_MCP_CONFIG`, `LOOPLAB_MCP_SERVERS`, then `REPO_ROOT/.mcp.json`), `_mcp_transport.py::_ServerHandle` (30 s / 120 s literals), `connect_server` (stdio child env) | Two `LOOPLAB_*` env vars that are not `Settings` fields (CLAUDE.md: 1:1); a repo-root config file neither tracked nor ignored that would start stdio subprocesses on the first non-plan turn; an inline `headers: {Authorization: …}` is not secret-shaped by name and rides into every child. | `Settings.mcp_config`/`mcp_*_timeout_s` rows; drop the repo-root default; add the var to the secret-name pattern. |
| TO-08 | L | C | `tools/reposcout.py::_iter_glob` (`tail = pattern[3:]`; any tail with `/` falls to `base.glob`) | `**/configs/*.yaml` recurses `.git`, `node_modules` and GB-scale checkpoints without `_SKIP_DIRS`; one root is `Path.home()` for the assistant. | Walk every `**` pattern with the pruned `os.walk`; assert a `**/<dir>/<leaf>` search never enters a skip dir. |
| TO-09 | M | C | `machine_runs_tools.py::_local_run_generation` ↔ `serve/run_commands.py::run_generation_token` (hand-copied preimage); `"cmd_" + sha256(key)[:32]` spelled 3× (`_RunCommandAdapter.submit`, `run_commands.py`, `tui.py`) | `predicted_id` decides "already applied, do not resend" after a transport failure; a drift in any spelling turns "uncertain" into a silent miss; 0 tests pin equality. | Move both rules to `core/`; pin in the seams test. |
| TO-10 | M | C | `_base.py::ToolCapability` (declared by 6 of 27 providers, consumed only as span attributes), `perm_modes.py::_ACTION_RISK` (the real gate, keyed on hand-spelled `(tool_kind, tool)` dicts; 0 tests), `MUTATING_KINDS`/`READONLY_KINDS` (0 consumers) | Two permission vocabularies — one recorded, one enforced — neither guarded; an unregistered pair degrades to `UNKNOWN` (asks even in `auto`), so a renamed verb silently changes the operator's mode. | Derive the action risk FROM the declared capability (or delete the unconsumed fields); a two-way scan. |
| TO-11 | L | C | `KnowledgeTools.grep` vs `RepoScoutTools.grep`, `read_run_experiment` ×2, `edit_file`/`write_file`/`delete_file` (tools vs adapters, different schemas), `read_sibling_experiment`/`read_run_experiment`/`read_experiment` for one delegate | Five colliding names, three spellings of one operation, no registry; `test_tool_collisions.py` drives fakes, never a real composition. | A package-level name census test; a naming rule. |
| TO-12 | L | C | `shell_tools.py::ShellTools` (`list_background`/`read_output` over the process-global `bg_tasks.MANAGER`; `exec_argv` without `cancel=`) | Any chat session can read another session's command output by id; a foreground command holds the turn slot up to 600 s with the stop button inert (the probe and dev-command surfaces pass `CancelSignal`). | Key `MANAGER` rows by session; implement `execute_result(..., cancel_check)`. |
| TO-13 | L | C | `dev_commands.py::DeveloperCommandRuntime` (dataclass defaults + `from_settings` fallbacks = six re-spelled `Settings` defaults) | The drift `shell_tools.py`'s own comment refuses ("how the two ended up describing different containers"). | Build from `Settings.model_fields` defaults; pin equality. |
| TO-14 | L | C | `tools/_mcp_transport.py` (0 tests; SDK absent here) | The sharp part (same-task `__aexit__`, abandoned-boot unwind, 120 s poll, `fut.cancel()`) is undriven. | An in-process fake server over the SDK's memory streams behind `importorskip`. |
| TO-15 | L | C | `docs/guide/memory.md` tool table (3 of `RunTools`' 11), `llm-and-agents.md` (omits `read_questions`, `search_lessons`, the eight `cross_run_*`), no MCP rows; `question_board.py` docstring "83 `fn_spec` tools" (103/98 today) | Guide coverage is partial and the one table is stale. | Generate the inventory per composition; pin with a doc-contract test. |
| TO-16 | L | C | `tools/agents_md.py`, `retrieval.py`, `edit_match.py`, `log_tools.py`'s 262-line measurement docstring | Non-provider helpers with one consumer each in a package whose name promises providers. | Move beside their consumers; move the ledger to a doc it can cite by symbol. |

Tracked: `run-lifecycle-primitives-cannot-move-down` (the injected `lifecycle=` has zero
production callers), `machine-runs-tools-not-split`, `agent-node-purge-has-no-durable-receipt`,
`cross-run-read-model-still-private`, `run-path-validators-not-unified`, two CODEX notes.

**Top moves.** (1) One contract scan for the whole package (names unique, `bind_state` arity,
never-raise, every `(kind, tool)` in `_ACTION_RISK`, every capability name a spec name). (2) A
shared, byte-bounded fold cache per run root (TO-01). (3) Register the two folded events as
control events and delete the tools-side dual write (TO-03).

### 3.9 AG — agents, trust, cli, top-level

**Shape.** 14 `agents/` modules (8,207 lines), 15 `trust/` (2,894), 10 `cli/` (6,209), the compat
shim, `bench.py`, `sweep.py`. Genuinely good: the `agents→search` direction is real and guarded (0
module-level offenders, 3 deferred sites); every duck-typed seam CLAUDE.md names has a two-way
scan; `LoopOptions` partitions `drive_tool_loop`'s 26 parameters by AST; the refusal boundary is
driven end to end; `preflight._REMEDIES` is registry-asserted; `unified_agent.triage_crash` reads
its vocabularies from the engine registries. Weakest: the one house rule NOT registry-guarded —
"a hard budget stop propagates, everything else degrades" — is inverted in `trust/verifier.py`;
the untrusted-data rule exists as a code-owned suffix on 3 of 9 system prompts; the CLI's stated
contracts (exit codes, groups, documented set) are each measurably contradicted.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| AG-01 | H | C | `trust/verifier.py::verify` (`_one_sample`: `except Exception: return None`) | `BudgetExceeded` subclasses `Exception`; a tripped ceiling is reported as `n_samples=0, score=None` — indistinguishable from an endpoint failure — and the loop keeps issuing the remaining samples (≤ 32). Consumers: `verifier_tiebreak.py` (SELECTION machinery), `foresight.py`, `graded_novelty.py`, `lesson_guard.contradiction_scan` (up to 40 more calls). The sibling `memo_verify.verify_memo` re-raises; `test_verifier.py` never names `BudgetExceeded`. | `except BudgetExceeded: raise` first; an AST guard that every broad `except` around a paid call in `agents/`+`trust/` re-raises it. |
| AG-02 | M | C | `agents/strategist.py::LLMStrategist.decide`/`ToolUsingStrategist.decide`, `unified_agent.py::_PILOT_SYSTEM`/`_TRIAGE_SYSTEM`/`_REPAIR_CRITIC_SYSTEM`, `roles.py::_DEVELOPER_SYSTEM` (repair), `trust/verifier.py::_SYSTEM` | `_UNTRUSTED_MEMORY_RULE` (whose own comment records "a label is not a rule") is appended for the two Researchers, the deep researcher and the memo rubric — not for the Strategist (whose brief splices a `UNTRUSTED_MEMORY_SUMMARY=`-labelled note and whose tool-using variant holds Memory/CrossRun/Knowledge/Web/Skill tools and sets `eval_parallel`/`policy`/`timeout`), the pilot, crash triage (`abandon`/`reject_idea` end a lineage), the repair critic, the script Developer's repair, or the verifier. `test_prompt_injection_rule.py` pins exactly the two Researcher prompts. | One assembler `agents/prompting.py::system_prompt(store, key, default, *suffixes)`; a test derived from `PROMPT_KEYS`. |
| AG-03 | M | C | `cli/__init__.py::REFUSAL_EXIT_CODE` comment, `docs/guide/cli-reference.md` "Exit codes" (`1` = crashed) | 24 deliberate `typer.Exit(1)` sites (file exists, handoff timeout, no champion to export, no endpoint, missing store, …), plus exits 3/4 from `comparability`; a script keyed on `1` retries "nothing to export" as a bug. Tally: `2` ×44, `1` ×24, `0` ×5, `3` ×1, `4` ×1. | Document `1` as "ran, negative outcome" (with 3/4) or route through `REFUSAL_EXIT_CODE`; pin the set. |
| AG-04 | M | C | `tests/test_cli_command_groups.py::GROUPS` (5 of 8 groups), `cli/export_cmds.py::harden`, `cli/run_cmds.py` (1,364 lines, unguarded) | `harden` writes `<memory_dir>/exploits.jsonl`, loaded by `orchestrator.py` into every later run's reward-hack scan — cross-run detector CONTENT under an "Export / diagnostics" header, against the stated rule; `run_cmds` hosts `reap-service-files` (destructive over the run ROOT) and `repair-log` unnamed in its docstring and above the ceiling the guarded groups are held to; the guard's docstring says "all 25 commands" (52). | Add the three groups to `GROUPS`; move `harden` beside the cross-run writers; derive the docs list from the same table. |
| AG-05 | M | P | `agents/cli_agent.py::CliAgentDeveloper._run`/`_prompt_delivery`/`implement`/`repair` | The external coding agent runs on the HOST (`subprocess.Popen`, `os.environ` minus secret-named vars, a temp worktree) — no sandbox tier, no read fence, no landlock, no network fence — fed `idea.rationale` and candidate stderr with no untrusted label; CLAUDE.md's probe row argues at length against exactly this surface one stage over; ADR-7's trust story is about what it may WRITE. | State the boundary; run under `landlock.no_mutation_source()`-style rules outside the worktree when `trust_mode != trusted_local`; label the spliced text. |
| AG-06 | M | C | `unified_agent.py::UnifiedAgent._TRIAGE_SYSTEM` vs `engine/failure_diagnosis.py::DIAGNOSED_FAILURE_REASONS` | "Choose from those FIVE only" while the registry enum has SIX (`check_false_positive`) and the field description recommends the sixth; the opening line names 6 of the 14 `FAILURE_REASONS` omitting the two diagnosable ones it later discusses. | Render the kind list from the registry; pin "names every member, no number". |
| AG-07 | L | C | `agents/cli_agent.py::CliAgentDeveloper.__init__` (`timeout: float = 600.0`), `factory.py::make_roles` | The external agent's wall clock is hard-coded, no `Settings` field, no doc; a timeout falls back to the task baseline so a repo task evaluates the untouched repo as a result. | `Settings.agent_timeout_s` + a row. |
| AG-08 | L | C | `cli/__init__.py::_DEV_BACKENDS`/`_BACKENDS` vs `core/config.py::DEVELOPER_BACKENDS` | Hand-written copies with no set-equality pin; the registry test scans `in *_BACKENDS` compares and `strat["developer"]=`, not `_choice(...)`. A preset added to core is refused by the flag and accepted by `--set`. | Import; one assertion. |
| AG-09 | L | P | `core/llm.py::llm_credential_consumers`/`llm_optional_credential_consumers` vs `LLM_ROLE_KEYS` | Preflight probes a hand-enumerated role set; a new stage key that gains a client is neither refused nor warned — the silent degradation preflight exists for. | Assert `LLM_ROLE_KEYS == strict ∪ optional ∪ external-stage-subtraction`. |
| AG-10 | L | C | `looplab/__init__.py::_LAYOUT` (288), `test_package_layout.py` (2×288 cases) | Tests-only (see XP-14); "many tests use old flat paths" no longer re-derives. | Shrink or generate; fix the sentence. |
| AG-11 | L | C | `tool_loop.py::CompositeTools.execute_result` and `::_run_tool_call` (identical 8-line `accepts_cancel` probe); emit-spec dict literal ×22 repo-wide (10 in `agents/`); literal `15` beside `JUDGE_MAX_TURNS` at 5 sites | Small duplications the constant's own docstring argues against. | `_accepts_cancel(fn)`; `tools/_base.py::emit_spec(...)`; import the constant. |
| AG-12 | L | C | seven sites | Comments no longer describing code: the `_NoTools()` sentinel "unnecessary" (still passed); `verifier.py` "live wiring in `engine/strategy.py`" (it is `verifier_tiebreak.py`); `cli/__init__.py` lists 4 groups (8); CLAUDE.md "the ONE structured-judge invocation both verifiers share" (4 callers); `run_cmds.py` docstring; `deep_research.py::state_brief` shadows the imported `render`; "all 25 commands". | Fix with AG-04/AG-10. |
| AG-13 | L | C | `docs/guide/cli-reference.md` (50 of 52; missing `reap-service-files`, `memory-orphans` — both destructive), `llm-and-agents.md` prompt-key table (18 of 19; missing `repair_critic_system`) | Documentation gaps on public surfaces. | Generate both tables from the registries. |
| AG-14 | L | C | `trust/cv.py::kfold_indices`/`purged_walk_forward`/`consistent_cv`/`Evaluator` (0 prod callers), `verifier.py::calibrate`/`LabelledCase` (0), `reward_hack.py::calibrate_detector`/`SEED_CALIBRATION_CORPUS` (0), `confirm.py::confirm_top_k` function (0) | Library code kept as "documented seams"; the calibration harnesses the docstrings say an operator should gate the trust mode on are reachable from nothing. | Expose through `looplab inspect`-style commands or mark declined/delete. |
| AG-15 | L | C/P | `cli/export_cmds.py::harden` (two hard-coded "legit" snippets), `trust/harden.py::ExploitSuite.scan` (`re.search` per rule per node, unbounded) | The paper's key finding is guarded by two inlined snippets; hand-edited rule patterns run against every node's scan surface with only `re.compile` validation. | Take the honest corpus from evaluated nodes; bound `scan` like `log_tools` bounds model regexes. |
| AG-16 | L | C | `agents/reachability.py::task_onboarder_llm_roles` (bare `ValueError`), `strategist.py::make_strategist` (bare `ValueError`), `unified_agent.py::UnifiedAgent.last_budget_exhausted` (one slot, two producers) | Residual bare refusals; a facade slot whose correctness rests on documented call ordering. | `ConfigRefusal`; two named slots with the registry updated. |
| AG-17 | L | P | `tool_loop.py::drive_tool_loop` (the `(emit_after or emit_force) and (tools is not None or self_plan)` gate) | With tools=None, `self_plan=False`, both budgets 0, a model hallucinating varying tool calls gets a fresh observation each turn and the `emit_force` ceiling is off — each turn a paid call. Shipped defaults close it. | Count `call_turns` toward `emit_force` whenever anything was CALLED. |

Tracked: `roles-module-still-a-god-module`, `make-roles-backend-wirings-not-split`,
`strategist-developer-field`, `temporal-leakage-flags-the-boundary`, `target-leakage-is-linear-only`.

**Top moves.** (1) AG-01 + the containment-polarity AST funnel. (2) One prompt assembler. (3) A
CLI contract registry (`cli/registry.py` rows: name, group, side-effects, exit codes) from which
`GROUPS`, the reference and the exit-code table derive.

### 3.10 SE — search

**Shape.** 26 files / 12,638 lines in three clusters — selection (`policy.py`, `card_selection.py`
2,086, `operators.py`, `scorer_fidelity.py`), calibration (`speculation_quality.py` 3,367,
`speculation_calibration.py`), and the concept cluster in its declared DAG order with its consumers
and three CLI-only offline analytics — plus the role wrappers. Selection is replay-deterministic
(all four policies pure over the folded state, no RNG, id-sorted; the package's only RNG is the
surrogate's, seed 0, whose output lands in `node_created`). Layering holds (0 `engine`/`serve`
imports). The concept DAG is exactly the declared order and pinned. The ASHA dark-GPU fix is
consulted only where the masked view is built. The calibration gate's ordered phases carry their
receipt. 37 `except Exception` across 12 modules, 18 of them in `speculation_quality.py` failing
CLOSED into `errors`.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| SE-01 | M | C (driven) | `card_selection.py::card_lane_width`, `::_lane_limit`, `::_default_select`, `::_selection_after_forced_gates` | For `EvolutionaryPolicy` the lane width is `elite`, for `MCTSPolicy` `n_seeds`, and the extra slots are filled from EVERY eligible card by score — no set restriction like `_asha_lane`. Driven on a 5-node board: MCTS proposed `improve(3)`, the lane executed `improve(3)` AND `improve(0)` (the worst node); same for Evolutionary. `card_driven_selection` is on by default and the Strategist may switch policies mid-run; the fidelity matrix is pinned "greedy-only" as a fact. | A legal-action SET per policy (`SearchPolicy.legal_card_keys(state)`: elites / UCB top-K / `{exact}`), filtered before `_lane_limit`; add both policies to the fidelity matrix. |
| SE-02 | M | C | `concept_tagging.py::tag_text_llm` (comment promises "bound and secret-redact … an explicitly untrusted data envelope … forbid embedded instructions"; code interpolates `f"ITEM:\n{text}"` raw) | On an ADMISSION input: `novelty.py::_graded_novelty_precheck` → `graded_novelty.tag_idea_llm` → the proposer's own unbounded theme/rationale/hypothesis; a level-4 grade short-circuits the dedup gate. The two LLM taggers also describe different fields while claiming "the SAME structural fields". | One shared bounded `tagger_item(text)` envelope for both taggers; implement the comment or delete it and mint a marker. |
| SE-03 | M | C | `policy.py` (`KIND_*`, `META_*`), `card_selection.py::_asha_lane` (`"_rung"`, `"_promoted"`), `_protected_due_action` (`"_reason"`), `_policy_metadata_for_card_action` (`"_chosen"`); kind literals `draft` 9 / `improve` 11 / `merge` 7 … | The registry is decorative — one guarded read justified by a comment about drift, everything else literals; the engine reads meta keys as literals 15× and as constants 0×; `_asha_lane` returns an empty lane on a missing `_promoted` with no red test. | `ACTION_KINDS`/`ACTION_META` frozensets with the two-way scan. |
| SE-04 | M | C | `concept_map.py::derive_reference_concepts` (`except Exception: … return []` under an imperative TODO comment); consumer `concept_cadence.py::_concept_coverage_snapshot` | A provider outage and "no blind spots" return the same value; the snapshot records `tag_mode: "llm"` with no failure field and `already_covered_at` suppresses re-derivation for the node-count window; open work with no marker. | Return `(items, available)` or raise to the guarded driver; record `importance_mode`; mint the marker. |
| SE-05 | L | C | `card_selection.py::_NOVELTY_LEVEL_CREDIT` vs `graded_novelty.py::_LEVELS`/`_RECO` | Two int-keyed tables and a third string vocabulary with no guard; a sixth grade scores as ungraded silently. | Derive the credit table's key set from `_LEVELS`; assert equality. |
| SE-06 | L | C | `research_targeting.py`, `taxonomy_dedup.py`, `novelty_recall.py` (490 lines, 21 tests, 1 production importer each: `cli/concept_cmds.py`) | Docstrings promise a "Phase 2" wiring that does not exist; a map reader cannot tell offline instruments from live machinery. | Markers per promise, or a `search/diagnostics/` sub-package named CLI-only. |
| SE-07 | L | C | `policy.py::_make_evolutionary`/`_make_mcts`/`_make_asha` (drop `operator_bandit`), `configuration.md` row | `operator_bandit` is silently inert under every policy but GreedyTree and the row does not say so; the `thorough` profile turns it on. | Say "greedy only" or refuse the pairing at `make_policy`. |
| SE-08 | L | C | `graded_novelty.py`, `lock_in.py`, `novelty_recall.py`, `research_targeting.py`, `taxonomy_dedup.py` (`from concept_tagging import <fn>`) | The module-object rule stops at the cluster boundary; a patch through `concept_tagging` reaches the analytics and not the grader. | Extend the split-guard test to every consumer. |
| SE-09 | L | C | `speculation_calibration.py` (two "spelled literally rather than imported … `search` importing `core.config` would be a new dependency" comments; the module imports `Settings` from `core.config`) | A false reason for two literals. | Import; delete the comments. |
| SE-10 | L | C | `policy.py::legal_actions` docstring | Still documents the Debug action F5 removed. | Drop the clause. |
| SE-11 | L | C | `concept_cadence.py::_tag_hypothesis_concepts` (stamps the node tagger's `mode`) over `tag_text_llm` (degrades to the heuristic with no receipt) | A hypothesis tagged by the fallback is written `mode: "llm"`. | A `producer` return. |
| SE-12 | L | P | `operators.py::merge_idea` (reads `p.idea.params`) vs the folded `metric_provenance.applied_params` | The one engine-authored numeric proposal mean-merges DECLARED params the record itself flags as false (the `params_overridden` caveat's own example). | Prefer applied coordinates per key; record provenance in the rationale. |
| SE-13 | L | C | CLAUDE.md ("five search modules import `agents`" — four), `events/types.py` (cites `concept_graph.py::tag_text_llm` after the move) | Stale counts and citations. | Fix; the citation guard should see these. |
| SE-14 | L | C | `research_targeting.py::research_targets` | Documents three `kind`s, emits a fourth (`derived-important`); skips normalization. | Document; normalize. |
| SE-15 | L | C | `hybrid_merge.py::HybridRetriever.candidates` vs `novelty_recall.py::_jac` | Two Jaccard tokenizations; the diagnostic orders the retriever's pairs by a different rule than produced them. | Expose `_tokens`/`lexical_similarity`. |
| SE-16 | L | C | `concept_lens.py::project_hierarchy` (dead `graph`/`edges`), `concept_graph.py::Concept` (parent encoded twice; a DESIGN NOTE recording three reviews the drift caused); the prefix rule spelled in 8 places | Dead parameters and an untagged design question. | `parent_of_id()` once; a marker or a decline. |
| SE-17 | L | P | `card_selection.py::_diversity_key`/`_default_select` | The one-per-niche pass keys on raw self-authored `concept_tags`, so with a lane width > 1 (SE-01) a card can claim a fresh niche by minting a slug; reorders preference only. | Key on the canonicalized membership or `(operator, parents)`. |

Tracked: `asha-promotion-mask-blocks-all-production`, `calibration-corpus-revoked-by-unrelated-settings`,
`concept-skeleton-matches-no-run` (still true), `classifier-rewrites-authored-membership`, the
declined `receiptless-work-reads-as-question`.

**Top moves.** (1) A legal-action set per policy, stated once (SE-01/17). (2) One action
vocabulary with a two-way guard (SE-03). (3) Typed availability for every LLM-derived analytic
(SE-04/11).

### 3.11 SC — serve control-plane core

**Shape.** 29 modules / 19,498 lines, four subsystems sharing one `AppState` bag: the durable
command lifecycle (`run_commands.py` 3,430 — record store, spawn/execution/activity leases, the
cross-process sequencer AND the worker in one 96-method class, with its validator already cut out
into `control_validation.py`: five registries asserted equal to `CONTROL_EVENTS`, 31 events); the
assistant (`assistant.py` 2,414 — session store + share store + prompt/toolset/`run_turn` + four
tool classes, reusing `drive_tool_loop`; `assistant_watch.py` a clean injected scheduler); engine
process management (`engine_proc.py`, `launch.py` — the P0 confined task-file read is real —,
`owner_token.py`); composition (`server.py` middleware, `router_wiring.py`, `appstate.py` caches,
the paid-work kits, `settings_store` with three locks in a fixed order, TUI as a stdlib client).
Layering holds; routers never import routers (pinned). 140 routes: 93 sync handlers on the
threadpool, 38 async; every request-path fold found is on a sync handler or explicitly offloaded.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| SC-01 | H | C (absence) / P (reach) | `serve/assistant.py::system_prompt`, `::run_turn`, `::expand_mentions`; `tools/machine_runs_tools.py::MachineRunsTools.execute` (`read_run_logs`/`read_run_trace`); `routers/assistant.py::_watch_run_turn`; `tools/perm_modes.py::_ACTION_RISK` | The Boss route wraps run evidence in `UNTRUSTED_RUN_EVIDENCE` + `BOSS_EVIDENCE_GUARD` (`llm_context.py::boss_prompt_parts`); the assistant returns candidate stdout/traces as bare tool results and splices `@run:` summaries into the user turn with no guard sentence, while in `auto` mode `write_file`/`edit_file`/`apply_patch` (reversible) and `finalize_run`/`stop_run`/`resume_run`/`reset_node`/`extend_budget`/`set_directive`/`git_commit`/`remember`/`concept_merge` (consequential) run with no approval — and a standing watch runs unattended at its pinned mode. No test drives injected log content through `run_turn`. | Wrap run-derived tool results and `@run:` blocks in the same envelope; an assistant twin of the guard; a driven test: injected `train.log` → `run_turn(mode="auto")` → no run-control/write call fires. |
| SC-02 | M | C (measured) | `run_commands.py::RunCommandService.sequence`, `::__init__` (`_run_locks: dict[str, threading.RLock]`) | The RLock passes a nested acquisition, then a second `flock(LOCK_EX|LOCK_NB)` contends with the thread's own first descriptor and spins to `lock_acquire_timeout` (60 s) before a 503 — measured: nested `with svc.sequence(rd)` → 503 after exactly the timeout. The RLock is a decoy that invites nesting; no production nesting found today; `run_activity`'s docstring warns about one such case by hand. | A `Lock` + owner-thread record; raise immediately on re-entry; pin. |
| SC-03 | M | C (absorbing) / P (trigger) | `run_commands.py::run_activity` (the `finally` swallows `HTTPException, OSError`), `::_active_command_ids`, `::resolve_active_claims`, `::_owner_exactly_alive` | If the cleanup unlink fails (a 503 sequencer timeout, EIO on the mounts), the `.activity_*.json` stays, carries this server's pid, counts as active, and every reset/delete/clear-trace answers `409 active_claim_owner_alive` until the process exits — planted and confirmed. | A heartbeat/`kind` on the claim; let `resolve_active_claims` retire an own-pid activity claim whose owning context is gone (process-local live-token registry); log-and-retry the unlink. |
| SC-04 | M | C | `serve/jupyter.py::setup_looplab` (`protected_shell = bool(env LOOPLAB_UI_TOKEN)`), `owner_token.py::resolve_owner_token` (exports the minted token into `os.environ`), `server.py::_index_response` (`X-Frame-Options: DENY` when tokened) | The Launcher tile decides `new_browser_tab` in the jupyter-server process at extension load, before the token is minted in the child; under a hub env with no token the tile opens in-frame and the child refuses framing. Three of the "four other readers" the export is justified by run in OTHER processes or read `srv.owner_auth_enabled`; `is_secret_env` keeps the export from any child. | `new_browser_tab = bool(env token) or (on_shared_origin() and not anonymous)`; drop or restate the export; test under `_hub`. |
| SC-05 | M | C | `control_validation.py::_normalize_inject_node` ("origin must be a JSON object or null"), `::_import_cross_run_source` (the server-stamped path), `replay.py::_on_node_created`, `ui/src/Dag.jsx`, `routers/reviews.py::_SUMMARY_OMIT_KEYS` | The normalizer refuses a client supplying any of `forked_from`'s server-stamped fields, yet accepts an arbitrary `origin` dict — `{"run_id": "rubert-dr-0807", "node_id": 9, "metric": 0.8776}` is written verbatim and rendered as verified lineage with a metric; `reviews.py` scrubs it as portfolio-disclosing. `hypothesis_added.source` is the same shape, smaller. | Mint `origin` only inside `_import_cross_run_source`; refuse a client `origin` with the `fork_receipt_forged` shape (or accept `{run_id, node_id}` and re-derive). |
| SC-06 | M | C | `run_commands.py::TERMINAL_STATUSES`, `routers/control.py::RunCommandRecord.status`, `tui_api.py::_COMMAND_*`, `tui.py::_COMMAND_*`, `tools/machine_runs_tools.py::_COMMAND_*`, `ui/src/commandModel.js` | The command-status vocabulary is spelled six times and is absent from `protocol.py`, whose docstring calls itself the home of the shared string contracts; no test pins any copy. | `protocol.py::COMMAND_TERMINAL_STATUSES`/`COMMAND_PENDING_STATUSES`; a `test_protocol_vocab.py` that pins the JS set. |
| SC-07 | M | C | `run_commands.py::RunCommandService` (3,147 lines / 96 methods: record store 17/197, leases + liveness 24/696, worker phases 9/593, observation predicates 38/1,006, submit/get/retry 3/374, `sequence` 79) | Four modules in one class; doc 25 SC-07 split `_execute` INSIDE it. | `command_store.py`, `command_leases.py`, `command_sequencer.py`, `command_worker.py`; the class stays as the façade (~170 `srv.commands.*` sites). |
| SC-08 | M | C | `serve/assistant.py::SessionStore` (+ fork ledger, ~750 lines), `::ShareStore` (~560), the runner, four tool classes | A session store, a capability store and an agent runner in one file whose docstring calls it "the dependency-light core"; `assistant_watch.py` shows the intended shape. | `assistant_sessions.py`, `assistant_shares.py`; re-exports. |
| SC-09 | M | C | `routers/control.py::build_router::control` ("KNOWN GAP (needs a deprecation, not a patch)") | The legacy route has no idempotency identity and no mandatory generation fence; a lost-response retry re-appends `budget_extend`/`inject_node`/`fork`/`deep_research` — paid or additive — and the gap is outside the marker index (`grep 'OPEN\[' looplab/serve/` → 0). First-party clients use `/commands`. | Mint the marker; `deprecated=True` + a `Deprecation` header now; migrate the 41 suite sites. |
| SC-10 | M | C | `tui_format.py::slug` (twin of `routers/genesis.py::_normalize_genesis`), `::spec_ready` (`EvalSpec._command_or_stages`), `::phase_meta` (the UI badge table), `::history_for_boss` (`Dock.jsx::buildHistory`), `::is_critical` (`isCritical`) | Five hand-synced twins with "must stay in step" comments and no cross-check; `spec_ready` decides whether the TUI fires `/api/start`. | Move `slug`/`is_critical`/launch-readiness to `core`/`adapters` and import from both; publish or pin the badge table. |
| SC-11 | L | C | eight serve files re-deriving `FILE_ATTRIBUTE_REPARSE_POINT`, `control_validation.py::_relative_file_name` re-spelling `WINDOWS_RESERVED` | The `pathsafe` single-spelling rule broken eight times with no stated reason. | Import; a negative pin. |
| SC-12 | L | C (probed) | `server.py::make_app::_reject_untrusted_host` comment ("runs FIRST … readable by any peer") | Starlette inserts at index 0, so the built stack (outermost first) is `_volatile_api_no_store → _require_token → _reject_cross_origin_mutation → _reject_untrusted_host`; bad Host + no token → 401, with token → 421. No regression (pure gates); the disclosure argument rests on a false premise; `test_server.py` pins one 421, not the order. | Fix the comment; assert the order over `app.user_middleware`. |
| SC-13 | L | C | `SettingsStore.credential_status` (0/0/0/0), `.settings_env` (0/0), `.refresh_env_secrets` (0 prod / 3 tests), `.prime_env` (documented no-op), `RunCommandService.cancel_external_preclaim` (1 `getattr` probe / 0 tests), `server.py::_RAW_GET_SUFFIX`/`_RAW_GET_EXACT` (0 readers), `engine_proc.sweep_stale_lifecycle_locks` (POSIX no-op called at every startup under an "F22: GC" comment) | Dead or test-only surface. | Delete; make the startup call Windows-only at the site. |
| SC-14 | L | P | `run_commands.py::resolve_spawn_claim` (route `POST /api/start/{run}/resolve-claim`), `::resolve_active_claims` | Four remediation strings name these routes as "the move"; 0 UI callers (curl only); `resolve_spawn_claim` driven by 0 tests by name. | A driven route test; a UI affordance on the `engine_start_uncertain` banner. |
| SC-15 | L | C | `LOOPLAB_JOB_INLINE_WAIT`, `LOOPLAB_GENESIS_INLINE_WAIT`, `LOOPLAB_UI_SRC`, `LOOPLAB_UI_PORT` (0 docs hits); the `cmd_*.json` record, `__looplab_revision__`, assistant `meta.json`/`messages.jsonl`, `.shares`, `.watches` (no guide page) | Undocumented knobs and on-disk formats; `docs/guide/ui.md` has no API/protocol section. | A `docs/guide/control-plane.md` generated from `CONTROL_SPECS`. |
| SC-16 | L | C | `run_commands.py`/`control_validation.py` docstrings + CLAUDE.md ("the 35 named per-event rules": 27+10+7 = 44 slots / 36 functions); `settings_ui_schema.py` (12 dated "N rows since …" paragraphs beside a DERIVED constant) | Counts that no longer re-derive. | Derive or delete. |
| SC-17 | L | P | `run_commands.py::RunCommandService.submit` (`gate_before`, `_standing_hint_duplicate`, `_decision` → `srv.state(rd)`), `control_validation.py::_ControlIntake.state` | Up to four full folds per submit inside the sequencer while the memoized `CommandObservation.state()` sits beside it (44 ms per `Event(**o)` pass on a 12 MB log). | Thread the observation's `state()` through; keep `normalize_control`'s lazy fold as the one fresh one. |

**Top moves.** (1) One evidence boundary for both agents (SC-01, with AG-02/TO-06). (2) Split
`run_commands.py` along the groups it already contains (SC-07). (3) `protocol.py` owns the
command-status vocabulary and the TUI twins become imports (SC-06/10); make the sequencer's
re-entrancy a stated rule (SC-02/03).

### 3.12 SD — serve destructive transactions and read-side services

**Shape.** 28 modules / 15,371 lines: three destructive whole-run transactions (Replay
`reset_route.py` + `reset_transaction.py`; deletion `deletion_service.py` + `deletion_transaction.py`;
`trace_clear.py`) on two shared tiers (`core/fence.py` markers, `serve/durable_op.py` receipts);
the paid scope-report ledger (`scope_report_store.py` 1,644 + `scope_actions.py` + `scope_sources.py`
+ `scope_report.py`) — the FOURTH durable-receipt machine in the package; the memory cascade and
the service-file reaper; and the read side (`public_cards.py` 1,457, `reviews.py`, `attention.py`,
`concept_frame.py`, `log_pages.py`, `artifacts.py`, `run_projections.py`, …). The receipt/fence
layer is in unusually good shape (paranoid double-`lstat` loads, read-back-confirmed strict writes,
closed key sets, phase lattices stated once and driven); every destructive body is off the ASGI
loop; the quiescence ladder is complete against every writer the review could enumerate. What is
wrong is the DEPLOYMENT of the mechanism and the seams around it.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| SD-01 | H | C (trace; premise = the tree's own 2026-08-13 geesefs measurement) | `reset_route.py::_durable_archive_move` → `atomicio.durable_no_replace_rename` (no `unique_destination`), `::_archive_forward`, `::_publish_and_archive` | The reset MARKER and the `archiving` receipt are published BEFORE the first rename; on geesefs the rename raises `EINVAL`, the route answers 425 "retry this exact operation" — a remedy that fails identically every retry — and from then on the marker fences every writer, deletion refuses `run_reset_in_progress`, the lattice has no exit from `archiving` except an archive CONFLICT, and no route or CLI calls `clear_run_reset_marker` (its only caller is the transaction). One click turns a run into a directory nobody can resume, replay OR delete; the backlog records exactly this state on the box (`live-cards-0804`) attributed to the sidecar leak. The stated TOCTOU justification is weaker than written (the rename runs under all six locks + a same-generation conflict check). | Operation-unique archive names + `unique_destination=True`; accept both shapes in `_valid_artifacts`; a `looplab reset-abandon <run>` exit (→ `superseded` + marker cleared, from `prepared|archiving` only); a flag-refusing-libc transaction test asserting a failed archive leaves the run deletable. |
| SD-02 | M | C | `deletion_service.py::begin_or_resume_run_deletion` (resume on `quarantine_ambiguous`), the `os.name == "nt"` branch after `mark_deletion_quarantine_ambiguous` — both return `_pending(...)` | The absorbing state — "requires manual storage recovery" by `_check_transition` — is answered `retryable: true`, the precise lie `_wedged` was introduced to end; 0 tests name `delete_quarantine_outcome_unknown`. | Route both through `_wedged` with `blocking_entries`; add the test. |
| SD-03 | M | C (latent) | `service_reaper.py::plan_service_file_reap` (no branch for `_TRACE_CLEAR_RECEIPT_PREFIX`; 0 mentions), `trace_clear.py::_pending_trace_clear_for_lifecycle`, `::_load_trace_clear_receipt` | Every successful clear leaves `.trace-clear.<stem>.tc_<id>.json` in the run root forever (a fifth unreaped population, in the directory the run list `lstat`s every poll), and every NEW clear strict-loads all of them — one malformed sibling 503s every future clear of that run. | A fifth reaper rule; a registry test deriving every root-level prefix from its writers; skip-and-report an unreadable sibling unless it is a matching PENDING one. |
| SD-04 | M | C | `trace_clear.py::_save_trace_clear_receipt` (7 sites), `::_load_trace_clear_receipt` vs `durable_op.py::save_receipt` | A third receipt store with a strictly weaker save: no immutable-identity check, no `check_transition`, no read-back confirm; an open key set where reset/deletion are closed. SC-06 was created because one copy quietly gaining a fix the other misses was the observed failure. | A `ReceiptProtocol` for trace-clear; route the seven saves through `save_receipt`. |
| SD-05 | M | C (mechanism) / P (cost) | `routers/reports.py::get_scope_report` → `scope_report_store.py::_action_bound_scope_record_is_confirmed` → `_write_scope_action_receipt` (strict fsync + parent fsync + read-back) inside `_scope_store_lock` (process-global + interprocess); `::_prior_learnings_index` (up to 256 records per Genesis call) | Every GET of an action-bound report re-publishes its receipt durably; `strict_fsync` is single-flight with a 5 s deadline raising `TimeoutError`, so on the mount `best_effort_fsync` exists for, a READ of a confirmed report becomes `quarantined: true, stale: true` and parks every other scope read behind the global lock. | Confirm-once: persist `confirmed_at` on the first successful strict re-publish; keep the strict path in `read_reconciled_action`/`abandon`. |
| SD-06 | M | C | `scope_report_store.py::_stat_identity` (drops `st_ctime_ns`, 16 + 4 uses), `::_is_link_or_reparse` (byte-for-byte `pathsafe.is_reparse` with a different fallback) | The identity CAS inside the read of every receipt, fence and lease marker in the paid ledger carries a weaker tuple with no stated reason — the subset rule CLAUDE.md requires a justification for (`scope_sources._file_identity` has one; this does not). | Import `is_reparse`; switch to `file_identity` or add the SC-11 justification; extend the fence test's AST sweep to `serve/`. |
| SD-07 | L | C | `reset_route.py::_prepare_receipt` (writes `.looplab-reset-task-<op>`), `reset_transaction.py::complete_reset_if_observed` (no cleanup on `succeeded`) | A successful Replay leaks its frozen task stage (16 MiB-capped) into the run directory forever, listed by the artifact browser. | Unlink on `succeeded` or archive under the manifest's stamp. |
| SD-08 | L | C (mechanism) / P (cost) | `metrics_adapters.py::TensorBoardAdapter.read` (`glob(**/events.out.tfevents.*, recursive=True)`), `engine/workspace_seed.py::link_input` (mounts are symlinked INTO the workdir), `routers/runs.py::node_metrics` (polled by the Inspector) | Recursive `glob` follows directory symlinks (verified), so a node whose task mounts a large corpus pays an O(dataset) walk per poll on a FUSE mount. | `os.walk(followlinks=False)` or prune the declared mount names; a symlinked-mount test. |
| SD-09 | L | C (narrow) | `service_reaper.py::_live_lifecycle_digests`/`plan_service_file_reap` (decides) vs `apply_service_file_reap` ("re-derives nothing") | A run recreated under the same name between plan and apply makes a new engine flock the OLD inode which apply then unlinks — the per-inode race the module exists to prevent. | A narrowing re-check at apply. |
| SD-10 | L | C | `run_projections.py::run_summaries` (`except Exception: continue` per run) | A run whose fold raises for a code defect vanishes from `/api/runs` with no receipt, where `routers/attention.py` marks its feed `partial`. | Emit a stub row (`unreadable: true, error_kind`). |
| SD-11 | L | C | `deletion_service.py::_detail` (`retryable` bool), `reset_route.py::_pending`/`trace_clear.py::_trace_clear_pending` (425 + prose), `scope_report_store.py` (`error_kind`/`ambiguous`); 17/16/~20/10 distinct error codes | "Retryable" is spelled three ways across four vocabularies with one meaning; no guide states the receipts, the quarantine layout or the purge body (`ui.md`: `operation_id`, `retryable`, `425`, trace-clear — 0 mentions each). | One closed enum `{retry_same_operation, wait_then_retry, new_operation, human_repair}` asserted over every 425/409/503 site; a `docs/guide/destructive-operations.md`. |
| SD-12 | L | C | `artifacts.py::_list_artifact_files` + `_artifact_exposure_policy.exposed` vs `routers/runs.py::artifact` (`base not in target.parents` → 404) | Listing follows a file symlink and applies only the trace-internal check, so an agent-planted `out/x -> /etc/passwd` is LISTED with the target's size/mtime; content is correctly refused. | Apply the parent check inside `exposed()`. |
| SD-13 | L | C | `appstate.py::AppState.run_dir`, `deletion_service.py::_plain_run_path`/`_strict_existing_run`, `reset_route.py::durable_reset_run` (inline), `scope_sources.py::_run_path` | Three spellings of "a plain direct-child run id" with slightly different refusal sets. | One `appstate.plain_run_path(run_id, *, strict)`. |

Tracked: `deletion-identity-leaked-before-refusal`; doc 40's memory-purge residuals; the
`atomicio` Windows item.

**Top moves.** (1) Unwedge Replay on the deployment filesystem (SD-01) — the only finding that
destroys operator recourse on the box today. (2) One receipt protocol + one service-file registry
(`core/service_files.py` holding every root-level prefix) (SD-03/04). (3) Retryability as a rule
instead of a vocabulary (SD-02/11).

### 3.13 SR — serve routers (the HTTP surface)

**Shape.** **140 registered routes** (132 in `routers/*`, 7 in `server.py`, 1 in `jobs.py`),
inventoried by AST into `routes.tsv` (method, path, handler, sync/async, auth plane, mutates,
paid, fold, body style, tests, clients, docs): runs 30, assistant 21, org 16, cross_run 13, misc
13, control 12, reviews 10, boss 7, server 7, reports 4, genesis 3, collaboration 2, attention 1,
jobs 1; GET 76 / POST 49 / DELETE 7 / PUT 5 / PATCH 3; 97 sync / 43 async; 64 mutating + 2 GETs
with documented durable side effects; 14 paid; 31 full-fold-on-request; owner 125 / reviewer 7 /
anonymous 2 / public-capability 1 / static 5. Auth is deny-by-default on `/api/*` when a token is
resolved; every file-serving route traced confines its path — **no auth-plane or traversal hole
found**. What the review adds is at the rule level.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| SR-01 | H | C | `routers/control.py::control` ("KNOWN GAP (needs a deprecation, not a patch)") | = SC-09: no request identity, `expected_seq` optional, a lost-response retry re-appends paid/additive intents; outside the marker index; 41 suite call sites; first-party clients already use `/commands`. | Mint the marker; `deprecated=True` + `Deprecation` header; migrate the suite. |
| SR-02 | H | C (sites) / P (cost) | `runs.py::_assert_historical_generation` (4× per historical `node_detail`), `::_assert_artifact_generation` (3× per `artifacts`/`artifact`), `::recover_concept_lens_receipt` (holds the sequencer across `read_all`), `reviews.py::_bound_run`, `control.py::start_status` — vs the lock-free before/after CAS in `_begin_trace_read`/`_finish_trace_read`, `node_logs`, `collaboration.py::_assert_still_current` | Two spellings of the read-side generation fence; one takes the EXCLUSIVE per-run command sequencer (RLock + flock, 503 on timeout) for a GET — 34 `commands.sequence(` sites in `serve/`, 17 GET handlers reaching one transitively — so a Files click or a reviewer poll contends with the command worker while it folds the log twice. | One `generation_fence(srv, rd, expected, *, sequenced=False)`; an AST test that no GET handler's call tree reaches `sequence(`, with the lens-recovery routes as named exceptions. |
| SR-03 | M | C (probed) | `server.py::make_app` (`_reject_untrusted_host`, `_require_token`) | = SC-12, plus: `_require_token` resolves a review header before `_unauth_api_ok`, so `GET /api/health` with a bogus/expired `X-LoopLab-Review` → 401, defeating the "zero-model liveness for an untokened monitor" contract in `misc.py::health`. | Register the host guard outermost; check `_unauth_api_ok` before the review branch; pin the order. |
| SR-04 | M | C | `routers/misc.py` — the authoring operation store (~800 of 2,172 lines: receipts, an interprocess lock, a 4,096-receipt quota, a v1 schema), the paid LLM-health probe registry (~385 lines — a SIXTH hand-rolled paid-work protocol), the memory-tier projection (~225) | Two durable-protocol subsystems and a read model in the grab-bag router, while `scope_actions.py`/`trace_clear.py`/`reset_route.py` are the house pattern for exactly this and cannot be tested without building the app. | `serve/authoring_store.py`, `serve/llm_probe.py`, `serve/memory_projection.py`; an AST guard that `misc.py` defines no `RuntimeError` subclass and calls `_interprocess_lock` only inside route bodies. |
| SR-05 | M | C (tally + probe) | all routers: 409 ×107, 400 ×71, 404 ×48, 503 ×21, 422 ×7, 500 ×6, 413 ×4, 410 ×3, 428 ×2 | Three malformed-body answers coexist (`http.py::json_object` → 400 on 39 hand-parsed routes, pydantic → 422 on 9, `/api/start` → structured 400); six `HTTPException(500)` sites answer an UNREADABLE snapshot where every sibling answers 503 with a `code`, and `run_config` reflects an `OSError` text carrying a host path; no doc states the rule. | A status table in `serve/http.py` + `tests/test_refusal_vocabulary.py` (no `HTTPException(500` in routers; the pydantic-body set = the 422 set). |
| SR-06 | M | C | `GET …/cost`, `GET …/log`, `POST …/chat`/`suggest`/`chat-compact`, `POST /api/assistant/sessions/{sid}/message`, `GET …/shares`, `GET /api/tasks`, + the 2 already deprecated; `cross_run.py::_steward_client`'s `getattr(srv, "engine")` branch | Nine routes with no first-party caller (grep over `ui/src`, `tui*.py`, `assistant*.py`, `tools/`, `cli/`); the boss `chat`/`suggest`/`chat-compact` trio is a second LLM chat plane beside the assistant; one dead branch reads an `AppState` attribute nothing assigns. | The SR-13 precedent: `deprecated=True` + header now, delete after one release; record the list in `ui.md`. |
| SR-07 | M | C (counts) / P (wall-clock) | `runs.py::_run_config_payload` (2 folds per `GET …/config`), `_put_run_config_locked` + `_repair_trust_gate_event` (3 folds + 1 `read_all` per PUT), `artifacts`/`artifact` (2 folds + 3 sequencer acquisitions), `node_metrics` (2 folds per 4 s Inspector poll); 13 `srv.state(` sites in runs.py | Uncached full folds per request are not a stated budget; `AppState.state`'s "DELIBERATELY uncached: engine invariant #4" over-reads #4 (which forbids caching across ENGINE loop iterations) while the server already caches `state_payload` by `file_identity`. | `AppState.folded(rd)` LRU keyed by `file_identity(events.jsonl)`; rule: one fold per (identity, request). |
| SR-08 | L | C | `control.py::resolve_start_claim` (inlines `(root / run_id).resolve()` + partial prefix checks) | A seventh, weakest spelling of the run-id validator: accepts names `launch.py::safe_run_dir` refuses and, by pre-resolving, defeats `resolve_spawn_claim`'s own symlink check. A new member of the tracked `run-path-validators-not-unified`. | `safe_run_dir(root, run_id, check_conflict=False)` as `start_status` does. |
| SR-09 | L | C/P | `GET …/cards/{card_id}/trace` (0 test hits for `cards/`), `GET …/deletions/{operation_id}` (0), `GET …/sessions/{sid}/fork/{action_id}` (P) | Three routes with no HTTP test; nothing asserts the route SET is covered, so a new route lands green. | Derive the inventory from `app.routes` in a test and assert each `(method, path)` appears in a recorded `TestClient` manifest. |
| SR-10 | L | C | `server.py::make_app` (`version="0.1.0"`), every router | Unversioned; `response_model` on 22/140; 38 hand-parsed bodies publish a schema only where `openapi_extra` is used (4); `docs/guide/ui.md` names 30 of 140 templates; no `Deprecation`/`Sunset` headers. | A generated `docs/guide/api-reference.md` from `app.openapi()` under `mkdocs --strict`; pin `(method, path, deprecated)`. |
| SR-11 | L | C | `server.py::_volatile_api_no_store` (12 path predicates) vs `Cache-Control` set in 28 handler sites | Cache policy spelled twice with no registry; in tokenless mode the polled routes in neither list carry no cache header. | A `NO_STORE_ROUTES` registry in `protocol.py` with a two-way scan. |
| SR-12 | L | C | `server.py::_RAW_GET_SUFFIX`/`_RAW_GET_EXACT` (0 readers, describe a surface that has grown), `router_wiring.py::router_builders` docstring (12 mounts listed, 13 mounted — `cross_run` missing), `appstate.py::_PUBLIC_STATE_RAW_KEYS` ("served WITHOUT the UI token") vs `server.py::_unauth_api_ok` (SSE is not exempt) | Documentation-as-code that predates deny-by-default. | Delete the constants; fix the docstring; reword the appstate comments as defence in depth. |

Tracked: `control-start-record-not-a-paid-ledger-spec`, `generate-scope-report-endpoint-still-in-router`,
`concept-lens-subsystem-inside-runs-router`, `generation-conflict-envelopes-hand-built` (35 sites),
`deletion-identity-leaked-before-refusal`, `run-path-validators-not-unified`, and two declines.

**Top moves.** (1) One read-side generation fence, lock-free by default (SR-02). (2) A
per-request fold budget (SR-07). (3) A stated refusal vocabulary with a guard (SR-05), and the
route inventory as a contract (SR-09/10).

### 3.14 U1 — the six largest React components

**Shape.** 19,111 lines (AssistantBar 4,469 · panels 3,288 · RunView 3,140 · Inspector 3,042 ·
RunList 2,986 · CardBoard 2,186). The house pattern is REAL where it exists: every model CLAUDE.md
names imports zero React and is consumed by the component beside it; `STAGE_SUPERSEDED_ICON` has
one definition and one reader; the doc-40 `expectedGeneration` defect is fixed with no sibling;
**0** `dangerouslySetInnerHTML` in `ui/src` (one filtered same-origin SVG-sprite `innerHTML`);
22 of 22 sampled client paths resolve to a router; effects clean up. What is NOT in models is the
bulk: AssistantBar's three layouts read ~98 component-scope names (144 with the shared
sub-renders), RunView's retained-work block is 201 lines / 51 `retained*` consts recomputed per
SSE frame (tracked), 7 hand-rolled polling loops beside `usePoll` (tracked). All 166 `disabled=`
expressions resolve to booleans; 4 gates rely on a `false` prop default (the fail-open shape; no
live hole).

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| U1-01 | H | C | `ui/test/assistantBarResourceTruth.test.js`, `forkFromSeqPanel.test.js` (SSR-loads RunView), `runList*.test.js` (mount hooks/`TreeNode` only) | AssistantBar, RunView and RunList's default export are never mounted by any test — 10.6k lines (55 % of scope) covered by a vite compile check plus source-text pins; 7 of the 17 hub panels are rendered by no test (Overview, Sensitivity, Failures, Queue, Compare, EventExplorer, Artifacts). The suite's own history (a dropped brace passing 767 tests) is the recorded cost. | A shared `mountWithFakeApi()` (jsdom + path-keyed fetch stub + fake timers) and one gate-flip test per component (`historical` disables the composer; `mutationReadOnlyMode` removes the settings button; `navigationBusy` disables the run menu). |
| U1-02 | M | C | `Inspector.jsx::TABS` + an inline list; `RunView.jsx::LIVE_INSPECT_TABS`/`READ_ONLY_INSPECT_TABS`/`allowedInspectTabs`; `runRouteState.js::RUN_ROUTE_TABS`/`REVIEW_*_TABS` | The Inspector tab vocabulary is spelled four times and RunView re-derives Inspector's own tab decision; a tab added in one but not the other is silently rewritten to Overview with "X is unavailable for this node or access level". | One pure `inspectorTabs({readOnly, readOnlyReason, evidence, sweep})` in `runRouteState.js`; a truth table. |
| U1-03 | M | C | `CardBoard.jsx::_CardKanbanCard` (`presentation === 'lane'` branch after 8 `useState` + 4 `useEffect`) | Lane cards pay the detail card's full hook budget for state they never read, per card (≤ 256) per 2.5 s poll; the file's own one-component argument is about `cardControlReflected`, which lives in the parent. | `_CardLaneCard` (hook-free). |
| U1-04 | M | C (traced) | `RunView.jsx::mutationReadOnlyMode` (prop), `::panelAllowed` (three allow-lists), `Inspector.jsx::Inspector` (nulling `runId`); `panels.jsx::QueuePanel::cancel`/`ResearchPanel::steer` submit controls with no `readOnly` prop | "Read-only" is enforced by three different mechanisms and is not statable as one rule; `_CardKanban`/`CardWorkspace`/`TrustPanel` default `readOnly = false`; safe today by construction of three lists. | A `PANEL_CAPABILITIES` table (`{name, mutates, historySafe, startOverSafe, reviewSafe}`) deriving the sets; a scan test over `CONTROL.*` sites. |
| U1-05 | M | C | `panels.jsx` (authoring journal, ~120 lines) vs `authoringRecoveryStorage.js` (27 lines, read by RunView — two readers of one prefix with different validators); `CardBoard.jsx` (hypothesis-delete journal, ~130); `AssistantBar.jsx` (fork recovery, ~40) | Two durable-recovery journals still live inline in components while comments/deletion/start-over have modules; doc 25 UI-04's reason for not extracting `AuthoringPanel` (hoisting `usePanelResource`) expired when `useScopedResource` landed. | A generic `recoveryJournal.js` (`prefix`, `validate`, `keyOf`; inspect/save/clear with CAS) instantiated by the six stores; extract `AuthoringPanel.jsx`. |
| U1-06 | M | C | 4 dialogs (`AssistantBar.jsx::deleteConfirmDialog`, `RunList.jsx::Modal`/`RunDeleteDialog`/`RunBulkDeleteDialog`), ≥ 8 resource-failure banners in 5 files | Each dialog re-spells overlay + `alertdialog` + focus + `aria-busy`; each banner re-decides status→copy and status→`role`; RunList's "the ONE place a mutation alert reflects server text" rule is not shared with `ArtifactsPanel`/`TrustPanel`, which reflect server text. | `ConfirmDialog.jsx`, `ResourceNotice.jsx` driven by `resourceModel` status; a pure copy table. |
| U1-07 | L | P | `RunView.jsx` (the `!panelAllowed(panel)` effect → `setPanel(null)` → `confirmRetainedPanelClose()`) | `window.confirm` can fire from an effect on a state the operator did not choose (a timeline scrub); on Cancel the URL and the screen disagree. | Route directly in the effect; keep the confirm for operator gestures. |
| U1-08 | L | C | `Inspector.jsx::StageLog` (`<pre className="training-log">`, no `tabIndex`/`role`) | The live training tail is not keyboard-scrollable, unlike its two sibling `<pre>` regions. | `role="region" tabIndex={0} aria-label`. |
| U1-09 | L | C | `Inspector.jsx::Trace` (`statusLabel` 🔧 🔀 ✍️ 🏋 ⏳), `::StageLog` (📄) | Colour emoji against the file's own monochrome rule (5 glyphs; 0 in the other five files). | `OpIcon` names via the `STAGE` tuple. |
| U1-10 | L | C | `runRouteState.js::KNOWN_KEYS`, the three panel allow-lists, 17 storage keys + 6 prefixes | The route grammar, the allow-lists and the browser-storage keys have no documented contract (`ui.md` names `?panel=memory|authoring|gpu` only; storage keys documented once, in doc 25). | A `storageKeys.js` registry + a test; a ui.md section derived from U1-04's table. |
| U1-11 | L | C | `RunView.jsx` (`liveStatus`/`liveLabel` two 8-arm ternaries) vs `RunList.jsx::effectiveRunStatus` | Two vocabularies for one run lifecycle (`done` vs `finished`, `on` vs `running`). | `runIndex.js::liveBadge(run, connected)`. |
| U1-12 | L | C | `AssistantBar.jsx` (two window listeners re-subscribed per keystroke; `ll.asstW` written per `sideW` change), `RunView.jsx` (`ll.sideW`/`dockH` per drag frame), `RunList.jsx` (history `replaceState` per filter keystroke) | Effect/persistence hygiene. | Refs + debounce. |
| U1-13 | L | C | `RunView.jsx` (three ~45-line rAF focus-settlement loops with identical bookkeeping) | Adjacent to the tracked retained-work item. | `useSettledFocus(...)` + a pure reducer. |
| U1-14 | L | C | `CardBoard.jsx::HypothesisBoard` (0 src consumers; 17 test/comment hits), `panels.jsx`'s `Panel` re-export (0 consumers) | Production-dead exports. | Delete; annotate the harness-only exports. |
| U1-15 | L | C | `RunView.jsx::renderNodeInspector` ("fifteen props" — 21; a paragraph duplicated 20 lines apart) | Comment drift. | Fix. |
| U1-16 | L | C | `CardBoard.jsx::_CardTrace` and `Inspector.jsx::Trace` (same `traceDeadlineGet(...)` + settle logic + the same measurement comment) | The card-trace read implemented twice. | `hooks.js::useCardTrace(...)`. |
| U1-17 | L | C | `Inspector.jsx::MetricCurves` (`setResource(r => r ? Array.isArray(r) ? r : [r] : false)`) | A fourth private load/stale/retry machine beside `useScopedResource`. | `useScopedResource` with `validate` + `pollMs`. |

Tracked: `runview-retained-work-machinery`, `assistantbar-runllm-and-fork-saga`.

**Top moves.** (1) The mount harness (U1-01) — until something mounts them, every other fix to
these files ships on pins. (2) The panel-capability table + `inspectorTabs` model (U1-02/04). (3)
`TraceSurface.jsx` out of Inspector (~1,500 lines with an existing external consumer), which also
removes the lazy-import bundling constraint.

### 3.15 U2 — every other component + the API client family

**Shape.** 47 `.jsx` outside the six giants, the `api.js` barrel over `apiClient.js` /
`commandModel.js` / `commandProtocol.js` / `commandStorage.js` / `eventStream.js` /
`scopeReportActions.js`, `reviewRouteApi.js`, `hooks.js`, the Vite config and the bundle gate. The
security boundary is small and single-sited (`apiClient.js::_authHeaders`, `::reviewReadPath`,
`::assertNotReviewMutation`, `runMode.js::assertRunMutationAllowed`); every mutation is a durable
command with an Idempotency-Key and a generation CAS. The API-path inventory (`client_paths.tsv`,
99 rows / 87 templates) shows 75 rows built inside the client family and **24 built in
components, hooks or models**. Roughly half the components have no model sibling. Dominant
structural defect: the barrel guard pushes leaf modules to re-declare protocol constants (~40
copies of `/^[0-9a-f]{64}$/`, 9 UUID regexes, 8 `[408,425,429]` lists, 6 timeout constants).

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| U2-01 | M | C | `LaunchCard.jsx::validReviewedSettings`, `launchDraft.js::LAUNCH_RUNTIME_FIELDS`, `LaunchCard.jsx::runExists`/`launchAmbiguous`; `ui/test/launchCardSemantics.test.js` | The PAID launch surface hand-copies seven server bounds (equal today) although `settingsSchema.js` already fetches the schema with bounds; a run-id conflict is matched by regex on the message while the server emits `run_id_conflict`; tests are pins only. | Bounds from the schema loader with the typed table as a pinned fallback; branch on `err.code`; one mounted preflight→start→status test. |
| U2-02 | M | C (mechanism) | `commandStorage.js::loadCommandTransport` (`protocolInvalid: true, canResubmit: false` when a payload has a key outside the four key sets) | Command envelopes are unversioned, so a deploy that adds one envelope key turns every in-flight envelope in an open tab into a dead "protocol invalid" chip — while two sibling stores in the same tree version theirs. | A `schema` field + a one-step migration ladder; older → `canResubmit: true`. |
| U2-03 | M | C | 24 path builders in `Dock.jsx`, `ConceptView.jsx`, `ConfigPanel.jsx`, `Report.jsx`, `RunCompare.jsx`, `RunView.jsx`, `PortfolioConcepts.jsx`, `Settings.jsx`, `panels.jsx` (5), `settingsSchema.js`, `commandProtocol.js::getRunGeneration`; `api.js::gpuStat`/`getSettings`/`getSettingsSchema` (no component caller) | The review rewrite, auth headers and deadline policy are properties of the WRAPPER, so a path built at a call site inherits whichever wrapper that site picked; `commandProtocol.js::_job` runs `reviewReadPath` over `/api/jobs/…` (a no-op) while `commandFetch` uses no translation. | A `runReads.js` sibling of `api.js` and a two-way scan for `/api/` literals outside the family. |
| U2-04 | M | C | `commandModel.js::UUID_V4_RE`/`COMMAND_ID_RE`/`TRANSIENT_HTTP` vs copies in `ScopeReport.jsx`, `CommentsThread.jsx`, `Settings.jsx`, `LaunchCard.jsx` (×2); `panelPrimitives.js::RUN_GENERATION_RE` vs inline regexes in `Report.jsx`, `Dock.jsx`, `CommentsThread.jsx`; `test/apiBarrel.test.js` | ~60 re-declarations because the barrel guard forbids the only sane import (models the barrel imports cannot import the barrel). | A `protocolConstants.js` leaf the guard admits (no imports, pinned by AST). |
| U2-05 | M | C (bare read) / P (cost) | `commandProtocol.js::getRunGeneration` (a bare `get(/state)` with no deadline before every durable submit); six deadline constants (8/10/12/15/70 s) across three wrappers | The ONE read before a paid click has no deadline on a route CLAUDE.md records at 4.3–15.6 s on a live geesefs run. | Through `commandFetch`'s deadline; one `DEADLINES` table. |
| U2-06 | M | C | `ScopeReport.jsx` (`generationFlights` Map, `ll.scope-report-generation.*`, `flightStorage` re-implementing `commandStorage.js::transportStorage`) | ~200 lines of durable-flight rules for a PAID action live in the component; the shared transport store cannot observe or reap them. | `scopeReportFlightModel.js`; envelopes through `commandStorage.js`. |
| U2-07 | M | C (dead branch) | `Settings.jsx` (`const credentialBlockedReason = ''`), `::LlmHealth`, `::launchGuardState` | The credential-blocked launch guard cannot fire — the reason is a literal empty string, so the provider-blocked rendering and the guard arm are unreachable; a third private storage protocol (`looplab.llm-health-recovery.v1`) sits inline. | Derive the reason from the health payload or delete the arm; move the store. |
| U2-08 | M | C (dup) / P | `CommentsThread.jsx::CommentComposer.submit`, `CommentCard.save`, `CommentCard.changeResolution`; `maxLength={8192}` vs `commentContract.js::COMMENT_MAX_BYTES` (8 KiB) | Three near-identical command sagas; the textarea admits 8,192 UTF-16 units against a byte contract, so a non-ASCII comment is refused only after a paid durable command is queued. | One `commentSagaModel.js`; byte-length the draft. |
| U2-09 | M | C | `docs/guide/ui.md` (route table), `globalNav.js`, `App.jsx`; 14 localStorage keys, 16 sessionStorage families, 2 `history.state` keys, 11 custom events in two spellings | No registry and no documentation for client state; the route table still says Runs = "list, map, portfolio comparison, projects" after the Lineage/Concepts relabel; `#/assistant/shared/<id>` and `?panel=fork` absent. | `storageKeys.js`, `uiEvents.js`, `routes.js` with two-way scans; fix the table. |
| U2-10 | M | P / C (dead) | `charts.jsx::MiniLine` (O(n) hover per pointermove), `::Trajectory`, `Inspector.jsx::MetricCurves` → `/nodes/{n}/metrics` (no cap found in `routers/runs.py::node_metrics`); `charts.jsx::Gantt`, `::MultiTrajectory` (0 consumers) | Unbounded series in the DOM with linear hover; two dead exports. | `?limit=`/bucketed series; binary-search hover; delete the exports. |
| U2-11 | L | C | `ConceptView.jsx::validateConceptPayload` (~190 lines), the ~500-line paid-lens saga; `api.js::submitConceptLens`/`abandonConceptLens`/… (no direct component caller) | The paid lens flow is testable only by mounting. | `conceptLensModel.js` + `conceptPayload.js`. |
| U2-12 | L | C | `Dock.jsx` (unused `workingId` import; `EventRow` always `autoOpen={false}`; a 430-line inline transport machine) | Dead prop/import; the Dock-only rules stay in JSX. | Drop both; `dockTransportModel.js`. |
| U2-13 | L | C | `ClaimsCuration.jsx::safeSource` vs `urlSafety.js::safeExternalHref`; `AssistantChat.jsx::exactRecoveryAvailable` vs `api.js::assistantRevert`; three time helpers vs `format.js` | Trust/identity helpers duplicated beside their canonical module; `rel` spelled two ways on five `target="_blank"` anchors. | Import the canonical helper. |
| U2-14 | L | C | `apiClient.js::_throw` (mines `cmd_…` out of free text under its own "branch on the code" comment), `CollabPanel.jsx::createFailureCopy` (`/LOOPLAB_UI_TOKEN/i`), `LaunchCard.jsx` | Four message-shaped contracts beside code-shaped ones; the server already emits codes. | Log-and-fallback, then remove. |
| U2-15 | L | C | `MapView.jsx::buildGraph`/`sortedRunConcepts`, `ResearchMemoCard.jsx::researchMemoTrust`, `Report.jsx::reportRefreshFailure`, `SharedAssistant.jsx` (an 85-line inline validator of a PUBLIC share payload), `ForkFromSeqPanel.jsx::parseForkParams` | Pure logic in JSX with zero tests. | Extract; `node --test`. |
| U2-16 | L | C | 32 `window.confirm` sites / 8 files | Blocking native dialogs gate destructive and paid actions; untestable under jsdom without stubbing. | One `ConfirmDialog` + `confirmModel.js` (with U1-06). |
| U2-17 | L | P | `ui/scripts/check-bundle.mjs::DEFAULT_BUDGETS` (JS 504 KiB, CSS 51 KiB gzip); last recorded 514,490 B / 50,957 B (2026-08-12) | The gate cannot run here (no `dist`); headroom ~0.3 %. | Record the measurement as a CI artifact. |

**Top moves.** (1) The `protocolConstants.js` leaf (U2-04/05). (2) Versioned command envelopes
(U2-02). (3) One home for API paths with a registry guard (U2-03).

### 3.16 U3 — the pure UI models and `ui/test`

**Shape.** 105 model/hook files (22,807 lines across the 112 `.js`), 189 test files (37,610
lines). The house pattern is real and mostly honoured: 61 test files drive models with no DOM and
**run here without `node_modules`**: 568 tests, 0 failures, 4.0 s. Every model file has ≥ 1
production importer (0 dead); 15 have no direct test import. 23 JS↔Python mirror pairs checked:
all agree in substance today, **3 are pinned across the language boundary** (comparability via a
shared fixture, `PHASE_TEXT` and `fmt` via Python tests reading JS source), 20 are not. No
`dangerouslySetInnerHTML`; narration becomes React text nodes; markdown hrefs go through
`urlSafety.js`.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| U3-01 | H | C | `portfolioModel.js::comparableRunRanking` (consumer `RunCompare.jsx`) vs `crossRunRank.js::crossRunGroups` + `runIndex.js::metricComparable` | The Compare view elects a "best" by raw min/max over one `task_id` + one `direction`, never consulting `sourceIncomplete` or `best_metric_comparability` — the two rungs `crossRunRank.js` documents as load-bearing (a run folded from a 20-of-1,624-record prefix "holds NO rank"). Two screens, two winners; the Compare view is where an operator picks a config to reuse. | Delete `comparableRunRanking`/`bestComparableRun*`; `RunCompare.jsx` reads `crossRunGroups`; keep `mixed-phase` as a `groupClaim` refusal. |
| U3-02 | H | C (arithmetic) | `panels.jsx::CrossRunPanel` (`metricCell` → `format.js::fmt`, `toPrecision(4)`), `crossRunRank.js::buildGroup`/`groupClaim`; the fix exists as `RunCompare.jsx::comparisonMetricFormatter` | Competition ranks over exact values render at 4 significant figures: `fmt(0.793426) === fmt(0.793411) === '0.7934'` — the two best numbers on the box — so rank 1/rank 2 print the same number with no `(tie)` while the claim sentence prints the unrounded value. `fmt(19915.75) → '19920'` for seconds. | Hoist `distinctMetricFormatter(values)` into `format.js`; use it in `CrossRunPanel`/`RunList`/`MapView`/`groupClaim`; durations via `fmtElapsedSeconds`. |
| U3-03 | M | C (static) | `runMode.js::assertRunMutationAllowed` ↔ `RunView.jsx` (`setRunAccess`) | RunView publishes six modes; the guard knows three and treats the rest as history, so a mutation while `unavailable` throws "Historical snapshot seq null is read-only — return to live to act". | A closed `RUN_ACCESS_MODES` table with `{code, message, mutable}`; unknown mode → protocol error. |
| U3-04 | M | C (JS) / P (mirror) | `grouping.js::groupKey` (`mode === 'niche'`: `${k}=${Math.round(n)}`), `layout.js::similarityRank`; Python `search/archive.py::_niche` (bucket width `resolution`) | Every sub-integer hyperparameter (lr 0.01/0.003/0.001 → `lr=0`) collapses into one niche and `autoCollapseSet` folds the nodes; the engine buckets by a configurable width. | Derive the key from the same discretization (publish `resolution`); one shared fixture. |
| U3-05 | M | P | `stageAttribution.js::STAGE_OK_STATUSES = ['ok','timeout','reused']`, `stageSupersessionNotice.failureText` | A `timeout` row has `failed: false`, so a strip of superseded timeouts reads "0 of them are failures"; the amber tone is deliberate, the `failed` count inherited it. | `failed = !['ok','reused'].includes(status)`; a `timeout` fixture. |
| U3-06 | M | P | `forkFromSeqModel.js::classifyForkFailure` (`STALE_PARENT_RE`) ↔ `control_validation.py` (two `f"stale parent #…"` sites) | The `moved: true` decision (nothing queued, re-read) is regex-matched from free text while the other refusals are keyed on `code`; a reword degrades a proven refusal into `applied: null`, which fences the submit button. | Emit `code: "fork_parent_stale"` with `detail`; keep the regex one release; pin the code on both sides. |
| U3-07 | M | C | `runDeletionRecovery.js`, `runStartOverRecovery.js` (near-identical; differ only in `UUID_V4_RE` vs `OPERATION_ID_RE`), `commentRecoveryStorage.js`, `reviewLinkRecovery.js`, `conceptLensRecovery.js`, `attentionStorage.js`, `authoringRecoveryStorage.js` | The durable-recovery envelope written five times with diverging validators: 8 storage-target spellings, 3 `safeRunId` (bounds 512/512/255 vs the server's 255), 6 UUID regexes in 3 shapes, `^[0-9a-f]{64}$` in 30 files while `panelPrimitives.js::RUN_GENERATION_RE` exists. | One `recoveryEnvelope.js` leaf; an AST test refusing a second declaration of those regexes. |
| U3-08 | M | C | `useAttention.js` (a hook + two unexported normalizers — the feed's honesty rules — reachable only through jsdom), `fx.js`, `inspectorDraftStore.js`, `runMode.js` (module-global + window events), `settingsLaunchGuard.js`, six fetch/timer modules | Hooks, DOM and I/O live inside "model" files; the pure/impure line is not derivable from the name (32 `*Model.js`, 9 `use*.js`, 10 `*Recovery/*Storage/*Store.js`, ~54 bare nouns). | Export the attention normalizers into `attentionModel.js`; a naming test (a file exporting `use*` is `use*.js` and exports only hooks). |
| U3-09 | M | C | `ui/test/*` — 71 of 189 files read production source; 7 pin-only (regexes over JSX such as `mode === 'finalization-stalled'\s*\n\s*&& stalledRemedy`); 48 build `new JSDOM(...)` inline; 69 create their own vite server; 14 wait on real `setTimeout`; 0 `mock.timers` | The suite's largest kind is the one CLAUDE.md's ladder ranks last; 9 components are reached ONLY by pins (incl. `LaunchCard.jsx`, which holds the paid Start button) and 6 by no test. | `test/_dom.js` + `test/_ssr.js` (one vite server per process); convert the 7 pin-only files; an allow-list test refusing new `readFileSync(..src..)` pins. |
| U3-10 | L | C | `launchDraft.js::LAUNCH_RUNTIME_FIELDS` (policy/profile/backend options), `settingsModel.js::ESSENTIAL_SETTING_KEYS` vs `search/policy.py`, `core/config.py` PROFILES, `settings_ui_schema.json` (already fetched by `settingsSchema.js`) | Two hand-written vocabularies shadow the served schema; equal today, unpinned. | Derive enum options from `settingsSchema.js::fieldByKey`; pin `ESSENTIAL ⊆ schema`. |
| U3-11 | L | C | `util.js::ASSISTANT_MODES`, `assistantRecovery.js::RECOVERY_MODES`, `assistantPermission.js::*_MODES`, `assistantDirectPolicy.js::DIRECT_MODE_DECISIONS` ↔ `tools/perm_modes.py::MODES` | Four JS spellings of the permission-mode vocabulary; none pinned to each other or to Python. | One `assistantModes.js`; a four-way equality test. |
| U3-12 | L | C | CLAUDE.md's `ui/` row ("`crossRunRank.js` ↔ … ↔ `engine/eval_contract.py`") | The browser mirrors `engine/comparability.py::comparability_status` (the ONE fixture-pinned cross-language pair); `eval_contract.py` is the run-level contract CLAUDE.md itself says is not wired into the store. | Correct the row. |
| U3-13 | L | C | `cardBoardModel.js` (5 line citations), `derivedMemory.js` (2), `CardBoard.jsx` (1); `conceptId.js` cites `core.models.valid_concept_id` (lives in `core/concepts.py`); `traceEpisodeModel.js::EPISODE_LABELS` carries a dead `handoff_summary` row (the span is `handoff-summary`) | The tracked `claim-ui-line-citations` says "four"; the tree holds 8 distinct on 10 lines; model headers carry point-in-time corpus counts. | Amend the marker; symbols not lines; delete the dead row. |
| U3-14 | L | C | `settingsLaunchGuard.js` (`readRetainedFence()` at module scope; module-global listeners/operations) | The paid Start gate's state machine has no `node --test` truth table because the module is a process-global singleton read at import. | `createSettingsLaunchGuard(storage)` factory. |
| U3-15 | L | P | `format.js::fmtDate` (`toLocaleString`), `assistantPermission.js::expiryLabel`, `RunCompare.jsx` vs `report.js::toMarkdown` (`toISOString`) | Times render browser-local with no zone marker; exports render UTC. | `timeZoneName: 'short'` or one header statement. |
| U3-16 | L | C | `commentsModel.js`/`runRouteState.js` (`/^[A-Za-z0-9_-]{8,160}$/`) vs `commentRecoveryStorage.js` and `events/comment_projection.py` (`^cmt_[0-9a-f]{32}$`) | Three client spellings of the comment id, one wider than the server's. | One `COMMENT_ID_RE` in `commentContract.js` with the legacy allowance named. |

Tracked: `node-graph-cannot-name-running-experiment`, `claim-ui-line-citations`,
`pareto-never-reaches-champion-selection`.

**Mirror pairs (23 checked).** Agree in substance: `stageRowSuperseded`, `forkIdeaFieldCarried`,
`buildConceptForest`/`runConstantConcepts`, comparability (FIXTURE-PINNED), `extraMetrics`
channels, command statuses, phases, watch statuses, `conceptShelf` sources, trace caps,
`normalizeConceptId`, permission modes, attention kinds, card blockers, card rollup chips,
`PHASE_TEXT` (PINNED), champion caveat slugs, `fmt` (PINNED), narration keys (108/108
registered), scope-report authorities. Mostly: launch-record statuses (client adds a never-emitted
`pending`). Dead: the `handoff_summary` label row. Wider: the comment-id reader.

**Top moves.** (1) One generated vocabulary fixture, two tests — the comparability fixture
generalized to every closed set the browser mirrors (19 silent mirrors become one red test). (2)
Delete the second comparability rule (U3-01) and hoist the precision-widening formatter (U3-02).
(3) The shared DOM/SSR harness and the retirement of pin-only files (U3-09).

### 3.17 DX — documentation architecture, CLAUDE.md, the diagram, the doc guards

**Shape.** Four tiers: the agent surface (`CLAUDE.md`: 232,919 B / 658 lines ≈ 58k tokens per
turn; 75 % is the package map, the `engine/` row alone 61,165 B; read by 2 tests); the user
contract (README + 16 guide pages, 1.04 MB; the data-driven diagram, 163 KB of which 137 KB is
narrative string literal); the design record (49 numbered docs ≈ 3.7 MB, four over 400 KB;
`BACKLOG.md` 590 KB / 6,783 lines / 64 headings); and the greppable index (at review time, before
this document's own 44: 103 `OPEN[…]` / 18 `DECLINED[…]` / 7 `CLAIM[…]` real markers in 26 files). Seven guard files / 46 tests, 45 green.
`mkdocs build --strict` passes in 11 s. Confirmed good: the quickstart runs offline as written
(6 nodes in 2.35 s; without `--backend toy` a clean exit-2 refusal naming the remedy); the
JupyterHub path is documented end to end; all 25 infographic numbers checked hold; all 100
CLAUDE.md symbol citations resolve.

| ID | Sev | Conf | Site | Finding | Proposal |
|---|---|---|---|---|---|
| DX-01 | H | C | `docs/guide/configuration.md` ("Web editors…"), `tests/test_config_docs_sync.py` | = CO-03: RED at baseline since `cc6a64e` (2026-08-30) — "184 of the 217" vs 185/218, the paragraph 4× duplicated, each copy also carrying a stale "176 catalogued keys"; CI has been red on the one guard that checks the settings page. | Derive the sentence from `SETTINGS_UI_SCHEMA`; collapse the copies now. |
| DX-02 | H | C | `CLAUDE.md` (package map, conventions) | 58k tokens per turn (~29 % of a 200k window); 93 date literals, 83 "measured", 1,619 numeric literals, 1,152 ALL-CAPS words, a 2,111-byte sentence. Re-deriving 28 named counts: 15 hold, **11 stale** ("35 control rules" → 31/44; "~170 `Engine(` sites" → 234; "653/471 citations" → 785/547; "11 TODOs → §21.20.13" → 5; "9 CODEX" → 8; "7 live REVIEW" → 1; "STILL OPEN 22 hits" → 5; "TWO §0.12s" → four doubled §s; "8,900+ tests" → 12,552; "eleven" FAILURE_REASONS → 14); 6 line citations remain against its own rule. | The CLAUDE.md diet (proposal P-DOC-1): keep Commands, a one-line-per-module map, the 7 invariants on one screen, the conventions as rules; every removed narrative leaves a pointer; guard `len ≤ 45,000` and "no `2026-` date outside Commands". |
| DX-03 | H | C | `CLAUDE.md#Commands` sync rule; `tests/test_config_docs_sync.py::test_every_settings_field_is_documented`; the diagram guards | Of the four stated doc↔code sync rules only fragments are machine-checked: (1) settings — NAME presence only (defaults unguarded; 0 wrong today by hand); (2) diagram numbers — 11 label tokens + order + geometry, none of the 25 `(field, value)` pairs; (3) event types → guide page — no page has a list (48 of 137 named in `concepts.md`); (4) cadences/thresholds — nothing reads them. | Generated settings table (P-DOC-2); an infographic number guard reusing the scratchpad script; a generated event-log page (EV-03). |
| DX-04 | M | C | `core/claimpin.py::citation_defects` (default `subtrees=("looplab",)`), `tests/test_claim_pins.py` | Citation checking covers `looplab/` only: docs hold 626 symbol citations (597 resolve, 11 dead paths, 17 dead symbols — incl. `events/replay.py::_card_debug_leaf_children` in doc 45, the very example claimpin's docstring names as fixed) and **2,434 line citations** (BACKLOG 304, doc 25 289, doc 43 147, doc 17 143); the maintained guide is clean on symbols (88/88) but carries 10 line citations; `tests/` has 57 defects. | Widen to `("looplab", "tests", "docs/guide", "CLAUDE.md", "README.md")` with a shrink-only baseline for the 57 test defects; refuse NEW `.py:NNN` in the guide. |
| DX-05 | M | C | `docs/BACKLOG.md`, `tests/test_open_item_index.py` | The index works where applied (103 OPEN, 0 duplicate slugs, guard green; 87 of 94 dated markers older than 7 days, mode 2026-08-19 ×26, oldest 2026-06-24) but the pools CLAUDE.md lists as untagged are still untagged (§2 Themes: 0 markers / 2 ⬜ / 12 🟡 / 45 ✅ / 3 "STILL OPEN"; §0.2 142.7 KB, 0 markers; §4–6; §1 Foundation not even on the list); BACKLOG overall ⬜30 / 🟡30 / ✅120 / 24 "STILL OPEN" / 94 dated amendments / 21 SURVIVOR; §0.12, §0.14, §0.15, §0.18 each appear twice and the sequence is out of order. | Split `§0.x` by date into `docs/backlog/…` or numbered docs; renumber; tag or strip the glyphs of the five pools. |
| DX-06 | M | C | `docs/00-INDEX.md`, `mkdocs.yml::nav` | The doc-25 row says 147/37/2/2 while doc 25's own guarded rollup is 148/38/2/0; "validated 2026-08-10" (docs 43–49 landed since); rows out of numeric order; 10 top-level docs, 3 `audit/` and `reference/` unindexed; 9 pages in neither `nav` nor `not_in_nav` (35–39, 44, 46, 47, `reference/…`) — doc 36, the STANDING design principle, is unreachable from the site nav. | Derive the doc-25 tally; sort; make the outside-nav set an explicit list the guard checks. |
| DX-07 | M | C | `docs/guide/cli-reference.md`, `tests/test_documentation_contracts.py::test_readme_and_guide_name_only_real_cli_commands_and_cover_the_registry` | 50 of 52 commands have a heading; `memory-orphans` and `reap-service-files` (both destructive, `--apply`) are absent and the guard accepts any guide page, so it stays green. | Generate per-command usage from Typer; guard one heading per registered command. |
| DX-08 | L | C | `README.md`, `docs/guide/installation.md`, `pyproject.toml`, `.github/workflows/docs.yml` | 6 extras declared, 5 documented, 3 in README; README badge "8.9k tests" and CLAUDE.md "8,900+" vs 12,552 collected; a stale CODEX comment in the docs workflow. | Derive the extras table; drop hand-typed counts. |
| DX-09 | L | C | `docs/guide/ui.md`, `serve/routers/*` | No API reference; `ui.md` names 21 of 121 normalized routes, 101 undocumented anywhere. | = SR-10. |
| DX-10 | L | C/P | `docs/` (38 dated + 12 undated numbered files; `18-` twice), `03-decisions.md` (ADRs stop at 18 while docs 36/44/45 carry decisions), `04-file-layout.md` vs `guide/concepts.md` (reconciled by banner; the tree omits `AGENTS.md`, `.spans-append.jsonl`, three lock files), `CODE_REVIEW.md` (72 line citations, all dead by its own account) | No archival tier, mixed naming, a stalled ADR cadence. | `docs/archive/` or `superseded_by:` front matter for 05–07, 10–17, 18A/B, 21–23, 26; `ADR-NN` in the title of any deciding doc + a row in doc 03. |

**CLAUDE.md anatomy (bytes → verdict).** preamble 524 keep · Commands 4,269 keep (move two
measurements) · `core/` 10,653 (~2/3 narrative) keep the map, move narratives to the docstrings
that already hold them · `events/` 4,023 keep, trim · `runtime/` 18,847 (~80 % measurement) keep
10 lines, point at doc 38 / BACKLOG §0.9, §0.12 · `tools/` 20,832 keep the ToolProvider contract +
one line per module · `agents/` 2,193 keep · `search/` 5,276 keep the layer rule · `trust/` 1,751
keep · `engine/` 61,165 (180 sentences retelling BACKLOG §0.6–§0.20) keep the 20-file list + the
three "must not" rules · `adapters/` 1,830 keep · `serve/` 19,493 (~40 % rules) keep the rules ·
`ui/` 29,328 keep the pattern + the pair list by name · invariants 11,957 keep 1–7 on one screen,
move each exception's history to the registry docstrings · conventions 5,446 keep · registries
8,207 keep the list, cut the incident stories · guard ladder 3,806 keep (compress) ·
`extra_metrics` 5,682 delete with a pointer to `core/models.py::EXTRA_METRIC_CHANNELS` · open-item
index 17,408 keep 15 lines + a pointer to doc 45. **Estimated rule content ≤ 45 KB (≈ 11k
tokens); the rest is history that is also — and better — recorded elsewhere.**

**Top moves.** (1) The CLAUDE.md diet with a byte budget. (2) Generated settings/CLI/route
tables with guards that compare, not grep. (3) Widen `citation_defects` to the guide, README,
CLAUDE.md and tests with a shrink-only baseline.

### 3.18 Suite health at the baseline

The full offline suite (`-m "not docker"`) run in this environment: **12,467 passed, 7 failed,
80 skipped in 25 min 38 s** (wall time under review load; CLAUDE.md says "a few minutes"). The
seven reproduce in isolation, so they are not flakes:

| Test | Class | What |
|---|---|---|
| `test_config_docs_sync.py::test_settings_catalogue_counts_and_profile_semantics_are_current` | real drift | CO-03 / DX-01 — the docs sentence says 184/217, code says 185/218, paragraph 4× duplicated. |
| `test_agent_containment_rule.py::test_triage_without_a_run_state_reaches_the_helper_with_binding_off` | test drift after a contract change | the test's spy does not accept the `on_budget=` keyword `unified_agent.py` now passes (the 2026-08-30 "terminal emit salvage is the CALLER's policy" change). |
| `test_repair_claim_without_write.py::test_budget_exhaustion_keeps_the_repair_instead_of_discarding_it` | regression or stale expectation | asserts the terminal salvage keeps the paid emit; the loop now returns `''` — same change as above; one of the two is wrong and neither was reconciled. |
| `test_calibration_profile_home.py::test_the_digest_did_not_change_when_the_profile_moved` | pin drift | a pinned sha256 of the calibration profile changed (`d7b686…` → `61cb98…`) — the calibration-receipt-revoked-by-Settings-growth shape already tracked as an open item. |
| `test_dev_probe.py` ×3 (`sqlite3` native writer, unix socket bind, `ctypes` into libc) | environment-conditional | `assert not target.exists()` — the kernel rung (Landlock) is not enforced in this container, so native writers create files; the tests do not skip when Landlock is unavailable, so they are red on any box without it (a portability defect in the tests, not in the fence). |

CI (`tests.yml`) runs the full suite on Linux 3.11 and Windows 3.12 (4 shards) with no lint and
no type-check, so at least the first four are red in CI at the baseline.

---

## 4. Ranked proposals (whole tree)

Ordered by leverage — what each buys against what it costs — not by scope. Each names the
findings it closes. "Fix now" items are one change each; "direction" items are one to three days
and change a shape.

| # | Proposal | Closes | Shape / cost / risk |
|---|---|---|---|
| P1 | **Take every paid and filesystem-heavy call off the engine loop** — the `_await_batch_proposal` shape (offload + capture sink + main-task publish) applied to the serial node build, fork/inject/rerun, the repair path (`_triage_crash`/`_repair`/`_repair_critic`), the cadence bodies, the eval FS steps, and the watchdog's citation stat; one driven ticker test per site. | ES1-01, ES2-01, ES2-04, EM-01, EM-08 (XP-01) | Direction, ~3 days. FIRST make `Developer.repair` return its change set (the freeze is what serializes concurrent repairs today). Risk: a moved `await` changes a CAS window — pin ordering with driven tests, not comments. |
| P2 | **Per-child containment with an `engine_error` terminal + "fail the node, never the run" as a guarded rule** — the `gpu_unpinnable` handler generalized at the three `_evaluate` callers; `run_command_eval` returns a `RunResult` for every malformed spec; `adapter` refused in secondary reader slots at submit; a mutation test that raising from any reader still yields a node terminal. | ES2-02, RA-01 (XP-02) | Fix now, small. Risk: a deterministic bug pauses the run with a terminal instead of crashing — the pause text is the disclosure. |
| P3 | **One run identity for cross-run READ models** — `core` helper `run_ref(row)` (uid, else `legacy:<run_id>`) used by the seven readers; a two-incarnation fixture per store. | EK-01/02/03 (XP-04) | Fix now, ~7 call sites. Risk: refs change spelling (`demo:3` → uid) — version the evidence digest. |
| P4 | **One untrusted-evidence boundary** — `core/evidence.py` (label + guard sentence) used by the Boss, the assistant, the tool results (web/arXiv/MCP), the taggers; one prompt assembler `agents/prompting.py::system_prompt(...)`; a test derived from `PROMPT_KEYS`. | SC-01, AG-02, TO-06, SE-02, SC-05 (XP-05) | Direction, ~2 days. Prompt strings are contracts: gate the rule like `memo_verdict_cue` (a Settings flag, legacy-snapshot default off). |
| P5 | **Unwedge Replay on the deployment filesystem** — operation-unique archive names + `unique_destination=True`; `looplab reset-abandon`; a flag-refusing-libc test asserting a wedged run stays deletable. | SD-01 | Fix now, ~150 lines. The only finding that destroys operator recourse on the box today. |
| P6 | **Registries for the six unguarded vocabularies** — engine terminal reasons, stage statuses, command statuses (in `protocol.py`, JS pinned), `ACTION_KINDS`/`ACTION_META`, `EVENT_PAYLOAD_KEYS` (+ generated event-log page), permission from `ToolCapability`; each with the two-way AST scan the existing registries carry. | ES2-03, ES1-06, RA-08, SC-06, SE-03, EV-03, TO-10 (XP-06) | Six small changes; mechanical. |
| P7 | **Exception posture made countable** — ruff with `BLE001` and the 634 `noqa`s turned into a reviewed allow-list; a `contain(span, reason)` helper stamping the enclosing span; the AST funnel "every broad `except` around a paid call re-raises `BudgetExceeded`"; `verifier.verify` fixed today. | AG-01, SD-10, SE-04, EK-05, ES2-13 (XP-03) | Direction; the allow-list review is the cost (~460 silent handlers, start with engine's 143). |
| P8 | **Typed engine state + an attribute guard, then the `EvalAttempt` split** — per-cluster state records declared once; an AST guard that every `self._x` read in `engine/` has exactly one declaring site; then `_evaluate` along its own phase comments and `orchestrator.py` along its non-fold boundaries (`reentry.py`, `setup_phase.py`, `EngineConfig`). | ES2-10, ES1-09, EM-06, EM-05 (XP-08) | Direction, a week in slices; each slice verified by the corpus-digest replay and the existing driven tests. |
| P9 | **One closed task-document schema, stated once** — `extra="forbid"` + grandfathered strip-and-warn on all five spec models; `STAGE_KEYS` closed; a per-kind `READER_KEYS` table; a `schema` stamp on `task.snapshot.json`; `validate_stages`' refusal reaches the model through the `declare_stages` bounce. | RA-02/03/04/14 (XP-11) | Fix now, ~1 day, one seam every submit surface passes through. |
| P10 | **Read-side HTTP rules** — one lock-free `generation_fence(...)` (no GET reaches `sequence(`); `AppState.folded(rd)` keyed by `file_identity` with "one fold per (identity, request)"; a refusal-code table in `serve/http.py` with a guard (no 500 for unreadable input); confirm-once for the paid scope ledger. | SR-02, SR-07, SR-05, SD-05, SC-17 | Fix now, three small changes + one measurement. |
| P11 | **Generated references with guards that compare, not grep** — the settings table from `Settings` (defaults compared), the CLI reference from Typer (one heading per command), an API reference from `app.openapi()` (one row per route, `deprecated` pinned), the event-log page from the payload registry, the tool inventory per composition; the doc-guard scope widened to `docs/guide`, README, CLAUDE.md and tests with a shrink-only baseline. | CO-03/17, DX-01/03/04/07/09, SR-09/10, AG-13, TO-15, EM-16, ES1-08f | Direction, ~2 days; retires four hand-maintained tables and the 4×-duplicated paragraph. |
| P12 | **The CLAUDE.md diet with a byte budget** — rules stay, dated measurements move to the numbered docs/docstrings that already hold them behind pins; guard `len ≤ 45,000` and no date literal outside Commands; the two missing packages added; the eleven stale counts removed rather than corrected. | DX-02, XP-09, XP-12, EM-10/11, SE-13, SC-16, AG-12, U3-12 | Direction, one careful pass (~1 day); buys ~47k tokens per agent turn. |
| P13 | **UI: mount harness + vocabulary fixture + protocol leaf** — a shared jsdom mount harness and one gate-flip test per giant component; a Python-emitted `ui_vocabulary.json` asserted by both suites (19 unpinned mirrors → one test); a `protocolConstants.js` leaf the barrel guard admits; versioned command envelopes; delete the second comparability rule and hoist the precision-widening formatter. | U1-01, U3-01/02/07/09, U2-02/04/05 | Direction, ~3 days across the three UI scopes. |
| P14 | **Cross-run store hygiene** — a store schema registry (name, version, validator, lock, writer) with a docs-diff test; governed reads snapshot bytes under the locks and compute outside; a paid-hygiene memo; `store_case` adopts the capsule writer's "persist or raise". | EK-04/05/08/09/10/12 | Direction, ~2 days. |
| P15 | **Small fixes with no design decision** — `RuleStrategist`'s width term (EM-02); receipts keyed on `acted` (EM-04); `_on_card_dropped` by type (EV-06); repair-ledger omission receipt (EV-01); `run_setup` as `EnvironmentRefusal` (ES2-06); the two `_ENUM_FIELDS` rows (CO-02); refuse unknown file keys (CO-01); `resolve_start_claim` through `safe_run_dir` (SR-08); the `quarantine_ambiguous` retryable lie (SD-02); the reaper's fifth rule (SD-03); `find_files`' `**/<dir>/<leaf>` prune (TO-08); `gpu_info` out of process (TO-05); the four bare head-cuts through `clip` (TO-02); `_dispatch_evals`' `hasattr` probes asserted (ES1-09); `seed_repo_tree`'s timeout branch (ES1-05); the systemic-stop registry (ES1-06). | as listed | Each one commit. |

## 5. Surface inventory

The public surfaces of the tree, what states their contract, what guards it, and where this
review found the contract weakest. Counts at the baseline.

| Surface | Size | Contract stated | Guarded by | Weakest point (finding) |
|---|---|---|---|---|
| `Settings` (env `LOOPLAB_<FIELD>`) | 218 fields; 67 legacy rows; 127 `EngineOptions` twins | per-field comments; the configuration table (all 218 named, 0 wrong defaults by hand) | `test_config_docs_sync` (names only; RED on the catalogue sentence), `test_options_divergence`, `test_engine_options` | file-layer unknown keys ignored (CO-01); two unvalidated enums (CO-02); the legacy-row rule is prose (CO-05) |
| Event log (`events.jsonl`) | 137 types (107 folded / 30 diagnostic); 205 `(handler, key)` reads | envelope + evolution rules in `types.py`; per-type payload NOT stated | `test_event_types` (partition, emitted literals — not `append_many`), splice tests | no payload contract (EV-03); 18 handlers alias raw data onto the wire (EV-04); the tools layer writes two folded types outside `CONTROL_EVENTS` (TO-03) |
| Control intents | 31 `CONTROL_EVENTS` + 10 collaboration; 44 rule slots | `protocol.py` + five registries asserted at import | `test_control_registry`, `test_run_command_service` (104) | the legacy `/control` route (SR-01/SC-09); client-supplied `origin` (SC-05); the command-status vocabulary absent from `protocol.py` (SC-06) |
| HTTP API | 140 routes (76 GET / 49 POST / 7 DELETE / 5 PUT / 3 PATCH); 22 `response_model`; 38 hand-parsed bodies | per-route docstrings; no version; no API reference (30 of 140 in `ui.md`) | `test_server` (144) + siblings; `test_router_wiring`; `test_serve_module_seams` | unstated refusal codes with 6 `500`s (SR-05); 17 GETs through the exclusive sequencer (SR-02); 9 client-less routes (SR-06); 3 untested (SR-09) |
| Task document | 5 spec models; 6 reader kinds; the stage manifest with 8 keys | pydantic + `validate_task`; `validate_stages` as the ONE definition (8 callers) | `test_repo_task`, `test_stage_contract` (32), `test_metric_reader_confinement` (23) | three open key sets (RA-02/03/04); `adapter` accepted where it crashes the run (RA-01); the adapter reader outside the fence (RA-05) |
| CLI | 52 commands in 8 groups; exit codes 0/1/2/3/4 | Typer help; `cli-reference.md` (50 of 52); the exit-code table (contradicted by 24 `Exit(1)` sites) | `test_cli_refusals`, `test_cli_command_groups` (5 of 8 groups) | 21 help strings cite dead `§` sections (XP-11); `harden` in the wrong group (AG-04); 2 destructive commands undocumented (AG-13) |
| Agent tools | 103 specs / 98 names across 28 providers; 33 writing tools; 3 network tools + MCP | per-tool docstrings; `ToolProvider` protocol; `perm_modes._ACTION_RISK` | `test_tool_provider_contract` (3), `test_bounded_tool_results`, `test_tool_collisions` (fakes) | two permission vocabularies unguarded (TO-10); 5 name collisions (TO-11); the MCP config surface (TO-07); `_mcp_transport` untested (TO-14) |
| Prompts | 19 `PROMPT_KEYS`; 37 `render(` sites; 9 system prompts in `agents/`+`trust/` | "prompt strings are contracts"; `PROMPT_KEYS` two-way scan | `test_prompt_keys`, `test_prompt_injection_rule` (2 prompts) | the untrusted rule on 3 of 9 prompts (AG-02); the triage prompt's "five" vs six (AG-06) |
| Cross-run stores | 9 JSONL stores + skills + curation ledgers | validators per store; schemas stated nowhere once | `test_claims` (97), `test_governance_health` (30), `test_trust_gates_reach_the_ledger` | `run_id`-keyed readers (EK-01/02/03); the unredacted skill body (EK-06); the docs table drifted (EK-12) |
| Run directory | `events.jsonl`, snapshots, `spans.jsonl` + index, receipts, markers, locks, per-node workdirs | `concepts.md` (accurate); receipts stated once each in their transaction | `test_durable_op_kit` (29), `test_fence_protocol` (21), `test_service_reaper` (26) | Replay wedges on geesefs (SD-01); trace-clear receipts unreaped (SD-03); the leaked task stage (SD-07) |
| UI routes / storage / events | hash routes + `KNOWN_KEYS`; 14 localStorage keys + 16 sessionStorage families + 2 history keys; 11 custom events | code only; `ui.md` names `?panel=` legacy trio and a stale route table | `runRouteState` tests | no registry, no doc (U1-10, U2-09); unversioned command envelopes (U2-02) |
| JS↔Python mirrors | 23 pairs | comments naming the twin | 3 pinned across the boundary | 20 unpinned (U3 mirror table); one wrong twin named in CLAUDE.md (U3-12) |
| CLAUDE.md | 232,919 B / 658 lines / ~58k tokens per turn | itself | `test_documentation_contracts` (row uniqueness), `test_open_item_index`, `test_claim_pins` (looplab/ only) | 11 of 28 re-derived counts stale (DX-02); two packages missing (XP-09) |
| Open-item index | 103 OPEN / 18 DECLINED / 7 CLAIM in 26 files | the CLAUDE.md convention; doc 45 | `test_open_item_index`, `test_claim_pins` | five untagged pools (DX-05); ~20 self-declared open items found by the scopes with no marker (ES1-08e, EV-09, EK-11, SE-04/06/16, SC-09, SR-01) |

## 6. What was refused, and what was not verified

**Refuted during the review** (recorded so nobody "fixes" them): `trust/critic.py::critic_findings`
is pure regex — no LLM call under `_write_lock`; `core/llm.py::CostAccountant.add` swallows sink
errors deliberately, so a ledger ENOSPC is not misreported as a provider failure; all eight node
terminals in `evaluate.py::_evaluate` are lexically under `_write_lock` as CLAUDE.md claims;
`ASHAPolicy` survivors and `rank_by_metric` agree on ties (both `(metric, id)`); the events fold
of six permutations of independent node blocks is byte-identical; `hardware.py`'s prompt no longer
contradicts the resource fence; `EV-10`'s obvious consequence (a stale generalization gap after
reset) does not occur because the finalize post-pass recomputes it; the review found no auth-plane
or path-traversal hole in the 140 routes, no `dangerouslySetInnerHTML` in `ui/src`, and no
production import through the compat shim.

**Not verified here, and why**: every corpus figure the code and CLAUDE.md quote (the `runs/`
directory is absent from the checkout — 0 run directories); first-appearance dates of any field
or default (the clone is shallow: 50 commits, all dated 2026-08-30); the UI bundle budget and the
`npm test` suite as a whole (`ui/node_modules` absent; the 61 dependency-free model test files
were run directly: 568 tests, 0 failures); the MCP transport (`mcp` SDK absent); the wall-clock
cost claims in SR-02/SR-07/SD-05/SD-08/TO-01/EK-08 (mechanisms traced, magnitudes not measured on
a live run); the Landlock-dependent `test_dev_probe` cases (the kernel rung is not enforced in
this container); anything requiring a reachable LLM endpoint.

**Left as PLAUSIBLE** rather than dropped: EM-03 (the citation bypass — the limit is documented,
the cost argument is the reviewer's), ES2-09 (the salvage-cause stuck rung), AG-05 (the external
agent's host reach), AG-17 (the tool-less loop), SE-12/17, U3-05/06/15, CO-05/14, RA-07/15,
SC-01's exploit reach, TO-05 (torch absent here).

## 7. How to work this document

This is a ledger, not a plan. Forty-four rows carried an `OPEN[…]` marker at their site (§8); the
rest do not, and that is deliberate — the CLAUDE.md rule is that a marker carries a falsifier
somebody re-derived, and minting one for a row whose predicate nobody can state is the unverified
glyph the convention exists to abolish. For an untagged row the path is: re-derive its evidence
against the tree (every finding names its symbol and, where it was driven, the reproduction), then
either fix it in one change or mint the marker at the site with the proof the row already states. When a row
is fixed or refuted, delete it from this document's successor rather than annotating it — the
2026-08-13 mega-review (doc 40) is the precedent for a CLOSED ledger whose sites became the code.

---

## 8. The markers this review minted

Forty-four items were tagged, each at the site it is about, each with a predicate evaluated
against the tree before it was written. Forty sit in the file the finding is about; the four below
are whole-tree items whose home is this document. **A red `tests/test_open_item_index.py` on one of
these is not a defect — it means the item shipped, and the fix is to delete the marker.**

OPEN[containment-is-unmeasured] 743 handlers catch `Exception`/`BaseException` in production and
458 of them neither re-raise, log, record nor assign; 636 `# noqa: BLE001` annotations decorate
them while no linter is configured anywhere in the tree, so the annotations document nothing and
nothing counts a containment. The review found the cost at the seams (a swallowed budget stop at a
selection site, a run dropped from the run list on a fold error, an outage that reads as a clean
verdict, a refused case the finalize step still marks done). Adopt the linter with that rule and
turn the existing annotations into a reviewed allow-list; then a `contain(span, reason)` helper so
a containment is countable rather than invisible. proof:missing:.ruff.toml

OPEN[claude-md-has-no-size-budget] the agent guide is 232,919 bytes — about 58k tokens on every
turn, roughly 29 % of a 200k window before a single file is read — and 75 % of it is the package
map, whose engine row alone is 61 KB. Re-deriving 28 of its named counts: 15 hold and 11 do not.
The rules an agent needs every turn are a small fraction of it and the dated measurements are also
recorded in the numbered docs and the module docstrings they came from. Guard the budget, and let
the narratives live where they are already written down.
proof:absent:CLAUDE_MD_MAX_BYTES@tests/test_documentation_contracts.py

OPEN[http-surface-has-no-generated-reference] 140 routes, 22 with a response model, 39 hand-parsed
bodies, no version, no deprecation headers, and 110 of the 140 templates named in no guide page;
three routes have no HTTP test and nothing asserts the route SET is covered, so a new route lands
green and undocumented. Generate the reference from the app's own schema under the strict docs
build and pin `(method, path, deprecated)`, the way the settings and CLI tables should also be
generated. proof:missing:docs/guide/api-reference.md

OPEN[largest-ui-components-are-never-mounted] AssistantBar, RunView and RunList's default export —
10,595 lines, 55 % of the six largest components — are named by 54 test files and mounted by none
(`jsdom` is already a devDependency, so the harness is cheap);
their coverage is a compile check plus source-text pins, which cannot see a `disabled` gate flip,
and the suite's own history records a dropped brace passing 767 tests. Seven hub panels are
rendered by no test at all. One shared jsdom harness (fetch stub keyed by path, fake timers) and
one gate-flip test per component. proof:missing:ui/test/_mount.js

### The site markers

| slug | home | the fix it is waiting for |
|---|---|---|
| `repair-path-holds-the-engine-loop` | `engine/evaluate.py` | offload the three paid repair calls |
| `eval-child-raise-cancels-every-sibling` | `engine/evaluate.py` | a shielded `engine_error` terminal |
| `eval-attempt-is-one-giant-method` | `engine/evaluate.py` | the `EvalAttempt` phase object |
| `serial-node-build-holds-the-loop` | `engine/orchestrator.py` | one offload helper for four sites |
| `paid-cadences-hold-the-engine-loop` | `engine/orchestrator.py` | offload under a capture sink |
| `engine-terminal-reasons-unregistered` | `core/models.py` | a registry + AST guard |
| `repair-ledger-drops-rows-without-a-receipt` | `core/models.py` | per-node bound + omission receipt |
| `reconcile-retires-another-incarnations-lessons` | `engine/lessons_reconcile.py` | key on the run uid |
| `capsule-readers-collapse-run-incarnations` | `engine/concept_capsules.py` | key on the run uid |
| `claim-receipts-group-by-run-name` | `engine/claims_health.py` | key on the run uid |
| `auto-skill-body-leaves-a-run-unredacted` | `engine/memory.py` | redact the body at both ends |
| `verifier-swallows-the-budget-stop` | `trust/verifier.py` | re-raise, then guard the polarity |
| `stage-manifest-keys-are-open` | `runtime/command_eval.py` | a closed stage key set |
| `stage-row-statuses-unregistered` | `runtime/command_eval.py` | a status registry + two-way scan |
| `adapter-reader-can-kill-the-run` | `runtime/command_eval.py` | refuse at submit, fail the node here |
| `adapter-reader-runs-outside-the-eval-boundary` | `runtime/command_eval.py` | thread the eval env through |
| `task-spec-models-ignore-unknown-keys` | `adapters/repo_task.py` | forbid extras, grandfathered |
| `config-file-keys-are-silently-ignored` | `core/appconfig.py` | refuse unknown keys per layer |
| `llm-reasoning-vocabularies-unvalidated` | `core/config.py` | two rows in the enum table |
| `replay-archive-has-no-unique-destination` | `serve/reset_route.py` | unique destination + an abandon path |
| `trace-clear-receipts-are-never-reaped` | `serve/service_reaper.py` | a fifth reaper rule |
| `absorbing-quarantine-answers-retryable` | `serve/deletion_service.py` | answer through the wedged form |
| `assistant-has-no-untrusted-evidence-boundary` | `serve/assistant.py` | one envelope, used by both agents |
| `command-sequencer-is-not-reentrant` | `serve/run_commands.py` | a plain lock that raises on re-entry |
| `command-status-vocabulary-has-no-home` | `serve/protocol.py` | the vocabulary, plus a JS pin |
| `inject-origin-is-client-supplied-provenance` | `serve/control_validation.py` | mint it server-side only |
| `read-fence-takes-the-command-sequencer` | `serve/routers/runs.py` | one lock-free generation fence |
| `no-per-request-fold-budget` | `serve/appstate.py` | one fold per (identity, request) |
| `tools-layer-writes-two-folded-events` | `tools/machine_runs_tools.py` | register both as control events |
| `foreign-run-fold-cache-thrashes` | `tools/_runcache.py` | one cache per root, bounded by bytes |
| `embedding-spend-is-unbilled` | `tools/vectorstore.py` | an accountant + an embedding cache |
| `card-lane-fills-outside-the-policy-population` | `search/card_selection.py` | a legal-action set per policy |
| `tagger-item-has-no-untrusted-envelope` | `search/concept_tagging.py` | the envelope the comment promises |
| `rule-fallback-picks-a-serialising-policy` | `agents/strategist.py` | a width conjunct |
| `event-payloads-have-no-registry` | `events/types.py` | a payload-key registry + generated page |
| `compare-view-has-its-own-comparability-rule` | `ui/src/portfolioModel.js` | read the ranking model |
| `ranked-metrics-print-fewer-digits-than-they-rank` | `ui/src/format.js` | hoist the widening formatter |
| `command-envelopes-are-unversioned` | `ui/src/commandStorage.js` | a version field + a migration step |
| `legacy-control-route-has-no-retry-identity` | `serve/routers/control.py` | sunset headers, then port the 41 call sites |

**What was deliberately NOT tagged**, so the index does not fill with rows nobody can falsify: the
structural proposals whose shape is a judgement call rather than a missing symbol (the package
splits, the god-module decompositions, the duplication clusters), every finding whose cost this
environment could not measure (§6), and the four red tests that are their own signal — a failing
guard is louder than a marker, and three of them are the Landlock item above.
