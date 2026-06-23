# Autonomous ML/DS Research Systems — Exploration & Recommendation

**Date:** 2026-06-20
**Doc set:** index [00-INDEX.md](00-INDEX.md) · product [01-product-design.md](01-product-design.md) · architecture [02-architecture.md](02-architecture.md) · decisions [03-decisions.md](03-decisions.md) · file-layout [04-file-layout.md](04-file-layout.md) · *(this doc = research basis)*
**Goal:** Pick the best open-source system for *autonomous ML experiments* — an LLM agent that writes code, trains/evaluates models, tunes hyperparameters, runs experiments, and iterates on results.

**Your requirements (weighting):**
1. **Best results now** on autonomous ML engineering (MLE-bench / Kaggle / DSBench).
2. **Pluggable LLM backend** — must drive both cloud APIs (Claude/OpenAI/Gemini) **and** local/open-weights (vLLM/Ollama/LiteLLM).
3. **Extensible / hackable** architecture I can fork and extend.

> ⚠️ **Read the caveats.** All headline benchmark numbers are **vendor/author self-reported** from each project's own paper/README — no independent third-party replications were found. MLE-bench standing is **time-sensitive**: AIDE's 16.9% is Oct-2024, R&D-Agent's 30.22% is mid-2025. During verification, several over-strong leaderboard claims were *refuted* (see "Refuted claims" at the end).

---

> ## 🔄 2026-06-21 UPDATE — the leaderboard below is stale; read this first
> A fresh SOTA review changed the picture (full detail in [03-decisions.md → ADR-6](03-decisions.md)):
> - **The field jumped to ~60–70% any-medal on MLE-bench full** (e.g. AIBuildAI-2 70.7% / Claude-Opus-4.7, Famou-Agent 2.0 64.4% / Gemini-3-Pro) — **R&D-Agent's 30.22% no longer leads.** Much of the jump is base-model (Gemini-3-Pro, Claude-Opus-4.6/4.7).
> - **The OpenAI leaderboard has been frozen since 2026-04-24 with no independent verification** — every number except the original o1-preview+AIDE **16.9% [IND]** is self-reported; two top entries were flagged for test-set leakage.
> - **The decisive study is Meta FAIR's AIRA** ([arXiv:2507.02554](https://arxiv.org/abs/2507.02554), *not* DeepMind): **operators > search policy** (greedy ≈ MCTS ≈ evo with good operators; greedy best at long budgets), **validation rigor is the #1 lever** (+9–15 pts), and **throughput-based parallel scaling** beats wall-clock.
> - **Ensembling is the best-evidenced single lift** (MLE-STAR 37.9→43.9%; KompeteAI merge ~+9 pts).
> - **Must-haves to be competitive in 2026:** consistent/leakage-proof evaluation · parallel test-time scaling · solution ensembling · rich memory-augmented operators + depth-bounded debugging · an evolving knowledge/retrieval base.
>
> The recommendation below (R&D-Agent / AIDE) is still the right place to **read and learn the loop**, and per-role routing remains validated — but for *raw results* in 2026 the winning ingredients are the techniques above, now reflected in [ADR-6](03-decisions.md) and the architecture.

---

## TL;DR Recommendation
*(Original Oct-2024→mid-2025 framing — see the 2026 update box above for current standings.)*

