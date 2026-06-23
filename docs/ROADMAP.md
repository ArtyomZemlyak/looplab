# LoopLab — Strategic Roadmap & Feature Development

**Date:** 2026-06-23 · **Updated:** 2026-06-24 (hardened Pass 1 + new research-layer pass) ·
**Author:** deep-research-backed synthesis · **Horizon:** next 3 phases.

> **Method & honesty note.** This roadmap is grounded in three inputs: (1) a precise read of
> LoopLab's *current* capabilities (code + build-status), (2) a full-repo code review
> ([CODE_REVIEW.md](CODE_REVIEW.md)), and (3) **seven deep web-research passes** (AI-scientist
> frontier ×2, search/allocation, coding agents, reward-hacking/eval-integrity, observability/UX,
> research/scientist layer) — see [RESEARCH_NOTES.md](RESEARCH_NOTES.md). The **2026-06-24 re-run
> hardened the previously rate-limited passes**: the AI-ML-engineering-frontier pass now carries
> 7 adversarially-verified (3-0 / 2-1) claims, and MLE-STAR's mechanism was confirmed by direct
> source fetch. The verification layer was *again* partially throttled on secondary claims, so a few
> in-paper facts abstained (flagged in RESEARCH_NOTES). Treat verified (✅) claims as solid and the
> rest as literature-sourced leads.

---

## 0. TL;DR — the three bets

LoopLab has already built the hard part: a correct, event-sourced, resumable search loop with
pluggable roles, trust-tiered sandboxing, five task kinds (toy / regression / code-regression /
mlebench / repo P1–P4), three policies (greedy / evolutionary / MCTS), multi-objective constraints,
drift cross-check, OTel tracing, and a rich live React UI (projects, time-travel, LLM trace,
semantic-zoom DAG, manual node injection, per-experiment chat, Pareto/sensitivity/compare/registry).
The offline-buildable plan is essentially *done*.

The frontier from here is **not more breadth — it's depth in three areas that separate a working
loop from a state-of-the-art autonomous ML researcher**:

1. **Search intelligence** — graduate from tree-search + random/hill-climb to **multi-fidelity
   racing (ASHA/Hyperband) + surrogate-guided proposal (BO/TPE)** so limited compute finds better
   solutions faster. *LoopLab already has the two primitives this needs (eval profiles = fidelities;
   a metric history = surrogate training data) — they're just not wired into a principled allocator.*
2. **Trust you can actually rely on** — close the **metric-integrity and secret-leakage gaps** the
   review found (host-side scoring, out-of-process grading, sandbox hardening), turning "honest
   caveat" into "enforced guarantee." This is the prerequisite for running *untrusted* tasks (real
   Kaggle/MLE-bench), which is the prerequisite for credible benchmarks.
3. **A reliable coding Developer** — adopt the SWE-bench-proven reliability stack (localization →
   best-of-N candidates → test-driven repair → independent verification) so the Developer role stops
   being the flaky link with small local models.

Everything else (benchmarks, multi-agent ideation, richer UX, scale, meta-learning) compounds on
these three.

---

## 0.5 — 2026 research update: two findings that reprioritize the plan

The hardened research (RESEARCH_NOTES Pass 1 re-run, verified 3-0) surfaces two results that **change
the order of the bets above** and add one new top-priority feature:

**① "Operators, not search, are the bottleneck" (arXiv:2507.02554, Meta/FAIR, vote 3-0).** When an
agent is limited to AIDE's operator set (draft/debug/improve), **MCTS and evolutionary search give
no gain over greedy** — sweeping the exploration constant barely moves the needle. Advanced search
only pays off once the *operators* are richer (the "AIRA" set lifts MLE-bench-Lite 39.6%→47.7%).
→ **Implication for LoopLab:** it already has the search variety (greedy/evolutionary/MCTS) and
should resist over-investing there. The higher-leverage work is **operator quality** — promote
Theme A's new **A0 (richer operators)** *above* the search-allocation items (A1–A4), which now read
as "compounds *after* A0." Concretely, adopt **MLE-STAR's ablation-of-code-blocks targeted
refinement** (LoopLab already ablates *params* — extend `_ablate` to *code blocks*; verified via
arXiv:2506.15692, 64% MLE-bench-Lite), a real solution-**merge** that composes top solutions (not
just mean-params), and a **memory operator** that reuses prior winning patterns.

