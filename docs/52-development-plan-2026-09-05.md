# 52 · Development plan (2026-09-05; revised 2026-09-06, fourth pass): what to build, improve and fix next

**Status: a PLAN over the open-item index, re-derived against `master` four times — 2026-09-05 (the
plan), 2026-09-06 morning (a second verification and an external SOTA pass), 2026-09-06 evening (a
pass over every document under `docs/`), and 2026-09-06 night (TEN independent review agents: three
re-derived every marker in the index against the code, one attacked the plan's own claims and
ranking against the tree, six checked §3 against primary sources in their subfield and proposed
falsifiable gaps).** It ranks; it ships no engine change and flips no default. Every open item it
ranks is an `OPEN[…]` marker or says why it cannot be one (§4.4).

What the fourth pass CHANGED, all recorded in §2.2: one more stale marker deleted (its fix shipped
2026-08-30 under a name the proof never used), one in-code marker converted to a decline on its own
measurement, eighteen proofs re-pointed from a guessed name to the line that decides the item, seven
of this page's own marker texts corrected where the agents showed the code does more than the text
said, a false sentence in a module docstring fixed, and thirty-four markers minted for gaps the agents
verified in the tree — every predicate evaluated against this head and mutated in a throwaway copy
with the shipped form of its fix before it was written.

Two constraints shape the ranking. **The `runs/` corpus is absent from this checkout**, so every
figure quoted from a run below is quoted from the tree's own record and not re-derived; items whose
next step is a corpus measurement are queued for the GPU box (§5.2). And the house rule that decides
order is **cost of leaving it, measured where a measurement exists**.

Companion authorities: [doc 50](50-architecture-review-2026-09-02.md), [doc 27](27-agent-system-mega-review-2026-08-09.md),
[doc 25](25-architecture-modularity-review-2026-08-01.md), [doc 34](34-review-deferred-decisions-2026-08-13.md),
[doc 10](10-autoresearch-improvement-research.md), [doc 11](11-agent-systems-research.md),
[doc 28](28-deep-research-sota-roadmap-2026-08-10.md), [doc 41](41-external-works-synergy-2026-08-14.md),
[doc 51](51-external-works-synergy-2026-09-03.md), [doc 45](45-claim-surfaces-2026-08-20.md),
[doc 36](36-agent-driven-decisions-2026-08-13.md), [BACKLOG](BACKLOG.md), [CODE_REVIEW](CODE_REVIEW.md).

---

## 1. Where the tree stands

### 1.1 The index, by home

Counted by the guard's parser after this revision's edits: **163 open markers and
22 declines**, in 25 files. (2026-09-05: 103 / 19 in 25 files; after the
third pass: 131 / 21 in 26.)

| Home | Open | What lives there |
|---|---:|---|
| `docs/52` (this page, §4) | 64 | the SOTA gaps, the doc 50 residue, and every item the docs and agent passes found open and falsifiable |
| `docs/25` modularity ledger | 30 | god-module splits, duplicated scaffolds, hand-rolled guards — judgement calls, not defects |
| `docs/BACKLOG.md` | 21 | product gaps, cadence and watchdog residue, the claim ladder |
| `docs/27` agent-system review | 14 | budgets, receipts, cancellation, prompt governance, the eval ladder |
| `docs/51` external works | 5 | skills, kNN uncertainty, perception hook, literature |
| `docs/50`, `docs/34`, `docs/CODE_REVIEW.md` | 4 + 4 + 4 | whole-tree items; the four deferred product decisions; the four surviving review rows |
| `looplab/` (in code, at the site) | 13 | the loop holds, the eval method, the ASHA mask, the payload registry, the legacy route, two caches, two measurements |
| `tests/` | 3 | three guards that state their own limit |
| `docs/29` | 1 | the F3 follow-up |

### 1.2 What the 71 commits since 2026-09-01 closed, against doc 50's proposals

| Proposal | State on 2026-09-06 | Residue — a marker where a falsifier exists (§4) |
|---|---|---|
| P2 per-child containment | **done** | — |
| P3 one run identity for readers | mostly done (`core/run_identity.py::run_ref` / `row_belongs_to_run`) | `claims_health.py::_qualify_refs` and two readers still key on `run_id` → `claim-readers-still-key-on-run-id` |
| P5 unwedge Replay | **done** | — |
| P6 six vocabularies | 4 of 6 | `event-payloads-have-no-registry`; the run-level stop vocabulary compared as a bare literal at eight sites → `run-stop-reason-compared-as-a-bare-literal` |
| P9 closed task schema | unknown keys refused at every launch layer (the task document refuses on SUBMIT and grandfathers on RELOAD by design) | no `schema` stamp on `task.snapshot.json` (untagged: no falsifier that survives a rename) |
| P10 read-side HTTP rules | one fold per request; `generation_fence` exists; `serve/http.py` holds the body-parsing half | six `HTTPException(500)` sites in `serve/routers/runs.py` and `reviews.py` → `refusal-codes-have-no-table`; two GETs still on the sequencer (§4.4) |
| P4 one untrusted-evidence boundary | 3 surfaces | `no-single-untrusted-evidence-envelope` |
| P15 small fixes | most landed | `run-setup-failure-is-not-a-refusal-type` |
| P1 loop offload | **not started** — and re-measured (§1.4) | `repair-path-holds-the-engine-loop`, `serial-node-build-holds-the-loop` |
| P7 containment made countable | not started (652 `noqa: BLE001` under `looplab/`, no linter) | `containment-is-unmeasured` |
| P8 typed engine state | not started | `engine-attributes-have-no-declaring-site-guard`, then `eval-attempt-is-one-giant-method` |
| P11 generated references | not started | `settings-doc-guard-compares-names-not-defaults`, `http-surface-has-no-generated-reference` |
| P12 CLAUDE.md byte budget | not started — the file is **238,234 bytes** (232,919 on 2026-09-02) | `claude-md-has-no-size-budget` |
| P13 UI mount harness | the PATTERN exists (`ui/test/cardKanban.test.js` mounts a real component through `vite.ssrLoadModule` + `renderToStaticMarkup`); the three giant components are outside it | `largest-ui-components-are-never-mounted` |
| P14 cross-run store hygiene | not started | untagged: a design call |

### 1.3 Suite health at this baseline

* The seven tests doc 50 found red are no longer red; the doc guards are green after every edit of
  this revision.
* `ui/`: `npm test` — **1,527 passed, 0 failed** (2026-09-05).
* Full Python suite (`-m "not docker"`, four `pytest-split` shards, 2026-09-05): **0 failures, 0
  errors, 80 skips over 13,059 passing tests**.

Sizes, re-measured by the agents on 2026-09-06 and drifting UP since doc 25's 2026-08-19 figures:
`engine/evaluate.py::_evaluate` **1,989** lines by AST (doc 25 said 1,471; the in-code marker says
1,898), `agents/roles.py` 1,779 (was 1,465), `tools/machine_runs_tools.py` 1,987, `agents/factory.py::make_roles`
222 (was 193), `ui/src/RunView.jsx` 3,148, `ui/src/AssistantBar.jsx` 4,469, `engine/orchestrator.py` 6,708.

### 1.4 The loop holds, as measured by their own markers — and one the plan had parked

* **Repair path** (`engine/evaluate.py`): ZERO loop ticks during a triage whose median is 116–276 s
  (one case 88.3 min). Not re-ranked.
