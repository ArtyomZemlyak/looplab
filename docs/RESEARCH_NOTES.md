# LoopLab — Research Notes (evidence behind ROADMAP.md)

Distilled from 5 deep web-research passes (2026-06-23). **Verification caveat:** the adversarial-vote
layer was throttled by a global API rate limit, so most claims scored 0-0 (un-voted, *not* refuted);
a handful cleared the bar (marked ✅). Treat the rest as *literature-sourced leads*, author-cross-
checked against known results. Re-run when capacity returns — especially Pass 1, which failed at scope.

---

## Pass 1 — AI-scientist / autonomous ML-engineering frontier  *(RE-RUN 2026-06-24, verified)*
The first run failed at scope (rate-limited); the re-run succeeded with 7 high-confidence claims
(adversarial verification partially throttled again, so a few in-paper claims abstained — flagged).

- ✅ **(3-0) Operators > search algorithm — THE headline 2025 finding.** ML-engineering agents = a
  *search policy* + an *operator set* over a tree of candidate code solutions, and **the operator set,
  not the search algorithm, is the dominant bottleneck**. Under AIDE's operators (draft/debug/improve),
  MCTS and evolutionary search gain *no* advantage over greedy (sweeping the UCT constant barely
  moves it); only richer **AIRA** operators make advanced search pay off, lifting MLE-bench-Lite
  medal rate **39.6% → 47.7%**. *(arXiv:2507.02554, Meta/FAIR)*
- ✅ **(3-0 / 2-1) Numbers.** AIRA-MCTS + DeepSeek-R1 = **47.0%** any-medal on MLE-bench Lite (vs
  AIDE-greedy R1 39.8%, AIDE+o1-preview 39.6%). AIRA-greedy + **o3 = 31.6%** on the full 75-task
  MLE-bench (≈2× the 16.9% o1-preview AIDE baseline). *(arXiv:2507.02554)*
- ✅ **(3-0) Overfitting on validation = the #1 UNSOLVED problem.** Agents select final solutions on
  the validation metric while the true *test* score plateaus or slightly **declines**. Searching on
  the test score (illegitimately) would gain **9.4%** (MCTS) / **12.4%** (evo); the validation-test
  generalization gap reaches **15%** (AIRA-greedy) to **16.6%** (AIDE-greedy). Validation gains "mask
  overfitting and ultimately undermine the search process." *(arXiv:2507.02554, §5.3)* → **Roadmap B6.**
- ✅ **(3-0) KompeteAI** = highest reported MLE-bench-Lite medal rate **51.5%** (±1.5, Gemini-2.5-Flash),
  ≈+3pp over RD-Agent (48.18%), ML-Master (48.5%), MLE-STAR (43.9%), AIDE (34.3%). Its key lever is
  **execution-cost reduction**: a *predictive scoring model* + accelerated debugging scores a
  solution's potential from **early-stage metrics** to skip costly full runs → **6.9× faster
  evaluation** (≈6.9× more search iterations under a fixed budget). *(arXiv:2508.10177)* → **Roadmap A6.**
- ✅ **(3-0) ArchPilot** = **proxy-guided** MCTS (with a restart mechanism) + 3 role-specialized
  agents: orchestration (UCT select/backprop + memory of prior candidates), generation
  (generate/improve/debug), evaluation (proxy training runs → a *fidelity-aware* metric). **Selective
  full-training escalation** cuts compute while converging to strong solutions. *(arXiv:2511.03985)*
- ✅ **MLE-STAR (verified directly via WebFetch of arXiv:2506.15692):** (a) **initializes** by using a
  **web search engine to retrieve effective models** from the web (not just parametric knowledge);
  (b) **refines via ablation studies analyzing the impact of individual CODE BLOCKS** → targeted
  refinement of the highest-impact component (not wholesale rewrite); (c) a novel **ensembling**
  method; (d) **64% medal rate on MLE-bench Lite**, "significantly outperforming the best alternative."
  → directly hardens **Roadmap A0** (LoopLab already ablates *params*; extend to *code blocks*).