**② Overfitting-on-validation is the #1 *unsolved* problem (arXiv:2507.02554 §5.3, vote 3-0).**
Across agents, the validation metric keeps improving while the true **test** score plateaus or
**declines** (val-test gap 15–16.6%); searching on test would gain 9.4–12.4%. Today LoopLab selects
`best` purely by the validation/CV metric — i.e., it is *structurally* exposed to this exact failure.
→ **New top feature: B6 — a held-out test split the search never sees + a generalization-gap guard**
(report val-vs-test, penalize/flag high-gap "winners"). This is both a *trust* feature and a
*search-quality* feature, and few systems solve it — a real differentiator.

**③ Two cost-reduction levers beat fancier search (KompeteAI 6.9×, ArchPilot proxy-MCTS, both 3-0).**
The top MLE-bench systems win by **evaluating more candidates per unit compute** — a *predictive
scoring model* that ranks a solution's potential from **early-stage signals** (first-epoch val,
partial data) to skip doomed full runs. → **A6 (proxy/predictive scoring)** joins A1 (multi-fidelity)
as the two ways to make best-of-N and broad search affordable. *(KompeteAI 51.5% is the current
MLE-bench-Lite leader; numbers in §2.)*

**④ Caution on LLM-judge ranking (Pass 6).** An LLM-as-judge is **≈random (~50%) at ranking** top vs
bottom research ideas. → Theme E must rank candidate ideas by **empirical cheap evals (A1) or a
surrogate**, using the LLM only as a weak prior + for diversity — *not* as the selection oracle.

---

## 1. Where LoopLab stands (honest baseline)

**Genuinely strong / done — do NOT re-propose:**
- Event-sourced loop, crash-resume by replay, files-as-truth, read/control process split (ADR-18).
- Roles behind Protocols; in-house LLM Developer + external coding-agent backend (OpenCode/aider/…)
  in a git worktree with a patch-gate edit surface and an output validator with retry/fallback.
- Trust tiers (trusted_local / untrusted-Docker), drift cross-check (`ratify_freeze_drift`),
  multi-objective constraints + feasibility gating, diversity archive, multi-seed confirmation gate.
- Policies: greedy tree, evolutionary, MCTS (UCB1); ablation/sensitivity operator.
- **Eval profiles (`smoke`/`full`)** — a *latent multi-fidelity primitive* already in the action space.
- Cross-run memory (case library + vector store + agentic retrieval), skills/prompts hot-reload.
- Tracing (custom JSONL + real OTel bridge), HTML + React UI, projects, time-travel scrubber,
  per-experiment chat + suggest-idea, manual node injection, reopen-finished-run, Pareto/sensitivity/
  compare/registry/meta-graph/report panels, parallel-coordinates + scatter charts, run launcher.

**Foundation gaps to fix first (from [CODE_REVIEW.md](CODE_REVIEW.md)) — these gate the roadmap:**
- **Metric integrity** not enforced (self-reported metric, writable eval mount, unprotected
  secondary readers, in-process mlebench grader). → Theme B.
- **Secret-leak gate (README I21) doesn't exist** + full host env to children. → Theme B.
- **UI server unauthenticated + CORS `*` + arbitrary-file-read.** → Theme F/G.
- **Replay determinism** only within one schema version; lock swallows failures; read-model can
  diverge. → Theme G.
- Robustness: LLM client has no network-error handling; external agent has no process-group kill.
  → Theme C/G.

*Several of these are already being fixed in the working tree (command_eval/replay/repo_task/sandbox
modified, `test_review_fixes_3.py` added) — Theme B/G should converge with that effort, not fork it.*

---

## 2. Competitive landscape (where LoopLab fits)

