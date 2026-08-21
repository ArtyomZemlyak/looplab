# 50. LoopLab — Benchmark Landscape + a Local Test Plan (2026-08-19)

> **Status: analysis + measurement.** Point-in-time external sweep in the
> [doc 13](13-external-works-analysis-2026-07.md) / [doc 41](41-external-works-synergy-2026-08-14.md)
> line, plus **measurements taken on this box on 2026-08-19**, re-verified against `master`
> `6fbd263b`. Nothing here flips a default.
> Companion authorities: [docs/MLEBENCH.md](MLEBENCH.md) (the runbook),
> [doc 27](27-agent-system-mega-review-2026-08-09.md) §4 (the agent eval ladder, still open),
> [doc 41](41-external-works-synergy-2026-08-14.md) §3/§8 (the "run the benchmark" recommendation).

**The question.** What benchmarks exist for agentic R&D systems, has anyone published a
consumer-GPU (4090-class) result, and what is the cheapest credible way to get a LoopLab number on
*this* box that can be compared against an existing framework.

**TL;DR.**

1. The gap doc 41 §3 names is still the gap: **LoopLab has no published benchmark number at all.**
2. **Yes, someone did it on a 4090** — Frontis-MA1 / OpenMLE (arXiv 2607.28568,
   `github.com/FrontisAI/OpenRSI`) reports MLE-Bench Lite under a **12 h/task budget on one RTX 4090
   capped at 12 GB VRAM**. That is the reference setup to copy, and it is *below* this box's specs.
3. **The pipeline already works here, today, offline.** Measured 2026-08-19: a real `mlebench_real`
   run graded by mle-bench's own grader against the held-out answers, with real medal thresholds.
4. The honest constraint is **the model, not the hardware**: leaderboard numbers use
   Gemini-3-Pro / Claude-Opus / GPT-5.5. A local Qwen3-30B-A3B-Q4 absolute number is not comparable
   to them. What *is* comparable is a **same-model, same-budget head-to-head against AIDE**.

---

## 1. What the docs already cover

| Doc | Benchmark content |
|---|---|
| [MLEBENCH.md](MLEBENCH.md) | The real-Kaggle runbook: `kind="mlebench_real"`, host-side out-of-process grading, Bearer-token download, per-competition rules acceptance. Three competitions named. |
| [doc 03](03-decisions.md) ADR-6 | Field moved to ~60–70 % any-medal on MLE-bench full; R&D-Agent 30.22 %, AIDE 16.9 %. |
| [doc 10](10-autoresearch-improvement-research.md) | Leaderboard clustering 61–64 %; **pass@1 16.9 % → pass@8 34.1 %** — more attempts ≫ more compute per attempt. |
| [doc 11](11-agent-systems-research.md) | Arbor 86.4 % MLE-bench-Lite; B6 held-out/generalization-gap named as the most-validated missing piece. |
| [doc 13](13-external-works-analysis-2026-07.md) | MARS 62.67 % All (no framework code released); **AgentDS** (17 domain DS challenges) named as a ready-made eval target for the Genesis/deep-research half. |
| [doc 27](27-agent-system-mega-review-2026-08-09.md) §4 | The five-tier **agent eval ladder** (routing → trajectories → outcomes → injection/confused-deputy → stochastic trials with CIs). Re-verified **STILL OPEN** on 2026-08-14. |
| [doc 41](41-external-works-synergy-2026-08-14.md) §3, §8 | Frontis-MA1/OpenMLE; the explicit finding *"LoopLab has zero real MLE-bench runs with a private held-out grader … no published number"*; step 5 of the recommended order is one MLE-Bench Lite run reporting **raw and hack-adjusted** scores. |
| [BACKLOG.md](BACKLOG.md) Theme D | **D1 real MLE-bench — shipped** (`adapters/mlebench_real.py`). **D2 self-benchmark harness — shipped** (`looplab bench`, `cli/export_cmds.py:66`). |

**What the docs do NOT have:** anything published after 2026-08-14, and no plan sized to *this*
box's actual constraints (8 CPUs, Windows, one GPU).

## 2. External landscape — the 2026-08-19 sweep

New or not previously recorded here. Sizes/claims are from abstracts and search snippets unless
marked measured.

### 2a. ML-engineering benchmarks (fixed metric, medal-graded)

| Benchmark | Shape | Why it matters here |
|---|---|---|
| **MLE-bench** (2410.07095) | 75 Kaggle comps; **Lite = the 22 low-complexity ones** (`experiments/splits/low.txt`) | The default currency. LoopLab already speaks it end to end. |
| **MLE-bench Lite** | 22 comps, official budget **1 GPU + 24 h/comp** | The realistic target for one box. |
| **MLE-Dojo** (2505.07782) | Gym-style *interactive* POMDP over 200+ comps | Alternative if we ever want RL-shaped evaluation; heavier to wire. |
| **DSBench** | 466 analysis + 74 modelling tasks | Broader, less medal-comparable. |
| **OpenMLE-Gym** (2607.28568) | **5,758 execution-verified MLE tasks** | The regression corpus doc 27 §4 / doc 28 DR-11 say is missing. Released. |

### 2b. Research-loop benchmarks (what LoopLab actually claims to be)

| Benchmark | Shape | Note |
|---|---|---|
| **RE-Bench** (METR) | 7 open-ended research-engineering envs + 71 8-h human-expert attempts | The only one with a calibrated *human* baseline. Heavy. |
| **PaperBench** (2504.01848) | From-scratch replication of top-tier ML papers | Very expensive; leaderboard led by Qwen3.8 Max 0.930 (Aug 2026). |
| **MLR-Bench** | 201 open-ended research tasks from NeurIPS/ICLR/ICML workshops | LLM-judged, cheap to *run*, expensive to *trust*. |
| **FML-bench** (2510.10472, `github.com/qrzou/FML-bench`) | **8 fundamental ML research tasks**; scores the *process*, incl. an **Exploration Diversity** metric | Smallest credible research-loop benchmark. Directly measures what LoopLab's search claims. |
| **ResearchGym** (2602.15112) | 5 containerized envs from ICML/ICLR/ACL papers, 39 sub-tasks | Headline finding is a *capability–reliability gap*: agents beat the baseline in **1 of 15** evaluations (6.7 %), 26.5 % of sub-tasks completed. |
| **NatureBench** (2607.28568) | 90 containerized tasks from Nature-family papers, "Match-SOTA" scored; Lite subset used as held-out transfer | Released with OpenRSI. |
| **AgentDS** (2603.19005) | 17 industry DS challenges, deliberate val→test shift | Already recommended in doc 13; harness availability still unconfirmed. |
| **ResearchClawBench** (2606.07591), **Act As a Real Researcher** (2606.07462) | End-to-end research-lifecycle suites | New, unvetted. |

### 2c. Systems worth comparing against

| System | Number | Code |
|---|---|---|
| **AIDE** (2502.13138) | 16.9 % full (o1-preview); **39.6 % Lite** as greedy baseline | `github.com/WecoAI/aideml`, **MIT**, `pip install aideml`, pure Python, honours `OPENAI_BASE_URL`. The standard comparison and the cheapest one. |
| **AIRA** (2507.02554, Meta) | **39.6 % → 47.7 % Lite** — the finding is that *AIDE's operators, not its greedy search, are the limit*: MCTS/Evo over AIDE operators gave no significant gain | **`github.com/facebookresearch/aira-dojo`** — a harness that ships `AIDE_GREEDY`, `AIRA_GREEDY`, `AIRA_MCTS`, `AIRA_EVO` as configs over MLE-bench Lite, LLM clients via LiteLLM (custom base URL OK). **CC BY-NC 4.0**; Slurm + Apptainer/singularity, i.e. Linux. |
| **AIRA₂** (2603.26499) | Sequel: "overcoming bottlenecks in AI research agents" | Meta, 2026. |
| **R&D-Agent** | 30.22 % full | Open. |
| **MLE-STAR** (2506.15692) | ~64 % Lite | Google. |
| **MARS+** (2602.02660) | **62.67 ± 0.77 % full** | Trajectories only — **no framework code**. |
| **ML-Master 2.0 / HCC** (2601.10402) | 56.44 % full, 24 h | — |
| **Frontis-MA1 / OpenMLE-Evo** (2607.28568) | **Lite 39.39 % → 60.61 %, 71.21 % Evo-Max** | `github.com/FrontisAI/OpenRSI`, CC BY-NC 4.0, weights + gym + RL + search released. |
| **MLEvolve** | — | `github.com/InternScience/MLEvolve` |

## 3. The 4090 question — answered

**Frontis-MA1 / OpenMLE is the consumer-GPU reference point**, and it is the tightest one published:

