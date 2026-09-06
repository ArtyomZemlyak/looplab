# 52 · Development plan (2026-09-05; revised 2026-09-06, third pass): what to build, improve and fix next

**Status: a PLAN over the open-item index, re-derived against `master` three times — 2026-09-05
(the plan), 2026-09-06 morning (a second verification and an external SOTA pass), 2026-09-06 evening
(a pass over EVERY document in `docs/` and the architecture, after which every open item this page
ranks is an `OPEN[…]` marker or says why it cannot be one).** It ranks; it ships no engine change and
flips no default. Its inputs are the three things this repo treats as the backlog — the greppable
marker index (`grep -rn 'OPEN\['`, counted with the guard's own parser in
`tests/test_open_item_index.py`), the whole-tree finding ledger of
[doc 50](50-architecture-review-2026-09-02.md) with its ranked proposals P1–P15, and the five
external-works items of [doc 51](51-external-works-synergy-2026-09-03.md) — read against the 71
commits that landed between 2026-09-01 and 2026-09-04, against what the field's leading systems do
in September 2026 (§3, every claim with its source), and, since the third pass, against the
seventy-odd documents under `docs/` (§2.1).

What the two revisions CHANGED in the tree, and it is deliberately small: two stale markers closed or
converted in `docs/BACKLOG.md` (§2), one decline minted at its BACKLOG row (§2.1), and the markers
minted in this page (§4) — each with a falsifier evaluated against the tree AND mutated in a
throwaway copy with the shipped form of its fix before it was written. Nothing else moved.

Two constraints shape the ranking. **The `runs/` corpus is absent from this checkout**, so every
figure quoted from a run below is quoted from the tree's own record and not re-derived; items whose
next step is a corpus measurement are queued for the GPU box (§5.2) rather than ranked as if they
could be advanced here. And the house rule that decides order is **cost of leaving it, measured where
a measurement exists** — a marker whose own text re-ranks it down (the paid cadences, at 0.3 % of a
run) is ranked down here too, whatever its severity label says.

Companion authorities: [doc 50](50-architecture-review-2026-09-02.md) (the finding ledger this plan
consumes), [doc 27](27-agent-system-mega-review-2026-08-09.md) (agent-system items),
[doc 25](25-architecture-modularity-review-2026-08-01.md) (the modularity ledger),
[doc 34](34-review-deferred-decisions-2026-08-13.md) (product/architecture deferrals),
[doc 10](10-autoresearch-improvement-research.md) and [doc 11](11-agent-systems-research.md) (the
2026-07 SOTA sweeps this page's §3 updates), [doc 28](28-deep-research-sota-roadmap-2026-08-10.md)
(the Deep Research ledger), [doc 41](41-external-works-synergy-2026-08-14.md) and
[doc 51](51-external-works-synergy-2026-09-03.md) (the two external-works passes),
[doc 45](45-claim-surfaces-2026-08-20.md) (why a recorded number is pinned to the site that decides
it), [doc 36](36-agent-driven-decisions-2026-08-13.md) (the trust line every capability item must
hold), [BACKLOG](BACKLOG.md) and [CODE_REVIEW](CODE_REVIEW.md) (the two prose ledgers whose live rows
carry markers).

---

## 1. Where the tree stands

### 1.1 The index, by home

Counted by the guard's parser after this revision's edits: **131 open markers and
21 declines**, in 26 files. (Before the first revision: 103 / 19 in 25
files; after the second: 105 / 20 in 26.)

| Home | Open | What lives there |
|---|---:|---|
| `docs/25` modularity ledger | 30 | god-module splits, duplicated scaffolds, hand-rolled guards — judgement calls, not defects |
| `docs/52` (this page, §4) | 30 | the SOTA gaps and every item the docs pass found open, falsifiable and untagged |
| `docs/BACKLOG.md` | 21 | product gaps (Pareto, drift, MLflow, MCTS value, distributed eval), cadence and watchdog residue, the claim ladder |
| `docs/27` agent-system review | 14 | budgets, receipts, cancellation, prompt governance, the eval ladder |
| `docs/51` external works | 5 | the cheap capability items (skills, kNN uncertainty, perception hook, literature) |
| `docs/50`, `docs/34`, `docs/CODE_REVIEW.md` | 4 + 4 + 4 | whole-tree items; the four deferred product decisions; the four surviving review rows |
| `looplab/` (in code, at the site) | 14 | the loop holds, the eval method, the card lane, the payload registry, the legacy route, two caches, two measurements |
| `tests/` | 3 | three guards that state their own limit |
| `docs/29`, `docs/46` | 1 + 1 | the F3 follow-up; the `.py`-only params guard |

Of doc 50's twelve site markers, three closed in the sweep (the sibling-cancelling eval boundary,
the unlabelled assistant tool results, the read fence on the command sequencer) and nine stand.

### 1.2 What the 71 commits since 2026-09-01 closed, against doc 50's proposals

| Proposal | State on 2026-09-06 | Residue — now a marker where a falsifier exists (§4) |
|---|---|---|
| P2 per-child containment | **done** (`engine_error` terminal; `adapter` refused at submit) | — |
| P3 one run identity for readers | mostly done (`core/run_identity.py::run_ref` / `row_belongs_to_run`; lessons, capsules, claims health) | `claims_assessments.py::_qualify_refs` / `_ingest_evidence`, `claims_retrieval.py::portfolio_atlas`, `concept_shelf.py::run_concept_index` still key on `run_id` → `claim-readers-still-key-on-run-id` |
| P5 unwedge Replay | **done** (`unique_destination` in `serve/reset_route.py`) | — |
| P6 six vocabularies | 4 of 6 (engine terminal reasons in `core/models.py`, stage statuses in `runtime/command_eval.py`, command statuses in `serve/protocol.py`, the `KIND_*`/`META_*` constants now read by `search/card_selection.py`) | `EVENT_PAYLOAD_KEYS` → `event-payloads-have-no-registry`; the systemic-stop reason → `systemic-stop-reason-has-no-registry`; permission still decided by `perm_modes._ACTION_RISK` (untagged: a design choice, doc 50 TO-10) |
| P9 closed task schema | unknown keys refused at every launch layer — the config file, the task document (refused on SUBMIT, grandfathered on RELOAD through `repo_task.py::_grandfathered`, deliberately not `extra="forbid"`), the stage manifest | no `schema` stamp on `task.snapshot.json`; no per-kind reader key table beyond `READER_PATH_KEYS` (untagged: no falsifier that survives a rename) |
| P10 read-side HTTP rules | one fold per request (`appstate.request_fold_scope`); `generation_fence` exists; `serve/http.py` exists with the body-parsing half | two GETs still take the exclusive sequencer (`/concepts/lens/recovery`, `/log-page`) — §4.2, count-shaped, no falsifier; no refusal-code table → `refusal-codes-have-no-table` |
| P4 one untrusted-evidence boundary | 3 surfaces (assistant, concept tagger, MCP cache key) | `agents/strategist.py`, `engine/triage.py`, `agents/unified_agent.py`, `tools/literature.py` carry no label → `no-single-untrusted-evidence-envelope` |
| P15 small fixes | most landed (receipts, reaper, parser, fsync, off-by-one, approval bypass) | `EnvironmentRefusal` for `run_setup` → `run-setup-failure-is-not-a-refusal-type` |
| P1 loop offload | **not started** — and re-measured (§1.4) | `repair-path-holds-the-engine-loop`, `serial-node-build-holds-the-loop` |
| P7 containment made countable | not started (no `.ruff.toml`, no `contain(` helper) | `containment-is-unmeasured` |
| P8 typed engine state | not started (no attribute guard) | `engine-attributes-have-no-declaring-site-guard`, then `eval-attempt-is-one-giant-method` |
| P11 generated references | not started (`docs/guide/configuration.md` hand-maintained; `test_config_docs_sync` compares names, not defaults; no API reference) | `settings-doc-guard-compares-names-not-defaults`, `http-surface-has-no-generated-reference` |
| P12 CLAUDE.md byte budget | not started — and the file GREW: **238,234 bytes / 705 lines** here against 232,919 / 658 on 2026-09-02 | `claude-md-has-no-size-budget` |
| P13 UI mount harness | not started (no `ui/test/_mount.js`) | `largest-ui-components-are-never-mounted` |
| P14 cross-run store hygiene | not started | untagged: a schema registry's shape is a design call; the three readers above are its first concrete row |