| System | What it is | LoopLab's position |
|---|---|---|
| **Sakana AI Scientist v1/v2** | End-to-end "write a paper" loop: idea → code → experiments → writeup → review. | LoopLab is narrower/deeper on the *experiment-optimization* core, with stronger eng. discipline (event log, resume, trust tiers). Missing: literature-grounded ideation, autonomous writeup. |
| **Weco AIDE** | Tree-search agent treating ML engineering as code-optimization over a solution tree (draft/debug/improve operators). | **Closest analog.** AIDE+o1-preview = 16.9% medals on full MLE-bench (the long-standing baseline). 2025 finding: AIDE's *limited operators* cap it — under them, fancy search doesn't help (§0.5①). LoopLab matches AIDE's shape; the edge is **A0 richer operators**. |
| **MLE-bench leaders (2025): AIRA, KompeteAI, ML-Master, RD-Agent, MLE-STAR** | The current frontier on OpenAI's MLE-bench (75 Kaggle comps, held-out medal grading). | **KompeteAI 51.5%** (Lite, Gemini-2.5-Flash) leads via 6.9× cheaper eval; **AIRA-MCTS 47%** (Lite) via richer operators; AIRA-greedy **31.6%** on full-75 with o3; **MLE-STAR 64%** (Lite) via web-init + code-block ablation. LoopLab has an mlebench-shaped adapter but not the *real* benchmark wired — **Theme D** + **A0/A6** make it competitive and measurable. |
| **SWE-bench / SWE-agent / agentless** | Benchmarks + agents for repo-level code fixing against held-out tests. | Directly informs the Developer role (Theme C). LoopLab's repo mode is the right substrate. |
| **AutoML (Auto-sklearn, SMAC3, Optuna, Ray Tune)** | Principled HPO/NAS via BO + multi-fidelity bandits (Hyperband/BOHB/ASHA). | LoopLab's policies are LLM/tree-centric; it lacks the *allocation* science. Theme A grafts this in — a differentiator vs pure-LLM agents. |
| **W&B / MLflow / ClearML / Aim** | Experiment tracking + sweep viz + model registry + lineage. | LoopLab's UI already rivals these for a *single autonomous run*; the gap is cross-run aggregation, provenance/lineage export, and collaboration. Theme F. |

**Strategic identity:** LoopLab's unique seam is *"a local-first, fully-auditable autonomous ML
researcher where the LLM drives a principled search you can watch and steer in real time."* No
competitor combines (event-log auditability + live steering UI + trust-tiered untrusted execution +
LLM-driven *and* AutoML-principled search). That intersection is the moat to widen.

---

## 3. Theme A — Operators & search intelligence (the highest-leverage bet)

**Problem.** Two gaps: (i) *operator quality* — the proposer's move set (draft/debug/improve/merge/
ablate) is what actually moves the metric, and the 2025 evidence says **this is the bottleneck, not
the search policy** (§0.5①); (ii) *compute allocation* — every eval runs to completion and the metric
history is ignored when proposing. **Do A0 first; A1–A4 compound on it.**

**A0 · Richer operators (the actual bottleneck — NEW top priority).** Invest in the *move set* before
the search policy. Concrete upgrades, all mapped to existing LoopLab seams:
  - **Code-block ablation → targeted refinement (MLE-STAR).** LoopLab's `_ablate` already finds the
    highest-impact *numeric param* and emits a `refine_block` child. Extend it to **code blocks /
    pipeline components** (feature-eng, model, loss, ensembling): ablate each block, then have the
    Developer refine only the highest-impact one. *Verified: MLE-STAR's core mechanism, 64% Lite
    (arXiv:2506.15692).* *Effort:* M. *Differentiator:* high — LoopLab is one extension away.
  - **Real solution-merge / ensembling.** `merge_idea` currently means-merges *params*. Add an
    operator that **composes top solutions' code** (stacking/ensembling/blending), the way MLE-STAR's
    "novel ensembling" and KompeteAI's candidate-merging do. *Effort:* M.
  - **Memory operator.** A `recall` operator that seeds a draft from the best matching prior case
    (cross-run memory already stores them) — turns memory from passive retrieval into an action.
    *Effort:* S–M. *Ties to:* E4.
  - **Web-retrieval-grounded init (optional).** Seed the first draft from a web/lit search for
    effective models (MLE-STAR's initialization), behind a network-optional flag. *Effort:* M. *Ties to:* E3.

**A1 · Multi-fidelity racing (ASHA / Hyperband / successive-halving).** *Highest ROI of the search items.* Run many
candidates at low fidelity (`smoke`), promote only the top fraction to higher fidelity (`full`),
repeat — instead of full-evaluating everything. LoopLab *already has fidelities* (`Idea.eval_profile`
smoke/full + confirm forces full); it just needs a **successive-halving scheduler** in `policy.py`
that (a) batches candidates at rung 0, (b) ranks, (c) promotes ⌈n/η⌉ to the next rung. ASHA is the
asynchronous variant — ideal for LoopLab's single-experiment-at-a-time default (promote as soon as a
rung fills, no barrier). *Literature: Hyperband (Li et al. 2018, arXiv:1603.06560) reports
order-of-magnitude speedups over BO on DL/kernel problems; ASHA (Li et al. 2020) scales it
asynchronously.*
  - *Code:* new `policy.SuccessiveHalvingPolicy` / extend the eval loop to carry a `rung`; reuse
    `total_eval_seconds` budget accounting; emit a `rung_promoted` event (replay-safe).
  - *Effort:* M. *Depends on:* nothing new — fidelities exist. *Differentiator:* yes.