- **one RTX 4090 capped at 12 GB VRAM**, **12 h per task**, MLE-Bench Lite;
- Medal Average **39.39 % → 60.61 %** (base model → trained model), **71.21 %** with OpenMLE-Evo-Max
  — reported above GPT-5.5 + Codex on the same setup;
- held-out NatureBench Lite: Match-SOTA 50 % → 70 % swapping in the trained model, 20 % → 50 %
  swapping in the search framework;
- full stack public: OpenMLE-Gym, OpenMLE-ERL, OpenMLE-Evo, Frontis-MA1 weights (35B + 30B),
  SFT traces. **License CC BY-NC 4.0 — non-commercial only.**

For contrast, the *typical* MLE-bench harness allocation in other papers is **2 × RTX 4090 + 32 CPU
cores, 24 h/task**; the official Lite setting is **1 GPU, 24 h**. So a 12 GB cap is deliberately
austere — this box is strictly above it on VRAM and RAM, and **below it on CPU** (see §4).

## 4. What this box can actually do — measured 2026-08-19

| Resource | Value | Verdict |
|---|---|---|
| GPU | RTX 5090, **32,607 MiB**, driver 595.79 | 2.7× the Frontis 12 GB cap. Fine. |
| RAM | 103 GB | Fine. |
| **CPU** | **8 logical** | **The real handicap** — papers assume 32–36. Hits CPU-bound tabular comps hardest. |
| Disk | 517 GB free on C: | Enough for a small-comp subset, not for the whole Lite split. |
| LLM | Ollama up: `qwen3:30b-a3b` (Q4_K_M, 18.5 GB, 262k ctx, tools+thinking), `qwen3:8b` | Usable. Docker/SGLang **not** running. |
| Kaggle auth | `check_auth() -> True` | Downloads work. |
| mle-bench | installed from `C:\Users\artyo\Documents\mle-bench-src` | Host grader available. |

**Prepared competition data already on disk:**

| Competition | prepared size | public files | private |
|---|---|---|---|
| spooky-author-identification | 3.3 MB | description, train/test.csv, sample_submission | `test.csv` (answers) |
| nomad2018-predict-transparent-conductors | 16 MB | + `train/`, `test/` geometry dirs | `test.csv` |
| detecting-insults-in-social-commentary | **0 bytes** | — | — (rules not acceptable on this account) |

**End-to-end smoke, run today:**

```
python -m looplab.cli run examples/mlebench_real_spooky.json --out <tmp> --backend toy --max-nodes 3
-> finished=True  nodes=3 evaluated=3  BEST node 0: metric=0.47123
```

and, on the same competition with `--backend llm` against local Ollama `qwen3:30b-a3b`
(`--max-nodes 2`, completed 2026-08-19, 802 s):

| node | metric | repairs | vs median 0.418785 |
|---|---|---|---|
| 0 | **0.58541** | 2 crashes | below |
| 1 | 1.09206 | 1 crash | far below |

**The LLM run lost to the numpy baseline** (0.58541 vs 0.47123, lower is better), and its second
node was worse than its first. Two nodes is far too small to judge a search policy — but it is
ample evidence about the *model*: a local Q4 30B-A3B produces code that crashes three times in two
nodes and never reaches a trivial baseline. This is the empirical case for §5a: on a model this
weak the head-to-head would measure which framework best survives broken code, not which searches
better.

with a real grading report per node:

```json
{"competition_id": "spooky-author-identification", "score": 0.47123,
 "gold_threshold": 0.16506, "silver_threshold": 0.26996, "bronze_threshold": 0.29381,
 "median_threshold": 0.418785, "any_medal": false, "above_median": false,
 "valid_submission": true, "is_lower_better": true}
```

So the **offline numpy baseline sits at 0.47123 vs a median of 0.418785** — below median, with
bronze at 0.29381. There is real headroom for an LLM-driven run to show a delta, and the delta is
measured by mle-bench's own grader against answers the candidate cannot read.

### 4a. Measured MLE-bench Lite download sizes (Kaggle API, 2026-08-19)

Queried through `adapters/kaggle_dl.py` with the box's own token. **Reliable rows only** — the six
image competitions returned exactly 20 files (API page cap), so their totals are truncated and are
excluded rather than reported wrong.

| GB | Competition | CPU-friendly? |
|---:|---|---|
| 0.002 | spooky-author-identification | ✅ **prepared** |
| 0.003 | detecting-insults-in-social-commentary | ⛔ rules blocked here |
| 0.006 | nomad2018-predict-transparent-conductors | ✅ **prepared** |
| 0.018 | random-acts-of-pizza | ✅ tabular/text, trivial |
| 0.025 | aerial-cactus-identification | ⚠️ small images, GPU helps |
| 0.036 | leaf-classification | ✅ tabular features |
| 0.055 | jigsaw-toxic-comment-classification-challenge | ✅ text |
| 0.059 | denoising-dirty-documents | ⚠️ small images |
| 0.096 | text-normalization-challenge-english-language | ✅ text |
| 0.126 | text-normalization-challenge-russian-language | ✅ text |
| 0.293 | the-icml-2013-whale-challenge-right-whale-redux | ⚠️ audio |
| 0.574 | tabular-playground-series-may-2022 | ⚠️ CPU-heavy on 8 cores |
| 0.585 | mlsp-2013-birds | ⚠️ audio |
| 0.693 | tabular-playground-series-dec-2021 | ⚠️ CPU-heavy on 8 cores |
| 0.855 | dogs-vs-cats-redux-kernels-edition | ⚠️ GPU images |
| 5.699 | new-york-city-taxi-fare-prediction | ⚠️ 8-core hazard |
| *n/a* | dog-breed-identification, plant-pathology-2020-fgvc7, histopathologic-cancer-detection, aptos2019-blindness-detection, ranzcr-clip-catheter-line-classification, siim-isic-melanoma-classification | **not measured** (API page cap); all multi-GB image comps |

**Totals (re-derived 2026-08-19):** the 10 cheapest reachable rows sum to **0.716 GB**; dropping
`the-icml-2013-whale-challenge` (0.293 GB, audio, the single largest of the ten) leaves **9
competitions at 0.423 GB**. Either way a workable subset of MLE-bench Lite is well under a
gigabyte on this box — the cost is wall-clock, not disk.

## 5. Options

Ordered by effort. Each is independently shippable; C is the one that answers the actual question.

### Option A — first real number, today (~2 h, zero new infra)

Run the two already-prepared competitions with `--backend llm` against the local Ollama
`qwen3:30b-a3b`, several seeds each.

```bash
export LOOPLAB_LLM_MODEL=qwen3:30b-a3b   # base_url already defaults to Ollama's /v1
python -m looplab.cli run examples/mlebench_real_spooky.json --out runs/mle-spooky-01 --backend llm --max-nodes 12
python -m looplab.cli run examples/mlebench_real_nomad.json  --out runs/mle-nomad-01  --backend llm --max-nodes 12
```

**Yields:** the first LoopLab number graded by a private held-out grader, plus a real exercise of
`read_fence` / `metric_subject` / `metric_salvage` on a non-synthetic task (doc 41 §8 step 1).
**Does not yield:** anything leaderboard-comparable — N=2 is not MLE-bench Lite.

> **Do this on `6fbd263b` or later, not before.** `db470130` (merged 2026-08-19) fixed
> `engine/cadence.py::at_creation_boundary`: since backlog F1f hoisted the eval task group to run
> scope, `strategy_decision` / `coverage_snapshot` / the whole Part IV-V concept classifier pass all
> opened with `if state.pending_nodes(): return False` and therefore **fired at most once per run,
> in the end-of-run drain** — measured 0 firings ever on v7, v9 and the live v2. A benchmark run
> started on the previous tree would have been scored with a Strategist that adapted nothing, i.e.
> it would measure a *different harness* than the one we mean to publish.

### Option B — a named, honest Lite subset (~1–2 days)

Accept Kaggle rules for the cheap missing comps, prepare them, and report
**"MLE-bench Lite subset (N=…)"** with the members listed. All CPU-tractable on 8 cores.

| Set | Members | Download |
|---|---|---|
| **N=9** (recommended) | spooky ✓, nomad ✓, random-acts-of-pizza, aerial-cactus-identification, leaf-classification, jigsaw-toxic-comment, denoising-dirty-documents, text-normalization-{english, russian} | **0.423 GB** |
| N=10 | + the-icml-2013-whale-challenge-right-whale-redux (audio, 0.293 GB) | 0.716 GB |

Prefer N=9: whale is audio, i.e. a modality alone in the set, and it nearly doubles the download
for one competition. `detecting-insults` (0.003 GB) would be the natural tenth but its rules are
**not acceptable on this Kaggle account** — download stays 403, and its prepared dir here is 0 bytes.

**Never report a subset number as "MLE-bench Lite"** — the 22-comp split is the published unit and
a subset is a different population. Name it, list it, and say why each member was chosen (size).

