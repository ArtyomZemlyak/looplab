# LoopLab — Product & Design Specification

**Version:** 0.1 (design) · **Date:** 2026-06-20
**Companion docs:** [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) · [04-file-layout.md](04-file-layout.md) · [05-build-decisions.md](05-build-decisions.md) · **Research basis:** [autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)

> **What this is.** A from-scratch (non-fork) design for an open, backend-flexible **autonomous ML/DS research engine**: you give it a task and a way to measure success; it *invents → implements → tests → improves* solutions in a loop and returns the best **verified** solution plus a full log of what worked and why.

---

## 1. Vision & one-line pitch

> **LoopLab turns a measurable ML/DS goal into a self-running research program.** It runs many experiment threads, searches the space of code solutions, and only promotes results that survive an adversarial, variance-aware evaluator — so the answer it hands back is real, not a lucky run or a benchmark exploit.

The differentiator vs everything surveyed: existing OSS systems each hold *one* piece — AIDE has tree search, R&D-Agent has dual-agent + per-role routing, MLE-STAR has ablation-refinement + ensembling + a leakage checker, AIRA has the operators-beat-policy + consistent-evaluation findings, Karpathy's `autoresearch` has the minimal loop. **No open system combines the result-moving techniques (rich operators + ensembling + leakage-safe consistent evaluation) with backend flexibility (local + API), reproducible event-log lineage, and per-role routing.** LoopLab aims at that combination.

> **⚑ 2026 re-prioritization ([ADR-6](03-decisions.md)).** A fresh SOTA review changed where we claim an edge: **we differentiate on operators, evaluation rigor, ensembling, and temporal/target-leakage safety — not on a hardened evaluator or fancy search.** Our honest position: *better* than the field on **trust/reproducibility/backend-portability/leakage-safety**; *competitive on raw results only if* we ship the high-ROI operators above (we are not claiming raw MLE-bench SOTA). See [ADR-6](03-decisions.md).

---

## 2. Problem & motivation

- ML/DS research is iterative trial-and-error: propose an idea → write code → train → measure → keep or discard → repeat. This burns the scarcest resource: expert time.
- LLMs can now write and repair code well enough that agents reach Kaggle-bronze level on MLE-bench (AIDE 16.9%, R&D-Agent 30.22%).
- **But naive auto-research fails in predictable ways:** it overfits to a single run, it games the metric (reward hacking), and it forgets what it already tried. Recursive's whole emphasis is the *co-evolving evaluator* that fights exactly this.
- **Opportunity:** an open system that is (a) strong out of the box, (b) usable with local *or* API models, and (c) trustworthy because it verifies adversarially.

---

## 3. Goals & non-goals

### Goals (v1)
1. **Autonomous ML experimentation** — propose/implement/run/evaluate/iterate with no human in the loop per step.
2. **Best verified results** — competitive on MLE-bench-style tasks and local "improve-this-script" tasks.
3. **Pluggable LLM backend** — cloud APIs (Claude/OpenAI/Gemini) *and* local (vLLM/Ollama) via one abstraction, with **per-role model assignment**.
4. **Trustworthy outputs** — no reward hacking, variance-aware acceptance, fully reproducible lineage.
5. **Extensible by config + clean plugin points** — swap search policy, evaluator checks, task adapters, backends without touching the core.