**A2 · Surrogate-guided proposal (Bayesian optimization / TPE).** When the action space is numeric
params (regression / repo-framework tuning), fit a cheap surrogate (TPE/GP/random-forest) on the
`(params → metric)` history and propose the next point by maximizing an acquisition function
(Expected Improvement / UCB) instead of random/hill-climb. *Literature: BO conditions on ALL prior
evaluations via a surrogate + acquisition fn (Frazier 2018; distill.pub/2020/bayesian-optimization);
TPE/SMAC3 are the practical workhorses; BOHB/DEHB fuse BO with Hyperband for anytime + final
performance (Falkner et al. 2018).*
  - *Code:* a `RandomForestSurrogateResearcher` / `TPEResearcher` behind the same Researcher Protocol
    (drops in like `RecursionError`-free `RepoParamResearcher`); pure-Python TPE keeps zero-dep
    default; optional `scikit-optimize`/`optuna` extra for GP/CMA-ES.
  - *Effort:* M–L. *Wins when:* expensive evals + ≤~20 continuous params.

**A3 · BOHB/DEHB fusion.** Combine A1+A2: Hyperband decides *how much* compute per candidate;
BO/evolution decides *which* candidate. This is the AutoML state of the art for HPO and a natural
capstone once A1 and A2 exist.
  - *Effort:* M (once A1+A2 land). *Differentiator:* strong.

**A4 · Better LLM-guided tree search (LATS-style).** Upgrade the MCTS policy with
language-agent-tree-search ideas: LLM-generated value estimates + self-reflection on failed branches
fed back as proposal context, and explicit novelty/dedup so the tree doesn't re-propose near-duplicate
ideas. *Ties to Theme E (ideation).*
  - *Effort:* M. *Wins when:* LLM is the proposer and the space is combinatorial (code edits).

**A5 · Cost/budget-aware proposal.** The Researcher should reason over *remaining* eval budget
(`max_eval_seconds − total_eval_seconds`) when choosing fidelity/breadth — already noted as
"deferred-by-design" in build-status. Surface budget into the proposal prompt + policy.
  - *Effort:* S.

**A6 · Proxy / predictive scoring (the other cost lever — verified high-ROI).** Rank a candidate's
*potential* from **early-stage signals** (first-epoch/partial-data val, a learned predictor over
`(early-metric, params, op) → final-metric`) so doomed candidates are killed before a full eval —
distinct from A1's rung-based racing (this is *model-based early prediction*). Frontier systems win
primarily on this lever: **KompeteAI's predictive-scoring + accelerated-debugging = 6.9× faster eval**
(≈6.9× more iterations/budget; arXiv:2508.10177); **ArchPilot = proxy-guided MCTS with selective
full-training escalation** (arXiv:2511.03985). *Code:* a `ProxyScorer` consulted before/around
`_evaluate`; emit a `proxy_scored` event; escalate to full eval only for promoters. Pairs with A1
(proxy picks rung-0 survivors cheaply) and **C2 best-of-N** (makes N affordable). *Effort:* M–L.
*Differentiator:* yes — this is what currently separates the MLE-bench leaders.

---

## 4. Theme B — Trust & eval integrity (the credibility prerequisite)

*These are the review's C1/C2 — restated as roadmap features because they unlock untrusted real
tasks (Theme D). Several are in-flight in the working tree; finish them.*