### Option C — the head-to-head (*this is the comparison being asked for*)

Same subset, same LLM endpoint, same node/wall-clock budget, same seeds. Two tiers, and the cheap
one is much cheaper than it first looked:

**C1 — LoopLab vs AIDE (~2–3 days). Windows-native, no new infra.**
`pip install -U aideml` (MIT, pure Python), then point it at the same Ollama endpoint LoopLab uses:

```bash
export OPENAI_BASE_URL="http://localhost:11434/v1"
export OPENAI_API_KEY="local"
aide data_dir="<prepared>/public" goal="…" eval="…" agent.code.model="qwen3:30b-a3b" agent.feedback.model="qwen3:30b-a3b"
```

AIDE's published Lite number (39.6 % greedy) is the reference, and its data-dir contract is
Kaggle-shaped, i.e. the same `public/` split `mlebench_prep` already writes. **Start here.**

**C2 — LoopLab vs the AIRA solver family (~4–6 days, higher fidelity, higher friction).**
`aira-dojo` ships `AIDE_GREEDY` / `AIRA_GREEDY` / `AIRA_MCTS` / `AIRA_EVO` as configs over MLE-bench
Lite with LiteLLM base-URL support — four baselines for one integration, including the 47.7 % SOTA
point. Costs: **CC BY-NC 4.0**, and Slurm + Apptainer/singularity means Linux.

> **Measured WSL caveat (2026-08-19).** WSL2 `Ubuntu-22.04` exists here and **does see the RTX 5090**
> (`nvidia-smi` returns it), 8 cores / 46 GB visible. But apptainer, singularity and slurm are all
> absent, and **Ollama is not reachable from WSL**: it binds `127.0.0.1`, and WSL2 NAT does not
> proxy host-localhost. Fix is `OLLAMA_HOST=0.0.0.0` plus a Windows firewall rule, then the host IP
> from `ip route show default` (measured `172.23.80.1`), *not* `localhost`. Budget this.

Report per competition: score, medal tier, above-median, wall-clock, tokens, $-equivalent.

