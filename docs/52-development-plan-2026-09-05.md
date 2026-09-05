# 52 · Development plan (2026-09-05): what to build, improve and fix next

**Status: a PLAN over the open-item index, re-derived against `master` on 2026-09-05.** It ranks;
it ships nothing and flips no default. Its inputs are the three things this repo treats as the
backlog — the greppable marker index (`grep -rn 'OPEN\['`, counted here with the guard's own parser
in `tests/test_open_item_index.py`), the whole-tree finding ledger of
[doc 50](50-architecture-review-2026-09-02.md) with its ranked proposals P1–P15, and the five
external-works items of [doc 51](51-external-works-synergy-2026-09-03.md) — read against the 71
commits that landed between 2026-09-01 and 2026-09-04. Where an item's status differs from what its
home document says, this page says which of the two the tree supports.

Two constraints shape the ranking and are stated up front. **The `runs/` corpus is absent from this
checkout**, so every figure quoted from a run below is quoted from the tree's own record and not
re-derived; items whose next step is a corpus measurement are queued for the GPU box in §5 rather
than ranked as if they could be advanced here. And the house rule that decides order is **cost of
leaving it, measured where a measurement exists** — a marker whose own text re-ranks it down (the
paid cadences, at 0.3 % of a run) is ranked down here too, whatever its severity label says.

Companion authorities: [doc 50](50-architecture-review-2026-09-02.md) (the finding ledger this plan
consumes), [doc 27](27-agent-system-mega-review-2026-08-09.md) (agent-system items),
[doc 25](25-architecture-modularity-review-2026-08-01.md) (the modularity ledger, 30 open),
[doc 34](34-review-deferred-decisions-2026-08-13.md) (product/architecture deferrals),
[doc 45](45-claim-surfaces-2026-08-20.md) (why a recorded number is pinned to the site that decides
it), [doc 36](36-agent-driven-decisions-2026-08-13.md) (the trust line every capability item below
must hold), [BACKLOG](BACKLOG.md) and [CODE_REVIEW](CODE_REVIEW.md) (the two prose ledgers whose
live rows carry markers).

---

## 1. Where the tree stands

### 1.1 The index, by home

Counted by the guard's parser on 2026-09-05: **103 open markers and 19 declines**, in 25 files.

| Home | Open | What lives there |
|---|---:|---|
| `docs/25` modularity ledger | 30 | god-module splits, duplicated scaffolds, hand-rolled guards — judgement calls, not defects |
| `docs/BACKLOG.md` | 23 | product gaps (Pareto, drift, MLflow, MCTS value, distributed eval), cadence and watchdog residue, claim ladder |
| `docs/27` agent-system review | 14 | budgets, receipts, cancellation, prompt governance, the eval ladder |
| `docs/51` external works | 5 | the cheap capability items (skills, kNN uncertainty, perception hook, literature) |
| `docs/50`, `docs/34`, `docs/CODE_REVIEW.md` | 4 + 4 + 4 | whole-tree items (containment census, CLAUDE.md budget, API reference, UI mounts); the four deferred product decisions; the four surviving review rows |
| `looplab/` (in code, at the site) | 14 | the loop holds, the eval method, the card lane, the payload registry, the legacy route, two caches, two measurements |
| `tests/` | 3 | three guards that state their own limit |
| `docs/29`, `docs/46` | 1 + 1 | the F3 follow-up; the `.py`-only params guard |

Of doc 50's twelve site markers, three closed in the sweep (the sibling-cancelling eval boundary,
the unlabelled assistant tool results, the read fence on the command sequencer) and nine stand.

### 1.2 What the 71 commits since 2026-09-01 closed, against doc 50's proposals

