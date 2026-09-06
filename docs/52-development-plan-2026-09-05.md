# 52 · Development plan (2026-09-05, revised 2026-09-06): what to build, improve and fix next

**Status: a PLAN over the open-item index, re-derived against `master` on 2026-09-05 and revised on
2026-09-06 after a second verification pass and an external SOTA pass.** It ranks; it ships no
engine change and flips no default. Its inputs are the three things this repo treats as the backlog —
the greppable marker index (`grep -rn 'OPEN\['`, counted with the guard's own parser in
`tests/test_open_item_index.py`), the whole-tree finding ledger of
[doc 50](50-architecture-review-2026-09-02.md) with its ranked proposals P1–P15, and the five
external-works items of [doc 51](51-external-works-synergy-2026-09-03.md) — read against the 71
commits that landed between 2026-09-01 and 2026-09-04, and, since the revision, against what the
field's leading systems do in September 2026 (§3, every claim with its source).

What the revision CHANGED in the tree, and it is deliberately small: one stale marker closed and one
converted to a decline in `docs/BACKLOG.md` (§2), and four new markers minted in this page (§4), each
with a falsifier that was evaluated against the tree before it was written. Nothing else moved.

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
2026-07 SOTA sweeps this page's §3 updates), [doc 41](41-external-works-synergy-2026-08-14.md) and
[doc 51](51-external-works-synergy-2026-09-03.md) (the two external-works passes),
[doc 45](45-claim-surfaces-2026-08-20.md) (why a recorded number is pinned to the site that decides
it), [doc 36](36-agent-driven-decisions-2026-08-13.md) (the trust line every capability item must
hold), [BACKLOG](BACKLOG.md) and [CODE_REVIEW](CODE_REVIEW.md) (the two prose ledgers whose live rows
carry markers).

---

## 1. Where the tree stands

### 1.1 The index, by home

Counted by the guard's parser after this revision's edits: **105 open markers and
20 declines**, in 26 files. (Before the revision: 103 / 19 in 25 files.)

| Home | Open | What lives there |
|---|---:|---|
| `docs/25` modularity ledger | 30 | god-module splits, duplicated scaffolds, hand-rolled guards — judgement calls, not defects |
| `docs/BACKLOG.md` | 21 | product gaps (Pareto, drift, MLflow, MCTS value, distributed eval), cadence and watchdog residue, the claim ladder |
| `docs/27` agent-system review | 14 | budgets, receipts, cancellation, prompt governance, the eval ladder |
| `docs/51` external works | 5 | the cheap capability items (skills, kNN uncertainty, perception hook, literature) |
| `docs/52` (this page, §4) | 4 | the SOTA gaps the revision found falsifiable: hidden-split selection for repo tasks, a code-level merge, the research-grade default, the external number |
| `docs/50`, `docs/34`, `docs/CODE_REVIEW.md` | 4 + 4 + 4 | whole-tree items; the four deferred product decisions; the four surviving review rows |
| `looplab/` (in code, at the site) | 14 | the loop holds, the eval method, the card lane, the payload registry, the legacy route, two caches, two measurements |
| `tests/` | 3 | three guards that state their own limit |
| `docs/29`, `docs/46` | 1 + 1 | the F3 follow-up; the `.py`-only params guard |

Of doc 50's twelve site markers, three closed in the sweep (the sibling-cancelling eval boundary,
the unlabelled assistant tool results, the read fence on the command sequencer) and nine stand.

### 1.2 What the 71 commits since 2026-09-01 closed, against doc 50's proposals

