# LoopLab — External Works Analysis: MARS, S1-NexusAgent, AgentDS, AgentRxiv, DS-Automation Surveys (2026-07-04)

**Method.** Three parallel research passes: (1) a full architecture map of the current engine
(seams, protocols, ADRs); (2) primary-source research on MARS (arXiv 2602.02660) and S1-NexusAgent
(arXiv 2602.01550 — note: the circulated ID 2606.06036 is a *different* paper, "Memory is
Reconstructed, Not Retrieved"), including cloning and inspecting both GitHub repos; (3) research on
AgentDS (arXiv 2603.19005), AgentRxiv (2503.18102) / Agent Laboratory (2501.04227), and the 2025–26
survey corpus on measuring data-science automation. arXiv full texts were proxy-blocked in this
session; paper-level claims come from search snippets and are marked where unverified. Repo-level
facts are verified by direct inspection.

**Question answered.** For each work: can it be integrated into LoopLab; is there synergy with what
we have; or is it simply better and worth replacing our core with.

**TL;DR verdict.** Nothing here is "simply better" than LoopLab as a whole — MARS ships no
framework code, Agent Laboratory is engineering-weaker and older, S1-NexusAgent targets a different
domain, AgentDS and the surveys are a benchmark and maps respectively. But **MARS is a direct
competitor in our exact niche with published SOTA MLE-bench numbers and three importable ideas**
(budget-aware search reward, modular solution construction with diff refinement, comparative lesson
distillation), **AgentDS is a ready-made evaluation target** that probes exactly the capabilities
our Genesis/deep-research stages claim (problem framing, domain sanity checks, pivoting), and
**AgentRxiv quantifies the ROI of cross-run shared knowledge** (+11–14% relative) — a pattern our
memory layer is one step away from. S1-NexusAgent is the long-horizon option if LoopLab ever
expands beyond ML tasks into multidisciplinary science; short-term its value is its Apache-2.0 tool
corpus (130+ scientific tools, MCP-exposable) and two context-engineering patterns.

---

## 1. MARS — arXiv 2602.02660 (Google Cloud AI Research, ICML 2026)

**What it is.** "Modular Agent with Reflective Search for Automated AI Research" — automated ML
engineering on MLE-bench. Three pillars:

1. **Budget-aware planning** — cost-constrained MCTS over actions {Draft new architecture, Debug,
   Improve valid solution} with an efficiency-guided reward that penalizes high execution cost.
2. **Modular construction** — a Design→Decompose→Implement pipeline (Idea / Modular / Coding
   agents) that splits each solution into independent, testable modules
   (`model.py, config.py, data.py, features.py, …`), enabling **diff-based refinement** of a single
   logic block instead of whole-script regeneration.
3. **Comparative reflective memory** — distills execution logs into structured "Debugging Lessons"
   and "Solution Lessons" via cross-solution difference analysis (credit assignment); 63% of
   utilized lessons transferred across search branches.

**Results.** MLE-bench, 24 h budget, 3 seeds: MARS+ (Gemini-3-Pro-Preview) **All 62.67 ± 0.77%**
(Low 78.79 / Medium 60.53 / High 44.44); base MARS 56.0%. Above PiEvolve (61.33), Famou-Agent 2.0
(59.56), ML-Master 2.0 (56.44). Snippet-sourced (unverified): 65.8% above-median, 31.1% gold rate.

**Code.** github.com/jfc43/MARS, MIT — **trajectories and grading reports only; no framework
source released.** Not adoptable as software.

**Relation to LoopLab.** This is our niche: MCTS search over candidate ML solutions on MLE-bench.
We already have the skeleton MARS describes — `search/policy.py` (MCTS/ASHA/evolutionary),
`engine/memory.py` (cross-run memory), `trust/` gates MARS lacks entirely, and event-sourced replay
MARS has no equivalent of. What MARS has and we don't:

- **Cost-aware reward in search.** Our policies select on score/novelty; MARS folds execution cost
  into the node reward. Seam: `search/policy.py` reward shaping + fidelity ladder we already have
  (ASHA) makes this a small, high-leverage change.
- **Modular solution representation.** Our Developer emits whole solutions; MARS's per-module
  layout enables targeted diff refinement and module-level attribution. Seam: Developer contract +
  `search/operators.py` (a "refine module X" operator). Medium effort, changes the node artifact
  shape.
- **Comparative lesson distillation.** Our memory stores outcomes and reflection priors; MARS
  *diffs sibling solutions* to assign credit before distilling a lesson. Seam: `engine/memory.py` +
  deep-research stage. Complements the memory-lifecycle concerns already flagged in
  [11-agent-systems-research.md](11-agent-systems-research.md) §memory.

**Verdict: synergy, not replacement.** No code to adopt; ideas port cleanly onto existing seams.
MARS+ 62.67% All is the number to benchmark our MLE-bench runs against. Their published
trajectories (~359k generated files) are usable as reference/eval data.

---

## 2. S1-NexusAgent — arXiv 2602.01550 (CASIA, Feb 2026)

**What it is.** Self-evolving multi-agent framework for multidisciplinary scientific research
(biology, chemistry, materials). Hierarchical **Plan-and-CodeAct dual loop** (global planning outer
loop, tool-executing inner loop); native MCP with **intention-aware dynamic tool retrieval**
(embedding-based, hot-plug, "thousands" of tools claimed); **object-reference sparse context
management** (subtask isolation + intermediate-result compression); self-evolution via a **Critic
Agent** distilling trajectories into reusable "Scientific Skills". Claims SOTA on biomini-eval,
ChemBench, MatSciBench (numbers not verifiable this session).

**Code (verified by cloning).** github.com/CASIA-LM/S1-NexusAgent, **Apache 2.0**, active
(2026-03). Real runnable framework on LangGraph/LangChain: intent→retrieval→planner→execute→report
pipeline, 130+ scientific tools (genomics/CRISPR, RDKit, Materials Project), MCP client, Docker
sandbox, Langfuse tracing, HITL CLI. **The paper's Critic/self-evolution loop is largely absent
from the open repo** — only skills-loading infrastructure ships. DeepSeek-chat as primary LLM,
Qwen3-Embedding-8B, Python 3.12+.

**Relation to LoopLab.** Orthogonal, not competing. S1-Nexus is breadth-first tool orchestration
for open-ended science; LoopLab is depth-first metric-driven ML experimentation with verification.
It has no equivalent of our event log, trust gates, candidate search, or replay; we have no
equivalent of its scientific-tool breadth. Adopting it wholesale would also violate ADR-18 (no
external agent framework — LangGraph is a hard dependency there).

Integration options, cheapest first:

- **Tool corpus via MCP.** Their 130+ Apache-2.0 scientific tools can be exposed to our agents
  through the existing MCP client (`tools/mcp_tools.py`) with zero engine changes. Only relevant
  if/when we take on science-flavored tasks (the `dataset`/`repo` adapters are the natural users).
- **Dynamic tool retrieval.** Embedding-based tool selection when the tool count exceeds what fits
  in a prompt. We don't need it at today's tool count; it becomes relevant with large MCP configs.
  Seam: `agents/agent.py` `CompositeTools`.
- **Object-reference sparse context.** Passing references to intermediate artifacts instead of
  inlining them — aligns with our events-as-truth design; a pattern worth stealing for the tool
  loop's context budget.

**Verdict: complementary; adopt components/patterns, not the framework.** "Simply better" only in
a domain we don't currently target.

---

## 3. AgentDS — arXiv 2603.19005 (UMN + Cisco Research, 2026)

**What it is.** A **benchmark + live competition**, not a framework: 17 domain-specific
data-science challenges across 6 industries (commerce, food production, healthcare, insurance,
manufacturing, retail banking); multimodal inputs (images/text/PDF); deliberate
validation-vs-test distribution shift; features requiring industry knowledge. First run: 29 teams;
**AI-only baselines landed below the top human quartile** (a GPT-4o baseline 17/29, Claude Code
10/29); winning entries were human-AI collaborations. Decisive human edges: problem framing before
coding, domain sanity-checking of results, knowing when to pivot.

**Availability.** agentds.org; a `lainmn/AgentDS` HF dataset exists but its official status is
unverified; no confirmed harness repo/license yet. One competition run — emerging, not stable.

**Relation to LoopLab.** Pure benchmark; zero overlap with our code, maximal fit with our eval
story. MLE-bench (which we already run, including real-Kaggle grading in
`adapters/mlebench_real.py`) measures ML engineering; AgentDS measures exactly the *other* half we
claim — Genesis's problem framing, deep-research's domain grounding, the Strategist's pivot
decisions, and the trust ladder's sanity checks under distribution shift. Its human-AI-collab
finding also directly validates our `approve`/confirm HITL tiers.

**Integration.** A new `TaskAdapter` (registered in `adapters/tasks.py` `_KINDS`), modeled on the
mlebench adapters — contingent on confirming data/harness availability from agentds.org. Low
effort, high signal.

**Verdict: adopt as evaluation target.** No synergy question — it's a measuring stick, and one
aimed at our weakest-evidenced claims.

---

## 4. AgentRxiv (2503.18102) & Agent Laboratory (2501.04227) — Schmidgall et al.

**Clarification.** The PhD/Postdoc/ML-Engineer/Professor role pipeline is **Agent Laboratory**
(AMD/JHU, Jan 2025, EMNLP 2025 Findings): literature review → experimentation (mle-solver) →
report writing (paper-solver), with NeurIPS-rubric Reviewer agents and optional human co-pilot
checkpoints; MIT, github.com/SamuelSchmidgall/AgentLaboratory. **AgentRxiv** (JHU/ETH, Mar 2025) is
the infrastructure layer on top: a shared preprint server letting multiple Agent-Laboratory
instances read each other's outputs. Measured effect: one lab reusing its own prior papers +11.4%
relative on MATH-500 (gpt-4o-mini 70.2→78.2); three labs sharing +13.7%, converging faster.

**Relation to LoopLab.** The experimentation core (mle-solver) is what LoopLab already does,
strictly better: they have no event sourcing, no replay, no leakage/reward-hack gates, no search
policies beyond iterate-and-score. Their fixed role cast maps onto our Researcher / Developer /
Strategist / critic protocols. Two things they have that we lack:

- **A report/paper-writing terminal stage with reviewer agents.** Our `serve/report.py` and
  deep-research memos are run summaries, not standalone research reports with a rubric-based
  review loop. If LoopLab's output should ever be "a paper", the paper-solver + Reviewer pattern
  is the reference. Seam: a post-run stage over the event log — clean, additive.
- **Cross-instance knowledge sharing (the AgentRxiv pattern).** Our cross-run memory
  (`engine/memory.py`, knowledge tools) is per-workspace. A shared artifact/lesson store across
  *parallel* runs (e.g. `sweep.py` fleets) is the same pattern, and AgentRxiv supplies the ROI
  estimate. This compounds with MARS's comparative-lesson idea: shared store + credit-assigned
  lessons.

**Verdict: pattern donor, not replacement.** Code is forkable-research-grade; adopt the two
patterns, keep our engine.

---

## 5. Surveys — where to position LoopLab

- **"Measuring Data Science Automation" (2506.08800, Cambridge CFI).** Benchmark landscape survey.
  Gaps it flags — data management and EDA barely benchmarked; intermediate human-AI collaboration
  levels ignored — are areas LoopLab has real machinery for (dataset adapter, HITL tiers). Use it
  to assemble our benchmark stack and to claim under-benchmarked ground.
- **"LLM-based Data Science Agent" survey (2508.02744)** — design-space taxonomy (roles ×
  execution × knowledge × reflection); useful to position UnifiedAgent/Strategist choices against
  the field.
- **"LLM-Based Data Science Agents" lifecycle survey (2510.04023)** — maps ~45 systems onto six
  lifecycle stages; flags business-understanding and deployment/monitoring as underserved (Genesis
  covers the former; the latter is out of scope by design).
- **"A Survey of AI Scientists" (2510.23045)** — six-stage AI-scientist framework; the checklist to
  audit our stage coverage (we are strong on execution/verification, thin on synthesis/writing —
  consistent with §4's gap).

Benchmark stack implied: MLE-bench (have) + AgentDS (add) + coverage check against 2506.08800.

---

## 6. Consolidated recommendation

Priority-ordered, each mapped to an existing seam; none requires replacing the engine:

| # | Item | Source | Seam | Effort | Why now |
|---|------|--------|------|--------|---------|
| 1 | Cost/budget-aware reward in search policies | MARS | `search/policy.py` | S | Direct MLE-bench leverage; ASHA fidelity ladder already exists |
| 2 | Comparative lesson distillation (diff-based credit assignment) | MARS | `engine/memory.py`, deep-research | S–M | Upgrades memory quality; compounds with #5 |
| 3 | AgentDS task adapter (pending data availability) | AgentDS | `adapters/tasks.py` | S | Evaluates Genesis/deep-research claims MLE-bench can't |
| 4 | Modular solution construction + diff-refinement operator | MARS | Developer contract, `search/operators.py` | M | Changes artifact shape; biggest single MARS idea |
| 5 | Shared lesson/artifact store across parallel runs | AgentRxiv | memory/knowledge layer, `sweep.py` | M | +11–14% measured ROI for the pattern |
| 6 | Report/paper-writing stage + rubric reviewer agents | Agent Laboratory | post-run stage over event log | M | Fills our synthesis/writing gap (also flagged in doc 11) |
| 7 | S1-Nexus scientific tools via MCP; dynamic tool retrieval; object-reference context | S1-NexusAgent | `tools/mcp_tools.py`, `CompositeTools` | S / M / M | Only when expanding beyond ML tasks or tool count grows |

**Bottom line.** Keep the LoopLab core — its event-sourced verification engine has no peer in this
set. Treat MARS as the benchmark-setting rival to learn from (ideas 1, 2, 4), AgentDS as the next
evaluation target (3), AgentRxiv/Agent-Laboratory as pattern donors for sharing and synthesis
(5, 6), and S1-NexusAgent as the option contract on multidisciplinary expansion (7).

---

## 7. Code-level verification pass (2026-07-04)

Each §6 item was re-checked against the actual code ("do we already have an equivalent or
better?"). Three parallel read-only audits, file:line evidence. Results:

| # | Item | Verdict on "we lack this" | What actually exists today |
|---|------|---------------------------|----------------------------|
| 1 | Cost-aware search reward | **Confirmed, with a seed to build on** | No node-selection reward anywhere folds cost: MCTS UCB is metric+exploration only (`search/policy.py:352-375`), ASHA ranks survivors purely by `(metric, id)` (`policy.py:441-458`). But `operator_yields` already computes Δmetric per `eval_seconds` (`policy.py:54-76`) — it only feeds the operator-kind bandit and is **off by default** (`operator_bandit=False`, `policy.py:147,160`; `thorough` preset only). A5 `budget_aware` is a prompt nudge (`orchestrator.py:1355-1363`), the eval budget is a stop-guard (`orchestrator.py:735-740`), not a penalty. So: extend `operator_yields` into node reward + enable the bandit — smaller than greenfield. |
| 2 | Comparative lesson distillation | **Confirmed** | `_reflect_lessons` (`orchestrator.py:1586-1633`) is one-shot whole-run reflection over top-5 winners + ≤3 failures — no pairwise sibling diff, no credit assignment. `_causal_meta_note` (`orchestrator.py:1675-1697`) asks "why it won" over the ranked field — implicit comparison only. `best_of_n._listwise_pick` compares side-by-side but for *selection*, emitting no lesson. The MARS mechanism (structured winner-vs-loser diff → credit-assigned lesson) is absent; the upgrade path is these two existing functions, not a new subsystem. |
| 3 | AgentDS adapter | **Confirmed, one design constraint** | `dataset_task` overlaps on "point at tabular data, open/agent-chosen metric" but its metric is **self-reported by the solution — no private grader** (`dataset_task.py:11-16`), i.e. reward-hackable; no multimodal or business-framing scaffold; data read by absolute path. An AgentDS adapter therefore needs the held-out-grader machinery of `mlebench_real`/`repo`, not `dataset` reuse. |
| 4 | Modular construction + diff refinement | **PARTIALLY WRONG as originally stated** | Repo kind already has real multi-file, git-diff, patch-gated editing (`cli_agent.py:223-254`, `Node.files` `models.py:87-90`), fault localization (`engine/localize.py`, Agentless-style ranking), and a named `refine_block` operator with code-block ablation (`orchestrator.py:3018-3145`). What is genuinely missing vs MARS: (a) an autonomous design→decompose→implement phase for *generated* solutions (repo modularity is inherited from the operator's repo, never synthesized); (b) cumulative parent→child diff — every node re-seeds from the pristine baseline (`cli_agent.py:185-191,269-278`), `refine_block` regenerates the whole script (`orchestrator.py:3139`), and on `improve` **`parent.code` is never passed to the Developer at all** (`orchestrator.py:2061-2072` — parent reaches only the Researcher as params+metric). The cheapest first step is passing parent code into `improve`/`refine_block` and patching in place, before any decompose phase. |
| 5 | Shared store across parallel runs | **PARTIALLY WRONG as originally stated** | The lesson store is **machine-global** (`~/.looplab/memory`, `config.py:14-16,310`), concurrent-append-safe (`orchestrator.py:1564-1569`). And `SiblingRunTools` (`run_tools.py:285-457`) already live-reads *running* sibling runs' experiments/code with mtime-invalidated cache and provenance-tracked node import — wired into Researcher, Strategist and pilot (`tasks.py:171-173,264-266,388-390`). The AgentRxiv gap narrows to: (a) **mid-run lesson refresh** — lessons are written only at run end (`orchestrator.py:1510-1573`) and read only at run start (`orchestrator.py:1405-1492`), so concurrent runs never see each other's *distilled* lessons; (b) no fleet launcher (`sweep.py` is the intra-node grid runtime, not a multi-run fleet); (c) sharing is same-run-root + same-`task_id` only (`run_tools.py:389-405`), pull-based. |
| 6 | Report/paper stage + reviewer agents | **Confirmed, but it is a declared non-goal** | `serve/report.py` = run summary (audit-only `report_generated`); `deep_research.py` memo = forward-looking steering doc; `trust/critic.py` = code sanity critic; the closest reviewer is `trust/verify.py` — a rubric-prompt pass grading *memo claims* as supported/unsupported (`verify.py:81-127`), not a deliverable review with an accept/revise loop. Note: idea→paper is listed as an explicit non-goal in `01-product-design.md:40` — adopting item 6 is a product-scope decision, not just an engineering gap. |
| 7 | Tool retrieval + object-reference context | **Confirmed** | All tool specs are statically flattened into every prompt (`CompositeTools.specs()` `agent.py:34-35`; `McpTools` builds specs once, `mcp_tools.py:60-74`); vectorstore/embedding machinery serves knowledge retrieval, never tool selection; `skills.py` progressive disclosure covers skill *bodies* only. Tool-loop results are inlined + truncated to 4000 chars (`agent.py:256-260`) with generic middle-truncation/compaction (`context_budget.py`). Adjacent pattern that already exists: `run_tools.py` JIT reads by `node_id`/asset name ("just-in-time retrieval instead of stuffing everything into the prompt", `run_tools.py:1-2`) — the reference pattern is proven in-house for run state, just not for intra-loop tool outputs. |

**Net effect on the recommendation.** Ordering unchanged; framing sharpened. Items 1, 2 are even
cheaper than estimated (extend existing `operator_yields` / `_reflect_lessons` machinery). Item 4's
first step shrinks to "pass parent code into improve/refine_block and patch in place" — the full
decompose phase can wait for evidence it pays. Item 5 shrinks to mid-run lesson refresh + an
optional fleet layer on top of the already-working `SiblingRunTools` channel. Item 3 must be built
on held-out grading (`mlebench_real` pattern), not `dataset`. Item 6 requires revisiting the
product-design non-goal first. All seven map onto pre-existing roadmap/backlog IDs (A5, I5, E4,
F2, G7, D8) — nothing here contradicts the shipped roadmap; MARS/AgentRxiv mainly add external
validation and concrete mechanisms for items already parked.