| Rank | System | Why |
|------|--------|-----|
| 🥇 **Best overall** | **Microsoft R&D-Agent** | Highest verified MLE-bench medal rate (30.22% vs AIDE 16.9%), dual-agent (Researcher + Developer) with **per-role model assignment**, LiteLLM by default (cloud + local), MIT, ~13.5k★ and actively maintained (last push 2026-06-15). Best fit for "best results + pluggable backend." |
| 🥈 **Runner-up** | **AIDE (Weco AI)** | The canonical, *lean*, MIT tree-search scaffold. Smallest/cleanest codebase to fork, model-neutral (OpenAI/Anthropic/Gemini/local), and it's the scaffold OpenAI's MLE-bench and Sakana's AI-Scientist-v2 both build on. Start here if you value hackability over raw score. |
| 🔬 Specialist (AutoML search) | **SELA** (inside MetaGPT) | MCTS over the AutoML pipeline; 65–80% win rate vs baselines. Great if your work is search/AutoML-flavored. |
| 🔬 Specialist (idea→paper) | **AI-Scientist-v2** (Sakana) | End-to-end hypothesis→experiment→manuscript. Built *on top of* AIDE. Not what you asked for (that's "scientific idea→paper"), but adjacent. |

**Bottom line:** Run **R&D-Agent** as your primary engine for best out-of-the-box results, and keep **AIDE** as the lean scaffold you fork when you want to deeply customize the search loop. They share the same tree-search DNA, so skills transfer.

---

## Scoring matrix

Legend: ●●● strong · ●●○ ok · ●○○ weak

| System | Results (MLE-bench) | Backend flex (local+API) | Hackability | Maintenance | ML-experiment focus | Overall |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| **R&D-Agent** | ●●● 30.22% | ●●● LiteLLM | ●●○ medium-large | ●●● ~13.5k★, active | ●●● purpose-built | **🥇** |
| **AIDE** | ●●○ 16.9% | ●●● OpenAI-compat+Ollama | ●●● lean & modular | ●●○ ~1.3k★, slower | ●●● purpose-built | **🥈** |
| **SELA (MetaGPT)** | n/a (own bench) | ●●● Ollama/OpenAI-compat | ●●○ inside MetaGPT | ●●○ big repo, slower | ●●● AutoML search | 3 |
| **AI-Scientist-v2** | n/a | ●●○ multi-provider | ●●○ research code | ●●○ v-line, infra-heavy | ●●○ idea→paper | 4 |
| **MLE-STAR (Google)** | claims SOTA* | ●●○ ADK+LiteLLM, Gemini-first | ●●○ ADK sample | ●●● active | ●●● ML-eng agent | 5 |
| **OpenHands** | lower than AIDE | ●●● LiteLLM | ●●○ general SWE | ●●● ~62k★, active | ●○○ general dev | 6 |
| **smolagents** | n/a (lib) | ●●● best-in-class | ●●● tiny lib | ●●● ~28k★, very active | ●○○ build-your-own | 7 |
| **MetaGPT Data Interpreter** | n/a | ●●● multi | ●●○ | ●●○ | ●●○ DS agent | 8 |
| **AutoGen / AG2** | n/a (lib) | ●●● | ●●○ | ●●● (AG2 active) | ●○○ generic multi-agent | 9 |
| **CrewAI** | n/a (lib) | ●●● LiteLLM | ●●○ | ●●● ~54k★ | ●○○ generic orchestration | 10 |
| **Agent Laboratory** | n/a | ●●○ API-leaning | ●●○ | ●●○ slowing | ●●○ research workflow | 11 |
| **Curie** | n/a | ●●● LiteLLM | ●●○ | ●●○ semi-active | ●●○ experimentation | 12 |
| **AutoKaggle** | n/a | ●○○ API-only | ●●○ | ●○○ **stale 2024** | ●●○ Kaggle | 13 |
| **DS-Agent** | n/a | ●○○ API-only | ●●○ | ●○○ **stale 2024** | ●●○ DS (case-based) | 14 |
| **MLAgentBench** | benchmark | ●○○ | ●●○ | ●○○ **stale 2024** | benchmark, not solver | 15 |

\*MLE-STAR SOTA claim is Google-self-reported and not independently verified here.

---

## Detailed profiles

### 🥇 1. Microsoft R&D-Agent — `microsoft/RD-Agent`
- **Stars / maintenance:** ~13,550★ · last push 2026-06-15 · **actively maintained** · **MIT**.
- **What it does:** Automates data-driven R&D loops (model proposal, factor/feature mining, data mining, a dedicated Kaggle agent). Purpose-built for autonomous data science.
- **Architecture:** **Dual-agent (multi-agent)**. A **Researcher** agent generates ideas from *solution-performance* feedback; a **Developer** agent refines code from *execution-error* feedback. Crucially, **you can assign a different LLM to each role** — the paper runs **o3 as Researcher** (reasoning/ideation) + **GPT-4.1 as Developer** (instruction-following). "By assigning each agent the model that best fits its role, we build a more effective team."
- **Benchmark (verified, 3-0 vote):** MLE-bench **30.22% ± 1.5** overall medal rate with o3(R)+GPT-4.1(D) — (Low 51.52%, Medium 19.3%, High 26.67%) — **vs AIDE's 16.9% ± 1.1** (o1-preview). Nearly double AIDE.
- **Backend flexibility (verified 3-0):** **LiteLLM is the default backend.** Supports OpenAI, Azure OpenAI, DeepSeek (experimental), and "any model supported by LiteLLM" via custom API bases → includes **local vLLM/Ollama**. Separate `CHAT_MODEL` / `EMBEDDING_MODEL` config + per-role assignment.
  - ⚠️ *Known friction:* Issue #1016 reports config pain with Ollama/DeepSeek ("LLM Provider NOT provided"). It's an ease-of-use bug, not a capability gap — but budget setup time for local models.
- **Extensibility:** Medium-to-large codebase, more framework than scaffold. More to learn than AIDE, but the role/loop structure is clean and the payoff is the strongest results.
- **Sources:** [github.com/microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) · [arXiv 2505.14738](https://arxiv.org/html/2505.14738v1) · [MLE-bench paper 2410.07095](https://arxiv.org/abs/2410.07095)

### 🥈 2. AIDE — `WecoAI/aideml`
- **Stars / maintenance:** ~1,326★ · last push 2026-05-02 · active but slower cadence · **MIT**.
- **What it does:** "AI-Driven Exploration in the Space of Code." The canonical lean scaffold for autonomous ML engineering.
- **Architecture (verified 3-0):** Frames ML engineering as a **code-optimization problem** and runs a **solution-space tree search**: each Python script is a **node**, LLM-generated **patches spawn child nodes**, and **metric feedback prunes/guides** the search. Components: Solution Generator, Evaluator, Solution Selector. Ships a Streamlit UI to visualize the solution tree. This is true tree search, not linear iteration.
- **Benchmark (verified 3-0):** Best open scaffold in OpenAI's original MLE-bench paper — **16.9%** with o1-preview; same scaffold across GPT-4o (8.7%), Claude 3.5 Sonnet (5.3%), Llama 3.1 405B (1.9%) — demonstrating backend pluggability. *Note: 16.9% was best-as-of-Oct-2024 and has since been exceeded (e.g. R&D-Agent). "AIDE is current SOTA" was refuted.*
- **Backend flexibility (verified 3-0, code-confirmed):** Model-neutral — OpenAI, Anthropic, Gemini, OpenRouter, and **any local LLM speaking the OpenAI API** (e.g. `OPENAI_BASE_URL=http://localhost:11434/v1` + `agent.code.model=qwen2.5`). Dispatch via dict-router in `aide/backend/__init__.py`; per-step models via `agent.code.model` / `agent.feedback.model`.
- **Extensibility (verified 3-0):** **The most hackable option.** README explicitly invites you to "swap in new search heuristics, evaluators or LLM back-ends." Lean module set: `agent.py` (search), `interpreter.py` (eval), `backend/`, `journal.py`. Caveat: no step-by-step docs for swapping heuristics/evaluators — you fork code, not flip flags.
- **Sources:** [github.com/WecoAI/aideml](https://github.com/WecoAI/aideml) · [arXiv 2502.13138](https://arxiv.org/pdf/2502.13138)

### 🔬 3. SELA — inside `FoundationAgents/MetaGPT`
- **Maintenance:** MetaGPT ~69k★, last push 2026-01-21 (large stable repo, slower cadence) · **MIT**.
- **What it does (verified 3-0):** Tree-search-enhanced AutoML. Uses **Monte Carlo Tree Search (MCTS)** to optimize the AutoML pipeline; pipeline configs are tree nodes; agents run experiments and iteratively refine strategies on feedback. Reports **65–80% win rate** vs traditional + agent-based AutoML baselines across 20 datasets (author-reported, no independent replication).
- **Backend:** Inherits MetaGPT's config — OpenAI, Anthropic, Azure, Gemini, **Ollama**, OpenAI-compatible/local.
- **Fit:** Best if your "experiments" are AutoML/pipeline-search shaped. Lives inside the big MetaGPT repo (more to navigate).

### 🔬 4. AI-Scientist-v2 — `SakanaAI/AI-Scientist-v2`
- **Maintenance:** ~6,612★ · ~13,997★ for v1 · last push 2025-12-19 · custom license.
- **What it does (verified 3-0):** End-to-end autonomous research — generates hypotheses, runs experiments, analyzes data, **writes manuscripts**. Template-free (unlike v1), uses **progressive agentic tree search** managed by an experiment-manager agent. Produced the first fully AI-generated paper accepted at an ICLR 2025 workshop.
- **Key link:** Its tree-search component is **built on top of AIDE** (`bfts_config.yaml`).
- **Fit:** This is "idea→paper," not "autonomous ML experiments." Adjacent to your goal; infra-heavy. Nuance: v2 README admits it "doesn't necessarily produce better papers than v1" when a strong template exists.

### 5. Google MLE-STAR — sample in `google/adk-samples`
- **Maintenance:** repo ~9,696★ · active (push 2026-06-20) · Apache-2.0.
- **What it does:** Google's ML-engineering agent — **web-search-grounded** model selection + targeted refinement loop. Google claims SOTA on MLE-bench (self-reported, **not independently verified here**).
- **Backend:** Via ADK — **Gemini-first**, but LiteLLM gives OpenAI-compatible + local. Worth watching; less neutral than R&D-Agent/AIDE out of the box.

### 6. OpenHands — `All-Hands-AI/OpenHands`
- **Maintenance:** ~62k★ · active · MIT. **Backend:** LiteLLM (excellent — local + ~all APIs).
- **Fit:** General autonomous **software engineer**, not ML-specialized. In MLE-bench it scored **below AIDE**. Use only if you want a general dev agent you adapt to ML, not a purpose-built ML researcher.

### 7. smolagents — `huggingface/smolagents`
- **Maintenance:** ~28k★ · very active · Apache-2.0. **Backend:** best-in-class (LiteLLM, Transformers/local, Inference Providers, OpenAI-compat).
- **Fit:** A *library* for building code agents, not a ready ML-research system. Pick this only if you want to **build your own** scaffold from scratch.

### 8–10. Generic multi-agent frameworks — MetaGPT Data Interpreter / AutoGen·AG2 / CrewAI
- **MetaGPT Data Interpreter:** DS agent inside MetaGPT (~69k★); solid multi-provider/local support.
- **AutoGen (~59k★, CC-BY-4.0) / AG2 (~4.7k★, Apache-2.0, the actively-developed successor):** generic multi-agent programming frameworks; great backend flexibility, but you build the ML loop yourself.
- **CrewAI (~54k★, MIT):** role-based orchestration via LiteLLM (Ollama + all clouds); generic, not ML-specialized.
- **Fit:** Building blocks, not turnkey ML researchers. Reach for these only if you're assembling a custom system.

### 11–12. Research-workflow agents — Agent Laboratory / Curie
- **Agent Laboratory** (`SamuelSchmidgall/AgentLaboratory`, ~5.7k★, MIT, slowing): end-to-end research assistant (lit review→experiments→report). OpenAI/Anthropic/DeepSeek + OpenAI-compatible local.
- **Curie** (`Just-Curieous/Curie`, ~363★, Apache-2.0, semi-active): rigorous automated experimentation; LiteLLM backend (local + cloud).

### 13–15. ⚠️ Stale research artifacts (no commits since 2024) — avoid as a base
- **AutoKaggle** (~305★, Apache-2.0), **DS-Agent** (~234★, no license), **MLAgentBench** (~343★, MIT — a *benchmark*, not a solver). Useful to read for ideas; **don't build on them** — unmaintained and API-only.

---

---

## Addendum: Karpathy `autoresearch` & Recursive (+ techniques to port)

*(Added 2026-06-20 follow-up. Knowledge cutoff caveat: both are 2026 projects post-dating training data; facts below rest on live GitHub data + 2026 write-ups.)*

### A. Karpathy `autoresearch` — ⭐ highly relevant, fully open source
- **Repo:** [`karpathy/autoresearch`](https://github.com/karpathy/autoresearch) · ~**87.8k★** · ~12.7k forks · created 2026-03-06, last commit 2026-03-26 → **a finished demonstration repo, not actively developed**. License: **MIT stated** in README/About but **no `LICENSE` file** (API reports `license: null`) — intent is MIT, not formally committed.
- **What it is:** "AI agents running research on single-GPU nanochat training automatically." A **~630-line**, single-GPU slimmed-down **nanochat**. You give a coding agent a real (tiny) LLM training setup and let it experiment **autonomously overnight**: it edits the training code, trains ~5 min, checks if validation **bits-per-byte (BPB)** improved, keeps/discards, commits via git, repeats. ~**12 experiments/hour** (~100 overnight). Karpathy's own run: ~50 experiments night one (found a better LR), ~700 over two days → ~20 training optimizations surfaced.
- **Architecture (deliberately minimal):**
  - `train.py` — the **single file the agent may modify** (arch, optimizer, hyperparams, batch size).
  - `prepare.py` — fixed data prep (agent doesn't touch).
  - `program.md` — human-written instructions that configure the agent's "research org" (the prompt/policy layer).
  - `analysis.ipynb`, `progress.png` for tracking.
  - Hardware: single NVIDIA GPU (tested H100); community forks add macOS/Windows/AMD (`jsegov/autoresearch-win-rtx`, `thenamangoyal/autoresearch`).
- **Why it matters for you:** This is the **simplest, cleanest, most hackable embodiment of "autonomous ML experiments"** in existence — and it's the *concept reference* for your whole goal. It's a **scaffold/pattern**, not a full framework: no tree search, no multi-agent, no built-in benchmark suite, no sophisticated backend abstraction (the agent loop is driven by whatever coding agent you point at it). Think of it as the **"hello world" you read to understand the loop**, then graduate to R&D-Agent/AIDE for production strength.
- **Sources:** [repo](https://github.com/karpathy/autoresearch) · [The New Stack](https://thenewstack.io/karpathy-autonomous-experiment-loop/) · [DataCamp guide](https://www.datacamp.com/tutorial/guide-to-autoresearch)

### B. Recursive ("Recursive Superintelligence") — ❌ closed system, ✅ techniques worth stealing
- **Company:** SF startup, Richard Socher (ex-Salesforce Chief Scientist); co-founders incl. Yuandong Tian, Tim Rocktäschel, Alexey Dosovitskiy, Josh Tobin, Caiming Xiong, Tim Shi, Jeff Clune; Norvig adviser. Out of stealth ~May 2026, ~$650M @ ~$4.65B (GV, Greycroft, Nvidia, AMD). Goal: recursive self-improvement to automate frontier-AI research.
- **Open source? → NO for the system; partial for results.** Repo [`recursive-org/first-steps-toward-automated-ai-research`](https://github.com/recursive-org/first-steps-toward-automated-ai-research) (Apache-2.0) contains **only output artifacts**: winning NanoGPT speedrun script, **10 of 235** GPU kernels, NanoChat scripts + per-seed metrics. **Not released:** the orchestrator/search engine, evolutionary loop, agent code, evaluator, prompts, weights. *"Artifacts ≠ system."*
- **What it is (method, from a deliberately thin write-up):** an **evolutionary / open-ended LLM-driven search over code** — propose→implement→test→validate→pick-next. Runs many **research threads** over long horizons, keeps **context from prior experiments**, **combines promising branches**, and validates for **reward hacks and variance**. Same family as **DeepMind AlphaEvolve** and **Sakana AI-Scientist** (which they don't name). The one genuinely emphasized idea is a **co-evolving evaluator** that hardens as the search gets stronger. Compute: H100 (8-GPU node for NanoGPT), B200 for kernels.
- **Results (all company self-reported):**
  | Benchmark | Metric | Prior | Recursive | Note |
  |---|---|---|---|---|
  | NanoChat (5-min single-GPU) | val BPB | 0.9372 | **0.9109** | ~1.3× speedup, 10 seeds; baseline is a community sol. *after they removed its reward hacks* |
  | NanoGPT speedrun (8×H100) | time to val loss ≤3.28 | 79.7s | **77.5s** | public leaderboard, p<0.01 — **most checkable**, but margin only 2.2s vs 83+ human records |
  | SOL-ExecBench (B200) | mean SOL score | 0.699 | **0.754** | ~18% gap-to-optimal reduction; only 10/235 kernels public |
  - **No independent reproduction exists.** Leaderboard results (NanoGPT, SOL) are checkable in principle; NanoChat comparison is a chosen/softened baseline. Treat as marketing-adjacent until third-party rerun.

### C. 🔧 Techniques to port into our framework (the actionable payoff)
Ranked by value for an autonomous-ML-research system. These are what separate a toy loop (Karpathy) from a strong searcher (Recursive/AlphaEvolve-class):

1. **Co-evolving evaluator (anti-reward-hacking arms race).** Each time the search "wins" via a benchmark exploit rather than a real gain, add an exploit-detection check and keep a **regression suite of past hacks**. The verifier strengthens in lockstep with the generator. *High value — this is the single most-emphasized Recursive idea and the main failure mode of naive auto-research.*
2. **Multi-seed + variance gating before promoting a result.** Re-run each candidate on N seeds; require statistical significance (e.g. mean improvement at p<0.01) before accepting. Kills "lucky single run" false positives. *High value, cheap to add.*
3. **Branch-and-combine evolutionary search with a persistent archive (AlphaEvolve / MAP-Elites style).** Maintain a population/archive of candidate programs + a store of past experiment summaries fed back to the LLM proposer; merge promising branches; allocate more compute to good lineages (quality-diversity selection). *This is the upgrade path from AIDE's tree search and Karpathy's linear loop.*
4. **Long-horizon parallel research threads.** Many independent trajectories concurrently sharing one archive — more exploration per wall-clock.
5. **Fixed-budget leaderboard objectives as the reward function.** Wrap a benchmark harness (with hard compute/time budget) so fitness = a scalar + validity flags. Clean, gameable-but-checkable signal. (This is exactly Karpathy's BPB-in-5-min and the NanoGPT speedrun setup.)
6. **Single-file edit surface + git-commit-per-experiment** (from Karpathy): constrain the agent to one mutable file, auto-commit each accepted change → clean lineage, trivial rollback, reproducible history.
7. **Muon optimizer (Newton-Schulz orthogonalization)** + the **modded-nanogpt** trick bag (rotary, QK-norm, FlashAttention-3 sliding windows, value embeddings, FP8, multi-token prediction, custom Triton kernels) — directly portable from [KellerJordan/modded-nanogpt](https://github.com/KellerJordan/modded-nanogpt). Good as *seed knowledge* the proposer can draw on. (See also NorMuon, arXiv:2510.05491.)
8. **SOL-normalized GPU-kernel search:** reward = achieved/analytical-optimum with strict correctness + anti-exploit checks; benchmark = NVIDIA [SOL-ExecBench](https://research.nvidia.com/benchmarks/sol-execbench).

**Where to get *open* reference code for these:** [`karpathy/autoresearch`](https://github.com/karpathy/autoresearch) (the loop + edit-surface pattern), [`SakanaAI/AI-Scientist`](https://github.com/SakanaAI/AI-Scientist) / [AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) (agentic tree search), [`WecoAI/aideml`](https://github.com/WecoAI/aideml) (clean tree search to extend with #1–#3), [`KellerJordan/modded-nanogpt`](https://github.com/KellerJordan/modded-nanogpt) (the optimization tricks + speedrun harness).

### D. Updated recommendation given these two
- Your **production engine** is unchanged: **R&D-Agent** (best results + per-role pluggable backend), with **AIDE** as the lean fork target.
- **New insight:** the *architecture you actually want to build toward* is **AIDE/R&D-Agent's tree search + the Recursive-style co-evolving evaluator + multi-seed variance gating + an AlphaEvolve-style archive** (techniques C1–C5). None of the off-the-shelf systems ship all of these.
- **Practical move:** read [`karpathy/autoresearch`](https://github.com/karpathy/autoresearch) first (one evening — it's 630 lines) to internalize the loop, then **fork AIDE** and graft on the evaluator-hardening + variance-gating + archive. That gives you a system in the Recursive/AlphaEvolve family but fully open and yours.

---

## How to choose (decision guide)

- **You want the strongest results with least effort, mixing a reasoning model + a coding model →** **R&D-Agent.** Set o3/Claude-Opus-style model as Researcher, a fast instruction-follower as Developer; point both at LiteLLM (API or vLLM/Ollama).
- **You want a small codebase to deeply own and modify the search loop →** **AIDE.** Fork it, swap heuristics in `agent.py`, evaluators in `interpreter.py`.
- **Your problem is AutoML/pipeline search →** **SELA**.
- **You want idea→experiment→paper →** **AI-Scientist-v2** (which itself sits on AIDE).
- **You're building a bespoke system from primitives →** **smolagents** or **AG2**.

## Suggested adoption path
1. **Week 1:** Stand up **R&D-Agent** with cloud APIs (LiteLLM → Claude/OpenAI) on its Kaggle/MLE-bench loop to get a working baseline and feel the dual-agent split.
2. **Week 2:** Wire in a **local model** (vLLM/Ollama via LiteLLM) for the Developer role to cut cost; keep a frontier model on the Researcher role. (Budget time for Issue #1016-style config friction.)
3. **In parallel:** Clone **AIDE**, run it on the same task. Its lean tree search is the best place to *understand and customize* the core loop, and learnings transfer back to R&D-Agent.
4. **If your work skews AutoML/search:** evaluate **SELA**'s MCTS against R&D-Agent's loop on your datasets.

---

## Refuted / corrected claims (from adversarial verification)
These plausible-sounding statements were **knocked down** during fact-checking — don't repeat them:
- ❌ "R&D-Agent leads the entire MLE-bench leaderboard" (0-3). The well-supported number is **30.22% for the o3+GPT-4.1 config**, not a blanket #1.
- ❌ "R&D-Agent achieves 24.00%" and ❌ "35.1% any-medal" (both 0-3). Use **30.22%**.
- ❌ "AIDE is current/overall SOTA" (refuted). It was best-as-of-Oct-2024; since exceeded.
- ❌ "AIDE wins 4× more medals than OpenHands" (1-2, not supported).
- ❌ "AIDE reports SOTA across Kaggle + MLE-bench + RE-bench" (1-2, not supported).

## Open questions / what to verify before committing
- Current **2026 MLE-bench leaderboard** — where do newer R&D-Agent versions, MLE-STAR, and AIDE-with-frontier-models rank *now*?
- Has anyone **independently reproduced** R&D-Agent's 30.22% / AIDE's 16.9% outside the originating labs — and do they hold with **local open-weights** rather than frontier APIs?
- Real-world **local-model ergonomics** for R&D-Agent (Issue #1016) — test before relying on Ollama/vLLM in production.

## Primary sources
- R&D-Agent: https://github.com/microsoft/RD-Agent · https://arxiv.org/html/2505.14738v1
- AIDE: https://github.com/WecoAI/aideml · https://arxiv.org/pdf/2502.13138
- MLE-bench (OpenAI): https://arxiv.org/abs/2410.07095 · https://github.com/openai/mle-bench
- SELA: https://arxiv.org/abs/2410.17238
- AI-Scientist-v2: https://github.com/SakanaAI/AI-Scientist-v2
- DS-agent survey (taxonomy): https://arxiv.org/html/2508.02744v1
- MLE-STAR: https://research.google/blog/mle-star-a-state-of-the-art-machine-learning-engineering-agents/