* **Serial node build** (`engine/orchestrator.py`): 608.6 min over 13 builds on v11. The marker calls
  its harm a ceiling because an eval in a subprocess is not slowed by a busy loop — but the critic
  agent found the harm the marker's own file records: a dead node waited **62 minutes** for its
  terminal while both H200s idled, because the loop was inside a build. The build lane goes INTO
  the offload row (#12), not the box queue.
* **Paid cadences**: 0.3 % of a run. Ranked down by its own marker.

---

## 2. Verification passes: what they changed

### 2.0 Second pass (2026-09-06, morning)

Two markers did not describe the tree and were fixed in that change: `judge-bench-cannot-see-a-post-exit-stage-failure`
(deleted — the evidence it asked for shipped 2026-08-20 as `train_monitor.py::stage_contract_context`
under a name its proof never spelled) and `judge-bench-covers-two-judges-of-four` (converted to a
decline on its own numbers: 7 critic decisions; a novelty gate no outcome label can score).

### 2.1 Third pass (2026-09-06, evening): every document, re-derived

Every file under `docs/` was scanned for its open-status vocabulary, every hit read in context, every
candidate that names a symbol checked against the tree. Per pool — with the fourth pass's corrections
folded in where an agent showed the third pass was wrong:

| Pool | What the pass found | Disposition |
|---|---|---|
| Docs 01–05 (design, ADRs) | ADR-11's hardening targets SHIPPED where they mattered (deny-by-default egress via `--network`, cgroup/ulimit caps, allow-listed installs, the reproduction manifest, approvals as command events); two SUPERSEDED (OpenTelemetry `gen_ai` conventions by doc 08's span model — still open as a low-rank bridge item, `otel-bridge-carries-no-genai-semconv`; gateway tokens by the sandbox's secret refusal); the dollar cap tracked (`no-shared-reserve-commit-run-budget`, promoted by the infrastructure agent) | one open DECISION untagged: "parallel sidecar ordering" (doc 03 §Open) |
| Doc 06 | every partial done; LanceDB / MCP server bus / gVisor / gateway tokens are design substitutions recorded in the ADRs | nothing to tag |
| Docs 10–12 (2026-07 roadmap) | shipped: T1, T2, T4, T5, T6, T7 (`core/llm.py::_ResponseCache`), P1, P3, P4 (off by default), M2/M3, D2, D4, D5, D7 (`weighted_parent`), D8, D9, D10, D11. **Corrected by the fourth pass**: T8 is HALF shipped — `merge_mode="auto"` resolves to `ensemble` for every LLM Developer and `engine/node_build.py::_ensemble_idea` is the A0b recombination merge, but the Developer receives a directive and never the parents' code (`speculation.py` hands it `parents[0]`); D3's stall rule EXISTS (`agents/strategist.py::improves_since_best` against `stall_window`, greedy⇄broad, deep research at 2×) and only the consult's TIMING ignores it; M1's in-run half exists (`events/digest.py::lineage_lessons` + `sibling_digest`), the cross-run half does not | markers rewritten: `merge-operator-is-mean-of-params-not-code`, `strategist-consult-is-cadence-not-stagnation-triggered`, `lessons-are-not-operator-scoped`, `no-plan-artifact-with-endgame-reserve` |
| Docs 13–16 (July reviews) | closed lists; doc 16's SSE blocking read is fixed, the enum gaps closed 2026-09-02 | untagged, stated |
| Doc 17 (capability matrix) | typed Developer result tracked (`developer-output-has-no-immutable-envelope`); manifest, default-deny auth, deadline watcher, temporal CV shipped; "no first-class Evaluator" is a naming question | `no-distance-from-seed-signal` (demoted by the search agent: no measured precedent; edit-TYPE annotation is what the field measured) |
| Docs 18–24 (UI, workspace) | Approve/Ratify and DecisionFreshness shipped; doc 21's atlas claims shipped as F7; doc 20's direction → `eval-parallelism-is-in-process-only` (re-pointed) | accessibility evidence untagged (cost unmeasured) |
| Doc 22 | phases 0–3 shipped; phase 4's golden never added | `parallel-build-has-no-golden-replay`; the shipped shape is a bulk-synchronous barrier → `parallel-build-is-a-bulk-synchronous-barrier` |
| Docs 25, 27, 34 | markers stand (30 / 14 / 4); seven proofs re-pointed (§2.2) | — |
| Doc 26 | #2 → `no-hack-adjusted-score-reporting` (shape corrected: the Mislead gap); #9 → `prior-injection-hit-rate-unmeasured` (renamed to a citation rate; needs `injected-priors-leave-no-structured-record` first) | — |
| Doc 28 | NOT SHIPPED and the tree agrees | `deep-research-plan-is-not-durable`, `research-evidence-has-no-exact-span-identity` |
| Doc 29 | F1–F9 built / shipped / declined | — |
| Doc 38 | engine-side `EACCES` translation NOT built | `landlock-refusal-is-not-translated-for-triage` (order corrected: WITH the Landlock validation, before the flip) |
| Doc 41 §8 | steps 1, 3, 4, 5 tracked; step 2 closed 2026-08-15 | — |
| Docs 45, 46 | 45 tagged; **46's marker was STALE** (§2.2) | deleted |
| Doc 50 residue | EK-03, SR-05, ES2-06, ES1-06, XP-05, XP-07, XP-08, DX-03 → markers | — |
| `docs/BACKLOG.md` | §0.1 #6 declined at its row; #10 → `cross-run-trajectory-overlay-unbuilt`; §0.2's watermark → marker, its log-integrity row untagged (§4.4); §5 → `launch-readiness-gate-is-two-copies`; §6 → `stage-assert-has-no-model-free-numeric-form`, `stage-rows-are-last-wins-per-name`; two `⬜` glyphs the critic found closed (§0.5's row-uniqueness guard, §0.14's `at_creation_boundary` call sites) flipped | — |
| `docs/CODE_REVIEW.md`, ROADMAP, PROMPT_REVIEW, RESEARCH_NOTES, guide | every review row closed or tagged; the rest superseded by BACKLOG or accurate | — |
| In-code `CODEX AGENT` notes (9) | re-derived by the critic: five are live open work and now carry markers (§4.2), two are covered by existing markers (`paid-eval-has-no-attempt-scoped-receipt`, `eval-lanes-admit-without-reserving-time`), one is a resolved note, one (`replay.py`'s lexicographic concept-rename rule) is a fold-authority design question | — |

### 2.2 Fourth pass (2026-09-06, night): ten agents

**Markers.** All 131 were re-derived against the code, not the doc. **One more was stale**:
`declared-params-guard-reads-only-py` (doc 46) — the fix landed 2026-08-30 as
`core/param_carriers.py::document_numeric_paths` wired through `engine/repair_verify.py::_carrier_kind`,
and its proof `present:endswith(".py")` can never go false because the `.py` branch legitimately
stays the Python extractor. Deleted. **One converted to a decline**: `card-lane-fills-outside-the-policy-population`
was bound to a symbol the repo decided never to write (the legal-set prescription was built and
reverted on 2026-09-03: on the board `tests/test_card_driven_selection.py` uses it retains exactly ONE
card per turn for MCTS and Evolutionary, deleting the lane rather than narrowing it). **Eighteen
proofs re-pointed** from a guessed fix name, a docstring, a prompt string, a re-export, or a literal
that survives every correct fix, to the line that decides the item where one exists and otherwise to
the name the item's own text gives the fix (said in the marker, re-pointed on landing): in BACKLOG
`mcts-has-no-llm-value-estimate`, `eval-parallelism-is-in-process-only` (the in-process thread hop
in `evaluate.py`, which any dispatcher replaces, rather than one library's import),
`timeseries-adapter-embeds-its-own-forecaster`,
`concept-skeleton-matches-no-run`, `classifier-rewrites-authored-membership`,
`node-graph-cannot-name-running-experiment`, `auto-skill-promotion-run-end-only`,
`tail-truncation-drops-the-payload` (`tools/_base.py::RESULT_CAP` is a re-export); in doc 27
`prompt-bundle-unpinned-across-hot-reload`, `no-shared-reserve-commit-run-budget` (both out of the
guard's prose allow-list, which shrinks by two); in doc 25 `unconverted-stat-signature-ledger`,
`generation-conflict-envelopes-hand-built`, `eligible-cards-recomputed-in-one-election`; in doc 34
`trace-exporter-hardens-per-span-not-per-batch`, `card-trace-scans-whole-run-span-index`; in doc 51
`knn-uncertainty-dropped-by-two-of-three-callers` (the in-tree exemplar tuple-unpacks, so `res[1]`
would never appear); in doc 50 `containment-is-unmeasured` (both linter-config shapes, `.ruff.toml`
and `[tool.ruff]`) and `largest-ui-components-are-never-mounted` (the mount itself rather than a
harness filename). Twenty-odd more proofs are still bound to a guessed name; each says so in its
text and re-points on landing.

**This page's own claims, corrected by the critic and the three marker agents.** `refusal-codes-have-no-table`
named `serve/http.py`, which has zero `500` sites — the six live in `serve/routers/runs.py` (4) and
`reviews.py` (2), two of them interpolating the exception text. `systemic-stop-reason-has-no-registry`
described a hazard with no reader (the sentence is interpolated and nothing compares against it) —
replaced by `run-stop-reason-compared-as-a-bare-literal`, the eight `.lower() == "error"` sites that
ARE the unguarded vocabulary. `claim-readers-still-key-on-run-id` and `lessons-are-not-operator-scoped`
pinned files that do not hold the code (`_qualify_refs` is defined in `claims_health.py`; retrieval
lives in `lessons_priors.py`). `containment-is-unmeasured` counted 636 annotations; the tree has 652
under `looplab/`. The T8 / D3 / M1 corrections above. And `looplab/trust/reward_hack.py`'s docstring
said the detector is OFF by default while `Settings.reward_hack_detect` has been `True` since
2026-08-23 — a false sentence beside a default, fixed in this change rather than tagged.

**The plan's ranking, corrected by the critic.** (1) The real MLE-bench path grades EVERY node on the
private test set and feeds it back as the search metric, and `holdout.py::build_holdout_idx` returns
an empty partition for that kind — so "holdout-selected" was unreachable and the campaign would have
published a test-selected number (§4.1, `mlebench-search-optimises-the-private-grade`; new #3). (2)
The hidden split for `repo_task` is unenforceable today (the eval's allow-list grants `readwrite`
over the whole run dir, the read fence is blind to native readers, Landlock is off) and the lever
AIRA₂ measured is host-side CONSISTENT per-node scoring, with the never-seen final pick only
"marginal" — #6's shape changed and its size is L. (3) The eval may WRITE the run record
(`eval-may-write-the-run-record`; new #2). (4) The endgame reserve and the ensemble merge half-exist;
#12 re-scoped. (5) #16 rested on numbers already banked; the live residue is the blind
`run.out[-4000:]` the stage checker is handed (`stage-checker-is-handed-a-blind-tail`). (6) The
profile A/B is confounded by `trust_gate="gate"` and needs ≥3 seeds per arm. (7) The model arm is
inert until `operator_bandit` is on.

**§3, corrected by the six literature agents.** Every number in §3.1 re-verified against its
primary source; the corrections are in the table itself: ML-Master 2.0's ablation is leave-one-out
(L1, the in-run traces, is the 50-point tier — not L2 + L3), AIRA₂'s "linear" is throughput while
rank saturates at 4 GPUs, the survey's 26 are entries over 24 runnable systems, Kosmos's number is
from arXiv 2511.02824 (57.9 % for synthesis statements), AlphaEvolve's August bound is arXiv
2608.16884, ShinkaEvolve's circle packing is "new SOTA at 150 samples". Added: ScienceFlow (70.22 %
full-set on ≤2 GPUs), AiScientist (File-as-Bus ablation −31.82 Lite points), MARS's cost-constrained
MCTS, the leaderboard's own protocol (README is the board, ≥3 seeds, mean ± SEM, last row
2026-03-06, test-set-feedback rows flagged), and the field's verification numbers (MLReplicate 59 %,
SciIntegrity 34.2 %, artifact oracles at 60 % sensitivity / 45 % specificity, Protocol Validity's
Mislead gap). Thirty-four gaps minted from the agents' findings, each predicate verified and flipped.

---

## 3. SOTA, September 2026 — verified against primary sources

### 3.1 The field's numbers

| System | What it is | Reported result (source in §8) |
|---|---|---|
| Famou-Agent 2.0 (Baidu) | multi-agent; "evolution strategies, long-horizon memory, infrastructure" (Baidu's post; the FM-Agent repo documents 1.0 only) | **64.44 ± 1.18 %** any-medal, MLE-bench full 75, Gemini-3-Pro-Preview, 24 h (README leaderboard, 2026-02-23); Lite 80.30 ± 1.52 |
| AIBuildAI (2604.14455) | manager + designer / coder / tuner sub-agents, hierarchical | 63.11 ± 0.44 % full, Claude-Opus-4.6, 24 h (2026-03-06) |
| CAIR MARS+ / MARS (2602.02660, ICML 2026) | cost-constrained MCTS, Design–Decompose–Implement, comparative reflective memory (63 % of used lessons cross-branch) | 62.67 ± 0.77 % full (MARS+), 56.0 % (MARS) |
| MLEvolve (2606.06473) | Progressive Monte Carlo GRAPH search with cross-branch fusion and stagnation detection; a global retrospective memory (BM25 + FAISS, RRF) | 61.33 ± 1.33 % full at 12 h on the board; **65.3 ± 0.8 %** with Gemini-3.1-Pro-preview; Lite ablation: −13.64 without the memory |
| PiEvolve (Fractal) | graph-structured evolutionary search, no paper | 61.33 ± 0.77 % full; Lite 80.30 — THREE main-board systems tie at 80.30 Lite |
| ML-Master 2.0 (2601.10402) | Hierarchical Cognitive Caching: L1 traces, L2 phase-distilled knowledge, L3 cross-task priors | 56.44 % full (DeepSeek-V3.2-Speciale); Lite LEAVE-ONE-OUT, one run each: −L1 **22.7**, −L2 59.1, −L3 54.5, full 72.7 — the in-run traces are the 50-point tier |
| ScienceFlow (Huawei, 2608.14354) | research segments with checkpointed executable states; re-anchoring; evidence-aware execution controller | **70.22 ± 1.18 %** full, 3 runs, 24 h, **≤2 GPUs**, DeepSeek-V4-Flash — the best full-set number, off-board |
| AiScientist (2604.13018) | "thin control over thick state": a File-as-Bus workspace | 81.82 % Lite with BOTH Gemini-3-Flash and GLM-5; removing File-as-Bus costs **−31.82** points |
| AIRA₂ (2603.26499, v2) | steady-state async pool, static 1 worker : 1 GPU, remote execution in ephemeral containers; Hidden Consistent Evaluation; ReAct debug agents | MLE-bench-30 percentile rank **81.5 ± 3.2 at 24 h / 83.1 at 72 h** (AIRA†₂, Gemini 3.1, 8 GPUs, 3 seeds); Gemini 3.0: 1 / 4 / 8 GPUs = 56.8 / 71.2 / 71.8 at 24 h (63.5 / 76.5 / 76.0 at 72 h) — THROUGHPUT linear, rank saturates at 4; HCE alone **+13.0 / +18.4**; the "overfitting" of prior work was evaluation noise |
| Arbor (2606.11926) | long-lived coordinator, short-lived executors in git worktrees, a persistent hypothesis tree with upward insight propagation; a change merges only if it clears a held-out margin (`merge_threshold: 5.0` in the README; the paper's algorithm is a strict `>`) | **86.36 %** any-medal Lite with GPT-5.5 (seeds undisclosed); 2.5× the held-out gain of Codex / Claude Code on six tasks |
| EurekAgent (2606.13662) | environment engineering: Docker per run with a grader container and hidden `/hidden_eval`, default-deny GPU helper, hook-protected controller-owned result files, a callable time helper plus deadline injection, cost tracked but hidden; 3 parallel sessions, per-STAGE budgets | **85.71 % any-medal / 71.43 % gold** on SEVEN curated Lite tasks, one run each, GLM-5.1, one GPU |
| HASTE (2606.30911) | skills in three tiers, loaded by tier | **77.3 %** Lite, Claude Sonnet 4.6, 12 h, single seed; tiered 100 % vs flat 62.5 % = NO skills on 8 competitions, flat burning 2× output tokens; warm starts 52 % fewer iterations; its "hit rate" (42 → 85 %) is the KEEP-fraction of attempted changes; no demotion anywhere |
| Frontis-MA1 / OpenMLE (2607.28568) | a 35B model post-trained on Draft / Improve / Debug / Crossover + async search + experience priors | Lite, 12 h, ONE RTX 4090 at 12 GB: 39.39 → 60.61 → **71.21 %** |
| CobraAgent (Dalpha) | vendor page only | 79.11 % overall (Low 86.36) — unverified |
| Leaderboard protocol | the README IS the board; ≥3 seeds, mean ± SEM; last dated row 2026-03-06; "additional submissions" flagged test-set feedback: Disarray 90.91 Lite (a four-model ensemble), LoongFlow 77.27 | so "the top cluster" must be read per protocol (§3.3) |
| ShinkaEvolve (2509.19349) | novelty rejection (embedding rejection does the work; the LLM novelty judge is "marginal"), weighted parent sampling, a bandit over an LLM ensemble; mutation mix diff / full / cross = 0.45 / 0.45 / 0.1 | ICLR 2026; new circle-packing SOTA at 150 samples |
| AlphaEvolve | evolutionary coding agent with automated evaluators; evaluation cascade | 2026-08-17: ω < 2.371177 from 2.371339 (arXiv 2608.16884) |
| Model routing (LEVI 2605.09764, DEI 2605.27130, cross-tier 2608.10694) | operator × model routers, heterogeneous LLM ensembles, cheapest tier on the high-volume loop | 3.3–6.7× smaller budget for the top score (35× on one problem); +124 % QD-score at iso-budget; 96 % of search tokens on the cheapest tier at 5.6–14× lower cost |
| EvoTrace (2605.20086) | traces of four evolutionary frameworks, nine edit types, replay interventions | ~30 % of added lines are byte-identical to lines deleted earlier in the lineage, rising in 118 / 121 runs; a 24-call Bayesian sweep over one program's hyperparameters matches the run's final best on 13 / 15; two of four frameworks overfit their evaluator on ≥30 % of problems |
| Kosmos (2511.02824) | world model + parallel literature / data agents, every statement cited | 102 statements from 3 reports: 79.4 % accurate; 85.5 % data-analysis, 82.1 % literature, **57.9 % synthesis** |
| karpathy/autoresearch | edit `train.py`, 5-minute fixed budget, keep / revert on `val_bpb` | ~12 experiments / hour (2026-03) |
| Survey of AI scientists (2608.05179) | 26 entries (24 runnable systems) coded on seven dimensions | 83 % release code, **38 %** release seeds or traces, 38 % report a novelty method, 67 % disclose a result-selection policy; "no LLM-era system … demonstrates an externally validated in-loop oracle" |
| Verification numbers | MLReplicate (2605.16616); SciIntegrity-Bench (2605.10246); From Runnable to Verifiable (2608.09567); Protocol Validity / HackDetect (2607.22368); BAITBENCH (2608.30724); SpecBench (2605.21384) | 59 % of accepted automated reviews carry fabricated or unsupported claims; 34.2 % integrity-problem rate, fabrication 20.6 → 3.2 % without completion pressure; artifact-embedded oracles at 60 % sensitivity / 45 % specificity; 67 % of ML-research traces exploit an exposure, score inflation 0.45–1.00 (the Mislead gap G = S_exploit − S_intended); 57.1 % of runs reward-hack, above 50 % when told not to; the visible-vs-held-out pass gap grows 28 points per tenfold code size |

### 3.2 The techniques that recur, against what LoopLab has — verified by symbol

| Technique | Who | LoopLab today | Verdict |
|---|---|---|---|
| **Host-side CONSISTENT per-node scoring on a label-hidden split, then a final pick on a never-seen split** | AIRA₂ (+13.0 / +18.4 is the consistent-scoring half; the never-seen final pick is "marginal"), Arbor's held-out merge margin, HarnessOpt-Bench, MLE-bench's hidden grader | `engine/holdout.py` implements the MARGINAL half (a finish-time top-k re-score) and only for host-graded list-label tasks; `build_holdout_idx` returns an empty partition for `kind == "mlebench"`, whose `apply_host_grade` feeds the PRIVATE grade back to the search; `repo_task` selects on the candidate's own printed number, which is exactly what AIRA₂ replaced | **GAP #1** on both families: `mlebench-search-optimises-the-private-grade`, `repo-task-champion-is-picked-on-the-candidates-own-metric` |
| **Throughput** | AIRA₂ (static 1:1, remote execution; +14.4 points 1 → 4 GPUs, +0.6 for 4 → 8), ScienceFlow (best full-set number on ≤2 GPUs), Arbor, EurekAgent (3 sessions) | in-process task group + prefetch; two GPUs; the loop holds of §1.4; the build lane is a bulk-synchronous barrier | **GAP #2**: the loop first (#12), the barrier next, the cross-machine pool only at ≥4 workers |
| **Memory** | ML-Master 2.0 (L1 is the decisive tier), HASTE (tiered loading), MLEvolve (−13.64), SkillZip Pro (naive compression costs up to 26 points), SkillAudit / Co-Evolving / ReMe (retention keyed on a READ-side utility), "Break It Down" (task-level skills are net-negative), AIRA-dojo (the only per-operator scoping ablation, and it is null) | L1 held (spans, log, `sibling_digest`, `lineage_lessons`) and served mostly by PULL; L2/L3 present; write-side ranking only (`lesson_hygiene.py::lesson_rank_key`); no record of which priors a proposal was shown; skills flat, unbounded, never demoted | **GAP #3** re-ordered: record → bounds → utility/demotion → tiers → scoping last |
| **Search** | model routing (four iso-budget confirmations), stagnation-adaptive control (MLEvolve, FML-bench, GEAR), crossover that reads both parents (Frontis-MA1) or their traces (MGM), proxy evaluators that prioritise only (Janus), edit-type diagnostics (EvoTrace), cost-constrained MCTS (MARS) | policies over folded state, cards, graded novelty (this run's history only), ablation refine, operator bandit (OFF), `weighted_parent`, foresight + surrogate; `_ensemble_idea` merge exists but sees no parent code; the stall rule exists and does not time the consult; `proxy.py` KILLS rather than prioritises; no model arm; no cost term | **GAP #4**: re-scoped to what is genuinely absent |
| **Verification** | AAR's four measures (provenance coverage / soundness, contradiction transparency, audit effort), claims as PROV individuals with evidence bindings, MLE-bench's rule-violation + plagiarism detectors, Protocol Validity's Mislead gap, transcript judges (EvilGenie) | the RECORD is ahead (replayable log, `metric_subject`, verifier verdicts, W3C-PROV, redaction); DETECTION is shape-based and blind to all four 2026 hack classes with numbers; `/prov` exports no claim; no number on the field's own measures | **ahead on mechanisms, unmeasured on the field's measures** |
| **Budget and time** | EurekAgent's callable clock + deadline injection; Token Budgets (2606.04056: fan-out races in 11 / 63 incidents, asyncio overshoot 30 / 30 without a reservation) | the budget reaches a role ONCE as a prompt cue; no tool returns elapsed / remaining; the eval process gets no deadline; per-client `CostAccountant` limits with no reserve | not parity: three markers |
| **Grounding** | Kosmos, Mechanist, OmniScientist; RQ-Bench's "novelty mirage" for LLM-judged novelty | literature not durable; novelty gates never consult literature; no perception hook on `repo_task` | doc 51's items + two |
| **Environment engineering** | EurekAgent (controller-owned result files the agent cannot touch), Sandlock (FS + TCP + IPC + syscall policy without root, ~5 ms) | fences and receipts are ahead — but the eval's allow-list grants `readwrite` over the run dir and the subprocess tier has no syscall or egress fence | qualified: two markers |

### 3.3 What "better than SOTA" has to mean for LoopLab

1. **A number nobody at the top publishes**, in the field's own vocabulary: medal rate on the
   README's protocol (≥3 seeds, mean ± SEM), with a Mislead gap (`S_exploit − S_intended`) beside it,
   the two official detectors run, holdout selection honoured, seeds and traces released as a
   reviewer bundle. The target is stated per protocol: the main-board Lite ceiling is 80.30 (three
   systems); paper-only claims run to 86.36; test-set-feedback rows to 90.91.
2. **The same selection discipline on the box's own tasks**: consistent host-side scoring on a
   hidden split for `repo_task`, with the never-seen final pick as the secondary half.
3. **Throughput that scales with what the box has**: the loop offload, the build barrier, then a
   pool once it can reach four workers.

### 3.4 What this pass could not establish

LoopLab's own position on any external benchmark: `docs/MLEBENCH.md` records no completed run, and
the run it would record today selects on the private test set (§4.1). Every "ahead" verdict is about
mechanisms.

---

## 4. The open items, as markers

Every marker below has a falsifier evaluated against the tree on 2026-09-06 and mutated in a
throwaway copy with the shipped form of its fix (the predicate flipped in every case). Where a proof
is bound to a guessed fix name it says so; a fix landing under another name re-points it in the same
change.

### 4.1 From the SOTA pass and the six literature agents

OPEN[repo-task-champion-is-picked-on-the-candidates-own-metric] the real GPU task family
(`adapters/repo_task.py`) elects its champion on the number the candidate's own scorer prints —
exactly the self-report AIRA₂ replaced with host-side CONSISTENT scoring on a label-hidden split
(+13.0 / +18.4 percentile points), the never-seen final pick being the "marginal" half. `engine/holdout.py`
holds only that marginal half, and only for host-graded tasks; the repo task document declares no
split, `command_eval.py` has no host-side stage, and a "hidden" split is unenforceable while the eval's
allow-list grants the run dir and Landlock is off — so this is L, and the honest first slice is
CONSISTENT (host-scored, held constant across candidates) before HIDDEN. Under the survey's coding
LoopLab is L4-m today; this is the move to L4-v. proof:absent:holdout@looplab/adapters/repo_task.py

*Closed 2026-09-06 (row 3 shipped): the marker `mlebench-search-optimises-the-private-grade` stood
here. `engine/holdout.py::apply_host_grade` graded every node against the private answers and
`build_holdout_idx` returned an empty partition for the kind. Now `adapters/mlebench_split.py` carves
a `holdout_fraction` slice of the public train rows out of what the agent sees, the search is scored
on that slice with the competition's own grader (`mlebench_grade.py --answers`), and the private
answers grade the search champion once at finish (`holdout_evaluated`, `protocol: private_grade`);
`host_grading.protocol` records which protocol a run used, an uncarvable layout refuses the run, and
`tests/test_mlebench_search_split.py` pins that the search never sees a public test id and the
private grade never sees a hidden train id. Deleted per the index rule.*

OPEN[merge-operator-is-mean-of-params-not-code] `merge_mode="auto"` resolves to `ensemble` for every
LLM Developer and `engine/node_build.py::_ensemble_idea` issues a recombination directive (mean params
plus 120-char parent rationales) — but the Developer is handed ONE parent's code (`speculation.py`
passes `parents[0]`), while Frontis-MA1's Crossover applies its construction to BOTH parents and MGM
hybridises the other lineage's TRACE. The field weights crossover low (ShinkaEvolve's mix: 0.1), so
the fix is the cheap one: give `_ensemble_idea`'s Developer both parents' committed files and traces;
a host-side top-k prediction ensemble needs a predictions file and is not the repo family's row.
proof:`present:parents[0] if self._merge_mode == "ensemble" and parents else None@looplab/engine/speculation.py`

OPEN[endgame-reserve-has-no-champion-sweep] `agents/strategist.py::RuleStrategist._decide_machinery`
reserves the endgame at `node_budget_frac >= 0.8` for an ensemble only; EvoTrace measured a 24-call
Bayesian sweep over one program's exposed hyperparameters matching or exceeding the evolutionary
final-best on 13 / 15 tasks, and `prefer_sweep` / `SurrogateResearcher` exist and are never invoked
there. proof:absent:endgame_sweep@looplab/agents/strategist.py

OPEN[research-grade-profile-is-not-the-default] `PROFILES["thorough"]` holds `operator_bandit`,
`ablate_every=3`, `confirm_top_k=3` / `confirm_seeds=3`, `trust_gate="gate"`, `budget_aware`,
`complexity_cue`; the shipped default is `profile="default"` with all seven off. The decision is a
MEASUREMENT on the box, and the critic showed the naive A/B is confounded — `thorough` also flips a
SELECTION policy (`trust_gate`) and buys confirm evals — so the arms are: knobs without the gate, the
gate alone, ≥3 seeds per arm, `generalization_gap` reported. FML-bench: strategy complexity alone
does not guarantee performance; measure, do not flip. proof:`present:profile: str = "default"@looplab/core/config.py`

OPEN[embedding-novelty-gate-declined-on-one-incident] `PROFILES["thorough"]` excludes
`novelty_semantic` on one live incident (node 61) while ShinkaEvolve's ablation reports "substantial
performance gains" from embedding rejection and only "marginal" gains from the LLM judge — LoopLab's
shipped default (`novelty_mode="llm"`, `novelty_semantic=False`) is the inverse of the field's
ablation; one more arm of the profile A/B decides it. proof:missing:docs/audit/novelty-gate-ab.md

OPEN[eval-noise-floor-is-never-measured] no run records the repeated-seed spread of one candidate's
metric, so whether a champion's margin exceeds evaluation noise — AIRA₂'s explanation of the field's
"overfitting" — is undecidable; `confirm_top_k=0` / `confirm_seeds=0` by default and
`trust/gate.py::one_se_better` is wired only into confirm. The instrument is the same ≥3-seed arm the
profile A/B needs. proof:absent:eval_noise@looplab+absent:noise_floor@looplab/trust

OPEN[no-external-benchmark-number-exists] `adapters/mlebench_real.py` and `docs/MLEBENCH.md` ship the
real host-graded path and no completed run is recorded anywhere in the tree. BLOCKED behind
`mlebench-search-optimises-the-private-grade` (today's path would publish a test-selected number),
the seed protocol, the percentile-rank record and the two official detectors; the audit page carries
the survey's Table 10 columns (code + prompts, seeds / traces, result-selection policy, novelty
method, HITL entry points, harness + cost). proof:missing:docs/audit/mlebench-lite-campaign.md

OPEN[mlebench-campaign-has-no-seed-protocol] the README's submission rule is ≥3 seeds reported as
mean ± SEM; `docs/MLEBENCH.md` names no seed count and no aggregation.
proof:absent:seed@docs/MLEBENCH.md

OPEN[mlebench-grader-records-no-percentile-rank] `adapters/mlebench_grade.py` records score, medal
and `above_median`; AIRA₂ and OpenAI report MLE-bench-30 PERCENTILE RANK, so LoopLab's number could
not be read on their scale. proof:absent:percentile@looplab/adapters/mlebench_grade.py

OPEN[mlebench-path-runs-neither-official-detector] `mle-bench/extras` ships an LLM rule-violation
detector over logs + code and a Dolos plagiarism check against downloaded kernels, both run in the
paper; neither has a counterpart in the tree.
proof:absent:rule_violation@looplab/adapters/mlebench_real.py+absent:plagiarism@looplab

OPEN[no-hack-adjusted-score-reporting] the trust layer flags reward hacks, leakage and salvage on the
row and no report projects an adjusted score beside the raw one. The field has since defined the
shape: Protocol Validity's Mislead gap, `G = S_exploit − S_intended` (inflation 0.45–1.00 on ML-research
traces), reported as the pair and its gap rather than a flag-count subtraction.
proof:absent:mislead_gap@looplab

OPEN[developer-hack-rate-unmeasured] nothing has measured how often LoopLab's own Developer takes a
planted, rule-compliant shortcut. BAITBENCH released its three tasks, its two-stage transcript judge
and annotated hack transcripts, so the instrument is S, not L; EvilGenie found held-out tests add
"only minimal improvement" over a transcript judge, so the A/B is NOT detectors on / off (the shape
detectors cannot see a rule-compliant shortcut by construction); SciIntegrity's infeasible-task trap
and completion pressure are the two extra arms. Precondition of the campaign's Mislead column.
proof:missing:docs/audit/developer-hack-rate.md

OPEN[leakage-scan-has-no-multi-test-detector] `trust/leakage.py` flags fit-before-split, fit-on-test,
row overlap and temporal overlap; LeakageDetector 2.0's third class — repeated evaluation against the
same test split followed by selection — is undetected, and on `repo_task` the candidate's own scorer
IS that split. proof:absent:multi_test@looplab/trust/leakage.py

OPEN[operator-bandit-has-no-model-arm] `search/policy.py::_bandit_pick` learns WHICH OPERATOR fires
from folded yields and nothing learns WHICH MODEL generates it; four independent 2026 results say the
lever is real at iso-budget (LEVI 3.3–6.7×, DEI +124 % QD-score, cross-tier 5.6–14×, ShinkaEvolve's
bandit > single > fixed). The shape is an operator × model ROUTER with the cheap tier on the
high-volume implement loop (`agent_stage_models[role]` is the static precedent) — INERT until
`operator_bandit` is on, which the profile A/B decides. proof:absent:model_arm@looplab/search/policy.py

OPEN[mcts-selection-has-no-cost-term] MARS's cost-constrained MCTS balances expected gain against
execution expense; every policy in `search/policy.py` ranks by metric alone and `budget_aware` is a
prompt cue. proof:absent:eval_cost@looplab/search/policy.py

OPEN[edit-cycling-and-edit-type-unannotated] nothing classifies a node's committed diff by edit type
or detects lines re-introduced byte-identically after a deletion earlier in the lineage — EvoTrace:
~30 % of added lines, rising in 118 / 121 runs, with gains concentrated in three of nine edit types;
`tools/node_diff.py` already reads both nodes' committed files. Replaces the seed-distance scalar as
the field-measured diagnostic. proof:absent:reintroduc@looplab/tools/node_diff.py+absent:edit_type@looplab/tools/node_diff.py

OPEN[proxy-prediction-accuracy-unmeasured] the pre-execution judges the field ships MEASURE
themselves (Meta's research preference models 0.684 → 0.729; predict-before-execute 61.5 % pairwise;
Rehearse's judge decaying 82.8 → 56.9 % late in a loop); `search/proxy.py::ProxyScorer` (k-NN over
params) has no measured accuracy, no CLI audit, and KILLS (`proxy_skipped`) where Janus's calibrated
proxies only prioritise. proof:absent:pairwise_accuracy@looplab/search/proxy.py

OPEN[foresight-selective-accuracy-unmeasured] `search/foresight.py::foresight_scoreboard` reports the
predict-before-execute hit rate as a prompt sentence; nothing records it by position in the run,
where Rehearse measured selective accuracy falling 82.8 → 56.9 % "while the judge remains willing to
decide". Box-only. proof:missing:docs/audit/foresight-hit-rate.md

OPEN[smoke-full-rank-fidelity-unmeasured] `EvalSpec.profiles` (smoke / full) is LoopLab's evaluation
cascade rung — AlphaEvolve's cascade, OpenEvolve's `cascade_evaluation`, LEVI's rank-preserving proxy
benchmark — and nothing measures whether smoke rank order survives at full, which is what decides
whether promotion may read it. Box-only. proof:missing:docs/audit/smoke-full-rank-fidelity.md

OPEN[skills-load-flat-not-by-tier] `tools/skills.py` serves one flat dict with no global / domain /
task tier; HASTE measured flat loading at 62.5 % on 8 competitions — the same as NO skills, at 2× the
output tokens — against 100 % tiered. SkillZip Pro's warning applies to doc 51's `clip`: cut whole
sections, never bytes (an unprotected 71 % compression lost 26 accuracy points). Ranked behind the
skill-body and demotion markers: a tier of unbounded bodies is still unbounded.
proof:absent:tier@looplab/tools/skills.py

OPEN[injected-priors-leave-no-structured-record] the cross-run prior is prose spliced into both role
prompts (`engine/lessons_priors.py::_render_role_prior`), lesson rows carry no id, and no event names
which rows a proposal was shown — so no utility signal can be computed and the prior citation-rate
audit has no instrument (the `CODEX AGENT` note in `tools/cross_run_tools.py` says the same of tool
results). The fix is a fold-ignored diagnostic `EV_PRIOR_INJECTED` (role, per-row statement digests)
appended by the main task at load. proof:absent:prior_injected@looplab/events/types.py

OPEN[lesson-rank-has-no-utility-term] `engine/lesson_hygiene.py::lesson_rank_key` ranks by similarity,
confidence × evidence count and recency — all write-side — and forgetting happens only by CONTRADICTION,
never by uselessness, while SkillAudit, Co-Evolving and ReMe each key retention on a read-side
outcome. Depends on the record above. proof:absent:utility@looplab/engine/lesson_hygiene.py

OPEN[eval-process-is-not-told-its-deadline] the runtime exports `LOOPLAB_EVAL_SEED` / `LANDLOCK` /
`READ_FENCE_DIR` / `DOCKER_IMAGE` and no deadline, so a candidate cannot size its last epoch at
runtime — EurekAgent's time helper and deadline warning are what its roles use.
proof:absent:LOOPLAB_EVAL_DEADLINE@looplab/runtime/command_eval.py

OPEN[agents-cannot-read-their-own-clock] no tool returns elapsed / remaining time and the tool loop
injects no deadline warning; a Developer or repair session is tree-killed at `agent_timeout` having
been told its budget once, at proposal time. proof:absent:remaining_time@looplab/tools+absent:deadline@looplab/agents/tool_loop.py

OPEN[no-trace-to-training-data-export] Frontis-MA1 (39.39 → 60.61 % from execution-grounded SFT / RL
on operator traces) and SandMLE (+20–67 % relative) train operators from exactly the corpus
`spans.jsonl` holds; `cli/export_cmds.py` exports MLflow and a notebook only. Enabling work, ranked
late. proof:absent:sft@looplab/cli/export_cmds.py

OPEN[memo-synthesis-statements-have-no-provenance-coverage] `trust/memo_verify.py` reads only the
`claims` list; `summary` / `findings` / `directions` — the synthesis statements Kosmos measured at
57.9 % accurate — are neither bound to evidence nor counted, so AAR's first measure cannot be computed
for a LoopLab memo. Instrument first: a coverage number per memo section.
proof:absent:provenance_coverage@looplab/trust/memo_verify.py

OPEN[memo-quoted-numbers-unmatched-against-cited-metrics] the deterministic verifier declines to
match numbers (a regex cannot tell an arXiv id from a metric) and leaves numeric correctness to the
LLM verifier; MLReplicate's 59 % says fabricated numbers are what survives review, and matching a
decimal against the CITED nodes' recorded metrics needs no classifier. Measure the match rate over
the corpus first. proof:missing:docs/audit/memo-number-fidelity.md

OPEN[novelty-gates-never-consult-literature] both novelty gates and `search/graded_novelty.py` grade
against this run's history and never against retrieved literature — the form RQ-Bench measured as a
"novelty mirage"; doc 51's `retrieved-literature-is-never-durable` is the retrieval half.
proof:absent:literature@looplab/engine/novelty.py

OPEN[prov-export-carries-no-claims] `serve/routers/runs.py::prov` exports solution entities,
experiment activities and two agents, and no claim entity with its evidence binding and verifier
verdict, though `research_completed` holds both; the field's PROV-O profile (2608.18312) is
claims-as-individuals. proof:absent:verdict@looplab/serve/routers/runs.py

OPEN[failure-diagnosis-emits-one-cause-not-competing-hypotheses] the diagnostician returns one
`failure_kind` with a findings trail (`FINDINGS_CAP = 6`) and never alternative explanations with
independent severity; SAGE's multi-hypothesis attribution moved metrics-bearing outputs 42 → 92 %; the
live classifier scores 88 / 118 on `failure_triage.v1`. proof:absent:hypotheses@looplab/engine/failure_diagnosis.py

OPEN[no-research-lifecycle-benchmark-number] `adapters/` holds toy / dataset / repo / MLE-bench and no
adapter for an experiment-level benchmark (EXP-Bench 461 tasks, ResearchClawBench 40, AARRI 82,
InnovatorBench 20), so the noun LoopLab is — a research loop, not a medal agent — has no external
number even after the campaign. proof:missing:docs/audit/research-lifecycle-benchmark.md

OPEN[no-reviewer-bundle-export] seeds (`LOOPLAB_EVAL_SEED`, `confirm_seed_base`) and traces
(`spans.jsonl`, `events.jsonl`) exist on disk and nothing packages them with code and claims for a
reviewer — the survey's 38 % dimension, the field's RO-Crate export. proof:absent:RO-Crate@looplab

*Closed 2026-09-06 (row 2 shipped): the marker `eval-may-write-the-run-record` stood here. The eval
launch allow-list granted the candidate's process `readwrite` over the whole run dir, the fence's
mutation events covered only source roots, and a training script could append a well-formed
`node_evaluated` row and elect itself. Now `read_allowlist.derive` grants the run dir READ (the
workdir and `.looplab-fence/` readwrite), the fence refuses every write-flagged `open` and every
mutation event under the record outside the launch's own workdir (`LOOPLAB_EVAL_WORKDIR`, handed to
the child by `run_argv`) on EVERY task, and `tests/test_run_record_fence.py` reproduces the forged
terminal unfenced and refuses it fenced. Deleted per the index rule.*

OPEN[subprocess-tier-has-no-syscall-or-egress-fence] no seccomp filter exists anywhere in
`looplab/`, Landlock TCP rules need ABI 4 and the box measured ABI 2, so on the default tier an eval
can `connect()` anywhere and the two audit-invisible mutators `dev_probe` found (`mknod`, `mkfifo`)
have no kernel rung; Sandlock enforces FS + TCP + IPC + syscall policy without root at ~5 ms startup on
exactly this constraint set. Doc 38's "seccomp irrelevant" verdict was about path-based READ refusal
and stands. proof:absent:seccomp@looplab/runtime

OPEN[otel-bridge-carries-no-genai-semconv] the OTel bridge opens spans with LoopLab's own attribute
names and no `gen_ai.operation.name` or `gen_ai.usage.*`; the GenAI conventions are Development-status
with no release in their new repository (2026-06). Deferred until they cut one.
proof:absent:gen_ai@looplab/core/tracing.py

### 4.2 From doc 50's residue and the in-code `CODEX AGENT` notes

*Closed 2026-09-06 (row 4 shipped): the marker `claim-readers-still-key-on-run-id` stood here.
`_qualify_refs` takes the row and qualifies a uid-bearing row's refs by incarnation
(`<name>@<uid>:<node>`; a uid-less row keeps `<name>:<node>`), claim groups carry `run_refs` beside
the display `runs`, `portfolio_atlas` counts runs over refs, `run_concept_index` indexes a summary
under its uid too and `attribute_row` looks a uid-bearing row up by uid only; the run summaries
carry `run_uid`. `tests/test_run_incarnation_identity.py` drives each reader with two incarnations
of one name. Deleted per the index rule.*

*Closed 2026-09-06 (row 5 shipped): the marker `refusal-codes-have-no-table` stood here. The six
sites read `serve/http.py::REFUSALS` (503 with a `code` and a remedy, never the `OSError` text);
the one hand-raised 500 left under `serve/` is the partial-write fault in the config PUT, allow-listed
by name with its reason and stripped of the exception text; `tests/test_refusal_vocabulary.py` scans
`serve/` for a literal 500 and re-derives the emitted slugs against the table both ways. Three GET
paths came off the exclusive sequencer in the same change (the Files fence, paid-lens recovery, the
review binding), each a CAS across its read, driven by holding the lock from another thread;
`start_status` keeps it because it reconciles a dead spawn's claim. Deleted per the index rule.*

*Closed 2026-09-06 (row 6 shipped): the marker `run-setup-failure-is-not-a-refusal-type` stood here.
`engine/eval_dispatch.py` raises `EnvironmentRefusal` for a failed `run_setup`, with the fix in the
sentence; every `except RuntimeError` above it still catches it. Deleted per the index rule.*

*Closed 2026-09-06 (row 6 shipped): the marker `run-stop-reason-compared-as-a-bare-literal` stood
here. `core/models.py::RUN_STOP_ERROR` / `is_error_stop` are the word and the one comparison; the
writer and the eleven readers (eight `==`, three `!=` in `finalize.py`) go through them, and
`tests/test_run_stop_word.py` scans the tree for the literal comparison and pins the reader set
both ways. Deleted per the index rule.*

OPEN[no-single-untrusted-evidence-envelope] the untrusted-text rule reaches the assistant, the concept
tagger and the MCP cache key and not the surfaces that move engine decisions: the Strategist (whose
answer sets `eval_parallel` / `policy` / `timeout`), the crash-triage and repair-critic prompts
(verbatim stderr), the arXiv / web results. Doc 50 XP-05's fix is ONE envelope — `core/evidence.py`,
label plus guard sentence — behind a Settings flag with a legacy-snapshot default of off, because
prompt strings are contracts; the field's apply / defer / reject controller (2608.05235) is its shape.
proof:missing:looplab/core/evidence.py

OPEN[engine-attributes-have-no-declaring-site-guard] `Engine` touches 772 attribute names across 20
mixins, 91 assigned only outside `__init__`, 47 from more than one file, with 143 silent handlers in
the package to absorb the `AttributeError` a typo produces (doc 50 XP-08; the `_AshaStub` incident).
The cheap half is an AST guard that every `self._x` read in `engine/` has exactly one declaring site.
proof:missing:tests/test_engine_attribute_sites.py

OPEN[layering-rules-are-not-machine-checked] 38 % of intra-package import edges are function-local
and only a third of the stated layering rules are guarded; nothing guards `core` or `events` purity,
`engine↛serve`, `tools↛serve`, `adapters` (doc 50 XP-07). One AST guard over the package matrix with
the deferred-import allowance explicit per edge. proof:missing:tests/test_package_layering.py

OPEN[settings-doc-guard-compares-names-not-defaults] `tests/test_config_docs_sync.py` asserts every
`Settings` field has a row in `docs/guide/configuration.md` and never that the row's DEFAULT is the
field's (doc 50 DX-03); the one default compared anywhere is `inline_repair_reasons`, in a different
file. proof:absent:default@tests/test_config_docs_sync.py

OPEN[stage-checker-is-handed-a-blind-tail] `runtime/command_eval.py::_call_stage_check` is the last
judge in the engine handed a blind 4,000-character tail of the stage's stdout with no log tools — the
same slice-vs-look defect the three watchdog judges were fixed for — and BACKLOG §0.9 records it as
the still-open residue behind the 2.33 GPU-h re-train; `train_monitor.repair_log_tools` is the
provider it should be given. proof:`present:run.out[-4000:]@looplab/runtime/command_eval.py`

OPEN[sse-retransmits-the-whole-folded-state] `serve/routers/runs.py`'s state stream serializes and
retransmits the complete, growing folded state on every event (its own `CODEX AGENT` note), so a long
run's tab costs O(events × state) bytes; the fix is a delta stream keyed on the seq the client last
saw. proof:`present:f"data: {json.dumps(payload)}\n\n")@looplab/serve/routers/runs.py`

OPEN[cross-run-tools-are-a-process-wide-flag] `serve/assistant.py` mounts `CrossRunTools` on one
process-wide `cross_run_enabled` flag, not per principal (its own `CODEX AGENT` note: a multi-user
security gap on the shared hub). proof:`present:if cross_run_enabled:@looplab/serve/assistant.py`

OPEN[parallel-build-is-a-bulk-synchronous-barrier] `engine/orchestrator.py`'s parallel build is a
join over a `parallel_build_batch` task group — a barrier, not the steady-state pool AIRA₂ dispatches
into as soon as any worker is free (its own `CODEX AGENT` note; doc 22's shape correction). The
in-box half of the throughput gap, before any cross-machine pool.
proof:`present:self.tracer.span("parallel_build_batch"@looplab/engine/orchestrator.py`

OPEN[cross-run-tool-results-leave-no-invocation-record] `tools/cross_run_tools.py` renders a memory
read into the prompt and records neither the exact rendered result nor an invocation id a later
decision could be joined to (its own `CODEX AGENT` note) — the tool-result twin of the prior-record
gap, and the second precondition of the citation-rate audit.
proof:absent:invocation_id@looplab/tools/cross_run_tools.py

OPEN[write-tool-reopens-the-approved-path-by-name] `tools/write_tools.py` proves containment on a
resolved pathname before approval and then reopens the path BY NAME for the atomic replacement, so a
concurrent symlink or junction swap of an ancestor can redirect an approved write outside the allowed
root (its own `CODEX AGENT` note); descriptor-relative no-follow opens with a final identity re-check
are the fix. proof:`present:p, content, preimage_mode, preimage_state, preimage_mode)@looplab/tools/write_tools.py`

### 4.3 From the docs pass (§2.1), corrected

OPEN[lessons-are-not-operator-scoped] cross-run LESSONS are retrieved by task fingerprint and role
(`engine/lessons_priors.py::_render_role_prior`: fingerprint Jaccard ≥ 0.34, harmonic recall, top-5)
regardless of the operator about to fire; the in-run parent-plus-siblings context DOES exist
(`events/digest.py::lineage_lessons`, `sibling_digest`). The only per-operator scoping ablation in the
field (AIRA-dojo) is null, so this is LAST in the memory stack and closes as a decline if the
citation-rate audit shows no operator effect. proof:absent:operator_scoped@looplab/engine/lessons_priors.py

OPEN[no-plan-artifact-with-endgame-reserve] the endgame reserve EXISTS as a rule
(`RuleStrategist._decide_machinery`: `node_budget_frac >= 0.8` → `merge_mode: "ensemble"`,
`ablate_every: 0`, durable through `EV_STRATEGY_DECISION`); what is absent is a durable PLAN artifact
— budget allocation across phases, a reserve the DISPATCHER honours rather than a consult that may
never fire, re-planning on stagnation (doc 10 P2, doc 11 D13). proof:absent:EV_PLAN@looplab/events/types.py

*Closed 2026-09-06 (row 7 shipped): the marker `strategist-consult-is-cadence-not-stagnation-triggered`
stood here. `engine/strategy.py::_should_consult` is also due on a plateau: `engine/cadence.py::plateau_due`
over `agents/strategist.py::stall_rung` — the `(rung, started_at)` identity of the stall, read against
`strategist_stall_window` (the Strategist's own window, default `DEFAULT_STALL_WINDOW` = 3; the LLM and
agent Strategists expose their fallback rule's). Not a third pace and not self-clearing, so it fires on
the stall's identity: a decision recorded at or after `started_at` closes the rung durably, the consult's
in-process `(leader, rung)` memo (`_strategist_plateau_seen`, spent before the provider call beside
`_strategist_consulted_at`) covers the unchanged outcome, and each further whole window (the hard
stall) is a new fact that fires once more — at most one extra consult per `stall_window` stall nodes.
The coverage snapshot shares the gate and takes one extra sample per rung.
`tests/test_strategist_plateau_trigger.py` drives the truth table, the property, the money bound, both
halves of the idempotence across a resume, the window's source and the other consumer. Rehearse's
judge-decay trigger stays unbuilt — it needs the judge-quality record row 16 owns.*

OPEN[prior-injection-hit-rate-unmeasured] nothing measures whether an injected prior (lesson, skill,
capsule, claim) was CITED by the proposal that followed or changed its outcome, so memory growth is
unbounded by any utility signal; the field's only "was it used" number is dialogue QA (2603.02473).
Named a CITATION rate to avoid HASTE's keep-fraction sense of "hit rate"; needs the two record
markers above first. Box-only. proof:missing:docs/audit/prior-injection-hit-rate.md

OPEN[deep-research-plan-is-not-durable] Deep Research planning survives only inside one tool-loop
context: no `ResearchPlan`, no `ProgressLedger`, no replayable episode state (doc 28 DR-01). Four
independent 2026 works converge on it (AAR, SCION's Research Execution Plan, auditable records with
applicability boundaries, claims as individuals). One fix with doc 27's
`inner-agent-phases-not-event-sourced`. proof:absent:ResearchPlan@looplab/agents/deep_research.py+absent:ProgressLedger@looplab/agents/deep_research.py

OPEN[research-evidence-has-no-exact-span-identity] a memo's evidence is a URL plus a snippet — no
immutable `EvidenceItem` with locator, hash and provenance (doc 28 DR-02) — so a verifier verdict
cannot be re-checked later; one durable record with doc 51's `retrieved-literature-is-never-durable`.
proof:absent:EvidenceItem@looplab

OPEN[landlock-refusal-is-not-translated-for-triage] under `landlock="enforce"` a refused read arrives
as `EACCES` → `PermissionError`, the silent-skip shape the read fence's own exception type avoids, and
nothing at the repair boundary rewrites it into the fence's sentence before the triage judge reads it
(doc 38; ActPlane's "opaque errors that confuse the agent"). Lands WITH the Landlock GPU validation,
before the default moves. proof:absent:EACCES@looplab/engine/crash_repair.py+absent:EACCES@looplab/engine/failure_diagnosis.py

OPEN[parallel-build-has-no-golden-replay] doc 22's phase 4 specified a golden for a 2-wide
parallel-build run pinning id monotonicity, one terminal per node and a deterministic fold;
`tests/test_golden_replay.py` holds only the serial golden. proof:absent:parallel@tests/test_golden_replay.py

OPEN[no-distance-from-seed-signal] nothing measures how far a candidate moved from the seed program
(doc 17 §11; MLGym's "models usually improve by finding better hyperparameters" is what it would
show). Demoted: no measured precedent in the window; the edit-type marker is the field-measured
diagnostic. Guessed names — re-point on landing.
proof:absent:distance_from_seed@looplab/search+absent:seed_distance@looplab/search

OPEN[stage-assert-has-no-model-free-numeric-form] a stage's `expect.assert` is judged by an LLM
against the stage's printed tail; a declared numeric relation the ENGINE evaluates against a named key
the stage prints (CapCode's cap, Arbor's margin are the shape) is not built — `STAGE_EXPECT_KEYS` is
the closed pair `("files", "assert")`. proof:`line:STAGE_EXPECT_KEYS&&"assert")@looplab/runtime/command_eval.py`

OPEN[stage-rows-are-last-wins-per-name] `replay.py::_on_stage_finished` keeps one row per stage NAME,
so after an inline repair the attempt that spent the training wall-clock leaves no row (BACKLOG §6
D5). Per-attempt rows change the FOLD output for every repaired node, so this lands after the
corpus-digest baseline the `EvalAttempt` split takes. proof:`present:n.stages[i] = rec@looplab/events/replay.py`

OPEN[readmodel-watermark-ignores-event-data] `readmodel.py::coverage_watermark` digests the ordered
`(seq, type)` prefix and nothing about event DATA, so a log whose `node_evaluated.metric` was edited in
place still certifies `current` (BACKLOG §0.2, driven).
proof:`present:rows = [[int(getattr(e, "seq", -1)), str(getattr(e, "type", ""))] for e in events]@looplab/events/readmodel.py`

OPEN[launch-readiness-gate-is-two-copies] one rule of "is this task launchable" is spelled twice —
`adapters/repo_task.py::EvalSpec._command_or_stages` and `serve/tui_format.py::spec_ready`, whose
docstring says it mirrors the backend and whose other checks are a superset — and there is no
`/api/validate` (BACKLOG §5). proof:`present:def spec_ready@looplab/serve/tui_format.py`

OPEN[cross-run-trajectory-overlay-unbuilt] the run-comparison screen ranks runs of one task and cannot
overlay their metric TRAJECTORIES, because the run-list payload carries `nodes` as a count;
`ui/src/crossRunRank.js` names the gap in its own constant (BACKLOG §0.1 #10).
proof:`present:export const TRAJECTORY_GAP@ui/src/crossRunRank.js`

### 4.4 Verified open, and NOT tagged — with the reason

* ~~Two GETs still take the exclusive command sequencer~~ — re-derived by AST on 2026-09-06 while
  shipping #5: the GET paths that HELD it were the Files fence (`_assert_artifact_generation`), paid-lens
  recovery and every review read (`_bound_run`), not `/log-page`; all three are CAS-across-the-read now,
  and `tests/test_refusal_vocabulary.py` pins that `start_status` is the one GET body left on the lock.
* **The log-integrity receipt counts lines as records** (a batch envelope is one line of up to 4,096
  events): no single line decides it.
* **"Parallel sidecar ordering"** (doc 03), **"no first-class Evaluator"** (doc 17), the
  concept-rename fold's lexicographic rule (`replay.py`): design questions.
* **Browser-level accessibility evidence** (doc 18): cost unmeasured.
* **Two idiom duplications**: cross-referenced residue.
* **Not re-derived this pass**: BACKLOG §0.6b #1 (`eval.inputs` declared by no task), §0.11 (Reattach
  spawns a driver), §0.8 #1; `docs/11` still quotes AIRA₂'s v1 numbers (a dated doc, historically
  accurate).

### 4.5 Deliberately NOT on the plan

The paid cadences (0.3 %); the card-lane legal set (now a decline); claim undercut (trigger unfired);
the semantic belief key (unvalidated); the doc 25 ledger as a programme; islands / migration
(ShinkaEvolve ran migration 0.0 at budgets like ours); harness self-evolution (gains localised in the
reflection slot on frozen small backbones — if ever, evolve `failure_reflection`-class text only);
the 22 declines.

---

## 5. The final ranked plan

One ordered list. Rank is cost of leaving it, with §3's leverage folded in and the critic's
dependency corrections applied. "S/M/L" is effort in this tree's terms. **Every row names the
marker(s) it retires**, so the list re-derives from `grep -rn 'OPEN\['`.

### 5.1 Do in this order

| # | Item | Why here | Size | Retires |
|---|---|---|---|---|
| 1 | Pin `require_approval` into `RUN_START_PINNED_FIELDS`, with a resume test | invariant #6 does not cover the one setting that gates a paid finish | S | `require-approval-not-pinned-at-run-start` |
| 2 | **The eval may not write the run record**: run dir read-only to the eval, writes on the workdir and the fence directory only, a mutation-event refusal as the message rung; a test that a forged `node_evaluated` row from a stage cannot land | a candidate can elect itself today; the store's foreign-writer stop sees only malformed rows | M | `eval-may-write-the-run-record` |
| 3 | **Stop hill-climbing the private grade on the real MLE-bench path**: a search-time host score on an agent-invisible split from the public train data, one private grade at finish, the partition no longer empty for that kind | the campaign would otherwise publish a test-selected number | M | `mlebench-search-optimises-the-private-grade` |
| 4 | The three claim readers through `run_ref` / `row_belongs_to_run` with the two-incarnation fixture | one-sided verdicts demoted whenever two runs share a name | S | `claim-readers-still-key-on-run-id` |
| 5 | A refusal-code table with a guard, and the last two GETs off the sequencer | six `500` sites, two leaking a host path; a read behind a cross-process `flock` | S | `refusal-codes-have-no-table` |
| 6 | `run_setup` as an `EnvironmentRefusal`; the run-level stop word into a registry with a two-way scan over its eight sites | a refusal reads as a crash; a bare literal decides "finished cleanly" | S | `run-setup-failure-is-not-a-refusal-type`, `run-stop-reason-compared-as-a-bare-literal` |
| 7 | **The Strategist consult due on plateau**: `_should_consult` gains `or improves_since_best >= stall_window` | the reading exists; one condition; FML-bench's adaptive agent beat all six baselines | S | `strategist-consult-is-cadence-not-stagnation-triggered` |
| 8 | One launch-readiness gate behind `/api/validate` | two copies, one pointing at the backlog | S | `launch-readiness-gate-is-two-copies` |
| 9 | **The stage checker gets the log tools** the three watchdog judges already have | the last blind 4,000-char judge; the re-train BACKLOG §0.9 recorded | S–M | `stage-checker-is-handed-a-blind-tail` |
| 10 | **Consistent host-side scoring for `repo_task`**, in two slices: (a) a host-side score stage held constant across candidates, `generalization_gap` folded for repo runs; (b) the split made HIDDEN once #2 and the Landlock validation hold, selection through `holdout_select`; replay-digest proof that undeclared runs are byte-identical | the field's largest measured selection effect, open on the box's own runs; L4-m → L4-v | L | `repo-task-champion-is-picked-on-the-candidates-own-metric` |
| 11 | **The profile A/B, properly designed** on the box: knobs without the gate / the gate alone / the embedding-novelty arm, ≥3 seeds per arm, `generalization_gap` and the noise floor reported | the built quality machinery ships off, undecided; the arms decide three markers at once | S code, box time | `research-grade-profile-is-not-the-default`, `embedding-novelty-gate-declined-on-one-incident`, `eval-noise-floor-is-never-measured` |
| 12 | **A `DeveloperResult` envelope, then the repair path AND the serial build lane off the loop** (one helper on the proposal pool, capture-sink discipline, a loop-liveness test per site) | zero ticks during a 116–276 s median hold; a dead node waited 62 min for its terminal while both GPUs idled | M | `developer-output-has-no-immutable-envelope`, `repair-path-holds-the-engine-loop`, `serial-node-build-holds-the-loop` |
| 13 | **One untrusted-evidence envelope** (`core/evidence.py`) behind a flag, on the Strategist, triage / critic stderr, arXiv / web | model-authored text reaches decision-moving surfaces unlabelled | M | `no-single-untrusted-evidence-envelope` |
| 14 | **Containment made countable**: ruff `BLE001` as a census, the 652 `noqa`s as an allow-list, `contain(span, reason)`, the paid-call `BudgetExceeded` funnel | 460 silent handlers; a swallowed budget stop at a selection site | M | `containment-is-unmeasured` |
| 15 | **Budget and time as facts the agents can read**: a reserve-commit run budget at `llm_broker.borrow()` calibrated from the `llm_usage` ledger; a clock tool + deadline warning in the tool loop; `LOOPLAB_EVAL_DEADLINE` for the eval process | Token Budgets: asyncio fan-out overshoots 30 / 30 without a reservation; EurekAgent's roles read their clock | M | `no-shared-reserve-commit-run-budget`, `agents-cannot-read-their-own-clock`, `eval-process-is-not-told-its-deadline` |
| 16 | **The durable research record** as one slice: a `ResearchPlan` / `ProgressLedger` the fold applies, an exact-span `EvidenceItem` ledger, retrieved literature as a registered event, inner agent phases event-sourced | four 2026 works converge on it; every memo-quality number below needs it | M–L | `deep-research-plan-is-not-durable`, `research-evidence-has-no-exact-span-identity`, `retrieved-literature-is-never-durable`, `inner-agent-phases-not-event-sourced` |
| 17 | **The memory stack, in the measured order**: doc 51's skill body bound (whole sections) and demotable status, the kNN uncertainty spent, the repo-task perception hook; the prior and tool-result RECORDS; then the citation-rate audit on the box; then a read-side utility term and forgetting; then tiered loading; operator scoping last or declined | L1 is the decisive tier and is held; flat loading equals no skills; task-level skills are net-negative; the only scoping ablation is null | S × 4, S × 2, box, M, M | `skill-body-served-whole-and-unbounded`, `skill-status-never-demoted-on-later-evidence`, `knn-uncertainty-dropped-by-two-of-three-callers`, `repo-task-exposes-no-perception-hook`, `injected-priors-leave-no-structured-record`, `cross-run-tool-results-leave-no-invocation-record`, `prior-injection-hit-rate-unmeasured`, `lesson-rank-has-no-utility-term`, `skills-load-flat-not-by-tier`, `lessons-are-not-operator-scoped` |
| 18 | **The endgame, re-scoped**: both parents' code and traces to `_ensemble_idea`'s Developer; a durable `EV_PLAN` whose reserve the dispatcher honours; a champion sweep in the reserve | the merge sees one parent; the reserve is a consult that may never fire; EvoTrace's 13 / 15 | M | `merge-operator-is-mean-of-params-not-code`, `no-plan-artifact-with-endgame-reserve`, `endgame-reserve-has-no-champion-sweep` |
| 19 | **An operator × model router** on the bandit's yield table, cheap tier on the implement loop — after #11 turns the bandit on | four iso-budget confirmations; inert until `operator_bandit` is on | M | `operator-bandit-has-no-model-arm` |
| 20 | **CLAUDE.md on a byte budget** | 238 KB and growing; every agent turn pays it | M | `claude-md-has-no-size-budget` |
| 21 | **The engine attribute guard**, then the `EvalAttempt` split along `_evaluate`'s phase comments, with a corpus-digest baseline taken FIRST | 772 attributes, 91 lazily minted; 1,989 lines | S, then L | `engine-attributes-have-no-declaring-site-guard`, `eval-attempt-is-one-giant-method`, doc 25's `evaluate-prestart-and-terminal-blocks-inline` |
| 22 | **Hack-adjusted reporting and its instruments**: the Mislead-gap pair on the run summary; the BAITBENCH tasks + judge run against LoopLab's own Developer on the box; the two official MLE-bench detectors; a multi-test leakage rung | the "ahead on verification" verdict becomes a number; the campaign's adjusted column needs the judge | S + box + M + M | `no-hack-adjusted-score-reporting`, `developer-hack-rate-unmeasured`, `mlebench-path-runs-neither-official-detector`, `leakage-scan-has-no-multi-test-detector` |
| 23 | **The MLE-bench Lite campaign** on the box, after #3 and #22: ≥3 seeds mean ± SEM, percentile rank recorded, raw and Mislead-adjusted numbers, a reviewer bundle of seeds + traces + code + claims, the survey's Table 10 columns | the external proof; the noun this repo's adjectives have never produced | S code, L wall-clock | `no-external-benchmark-number-exists`, `mlebench-campaign-has-no-seed-protocol`, `mlebench-grader-records-no-percentile-rank`, `no-reviewer-bundle-export` |
| 24 | **A model-free numeric `assert`** for stage contracts (a declared relation the engine evaluates; CapCode's cap, Arbor's margin) | the LLM-judged form is only as good as what the stage prints | M | `stage-assert-has-no-model-free-numeric-form` |
| 25 | **Guards that compare, not grep**: settings defaults against `Settings`; an API reference from `app.openapi()`; the CLI reference from Typer | a new route lands green and undocumented | M | `settings-doc-guard-compares-names-not-defaults`, `http-surface-has-no-generated-reference` |
| 26 | **Mount the three giant components** through the `cardKanban.test.js` pattern, one gate-flip test each, the harness extracted; the trajectory overlay once the run row carries a series | the pattern exists; the components are outside it | S–M | `largest-ui-components-are-never-mounted`, `cross-run-trajectory-overlay-unbuilt` |
| 27 | **Verification of the seams**: a 2-wide parallel golden; a layering guard; per-attempt stage rows (after #21's baseline); a watermark that hashes data; the write tool's descriptor-relative reopen | each one test or one line, each a hole a review found | S × 5 | `parallel-build-has-no-golden-replay`, `layering-rules-are-not-machine-checked`, `stage-rows-are-last-wins-per-name`, `readmodel-watermark-ignores-event-data`, `write-tool-reopens-the-approved-path-by-name` |
| 28 | **The kernel rungs, together**: the Landlock GPU validation on the box, the `EACCES` translation at the repair boundary landing WITH it, then the default flip; a seccomp / egress fence for the subprocess tier on Sandlock's shape | the refusal must not read as a missing file to the judge; the default tier can `connect()` anywhere | box + S + M | `landlock-is-opt-in-by-default`, `landlock-refusal-is-not-translated-for-triage`, `subprocess-tier-has-no-syscall-or-egress-fence` |
| 29 | **Retire the legacy `/control` route**; the per-POST rescan shrinks with it; the SSE state stream as deltas; the cross-run flag per principal | a lost-response retry re-appends paid intents; O(events × state) bytes per tab; a shared-hub gap | M | `legacy-control-route-is-not-retired`, `eventstore-rescans-the-log-per-control-post`, `sse-retransmits-the-whole-folded-state`, `cross-run-tools-are-a-process-wide-flag` |
| 30 | **The event payload contract**, the PROV export carrying claims with verdicts, the GenAI semconv bridge when the spec ships | invariant #5 unverifiable; `/prov` exports no claim | M, S, deferred | `event-payloads-have-no-registry`, `prov-export-carries-no-claims`, `otel-bridge-carries-no-genai-semconv` |
| 31 | **Search diagnostics**: edit-type and re-introduction annotation over `node_diff.py`; a cost term in MCTS; the proxy's pairwise accuracy, foresight's selective accuracy and smoke→full rank fidelity measured on the box; the seed-distance scalar when a run pays for it | the field measures its judges; LoopLab's kill and prioritise on unmeasured ones | S, M, box × 3, S | `edit-cycling-and-edit-type-unannotated`, `mcts-selection-has-no-cost-term`, `proxy-prediction-accuracy-unmeasured`, `foresight-selective-accuracy-unmeasured`, `smoke-full-rank-fidelity-unmeasured`, `no-distance-from-seed-signal` |
| 32 | **The memo's own measures**: provenance coverage per section; a number-fidelity audit against cited metrics; literature in the novelty gates (after #16); competing hypotheses in failure diagnosis | 57.9 % synthesis accuracy is the field's number for the unchecked fields; 59 % fabricated among accepted | S, S, M, M | `memo-synthesis-statements-have-no-provenance-coverage`, `memo-quoted-numbers-unmatched-against-cited-metrics`, `novelty-gates-never-consult-literature`, `failure-diagnosis-emits-one-cause-not-competing-hypotheses` |
| 33 | **Throughput beyond the box and the noun's own benchmark**: the build barrier into a steady-state lane; a cross-machine pool with static per-worker GPU pinning and remote execution once it can reach four workers; the trace export for operator training; an experiment-level benchmark adapter | rank saturates at 4 GPUs; the best full-set number ran on ≤2; Frontis trained on exactly this corpus | M, L, S, box | `parallel-build-is-a-bulk-synchronous-barrier`, `eval-parallelism-is-in-process-only`, `no-trace-to-training-data-export`, `no-research-lifecycle-benchmark-number` |
| 34 | The remaining product rows and ledgers when their trigger fires or the file is open: MLflow autolog, Pareto in selection under `select`, a drift detector, the MCTS value estimate, the FE operator, a forecasting backend; CODE_REVIEW's Windows job object and categorical leakage; doc 29's F3 byte total; the doc 25 / 27 / 34 ledgers | real gaps with driven falsifiers, lower leverage | — | the six BACKLOG product markers, `windows-tree-kill-is-not-atomic`, `target-leakage-misses-non-monotone-and-categorical`, `f3-workspace-byte-total`, the 48 ledger markers |

### 5.2 The box-only queue (needs `runs/` or a GPU)

In the order they pay: (1) #11, the profile A/B with its three arms; (2) the hack-rate audit
(`developer-hack-rate-unmeasured`) — before the campaign; (3) the serial-build harm report beside #12
(`serial-node-build-holds-the-loop`); (4) #23, the campaign; (5) the prior citation-rate audit
(`prior-injection-hit-rate-unmeasured`); (6) the first-propose split
(`first-propose-runs-with-every-gpu-idle`); (7) ASHA's promotion mask — the 2.08 starved hours are
already in hand, the soundness question is not (`asha-promotion-mask-blocks-all-production`); (8)
`TrainingVerdict.fault`'s outcome label (`monitor-fault-has-no-outcome-label`); (9) researcher
questions (`researcher-questions-not-appended`); (10) the Landlock validation with the `EACCES`
translation (#28); (11) the two caches' counts; (12) crash lead time
(`crash-predictability-unmeasured`); (13) smoke→full fidelity, foresight and proxy accuracy (#31); (14)
the research-lifecycle benchmark (#33).

### 5.3 Dependencies stated once

#2 before #10(b) (a hidden split needs a run dir the eval cannot write); #3 and #22 before #23; the
`DeveloperResult` envelope before the offload (#12); the attribute guard and a digest baseline before
the `EvalAttempt` split and the per-attempt stage rows (#21 → #27); #11 before #19 (the bandit must
be on); the two record markers before the citation-rate audit (#17); #16 before literature in the
novelty gates (#32); the Landlock validation before the flip, the translation WITH the validation
(#28); the CLAUDE.md diet (#20) before anything that adds prose to it.

---

## 6. Baseline record for this head

`master` at `bf860b7` (2026-09-04); plan branch `claude/prioritize-development-plan-0k77gb`.

* Doc guards green after every marker edit of this revision; `ui/` 1,527 / 0; the full Python
  suite 0 failures / 0 errors / 80 skips / 13,059 passed (2026-09-05). A later reader re-runs.

## 7. How to work this plan

* One row is one change with one driven test; a row's close is the DELETION of the marker it names.
  When this page and the tree disagree, the tree is right.
* No new marker without a falsifier the guard re-derives AND that flipped under mutation; no decline
  without a number. A proof names the fix's OWN symbol or the defect's own line — this revision
  re-pointed eighteen that did not.
* A measurement precedes a policy: #11, #22, #23 and every §5.2 row ship the instrument first.
* The trust line is not negotiable: an advisory rung may re-rank, refuse or annotate; it may not
  mint a metric, a champion, a violation or a selection. #10's host-side score is admissible because
  the engine computes it; a candidate-printed number may only ever REFUSE.

## 8. Sources for §3

Primary sources, fetched 2026-09-06 by the six literature agents; the numbers are theirs.

* MLE-bench README leaderboard and extras — https://github.com/openai/mle-bench ; https://github.com/openai/mle-bench/blob/main/extras/README.md ; https://arxiv.org/abs/2410.07095 ; rendition https://www.mlebench.com/
* Famou-Agent 2.0 — https://x.com/Baidu_Inc/status/2042856173171347534 ; https://github.com/baidubce/FM-Agent (1.0)
* AIBuildAI — https://arxiv.org/abs/2604.14455 · MARS — https://arxiv.org/abs/2602.02660 · PiEvolve — https://fractal.ai/ai-research/pievolve
* MLEvolve — https://arxiv.org/abs/2606.06473 ; https://github.com/InternScience/MLEvolve
* ML-Master 2.0 — https://arxiv.org/abs/2601.10402 · ML-Master 1.0 — https://arxiv.org/abs/2506.16499
* ScienceFlow — https://arxiv.org/abs/2608.14354 · AiScientist — https://arxiv.org/abs/2604.13018
* AIRA₂ — https://arxiv.org/abs/2603.26499 · AIRA-dojo — https://arxiv.org/abs/2507.02554 ; https://github.com/facebookresearch/aira-dojo
* Arbor — https://arxiv.org/abs/2606.11926 ; https://github.com/RUC-NLPIR/Arbor
* EurekAgent — https://arxiv.org/abs/2606.13662 ; https://github.com/THU-Team-Eureka/EurekAgent
* HASTE — https://arxiv.org/abs/2606.30911 · Frontis-MA1 / OpenRSI — https://arxiv.org/abs/2607.28568 ; https://github.com/FrontisAI/OpenRSI
* CobraAgent (vendor) — https://dalphakr.github.io/CobraAgent/ · KompeteAI — https://arxiv.org/abs/2508.10177 · FML-bench — https://arxiv.org/abs/2605.17373 · GEAR — https://arxiv.org/abs/2605.13874 · SwarmResearch — https://arxiv.org/abs/2607.02807 · Rehearse — https://arxiv.org/abs/2607.27687 · Research preference models — https://arxiv.org/abs/2608.13940 · SandMLE — https://arxiv.org/abs/2604.04872 · Break It Down — https://arxiv.org/abs/2608.20274 · CBR R&D-Agent — https://arxiv.org/abs/2606.05250
* ShinkaEvolve — https://arxiv.org/abs/2509.19349 ; https://github.com/SakanaAI/ShinkaEvolve · AlphaEvolve ω bound — https://arxiv.org/abs/2608.16884 · EvoTrace — https://arxiv.org/abs/2605.20086 · LEVI — https://arxiv.org/abs/2605.09764 · DEI — https://arxiv.org/abs/2605.27130 · cross-tier — https://arxiv.org/abs/2608.10694 · Janus — https://arxiv.org/abs/2608.08189 · Mendel Gödel Machine — https://arxiv.org/abs/2608.07645 · OpenEvolve — https://github.com/codelion/openevolve · karpathy/autoresearch — https://github.com/karpathy/autoresearch
* SkillZip / ReZip — https://arxiv.org/abs/2608.05604 ; https://arxiv.org/abs/2608.11079 ; SkillZip Pro — https://arxiv.org/abs/2608.30785 · SkillAudit — https://arxiv.org/abs/2606.14239 · Co-Evolving skills — https://arxiv.org/abs/2606.08755 · AFTER — https://arxiv.org/abs/2606.23127 · Retrieval vs utilization — https://arxiv.org/abs/2603.02473 · EvoMem — https://arxiv.org/abs/2608.10795
* Survey of AI scientists — https://arxiv.org/abs/2608.05179 · Kosmos — https://arxiv.org/abs/2511.02824 · AAR — https://arxiv.org/abs/2602.13855 · Claims as individuals — https://arxiv.org/abs/2608.18312 · Trajectories to evidence — https://arxiv.org/abs/2608.05235 · Runnable to verifiable — https://arxiv.org/abs/2608.09567 · SAGE / MHFA — https://arxiv.org/abs/2606.31478 · RQ-Bench — https://arxiv.org/abs/2606.12071 · MLReplicate — https://arxiv.org/abs/2605.16616 · SciIntegrity-Bench — https://arxiv.org/abs/2605.10246 · SCION — https://arxiv.org/abs/2607.03863 · AARRI-Bench — https://arxiv.org/abs/2606.07462 · ResearchClawBench — https://arxiv.org/abs/2606.07591 · EXP-Bench — https://arxiv.org/abs/2505.24785 · AI Scientist-v2 — https://arxiv.org/abs/2504.08066
* Protocol Validity / HackDetect — https://arxiv.org/abs/2607.22368 · BAITBENCH — https://arxiv.org/abs/2608.30724 · SpecBench — https://arxiv.org/abs/2605.21384 · Reward Hacking Benchmark — https://arxiv.org/abs/2605.02964 · EvilGenie — https://arxiv.org/abs/2511.21654 · CapCode — https://arxiv.org/abs/2606.07379 · HarnessOpt-Bench — https://arxiv.org/abs/2608.06301 · What Fits Doesn't Overfit — https://arxiv.org/abs/2606.11045 · GRACE-DS — https://arxiv.org/abs/2606.16000 · LeakageDetector 2.0 — https://arxiv.org/abs/2509.15971
* Sandlock — https://arxiv.org/abs/2605.26298 · ActPlane — https://arxiv.org/abs/2606.25189 · Token Budgets — https://arxiv.org/abs/2606.04056 · OTel GenAI conventions — https://github.com/open-telemetry/semantic-conventions-genai · Landlock ABI — https://docs.kernel.org/userspace-api/landlock.html