**Why this is the credible comparison and an absolute number is not:** with a local Q4 30B-A3B,
both systems will land far below published leaderboard figures. Holding the model fixed makes the
*framework* the only variable — which is exactly the question. It also directly mirrors Frontis's
own ablation shape ("with the framework fixed, swap the model; with the model fixed, swap the
framework").

### Option D — the differentiator run (fold into B/C, ~+0.5 day)

Report **raw Medal Average beside a hack-adjusted one**: nodes excluded by
`trust/leakage.py`, `trust/reward_hack.py`, `engine/metric_salvage.py`'s `metric_unmeasured`, and
unbound `runtime/metric_subject.py` subjects. Per doc 41 §3 this is **a number nobody else on that
leaderboard can produce**, and it costs almost nothing once C exists — the gates already run.

### Option E — the research-loop half (~1–2 days, complementary)

MLE-bench measures ML *engineering*. LoopLab's differentiated claim is the *research loop*
(Genesis framing, deep research, Strategist pivots, hypothesis cards). **FML-bench** (8 tasks,
Exploration Diversity) is the smallest benchmark that scores that, and **ResearchGym**'s failure
taxonomy — impatience, poor resource management, overconfidence in weak hypotheses, difficulty
coordinating parallel experiments — is a ready-made checklist to score our own traces against.
Cheaper than PaperBench/RE-Bench by an order of magnitude.

### Option F — ingest OpenMLE-Gym as a regression corpus (~2–3 days, later)

5,758 execution-verified tasks behind the existing `TaskAdapter` contract. This is the doc 27 §4 /
doc 28 DR-11 corpus. **License is CC BY-NC 4.0 — non-commercial only**, which is a product
decision, not just an engineering one.

## 5a. Comparison protocol — where the LLM runs, and why it is not on the 5090

**Decision: the 5090 is the CANDIDATE's GPU. The agent's LLM comes from OpenRouter, off-box.**
Three independent reasons, the second one decisive.

**(1) Measured contention.** During the Option-A verification run above, with the LLM served by
local Ollama and the task being CPU-only `spooky` (3.3 MB of text), `nvidia-smi` read
**23,256 / 32,607 MiB and 92 % utilization**. All of it was the LLM. A competition that actually
wants the GPU would be left ~9 GB and would contend for SMs with the agent's own thinking.

**(2) The confound is architecture-correlated — this is the real reason.** If the LLM shares the
card with candidate training, then *a framework that makes more LLM calls steals more compute from
its own candidate*. Measured on the completed verification run: **110 calls / 501,044 tokens /
802 s wall-clock for TWO evaluated nodes** (plus three crash repairs) on a trivial text task,
because LoopLab pays a Genesis/research phase (~22 calls before the first node), a Researcher, a
Developer, a Strategist, two live watchdogs and a concept classifier. AIDE's draft/debug/improve
loop is structurally far leaner. Co-hosting would therefore penalize LoopLab *in proportion to its own architecture*, and the
benchmark would answer "which framework talks less", not "which framework searches better". A
confound that scales with the thing under test is the one kind that cannot be corrected afterwards.

**(3) Every published baseline already does it this way.** Frontis's "one RTX 4090 capped at 12 GB
VRAM" is the *task* GPU — a 35B model cannot be served in 12 GB, so their model was served
elsewhere. AIDE/AIRA use API models. MLE-bench's own setting gives the agent a GPU for training and
treats the LLM as an API. Serving the LLM on the task card is a deviation from every setup we want
to be commensurable with.

### The OpenRouter traps, and the one fix for all of them

| # | Trap | Consequence |
|---|---|---|
| T1 | OpenRouter routes one model name across providers with **different quantizations**. `order` alone is only a try-preference — it still falls through without `allow_fallbacks: false`. | The same config silently draws a different backend on a different day; a framework delta becomes model variance. |
| T2 | **In LoopLab specifically:** the only seam for injecting `provider` is `Settings.llm_reasoning_extra`, which rides the REASONING channel. `core/llm.py:1174` sets `self._reasoning_ok = False` **permanently for that client** on any reasoning-param rejection, and lines 834 / 1468 then stop sending `extra_body` **at all** — pin included. | One 400 mid-run silently un-pins the provider for the remaining hours, fallback routing resumes, and nothing in the event log records it. |
| T3 | AIDE has no provider-pinning knob at all. | Even a correct LoopLab pin would not be matched on the other side — i.e. no parity. |

**One fix for all three: a local LiteLLM proxy in front of OpenRouter.** Both frameworks point at
`http://localhost:<port>/v1`; the proxy injects the `provider` block on every request, where
neither framework's degrade paths can switch it off:

```yaml
model_list:
  - model_name: bench-model
    litellm_params:
      model: openrouter/<vendor>/<model>
      extra_body:
        provider: {order: ["<slug>"], allow_fallbacks: false, quantizations: ["fp8"]}
```

Bonus, and it is a large one: the proxy becomes the **single meter** — identical token counting,
identical `$` accounting and one call log for both frameworks, which is what makes the budget
numbers in §5a comparable at all.

> **Verify the pin; do not trust it.** LiteLLM has open bugs on `extra_body` precedence
> (BerriAI/litellm #18039, #18061 — `default_litellm_params.extra_body` can overwrite the
> per-model one). Before each campaign, probe the proxy directly N times and assert OpenRouter's
> returned `provider` field is constant, and re-check it on the activity dashboard afterwards.

### VERIFIED LIVE, 2026-08-19 — the drift is real and the pin works

Measured against OpenRouter with this box's own key, model `deepseek/deepseek-v4-flash-0731`:

**The hazard, in three requests.** Unpinned, the same prompt drew **two different providers**
(Sail Research ×2, Inceptron ×1 — both **fp4**) and returned **96 / 17 / 96** completion tokens.
That is the whole §5a argument reproduced in under a minute: one model slug, three requests, two
backends, wildly different outputs.

**Twenty-four endpoints serve this one slug**, at `fp4` / `fp8` / `bf16` / `unknown`, context
131,072 → 1,310,720. Choosing "the model" chooses none of that.

**The pin holds, and fails the right way.** `provider: {order: [X], allow_fallbacks: false}` returned
exactly one provider across 3 calls for each of three pins. When the pinned endpoint was down
(DeepInfra, 502) it **refused the request** rather than silently rerouting — which is the required
behaviour: a benchmark must stop rather than quietly change backends mid-run.

**Provider selection, measured** (3 calls each, same prompt, `max_tokens=300`, `temperature=0`):

| pin | tok/s | mean | price in/out | uptime 30m |
|---|---|---:|---|---|
| **`siliconflow/fp8`** ← chosen | 34.5 / 67.6 / 54.5 | **~52** | $0.140 / $0.280 | 97.8 % |
| `streamlake/fp8` | 29.7 / 47.0 / 30.2 | ~36 | $0.079 / $0.157 | 94.0 % |
| `gmicloud/fp8` | 26.6 / 14.9 / 28.8 | ~23 | $0.112 / $0.224 | 99.5 % |
| `deepinfra/fp8` | — | — | 502 Bad Gateway | 99.0 % (stale) |
| `baseten` / `parasail` / `baidu` fp8 | — | — | 429 Too Many Requests | — |
| `novita/fp8` | hung 300 s, 0 tokens | — | — | 99.5 % (stale) |

Note the published `uptime_last_30m` did **not** predict availability — DeepInfra at 99.0 % was
down and Novita at 99.5 % hung. **Probe the endpoints yourself before a campaign**; the catalogue
is not evidence. Sample is 3 calls per provider, so treat the ordering as indicative, not tight.

**Model choice: the dated slug, not the alias.** `deepseek/deepseek-v4-flash-0731` rather than bare
`deepseek/deepseek-v4-flash` or `~deepseek/deepseek-v4-flash-latest` — the `-0423` snapshot named in
the literature has already vanished from the API, which is direct evidence the bare pointer moves.

### What must be IDENTICAL (pin it)

1. Model, **provider and quantization** — via the proxy, verified.
2. `temperature`, `top_p`, `max_tokens`, context window.
3. GPU available to the candidate — the whole 32 GB, *or* capped, but the **same** either way.
   (Do **not** cap to 12 GB to "match Frontis": their number is unreachable anyway on a different
   model, and capping only shrinks what we can measure.)
4. CPU / RAM (8 / 103 GB here for both).
5. Task set and the exact prepared split — mle-bench's own `public/`.
6. The grader — mle-bench's, host-side, out of process.
7. **`eval_parallel = 1`.** LoopLab can evaluate nodes concurrently and AIDE cannot; on one GPU that
   is simultaneously an advantage (wall-clock) and a handicap (contention). Pin serial for the
   head-to-head, then report "LoopLab, parallel" as a *separate labelled row* if wanted.
8. Seeds: N ≥ 3, the same seeds for both (MARS reports 3).

### What must be REPORTED, not equalized

Tokens, `$`, wall-clock, LLM-call count, crash/repair count. **These are the result, not noise** —
"LoopLab reaches the same score for 4× the tokens" is a finding, and flattening it hides the
finding.

### Which budget is primary

Three candidate units and they disagree. **Wall-clock per competition is primary**, because that is
what MLE-bench specifies (24 h; Frontis used 12 h) and what every leaderboard number means. A node
or step count is *not* comparable — a LoopLab "node" and an AIDE "step" are different objects.
Tokens/`$` ride along as a reported result. On a wall-clock budget, "LoopLab thinks more" is a
legitimate strategy rather than a rules violation, which is the right framing.

### Residual asymmetries — accept and state, do not paper over

- **LoopLab's trust gates reject nodes AIDE would keep.** Raw medal rate can therefore come out
  *lower by design*. This is exactly why Option D reports raw **and** hack-adjusted.
- **LoopLab pays a research phase AIDE has no equivalent of** (~22 calls before node 0, measured).
  Under a wall-clock budget that is LoopLab's own choice to fund.
- **8 CPUs vs the 32–36 papers assume**, biting hardest on CPU-bound tabular comps.

### Cost order-of-magnitude

Measured on the completed verification run: **501,044 tokens / 110 calls / 802 s for 2 evaluated
nodes** on the *smallest* competition in the set — i.e. **~250 k tokens per node**, one-time
research phase and 3 crash repairs included. Extrapolating at ~20 nodes/competition gives ~3–5 M
tokens per competition-run, so **9 comps × 3 seeds ≈ 80–135 M tokens per framework**.

That is roughly **$25–40 at cheap blended rates and $400–700 at frontier input/output pricing**, per
framework — double it for a two-framework head-to-head. Two things push the real figure *up*, not
down: spooky is the smallest task here (bigger tasks carry longer code and more context per call),
and 3 crash repairs across 2 nodes is a rate a weak model inflates. One thing pushes it down: the
research phase is one-time per run, so the marginal per-node cost is below 250 k.

**So the model tier is a budget decision to take BEFORE the campaign, and pinned identically for
both sides.** It is also the single largest lever on the whole exercise — see the result below.

## 5aa. WHO was compared, on WHAT, with WHICH model — the matrix that decides the benchmark

A benchmark is only useful to us where **several frameworks were run on the SAME model**. A table of
frameworks each on its own frontier model measures models, not frameworks, and we cannot join it.

| Benchmark | Frameworks compared | Model | One model for all? | Hardware | Gated data? |
|---|---|---|:-:|---|:-:|
| **FML-bench** (2605.17373, 2026) | **AI Scientist v1, AI Scientist v2, AIDE, AIRA, Autoresearch, OpenEvolve, AdaptiveSearch** — **seven** | **GPT-5.4** | ✅ **yes, all seven** | A100-80GB | ❌ **none** — public research repos, one `setup.py` |
| **MLE-bench Lite** (AIRA 2507.02554) | AIDE-greedy, AIRA-greedy, AIRA-MCTS | **DeepSeek R1** (o1-preview / o3 partial) | ✅ mostly | 1× **H200**, 24 CPU, 100 GB | ✅ **Kaggle** |
| MLE-bench full (leaderboard) | MARS+, ML-Master 2.0, Frontis-MA1, MLE-STAR, R&D-Agent | Gemini-3-Pro / GPT-5.5 / own 35B / … | ❌ **all different** | varies | ✅ Kaggle |
| PaperBench, MLR-Bench | models, not frameworks | various | — | — | — |

**Reference numbers we could join.**
FML-bench, all on GPT-5.4, 18 tasks × 3 rounds, T=100 steps, mean normalized test improvement:
AdaptiveSearch **0.208**, TAS v2 **0.193**, Autoresearch **0.192** (AIDE / AIRA / TAS v1 / OpenEvolve below).
MLE-bench Lite (AIRA, 1× H200, 24 h): AIRA-MCTS (R1) **47 %**, AIDE-greedy (o1-preview) **45.9 %**,
AIRA-greedy (R1) **45.5 %**, AIDE-greedy (R1) **39.8 %**.

### Measured: the Kaggle access problem is real and it is the deciding factor

Download tested per competition on this box's token, 2026-08-19 (per-file endpoint, 64-byte range):

| Result | Competitions |
|---|---|
| **OK (4)** | spooky ✓, nomad ✓, denoising-dirty-documents, dog-breed-identification |
| **403 (6)** | random-acts-of-pizza, leaf-classification, jigsaw-toxic-comment, aerial-cactus-identification, text-normalization-english, detecting-insults |
| **404 (1)** | plant-pathology-2020-fgvc7 |

**4 of 11.** Each 403 needs a manual "I Understand and Accept" on the competition page — and
`detecting-insults` is already known **ungrantable on this account**, so an unknown fraction of the
rest may be too. A 9-competition subset is therefore not a download, it is a negotiation with
Kaggle whose outcome we cannot predict.

### Consequence: FML-bench becomes primary, MLE-bench becomes the sanity check

FML-bench wins on all three of the operator's stated constraints at once:

1. **Densest same-model field in the literature** — seven frameworks on one model, versus MLE-bench's
   best of three. **AIDE and AIRA both ship as wrappers**, i.e. exactly the two baselines Option C
   wanted, without Slurm, without Apptainer and without Kaggle.
2. **No gated data at all** — `python setup.py` bootstraps task repos, datasets and per-task conda
   envs. The 403 table above simply does not apply.
3. **Apache 2.0** — against CC BY-NC 4.0 for both aira-dojo and OpenRSI, which matters the moment
   any of this is commercial.

And it measures **what LoopLab claims to be**: twelve process-level metrics over five dimensions —
exploration (spread, reach, uniqueness, effective dimensionality), generalization, reliability,
efficiency (AUC-over-steps, convergence timing) and cost. That is a search-dynamics benchmark, not
an ML-engineering one.

**Costs and open risks, stated:**

- **A LoopLab wrapper does not exist.** Seven ship; ours is the eighth. This is the real price of
  switching, and it is one adapter against an unbounded Kaggle negotiation.
- **A100-80GB reference vs our 32 GB.** Whether all 8 FML-bench-Lite tasks fit in 32 GB is
  **unverified** — the README states no VRAM floor. Must be measured before committing.
- **Runtime is step-budget-bound, not cheap.** The published "40 min on one GPU" is a *validation*
  run; the agent budget is T=100 steps, which the authors themselves say is "a few days" per task.
  `max_steps` is a CLI override (`agent.<name>.max_steps=N`) and must be **equal for both sides**.
- Their headline metric is *mean normalized test improvement*, not a medal rate — so nothing here
  joins the MLE-bench leaderboard. Different currency, denser field.

**MLE-bench keeps a role**: the 4 competitions that actually download are a cheap, objective,
medal-graded sanity check with a private held-out grader — worth running, not worth building the
campaign on.

## 5ab. The full field, and every consumer-GPU result found

### Consumer-GPU experiments — there are TWO, and they are the two strongest recent MLE-bench results

| Work | Hardware | Budget | Benchmark | Result |
|---|---|---|---|---|
| **Frontis-MA1 / OpenMLE** (2607.28568) | **1× RTX 4090, capped at 12 GB VRAM** | 12 h/task | MLE-Bench **Lite** | 39.39 % → 60.61 %, **71.21 %** Evo-Max |
| **ML-Master 2.0 / HCC** (2601.10402) | **2× RTX 4090 + 36 AMD EPYC vCPU**, 1008 GB RAM & 1 TB SSD per 4 tasks | 24 h/task | MLE-bench **full** | **56.44 %** medal rate (SOTA at publication) |
| AIRA (2507.02554) | 1× H200, 24 CPU, 100 GB | 24 h/task | MLE-bench Lite | 47 % (AIRA-MCTS, R1) |
| MLE-bench official | 1 GPU (unspecified class) | 24 h/task | — | — |

So the 4090 class is not a compromise anybody has to apologise for — **it is where the current
public SOTA on this benchmark was produced.** This box (32 GB, 8 CPU) is above Frontis on VRAM and
below both on CPU; the CPU gap is the one to disclose.

### The whole field, filtered by what we actually need

| Benchmark | Tasks | Multi-framework, ONE model? | Gated data | Speed per iteration | Fits 5090 |
|---|---|---|---|---|---|
| **FML-bench** (2605.17373) | 18 (**Lite 8**) | ✅ **7 frameworks on GPT-5.4** — the densest in the field | ❌ none, `setup.py` | ~40 min validation run; T=100 steps ⇒ days/task | ? A100-80GB reference, **unverified** |
| **KernelBench** (+ KernelBench-X 2605.04956) | **250** kernels (L1 100 / L2 100 / L3 50) | ❌ models, not frameworks | ❌ none — tasks *are* PyTorch reference code | **seconds–minutes** (compile + time a kernel) | ✅ **natively** — the GPU IS the subject |
| **AlgoTune** (2507.15887) | **154** expert tasks | ❌ one agent (AlgoTuner) × frontier models | ❌ none | **fast, and deliberately UNIFORM** (controllable runtimes, unlike KernelBench) | ✅ CPU-oriented (SciPy/sklearn/CVXPY refs) |
| **MLE-bench** / Lite | 75 / 22 | ~ 3 frameworks on DeepSeek R1 (AIRA) | ✅ **Kaggle — measured 4/11 here** | 24 h/task | ✅ |
| MLGym-Bench (2502.14499, Meta) | 13 open-ended | ❌ models under one scaffold | ~ | — | ? |
| MLS-Bench (2605.08678) | 140 / 12 domains | ? | ? | — | ? |
| AIRS-Bench (2602.06855) | 20 | ? | ? | — | ? |
| MLRB (2410.22553) | 7 conference-track tasks | ? | ~ | — | ? |
| RE-Bench (METR) | 7 + **human baseline** | ❌ | ❌ | 8 h/attempt | ✗ heavy |
| ResearchGym (2602.15112) | 5 envs / 39 subtasks | ❌ | ❌ containerized | — | ? |
| NatureBench (2607.28568) | 90 containerized | ~ | ❌ | — | ? |
| MLE-Dojo / DSBench | 200+ / 540 | ❌ | ✅ Kaggle | — | ✅ |
| PaperBench / MLR-Bench / ResearchClawBench | — | ❌ | — | very expensive, LLM-judged | ✗ |
| OpenMLE-Gym | 5,758 | — (a corpus) | ❌ | — | ✅ (CC BY-NC) |

### The three tiers this resolves into

The three constraints — *joinable comparison*, *fast on the 5090*, *no gated data* — are **not
satisfiable by one benchmark**, and trying to pick a single winner is what made the previous two
recommendations wobble. They are three different jobs:

1. **Fast iteration rig — KernelBench (GPU) or AlgoTune (CPU).** Seconds-to-minutes per iteration,
   zero gating, and for KernelBench the 5090 *is* the object under test. This is where the harness,
   the OpenRouter pin, the metering and LoopLab's own knobs get debugged — at a hundred iterations
   per hour instead of one per day. Weakness: neither has a multi-framework field, and both measure
   narrow optimization rather than a research loop. **Note KernelBench results are inherently
   per-GPU** (speedup vs a PyTorch reference on the *same* card), so a 5090 arena is internally
   valid even though it cannot join their H100/B200 leaderboard.
2. **Comparison of record — FML-bench-Lite.** Seven frameworks on one model, AIDE and AIRA among
   them, Apache 2.0, no gating. This is the number to publish.
3. **Medal-graded sanity check — the 4 MLE-bench competitions that actually download.** Objective,
   private held-out grader, and the currency every leaderboard speaks.

## 5b. Benchmarks in priority order, and the campaign that actually fits

### Ranking criteria

(a) connects to numbers we can be compared against; (b) already wired in LoopLab; (c) **objective**
grader, not an LLM judge; (d) fits this box; (e) measures what LoopLab claims to be differentiated at.

| # | Benchmark | a | b | c | d | e | Verdict |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| **1** | **MLE-bench Lite subset** | ✅✅ | ✅ | ✅ | ✅ | ~ | **Start here.** The only currency that connects to AIDE 39.6, AIRA 47.7, MLE-STAR ~64, Frontis 60.61/71.21. Wired end to end; private held-out grader. |
| **2** | **The same runs, hack-adjusted** | — | ✅ | ✅ | ✅ | ✅✅ | Not a second benchmark — a second **column**. Free once #1 runs, and nobody else can produce it. |
| **3** | **FML-bench** (8 tasks) | ~ | ✗ | ~ | ✅ | ✅✅ | The smallest benchmark that scores the *research loop* rather than ML engineering. Needs one TaskAdapter. |
| **4** | **NatureBench Lite** | ✅ | ✗ | ✅ | ? | ✅ | Held-out transfer, containerized, released with OpenRSI. |
| **5** | **OpenMLE-Gym** (5,758) | ✅ | ✗ | ✅ | ✅ | ~ | The regression corpus doc 27 §4 wants. **CC BY-NC 4.0** — a product decision. |
| **6** | **RE-Bench** | ✅✅ | ✗ | ✅ | ✗ | ✅ | Uniquely has a *calibrated human baseline*. Too heavy for one box today. |
| **7** | ResearchGym / ResearchClawBench | ~ | ✗ | ✅ | ? | ✅ | New; its failure taxonomy is usable as a trace checklist before the benchmark itself is. |
| **8** | MLE-Dojo, DSBench | ~ | ✗ | ✅ | ✅ | ✗ | Different shape (RL-gym / analysis tasks); not our claim. |
| **9** | MLR-Bench, PaperBench | ~ | ✗ | **✗** | ✗ | ✅ | LLM-judged and/or very expensive. Judge-scored numbers are the ones we least want to defend. |
| **10** | AgentDS | ✗ | ✗ | ? | ? | ✅ | Harness/licence availability still unconfirmed. |

### The binding constraint is WALL-CLOCK, not money

With the LLM off-box, `$` stops being the limit and exclusive GPU-days become it. On one box, serial:

| Campaign shape | 1 framework | head-to-head (2) |
|---|---:|---:|
| 9 comps × 3 seeds × 24 h (**official MLE-bench**) | 648 h (27 d) | **1,296 h (54 d)** |
| 9 × 3 × 12 h (**Frontis budget**) | 324 h (13.5 d) | 648 h (27 d) |
| 9 × 1 × 12 h | 108 h (4.5 d) | 216 h (9 d) |
| 9 × 1 × 4 h | 36 h (1.5 d) | 72 h (3 d) |
| **PILOT** 4 × 1 × 4 h | 16 h | **32 h (1.3 d)** |
| **SMOKE** 1 × 1 × 4 h | 4 h | 8 h |

Cost over the same shapes (at the measured ~250 k tok/node, 20 nodes/comp), **per framework**:
9×1 ≈ 45 M tok ≈ $22 / $112 / $270 at cheap / mid / frontier blended rates; 9×3 ≈ 135 M ≈ $68 /
$338 / $810.

**Read those two tables together: the full official shape costs ~$1.6 k and 54 GPU-days.** The money
is affordable and the calendar is not — so the campaign must be shaped by wall-clock, and seeds are
the first thing to spend last.

### Revised subset — moving the LLM off-box CHANGES which competitions to pick

§5's original 9 were text/small-tabular *because a local LLM was eating 23 GB of the card*. With the
LLM on OpenRouter the full 32 GB is the candidate's, so GPU competitions become viable — and they
should be included, because a subset that is entirely CPU-bound never exercises the one asset this
box has, and says nothing about the GPU-bound majority of MLE-bench.

| Lane | Members | Note |
|---|---|---|
| CPU-light (5) | spooky ✓, nomad ✓, random-acts-of-pizza, leaf-classification, jigsaw-toxic-comment | fast, cheap, can run concurrently |
| GPU (4) | aerial-cactus-identification (0.025 GB), denoising-dirty-documents (0.059 GB), dog-breed-identification, plant-pathology-2020-fgvc7 | exercises the 5090; the last two are multi-GB, size not measurable via the API page cap |

> **Concurrency is a lever and a hazard.** The CPU-light lane does not need the GPU, so 2–3 can run
> beside a GPU competition and cut the calendar. But 8 cores shared between concurrent competitions
> is CPU contention — a confound. Permitted only if the *same* concurrency plan runs for both
> frameworks, and it must be stated in the report.

## 5c. Execution log — what actually happened when we ran it (2026-08-19)

### Kaggle access went from 4/12 to 10/12, and the flow is not what the docs imply

Rules acceptance for these old competitions is **hidden behind the "Late Submission" button**, not a
distinct consent control — `docs/MLEBENCH.md` says "click I Understand and Accept", which is the text
*inside* the dialog that button opens. Accepted for six: `random-acts-of-pizza`,
`leaf-classification`, `jigsaw-toxic-comment`, `aerial-cactus-identification`,
`text-normalization-{english,russian}`. Each verified afterwards by a real ranged download.

Two remain unobtainable and both are structural, not fixable by retrying:

- **`detecting-insults-in-social-commentary`** — a *recruitment* competition; its Late Submission
  button is rendered **disabled**. This confirms `MLEBENCH.md`'s standing note.
- **`plant-pathology-2020-fgvc7`** — 404 at the API level (file listing succeeds, download does not).

**So the N=9 subset of §5b is fully obtainable**, with `dog-breed-identification` standing in for the
tenth. That retires the "unbounded Kaggle negotiation" objection that demoted MLE-bench in §5aa —
the negotiation was bounded and took one session.

### The environments are Linux-first and their pins have rotted

Both benchmarks needed WSL2 (conda + bash + `pythran`/`dace`). Four traps, all real:

1. **pip's resolver thrashes on both projects.** AlgoTune's `pip install -e .` burned 464 MB of
   downloads and **9 seconds of CPU across 40 minutes** before being replaced with `uv`. FML-bench's
   `setup.py` hit the same wall inside its conda env. `uv` resolves both in minutes.
2. **AlgoTune's own `requirements.txt` is doubly stale**: it needs **Python ≥ 3.11** (via
   `networkx==3.5`) while its README recommends conda with 3.10, and it pins `pot==1.0.0`, a version
   that does not exist on PyPI. Resolving `pyproject.toml` with uv under 3.11 works.