**B1 · Host-side metric scoring.** For any non-trusted run, the *host* (not the candidate's process)
computes the metric from artifacts the code can't rewrite. Generalize the eval contract: the
candidate writes `predictions.json`; the host (holding held-out labels) scores it. Makes
`stdout_json` self-reporting a trusted-local-only convenience.
  - *Code:* `command_eval` read-only input mount (`-v root:/work:ro` + a separate writable
    `out/`); `mlebench` grader runs **out-of-process** with labels never on the candidate FS.
  - *Effort:* M. *Unlocks:* credible held-out benchmarks.

**B2 · Protect every reader + glob `protect`.** Add `metrics`/`constraints`/`cross_check` reader
paths to `_protected_names`; make `protect` glob-match. *(Review C1.)* *Effort:* S.

**B3 · Real secret-leak gate.** Implement the advertised I21: minimal env allow-list to children
(never `LLM_API_KEY`), + a redaction pass (regex set + entropy) over every stdout/stderr/completion
tail before it hits the event log / spans / UI. *(Review C2.)* *Effort:* S–M. *Or* correct the docs.

**B4 · Sandbox hardening for the untrusted tier.** Docker: `--read-only` + tmpfs, `--memory` /
`--pids-limit` / `--cpus`, `--cap-drop ALL`, `--user`, `--security-opt no-new-privileges`; Windows
Job Object for atomic tree-kill; bounded in-flight output (kill-on-exceed, not post-hoc cap). **Add a
true-isolation tier (gVisor / Kata / Firecracker microVM).** *Research (verified, vote 2-0): standard
shared-kernel container hardening — seccomp, cap-drop, read-only FS — "improves the posture but does
not address the fundamental shared-kernel problem"; a real boundary (user-space kernel like gVisor or
a microVM like Kata/Firecracker) is required for untrusted LLM-generated code* (zylos.ai, corroborated
by Docker/Northflank). So tier the trust model: `untrusted` = hardened Docker (now), `hostile` =
gVisor/Firecracker (new, optional). *Effort:* M (hardening) + L (microVM tier).

**B5 · Reward-hacking detector.** A lightweight monitor that flags suspicious wins: metric exactly at
the optimum, a candidate that imports the grader, output that touches protected paths at runtime, or
a metric that diverges from the host-side recompute. Emit a `reward_hack_suspected` event surfaced in
the Trust panel. *Research: specification-gaming is a documented, recurring failure mode — agents
delete failing unit tests instead of fixing bugs, manipulate the task environment, or overwrite
evaluator state; "ImpossibleBench" measures a model's cheating rate by pitting the spec against the
tests, and reasoning models attempt to hack at high rates on tampered tasks. Single-mechanism defenses
each block only part of the attack surface, so defense-in-depth (independent host-side recompute +
behavioral monitors + held-out-artifact denial) is the recommended architecture.* *Effort:* M.
*Differentiator:* yes — few systems surface this to the operator live.

**B6 · Held-out test split + generalization-gap guard (the #1 *unsolved* problem — verified 3-0).**
*The single highest-value feature the 2026 research surfaced.* MLE-bench agents systematically
**overfit the validation metric while true test performance plateaus or declines** (val-test gap
15–16.6%; searching on test would gain 9.4–12.4% — arXiv:2507.02554 §5.3). LoopLab today selects
`best` purely by the validation/CV metric, so it is *structurally* exposed to this. Add a **final
held-out split the proposer/policy/confirm phase never see**, scored only when reporting a champion;
fold a per-node **`generalization_gap = val − test`** into the trust model; **prefer/flag** candidates
by a *robust* objective (e.g. penalize large gaps, or select by test-on-the-held-out among the
val-top-k) rather than raw validation rank.
  - *Code:* extend the eval contract with an optional `holdout` reader the search can't address;
    emit `holdout_evaluated`; surface the gap in the Trust panel + the report's "what worked" (a node
    that won on val but lost on holdout is the headline "what didn't actually work"). Reuses the
    existing confirm/feasibility machinery (a high-gap node is "infeasible-for-selection").
  - *Effort:* M. *Differentiator:* **high** — current SOTA systems do *not* solve this. *Gates:* D1
    (a credible MLE-bench score is only meaningful with held-out selection).

---

## 5. Theme C — Reliable coding Developer (SWE-bench stack)

**Problem.** Build-status notes the local small model (qwen3:8b) is flaky with edit/diff tools.
SWE-bench-Verified leaders converged on a reliability recipe LoopLab can adopt wholesale. *Research
evidence (SWE-bench Verified = 500 human-validated tasks): **Agentless** — a fixed three-phase
pipeline (hierarchical localization → sampled diff-format patches → patch validation), no open-ended
agent loop — hit 32% on SWE-bench Lite at **$0.70/issue** and >50% Verified with Claude 3.5 Sonnet,
and its recipe trained **Kimi-Dev to 60.4% Verified (open-source)**; **PatchPilot** (fixed
human-style workflow) beat agentic OpenHands 53.6% vs 53.0% AND was far more stable across repeats
(std-dev 2.5 vs 3.2). **SWE-agent** showed the **Agent-Computer Interface (ACI) is the key driver** —
a code-edit/navigate/test interface tuned for the model, not raw shell. **Best-of-k selection with an
execution-free reward model (SWE-RM)** lifted Qwen3-Coder 51.6%→62.0%. Takeaway: for a weak local
model, a **fixed agentless workflow + a good ACI + best-of-N selection** beats an open-ended agent
loop on both quality and stability — exactly LoopLab's situation.*