| Proposal | State on 2026-09-05 | Residue that decides the rank below |
|---|---|---|
| P2 per-child containment | **done** (`engine_error` terminal; `adapter` refused at submit) | — |
| P3 one run identity for readers | mostly done (`core/run_identity.py::run_ref` / `row_belongs_to_run`; lessons, capsules, claims health) | `claims_assessments.py::_qualify_refs` / `_ingest_evidence`, `claims_retrieval.py::portfolio_atlas`, `concept_shelf.py::run_concept_index` still key on `run_id` — the EK-03 half that demotes one-sided verdicts |
| P5 unwedge Replay | **done** (`unique_destination` in `serve/reset_route.py`) | — |
| P6 six vocabularies | 4 of 6 (engine terminal reasons in `core/models.py`, stage statuses in `runtime/command_eval.py`, command statuses in `serve/protocol.py`, the `KIND_*`/`META_*` constants now read by `search/card_selection.py`) | `EVENT_PAYLOAD_KEYS` (a documentation job, its marker says so); permission still decided by `perm_modes._ACTION_RISK` with `ToolCapability` read once |
| P9 closed task schema | unknown keys refused at every launch layer — the config file, the task document (refused on SUBMIT, grandfathered on RELOAD through `repo_task.py::_grandfathered`, deliberately not `extra="forbid"`), the stage manifest | no `schema` stamp on `task.snapshot.json`; no per-kind reader key table beyond `READER_PATH_KEYS` |
| P10 read-side HTTP rules | one fold per request (`appstate.request_fold_scope`); `generation_fence` exists | two GETs still take the exclusive sequencer (`/concepts/lens/recovery`, `/log-page`) plus `_state_payload`'s reset-marker reconcile; no refusal-code table (`serve/http.py` absent, six `500` sites) |
| P4 one untrusted-evidence boundary | 3 surfaces (assistant, concept tagger, MCP cache key) | `agents/strategist.py`, `engine/triage.py`, `agents/unified_agent.py`, `tools/literature.py` carry no label; no `core/evidence.py`, no prompt assembler |
| P15 small fixes | most landed (receipts, reaper, parser, fsync, off-by-one, approval bypass) | `EnvironmentRefusal` for `run_setup`; the systemic-stop registry |
| P1 loop offload | **not started** — and re-measured (§2) | the repair path, the serial build |
| P7 containment made countable | not started (no `.ruff.toml`, no `contain(` helper) | 460 silent handlers, 143 in `engine/` |
| P8 typed engine state | not started (no attribute guard) | 772 attributes, 91 minted outside `__init__` |
| P11 generated references | not started (`docs/guide/configuration.md` hand-maintained; `test_config_docs_sync` compares names, not defaults; no API reference) | four hand-kept tables |
| P12 CLAUDE.md byte budget | not started — and the file GREW: **238,234 bytes / 705 lines** here against 232,919 / 658 on 2026-09-02 | every agent turn pays it |
| P13 UI mount harness | not started (no `ui/test/_mount.js`) | 10.6k lines never mounted |
| P14 cross-run store hygiene | not started | schema stated nowhere once |

### 1.3 Suite health at this baseline

* The seven tests doc 50 found red are no longer red: four were fixed in the sweep and the three
  `test_dev_probe` cases now **skip** where Landlock is not enforced instead of failing.
* The doc guards (`test_open_item_index`, `test_claim_pins`, `test_documentation_contracts`,
  `test_config_docs_sync`) are green on this head.
* `ui/`: `npm test` — **1,527 passed, 0 failed** (2026-09-05, this container).
* Full Python suite (`-m "not docker"`, four `pytest-split` shards, this container, 2026-09-05): **0 failures, 0 errors, 80 skips over 13,059 passing tests** — counted from the progress glyphs, because the doubled `-q` suppressed pytest's own summary line.

Sizes that the structural items below are about, measured here: `engine/orchestrator.py` 6,708
lines, `engine/evaluate.py` 3,865 (its `_evaluate` 1,898 by its own marker), `serve/routers/runs.py`
3,859, `ui/src/AssistantBar.jsx` 4,469, `ui/src/RunView.jsx` 3,148, `ui/src/RunList.jsx` 2,986,
`tools/machine_runs_tools.py` 1,987, `agents/roles.py` 1,779.

---

## 2. Two markers that no longer describe the tree

Both are the shape CLAUDE.md warns about — a falsifier keyed on a NAME the fix did not use — and
both are the cheapest items on this page, because closing is a deletion.