3. **Conda now demands acceptance of Anaconda's channel ToS.** Not accepted here — those terms carry
   commercial-use licensing consequences for the operator. Pointing conda at **conda-forge**
   (`channel_priority: strict`, `default_channels: []`) sidesteps the question entirely and resolves
   clean.
4. `unzip` absent; `unattended-upgrades` holds the dpkg lock on a fresh WSL — wait it out rather than
   forcing.

### The FML-bench blocker is Blackwell, not VRAM

§5ab left "does it fit 32 GB?" open. Measured: **RTX 5090 reports compute capability 12.0
(sm_120)**, while FML-bench's `domainbed` task env pins `pytorch==1.12.1` + `cudatoolkit=11.3`,
whose fat binaries top out at **sm_86**. The pinned task environments predate this GPU generation.

This is a *worse* problem than a memory ceiling, because the obvious fix is not neutral: upgrading
torch changes the **baseline the agent is asked to improve**, so the numbers would no longer be
comparable to the published seven-agent table — which was the entire reason for choosing FML-bench.
Open options, in preference order: (a) find which FML-bench-Lite tasks are CPU-only or already on
modern pins; (b) run the affected tasks on CPU and report that; (c) upgrade the stack and report as
a *modified* benchmark, never as FML-bench. Not yet resolved.