- ⚠️ *Abstained (in-paper, lost verification votes to rate-limiting — cite cautiously):* AIDE's
  16.9%/8.7%/7.6%/3.0% per-model breakdown and tree+3-operator+summarization design
  (arXiv:2502.13138 / 2410.07095); AI-Scientist-v2 progressive agentic tree search + templateless
  generalization (arXiv:2504.08066); KompeteAI's explicit "merging stage."

## Pass 2 — Search, allocation & candidate generation
- **Hyperband** = pure-exploration infinite-armed bandit; adaptively allocates a resource
  (iters/data/features) to random configs via successive-halving + early-stopping; order-of-magnitude
  speedups over BO on DL/kernel tasks. *(arXiv:1603.06560)*
- **Successive halving**: eval all configs at small budget → drop worst fraction → double budget for
  survivors; provably beats uniform allocation; beats UCB/EXP3 when algos converge favorably.
  *(AutoML book, ch.1)*
- **BOHB** = BO (TPE-style, KDE surrogate) × Hyperband: strong anytime + final perf; up to 55× over
  random, ~20× over vanilla BO early; can lose when low-fidelity rankings mislead. *(automl.org)*
- **DEHB** = Differential Evolution × Hyperband; robust on high-dim / discrete (NAS) spaces.
  *(arXiv:2105.09821)*
- **Bayesian optimization**: probabilistic surrogate (GP/RF) conditioned on *all* prior evals +
  acquisition fn (EI / UCB `μ+λσ` / PI / Thompson) trading off explore/exploit; preferred when evals
  are expensive. *(distill.pub/2020/bayesian-optimization; Cornell CS4787)*
- **SMAC3**: BO + aggressive racing/intensification + multi-fidelity; RF or GP surrogate.
- **CMA-ES / Differential Evolution**: evolutionary global search; pair well with BO surrogates.
→ *Roadmap Theme A.* LoopLab already has fidelities (`eval_profile` smoke/full) + metric history →
graft ASHA + a TPE/RF surrogate, then fuse (BOHB/DEHB).

## Pass 3 — Coding agents (SWE-bench)
- ✅ **SWE-bench Verified** = 500 human-validated samples (OpenAI + SWE-bench authors); supersedes
  Lite/original for fair eval.