| Proposal | State on 2026-09-06 | Residue that decides the rank below |
|---|---|---|
| P2 per-child containment | **done** (`engine_error` terminal; `adapter` refused at submit) | — |
| P3 one run identity for readers | mostly done (`core/run_identity.py::run_ref` / `row_belongs_to_run`; lessons, capsules, claims health) | `claims_assessments.py::_qualify_refs` / `_ingest_evidence`, `claims_retrieval.py::portfolio_atlas`, `concept_shelf.py::run_concept_index` still key on `run_id` — the EK-03 half that demotes one-sided verdicts |
| P5 unwedge Replay | **done** (`unique_destination` in `serve/reset_route.py`) | — |
| P6 six vocabularies | 4 of 6 (engine terminal reasons in `core/models.py`, stage statuses in `runtime/command_eval.py`, command statuses in `serve/protocol.py`, the `KIND_*`/`META_*` constants now read by `search/card_selection.py`) | `EVENT_PAYLOAD_KEYS` (a documentation job, its marker says so); permission still decided by `perm_modes._ACTION_RISK` with `ToolCapability` read once |
| P9 closed task schema | unknown keys refused at every launch layer — the config file, the task document (refused on SUBMIT, grandfathered on RELOAD through `repo_task.py::_grandfathered`, deliberately not `extra="forbid"`), the stage manifest | no `schema` stamp on `task.snapshot.json`; no per-kind reader key table beyond `READER_PATH_KEYS` |
| P10 read-side HTTP rules | one fold per request (`appstate.request_fold_scope`); `generation_fence` exists | two GETs still take the exclusive sequencer (`/concepts/lens/recovery`, `/log-page`) plus `_state_payload`'s reset-marker reconcile; no refusal-code table (`serve/http.py` absent, six `500` sites) |
| P4 one untrusted-evidence boundary | 3 surfaces (assistant, concept tagger, MCP cache key) | `agents/strategist.py`, `engine/triage.py`, `agents/unified_agent.py`, `tools/literature.py` carry no label; no `core/evidence.py`, no prompt assembler |
| P15 small fixes | most landed (receipts, reaper, parser, fsync, off-by-one, approval bypass) | `EnvironmentRefusal` for `run_setup`; the systemic-stop registry |
| P1 loop offload | **not started** — and re-measured (§1.4) | the repair path, the serial build |
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

## 2. Second verification pass: what it changed

Every one of the 103 markers was read against its proof and, where the proof names a symbol the fix
might have landed under another name, against the tree. Two did not describe the tree, and both are
now fixed IN THIS CHANGE rather than listed as work:

* **`judge-bench-cannot-see-a-post-exit-stage-failure` — CLOSED (deleted).** It asked for "the
  declared `expect`/`assert` contract, in front of the judge while the stage still runs" and proved
  itself open by `absent:monitor_expect_context@looplab/engine/train_monitor.py`. That evidence shipped
  on 2026-08-20 under another name — `train_monitor.py::stage_contract_context`, spliced into the
  judge's tick under `Settings.train_monitor_contract` (ON) — and was scored on the committed
  450-decision corpus (CLAUDE.md's `train_monitor.py` row: 12 decisions fire, 12 wasted / 0
  productive, 6 -> 9 of 27 wasted attempts caught). The guard stayed green for seventeen days over a
  shipped item because no symbol was ever spelled `monitor_expect_context`. The BACKLOG paragraph now
  records the closure and keeps the measurement.
* **`judge-bench-covers-two-judges-of-four` — converted to a DECLINE.** Its own text says why the
  remaining two judges will not be benched: the repair critic has 7 decisions in the whole corpus and
  the novelty gate rejects ideas that are never run, so no outcome label can exist for it. A decline
  with a number, per the house rule, so the index stops asking for it.