### AlgoTune is up and running

Installed and verified in WSL: Python 3.11.16, litellm 1.83.0, **torch 2.13.0+cu130, CUDA available,
RTX 5090 detected**. The first agent run against the pinned model started cleanly — the task loads,
the command parser works, edits apply, and full-dataset evaluation runs.

One early behavioural signal worth recording: **the fast model fumbles the strict command protocol.**
`deepseek-v4-flash` emitted several commands in one message and tripped AlgoTune's `TRAIL_CHECK`
("Found trailing text after command block"), then spent a long reasoning block confused about
whether its command had executed. Cheap-and-fast has a harness-compliance cost that a benchmark
comparing *frameworks* will absorb as noise — record it, and hold the model identical across arms so
it cancels.

## 5d. What is actually measured — environment is the OPERATOR's job in both benchmarks

The operator's question: *does the agent have to fix its own environment, or is that provisioned?*
Because if the agent is fighting a broken environment, the benchmark measures the wrong thing.
Answered by reading both harnesses, 2026-08-19. **Both say: provisioned. The agent does algorithm
work only.**

### AlgoTune — verified consistent on this box

The agent's ENTIRE command set: `ls`, `view_file`, `edit`, `delete`, `revert`, `reference`, `eval`,
`eval_input`, `profile`, `profile_lines`. **No shell, no `pip`, no environment access.** The prompt
enumerates what it may use — *"Apart from the default Python packages, you have access to the
following additional packages: …"* — 27 of them. That enumeration is why their dependency list is
enormous: the operator is expected to provision all of it.

**Checked every promised package against our install: 27/27 importable.** The environment matches
what the agent is told. (`pot` is present — the earlier failure was only the stale `pot==1.0.0` pin
in `requirements.txt`; `pyproject.toml` leaves it unpinned and uv resolves it.)

AlgoTune is also **CPU-only in substance**, so the Blackwell problem does not arise, and its metric
is a **speedup ratio against a reference solver timed on the same machine in the same run** — it
self-normalizes against hardware. **This makes AlgoTune the clean arena on this box.**

### FML-bench — same design, but two things must be fixed before any number is honest

Design confirmed identical in spirit: `benchmark/executor.py` runs everything as
`conda run --no-capture-output -n <conda_env> bash -c <cmd>` in an env `setup.py` pre-creates, and
each task pins what the agent may touch:

```json
{"repo_dir": "workspace/Generalization_domainbed/DomainBed",
 "pinned_commit": "b93c22a1cfc3b2428398272c1a116c8de1f4139e",
 "conda_env": "domainbed",
 "target_files": ["domainbed/algorithms.py", "domainbed/networks.py"]}
```

and `val_command` / `test_command` **restore `train.py`, `datasets.py` and friends from
`original_file_backup/` before every evaluation** — so the agent cannot touch the training loop, the
data pipeline or the scorer. Its scope is the algorithm and the network. Exactly the right thing.

**Problem 1 — the pins predate this GPU.** None of the eight Lite task envs pins a torch that
supports sm_120: `domainbed` 1.12.1+cu11.3, `privacy_meter` 2.4.1, and `pycil` / `opacus` / `usb` /
`openood` all 2.5.1. Provisioning a working stack is legitimately the operator's job here — but it
changes what the baseline code achieves.

**Problem 2 — and this is the one that decides comparability — the baseline is a COMMITTED
CONSTANT, not a re-measurement.** `benchmark/runner.py::_load_baseline()` reads
`ml_tasks/<task>/baseline_results/val_info.json`, a number recorded on the authors' A100-80GB with
the authors' pins. Meanwhile `compute_agent_metrics.py::RANGE_META` normalizes **7 of the 8 Lite
tasks against fixed absolute constants** (`("higher", 1.0, 0.0)`), not against that baseline — only
`causalml`, `gcastle` and `open_unlearning` use `"baseline"` as the range endpoint.

The consequence cuts both ways and must be stated:

- *Good:* changing torch cannot silently rescale the metric, because the scale is absolute.
- *Bad:* precisely because the scale is absolute, the accuracy the code actually reaches is
  environment-dependent, while `p_baseline` stays frozen at the authors' value. `normalized_
  improvement(p_agent, p_baseline, …)` would then blend "our environment vs theirs" into what is
  supposed to be "agent skill".

**Fix, and it is supported by the harness's own design:** the baseline is read *from a file*, so
re-measure it in OUR environment and regenerate `baseline_results/val_info.json` per task. Improvement
is then measured against a baseline from the same stack, and the comparison between agents is
internally valid. What is permanently lost is the right to put our number beside their published
0.208 / 0.193 / 0.192 — that table belongs to their hardware and their pins.

Shipped baselines, for reference when we re-measure (Lite): DomainBed ColoredMNIST `in_acc_mean`
0.2792 · DomainBed OfficeHome `avg_acc_mean` 0.8832 · PyCIL `avg_incremental_acc_mean` 0.5911 ·
USB `test_acc_mean` 0.0757 · OpenOOD `auroc_mean` 0.8758 · Opacus `test_acc_mean` 0.5894 ·
PrivacyMeter `AUC_gap_mean` 0.3339 · ART `clean_acc_mean` 0.9442.

### Consequences for the plan

1. **AlgoTune is the comparison of record on this box.** Ratio metric, hardware-self-normalizing,
   environment verified against its own prompt, no GPU-generation exposure.
2. **FML-bench requires re-baselining to be honest**, and must then be reported as
   *"FML-bench protocol, re-baselined on RTX 5090 / modern torch"* — never as a row in their table.
   That is a real cost and it should be paid only after AlgoTune shows a signal worth chasing.
3. **Every harness modification is disclosed.** So far one: a genuine upstream bug in
   `AlgoTuner/utils/isolated_benchmark.py::clear_solver_caches`, which iterated `sys.modules.items()`
   while `inspect.getmembers()` inside the loop triggered lazy imports and mutated it —
   `RuntimeError: dictionary changed size during iteration`, **224 occurrences, failing every single
   benchmark run so no speedup could ever be recorded**. Fixed by snapshotting to
   `list(sys.modules.items())` at both call sites; original kept as `.orig`; re-run shows 0
   occurrences. This does not change what is measured — it is a between-timings cache-clearing
   utility — but it must be in any published methods note.

## 5e. AlgoTune ships 2,595 reference solvers — the cross-hardware problem dissolves

`evaluate_results.py` "discovers models from the ./results directory" with the layout

```
results/<model_name>/<task_name>/solver.py
```

and the repo **ships 17 models x 154 tasks = 2,595 solver.py files**: o4-mini, R1, GPT-5, GPT-5-mini,
GPT-5.2, GPT-5.4, GPT-5 Pro (medium), GPT-OSS-120b, GLM-4.5, Qwen3 Coder, Gemini 2.5 Pro,
Gemini 3 Pro Preview, Gemini 3.1 Pro Preview, Claude Opus 4 / 4.1 / 4.5 / 4.6.

Two consequences, both large:

1. **LoopLab plugs in as one more directory.** Write its produced solvers to
   `results/LoopLab/<task>/solver.py` and `./algotune.sh evaluate --standalone` scores them with the
   **identical evaluator, identical baselines, identical machine**. No adapter to their agent loop is
   needed — the seam is the artifact, not the harness.
2. **The cross-hardware comparability problem disappears.** Instead of setting our number beside a
   published one measured on someone else's box, we **re-time their shipped solvers on ours** in the
   same pass. Every arm is measured by the same clock.

**What this comparison does and does not isolate — state it precisely.** The 17 shipped arms were all
produced by *AlgoTuner's own agent loop* driving different models. So re-timing them compares
**artifacts**, and a LoopLab-vs-reference row mixes "different loop" with "different model". The
clean controlled arm remains **LoopLab vs AlgoTuner on the SAME model** (`deepseek-v4-flash-0731`),
which we produce ourselves — the 17 are context, not controls.

Target shape:

| Arm | Loop | Model | Provenance |
|---|---|---|---|
| A | AlgoTuner | deepseek-v4-flash-0731 | produced here |
| B | LoopLab | deepseek-v4-flash-0731 | produced here — the controlled comparison |
| ref x17 | AlgoTuner | GPT-5.4, Opus 4.6, Gemini 3.1 Pro, R1, … | shipped, re-timed here |

## 5f. Wiring LoopLab into AlgoTune — and the parity trap in the obvious wiring

### The seam

AlgoTune's evaluator does `from solver import Solver; Solver()`, so the whole agent contract is one
file:

```python
class Solver:
    def solve(self, problem: dict[str, Any], **kwargs) -> Any: ...