- **GPT-4o** = 33.2% Verified with the **Agentless** scaffold (2× its 16% on original SWE-bench).
- **Agentless** = fixed 3-phase pipeline (hierarchical **localization** → sampled diff-format
  **repair** → **patch validation**), *no* autonomous agent loop; 32% Lite @ **$0.70/issue**; >50%
  Verified w/ Claude 3.5 Sonnet; recipe adopted by OpenAI/DeepSeek. *(arXiv:2407.01489; FSE'25)*
- **Kimi-Dev** (open-source, Agentless-recipe training) = **60.4% Verified**; +SFT → 48.6% pass@1 in
  agent mode (≈Claude 3.5 Sonnet). *(openreview tYppHuGhxJ)*
- ✅ **SWE-agent** = 12.5% pass@1; **the Agent-Computer Interface (ACI) is the key driver** (tuned
  edit/navigate/test interface > raw shell). *(arXiv:2405.15793)*
- **PatchPilot** (fixed human-style workflow) = 53.6% Verified, beats agentic OpenHands 53.0% AND is
  more *stable* across repeats (std-dev 2.5 vs 3.2) at ~$0.99/instance. *(arXiv:2502.02747)*
- ✅-ish **SWE-RM** (execution-free reward model, best-of-k) = Qwen3-Coder 51.6%→62.0% / 67%→74.6%.
- Leaderboards mid-2025: Lite median 31.5% / max 60%; Verified median 46.9% / max 75.2%; SOTA mostly
  proprietary (Claude); no single architecture dominates. *(arXiv:2506.17208)*
- Failure modes: missing multi-location edits, weak logical reasoning, over-aligning to wrong user
  instructions vs env feedback. Long refinement helps (72% of Devin passes took >10 min).
→ *Roadmap Theme C.* For a weak local model: **agentless workflow + good ACI + best-of-N** > agent loop.

## Pass 4 — Reward hacking & eval integrity
- ✅ **(vote 2-0)** Shared-kernel container hardening (seccomp, cap-drop, read-only FS) *improves
  posture but is not an isolation boundary*; untrusted LLM code needs a real boundary — **gVisor**
  (user-space kernel) or **Kata/Firecracker** (microVM). *(zylos.ai; corroborated Docker/Northflank)*
- Specification gaming is well-documented: agents **delete failing tests** instead of fixing bugs,
  **manipulate the task environment**, overwrite evaluator/board state (chess), swap in weaker
  opponents. *(leads, 0-0: arXiv:2502.13295, ImpossibleBench arXiv:2510.20270)*
- **ImpossibleBench** measures a model's *cheating rate* by making the spec conflict with the tests;
  reasoning models attempt to hack at high rates on tampered tasks. *(lead)*
- **Credential leakage**: the dominant mechanism is secrets printed to **stdout** then fed back into
  the LLM context; capability-based isolation recommended. *(lead — directly corroborates review C2)*
- Defense = **layered**: single mechanisms each block only part; combine host-side out-of-process
  grading + held-out-artifact denial + behavioral monitors + real kernel isolation.
→ *Roadmap Theme B.* Validates host-side scoring, out-of-process grader, microVM tier, secret gate.

## Pass 5 — Observability / experiment-tracking UX
- Essential tracker features: run comparison, **sweep viz (parallel-coordinates + hyperparameter-
  importance)**, artifact/dataset versioning + lineage, model registry, collaboration. *(multiple)*
- **W&B** auto-generates 3 sweep views: parallel-coordinates, scatter, **parameter-importance**
  (which params best predict the metric). *(docs.wandb.ai)*
- **MLflow Model Registry** = lineage via Registered Model / immutable Version / mutable Alias
  (@champion/@production); language-agnostic autolog; weaker collaboration. *(uplatz; dagshub)*
- **DVC** = first-class data versioning + git-native PR workflow; MLflow logs only data *hashes*;
  W&B tracks artifacts but not raw-dataset versions. *(techsaas)*
- Reproducibility needs 5 elements: code commit, data version/hash, hyperparameters, metrics,
  environment. *(techsaas)*
- AutoML adoption barrier = lack of transparency; visual analytics + XAI raise trust; **PipelineProfiler**
  lets users compare AutoML pipeline solution-spaces. *(arXiv:2202.11954, 2005.00160, 2001.06509)*
→ *Roadmap Theme F.* LoopLab already has parallel-coords + registry + DAG lineage; add global
param-importance, cross-run sweep aggregation, lineage/provenance export, data-version pinning.

## Pass 6 — Research/scientist layer: ideation, analysis, stopping, cross-run learning *(2026-06-24)*
- ✅ **(3-0) AI co-scientist** (DeepMind, Gemini 2.0) = a **multi-agent specialization pipeline**
  (Generation, Reflection, Ranking, Evolution, Proximity, Meta-review) coordinated by a Supervisor.
  The **Reflection agent is a "virtual peer reviewer"** (correctness/quality/novelty); the **Ranking
  agent runs an Elo "idea tournament"** of pairwise multi-turn scientific debates (new hypotheses seed
  at Elo 1200). The "generate, debate, evolve" loop self-improves. *(deepmind.google blog; arXiv:2502.18864)*
- ✅-ish **(2-1)** The **majority of the co-scientist's compute is spent VERIFYING hypotheses** by
  cross-checking against literature/data to keep them grounded — verification, not generation, is the
  cost center. → mirrors LoopLab's confirm/trust emphasis; supports an explicit verify budget.
- ⚠️ *(leads, rate-limited):* **LLM-generated research ideas were judged MORE novel than expert humans**
  (5.64 vs 4.84, p<0.05) but slightly **weaker on feasibility** (Si et al., ICLR 2025) — ideation is a
  real strength, feasibility-filtering is the gap. **LLM ideation hits a diversity ceiling** (~5%
  non-duplicate of 4000 over-generated ideas). **CAUTION: LLM-as-judge is ≈RANDOM (~50%) at *ranking*
  top vs bottom ideas** (GPT-4o 45–50, AI-Scientist reviewer 43.3, vs ~56 human) — so **do NOT use an
  LLM-judge as the selection oracle**; rank by *empirical* cheap evals (ASHA) / a surrogate, use the
  LLM only as a weak prior + for diversity. **Multi-agent ideation + self-critique** raises idea
  diversity (Non-Duplicate-Ratio 0.69→0.85) but **saturates at ~3 critics / interaction-depth 3.**
  *(ICLR 2025 ea94957d…; arXiv:2507.08350)*
- **Gradient-free cross-run learning:** the co-scientist's **Meta-review agent distills recurring
  critique patterns into a note appended to every agent's prompt next iteration** + a Supervisor
  writes tournament stats to persistent memory. → concrete pattern for LoopLab's cross-run memory →
  **warm-start priors** (Roadmap E4) without any fine-tuning.
→ *Roadmap Theme E.* Validates a Researcher *panel* + Elo/empirical ranking + meta-review-to-prompt
priors; warns against LLM-judge-as-oracle; bounds the panel size (~3).

## Pass 7 — Local-LLM serving & structured-output reliability *(2026-06-24, RTX 5090 32 GB focus)*
*Drives the new Roadmap Theme H. Verification was again partially rate-limited (3 concurrent passes
+ a live engine run), so most claims below are literature-sourced leads, not adversarially voted.*
- **Qwen3-Coder-30B-A3B-Instruct** (MoE, ~3B active, 256K ctx → 1M via YaRN) is the size-appropriate
  top open coding/agentic model for a 32 GB GPU; reported agentic-coding comparable to Claude Sonnet
  among open models; integrates with Qwen Code / CLINE / Claude Code via a dedicated function-call
  format. *(github.com/QwenLM/Qwen3-Coder — lead)*
- **Serving for agentic tool-calling:** Qwen3-Coder's tool/function calling has **dedicated parsers in
  both vLLM and SGLang** → those stacks are recommended over Ollama for robust agentic loops. **vLLM
  guided decoding** drives schema-valid output via `guided_json` (from a Pydantic JSON schema),
  `guided_grammar` (CFG), `guided_regex`, `guided_choice`, over the OpenAI-compatible API; backends are
  XGrammar (caches well for long gens), Guidance (lower per-request latency), Outlines. *Caveat:*
  XGrammar historically **rejects JSON schemas containing `Enum`** ("features not supported by
  xgrammar") on several v0.6–v0.8 releases. *(docs.vllm.ai; redhat developers — leads)*
- **Structured output is highly model-dependent.** On BFCL, native function-calling **collapses on
  small/weak models** (GPT-4o-mini ~19.8% vs GPT-4o ~87%). **Schema-Aligned Parsing (SAP / BAML)** —
  error-correct the raw output instead of hard token constraints — scored **92–94%** and beat both
  native FC and a plain AST parser across all tested models. *(boundaryml.com; gorilla BFCL — leads)*
- **Tool-calling leaderboard (BFCL, Oct-2025):** open-weight **GLM-4.5 (FC) led overall at 70.85%**,
  edging Claude Opus 4.1 (70.36%) / Sonnet 4 (70.29%); GPT-5 59.22%. BFCL V4 (2026-04-12) adds
  holistic agentic eval (web search, memory, format sensitivity). *(klavis.ai; gorilla.cs.berkeley.edu
  — leads)* SOTA tool-calling models still struggle with memory + long-horizon multi-step reasoning.
- **R1/R2 corroboration this round (verified 3-0 unless noted):** MLE-bench = 75 Kaggle comps,
  best o1-preview+AIDE 16.9%; **KompeteAI merge-stage + RAG-from-notebooks/arXiv + predictive scorer
  6.9× faster, +3%**; **AIRA 39.6%→47.7%, operators are the bottleneck (MCTS/evo no gain under AIDE
  ops)**. New sourced leads: **ideation-diversity loss costs 6.9 (greedy)/8.4 (MCTS) pts**; naive
  **parallelism saturates without shared state** (async multi-GPU evolution needed); **best-of-N
  list-wise +~8 pts on GAIA**; **AlphaEvolve = QD program-DB + per-role routing (fast model breadth,
  strong model depth)**. UX (R2, 3-0): **checkpoint time-travel with _forking_** (branch a new run
  from an edited past state) is the top steering pattern; **session-replay + click-to-inspect node
  graph + path-frequency analytics** (AgentOps / Galileo Graph View) are the cockpit features to copy.
→ *Roadmap Theme H* (serving + structured output) and hardening of Themes A/E/F.

## Pass 8 — Operator / action-space design (deepens A0; all 9 claims verified 3-0) *(2026-06-24)*
The best-verified pass. Reference impl: **Meta/FAIR `aira-dojo`** (open source) implements every
mechanism below — A0 is largely a *port*, not research.
- ✅ **Operators > search, re-confirmed by a 2nd paper:** AIRA_2 (arXiv:2603.26499) — advanced search
  only beats greedy "when operators can reliably generate diverse and valid child artifacts."
- ✅ **Isolated operator effect:** AIDE→AIRA operators at **fixed greedy search = 39.8%→45.5%** medals
  (14% rel; best pairing 47.7%). Same policy ⇒ the gain is purely operators. *(2507.02554)*
- ✅ **AIRA's 4 additions beyond draft/debug/improve:** (a) **complexity cue scaled by node child-count**
  (nc<2 minimal / 2–4 moderate / ≥5 advanced) in draft/improve prompts — avoids premature
  over-engineering (repo `draft.yaml` simple/normal/complex branches); (b) scope-aware **memory**; (c)
  explicit think-token reasoning; (d) **Crossover**. *(2507.02554 + aira-dojo)*
- ✅ **Crossover = code recombination:** 2 prior solutions → NL "Crossover Plan" + combined Python (NOT
  param/output averaging). *(aira-dojo `crossover.yaml`)*
- ✅ **Operator-scoped memory:** draft/improve pull **sibling** memories (diversity); debug pulls the
  **ancestral debug-chain** (avoid undo↔redo oscillation). Repo `MEM_OPS`: sibling/ancestral/simple/
  no_memory. *(2507.02554 + aira-dojo)*
- ✅ **MLE-STAR refine_block detail:** extract one pipeline-component code block → ablation study →
  refine only the highest-impact block via an inner loop of **K plans**, repeat with prior attempts as
  feedback. *(2506.15692)*
- ✅ **Ensembling = agent-PROPOSED, iteratively-refined strategy** (not voting / best-of-N): MLE-STAR
  proposes a merge over R rounds using history → **37.9%→43.9%** (beats best-of-N 42.4%). KompeteAI =
  **multi-level merge** (MergeFE + MergeMT, throughout search): removing it drops Lite **47.6%→38.5%**.
  *(2506.15692; 2508.10177)*
- ✅ **Reflection/debug = interactive multi-turn (ReAct), not single-shot:** AIRA_2 replaces fixed
  single-turn operators (a ceiling) with ReAct agents → **+5.5 percentile-rank** (81.5% PR @24h).
  *(2603.26499)*
→ *Roadmap A0.* For LoopLab (already has draft/debug/improve/merge/refine_block/ablate + param-ablation),
highest-leverage ports: **A0d** complexity cue (quick win), **A0c** operator-scoped memory, **A0b**
crossover + agent-proposed merge, **A0a** code-block ablation, **A0e** ReAct debug.

---

*These notes back [ROADMAP.md](ROADMAP.md). Several sources are 2026-dated arXiv IDs surfaced by the
search agents — confirm they resolve before quoting externally.*