* **`judge-bench-cannot-see-a-post-exit-stage-failure`** (BACKLOG §0.19) asks for "the declared
  `expect`/`assert` contract, in front of the judge while the stage still runs" and proves itself
  open by `absent:monitor_expect_context@looplab/engine/train_monitor.py`. That evidence shipped on
  2026-08-20 under another name: `train_monitor.py::stage_contract_context`, spliced into the
  judge's tick under `Settings.train_monitor_contract`, scored on the committed 450-decision corpus
  (CLAUDE.md's `train_monitor.py` row: 12 decisions fire, 12 wasted / 0 productive, 6 -> 9 of 27
  wasted attempts caught). The marker survived because no symbol was ever spelled
  `monitor_expect_context`. Delete it; keep the measurement paragraph.
* **`judge-bench-covers-two-judges-of-four`** (BACKLOG §0.19) records, in its own text, why the
  remaining two judges will not be benched: the repair critic has **7 decisions** in the whole
  corpus, and the novelty gate rejects ideas that are never run, so no label can exist for it. That
  is a decline with a number, not open work. Convert it to a decline (the house rule: a decline
  carries `measured:` and a doc citation) so the index stops asking for it.

Neither changes a line of `looplab/`.

---

## 3. The ranked plan

Ranked by cost of leaving it. "S/M/L" is effort in this tree's own terms: S is one change with one
driven test, M is a few days with a design note, L is a shape change with its own doc. Each row names
the marker or finding it retires so that closing it is a deletion.

### Tier 0 — this week, small, all verifiable in a checkout

| # | Item | Why now | Size | Retires |
|---|---|---|---|---|
| 0.1 | Pin `require_approval` into `core/config.py::RUN_START_PINNED_FIELDS`, with a resume test that a snapshot EDIT cannot move the approval gate | Invariant #6 does not cover the one setting that gates a paid finish; the deletion half closed 2026-09-03, the edit half is one line | S | `require-approval-not-pinned-at-run-start` |
| 0.2 | Route `claims_assessments.py::_qualify_refs` / `_ingest_evidence`, `claims_retrieval.py::portfolio_atlas` and `concept_shelf.py::run_concept_index` through `run_ref` / `row_belongs_to_run`, each with the two-incarnation fixture the other readers got | The EK-03 half still standing is the one that flips `producer_receipt_known=False` and refuses every ratification when two runs share a directory name — doc 50's own HIGH | S | doc 50 EK-03 (residue) |
| 0.3 | Take the last two GETs off the exclusive command sequencer (`/api/runs/{run_id}/concepts/lens/recovery`, `/api/runs/{run_id}/log-page`) through `generation_fence`, and decide `_state_payload`'s reset-marker reconcile the same way | A read waiting on a cross-process `flock` behind a live run's writes is the SR-02 cost the sweep measured and fixed at the other sites | S | doc 50 SR-02 (residue) |
| 0.4 | A refusal-code table in one `serve/http.py` with a guard: no `500` for unreadable input, no host path in a reflected `OSError` | Six `HTTPException(500)` sites answer what every sibling answers `503`; one reflects a filesystem path | S | doc 50 SR-05 |
| 0.5 | Delete the stale marker and convert the decline named in §2 | The index is the backlog; two rows of it ask for shipped or refused work | S | the two §2 rows |
| 0.6 | `run_setup` failure as an `EnvironmentRefusal`; the systemic-stop reason into the terminal-reason registry with its two-way scan | Both are P15 rows the sweep left; a bare `RuntimeError` puts a deliberate refusal back into the 42-frame presentation | S | doc 50 ES2-06, ES1-06 |

### Tier 1 — the correctness items with a measured cost (weeks 1–2)