```

Arm B is therefore a LoopLab `repo` task over a workspace holding `description.txt` (the problem),
`reference_svm.py` (the reference `solve()`/`is_solution()`, **protected**) and an editable
`solver.py`, with `edit_surface: ["solver.py"]` and an eval command that calls a thin bridge
(`looplab_eval.py`) which copies the candidate solver into `results/LoopLab/<task>/` and runs
**AlgoTune's own** `evaluate_results.py --models LoopLab --tasks <task>`, returning `speedup` as
stdout JSON. No scoring logic lives in the bridge — it moves a file and reads a number back.

### The trap: the naive bridge silently handicaps LoopLab

Measured on this box: the agent run's **oracle phase alone timed ~150 instances at ~3 s each, about
30 minutes for ONE task.** Inside AlgoTuner that is paid **once per run** — `BaselineManager` holds
it in-process, and every later `eval` is cheap. A bridge that shells out per node **re-pays it every
node**.

That would make arm B look slow for a reason that is a property of *my wiring*, not of LoopLab —
precisely the environment noise this whole comparison is meant to exclude. And it cannot be fixed by
handing LoopLab more wall-clock, because the overhead scales with its node count.

Two related facts pin it down:

- `evaluate_results.py`'s docstring claims it "reads generation.json for baseline timings". **It does
  not.** `generation_data` is consumed only at line 882 (`all_tasks = set(generation_data.keys())`),
  line 1086 (task filtering) and line 1168 (summary formatting). The baseline is **always
  re-measured locally**.
- That local re-measurement is also what makes the whole re-timing plan of §5e valid
  (`speedup = baseline_ms / optimized_ms`, both on this machine). The property is good; paying for it
  per node is the problem.

**Fix — restore parity rather than grant compensation:** measure each task's baseline **once**, cache
it, and have the bridge compute `cached_baseline_ms / measured_solver_ms`. That is exactly what
`BaselineManager` does inside an AlgoTuner run, so it is parity, not a deviation — and it must be
stated as such in the methods note. Persist the dataset too (`generate` writes `data/`, which is
empty here, so every run currently regenerates it).

**General rule this instance illustrates, worth carrying into FML-bench and MLE-bench:** when the
reference agent has an in-process cache and our integration is out-of-process, equal *budgets* are
not equal *work*. Compare the cost structure of the two loops before trusting any timing.

### The trap has a SECOND half, and caching the number does not close it

The fix above caches the aggregate `baseline_ms`, which stabilises the **denominator** so every node
is scored against one number. It does not reduce the **cost**: `looplab_eval.py` still shells out to
`evaluate_results.py`, which builds a fresh `BaselineManager` in a fresh interpreter, and
`BaselineManager`'s cache is `self._cache` — *process memory*. So the reference pass was still being
re-measured on every node; only the value it produced was being discarded.

Re-measured 2026-08-19 during a live arm-A run: the oracle pass advances at roughly **2.4 s per
instance** and one task's pass ran **77+ instances in 7 minutes** without finishing. Minutes per
node, paid by one arm and not the other, scaling linearly with node count.

`benchmarks/algotune/run_evaluator.py` closes it: it wraps `BaselineManager.get_baseline_times` in a
**disk-backed** cache keyed by `(task, subset)` and then runs the upstream script unmodified through
`runpy`. First call measures and writes, later calls load. It never caches across tasks or across
the train/test split — those are different reference sets — and honours `force_regenerate`.

This is parity restoration on the same argument as the first half: it makes the out-of-process bridge
pay what the in-process reference loop pays, **once**. The solver timing is still measured fresh on
every node, and the ratio is unchanged.

### The budget must be SPEND, not wall-clock — because that is what the reference harness uses

The campaign was originally sized as "20 minutes per task-arm". Reading arm A's own startup log
settles that this was the wrong axis:

```
INFO - Configuration loaded successfully. Budget: $1.0000
INFO - Config loaded: spend_limit=1.0 total_messages=9999 max_messages_in_history=5 oracle_time_limit=100
```

**AlgoTuner is budgeted by money, not time**, with an effectively unlimited message count. And its
oracle pass — minutes of wall clock — costs **$0** of that budget. A wall-clock cap therefore charges
one loop for a measurement pass neither one's *agent* performs, and on a 20-minute budget arm B would
have completed zero evaluations.

So both arms get **$1.00 per task**: arm A natively, arm B through the new `llm_budget_usd` setting
(`CostAccountant` has always accepted a `limit`, and all ten `except BudgetExceeded` sites in
`agents/` already treat it as a hard stop that propagates — the only thing missing was a way to set
it). Wall-clock survives only as a safety net against a hung process.

### Reasoning effort: `medium` on both arms

Measured on this endpoint: `medium` 21.5 s/call, `high` 111.3 s/call — 5x — with answer quality at
`high` **not measured either way**. Under any bounded budget, `high` buys so few model calls that the
comparison becomes one of two truncated runs. Both arms are pinned to `medium`; parity is what the
shared value buys, not the level itself.

### The bridge scored 0.0 for everything, for three independent reasons

Validating the arm end to end — rather than reasoning about it — found three defects, any one of
which alone would have produced a campaign of zeros indistinguishable from a bad agent:

1. **`RLIMIT_AS` killed every evaluation.** `validation_pool.disable_rlimit_as: false` caps VIRTUAL
   address space, which JAX/torch/BLAS reserve in tens of GB without touching. Every run died with
   "A process in the process pool was terminated abruptly" — at 14 GB and again at 30 GB, on a
   321 MB task and on a 28 KB one, with 45 GB free. It is neither dataset size nor real memory.

2. **The summary reader was keyed on fields nothing writes.** The file is
   `{"discrete_log": {"BV4": {"final_speedup": "0.9963"}}}` — no `task_name`, no `speedup`, value a
   string — and `looplab_eval.py` searched for `task_name`/`speedup`. It reported `speedup: 0.0` on
   a summary that had just been written successfully. This is the same defect class doc 50's own
   subject matter keeps producing: *a reader keyed on a field nothing writes is a silent empty
   answer, not a red test.*

3. **My own fix for the parity trap broke the thing it was optimising.** Wrapping
   `BaselineManager.get_baseline_times` from the parent process and running the evaluator through
   `runpy` crashed the worker pool. Decisive measurement: same task, same config — direct
   `0.9963x`, through the wrapper a pool crash. The cache now ships as an on-disk patch applied
   before anything imports the module, which is the recipe this checkout already uses for the
   upstream `sys.modules` bug.

After all three: `discrete_log` scores end to end, and the cache takes a repeat evaluation from
**668 s to 215 s**.

### The metric is noisy, and the arms are noisy symmetrically

The same solver scored **1.0006** then **1.4468** on consecutive runs of `discrete_log`. Caching the
reference means the numerator and denominator are no longer timed in one pass, so machine drift stops
cancelling. **This is not an asymmetry** — AlgoTuner's `BaselineManager` decouples them the same way
inside its own run — but it does decide how to READ the campaign: one task's ratio is weak evidence,
and the aggregate over 20 tasks is the comparison. `compare_arms.py` prints every per-task row so a
single wild value cannot hide inside a mean.

### Sizing the campaign: three things the first run taught, all of them cost-shaped

**1. A wall-clock net that BINDS deletes rows.** Arm A on `svm` reached $0.0182 of its $0.02 in
1 h 59 m and was cut by a 2 h net — writing **no `final_speedup` at all**. A cut run is not a shorter
run, it is a missing one. The net is now 4 h and exists only to catch a hung process.

**2. One timing-out candidate can cost 87 minutes.** After its 14th message that run made 69 isolated
benchmark runs and completed ZERO problems: `baseline_timeout` is 60 s and a task has ~100 instances,
so a single bad candidate is up to 100 minutes of pure timeout. Set to **10 s for both arms** — same
harness, so parity-preserving, and a solver 100x over a 100 ms target scores 0 either way.

**3. The first evaluation of a task downloads its dataset.** `evaluate_results.py` pulls from
HuggingFace into `.hf_datasets/`. Sizes are wildly uneven — `discrete_log` 28 KB, `svm` 24 MB,
`base64_encoding` **19 GB** — and an hour was lost concluding the bridge was broken when it was
really that one enormous dataset. Validate a bridge on a SMALL task.

None of the three is a property of either agent loop; all three are properties of the arena, and all
three were invisible until a real campaign ran.

## 5g. Wasted tool calls — measured, then closed on both sides

Running LoopLab against an EXTERNAL arena exposed something the internal runs never made visible: a
cold-start agent turn opens with a snapshot of a run that contains nothing, and closes with a
toolbox of 22 tools reading stores that are all empty.

**Measured 2026-08-19 over six cold-start runs: 138 of 227 tool calls returned nothing at all** —
`read_asset` 20/20, `cross_run_search` 12/12, `read_concept_tree` 10/10, `data_schema` 9/9,
`list_themes` 9/9, `list_notes` 8/8. Every one of those is a store the process could see was empty
while it was building the prompt.

### A rule did not move it; the same knowledge as DATA did

A prompt RULE ("read your context before reaching for a tool") was A/B'd over three models x three
runs and moved nothing: deepseek 21.0 -> 20.3 calls, gemini 4.0 -> 4.0, glm 17.7 -> 19.3. Publishing
the same knowledge as DATA in the user turn — one `tool=count` row per tool — moved it a long way.

The mechanism is `tools/_base.py::inventory()`, an optional provider hook. Each provider answers for
its own tools out of the same reads its answers come from, so a published count cannot drift from
what a call would show, and each row carries that provider's own scope rules rather than a looser
restatement of them. `agents/answered_by_context.py` renders it; `CompositeTools.inventory()` merges
it first-wins, matching dispatch.

`int` and `str` are different values on purpose. "I looked and there are none" and "I could not
look" have opposite consequences: a published `0` is decisive and suppresses a pointless call, while
publishing `0` for an unreadable store suppresses a call that had an answer. So a count is an `int`
and a reason is a `str`, rendered `UNKNOWN(reason)`.

### A correct count is defeated by an answer that reads as a near-miss

With the block live and publishing `read_asset=0`, one phase still spent NINE `read_asset` calls
walking `solver.py`, `reference_svm.py`, `reference`, `train`, `test`. The answer was "(this task
has no data assets)" — which reads as *not that one*. `cross_run_search=0` lost the same way to five
rephrasings, its receipt even printing a `corpus=<digest>` that looks like a corpus that exists.

Three answers are now TERMINAL: they state the CLASS of the emptiness and that no argument changes
it. `data_schema` also stopped referring the model to `read_asset`/`data_profile` when all three are
empty for the same reason, and the knowledge tools stopped answering "(empty)"/"(no matches)" — which
are claims about the *query* — when the note set is empty.

### Results (3 runs per arm, 5-minute budget, same task/model/provider)

| arm | tool calls | empty | duplicates | LLM calls |
|---|---:|---:|---:|---:|
| no block (baseline) | 41.3 | 25.0 | 6.7 | 14.7 |
| block, deep-research only | 34.3 | 22.0 | 3.7 | 13.0 |
| **full plumbing, flat rows** | **17.7** | **5.3** | **0.7** | **5.7** |
| grouped by answer | 19.3 | 5.3 | 3.0 | 10.0 |
| terse (prose removed) | 24.0 | 5.7 | 3.3 | 12.7 |

Tool calls **-57 %**, empty answers **-79 %**, exact duplicates **-90 %** against the baseline. The
three renderings are within the run-to-run spread of each other at n=3 — the honest reading is that
*publishing the counts* is what matters and the layout does not — but terse being worst is at least
consistent with the explanatory clauses being load-bearing rather than filler. Flat is shipped.

### What this cost to find, and a defect it surfaced

`Settings.hide_empty_tools` (stop ADVERTISING a tool that holds nothing) measured as *no
improvement* — and that measurement was worthless, because the flag never reached the phase being
measured. `make_deep_researcher` hand-rolled its own `CompositeTools(providers)` while its comment
claimed it used "the same capability assembly as the Researcher/Strategist", and the deep-research
phase makes essentially every tool call of a cold-start run. A run launched with the flag ON recorded
`hide_empty_tools: true` in its config snapshot and was offered every empty tool anyway.

There is now one composition helper (`agents/tool_loop.py::compose_tools`), all four agent-side call
sites go through it, and an AST guard fails if a fifth appears.

## 6. Recommendation

**0 (pin the LLM off-box, §5a) → A → B → C1+D**, in that order, then E.

A is nearly free and de-risks the whole path (it is the first time the provenance hardening meets a
real graded task). B is a half-gigabyte download. C is the deliverable — a same-model head-to-head
is defensible in a way an absolute local number never will be. D rides along for almost nothing and
is the only number in this space that is ours alone.

**Three things to hold honest throughout:**

1. **Name the subset, never call it Lite.** N=10 != N=22.
2. **The model is the confound.** Every reported number must carry
   `qwen3:30b-a3b Q4_K_M @ Ollama, 8 CPU, 1x RTX 5090`. Cross-paper comparison is invalid; only the
   within-experiment framework delta is.
3. **8 cores is a stated limitation.** Papers assume 32–36. It biases against CPU-heavy tabular
   comps, which is precisely why the subset in B is text/small-tabular weighted — and that bias
   must be disclosed, because it is a choice that flatters the harness.