### 1.3 Suite health at this baseline

* The seven tests doc 50 found red are no longer red: four were fixed in the sweep and the three
  `test_dev_probe` cases now **skip** where Landlock is not enforced instead of failing.
* The doc guards (`test_open_item_index`, `test_claim_pins`, `test_documentation_contracts`,
  `test_config_docs_sync`) are green on this head, including after this revision's marker edits.
* `ui/`: `npm test` — **1,527 passed, 0 failed** (2026-09-05, this container).
* Full Python suite (`-m "not docker"`, four `pytest-split` shards, this container, 2026-09-05):
  **0 failures, 0 errors, 80 skips over 13,059 passing tests** — counted from the progress glyphs,
  because the doubled `-q` suppressed pytest's own summary line.

Sizes that the structural items below are about, measured here: `engine/orchestrator.py` 6,708
lines, `engine/evaluate.py` 3,865 (its `_evaluate` 1,898 by its own marker), `serve/routers/runs.py`
3,859, `ui/src/AssistantBar.jsx` 4,469, `ui/src/RunView.jsx` 3,148, `ui/src/RunList.jsx` 2,986,
`tools/machine_runs_tools.py` 1,987, `agents/roles.py` 1,779.

### 1.4 The loop holds, as measured by their own markers

* **Repair path** (`engine/evaluate.py`): driven, a 5 ms ticker sees ZERO loop ticks during a
  triage whose median is 116–276 s (one recorded case 88.3 min). Not re-ranked.
* **Serial node build** (`engine/orchestrator.py`): the largest hold on the box (608.6 min over 13
  builds on v11), but the marker's own 2026-09-04 measurement says the harm is a CEILING, not a cost —
  an eval runs in a subprocess and a busy loop does not slow it; the cost is a FREE GPU with
  BUILDABLE work, which needs board state per instant that spans alone cannot give.
* **Paid cadences**: 15.5 min against 5,026.6 min of evaluation on a full 24 h run, 0.3 %. Ranked
  down by its own marker, and here.

---

## 2. Verification passes: what they changed

### 2.0 The second pass (2026-09-06, morning)

Every one of the 103 markers was read against its proof and, where the proof names a symbol the fix
might have landed under another name, against the tree. Two did not describe the tree, and both were
fixed IN THAT CHANGE rather than listed as work:

* **`judge-bench-cannot-see-a-post-exit-stage-failure` — CLOSED (deleted).** It asked for "the
  declared `expect`/`assert` contract, in front of the judge while the stage still runs" and proved
  itself open by `absent:monitor_expect_context@looplab/engine/train_monitor.py`. That evidence shipped
  on 2026-08-20 under another name — `train_monitor.py::stage_contract_context`, spliced into the
  judge's tick under `Settings.train_monitor_contract` (ON) — and was scored on the committed
  450-decision corpus (CLAUDE.md's `train_monitor.py` row: 12 decisions fire, 12 wasted / 0
  productive, 6 -> 9 of 27 wasted attempts caught). The guard stayed green for seventeen days over a
  shipped item because no symbol was ever spelled `monitor_expect_context`.
* **`judge-bench-covers-two-judges-of-four` — converted to a DECLINE.** Its own text says why the
  remaining two judges will not be benched: the repair critic has 7 decisions in the whole corpus and
  the novelty gate rejects ideas that are never run, so no outcome label can exist for it.

### 2.1 The third pass (2026-09-06, evening): every document, and the architecture, re-derived

Method: every file under `docs/` was scanned for its open-status vocabulary (`STILL OPEN`, `⬜`,
`🟡`, `DEFERRED`, `PARTIAL`, "not shipped", "unbuilt"), every hit was read in context, and every
candidate that named a symbol was checked against the tree by grep on this head. The architecture
was re-read through [doc 02](02-architecture.md), the ADRs in [doc 03](03-decisions.md), the
tracing model in [doc 08](08-tracing-architecture.md), the package map and invariants in CLAUDE.md,
and the guide's process diagram page. The result, per pool:

| Pool | What the pass found | Disposition |
|---|---|---|
| Docs 01–05 (design, ADRs) | ADR-11's hardening targets are SHIPPED where they mattered (deny-by-default egress via `--network`, cgroup/ulimit caps, allow-listed installs in `runtime/deps.py`, the reproduction manifest in `core/setup_identity.py`, approvals as command events); two are SUPERSEDED by the shipped shape (OpenTelemetry `gen_ai` conventions by doc 08's own span model; gateway tokens by the sandbox's secret refusal in `core/envsafe.py`); the Settings-level dollar cap is absent and already tracked (`no-shared-reserve-commit-run-budget`) | one open DECISION, untagged: "parallel sidecar ordering" (doc 03 §Open) — a design question with no statable falsifier |
| Doc 06 (implementation plan) | every partial is done; the "remaining seams" (LanceDB, an MCP server bus, gVisor, gateway tokens) are design substitutions recorded in the ADRs, not open work; the co-evolving evaluator shipped as `trust/harden.py` | nothing to tag |
| Docs 10–12 (2026-07 roadmap) | shipped: T1 preset, T2 gate ladder, T4 embeddings, T5 novelty, T6 fold cache, T7 response cache (`core/llm.py::_ResponseCache`), P1 cards, P3 ablation, P4 operator bandit (off by default), M2/M3 lessons, D2 hygiene, D4 novelty-before-compute, D5 harden loop, D7 `weighted_parent`, D8 memo verification, D9 concurrent research, D10 best-of-N, D11 compressor slot. OPEN and now tagged: T3/D1 for the repo family (§4 `repo-task-champion-is-picked-on-the-candidates-own-metric`), T8 (`merge-operator-is-mean-of-params-not-code`), P2/D13 (`no-plan-artifact-with-endgame-reserve`), M1 (`lessons-are-not-operator-scoped`), D3 (`strategist-consult-is-cadence-not-stagnation-triggered`) | 5 markers |
| Doc 13, 14, 15, 16 (July reviews) | closed lists; doc 16's residue re-derived: the SSE blocking `q.get` is fixed (the comment at the site records the old shape), the enum gaps closed 2026-09-02; "aggregate context truncation" and "fork/inject effect-before-gate" are P2/P3 rows nobody has reproduced since July | untagged, stated |
| Doc 17 (capability matrix) | typed `DevelopmentResult` → tracked (`developer-output-has-no-immutable-envelope`); the run manifest, default-deny auth on the shared hub, the deadline watcher and temporal CV all shipped; the "no first-class Evaluator" ⬜ is a naming/architecture question (verification IS a distributed subsystem here by design); the "distance-from-seed" lever → `no-distance-from-seed-signal` | 1 marker, 1 design question untagged |
| Docs 18, 18-desktop, 19, 20, 21, 23, 24 (UI and workspace reviews) | Approve/Ratify shipped, the DecisionFreshness API shipped, the atlas claims of doc 21 shipped as F7, doc 23 shipped, doc 20's distributed direction → `eval-parallelism-is-in-process-only`; browser-level accessibility evidence (axe, screen reader, touch, cross-browser, visual regression) is still absent | untagged: product work whose cost nobody has measured |
| Doc 22 (parallelism) | phases 0–3 shipped; phase 4's "golden for a 2-wide parallel build" was never added → `parallel-build-has-no-golden-replay` | 1 marker |
| Docs 25, 27, 34 | their markers stand (30 / 14 / 4) | — |
| Doc 26 (Ouroboros) | #2 action-trace audit + `hack_adjusted` reporting → `no-hack-adjusted-score-reporting`; #9 prior-injection hit-rate audit → `prior-injection-hit-rate-unmeasured`; #4 verifier quorum shipped as the R1-c tie-break; #13 beacons shipped as diagnostics; #12 reviewed self-evolution is a programme, untagged | 2 markers |
| Doc 28 (Deep Research ledger) | NOT SHIPPED by its own status line, and the tree agrees: none of `ResearchPlan`, `ProgressLedger`, `EvidenceItem` exists → `deep-research-plan-is-not-durable`, `research-evidence-has-no-exact-span-identity`; DR-03..13 stand behind them in the ledger | 2 markers |
| Doc 29 (operator backlog) | F1–F9 BUILT / SHIPPED / DECLINED; the one nested follow-up is tagged | — |
| Docs 30–33, 35–37, 39, 40, 42–44, 47–49 | options papers, audits and day reports; no open work beyond what their tagged items already carry | — |
| Doc 38 (fence audit) | the engine-side `EACCES` translation for triage is NOT built → `landlock-refusal-is-not-translated-for-triage`; the GPU validation is the tagged `landlock-is-opt-in-by-default` | 1 marker |
| Doc 41 §8 | step 1 tagged, step 2 closed 2026-08-15, step 3 → the hit-rate marker, step 4 → the two doc 28 markers, step 5 → `no-external-benchmark-number-exists` | — |
| Docs 45, 46 | tagged (`claim-legacy-prompt-branches`, `declared-params-guard-reads-only-py`) | — |
| Doc 50 residue (untagged findings the plan ranked) | EK-03, SR-05, ES2-06, ES1-06, XP-05, XP-07, XP-08, DX-03 → markers (§4); SR-02's two GETs → §4.2 | 8 markers |
| Doc 51, A7 | their markers stand; A7's Strategist-developer row shipped 2026-09-03 | — |
| `docs/BACKLOG.md` §0.1 | #2, #3, #4, #8, #9, #11, #19 closed; #5, #7, #12–#18 tagged; **#6 → DECLINED at its row** (the P0 rested on "this box serves local models", which it does not; the local instrument reports 33 asks / 0 repaired / 0 failed); #10 → `cross-run-trajectory-overlay-unbuilt` | 1 decline, 1 marker |
| `docs/BACKLOG.md` §0.2 (low-cost residue) | re-derived: the repair critic's verdict IS durable now (`EV_REPAIR_CRITIC`), `Dag.jsx` knows `salvaged`, `stage_budget_refusal` reaches `write_file`/`edit_file`, `kill_background` exists; STILL TRUE: the read-model watermark hashes `(seq, type)` only → `readmodel-watermark-ignores-event-data`; the log-integrity receipt counts lines as records — §4.2, no falsifier; the two idiom duplications (socket shutdown, timeout nulling) are cross-referenced residue | 1 marker |
| `docs/BACKLOG.md` §2 Themes A–I | A2 (k-NN surrogate, not TPE/RF) is a design substitution its row states; A3 (`bohb`) shipped; C5 declined; H2 → the §0.1 #6 decline; A4, F2, G3, I1–I5 tagged (`mcts-…`, the overlay marker, `eval-parallelism-…`, the product markers) | — |
| `docs/BACKLOG.md` §4–§6 | §4's flat-import codemod is nil (the remaining `looplab.server` strings are the LOGGER's name, doc 50 XP-14); §5's launch-readiness gate → `launch-readiness-gate-is-two-copies`; §6's model-free `assert` → `stage-assert-has-no-model-free-numeric-form`, D5 → `stage-rows-are-last-wins-per-name`; the reward-hack/hardened-suite residual is CLOSED (`ExploitSuite.scan` takes `grader_import_ok`) | 3 markers |
| `docs/CODE_REVIEW.md` | every 🟡 row is closed or carries a marker; nothing untagged remains | — |
| `docs/ROADMAP.md`, `PROMPT_REVIEW.md`, `RESEARCH_NOTES.md`, `GRAPH_GROUPING_REDESIGN.md` | superseded by BACKLOG; PROMPT_REVIEW's deferred rows are BACKLOG §5/§6 rows, handled above | — |
| `docs/guide/*` | user contract, accurate; the architecture guide's "the trigger is still open" is the v12 exporter incident, since instrumented by `TRACE_WORKER_STOP_REASONS` — a record, not open work | — |
| In-code `CODEX AGENT` notes (9) | not re-derived one by one in this pass; doc 50 EV-09 asks for each to be tagged or closed | next pass |

Two SOTA-derived items from §3 that the morning pass left as "design rows" are now markers too,
because each has a falsifier that flipped under mutation: `operator-bandit-has-no-model-arm` and
`skills-load-flat-not-by-tier`; and the BAITBENCH-shaped measurement is `developer-hack-rate-unmeasured`.

---

## 3. SOTA, September 2026 — and what "better than SOTA" has to mean here

### 3.1 The field's numbers

Every number below is from the cited primary source (list at the end of this page); none was
re-derived here, and LoopLab has NO comparable number of its own — see §3.4.

| System | What it is | Reported result |
|---|---|---|
| Famou-Agent 2.0 (Baidu) | multi-agent framework; "evolution strategies, long-horizon memory, infrastructure" | **64.44 %** any-medal, MLE-bench full 75, Gemini-3-Pro-Preview, 24 h (2026-02-23, official leaderboard); 80.3 % on Lite |
| AIBuildAI | — | 63.11 % full, Claude-Opus-4.6, 24 h (2026-03-06) |
| CAIR MARS+ | modular agent with reflective search (doc 13) | 62.67 % full, 24 h |
| MLEvolve (InternScience) | progressive MCTS with cross-branch fusion, stagnation detection, BM25+FAISS experience memory | 61.33 % full at 12 h on the leaderboard; **65.3 % ± 0.8** with Gemini-3.1-Pro-preview per its README |
| ML-Master 2.0 | Hierarchical Cognitive Caching: L1 traces, L2 phase-level distilled knowledge, L3 cross-task priors | 56.44 % full (DeepSeek-V3.2); Lite ablation **22.7 % → 54.5 % → 72.7 %** as L2 then L3 are added |
| AIRA₂ (Meta) | async multi-GPU worker pool + Hidden Consistent Evaluation + ReAct debug agents | MLE-bench-30 percentile rank **81.5 % at 24 h / 83.1 % at 72 h** (Gemini 3.1); 1 GPU 56.8 % → 8 GPUs 71.8 % (linear); HCE alone **+13.0 / +18.4** points; the earlier "validation overfitting" was evaluation NOISE |
| Arbor (RUC) | long-lived coordinator + short-lived executors in git worktrees + a persistent hypothesis tree with insights propagated upward; a change is kept only if it clears `merge_threshold` on a HELD-OUT split | **86.36 %** any-medal on MLE-bench Lite with GPT-5.5; 2.5× the held-out gain of Codex / Claude Code on six real tasks |
| EurekAgent | "agent environment engineering": permissions, artifacts (git-tracked solutions), budget (time-helper API + deadline warnings), human-in-the-loop; controller-owned result files the agent cannot modify | **85.71 % any-medal / 71.43 % gold** on SEVEN curated Lite tasks, one run each, GLM-5.1, one GPU |
| HASTE | skills accumulated in three tiers (global / domain / competition) and loaded by tier | **77.3 %** Lite, Claude Sonnet 4.6, 12 h; tiered loading 100 % vs flat loading 62.5 % — the same as NO skills — on 8 competitions; warm starts use 52 % fewer refinement iterations; single seed |
| Frontis-MA1 / OpenMLE (FrontisAI) | a 35B model post-trained on four operators (Draft / Improve / Debug / Crossover) plus async search and "benchmark-independent experience priors" | Lite, 12 h, ONE RTX 4090 at 12 GB: 39.39 % → 60.61 % (Evo) → **71.21 %** (Evo-Max) |
| ShinkaEvolve (Sakana) | evolutionary program search: novelty rejection before evaluation, adaptive parent sampling, a bandit over an LLM ensemble | ICLR 2026; circle packing beyond AlphaEvolve's; CLI-backed mutation models since 2026-05 |
| AlphaEvolve (DeepMind) | evolutionary coding agent with automated evaluators | 2026-08: matrix-multiplication exponent bound pushed below 2.371177 |
| Kosmos (Edison / FutureHouse) | literature + data analysis + world model, ~200 rollouts per 12 h run | independent scientists rated 79.4 % of statements accurate |
| karpathy/autoresearch | edit `train.py`, 5-minute fixed budget, keep/revert on `val_bpb`, ~12 experiments/hour | the "autoresearch" pattern (2026-03) |
| Survey of AI scientists (2608.05179) | 26 systems coded on seven dimensions | 83 % release code, **38 %** release seeds or traces, 38 % report any novelty verification; "no LLM-era system … demonstrates an externally validated in-loop oracle" |
| BAITBENCH (2608.30724) | optional, rule-compliant shortcuts planted in ML tasks | **57.1 %** of runs reward-hack, five of seven agents above 50 %, above 50 % even when told not to |