### Non-goals (v1 — explicitly out of scope)
- ❌ General software-engineering agent (that's OpenHands' job).
- ❌ Idea→paper writing / manuscript generation (that's AI-Scientist-v2; possible v2 module).
- ❌ Hosted multi-tenant SaaS (single-user / single-org self-host first).
- ❌ Distributed multi-node training orchestration (single-node, possibly multi-GPU, first).

---

## 4. Target users & use cases

| User | Goal | Example task |
|------|------|--------------|
| **ML researcher** | Improve a model/recipe under a budget | "Lower val BPB on this nanochat training in a 5-min/run budget" |
| **DS practitioner** | Solve a dataset to a metric | "Maximize AUC on this tabular dataset, ≤2h total compute" |
| **Kaggle/benchmark user** | Win medals | "Solve this MLE-bench competition" |
| **Infra / perf engineer** | Optimize speed | "Cut time-to-target-loss on this training script" / "speed up this GPU kernel" |

**Primary "job to be done":** *"Here is a task and a number that defines success. Find me the best solution you can within this budget, and prove it's real."*

---

## 5. Core capabilities (feature groups)

### A. Task intake & adapters
- **Task spec**: dataset/handle, metric, direction (max/min), compute & wall-clock budget, constraints (allowed libs, no-internet, GPU/CPU), success threshold.
- **Task adapters** (pluggable): MLE-bench/Kaggle, **single-file script** (Karpathy-style: one mutable `train.py` + fixed harness), custom benchmark harness, GPU-kernel/SOL-bench.

### B. Idea generation — *Researcher role*
- Propose hypotheses conditioned on: task spec + **archive of prior results** + a **seed-knowledge library** (known tricks: Muon optimizer, modded-nanogpt techniques, common DS feature-engineering moves).
- Rank/diversify proposals (don't re-propose what failed; explore vs exploit).

### C. Implementation — *Developer role* (pluggable backend — [ADR-7](03-decisions.md))
- Turn an idea into a concrete **code patch** against the current-best solution node; repair execution errors from runtime feedback.
- **The Developer is an abstraction over a pluggable backend:** a raw LLM call **or a complete external coding agent** (OpenHands / Aider / SWE-agent / Claude Code) — max quality per phase, no reimplementing a coding agent. Selected by config; the agent backs only the inner *step*, our loop/operators stay ours.

### D. Execution
- Run candidate code in a **sandbox**: enforce timeout, compute budget, resource caps, optional no-network; capture metric, logs, stdout/stderr, artifacts.

### E. Evaluation — *leakage-first trust layer* ([ADR-6](03-decisions.md))
- **Leakage checker (primary)**: train/test **+ temporal + target** leakage auto-detect/correct (our genuine differentiator).
- **Consistent evaluation**: fixed splits/seeds across candidates; select on a trustworthy validation metric, not test (the +9–15 pt lever).
- **Variance gating, tiered**: robust CV everywhere; multi-seed confirmation only at the top-k promotion frontier (not p<0.01 on every node).
- *Optional (open-ended mode):* co-evolving anti-reward-hack evaluator — only when the agent defines its own metric.

### F. Operators & search ([ADR-6](03-decisions.md): operators > policy)
- **Rich operators (the real levers):** draft · depth-bounded **debug** · improve · **ablation-driven targeted refinement** (find + optimize highest-impact code block) · **ensemble/merge** (LLM-proposed, iteratively refined — best-evidenced lift).
- **Greedy tree search by default** (validated strong); `parent_ids` DAG enables the **merge** (multi-parent) operator.
- **Persistent archive** feeds the Researcher; diversity/novelty selection + fancier policies (MCTS/evolutionary) are **opt-in experiments**, not core.
- **Throughput test-time scaling**: parallel candidates + cheap proxy evals (subset/reduced-epoch) over wall-clock.

### G. Orchestration
- Main loop, budget accounting (tokens + compute), **parallel research threads**, model routing per role, stop conditions.

### H. Observability & exploration UI *(decoupled — [ADR-1](03-decisions.md))*
- **UI is a separate layer** reading from **files** (append-only event log = source of truth); engine is the only state writer.
- Explore **traces, flow, the DAG, per-node/experiment info**; **act** (pause/resume/fork-from-node, edit config) by appending command intents.
- **Textual TUI now → browser via textual-serve → React Flow web UI later**, all on the same files. Live updates via file-watching (no streaming backend needed).

### I. Reproducibility & experiment tracking *(new — [ADR-4](03-decisions.md); [ADR-6](03-decisions.md): MLflow is an optional exporter, not core)*
- **MLflow headless exporter** (SQLite backend, no MLflow UI) over the event log: params/metrics/artifacts/tags/search, **auto git-commit capture**, autolog, model registry.
- **Multi-parent lineage DAG** via our own `parent_ids` tag; **reproduce-then-branch** from any past experiment.
- Git commit per accepted experiment (Karpathy pattern); every result ships `{git_ref, seeds, deps_lock, mlflow_run}`.

### J. Artifact ingestion & goal-grounded planning *(new — [ADR-3](03-decisions.md))*
- Accept pre-existing artifacts (web pages, tables, PDFs, files, prior runs); **analyze against the goal first**, then plan.
- Pre-phase: ingest (Docling/GROBID/trafilatura) → web/literature grounding (MLE-STAR style) → goal-conditioned analysis + novelty gate → **ranked experiment backlog**; re-ground as results arrive. Immutable goal anchor.

### K. Configuration & backends
- One **LiteLLM** layer; per-role model config; local/API switch without code changes.
- **Pluggable research algorithm** ([ADR-2](03-decisions.md)): the search strategy is a config-selected plugin (`GreedyTree`/`Evolutionary`/`MCTS` or your own) — overridable without forking.
- **Pluggable role backends** ([ADR-7](03-decisions.md)): each role is `backend: llm` or `backend: cli_agent` (OpenHands/Aider/SWE-agent/Claude Code) — config-selected, no fork.
- Everything common is config-driven; deep changes go through documented plugin interfaces.

### L. Capability layer — tools & skills ([ADR-9](03-decisions.md))
- **MCP capability bus**: engine capabilities (`query_archive`, `profile_data`, `check_leakage`, `run_code`) exposed as MCP servers, consumed identically by our roles **and** external agent backends — one capability, one place, identical results.
- **Agent Skills** (`SKILL.md`): ML recipes (K-fold CV, Muon, leakage checks) with progressive disclosure; the old seed-knowledge library migrates here. Per-role tool allow-lists as a security boundary.

### M. Knowledge & memory ([ADR-10](03-decisions.md))
- Four tiers (working / episodic / semantic / procedural), one representation + a goal-conditioned retrieval router over separate indices.
- **Cross-run experience** (new): a case library + distilled lessons that accumulate across tasks, gated (retain-on-improvement, decaying confidence, contradiction handling, poisoning defense) — a top-2026-system differentiator.

### N. Prompt & instruction management ([ADR-8](03-decisions.md))
- Prompts are **versioned, hot-reloadable files** (`prompts/<role>/<op>.md`), **UI-editable** (thin editor / optional Langfuse); one canonical store, no two-way sync.
- **AGENTS.md** generated per-run to instruct external coding-agent backends (conventions in AGENTS.md, task in prompt, hard limits in CLI flags).

### O. Hardening ([ADR-11](03-decisions.md))
- Secrets via LiteLLM gateway (tokens, not keys); **`pydantic-settings`** typed config with resolved snapshot; OpenTelemetry-GenAI event taxonomy; human-in-the-loop **approvals as command events**; idempotent crash-resume; gateway-enforced hierarchical budgets; per-run isolation (worktree + sandbox + namespace).

> **New requirements (2026-06-20)** — decoupled UI, pluggable algorithm, artifact ingestion, reproducibility/tracking, graph-vs-tree — in **[03-decisions.md](03-decisions.md)** (ADR-1…5). **2026-06-21 additions** — external-agent role backends (ADR-7), prompts/AGENTS.md (ADR-8), MCP+Skills (ADR-9), knowledge/memory (ADR-10), hardening (ADR-11) — plus the SOTA re-prioritization (ADR-6). All reflected across this doc and the architecture.

---

## 6. Functional requirements — the functions/features we need

Concrete capabilities the system must expose (interfaces detailed in the architecture doc):

**Task layer**
- `load_task(spec) -> Task` — parse a task spec into a normalized object.
- `TaskAdapter.prepare()` / `.score(solution) -> Metric` / `.validate(run) -> ValidityReport`.

**Agent layer**
- `Researcher.propose(context) -> list[Idea]` — generate diverse, novelty-filtered ideas.
- `Developer.implement(parent_solution, idea) -> Patch` — produce a code diff (via the configured `RoleBackend`).
- `Developer.repair(solution, error) -> Patch` — fix a failed run.
- `RoleBackend.run(RoleRequest) -> RoleResult` — raw-LLM or external coding-agent CLI, interchangeable ([ADR-7](03-decisions.md)).

**Execution layer**
- `Sandbox.run(solution, budget, seed) -> RunResult` — isolated, budgeted execution.

**Evaluation layer**
- `Evaluator.evaluate(run) -> Verdict` — objective + validity + anti-hack checks.
- `VarianceGate.confirm(solution, n_seeds) -> GateResult` — statistical promotion test.
- `Evaluator.add_exploit_rule(rule)` — harden the evaluator (co-evolution).

**Search/memory layer**
- `SearchPolicy.select(dag, archive) -> Node` — pick where to expand next (explore/exploit). Also `.expand`, `.should_merge`, `.stop` (see [02 §3.7](02-architecture.md)).
- `Archive.add(entry)` / `.query(context) -> list[Entry]` / `.merge(a, b) -> Idea`.

**Orchestration layer**
- `Orchestrator.run(task, config) -> ResearchReport` — the top-level loop.
- `BudgetManager` — track/enforce token + compute budgets, allocate to threads.

**Backend layer**
- `LLM.complete(role, messages) -> Response` — routed per role via LiteLLM.

**Observability**
- `Journal.log(event)` (appends to `events.jsonl`), `LineageView.render()` (DAG), `Report.build() -> ResearchReport`.

**Ingestion layer** ([ADR-3](03-decisions.md))
- `Ingestion.goal_anchor` / `.ingest` / `.retrieve_more` / `.analyze` / `.plan -> Backlog`.

**Tracking layer** ([ADR-4](03-decisions.md))
- `Tracker.log_run` / `.set_lineage(parent_ids)` / `.reproduce(run_id)` / `.branch_from(run_id, overrides)`.

---

## 7. Non-functional requirements

| Dimension | Requirement |
|-----------|-------------|
| **Cost control** | Hard caps on tokens *and* compute/wall-clock; per-thread budget allocation; cost shown live. |
| **Safety** | All agent-written code runs sandboxed (container/subprocess), resource-limited, network-off by default; agent edits a constrained surface only. |
| **Reproducibility** | Pinned deps, controlled seeds, git commit per node, full lineage from any result back to root. |
| **Reliability** | No result promoted without validity + variance checks; reward-hack regression suite always runs. |
| **Backend flexibility** | Works with any LiteLLM-supported provider; per-role override; graceful degradation if a model is unavailable. |
| **Observability** | Every experiment logged with inputs, diff, metric, cost, verdict; lineage DAG viewable. |
| **Extensibility** | New task adapter / search policy / evaluator check addable without forking core. |

---

## 8. Inputs & outputs

**Input:** a `task.yaml` (dataset/handle, metric, direction, budgets, constraints) + a `config.yaml` (models per role, backend, search params, evaluator settings).

**Output:** a `ResearchReport` containing:
- Best **verified** solution (code + exact reproduction command + git ref).
- Metric with variance/confidence (multi-seed).
- Ranked log of all experiments (what was tried, diff, result, verdict, why kept/killed).
- Lineage DAG + cost summary.
- List of reward-hacks caught (transparency).

---

## 9. Success metrics (how we know LoopLab is good)

1. **Benchmark strength** — medal rate on a held-out MLE-bench subset; should be in AIDE→R&D-Agent range and improve with the evaluator/archive on.
2. **Trust** — % of promoted results that reproduce on a fresh seed/machine (target ≥95%); number of reward-hacks caught vs leaked.
3. **Efficiency** — improvement per token and per GPU-hour; experiments/hour throughput.
4. **Backend portability** — runs end-to-end on (a) all-API, (b) all-local, (c) hybrid configs with no code change.
5. **Extensibility** — time to add a new task adapter or evaluator check (target: < 1 day, no core edits).

---

## 10. Phased delivery

*(Mirrors the engineering build order in [02 §17](02-architecture.md); re-ordered by [ADR-6](03-decisions.md) — result-moving levers first, infra later.)*

| Phase | Scope | Looks like |
|-------|-------|-----------|
| **P0 — Working loop** | Single-file adapter, per-role Researcher/Developer, sandbox, `GreedyTree`, LiteLLM, git commits, **`events.jsonl` + static HTML lineage tree**. | Karpathy/AIDE-class, backend-flexible, observable — minimal infra. |
| **P1 — The levers that move results** | **Rich operators** (draft/depth-bounded debug/improve/ablation-refine) + **leakage checker** (train/test+temporal+target) + **consistent evaluation** + robust-CV selection. | Where medal-rate is actually won (AIRA+MLE-STAR). |
| **P2 — Ensembling + frontier rigor** | First-class **ensemble/merge operator** (iteratively refined) + **top-k multi-seed confirmation** + DAG data model (`parent_ids`). | Best-evidenced lift (MLE-STAR/KompeteAI). |
| **P3 — Scale + reproducibility** | **Parallel/throughput test-time scaling** + cheap proxy evals + **MLflow optional exporter** + Textual TUI. | Throughput (AIRA_2) + reproducibility. |
| **P3.5 — Grounding** | **Lightweight** retrieve-and-seed (≈4 candidates) + data profiling; heavier ingestion (Docling/web) behind a flag. | MLE-STAR-style, kept light. |
| **P3.6 — Knowledge & capability** | **MCP capability bus** + **Agent Skills** (migrate seed-knowledge) + **prompt store** + **AGENTS.md** generation. | [ADR-8](03-decisions.md)/[ADR-9](03-decisions.md). |
| **P3.7 — Cross-run memory** | Episodic **case library** + distilled **lessons** with retain-on-improvement + confidence gates. | [ADR-10](03-decisions.md) — top-system differentiator. |
| **P4 — Breadth + hardening + opt-in machinery** | `MLEBenchAdapter` + `KernelSOLAdapter` + **React Flow web UI** + hardening pass ([ADR-11](03-decisions.md)); opt-in: `Evolutionary`/`MCTS`, diversity archive, co-evolving evaluator. | Production breadth; demoted bets become measurable experiments. |

Each phase is independently useful and shippable.