**C1 · Fault localization before editing.** Give the Developer a localization step (grep/embedding
retrieval over the repo → the handful of files/functions to touch) so it edits the right place — the
first phase of the Agentless recipe. The read-only `RepoTools` already exists — wire a localization
sub-phase into `cli_agent`/repair. *Documented failure mode it fixes: agents "missing relevant files
needing edits across multiple locations" (Cognition/Devin report).*
  - *Effort:* M.

**C2 · Best-of-N candidate solutions + selection.** Generate N independent attempts (vary
temperature/seed/prompt-angle), run each against the eval, keep the best — the single most reliable
SWE-bench lever (SWE-RM best-of-k: +10pts). Naturally parallel; reuses the existing eval harness.
*Wins big with weak models.* Pair with a learned/LLM selector when a held-out eval isn't available.
  - *Effort:* M. *Depends on:* Theme A1 (so the N attempts are raced cheaply, not all full-eval'd).

**C3 · Test-driven self-repair (already partial — deepen).** LoopLab has an error-feedback repair
operator. Strengthen it: feed the *failing test output + minimal repro* (not just stderr), cap repair
depth, and add a "write a test that reproduces, then fix" mode for repo tasks.
  - *Effort:* M.

**C4 · Independent verification / critic.** Before accepting an agent solution, a separate critic
pass (or self-consistency vote) checks it does what the idea claims and didn't game the eval. Folds
into the existing `ValidatingDeveloper`.
  - *Effort:* S–M. *Ties to:* B5.

**C5 · Agentless mode (high priority given the weak local model).** A deterministic
localize→generate-N-patches→validate pipeline (no open-ended agent loop) is empirically more reliable,
more *stable*, and far cheaper than a full agent (Agentless 32% Lite @ $0.70; PatchPilot more stable
than agentic). Offer it as a `developer_backend=agentless` preset — likely LoopLab's best default
coding path until a stronger model is available. *This subsumes C1+C2+C4 into one proven recipe.*
  - *Effort:* M. *Recommendation:* make this the **default** repo-mode Developer.

**C6 · Better ACI / write-over-edit.** Build-status already found qwen3:8b is more robust writing a
file whole than using strict edit/diff tools. Formalize this into a tuned Agent-Computer Interface:
constrained, validated edit primitives + clear navigation/test affordances (SWE-agent's central
finding: the *interface* drives reliability more than the scaffold). *Effort:* M.

---

## 6. Theme D — Benchmarks & real tasks (the external scorecard)

**D1 · Wire real MLE-bench.** The adapter shape exists; add Kaggle dataset download (behind the
proxy/SSL caveat) + the real grader, gated behind Theme B (host-side/out-of-process scoring). Report
LoopLab's MLE-bench score — the single most credible proof point.
  - *Effort:* L. *Depends on:* B1. *Payoff:* huge (comparability vs AIDE/agents).

**D2 · A LoopLab self-benchmark harness.** A reproducible suite of N held-out tasks the engine runs
end-to-end on each release, reporting best-metric, eval-seconds-to-target, and reward-hack flags — a
regression test for *capability*, not just code. (`tools/e2e_report.py` is the seed.)
  - *Effort:* M.

**D3 · More task adapters.** Time-series forecasting, tabular AutoML (sklearn/LightGBM), small NLP —
each a `TaskAdapter`. Validates generality and grows the demo surface.
  - *Effort:* M each, parallelizable.

**D4 · Dataset/data-version provenance.** Pin dataset hashes into the run (extend the workspace
fingerprint) so a result is tied to exact data — table-stakes for reproducibility (DVC-style).
  - *Effort:* S.

---

## 7. Theme E — Idea generation & multi-agent ideation

**E1 · Novelty / dedup gate.** Before evaluating a proposed idea, check it isn't a near-duplicate of
an existing node (embedding similarity over `idea` text + params). Stops the tree wasting evals on
the same idea reworded. *Reuses the vector store.* *Effort:* S–M.

**E2 · Researcher panel + *empirical* ranking (not LLM-judge-as-oracle).** Generate K diverse ideas
(different "angles": risk-first, baseline-first, exploit-leader) — multi-agent ideation + self-critique
measurably raises idea *diversity* but **saturates at ~3 critics / depth 3** (arXiv:2507.08350), so
keep the panel small. **Rank them by cheap empirical evals (A1 rung-0) or the surrogate (A2)**, NOT by
an LLM-judge: *verified caution — an LLM-as-judge is ≈random (~50%) at ranking top vs bottom ideas*
(ICLR 2025), so the LLM is a weak prior + diversity source, not the selection oracle. An **Elo
"idea tournament"** (pairwise debate, à la DeepMind's AI co-scientist) is fine as a *prior* to seed
which ideas get the cheap eval first — but the eval, not the debate, decides. *Effort:* M. *Ties to:* A1/A4.

**E3 · Literature-grounded ideation.** Optional: a Semantic-Scholar/arXiv tool so the Researcher can
ground ideas in real techniques (like AI Scientist). Behind a network-optional flag. *Effort:* M.

**E4 · Reflection memory → priors (gradient-free meta-learning).** Turn cross-run memory into
*warm-start priors*: meta-learn which operators/params worked for this task-type and bias the first
proposals. LoopLab stores the cases; it doesn't yet use them to seed search. Concrete pattern from
DeepMind's AI co-scientist (Pass 6): a **"meta-review" step distills recurring lessons from a run's
trace into a short note appended to the next run's proposal prompt** + persistent stats — meta-learning
with *no fine-tuning*, which fits LoopLab's local-first, event-sourced design exactly. Pairs with the
**A0 memory operator** (recall a prior winner as a draft). *Effort:* M. *Differentiator:* meta-learning
across runs without training.

---

## 8. Theme F — Observability & researcher UX

*The UI already rivals trackers for one run. Gaps are cross-run, provenance, and collaboration.*
Essential tracker features per the research (W&B/MLflow/ClearML/Neptune): run comparison, sweep viz
(**parallel-coordinates + hyperparameter-importance**), artifact/lineage tracking, model registry,
collaboration. LoopLab has parallel-coords, a registry, and lineage-via-DAG already.

**F1 · Hyperparameter-importance view.** W&B's "param importance" (which params most predict the
metric) — LoopLab has ablation/sensitivity per-node; add a *global* importance plot across all nodes
in a run (and across a sweep). *Effort:* S–M.

**F2 · Cross-run sweep aggregation.** A view that overlays many runs of the same task: best-metric
trajectories, param↔metric correlations, which policy/settings won. Turns the per-run UI into a lab
dashboard. *Effort:* M.

**F3 · Lineage / provenance export.** Export a run's full lineage (data hash → code → params →
metric → champion) as a portable artifact (JSON/HTML), and a model-card for the champion. *Effort:* S.