| # | Item | Why | Size | Retires |
|---|---|---|---|---|
| 1.1 | **A `DeveloperResult` envelope, then the repair path off the loop.** Make `Developer.repair`'s per-call outputs a RETURN value instead of the mutable `DEVELOPER_OUTPUT_ATTRS` side channels; then run `_triage_crash` / `_repair` / `_repair_critic` under `anyio.to_thread` with the capture-sink discipline `novelty.py::_capture_proposal_events` already uses, and a loop-liveness twin of `tests/test_propose_does_not_freeze_the_loop.py` per site | The marker in `engine/evaluate.py` is driven: a 5 ms ticker sees ZERO loop ticks during a triage whose median is 116–276 s (one recorded case 88.3 min); while it holds, watchdog kills, operator aborts, sibling terminals and GPU refills all wait. The envelope is the precondition doc 50 names (the freeze is what serializes concurrent repairs on the shared developer instance) and is itself an open doc 27 item | M | `repair-path-holds-the-engine-loop`, `developer-output-has-no-immutable-envelope` |
| 1.2 | **The remaining untrusted-text surfaces**, behind one Settings flag with a legacy-snapshot default of off (the `memo_verdict_cue` pattern, because prompt strings are contracts): the Strategist's memory note, the crash-triage and repair-critic stderr, arXiv/web tool results; one `core/evidence.py` envelope so the label and the guard sentence are spelled once; a test derived from `PROMPT_KEYS` | The Strategist's answer sets `eval_parallel` / `policy` / `timeout` from a note labelled untrusted and carrying no rule; the triage judge reads a candidate's stderr verbatim and decides the repair directive from it. Three of the review's five surfaces are done; these are the ones that move engine decisions | M | doc 50 AG-02, TO-06 (P4 residue) |
| 1.3 | **Containment made countable, not fixed.** Adopt ruff with `BLE001` as a CENSUS (the 634 `noqa` annotations become a reviewed allow-list, nothing is rewritten), one `contain(span, reason)` helper that stamps the enclosing span, and the AST funnel "every broad `except` around a paid call re-raises `BudgetExceeded` first" | The cost was found at the seams (a swallowed budget stop at a selection site, a run dropped from `/api/runs` on a fold error); the funnel is the one rule with a known victim (`trust/verifier.py::verify`, fixed) and no guard | M | `containment-is-unmeasured` (the census half; the 460-handler triage is L and is NOT this row) |
| 1.4 | **The serial node build: measure the harm before moving it.** A `looplab timings`-style report that, per `card_build` span, says whether a GPU was FREE with a claimable card while the loop was held — board state per instant, which spans alone cannot give | The marker in `engine/orchestrator.py` is the largest hold on the box (608.6 min over 13 builds on v11) and its own text says the harm is a CEILING, not a cost: overlapping an eval is free, a free GPU with buildable work is not. The offload is a concurrency change against invariant #1 and is not bought on a ceiling | S (the report) / M (the offload, conditional) | `serial-node-build-holds-the-loop` — the offload only if the number is material; **needs the box** (§5) |

### Tier 2 — investments that lower the cost of every later change (weeks 2–4)

| # | Item | Why | Size | Retires |
|---|---|---|---|---|
| 2.1 | **CLAUDE.md on a byte budget.** `CLAUDE_MD_MAX_BYTES` in `tests/test_documentation_contracts.py` with a shrink-only baseline; the rules, the package map, the invariants and the conventions stay; the dated measurement ledgers move to the numbered docs and module docstrings that already hold them behind pins; no date literal outside the Commands block; the two unmapped packages (`looplab/maintenance/`, `looplab/judgebench/`) added | It is 238 KB and grew 5 KB in three days; doc 50 re-derived 28 of its counts and found 11 stale. Every agent turn on this repo pays it before reading a file — the largest recurring cost this plan can remove, and the one that decides how much of everything else an agent can hold in view | M (one careful pass; the risk is deleting a measurement instead of moving it) | `claude-md-has-no-size-budget`, doc 50 XP-09/XP-12 |
| 2.2 | **An engine attribute guard**, then the typed state records. An AST guard that every `self._x` read in `engine/` has exactly one declaring site, making the 91 lazily-minted attributes visible; then per-cluster records (`EvalState`, `CardState`, `WatchdogState`) declared once | 772 attribute names across 20 mixins with 143 silent handlers that absorb the `AttributeError` a typo produces — the documented `_AshaStub` incident. The guard is cheap and is what makes the `EvalAttempt` split (2.6) safe to attempt | S (guard) / M (records) | doc 50 XP-08 (guard half) |
| 2.3 | **Generated references with guards that compare.** The settings table from `Settings` with DEFAULTS compared (today only names are); the CLI reference from Typer; an API reference from `app.openapi()` under the strict docs build with `(method, path, deprecated)` pinned; the doc-guard scope widened to `docs/guide`, README and CLAUDE.md | Four hand-kept tables and a route set nothing asserts is covered; a new route lands green and undocumented | M | `http-surface-has-no-generated-reference`, doc 50 DX-03/DX-04 |
| 2.4 | **A UI mount harness** (`ui/test/_mount.js`: jsdom, a fetch stub keyed by path, fake timers) and one gate-flip test per giant component; a Python-emitted `ui_vocabulary.json` asserted by both suites for the 20 unpinned JS↔Python mirrors | AssistantBar, RunView and RunList — 10.6k lines — are mounted by no test; the suite's own history records a dropped brace passing 767 tests. Source-text pins cannot see a `disabled` gate flip | M | `largest-ui-components-are-never-mounted`, doc 50 U3-01/02 residue |
| 2.5 | **One AST layering guard** over the package matrix with the deferred-import allowance explicit per edge; promote `events/eventstore.py::_interprocess_lock` to a public `core/jsonlio.py` name | 38 % of intra-package edges are function-local and only a third of the stated rules are machine-checked; one private name reaches 27 modules | S | doc 50 XP-07 |
| 2.6 | **The `EvalAttempt` phase object** along `_evaluate`'s own phase comments (admit / run_attempt / settle_outcome / salvage / decide_repair / apply_repair / write_terminal), every append and every lock staying where it is; verified by the corpus-digest replay | 1,898 lines reading 51 engine attributes, with six test files reading its source to find things | L — after 2.2 | `eval-attempt-is-one-giant-method`, doc 25's `evaluate-prestart-and-terminal-blocks-inline` |
| 2.7 | **Retire the legacy `/control` route**: port the 41 suite call sites to `/commands`, then delete it; the `EventStore.__init__` rescan per control POST shrinks with it | The route announces its deprecation and counts callers since the sweep; what is open is the port. A lost-response retry re-appends paid intents there | M (mechanical) | `legacy-control-route-is-not-retired`, `eventstore-rescans-the-log-per-control-post` |
| 2.8 | **The event payload contract** — the documentation job the marker in `events/types.py` sizes: 65 undescribed constants and 15 types named in no document, then `EVENT_PAYLOAD_KEYS` and a generated event-log page | Invariant #5's additive-only rule cannot be checked against a contract that exists only as handler code; the mechanical join was tried and does not answer | M | `event-payloads-have-no-registry` (P6's last row) |