Everything else re-verified as it stood on 2026-09-05. Two wordings in this page's own §1.2 were
corrected in the same pass: P6's fourth vocabulary is the policy `KIND_*`/`META_*` constants, and
P9's task-document layer refuses unknown keys on submit and grandfathers on reload by design
(`repo_task.py::_grandfathered`), so `extra="forbid"` is not its residue.

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
| **Hierarchical memory** — traces → phase knowledge → cross-task priors, loaded by tier | ML-Master 2.0 (22.7 → 54.5 → 72.7 %), HASTE (flat loading = no skills), MLEvolve, Famou 2.0 | L1 = spans + event log; L2 = `failure_reflection` / `watchdog_reflection` (ON), memos, cards/beliefs; L3 = fingerprint-keyed lessons incl. negatives, skills, concept capsules, claims, hybrid BM25+vector retrieval | structurally at parity; the measured gaps are the skill LIFECYCLE (doc 51: unbounded body, never demoted), tiered LOADING (HASTE's result), and the prior-injection hit rate nobody has measured (doc 26 §4.2 #9) — **GAP #3** |
| **Search: multi-branch tree with insight propagation, stagnation detection, cross-branch fusion; evolutionary novelty rejection + parent sampling + model bandit; ablation-targeted refinement + ensembling; a Crossover operator** | MLEvolve, Arbor, ShinkaEvolve, MLE-STAR, Frontis-MA1 | greedy / evolutionary / MCTS / ASHA / BOHB policies over folded state, cards + beliefs, graded novelty, `engine/ablation.py` (MLE-STAR refine_block), `search/policy.py::_bandit_pick` over OPERATORS, foresight + surrogate + panel; the only multi-parent operator is `merge_idea` = MEAN OF NUMERIC PARAMS | search breadth at parity or ahead; **GAP #4** is the endgame: no code-level crossover, no top-k ensemble of predictions; and the knobs that matter (`operator_bandit`, `ablate_every`, `confirm_top_k`, `trust_gate="gate"`) live in `PROFILES["thorough"]` and are OFF under the shipped `profile="default"` |
| **Verification and anti-hacking as the differentiator** | the survey's 38 %; BAITBENCH's 57.1 %; EurekAgent's controller-owned result files; claim-level auditability (AAR) | replayable log, `metric_subject`, read fence + Landlock, `reward_hack`, `leakage` (Pearson + Spearman), salvage rules, claims ledger with verifier verdicts, W3C-PROV export, redaction of every persisted tail | **AHEAD** — this is the axis on which "better than SOTA" is credible; what is missing is the PROOF: no published number, no hack-rate measurement of its own Developer, Landlock off by default |
| **Budget and time awareness** — a time helper the agent can call, deadline warnings, fixed-budget experiments | EurekAgent, autoresearch | `effective_eval_time_budget`, the time and memory cues, `train_monitor`'s projected overrun, `budget_aware` (OFF by default) | parity |
| **Literature and data grounding** | Kosmos (every statement cited), Mechanist, OmniScientist, AutoMind's KB, MLE-STAR's web-seeded drafts | `tools/literature.py` (not durable), knowledge tools, `EV_DATA_PROFILED` for six adapters and NOT for `repo_task` | doc 51's five items — cheap, all falsifiable |
| **Human-in-the-loop steering** | EurekAgent's TUI + web monitor, Arbor's tree | cards / kanban, branch-from-history, standing watches, assistant, reviews | parity or ahead |
| **Environment engineering** — permissions, artifacts, budgets as the product | EurekAgent | sandbox tiers, fences, receipts, the durable-op kit | ahead |

### 3.3 What "better than SOTA" has to mean for LoopLab

Not "a higher medal rate at any cost": the survey and BAITBENCH say the field's numbers are produced
by systems that release traces 38 % of the time and hack 57 % of the time when a shortcut is
available. The credible target has three parts, and the order is the order of leverage:

1. **A number nobody at the top publishes: medal rate WITH holdout selection, hack-adjusted, from a
   replayable log.** LoopLab already has the machinery for every adjective; it has never produced the
   noun. One MLE-bench Lite campaign through the trust layer (§4 `no-external-benchmark-number-exists`)
   is the external proof, and the target is the top cluster (77–86 % Lite) on the honest number.
2. **The same selection discipline on the box's own tasks.** The dense-retrieval runs this box pays
   for choose a champion on the candidate's printed metric. A hidden consistent split for `repo_task`
   is the single largest measured lever in the field (AIRA₂'s +13 / +18.4) and it is one declaration
   plus a host-side score stage (§4 `repo-task-champion-is-picked-on-the-candidates-own-metric`).
3. **Throughput that scales with what the box has**, which is P1's loop offload first and the
   cross-machine pool after — and, before either, the research-grade knobs measured against the
   default on the same task, because the field's technique map says the difference is large and this
   repo has shipped it as a preset nobody has A/B'd.

### 3.4 What this pass could not establish

LoopLab's own position on any external benchmark. `docs/MLEBENCH.md` documents the real host-graded
path and records no completed run; doc 41 §8 step 5 asked for one on 2026-08-14 and it has not
happened. Every "ahead / parity / gap" verdict in §3.2 is therefore a verdict about MECHANISMS
present in the tree, not about outcomes — which is exactly the field's verification gap, turned on
this repo.

---

## 4. The refined open items

Four new markers, minted here because each has a falsifier evaluated against the tree on
2026-09-06. Per the house rule, the fix must land under the NAMED symbol or re-point the proof —
the §2 closure is what happens otherwise.

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

### 4.1 Deliberately NOT tagged

* **A model arm for the operator bandit** (ShinkaEvolve's LLM-ensemble bandit): a policy change over
  per-role models this repo already routes; its shape is a design call, so it is a plan row (§5, #20)
  and not a marker.
* **Tiered skill loading** (HASTE): the skill library first has to be bounded and demotable (doc 51's
  markers), or a tier is a tier of unbounded bodies; ranked behind them.
* **A hack-rate measurement of LoopLab's own Developer** (BAITBENCH's shape): box-only, §5.2.
* The structural proposals whose shape is a judgement call (doc 50 §8's own rule).

### 4.2 Deliberately NOT on the plan

* **The paid cadences off the loop** (0.3 % of a run, re-measured 2026-09-04). A concurrency change
  against invariant #1 is not bought for that.
* **A legal-action set for the card lane.** Built on 2026-09-03 and REVERTED; a product decision
  between two designs, recorded at the marker.
* **Claim refutation flowing down as undercut.** Its trigger has not fired.
* **The semantic belief key.** Unbuilt and unvalidated; design first, on the corpus.
* **The doc 25 modularity ledger** (30 items) as a programme; paid when the file is open.
* **The 20 declines.** Each carries a number; none is reopened here.

---

## 5. The final ranked plan

One ordered list. Rank is cost of leaving it, with the SOTA leverage of §3 folded in; "S/M/L" is
effort in this tree's own terms (S: one change with one driven test; M: days with a design note; L: a
shape change with its own doc). Each row names what it retires so that closing it is a deletion.

### 5.1 Do in this order

| # | Item | Why here | Size | Retires |
|---|---|---|---|---|
| 1 | Pin `require_approval` into `core/config.py::RUN_START_PINNED_FIELDS`, with a resume test that a snapshot EDIT cannot move the approval gate | invariant #6 does not cover the one setting that gates a paid finish; one line | S | `require-approval-not-pinned-at-run-start` |
| 2 | Route `claims_assessments.py::_qualify_refs` / `_ingest_evidence`, `claims_retrieval.py::portfolio_atlas`, `concept_shelf.py::run_concept_index` through `run_ref` / `row_belongs_to_run`, each with the two-incarnation fixture | the EK-03 half still standing flips `producer_receipt_known=False` and refuses every ratification when two runs share a name | S | doc 50 EK-03 residue |
| 3 | The last two GETs off the exclusive command sequencer (`/concepts/lens/recovery`, `/log-page`) through `generation_fence`; decide `_state_payload`'s reset-marker reconcile the same way | a read waiting on a cross-process `flock` behind a live run's writes | S | doc 50 SR-02 residue |
| 4 | A refusal-code table in one `serve/http.py` with a guard: no `500` for unreadable input, no host path in a reflected `OSError` | six `500` sites where every sibling answers `503`; one reflects a filesystem path | S | doc 50 SR-05 |
| 5 | `run_setup` failure as an `EnvironmentRefusal`; the systemic-stop reason into the terminal-reason registry with its two-way scan | two P15 rows the sweep left | S | doc 50 ES2-06, ES1-06 |
| 6 | **Hidden consistent evaluation for `repo_task`**: a `holdout` declaration (a host-side score stage over a split the candidate never reads, bound like `metric_subject`), `generalization_gap` folded for repo runs, selection through `holdout_select`; replay-digest proof that runs without the declaration are byte-identical | the largest measured selection lever in the field, open on the box's own runs | M | `repo-task-champion-is-picked-on-the-candidates-own-metric` |
| 7 | **A `DeveloperResult` envelope, then the repair path off the loop** (`_triage_crash` / `_repair` / `_repair_critic` under `to_thread` with the capture-sink discipline of `novelty.py`; a loop-liveness twin of `tests/test_propose_does_not_freeze_the_loop.py` per site) | zero loop ticks during a 116–276 s median hold, one case 88.3 min; the envelope is the precondition and is itself a doc 27 item | M | `repair-path-holds-the-engine-loop`, `developer-output-has-no-immutable-envelope` |
| 8 | **The research-grade knobs, measured**: one paired run per profile (`default` vs `thorough`) on the same repo task on the box; then the flip or the decline | the field's technique map says the difference is large; this repo shipped the preset and never A/B'd it | S code, box time | `research-grade-profile-is-not-the-default` |
| 9 | **The remaining untrusted-text surfaces** behind one Settings flag (legacy-snapshot default off): the Strategist's memory note, triage and repair-critic stderr, arXiv/web results; one `core/evidence.py` envelope; a test derived from `PROMPT_KEYS` | the Strategist sets `eval_parallel` / `policy` / `timeout` from a note with no rule; the triage judge decides the repair directive from verbatim stderr | M | doc 50 AG-02, TO-06 |
| 10 | **Containment made countable, not fixed**: ruff `BLE001` as a census, the 634 `noqa`s as a reviewed allow-list, a `contain(span, reason)` helper, the AST funnel "every broad `except` around a paid call re-raises `BudgetExceeded` first" | the cost was found at the seams; the funnel has a known victim and no guard | M | `containment-is-unmeasured` (census half) |
| 11 | **Code-level merge and an endgame ensemble**: `operators.py::merge_code` reading both parents' committed code; a top-k prediction ensemble with a reserved budget where a predictions file exists | the leaderboard systems' endgame; the only multi-parent operator here averages params | M | `merge-operator-is-mean-of-params-not-code` |
| 12 | **The five doc 51 items in one pass** (bound `use_skill` through `clip`; a demotable skill status; `knn_idw`'s uncertainty spent in `panel.py` and `proxy.py`; a `columns` / `data_samples` hook on `repo_task`; a registered `EV_LITERATURE_RETRIEVED`), then **tiered skill loading** with a prior-injection hit-rate audit (doc 26 §4.2 #9) | HASTE: flat loading equals no skills; ML-Master 2.0: L2 + L3 are 50 points on Lite; nothing here measures the hit rate | S × 5, then M | all five doc 51 markers |
| 13 | **CLAUDE.md on a byte budget**: `CLAUDE_MD_MAX_BYTES` with a shrink-only baseline; rules, map, invariants and conventions stay; dated ledgers move to the docs and docstrings that hold them behind pins; the two unmapped packages added | 238 KB and growing; every agent turn pays it before reading a file | M | `claude-md-has-no-size-budget`, doc 50 XP-09/XP-12 |
| 14 | **An engine attribute guard** (every `self._x` read in `engine/` has exactly one declaring site), then per-cluster typed state records | 772 attributes, 91 lazily minted, 143 silent handlers to absorb the typo — the `_AshaStub` incident; the guard is what makes #22 safe | S, then M | doc 50 XP-08 (guard half) |
| 15 | **One MLE-bench Lite campaign** through the trust layer on the box: raw, hack-adjusted, holdout-selected numbers, seeds and traces, recorded as an audit page | the external proof, and the noun this repo's adjectives have never produced | S code, L wall-clock | `no-external-benchmark-number-exists` |
| 16 | **Generated references with guards that compare**: settings defaults, the CLI reference from Typer, an API reference from `app.openapi()` under the strict build, the doc-guard scope widened | four hand-kept tables; a new route lands green and undocumented | M | `http-surface-has-no-generated-reference`, doc 50 DX-03/04 |
| 17 | **A UI mount harness** (`ui/test/_mount.js`) and one gate-flip test per giant component; a Python-emitted `ui_vocabulary.json` for the 20 unpinned mirrors | 10.6k lines mounted by no test; a dropped brace once passed 767 tests | M | `largest-ui-components-are-never-mounted` |
| 18 | **Retire the legacy `/control` route**: port the 41 suite call sites to `/commands`, delete it; the per-POST `EventStore` rescan shrinks with it | a lost-response retry re-appends paid intents there | M | `legacy-control-route-is-not-retired`, `eventstore-rescans-the-log-per-control-post` |
| 19 | **A layering guard** over the package matrix with the deferred-import allowance explicit per edge; promote `_interprocess_lock` to a public `core/jsonlio.py` name | 38 % of edges are function-local and a third of the rules are checked | S | doc 50 XP-07 |
| 20 | **A model arm for the operator bandit** (ShinkaEvolve's shape): which model generates a given operator's proposal, learned from folded yields, over the per-role models this repo already routes | breadth per cost; a policy change, no new trusted input | M | — (a design row, §4.1) |
| 21 | **The event payload contract**: describe the 65 undescribed constants and the 15 undocumented types, then `EVENT_PAYLOAD_KEYS` and a generated event-log page | invariant #5's additive rule cannot be checked against handler code | M | `event-payloads-have-no-registry` |
| 22 | **The `EvalAttempt` phase object** along `_evaluate`'s own phase comments, every append and lock staying put; verified by the corpus-digest replay | 1,898 lines reading 51 attributes | L, after #14 | `eval-attempt-is-one-giant-method`, doc 25's `evaluate-prestart-and-terminal-blocks-inline` |
| 23 | **Cross-machine eval dispatch** with AIRA₂'s decoupling shape (an orchestrator that dispatches to whichever worker is free; the log stays the single writer) | throughput is linear in GPUs for the field's best; the box has two; changes what "quiescent" means for every main-task decision (invariant #1) | L, after #7 | `eval-parallelism-is-in-process-only` |
| 24 | The remaining product rows, each when its trigger fires or the file is open: MLflow autolog (S), Pareto front where selection reads it under `trust_gate=select` (M), a drift detector (M), an LLM value estimate for MCTS (M), the feature-engineering operator (M), a real forecasting backend (M) | real gaps with driven falsifiers, lower leverage than everything above | — | the six BACKLOG product markers |

### 5.2 The box-only queue (needs `runs/` or a GPU)

In the order they pay: (1) #8, the profile A/B; (2) the serial-build harm report — per `card_build`
span, was a GPU FREE with a claimable card while the loop was held; (3) #15, the MLE-bench Lite
campaign; (4) the first-propose split between `research_cadence.py::_ground_run_start` and the first
propose; (5) ASHA's promotion mask soundness (2.08 starved hours on v8); (6) `TrainingVerdict.fault`'s
outcome label — a run must reach the repair branch; (7) researcher questions under the prose ask (0 of
155 before it; if zero again, a decline); (8) Landlock's default via `looplab landlock-check`; (9) the
two caches' counts; (10) crash lead time in the bench corpus builder; (11) a BAITBENCH-shaped hack-rate
measurement of LoopLab's own Developer with the detectors on and off — the number that would make the
§3.2 "ahead" verdict a result.

### 5.3 Dependencies stated once

`DeveloperResult` before the repair offload (#7); the attribute guard before the `EvalAttempt` split
(#14 → #22); the loop offload before the cross-machine pool (#7 → #23); doc 51's skill bounds before
tiered loading (#12, in that order); the payload registry before a generated event page (#21 → #16's
last table); the CLAUDE.md diet (#13) before anything that adds prose to it.

---

## 6. Baseline record for this head

`master` at `bf860b7` (2026-09-04); plan branch `claude/prioritize-development-plan-0k77gb`.

* Doc guards and the seven formerly-red tests: green (this container, 2026-09-05 and after the
  2026-09-06 marker edits).
* `ui/`: 1,527 passed / 0 failed.
* Full Python suite (`-m "not docker"`, four `pytest-split` shards): 0 failures / 0 errors / 80 skips /
  13,059 passed. A later reader re-runs rather than trusts it.

## 7. How to work this plan

* One row is one change with one driven test; a row's close is the DELETION of the marker it names,
  never an edit to this page. When this page and the tree disagree, the tree is right and this page
  is stale — it is dated for that reason.
* No new marker without a falsifier the guard re-derives; no decline without a number. A proof names
  the fix's OWN symbol or the defect's own text — §2 is the cost of a name guessed in advance.
* A measurement precedes a policy: #8, #15 and every §5.2 row ship the instrument first.
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