**F4 · Collaboration & sharing.** Read-only shareable run links, comments/annotations (annotations
exist as events — surface a thread UI), and export-to-report (report.js exists — extend). *Effort:* M.

**F5 · UX correctness debt** (from review): fix the `Reasoning→Trace` tab regression, the
`layoutWithGroups` cycle guard, SSE/Dock O(n²) refetch, `delete_run` `ignore_errors`, listener leaks.
*Effort:* S — do alongside Theme G.

---

## 9. Theme G — Scale, ops, and hardening

**G1 · Server auth + hardening.** Token on mutating `/api/*`, CORS allow-list, SPA traversal guard,
loopback bind, `task_file` allow-list. *(Review C3.)* Prerequisite for any non-local deployment.
*Effort:* S.

**G2 · Replay/durability hardening.** Read/enforce `Event.v`, idempotent `fold` for terminal events,
fail-loud append lock, atomic read-model with a seq watermark + refresh-on-append. *(Review C4/C5.)*
*Effort:* M.

**G3 · Distributed / parallel eval.** The anyio fan-out seam exists (`max_parallel>1`). Make it
production-grade: a worker pool (local processes now, remote workers later) with the budget guard the
review flagged for the parallel path. Enables A1's batched rungs at scale. *Effort:* L.

**G4 · LLM-client robustness.** Wrap `_post` (typed transport errors + bounded retry/backoff), guard
`choices[0]`, reuse `_kill_tree`/process-groups in `cli_agent`. *(Review high-sev.)* *Effort:* S–M.

**G5 · MLflow / OTLP bridges (consumer-facing).** OTel export already works; add an optional MLflow
run-export so LoopLab plugs into existing MLOps stacks (the build-status "MLflow seam"). *Effort:* M.