### Tier 3 — capability: what to implement next

The answer to "what do we build next" is ordered the same way — the cheap items whose absence has a
stated cost first, the large product surfaces after. Every row must hold the doc 36 line: a wider
action space never widens the trusted set.

| # | Item | Why | Size | Retires |
|---|---|---|---|---|
| 3.1 | **The five doc 51 items, in one pass**: bound `use_skill`'s body through `tools/_base.py::clip`; a `next_skill_status` that can DEMOTE a skill on later evidence; spend `knn_idw`'s uncertainty in `search/panel.py` and `search/proxy.py` as the abstain rung it already is in `surrogate.py`; a `columns` / `data_samples` perception hook on `adapters/repo_task.py`; a registered `EV_LITERATURE_RETRIEVED` so a retrieved abstract is durable and citable | Each is one change, each falsifier was driven, and together they close the gaps the external cohort measured: a skill library that cannot grow without eating the context window, a lifecycle that can only go up, a panel ranking purely exploitatively, a repo task the foresight rung sees no data for, literature nothing keeps | S × 5 | all five doc 51 markers |
| 3.2 | **MLflow autolog** while a run is in flight, keyed by `run_uid`, additive beside the existing export | The cheapest of BACKLOG's product rows and the one a human asks for first | S–M | `mlflow-is-export-not-autolog` |
| 3.3 | **A Pareto front where selection can read it** — `search/policy.py::pareto_front` over the primary metric and the declared secondary metrics, consumed by selection only under the trust gate's `select`, never by the champion pick under `audit` | The front is computed in the browser and nothing in the engine consumes it; a run elects one champion on one scalar. It is SELECTION machinery, so it lands gated, with the corpus-digest replay proving the `audit` default is byte-identical | M | `pareto-never-reaches-champion-selection` |
| 3.4 | **A distribution-shift detector** (`trust/drift.py`) beside the leakage gates, deterministic and advisory | Nothing tells a shifted input from a worse model; the gate family it joins is pure Python and adds nothing to the log when clean | M | `no-distribution-shift-detector` |
| 3.5 | **An LLM value estimate for MCTS** (`search/lats.py`), advisory over the metric-only reward, never a metric | An unexplored branch is indistinguishable from a bad one; the value is a prior, so it may re-rank exploration and may not touch the record | M | `mcts-has-no-llm-value-estimate` |
| 3.6 | **The feature-engineering operator** the proposer's prompt already promises | The eval never drops a feature that fails CV; the only enforcement is a sentence | M | `fe-cv-gate-is-prose-not-enforcement` |
| 3.7 | **A real forecasting backend** behind the time-series adapter | The adapter validates plumbing, not forecasting | M | `timeseries-adapter-embeds-its-own-forecaster` |
| 3.8 | **Cross-machine eval dispatch** | The second GPU is the ceiling and a queued node waits; the largest row here and the last, because it changes what "quiescent" means for every main-task decision (invariant #1) | L | `eval-parallelism-is-in-process-only` |

---

## 4. What is deliberately NOT on the plan

* **The paid cadences off the loop.** Re-measured 2026-09-04 on a full 24 h run: every paid cadence
  together is ~15.5 min against 5,026.6 min of evaluation, 0.3 % of the run. A concurrency change
  against invariant #1 is not bought for that; the marker stays open because a costlier Strategist
  could change the number.
* **A legal-action set for the card lane.** Built on 2026-09-03 and REVERTED: on the board the
  MCTS test uses, the policy proposes one action, so filtering to it deletes the lane rather than
  narrowing it. A product decision between two designs, recorded at the marker, not a fix.
* **Claim refutation flowing down as undercut.** Its own trigger — a fold producing a card with both
  children and own-level evidence — has not fired.
* **The semantic belief key.** Text identity splits 18 of 84 concept-equal groups; the replacement is
  unbuilt and, more to the point, unvalidated. Design first, on the corpus.
* **The doc 25 modularity ledger** (30 items) as a programme. They are judgement calls about shape
  and are best paid when the file is open for another reason; 2.2 and 2.6 are the two that a
  correctness item depends on and are ranked on that basis.
* **The 19 declines.** Each carries a number; none is reopened here.

---

## 5. The box-only queue

These cannot move in a checkout without `runs/` or a GPU, and ranking them beside the rest would be
the unmeasured-policy shape this repo refuses. In the order they pay:

1. **Serial-build harm** (1.4): the per-instant "free GPU with buildable work" report.
2. **First propose with every GPU idle**: measure the split between
   `research_cadence.py::_ground_run_start` and the first propose before choosing the overlap.
3. **ASHA's promotion mask**: whether an unmasked policy query plus a filter over the masked node's
   own action is sound; the residue is 2.08 starved hours over 6 intervals on v8.
4. **`TrainingVerdict.fault` outcome label**: a run must reach the repair branch
   (`train_monitor_kill` on, a confirmed `broken`, `fault="implementation"`); none preserved has.
5. **Researcher questions**: one fresh run under the prose ask; 0 of 155 `node_created` rows carried
   one before it. If zero again, the honest close is a decline with that number.
6. **Landlock's default**: one ruleset through a real GPU eval via `looplab landlock-check`, then the
   flip.
7. **The two caches** (`tools/knowledge_tools.py`, `tools/_runcache.py`): records-per-rebuild and
   folds-per-turn on a real corpus, then a bound sized from the number.
8. **Crash lead time**: where in each attempt's own byte range the first traceback lands, in the
   bench corpus builder beside the labels it qualifies.

---

## 6. Baseline record for this head

`master` at `bf860b7` (2026-09-04), plan branch `claude/prioritize-development-plan-0k77gb`.

* Doc guards and the seven formerly-red tests: green (this container, 2026-09-05).
* `ui/`: 1,527 passed / 0 failed.
* Full Python suite (`-m "not docker"`, four `pytest-split` shards): 0 failures / 0 errors / 80 skips /
  13,059 passed. A later reader re-runs rather than trusts it.

## 7. How to work this plan

* One row is one change with one driven test; a row's close is the DELETION of the marker it names,
  never an edit to this page. When this page and the tree disagree, the tree is right and this page
  is stale — it is dated for that reason.
* No new marker without a falsifier the guard re-derives; no decline without a number.
* A measurement precedes a policy: 1.4, and every §5 row, ship the instrument first.
* The trust line is not negotiable on Tier 3: an advisory rung may re-rank, refuse or annotate; it
  may not mint a metric, a champion, a violation or a selection.