### 3.2 The techniques that recur, against what LoopLab has

"Has" was checked by symbol on this head, not by reading a doc.

| Technique | Who | LoopLab today | Verdict |
|---|---|---|---|
| **Hidden consistent evaluation / holdout-gated selection** — the champion is chosen on a split the search never optimised, held constant across candidates | AIRA₂ (+13.0 / +18.4 points), Arbor (`merge_threshold` on held-out), MLE-bench's hidden grader | `engine/holdout.py` implements D1 holdout-gated promotion (`holdout_select=True`, `holdout_fraction=0.25`, `holdout_top_k=3`) — for HOST-GRADED tasks with a predictions file only. `adapters/repo_task.py`, the family every real GPU run here uses, has no hidden split: the champion is elected on the metric the candidate's own script prints (`metric_subject` says what the number is ABOUT, not whether the split was hidden) | **GAP #1** — the largest measured selection effect in the field is open on exactly the runs this box pays for |
| **Throughput: async worker pool, linear in GPUs** | AIRA₂ (56.8 → 71.8 % for 1 → 8 GPUs), OpenMLE-Evo, Arbor's executors | in-process eval task group + speculative prefetch, `eval_parallel` AUTO; two GPUs; the loop holds of §1.4 subtract from it; `eval-parallelism-is-in-process-only` | **GAP #2**, in two halves: the loop (P1) and the box's size (the cross-machine pool) |
| **Hierarchical memory** — traces → phase knowledge → cross-task priors, loaded by tier | ML-Master 2.0 (22.7 → 54.5 → 72.7 %), HASTE (flat loading = no skills), MLEvolve, Famou 2.0 | L1 = spans + event log; L2 = `failure_reflection` / `watchdog_reflection` (ON), memos, cards/beliefs; L3 = fingerprint-keyed lessons incl. negatives, skills, concept capsules, claims, hybrid BM25+vector retrieval | structurally at parity; the measured gaps are the skill LIFECYCLE (doc 51), tiered LOADING (HASTE's result), operator scoping (doc 10 M1) and the prior-injection hit rate nobody has measured (doc 26 §4.2 #9) — **GAP #3**, four markers |
| **Search: multi-branch tree with insight propagation, stagnation detection, cross-branch fusion; evolutionary novelty rejection + parent sampling + model bandit; ablation-targeted refinement + ensembling; a Crossover operator** | MLEvolve, Arbor, ShinkaEvolve, MLE-STAR, Frontis-MA1 | greedy / evolutionary / MCTS / ASHA / BOHB policies over folded state, cards + beliefs, graded novelty, `engine/ablation.py` (MLE-STAR refine_block), `search/policy.py::_bandit_pick` over OPERATORS, `weighted_parent`, foresight + surrogate + panel; the only multi-parent operator is `merge_idea` = MEAN OF NUMERIC PARAMS; the Strategist consults on a CADENCE, never on stagnation | search breadth at parity or ahead; **GAP #4** is the endgame (no code-level crossover, no top-k ensemble), the stagnation trigger, the model arm, and the fact that the knobs that matter live in `PROFILES["thorough"]` and are OFF under the shipped `profile="default"` |
| **Verification and anti-hacking as the differentiator** | the survey's 38 %; BAITBENCH's 57.1 %; EurekAgent's controller-owned result files; claim-level auditability (AAR) | replayable log, `metric_subject`, read fence + Landlock, `reward_hack`, `leakage` (Pearson + Spearman), salvage rules, claims ledger with verifier verdicts, W3C-PROV export, redaction of every persisted tail | **AHEAD** — this is the axis on which "better than SOTA" is credible; what is missing is the PROOF: no published number, no hack-adjusted score, no hack-rate measurement of its own Developer, Landlock off by default |
| **Budget and time awareness** — a time helper the agent can call, deadline warnings, fixed-budget experiments | EurekAgent, autoresearch | `effective_eval_time_budget`, the time and memory cues, `train_monitor`'s projected overrun, `budget_aware` (OFF by default); no plan artifact with an endgame reserve | parity on the cues; `no-plan-artifact-with-endgame-reserve` |
| **Literature and data grounding** | Kosmos (every statement cited), Mechanist, OmniScientist, AutoMind's KB, MLE-STAR's web-seeded drafts | `tools/literature.py` (not durable), knowledge tools, `EV_DATA_PROFILED` for six adapters and NOT for `repo_task`; doc 28's durable research plan and exact-span evidence unbuilt | doc 51's five items plus the two doc 28 markers — all falsifiable |
| **Human-in-the-loop steering** | EurekAgent's TUI + web monitor, Arbor's tree | cards / kanban, branch-from-history, standing watches, assistant, reviews | parity or ahead |
| **Environment engineering** — permissions, artifacts, budgets as the product | EurekAgent | sandbox tiers, fences, receipts, the durable-op kit | ahead |

### 3.3 What "better than SOTA" has to mean for LoopLab

Not "a higher medal rate at any cost": the survey and BAITBENCH say the field's numbers are produced
by systems that release traces 38 % of the time and hack 57 % of the time when a shortcut is
available. The credible target has three parts, and the order is the order of leverage:

1. **A number nobody at the top publishes: medal rate WITH holdout selection, hack-adjusted, from a
   replayable log.** LoopLab already has the machinery for every adjective; it has never produced the
   noun. One MLE-bench Lite campaign through the trust layer (`no-external-benchmark-number-exists`,
   with `no-hack-adjusted-score-reporting` as the reporting half) is the external proof, and the
   target is the top cluster (77–86 % Lite) on the honest number.
2. **The same selection discipline on the box's own tasks.** The dense-retrieval runs this box pays
   for choose a champion on the candidate's printed metric. A hidden consistent split for `repo_task`
   is the single largest measured lever in the field (AIRA₂'s +13 / +18.4) and it is one declaration
   plus a host-side score stage (`repo-task-champion-is-picked-on-the-candidates-own-metric`).
3. **Throughput that scales with what the box has**, which is P1's loop offload first and the
   cross-machine pool after — and, before either, the research-grade knobs measured against the
   default on the same task, because the field's technique map says the difference is large and this
   repo has shipped it as a preset nobody has A/B'd (`research-grade-profile-is-not-the-default`).

### 3.4 What this pass could not establish

LoopLab's own position on any external benchmark. `docs/MLEBENCH.md` documents the real host-graded
path and records no completed run; doc 41 §8 step 5 asked for one on 2026-08-14 and it has not
happened. Every "ahead / parity / gap" verdict in §3.2 is therefore a verdict about MECHANISMS
present in the tree, not about outcomes — which is exactly the field's verification gap, turned on
this repo.

---

## 4. The open items, as markers

Every marker below was minted with a falsifier evaluated against the tree on 2026-09-06 AND mutated
in a throwaway copy with the shipped form of its fix (the predicate flipped in every case). Per the
house rule, the fix must land under the NAMED symbol or re-point the proof — §2.0 is what happens
otherwise. Grouped by where the item came from; the rank is in §5.

### 4.1 From the SOTA pass (§3)

OPEN[repo-task-champion-is-picked-on-the-candidates-own-metric] the real GPU task family
(`adapters/repo_task.py`) elects its champion on the metric the candidate's own script prints:
`engine/holdout.py`'s holdout-gated selection exists only for host-graded tasks with a predictions
file, and the repo task document declares no hidden split at all, so the field's largest measured
selection effect (AIRA₂: +13.0 / +18.4 percentile points from Hidden Consistent Evaluation) is open on
exactly the runs this box pays for. The fix is a `holdout` declaration on the repo task document — a
host-side score stage over a split the candidate never reads, bound like `metric_subject` — and
selection through the existing `holdout_select` rule. proof:absent:holdout@looplab/adapters/repo_task.py

OPEN[merge-operator-is-mean-of-params-not-code] the only multi-parent operator averages numeric
parameters (`search/operators.py::merge_idea`); there is no code-level crossover and no end-of-run
ensemble of the top-k, which the leaderboard systems carry as their endgame (MLE-STAR and KompeteAI
ensembling, Frontis-MA1's Crossover operator). The fix is `operators.py::merge_code` — a merge that
reads both parents' committed code — plus a reserved endgame ensemble where a predictions file exists
(doc 10 T8 / P2). proof:`absent:def merge_code@looplab/search/operators.py`

OPEN[research-grade-profile-is-not-the-default] `PROFILES["thorough"]` holds the knobs the field's
technique map says matter (`operator_bandit`, `ablate_every=3`, `confirm_top_k=3` / `confirm_seeds=3`,
`trust_gate="gate"`, `budget_aware`, `complexity_cue`) and the shipped default is `profile="default"`
with every one of them off. Whether the default should move is a MEASURED decision — one paired run
per profile on the same task on the box — and until that number exists the gap is recorded, not
assumed; the honest close is either the flip or a decline carrying the delta.
proof:`present:profile: str = "default"@looplab/core/config.py`

OPEN[no-external-benchmark-number-exists] `adapters/mlebench_real.py` and `docs/MLEBENCH.md` ship the
real host-graded MLE-bench path and no completed run is recorded anywhere in the tree, so LoopLab's
position against the 61–65 % full-set / 77–86 % Lite cluster is unknown, and every "ahead" verdict in
§3.2 is about mechanisms rather than outcomes. The deliverable is an audit page recording one
MLE-bench Lite campaign with raw, hack-adjusted and holdout-selected numbers and the seeds and traces
the survey says 62 % of systems withhold. proof:missing:docs/audit/mlebench-lite-campaign.md

OPEN[operator-bandit-has-no-model-arm] `search/policy.py::_bandit_pick` learns WHICH OPERATOR to
fire from folded yields and nothing learns WHICH MODEL should generate it: the per-role models this
repo already routes (`researcher_model`, `developer_model`, …) are fixed for the run, while
ShinkaEvolve's bandit over an LLM ensemble is where its sample efficiency comes from. The fix is a
`model_arm` on the same yield table — breadth per cost, a policy change that widens no trusted input.
proof:absent:model_arm@looplab/search/policy.py

OPEN[skills-load-flat-not-by-tier] `tools/skills.py` serves every skill the same way, with no
global / domain / task tier and no tier-ordered loading — and HASTE measured flat loading at 62.5 %
medal rate on 8 competitions, the same as loading NO skills, against 100 % for tiered loading of the
identical inventory. Ranked behind doc 51's skill-body and skill-demotion markers, because a tier of
unbounded bodies is still unbounded. proof:absent:tier@looplab/tools/skills.py

OPEN[developer-hack-rate-unmeasured] nothing has measured how often LoopLab's own Developer takes a
planted, rule-compliant shortcut (BAITBENCH's shape: 57.1 % of runs across seven frontier agents, above
50 % even when told not to), with the detectors ON and OFF — so the §3.2 "ahead on verification"
verdict is a statement about mechanisms and not a number. The deliverable is an audit page over three
synthetic tasks with optional shortcuts, on the box. proof:missing:docs/audit/developer-hack-rate.md

### 4.2 From doc 50's untagged residue

OPEN[claim-readers-still-key-on-run-id] the sweep moved lessons, capsules and claims health onto
`core/run_identity.py::run_ref` / `row_belongs_to_run`, and three readers still key on the directory
NAME: `claims_assessments.py::_qualify_refs` / `_ingest_evidence`, `claims_retrieval.py::portfolio_atlas`,
`concept_shelf.py::run_concept_index`. On the half of the corpus where names are reused, two
incarnations' rows collapse into one group, `producer_receipt_known` flips false and every one-sided
verdict is demoted (doc 50 EK-03). The fix routes each through the shared rule with the
two-incarnation fixture the other readers got.
proof:absent:run_ref@looplab/engine/claims_assessments.py+absent:row_belongs_to_run@looplab/engine/claims_assessments.py

OPEN[refusal-codes-have-no-table] `serve/http.py` holds the body-parsing half of the read-side rules
and no refusal-code table: six `HTTPException(500)` sites answer an unreadable snapshot where every
sibling answers `503`, and one reflects an `OSError` text carrying a host path (doc 50 SR-05). The fix
is one table of refusal codes in that module with a guard that no route answers `500` for unreadable
input. proof:absent:503@looplab/serve/http.py

OPEN[run-setup-failure-is-not-a-refusal-type] a failed `run_setup` surfaces as a bare error with its
42 frames rather than as the `EnvironmentRefusal` every other deliberate refusal about the operator's
environment wears (doc 50 ES2-06; `core/errors.py::OperatorRefusal` is the marker type).
proof:absent:EnvironmentRefusal@looplab/engine/eval_dispatch.py

OPEN[systemic-stop-reason-has-no-registry] `orchestrator.py::systemic_failure_stop_reason` mints its
stop sentence as a bare literal, the one engine-minted terminal vocabulary the 2026-09-02 registry
pass left out (doc 50 ES1-06); a typo'd reader compares against a string nothing guards. The fix is a
`SYSTEMIC_STOP` constant beside `ENGINE_TERMINAL_REASONS` with the two-way scan.
proof:absent:SYSTEMIC_STOP@looplab/engine/orchestrator.py

OPEN[no-single-untrusted-evidence-envelope] the untrusted-text rule reaches the assistant, the
concept tagger and the MCP cache key and not the surfaces that move engine decisions: the Strategist
(`agents/strategist.py`, whose answer sets `eval_parallel` / `policy` / `timeout`), the crash-triage and
repair-critic prompts (`engine/triage.py` reads a candidate's stderr verbatim), the arXiv / web results
(`tools/literature.py`). Doc 50 XP-05's fix is ONE envelope — `core/evidence.py`, label plus guard
sentence — used by every splice, behind a Settings flag with a legacy-snapshot default of off because
prompt strings are contracts. proof:missing:looplab/core/evidence.py

OPEN[engine-attributes-have-no-declaring-site-guard] `Engine` touches 772 attribute names across 20
mixins, 91 assigned only outside `__init__`, 47 from more than one file, with 143 silent handlers in
the same package to absorb the `AttributeError` a typo produces (doc 50 XP-08; the `_AshaStub`
incident). The cheap half of the fix is an AST guard that every `self._x` read in `engine/` has exactly
one declaring site; the typed state records come after it.
proof:missing:tests/test_engine_attribute_sites.py

OPEN[layering-rules-are-not-machine-checked] 38 % of intra-package import edges are function-local
and only a third of the stated layering rules are guarded (`runtime` purity, `agents→search`, the
private seams); nothing guards `core` or `events` purity, `engine↛serve`, `tools↛serve`, `adapters`
(doc 50 XP-07). The fix is one AST guard over the package matrix with the deferred-import allowance
explicit per edge. proof:missing:tests/test_package_layering.py

OPEN[settings-doc-guard-compares-names-not-defaults] `tests/test_config_docs_sync.py` asserts that
every `Settings` field has a row in `docs/guide/configuration.md` and never that the row's DEFAULT is
the field's (doc 50 DX-03); CLAUDE.md's sync rule says every row must carry the CORRECT default, and
by hand it does today. The fix compares each documented default against `Settings` and fails on the
first drift. proof:absent:default@tests/test_config_docs_sync.py

### 4.3 From the docs pass (§2.1)

OPEN[lessons-are-not-operator-scoped] cross-run lessons are keyed by task fingerprint and retrieved
for every proposal regardless of the OPERATOR about to fire (`engine/lessons.py` — draft, improve,
merge, ablate all read one pool), while AIRA's per-operator scoped memory and ML-Master's
parent-plus-siblings context are the recurring winning design (doc 10 M1, "designed, still
unbuilt"). The fix is an `operator_scoped` retrieval keyed on `(fingerprint, operator)` beside the
existing pool. proof:absent:operator_scoped@looplab/engine/lessons.py

OPEN[no-plan-artifact-with-endgame-reserve] a run has no durable plan: budget allocation across
phases, an endgame reserve for confirmation and ensembling, and re-planning on stagnation live in
nobody's record (doc 10 P2, doc 11 D13). The fix is an `EV_PLAN` event the fold applies and an
endgame reserve the dispatcher honours. proof:absent:EV_PLAN@looplab/events/types.py+absent:endgame@looplab/engine/orchestrator.py

OPEN[strategist-consult-is-cadence-not-stagnation-triggered] `engine/strategy.py` consults the
Strategist every `strategist_every` nodes and never because the run has STOPPED IMPROVING; the
stagnation signal that MLEvolve's stagnation detection and doc 11 D3's FML-bench result key on is
mentioned only in the Strategist's prompt (`agents/strategist.py`). The fix is a deterministic
plateau trigger over folded metrics that makes a consult due early, with the cadence as the fallback.
proof:absent:stagnat@looplab/engine/strategy.py+absent:plateau@looplab/engine/strategy.py

OPEN[no-hack-adjusted-score-reporting] the trust layer flags reward hacks, leakage and salvage on the
row and no report projects a HACK-ADJUSTED score beside the raw one — the number a benchmark
publication would need and the number doc 26 §4.2 #2 asked for. The fix is a `hack_adjusted` field on
the run summary and the report, derived from the flags already folded.
proof:absent:hack_adjusted@looplab

OPEN[prior-injection-hit-rate-unmeasured] every cross-run prior (lessons, skills, capsules, claims)
is injected into prompts and nothing measures whether an injected prior was USED by the proposal that
followed or changed its outcome (doc 26 §4.2 #9, doc 41 §8 step 3) — so memory growth is unbounded by
any utility signal. The deliverable is an audit over the box's logs: injected priors × cited-in-idea ×
metric delta. proof:missing:docs/audit/prior-injection-hit-rate.md

OPEN[deep-research-plan-is-not-durable] Deep Research planning survives only inside one tool-loop
context: no `ResearchPlan`, no `ProgressLedger`, no replayable episode state, so a resume repeats a
settled question and a crash after the attempt receipt loses the branch (doc 28 DR-01, its own P0).
proof:absent:ResearchPlan@looplab/agents/deep_research.py+absent:ProgressLedger@looplab/agents/deep_research.py

OPEN[research-evidence-has-no-exact-span-identity] a research memo's evidence is a URL plus a short
snippet — no immutable `EvidenceItem` with locator, hash and provenance — so a verifier verdict cannot
be re-checked later and a claim cannot resolve to a stable evidence id (doc 28 DR-02, its own P0;
Kosmos cites every statement to code or literature). proof:absent:EvidenceItem@looplab

OPEN[landlock-refusal-is-not-translated-for-triage] under `landlock="enforce"` a refused read arrives
as `EACCES` → `PermissionError`, the silent-skip shape the read fence's own exception type exists to
avoid, and nothing at the repair boundary rewrites that failure into the fence's sentence before the
triage judge reads it (doc 38 §3 item 2). Blocked behind the default flip (`landlock-is-opt-in-by-default`).
proof:absent:EACCES@looplab/engine/crash_repair.py+absent:EACCES@looplab/engine/failure_diagnosis.py

OPEN[parallel-build-has-no-golden-replay] doc 22's phase 4 specified "a new golden for a 2-wide
parallel-build run" pinning id monotonicity, one terminal per node and a deterministic fold, and
`tests/test_golden_replay.py` still holds only the serial golden — the only golden this concurrency
seam has is the one that cannot see it. proof:absent:parallel@tests/test_golden_replay.py

OPEN[no-distance-from-seed-signal] nothing measures how far a candidate has moved from the seed
program, so "still the baseline with a comment" and a real change are indistinguishable to selection
and to novelty (doc 17 §11; ResearchStudio's Scoop-Check is the precedent). A pure function over
committed code — a diff-size and structure distance — is the fix.
proof:absent:distance_from_seed@looplab/search+absent:seed_distance@looplab/search

OPEN[stage-assert-has-no-model-free-numeric-form] a stage's `expect.assert` is judged by an LLM
against the stage's printed tail, so it is only as good as what the stage prints and it was measurably
unstable (the 2.33 GPU-h re-train in BACKLOG §0.9); the obvious next step — a declared numeric
relation the ENGINE evaluates against a named key the stage prints — is not built (BACKLOG §6).
proof:absent:expect_numeric@looplab/runtime/command_eval.py+absent:numeric_assert@looplab/runtime/command_eval.py

OPEN[stage-rows-are-last-wins-per-name] `replay.py::_on_stage_finished` keeps one row per stage
NAME, so after an inline repair the attempt that actually spent the training wall-clock is not on the
node's record (BACKLOG §6 D5); the repair-epoch stamp says which attempt a surviving row belongs to,
not what the replaced attempts cost. Accounting and UI only; metrics and replay are unaffected.
proof:`present:n.stages[i] = rec@looplab/events/replay.py`

OPEN[readmodel-watermark-ignores-event-data] `readmodel.py::coverage_watermark` digests the ordered
`(seq, type)` prefix and nothing about event DATA, so a log whose `node_evaluated.metric` was edited
in place still certifies `current` while the fold answers the new value (BACKLOG §0.2, driven).
proof:`present:rows = [[int(getattr(e, "seq", -1)), str(getattr(e, "type", ""))] for e in events]@looplab/events/readmodel.py`

OPEN[launch-readiness-gate-is-two-copies] "is this task launchable" is spelled twice —
`adapters/repo_task.py::EvalSpec._command_or_stages` and `serve/tui_format.py::spec_ready`, whose own
docstring points at the backlog row — and there is no `/api/validate` (BACKLOG §5).
proof:`present:def spec_ready@looplab/serve/tui_format.py`

OPEN[cross-run-trajectory-overlay-unbuilt] the run-comparison screen ranks runs of one task and cannot
overlay their metric TRAJECTORIES, because the run-list payload carries `nodes` as a count and no
series; `ui/src/crossRunRank.js` names the gap in its own constant (BACKLOG §0.1 #10).
proof:`present:export const TRAJECTORY_GAP@ui/src/crossRunRank.js`

### 4.4 Verified open, and NOT tagged — with the reason

* **Two GETs still take the exclusive command sequencer** (`/api/runs/{run_id}/concepts/lens/recovery`,
  `/api/runs/{run_id}/log-page`; doc 50 SR-02's residue). Count-shaped: the literal survives at the
  eight legitimate POST sites, and the index has no count predicate. Fix it with #3 in §5.
* **The log-integrity receipt counts lines as records** (`eventstore.py::log_divergence`; a batch
  envelope is one line of up to 4,096 events; BACKLOG §0.2). No single line decides it.
* **"Parallel sidecar ordering"** (doc 03 §Open): a design decision, no symbol.
* **"No first-class Evaluator"** (doc 17): a naming question about a subsystem that is distributed by
  design.
* **Browser-level accessibility evidence** (doc 18): product work whose cost nobody has measured.
* **Two idiom duplications** (the socket-shutdown idiom, the timeout-nulling spelling): cross-referenced
  residue, cheaper open than tracked.
* **The nine in-code `CODEX AGENT` notes**: not re-derived one by one this pass (doc 50 EV-09).

### 4.5 Deliberately NOT on the plan

* **The paid cadences off the loop** (0.3 % of a run, re-measured 2026-09-04).
* **A legal-action set for the card lane.** Built on 2026-09-03 and REVERTED; a product decision.
* **Claim refutation flowing down as undercut.** Its trigger has not fired.
* **The semantic belief key.** Unbuilt and unvalidated; design first, on the corpus.
* **The doc 25 modularity ledger** (30 items) as a programme; paid when the file is open.
* **The declines** (§1.1). Each carries a number; none is reopened here.

---

## 5. The final ranked plan

One ordered list. Rank is cost of leaving it, with the SOTA leverage of §3 folded in; "S/M/L" is
effort in this tree's own terms (S: one change with one driven test; M: days with a design note; L: a
shape change with its own doc). **Every row names the marker(s) it retires**, so closing a row is a
deletion and this list can be re-derived from `grep -rn 'OPEN\['` at any time.

### 5.1 Do in this order

| # | Item | Why here | Size | Retires |
|---|---|---|---|---|
| 1 | Pin `require_approval` into `core/config.py::RUN_START_PINNED_FIELDS`, with a resume test that a snapshot EDIT cannot move the approval gate | invariant #6 does not cover the one setting that gates a paid finish; one line | S | `require-approval-not-pinned-at-run-start` |
| 2 | The three claim readers through `run_ref` / `row_belongs_to_run`, each with the two-incarnation fixture | the half still standing refuses every ratification when two runs share a name | S | `claim-readers-still-key-on-run-id` |
| 3 | A refusal-code table in `serve/http.py` with a guard; in the same change the last two GETs off the exclusive sequencer through `generation_fence` | six `500` sites; a read waiting on a cross-process `flock` behind a live run's writes | S | `refusal-codes-have-no-table` (+ §4.4's first row) |
| 4 | `run_setup` failure as an `EnvironmentRefusal`; the systemic-stop sentence into the terminal-reason registry with its two-way scan | two P15 rows the sweep left | S | `run-setup-failure-is-not-a-refusal-type`, `systemic-stop-reason-has-no-registry` |
| 5 | One launch-readiness gate behind a `/api/validate` route | two copies, one pointing at the backlog from its own docstring | S | `launch-readiness-gate-is-two-copies` |
| 6 | **Hidden consistent evaluation for `repo_task`**: a `holdout` declaration (a host-side score stage over a split the candidate never reads, bound like `metric_subject`), `generalization_gap` folded for repo runs, selection through `holdout_select`; replay-digest proof that runs without the declaration are byte-identical | the largest measured selection lever in the field, open on the box's own runs | M | `repo-task-champion-is-picked-on-the-candidates-own-metric` |
| 7 | **A `DeveloperResult` envelope, then the repair path off the loop** (`_triage_crash` / `_repair` / `_repair_critic` under `to_thread` with the capture-sink discipline of `novelty.py`; a loop-liveness twin of `tests/test_propose_does_not_freeze_the_loop.py` per site) | zero loop ticks during a 116–276 s median hold, one case 88.3 min; the envelope is the precondition | M | `developer-output-has-no-immutable-envelope`, `repair-path-holds-the-engine-loop` |
| 8 | **The research-grade knobs, measured**: one paired run per profile (`default` vs `thorough`) on the same repo task on the box; then the flip or the decline | the field's technique map says the difference is large; this repo shipped the preset and never A/B'd it | S code, box time | `research-grade-profile-is-not-the-default` |
| 9 | **One untrusted-evidence envelope** (`core/evidence.py`) behind a Settings flag (legacy default off) on the Strategist, triage / critic stderr and arXiv / web results; a test derived from `PROMPT_KEYS` | the Strategist sets `eval_parallel` / `policy` / `timeout` from a note with no rule | M | `no-single-untrusted-evidence-envelope` |
| 10 | **Containment made countable, not fixed**: ruff `BLE001` as a census, the 634 `noqa`s as a reviewed allow-list, a `contain(span, reason)` helper, the AST funnel "every broad `except` around a paid call re-raises `BudgetExceeded` first" | the cost was found at the seams; the funnel has a known victim and no guard | M | `containment-is-unmeasured` (census half) |
| 11 | **A stagnation trigger for the Strategist**: a deterministic plateau reading over folded metrics that makes a consult due early | the cheapest search-side item with a field-measured precedent (MLEvolve, FML-bench); the cadence stays as the fallback | S–M | `strategist-consult-is-cadence-not-stagnation-triggered` |
| 12 | **Code-level merge and an endgame ensemble**: `operators.py::merge_code` reading both parents' committed code; a top-k prediction ensemble with a reserved budget where a predictions file exists — and the plan artifact that reserves it | the leaderboard systems' endgame; the only multi-parent operator here averages params | M | `merge-operator-is-mean-of-params-not-code`, `no-plan-artifact-with-endgame-reserve` |
| 13 | **The memory stack, in order**: the five doc 51 items (bound `use_skill` through `clip`; a demotable skill status; `knn_idw`'s uncertainty spent in `panel.py` and `proxy.py`; a `columns` / `data_samples` hook on `repo_task`; a registered `EV_LITERATURE_RETRIEVED`), then tiered loading, then operator scoping, with the hit-rate audit shipped in the same change as the first consolidation | HASTE: flat loading equals no skills; ML-Master 2.0: L2 + L3 are 50 points on Lite; nothing here measures the hit rate | S × 5, then M | all five doc 51 markers, `skills-load-flat-not-by-tier`, `lessons-are-not-operator-scoped`, `prior-injection-hit-rate-unmeasured` |
| 14 | **CLAUDE.md on a byte budget**: `CLAUDE_MD_MAX_BYTES` with a shrink-only baseline; rules, map, invariants and conventions stay; dated ledgers move to the docs and docstrings that hold them behind pins; the two unmapped packages added | 238 KB and growing; every agent turn pays it before reading a file | M | `claude-md-has-no-size-budget` |
| 15 | **The engine attribute guard**, then per-cluster typed state records | 772 attributes, 91 lazily minted, 143 silent handlers to absorb the typo; the guard is what makes #26 safe | S, then M | `engine-attributes-have-no-declaring-site-guard` |
| 16 | **A model-free numeric `assert`** for stage contracts: a declared relation the engine evaluates against a named key the stage prints | the LLM-judged `assert` cost a 2.33 GPU-h re-train; `check_failed` is 13.4 of 20.1 saveable hours in the bench | M | `stage-assert-has-no-model-free-numeric-form` |
| 17 | **One MLE-bench Lite campaign** through the trust layer on the box, with `hack_adjusted` reported beside raw and holdout-selected numbers, seeds and traces, as an audit page | the external proof, and the noun this repo's adjectives have never produced | S code, L wall-clock | `no-external-benchmark-number-exists`, `no-hack-adjusted-score-reporting` |
| 18 | **Guards that compare, not grep**: settings defaults compared against `Settings`; the API reference generated from `app.openapi()` under the strict build; the CLI reference from Typer | four hand-kept tables; a new route lands green and undocumented | M | `settings-doc-guard-compares-names-not-defaults`, `http-surface-has-no-generated-reference` |
| 19 | **A UI mount harness** (`ui/test/_mount.js`) and one gate-flip test per giant component; the trajectory overlay on the comparison screen once the run row carries a series | 10.6k lines mounted by no test; a dropped brace once passed 767 tests | M | `largest-ui-components-are-never-mounted`, `cross-run-trajectory-overlay-unbuilt` |
| 20 | **Verification of the seams**: a golden for a 2-wide parallel build; a layering guard over the package matrix; per-attempt stage rows; a watermark that hashes data | each is one test or one line, and each closes a hole a review found and nothing guards | S × 4 | `parallel-build-has-no-golden-replay`, `layering-rules-are-not-machine-checked`, `stage-rows-are-last-wins-per-name`, `readmodel-watermark-ignores-event-data` |
| 21 | **Retire the legacy `/control` route**: port the 41 suite call sites to `/commands`, delete it; the per-POST `EventStore` rescan shrinks with it | a lost-response retry re-appends paid intents there | M | `legacy-control-route-is-not-retired`, `eventstore-rescans-the-log-per-control-post` |
| 22 | **A model arm for the operator bandit** (ShinkaEvolve's shape) over the per-role models this repo already routes | breadth per cost; a policy change, no new trusted input | M | `operator-bandit-has-no-model-arm` |
| 23 | **The Deep Research P0 slice** (doc 28's own decision: DR-01 + DR-02 as one design slice): a durable `ResearchPlan` / `ProgressLedger` and an exact-span `EvidenceItem` ledger | replayable research state and re-checkable evidence are what make the memo's verifier verdicts mean anything later | M–L | `deep-research-plan-is-not-durable`, `research-evidence-has-no-exact-span-identity` |
| 24 | **A distance-from-seed signal** as a pure function over committed code, fed to novelty and selection as an annotation | "still the baseline" and a real change are indistinguishable today | S–M | `no-distance-from-seed-signal` |
| 25 | **The event payload contract**: describe the 65 undescribed constants and the 15 undocumented types, then `EVENT_PAYLOAD_KEYS` and a generated event-log page | invariant #5's additive rule cannot be checked against handler code | M | `event-payloads-have-no-registry` |
| 26 | **The `EvalAttempt` phase object** along `_evaluate`'s own phase comments, every append and lock staying put; verified by the corpus-digest replay | 1,898 lines reading 51 attributes | L, after #15 | `eval-attempt-is-one-giant-method`, doc 25's `evaluate-prestart-and-terminal-blocks-inline` |
| 27 | **Cross-machine eval dispatch** with AIRA₂'s decoupling shape (an orchestrator that dispatches to whichever worker is free; the log stays the single writer) | throughput is linear in GPUs for the field's best; the box has two | L, after #7 | `eval-parallelism-is-in-process-only` |
| 28 | **After the Landlock default flips**: the `EACCES` translation at the repair boundary | a kernel refusal must not read as a missing file to the triage judge | S, after the flip | `landlock-refusal-is-not-translated-for-triage` |
| 29 | The remaining product rows, each when its trigger fires or the file is open: MLflow autolog (S), Pareto front where selection reads it under `trust_gate=select` (M), a drift detector (M), an LLM value estimate for MCTS (M), the feature-engineering operator (M), a real forecasting backend (M); and the doc 25 / doc 27 / doc 34 ledgers when their files are open | real gaps with driven falsifiers, lower leverage than everything above | — | the six BACKLOG product markers; the 48 ledger markers |

### 5.2 The box-only queue (needs `runs/` or a GPU)

In the order they pay: (1) #8, the profile A/B; (2) the serial-build harm report — per `card_build`
span, was a GPU FREE with a claimable card while the loop was held (`serial-node-build-holds-the-loop`);
(3) #17, the MLE-bench Lite campaign; (4) the hack-rate audit (`developer-hack-rate-unmeasured`);
(5) the prior-injection hit-rate audit (`prior-injection-hit-rate-unmeasured`); (6) the first-propose
split between `research_cadence.py::_ground_run_start` and the first propose
(`first-propose-runs-with-every-gpu-idle`); (7) ASHA's promotion mask soundness
(`asha-promotion-mask-blocks-all-production`); (8) `TrainingVerdict.fault`'s outcome label
(`monitor-fault-has-no-outcome-label`); (9) researcher questions under the prose ask
(`researcher-questions-not-appended`); (10) Landlock's default via `looplab landlock-check`
(`landlock-is-opt-in-by-default`); (11) the two caches' counts (`knowledge-index-re-embeds-every-record`,
`repeated-sweep-refolds-the-whole-corpus`); (12) crash lead time in the bench corpus builder
(`crash-predictability-unmeasured`).

### 5.3 Dependencies stated once

`DeveloperResult` before the repair offload (#7); the attribute guard before the `EvalAttempt` split
(#15 → #26); the loop offload before the cross-machine pool (#7 → #27); doc 51's skill bounds before
tiered loading and operator scoping (#13, in that order); the payload registry before a generated
event page (#25 → #18's last table); the CLAUDE.md diet (#14) before anything that adds prose to it;
the plan artifact with #12, because the endgame ensemble is what the reserve is for; the Landlock
flip before #28.

---

## 6. Baseline record for this head

`master` at `bf860b7` (2026-09-04); plan branch `claude/prioritize-development-plan-0k77gb`.

* Doc guards and the seven formerly-red tests: green (this container, 2026-09-05 and after each
  2026-09-06 marker edit).
* `ui/`: 1,527 passed / 0 failed.
* Full Python suite (`-m "not docker"`, four `pytest-split` shards): 0 failures / 0 errors / 80 skips /
  13,059 passed. A later reader re-runs rather than trusts it.

## 7. How to work this plan

* One row is one change with one driven test; a row's close is the DELETION of the marker it names,
  never an edit to this page. When this page and the tree disagree, the tree is right and this page
  is stale — it is dated for that reason.
* No new marker without a falsifier the guard re-derives AND that flipped under mutation; no decline
  without a number. A proof names the fix's OWN symbol or the defect's own text — §2.0 is the cost of a
  name guessed in advance, and a fix that lands under another name re-points the proof in the same
  change.
* A measurement precedes a policy: #8, #17 and every §5.2 row ship the instrument first.
* The trust line is not negotiable on any capability row: an advisory rung may re-rank, refuse or
  annotate; it may not mint a metric, a champion, a violation or a selection. A hidden split (#6) is
  admissible precisely because it can only REFUSE a champion the search metric would have elected.

## 8. Sources for §3

Primary sources, fetched 2026-09-06; the forwarded numbers are theirs, not re-derived here.

* MLE-bench official leaderboard rendition — https://www.mlebench.com/
* MLEvolve — https://github.com/InternScience/MLEvolve
* ML-Master 2.0, "Toward Ultra-Long-Horizon Agentic Science: Cognitive Accumulation for Machine Learning Engineering" — https://arxiv.org/abs/2601.10402
* AIRA₂, "Overcoming Bottlenecks in AI Research Agents" — https://arxiv.org/abs/2603.26499
* Arbor, "Toward Generalist Autonomous Research via Hypothesis-Tree Refinement" — https://arxiv.org/abs/2606.11926 and https://github.com/RUC-NLPIR/Arbor
* EurekAgent, "Agent Environment Engineering is All You Need for Autonomous Scientific Discovery" — https://arxiv.org/abs/2606.13662
* HASTE, "Why Solve It Twice? Hierarchical Accumulation of Skills for Transfer-Efficient ML Engineering" — https://arxiv.org/abs/2606.30911
* Frontis-MA1 / OpenRSI — https://arxiv.org/abs/2607.28568 and https://github.com/FrontisAI/OpenRSI
* Famou-Agent 2.0 — https://github.com/baidubce/FM-Agent
* ShinkaEvolve — https://arxiv.org/abs/2509.19349 and https://github.com/SakanaAI/ShinkaEvolve
* AlphaEvolve, 2026-08 matrix-multiplication result — https://github.com/google-deepmind/alphaevolve_results
* Kosmos / Edison Scientific — https://www.futurehouse.org/about
* karpathy/autoresearch — https://github.com/karpathy/autoresearch
* "Autonomous Research Agents: A Survey of AI Scientists and the Verification Gap" — https://arxiv.org/abs/2608.05179
* BAITBENCH, "Measuring Agent Reward Hacking with Optional Shortcuts Planted in ML Tasks" — https://arxiv.org/abs/2608.30724
* "From Fluent to Verifiable: Claim-Level Auditability for Deep Research Agents" — https://arxiv.org/abs/2602.13855
* AARRI-Bench, "Act As a Real Researcher" — https://arxiv.org/abs/2606.07462