---

## 10. Phased plan (sequenced)

**Phase 1 — "Trust the numbers" (foundation; ~now).** Finish the in-flight review fixes as features:
B1 host-side scoring, B2 reader protection, B3 secret gate, **B6 held-out test + generalization-gap
guard**, G1 server auth, G2 replay hardening, G4 LLM robustness, F5 UX correctness. *Why first:*
everything credible (benchmarks, untrusted tasks, sharing) depends on integrity + safety being real;
**B6 is here because selecting on validation alone is the verified #1 failure mode** and a credible
benchmark score (D1) is meaningless without held-out selection. Much of this is already underway in
the tree (the review-fix commits).

**Phase 2 — "Better moves, then better search" (differentiation).** **A0 richer operators FIRST**
(code-block ablation refinement, real merge/ensembling, memory operator) — the verified bottleneck —
then **A6 proxy scoring** + A1 ASHA (the two cost levers that make breadth affordable), A2 surrogate
proposal, A5 budget-aware; C1/C2/C3 Developer reliability (localization + best-of-N + deep repair),
E1 novelty gate. *Why this order:* the research is explicit that fancy search without rich operators
is wasted (§0.5①), and proxy/ASHA are what make best-of-N + broad operator search affordable.

**Phase 3 — "Prove it & scale it" (validation + reach).** D1 real MLE-bench (needs B1+B6), D2
self-benchmark, A3 BOHB capstone, B5 reward-hack detector, E2/E4 panel + Elo prior + meta-priors,
F1/F2/F3 cross-run UX, G3 distributed eval, D3 more adapters. *Why last:* benchmarks need integrity +
held-out selection (Phase 1) and good operators/search (Phase 2) to produce results worth publishing.

---

## 11. Quick wins vs big bets

| Quick wins (≤ S, high value) | Big bets (L, transformative) |
|---|---|
| B2 protect readers · B3 secret gate · G1 server auth · A5 budget-aware · E1 novelty gate · F3 lineage export · D4 data provenance · F5 UX fixes · A0 memory-operator (S–M) | **A0 code-block ablation + merge/ensembling (the verified bottleneck)** · **B6 held-out test + generalization-gap guard (#1 unsolved)** · A6 proxy scoring + A1 ASHA · D1 real MLE-bench · C2/C5 best-of-N / agentless Developer · G3 distributed eval · E4 meta-learned priors |

**If you do only three things:** (1) **A0** — extend `_ablate` to code blocks + add a real
merge/ensembling operator (the verified #1 lever, and LoopLab is closest to it); (2) **B6** — a
held-out test the search can't see + a generalization-gap guard (the verified #1 *unsolved* problem,
and LoopLab is structurally exposed today); (3) **A6/A1** — proxy scoring + ASHA racing so broad
operator search and best-of-N are affordable (what currently separates the MLE-bench leaders).

---

## 12. Risks & open questions

- **Local-first vs heavy deps.** A2 (GP/CMA-ES), D1 (Kaggle), gVisor — each risks the zero-dep
  default. Keep them *optional extras*; pure-Python fallbacks for the default path (TPE not GP;
  templated mlebench not live Kaggle). Consistent with the project's stated local-first principle.
- **Small-model ceiling.** Themes C/E assume the LLM can propose/edit well; qwen3:8b is flaky.
  Best-of-N + validator + agentless mode are the hedges; also support a bigger optional model.
- **Network/proxy environment.** Literature grounding (E3) and Kaggle (D1) need egress that's been
  flaky on this box (corporate MITM proxy). Gate behind explicit flags.
- **Scope discipline.** The breadth is done; the temptation is more breadth. The roadmap deliberately
  goes *deep* on search + trust + Developer, not wide.
- **Research re-run.** *Partly done (2026-06-24):* the AI-ML-engineering-frontier pass was re-run and
  now carries verified (3-0) claims (operators-bottleneck, overfitting wall, KompeteAI/ArchPilot,
  MLE-STAR), and a research/scientist-layer pass was added. The verification layer was again throttled
  on secondary claims (AIDE per-model breakdown, AI-Scientist-v2, reward-hacking specifics) — re-run
  those when capacity returns to harden the remaining ⚠️ leads in RESEARCH_NOTES.

---

*Companion docs: [CODE_REVIEW.md](CODE_REVIEW.md) (the foundation gaps Phase 1 closes),
`06-implementation-plan.md` (original iteration plan), `03-decisions.md` (ADRs).*
