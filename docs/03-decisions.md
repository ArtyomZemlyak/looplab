# LoopLab — Decision Records (added requirements)

**Version:** 0.1 · **Date:** 2026-06-20
**Companion docs:** [01-product-design.md](01-product-design.md) · [02-architecture.md](02-architecture.md) · [04-file-layout.md](04-file-layout.md) · [05-build-decisions.md](05-build-decisions.md) · research basis: [autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)

> This document records the five new requirements you added, the options researched for each, and the **evidence-backed decision**. Each decision is then folded into the architecture doc. Evidence tags: **[IND]** independent, **[SR]** self-reported, **[BENCH]** standardized benchmark.

---

## ADR-1 — UI as a decoupled layer (files-first → TUI now → web later)

**Requirement.** Explore everything that happens (traces, flow, tree/graph, per-node/experiment info). The UI must be a **separate layer** that reads state from the backend or (preferably) **files** and renders it, and can optionally **write changes back** (act/edit while a run is in progress). Decide between: (A) decoupled UI off backend/files, (B) terminal/TUI first, (C) "just files."

**Key finding from research.** Your hard constraint *is literally* three established patterns combined: **event sourcing** (append-only log is source of truth, state = fold over the log), **local-first** (the directory is the primary copy), and **CQRS** (engine writes, UI builds read-models). Also: in every real tool surveyed (W&B, Aim, MLflow, Phoenix, LangSmith, Langfuse), the "live" feel comes from **client polling / file-tailing, not server push** — so you do **not** need a streaming backend to feel live.

**Decision — do all three, layered on one file contract. C is the foundation, B is the first renderer, A is the second.**
1. **Files are the source of truth** — the engine writes an append-only `events.jsonl` + per-node files; current state = fold over the log. *(Event sourcing.)*
2. **Single-writer principle** — the **engine is the only writer of run state**. The UI never mutates state directly; it **appends intents** to `commands.jsonl` (imperative: pause/resume/fork) and/or writes a level-triggered `desired_state.json` (declarative, crash-safe, GitOps-style reconcile). The engine folds these in on its tick and emits a `command.ack` event. This eliminates UI↔engine races by construction.
3. **TUI now** — a **Textual** app (`Tree` widget for the search DAG, `DataTable` for metrics, a log pane tailing node logs, key-bindings for actions). One Python process, no frontend build. Crucially **not a dead end**: `textual-serve` serves the *same* TUI to a browser over xterm.js for free.
4. **Web later** — FastAPI (tails JSONL / queries an optional SQLite projection) + **React Flow/xyflow** + a layout engine (**ELK/elkjs**, or dagre) for an interactive zoomable DAG. Same files; the web UI is just a second projection — exactly how DVC's CLI, VS Code extension, and Studio all coexist on the same files.

**Live updates** = UI watches the dir with `watchfiles` (OS events; polling fallback for network mounts) and tails new JSONL lines. **Safety rule: atomic writes** — temp-file + `os.rename()` for `*.json`; JSONL append is line-atomic. **Version every record** (`"v": 1` + `"type"`) from day one so future renderers can upcast old events without rewriting stored bytes.

**Why this and not web-first (pure A):** web-first on a DB is the most effort, locks you to one renderer, and buys nothing on liveness that file-tailing doesn't. Do it last, as a projection.

**File contract (sketch — the authoritative, expanded on-disk spec is [04-file-layout.md](04-file-layout.md)):**
```
run_dir/
  state.json          # current run status (atomic rewrite)
  desired_state.json  # control plane (declarative; engine reconciles toward it)
  events.jsonl        # APPEND-ONLY event log  ← source of truth
  commands.jsonl      # UI→engine intents (engine is sole reducer)
  nodes/<id>/         # code, logs, metrics, artifacts (see 04 for full per-run layout)
  _derived/           # OPTIONAL rebuildable projections (SQLite/Parquet/index) for fast UI queries
```
Event envelope (one JSON value per line): `{v, type, ts, run_id, node_id, parent_ids[], seq, data}`. `seq` is a monotonic per-run sequence (ordering + optimistic-concurrency guard); **do not assume file order** (OTel JSONL gives no ordering guarantee — UI sorts by `seq`/`ts`).

> ⚠️ This is the minimal control/event contract. [04-file-layout.md](04-file-layout.md) supersedes it with the full data-class taxonomy (docs, config, metrics, weights/data, content-addressed store) and the canonical-vs-derived rules.

**Consequences.** Decoupling, replay/resume, and "richer UI later" all fall out for free. Cost: discipline (atomic writes, single-writer, versioned records). Tech: Textual, `textual-serve`, `watchfiles`; later FastAPI + React Flow + ELK.

---

## ADR-2 — Pluggable / overridable research algorithm

**Requirement.** The research/search algorithm must be changeable (plugin or override).

**Decision.** The search strategy is a **first-class plugin** behind a `SearchPolicy` protocol, selected by config and injected by the orchestrator. The core loop knows only the protocol, never a concrete algorithm.
```python
class SearchPolicy(Protocol):
    def select(self, dag: ExperimentDAG, archive: Archive) -> Node: ...      # where to expand
    def expand(self, node: Node) -> list[Idea]: ...                          # how to branch (optional override)
    def should_merge(self, archive: Archive) -> tuple[Node, ...] | None: ... # branch-and-combine trigger
    def stop(self, state: RunState) -> bool: ...                             # custom stop condition
```
Built-ins ship as separate classes: `GreedyTree` (AIDE-style), `Evolutionary` (archive + merge, AlphaEvolve-style), `MCTS` (SELA-style, for AutoML-pipeline tasks). Users add a new algorithm by implementing the protocol and naming it in `config.search.policy` — **no core edit, no fork**. The same injection pattern already covers `TaskAdapter`, `ExploitRule`, `VarianceGate`, and the LLM backend (see [architecture §13](02-architecture.md)). *(This realizes AIDE's "swap in new search heuristics" promise as a config switch instead of a code fork.)*

---

## ADR-3 — Ingest & analyze existing artifacts *before* planning experiments

**Requirement.** The system can be given pre-existing artifacts (web pages, tables, files, papers, prior results) and must **analyze them against the research goal first**, then form an experiment plan — instead of starting blind.

**Key finding.** Every serious system does this as a **front-loaded grounding pre-phase** with the same skeleton, and grounding measurably helps: Google's **MLE-STAR** retrieves SOTA models/code from the web *before* writing a solution and reaches **63–64% medals on MLE-bench-Lite vs ~25.8% for the blind AIDE baseline** [BENCH/SR]. **PaperQA2**'s goal-conditioned per-source summarization ("Rerank + Contextual Summarization") is **superhuman on literature QA** (precision 85.2% vs 64.3% human, p=0.0029) [SR].

**Decision — add an `Ingestion → Analysis → Planning` PRE-PHASE that runs once before the search loop**, plus a **re-grounding** hook inside the loop.

Pipeline:
0. **Goal anchor** — parse the task into an **immutable structured config**: success metric, constraints, and hypothesis quality criteria (novelty/feasibility). Stays fixed for the whole run so re-grounding never drifts off-task. *(co-scientist "research plan configuration"; AIDE's objective `h(s)`.)*
1. **Ingest** heterogeneous artifacts → a canonical lossless structure + Markdown + chunks **with provenance** (source, section, page, offsets, DOI). Libraries: **Docling** (default, MIT, broad formats), **GROBID** (scholarly PDFs + references), **trafilatura** (web pages), **Table Transformer/Docling TableFormer** (scanned/merged-cell tables), **repomix** (code repos).
2. **Retrieve-more** to close gaps vs the goal — web search (MLE-STAR style: pull SOTA model+code candidates) + literature (Semantic Scholar / arXiv).
3. **Store / ground (dual representation)** — hybrid **vector + contextual-BM25** with **Anthropic Contextual Retrieval** (−67% retrieval failures with reranking) **+ GraphRAG** entity graph + community summaries for global "what's known / what's missing" questions. Escape hatch: **corpus < ~200k tokens → skip RAG, load in-context.**
4. **Analyze (goal-conditioned)** — per-artifact extractive summary with span citations (PaperQA2 RCS), GraphRAG global search for gaps, and a **novelty gate** vs literature (AI-Scientist's Semantic Scholar check) so the agent doesn't re-discover known results.
5. **Plan** — generate hypotheses grounded in step 4 (each with provenance) → filter (correctness/feasibility/novelty) → **rank via Elo tournament + dedup** → emit a **prioritized experiment backlog** `{hypothesis, method, expected outcome, metric}`. *(Google AI co-scientist.)*
6. **Re-ground (in-loop hook)** — after results arrive, append a meta-review critique to the planner prompt and re-rank the backlog; re-run analysis if a result invalidates an assumption. No retraining — long-context feedback only.

**Contract handed to the search loop:** a queryable **knowledge store** + the **immutable goal anchor** + a **ranked experiment backlog** (which seeds the Researcher's proposals). Retrieval is exposed as **agentic tools** (vector/graph/web), not a fixed step, so the planner can re-query during the loop.

**Caveat to design against** [IND, arXiv 2502.14297]: evaluations of AI-Scientist found it modified code only ~8%/iteration and 5/12 experiments failed on coding errors — so the planner must produce *concrete, runnable* experiment specs, not vague directions.

---

## ADR-4 — Reproducibility & experiment tracking

> **⚠️ Re-prioritized by [ADR-6](03-decisions.md):** the conclusion below ("adopt MLflow core") is **demoted** — the event log is the source of truth and **MLflow becomes an *optional exporter*, not a core dependency**. The MLflow API details remain accurate; just treat it as swappable and off the hot path.

**Requirement.** Experiments must be reproducible: (a) consider native **MLflow core** integration (not its UI); (b) or build our own — assess hardness; (c) be able to fall back to an older experiment, reproduce it or pull its full info, then **branch from it**.

**Key finding.** MLflow's **core can run fully headless** — no server, no UI: `MlflowClient` + a local **SQLite backend** (`sqlite:///mlflow.db`) gives params/metrics/artifacts/tags/search/reload, **auto-captures the git commit**, supports `autolog` (sklearn/pytorch/…), model signatures, and a model registry. The **only** thing it doesn't model natively is a **multi-parent DAG** (a run has exactly one `mlflow.parentRunId`) — but neither does any mainstream tool, and you'd have to build the DAG layer anyway (see ADR-5).

**Decision — HYBRID: adopt MLflow core as the storage/logging substrate, layer our own DAG on top.**
1. **Backend:** `mlflow.set_tracking_uri("sqlite:///<root>/mlflow.db")` + local artifact root. SQLite (not the file store) → fast search **and** model registry, still serverless. Use `MlflowClient` (explicit, thread-safe) since the engine runs many concurrent experiments.
2. **Lineage (the one thing we build):** on each run set a `parent_ids` tag = JSON list (1 parent = normal branch; N = branch-and-combine), plus also set `mlflow.parentRunId` to the primary parent so MLflow's own grouping still works. The DAG index lives in our engine (rebuildable from tags via `search_runs`).
3. **Reproducibility:** rely on automatic git-commit capture; additionally log a `requirements.txt`/lockfile artifact per run; turn on `autolog` where applicable. A result is reproducible from *(recorded git commit) + (logged params) + (logged env/artifacts) + (recorded seeds)*.
4. **Fall-back / branch from an old experiment:** `search_runs` to find it → `get_run` for config → `mlflow.artifacts.download_artifacts` / `load_model("runs:/<id>/...")` for artifacts → start a **new** run that copies params, checks out the recorded commit, sets `parent_ids=[old_id]`, mutates the delta, and continues. This *is* the "reproduce, then branch" flow.

**Why not roll our own fully?** A home-grown tracker would have to redo robust artifact handling, concurrency/locking, schema migrations, search performance, and autolog/signatures — MLflow gives all of that for free; the genuinely custom part (the DAG) we own anyway. **Why not Aim/W&B/ClearML/DVC?** Aim has no native lineage + weaker Windows support (you're on Windows 11); W&B/Neptune backends are proprietary (disqualifying for embedded/no-server); ClearML's best lineage needs its server; DVC is git-CLI-first. MLflow core is the best fit for *headless + OSS + serverless + tree/DAG*.

**Note:** MLflow is the **reproducibility/lineage substrate**; the `events.jsonl` from ADR-1 is the **live-observability** stream. They are complementary — the UI tails events for liveness; MLflow is the durable, queryable record of record. (We can also mirror runs as OTel/OpenInference spans later for Phoenix/Langfuse interop.)

---

## ADR-5 — Graph vs tree (and why "tree-with-merge" wins)

**Requirement.** Consider a **graph** instead of a tree, since DNN training params/design choices all influence each other — but research whether graph methods actually deliver (maybe prior work already settles it).

**Key finding — your intuition is *half* right, and the evidence is clear and mostly [IND].**
- The **strong claim "a full graph models reality better, so it gets better results" is NOT supported.** Nobody has shown graph *topology per se* beats a tree at equal compute. Graph-of-Thoughts only wins on hand-picked, hand-built decomposable synthetic tasks; successors (XoT, Buffer-of-Thoughts) win by *reducing* topology. Across LATS/CodeTree/Reflexion ablations, **the evaluator/value signal — not the branching — drives results** (LATS: removing the value function costs 5× more than switching MCTS→DFS). At equal compute, **MCTS and PRM-guided tree search do not beat simple sampling / best-of-N** [IND].
- **What *is* supported:** (1) keeping a **diversity-preserving archive** with fitness+novelty parent selection beats greedy hill-climbing and best-of-N (ShinkaEvolve ablation; MAP-Elites theory [IND]); (2) **merging genuinely-distinct improvements** helps in the one in-domain system that tried it — **KompeteAI** reports 51.5% vs AIDE 34.3% on MLE-bench-Lite with a DAG-with-merge agent [SR, unverified]; (3) **lineage** (not search) is correctly a DAG — MLflow/W&B/DVC all model it that way because one solution feeds many children and a merge has many parents.
- **Real costs of full graphs** [IND]: multi-parent **credit assignment is genuinely broken** (information leak, double-counting, path-dependent values / "graph history interaction" — no canonical merge rule); search-space blowup; recurring per-task engineering. Controlled merging only pays off when the *same state is reached many ways and equality is cheap & path-independent* (chess transposition tables: +69–310 Elo).

**Decision — model the system as an append-only DAG of immutable experiment nodes ("tree-with-merge"), but keep value/credit tree-like.**
1. **Default search = best-first tree** (AIDE-style: Draft / Debug / Improve). Proven, low-risk backbone.
2. **Add a gated Merge operator** (the one idea worth taking from GoT/KompeteAI): occasionally pick 2–3 high-scoring nodes from *different lineages that touched different parts of the pipeline* (data vs model vs training) and prompt the LLM to combine them → a child with multiple parents. Gating to disjoint pipeline parts is exactly your "design choices influence each other" case **and** avoids the credit-assignment ambiguity.
3. **Maintain a diversity archive** (MAP-Elites-style buckets by approach family / island model); select parents by **fitness + novelty**, not pure greedy. *(This is the best-supported gain.)*
4. **Sidestep DAG credit assignment:** each node's score is **evaluator-derived and standalone** — we **never back-propagate value across a merge**. The DAG is for **lineage, provenance, and parent-sampling** (where DAGs are proven correct), not for MCTS-style multi-parent value backup (where they're proven problematic).
5. **Spend the engineering budget on the evaluator**, not topology — the consistent [IND] lesson.

**Data model (graph benefits, minimal graph complexity):**
```python
Node {
  id, code_ref, parent_ids: [id],     # 1 parent = tree edge; >1 = merge node  ← the whole graph capability
  metric, eval_score, ci, status,     # evaluator-derived, standalone (no back-prop)
  approach_tags: [...],               # MAP-Elites diversity buckets
  created_from_op: draft|debug|improve|merge,
  novelty_score
}
```
`parent_ids` as a **list** is the entire graph capability — single-parent nodes form a tree; merge nodes have several. It's just an adjacency list in a table; no graph-search engine required. This aligns 1:1 with the MLflow `parent_ids` tag (ADR-4) and the `parent_ids[]` event field (ADR-1).

**Consequence.** You get composable multi-parent improvements (your real insight), proven diversity gains, and clean credit assignment — without GoT-style hand-built graphs or MCTS multi-parent backprop. Treat "merge beats tree-only" as a **hypothesis to A/B test** in your own runs (KompeteAI is one unverified data point), not a guarantee.

---

## Summary — how the five decisions wire together

| Req | Decision | New/changed component |
|-----|----------|-----------------------|
| UI | Files-as-truth (event sourcing) → Textual TUI → web; engine is sole writer, UI appends commands | **Observability layer** (§ arch), event/command schema |
| Pluggable algo | `SearchPolicy` plugin, config-selected, DI'd | Search Policy (already a plugin — made explicit) |
| Ingest artifacts | `Ingestion→Analysis→Planning` pre-phase + in-loop re-grounding; immutable goal anchor; ranked backlog | **Ingestion/Knowledge component** (new) |
| Reproducibility | MLflow core (SQLite, headless) + own `parent_ids` DAG tag; reproduce-then-branch | **Tracking/Reproducibility layer** (new) |
| Graph vs tree | Tree-with-merge DAG + diversity archive; scores standalone; invest in evaluator | **Data model → DAG**; Merge operator; Archive |

All five are folded into [02-architecture.md](02-architecture.md) (data model, components, principles, phased build).

---

## ADR-6 — 2026 SOTA re-review: are we actually better, and what to change

**Date:** 2026-06-21. After a fresh comparison against the *current* best systems, this ADR records the honest verdict and **re-prioritizes** ADR-1…ADR-5. It is the most important decision record — where it conflicts with ADR-1…5, **ADR-6 wins.**

### What changed in the world (correct the research doc)
- **The field jumped.** MLE-bench *full* leaders are now ~**60–70%** any-medal ([SR]; e.g. AIBuildAI-2 70.7% Claude-Opus-4.7, Famou-Agent 2.0 64.4% Gemini-3-Pro), up from sub-50% in mid-2025. **R&D-Agent (30.22%) no longer leads.** The official OpenAI leaderboard has been **frozen since 2026-04-24** and has **no independent verification** — every number above the original o1-preview+AIDE **16.9% [IND]** is self-reported, and base-model upgrades (Gemini-3-Pro, Claude-Opus-4.6/4.7) explain much of the jump.
- **The decisive study: Meta FAIR's AIRA** ("AI Research Agents for ML", [arXiv:2507.02554](https://arxiv.org/abs/2507.02554); follow-up AIRA_2) — **not DeepMind**. Its findings reshape our design:
  1. **Operators > search policy.** With weak operators, MCTS/evolutionary gain **nothing** over greedy; greedy is actually *best at long budgets* (MCTS overfits and decays). Only richer operators unlock policy gains, and even then MCTS ≈ greedy ≈ evo (~1–2 pts apart).
  2. **Validation rigor is the single biggest lever.** Selecting by validation instead of test costs **9–13 medal points**; AIRA_2's "Hidden Consistent Evaluation" recovered **+15 points** and removed long-horizon decay.
  3. **Throughput-based test-time scaling** (parallel samples), not wall-clock, is what pays — and only paired with consistent evaluation (else it overfits).
- **Ensembling is the best-evidenced single lift.** MLE-STAR's iteratively-refined ensemble: 37.9%→43.9% (gold 25.8%→30.3%); KompeteAI's merge operator is worth ~**9 points** (removing it: 47.6%→38.5%).

### Verdict: are we better?
**On our chosen axes — yes; on raw results — not as designed.**
- ✅ **Genuine, still-defensible edges:** per-role model routing ([ADR-4 idea]), backend portability (local+API), the **append-only event-log spine** (OpenHands-validated), reproducibility, and **temporal/target data-leakage detection** (a real gap *no* system covers well). No single OSS system combines these.
- ⚠️ **Where we were over-engineered or off-target:** we spent our differentiation budget on **search machinery and a hardened evaluator** — exactly the things AIRA says *don't* move the needle — while **under-weighting the things that do** (operators, evaluation rigor, ensembling). A results-focused reviewer would say our design optimizes bookkeeping over capability.

### Re-prioritization of ADR-1…ADR-5 (this supersedes their emphasis)

| Prior bet | 2026 verdict | Change |
|-----------|--------------|--------|
| **ADR-5** tree-with-merge DAG **+ diversity archive** | Merge = **keep (high value)**; diversity archive + fancy policy = **demote** (unproven on MLE-bench; greedy+good-operators wins) | Default = **greedy tree + strong operators + merge**. Archive/MCTS become *ablation-gated experiments*, not core. |
| **ADR-1** decoupled UI (Textual→web) | Event-log spine = **keep**; early decoupled UI = **defer** | Ship an **AIDE-style static HTML tree** first; build the TUI/web UI only once the agent gets results. |
| **ADR-4** MLflow **core** | Reproducibility = keep; **MLflow-as-core = demote** | Event log is the source of truth; **MLflow becomes an optional exporter** (a thin adapter), not a core dependency. No surveyed top system uses MLflow. |
| **co-evolving evaluator** (ADR-5/arch §7) | **Over-engineered for fixed-metric tasks** | Lead with a **data-leakage checker** (train/test **+ temporal + target**) + **consistent-evaluation protocol**. Keep the co-evolving adversarial evaluator **only** for future open-ended/self-metric mode. |
| **multi-seed variance gating p<0.01** (arch §8) | Right instinct, **too expensive as specified** | Default to **robust CV** for the validation metric everywhere; reserve **multi-seed confirmation for the top-k promotion frontier only**; replace p<0.01 with a practical-significance margin (> 1 SE). |
| **ADR-2** pluggable algorithm | **Keep** (cheap, correct) | No change — but the *default* plugin is greedy+operators+merge. |
| per-role routing ([ADR-4 model split]) | **Keep — our most defensible bet** | Add a third cheap tier for mechanical roles (debug loop, checkers, summarization). |
| **ADR-3** ingestion pre-phase | **Right direction, lighten it** | **Lightweight grounding + data profiling** (≈4 retrieve-and-seed candidates + schema/leakage profiling), not a heavy RAG pipeline. The profiling doubles as the leakage checker's front-end. |

### New first-class capabilities to ADD (highest ROI — these are what make us competitive)
1. **Ensemble/Merge operator** — LLM-proposed, iteratively-refined fusion of top solutions (in-loop, not terminal voting). *Best-evidenced lift in the field.*
2. **Ablation-driven targeted refinement** — generate an ablation to find the highest-impact code block, then deeply optimize *that* block (MLE-STAR's signature; an AIRA-class "strong operator").
3. **Depth-bounded Debug operator** — explicit, capped iterative repair as a first-class operator (every strong system has one).
4. **Leakage checker** — train/test **+ temporal + target** leakage auto-detection (our genuine differentiation opportunity; only MLE-STAR ships even a partial one).
5. **Consistent, leakage-proof evaluation + throughput-based parallel test-time scaling** — the highest-ROI levers per AIRA (+10–15 pts), plus cheap proxy evals (subset/reduced-epoch) to multiply iterations.

### The one-line strategy correction
> **Differentiate on operators, evaluation rigor, ensembling, and leakage-safety — not on search bookkeeping or a hardened evaluator.** Keep our real edges (per-role routing, backend portability, event-log reproducibility, leakage detection); demote the machinery the evidence says doesn't move results.

All of ADR-6 is folded into [01-product-design.md](01-product-design.md), [02-architecture.md](02-architecture.md), and [04-file-layout.md](04-file-layout.md); the leaderboard correction is in [autoresearch-systems-exploration.md](autoresearch-systems-exploration.md).

---

## ADR-7 — Pluggable role backends: raw-LLM *or* full external coding-agent CLI

**Date:** 2026-06-21. **Requirement.** A role like **Developer** must be able to be backed not only by a single API/local LLM call, but by a **complete external coding agent** (Claude Code, OpenCode, Aider, OpenHands, SWE-agent) — to get max quality per phase without reimplementing a coding agent from scratch.

**Key finding.** The strong agent CLIs are *already* event-sourced, LiteLLM-backed, sandboxed loops — the same architecture as LoopLab — so this is **nesting a compatible sub-engine, not bolting on an alien.** They all support headless, unattended, structured-output invocation:
- **OpenHands** (MIT): `openhands --headless --json` → JSONL event stream; Python `software-agent-sdk`; LiteLLM (local+API); Docker sandbox; always-approve in headless. **Best fit** (its event-sourced/LiteLLM/sandboxed internals mirror ours).
- **Aider** (Apache-2.0): `--message --yes-always --no-stream`; the artifact *is a git commit/diff*; LiteLLM (Ollama/OpenAI-compatible). **Simplest to wrap.**
- **SWE-agent / mini-swe-agent** (MIT): `run`/`run-batch`, importable `RunSingle`; emits a clean **`.patch`** + JSON trajectory; LiteLLM. **Cleanest diff artifact.**
- **Claude Code** (proprietary, Anthropic-API-shaped only — no real local models): best DX, `-p --output-format json --max-turns`, Agent SDK. Use only if those constraints are acceptable.
- Excluded for failing local-model/OSS: **Gemini CLI** (Google-only), **Cursor CLI** (hosted+closed).

**Decision — add a `RoleBackend` abstraction one level ABOVE `LLM.complete()`** (which returns text; a full agent returns a *workspace mutation*). A role's work is requested as `RoleRequest` and returns a `RoleResult` whose contract output is a **surface-filtered diff + normalized events + cost + summary + status** — *never* the agent's raw chat message. Three interchangeable impls, selected per-role by config exactly like `roles.*.model`:
- `LLMBackend` — wraps the existing single `LLM.complete()` (today's Developer).
- `CliAgentBackend` — generic subprocess wrapper + per-agent adapter (OpenHands/Aider/SWE-agent/Claude Code).
- `LibAgentBackend` — in-process import where a real API exists (SWE-agent `RunSingle`, OpenHands SDK) — avoids subprocess fragility.

```yaml
roles:
  developer: { backend: cli_agent, agent: openhands, model: "anthropic/claude-opus-4-8",
               temperature: 0, seed: 1234, scope: step, limits: {max_turns: 30, walltime_s: 1800, usd: 4.0} }
  researcher:{ backend: llm,       model: "openai/o3" }
```

### The two load-bearing rules
1. **Delegate the *step*, own the *loop*.** The external agent backs only the inner "implement / refine_block / debug this one change" step (one bounded invocation per operator application). **Your search loop, operators (ablation-refine, ensemble-selection, depth-bound), evaluator, leakage checker, variance gate, and budget stay yours** — they are the ADR-6 moat, and the MLE-bench evidence is that *narrow* optimization loops (AIDE) beat *general* agents (OpenHands 4.4% vs AIDE 8.7% at equal model) on the tight ML loop. Do **not** let one agent own a whole node or the search (cost 10–100×; bypasses your rigor). Expose `scope: step|node` to A/B it.
2. **Enforce constraints by construction + adjudication, never by instruction.** For each invariant:
   - **Edit surface** → run the agent in a throwaway **git worktree**; afterward `git diff` and **reject/strip out-of-surface hunks** (agents wander; don't trust the prompt). *(Rule 3)*
   - **Sandbox** → run the **agent process itself** inside our sandbox (network-off — also anti-cheat; resource/time caps), since it executes code mid-run. Nest around the agent's own Docker. *(§10)*
   - **git-commit-per-experiment** → the agent never commits to our lineage; **we** commit the filtered diff with `parent_ids`/`created_by=openhands@<ver>`. *(Rule 4)*
   - **Event log** → a per-agent **normalizer** folds its JSON/JSONL stream into our `events.jsonl` (namespaced `node.developer.subagent.*`, versioned); raw stream stashed in the content-addressed `store/`. *(Rule 12, [04](04-file-layout.md))*
   - **Reproducibility** → pin `{agent, agent_version (container digest), model, temperature, seed, cli_flags_hash}`. Full agents are only *weakly* reproducible (multi-turn nondeterminism) — but the **committed diff is the reproducible artifact**, not the trajectory.
   - **Budget** → pass caps as CLI flags (`--max-turns`, cost cutoff) **and** an external SIGKILL watchdog tailing streamed cost; debit the global budget from the returned `CostReport`. (One agent call can cost $1–$30 = dozens of raw-LLM nodes — the economic reason to delegate only the step.)

### Which roles get a full agent
| Role | Backend | Why |
|------|---------|-----|
| **Developer** (`implement`/`refine_block`/`debug`) | **Full agent** (default) + raw-LLM fallback tier for trivial edits | Multi-step coding/debugging is the agent's clear strength. |
| **Researcher** (`propose`) | **Plain LLM** (reasoning) | Idea generation is single-shot reasoning + archive context; an agent buys nothing at 10× cost. *Exception:* a web/code-grounding sub-task may use agent/ingestion tools. |
| **Evaluator / Leakage-checker** | **Plain LLM + our code** | Metric-touching logic must be deterministic and trustworthy — never delegate to an opaque agent (reward-hack/leakage vector). |
| **Ablation / Ensemble selection** | **Our code + plain LLM**; agent only for the narrow edit | Selection/measurement = our rigor; only mechanical code is agent-worthy. |

**Consequences.** Max quality on the coding phase via best-in-class agents, free maintenance as they improve — while keeping every invariant and the ADR-6 operator moat. Cost/nondeterminism/version-drift are the prices, mitigated by step-granularity + version pinning + external budget watchdog. This **extends ADR-2's plugin philosophy to the *backend* dimension** and reuses [ADR-1](03-decisions.md) (event log), [ADR-4](03-decisions.md) (lineage), and [§10](02-architecture.md) (sandbox). **First integration to build:** `CliAgentBackend(openhands)` (or `aider` for simplicity) behind `Developer.implement()` — slots into P0/P1 with no change to the rest of the loop.

---

## ADR-8 — Prompt & instruction management (+ AGENTS.md bridge)

**Requirement (your questions).** Where are LLM prompts stored? Can we edit them in the UI? How do we instruct the external coding-agent backends (ADR-7)?

**Decision.**
- **Prompts are config, not code → stored as Markdown + YAML frontmatter files in git**, one per role×operator (e.g. `prompts/developer/debug.md`); frontmatter carries `id, version, model, inputs`. Best git-diff story, trivial to render/edit in our own UI, dead-simple hot-reload. (Considered `.prompty`/Dotprompt/POML/BAML — adopt **BAML only for the Evaluator/structured-output** prompts that need type-safe parsing; plain Markdown for the rest.) **Concretized in [ADR-14](05-build-decisions.md):** structured output defaults to **standard tool calling** (LiteLLM→pydantic; prompts stay MD files) with **BAML (SAP) as a secondary fallback** for weak local models + the Evaluator `Verdict` — a per-role `parser` strategy. Prompt bodies stay hot-reloadable/UI-editable.
- **UI-editable: yes.** A thin editor over the canonical files (the engine watches the dir and hot-reloads). In front of the loader, a **Langfuse-style cache** (TTL + stale-while-revalidate + fallback-to-committed-file) lets a UI edit take effect without redeploy while git stays the safety net. **Pick ONE canonical store — do not attempt two-way file↔registry sync** (unsolved in 2026; drift risk). If a non-engineer live-edit workflow is required, make **Langfuse** (MIT, self-hostable) canonical with one-way export-to-git for audit + a `prompt_hash` mismatch alarm. Default: **files canonical, UI commits via PR**, promotion gated on eval.
- **Optimization later, not now:** hand-write to ship; once graded traces accumulate, compile **Evaluator and Developer** prompts with **`dspy.GEPA`** (reflective, NL-feedback evolution; reported >10% over MIPROv2). Keep **Researcher** prompts hand-authored (open-ended ideation has no single metric).
- **AGENTS.md is the bridge to external agents ([ADR-7](03-decisions.md)).** Adopt **AGENTS.md** (the cross-tool standard, now under the Linux Foundation's Agentic AI Foundation) as the per-run conventions file the orchestrator **generates into the throwaway worktree**. Pass instructions through **three channels, none alone sufficient:** (a) **AGENTS.md** = durable conventions (style, test/build cmds, do-not-touch dirs, the goal anchor); (b) **prompt/message arg** = the specific task + acceptance criteria; (c) **CLI flags** = *enforced* hard constraints (edit-surface, tool perms, turn/cost caps). Bridge to Claude Code via a `CLAUDE.md` that `@import`s AGENTS.md (or symlink); to Aider via `--read AGENTS.md`. Prefer each agent's SDK over raw shell-out; always parse JSON/patch output.

**On disk:** `prompts/<role>/<operator>.md` (canonical, git) + per-run generated `AGENTS.md` in the worktree. See [04-file-layout.md](04-file-layout.md).

---

## ADR-9 — Capability layer: MCP as the tool bus + Agent Skills for recipes

**Requirement (your questions).** Can we set up skills? tools? Standardize how roles and external agents get capabilities.

**Key finding.** **MCP (Model Context Protocol) is the de-facto 2026 standard** (adopted by OpenAI/Google/Microsoft/AWS; donated to the Linux Foundation's Agentic AI Foundation; native client in Claude Code, OpenHands, etc.). **Agent Skills** (`SKILL.md` + progressive disclosure) are *complementary*, not competing: **MCP carries executable capabilities; Skills carry the procedural knowledge that orchestrates them.**

**Decision.**
- **Standardize on MCP as the internal capability bus.** Build one MCP server per trust/lifecycle boundary, consumed identically by our own roles (via function-calling, `async_mcp_tool(...)`) and by external agent backends (via MCP config URL):
  - `knowledge-mcp` (**agentic retrieval toolset**: `grep`/`glob`/`ls`/`read` + `vector_search`/`hybrid_search` — the LLM picks lexical-navigation vs semantic recall per query; see [ADR-16](05-build-decisions.md)),
  - `archive-mcp` (Resources: runs/metrics/artifacts by URI + a `query_archive` tool),
  - `ml-tools-mcp` (`profile_data`, `check_leakage`),
  - `sandbox-mcp` (`run_code` — isolated; the dangerous one),
  - `web-mcp` (consume an existing server / provider web tools).
  The leakage-checker then exists in exactly **one place** and our Evaluator and a delegated OpenHands/Claude Code run get **identical** results. *Caveat:* keep genuinely hot, role-private helpers as in-process functions — don't MCP-ify everything (RPC overhead).
- **Migrate the seed-knowledge library to Agent Skills** (`SKILL.md` directories: instructions + scripts + resources). Progressive disclosure keeps cost ~100 tokens/skill at rest, unbounded payload on demand. ML "recipes" (K-fold CV, Muon optimizer, time-series leakage checks) become skills whose prose guides Claude-family roles and whose executable parts are exposed as MCP tools so **non-Claude backends benefit too**. Don't invent a custom seed-knowledge format — Skills give versioning + distribution + native consumption for free.
- **Tool registry:** one JSON-Schema definition per tool → flows to both our roles and external agents; **per-role allow-lists** are both ergonomics and a security boundary (Researcher: read-only `query_archive`/`web`/`profile`; Developer: + `run_code`; Evaluator: `check_leakage`/`query_archive`/read-only `run_code`).
- **Security:** tiered sandboxes; promote dangerous actions to *typed* tools (not raw bash) so they can be gated/logged; **pin & audit every MCP server like a dependency**; secrets in vaults with egress-time substitution — never in prompt/log/sandbox (see [ADR-11](03-decisions.md)).

**Invariant to design toward:** *one capability lives in exactly one place (an MCP server); one recipe (a Skill) teaches every agent how to use it well.* This overlaps the knowledge architecture ([ADR-10](03-decisions.md)) — Skills are the **procedural** memory tier.

---

## ADR-10 — Unified knowledge & memory architecture

**Requirement (your question).** How is the overall knowledge structured? (We had 3 disconnected stores and **no cross-run experience** — the 2026 finding that *"the gap between has-memory and no-memory is larger than the gap between LLM backbones,"* and that evolving knowledge bases distinguish top systems: AIBuildAI-2 70.7%, ML-Master L3 ablation 72.7%→54.5%.)

**Decision — adopt the CoALA four-type taxonomy; unify the *representation + interface*, keep *indices* separate; add the missing cross-run tiers; files-as-truth on disk.**
1. **Four memory types:** **working** (live run = current archive), **episodic** (past runs/cases — *new*), **semantic** (facts/tricks + distilled lessons — seed library + ingested), **procedural** (recipes + skill code = ADR-9 Skills). Our three old stores map in; **the missing pieces are the episodic case library + the cross-run distilled-lessons tier.**
2. **One representation, one retrieval interface, separate indices.** Shared note schema (content + frontmatter provenance + embedding + tags + `type`); a single retrieval API backed by a **role/goal-conditioned router** over physically separate indices. **Do not merge curated knowledge with distractor-rich ingested RAG in one index** (context-rot/distractor evidence). Different lifecycles: seed=stable, ingested=ephemeral/per-task, archive=append-only/lineage.
3. **Cross-run accumulation = case library (DS-Agent) + distilled wisdom (ML-Master L3), gated for safety:** retain a case **only if it beat the prior best** (retain-on-verified-improvement); rich provenance + **task fingerprint**; tiered promotion (`candidate→distilled→trusted`, promote only after repeat success); **decaying confidence**; contradiction resolution by mark-invalid + append-only ledger (mem0-style), not blind append; **memory-poisoning defense** (filter before injecting; a populated correct memory is itself protective); never recursively train on un-curated self-output.
4. **Feed roles via progressive disclosure:** always-cheap manifest (title/tags/provenance, tens of tokens) → full note on demand → bundled detail as needed; return condensed summaries (~150–200 tokens), not raw blobs.
5. **On disk (files-as-truth, memsearch pattern):** Markdown + frontmatter is canonical/git-tracked; the vector index (and optional graph from `[[wikilinks]]`) is a **derived, rebuildable projection** synced by content-hash.
```
knowledge/
  seed/<topic>.md            # semantic, stable  (now also Skills, ADR-9)
  tasks/<task-id>/<note>.md   # ingested RAG, ephemeral/per-task
  experiments/<exp>.md        # episodic archive, append-only, lineage
  lessons/<topic>.md          # distilled wisdom, cross-task (candidate|distilled|trusted|deprecated)
  index/                      # DERIVED — gitignored, rebuildable (vectors, optional graph, manifest)
```

**Sequencing:** (1) unify the record schema + router over the existing three indices; (2) add the episodic case library with the retain-on-improvement gate; (3) add post-run distillation into `lessons/` with confidence + promotion gates; (4) add reflection/linking once the corpus is large.

---

## ADR-11 — Cross-cutting hardening (the smaller nuances, resolved)

**Requirement.** Clear the remaining un-specified nuances. One decision each:

1. **Secrets / API keys** → front all providers with a **local LLM gateway (LiteLLM Proxy)**; roles/sandbox get short-lived **gateway tokens, never provider keys**; real keys in a secret manager (dev: gitignored `.env`; prod: Vault), referenced not inlined; **redaction filter + `gitleaks`** over the event log. Keys never enter sandbox, prompt, or log.
2. **Configuration** → **`pydantic-settings`** (typed `BaseSettings`) as the single config model, loading layered sources in precedence **defaults < `config.yaml` < `.env` < env vars < CLI** (via `settings_customise_sources` / a YAML source). Typed validation + coercion fail-fast at startup; **nested env override** (`LOOPLAB__ROLES__DEVELOPER__MODEL=...`) for per-field tweaks and profiles. Secrets are **env/`SecretStr` references, never values**, and are excluded from dumps. Write the fully-resolved, **secret-masked** snapshot to `runs/<id>/config.resolved.yaml` + git SHA + `uv.lock` hash — the reproduction key. *(Chose pydantic-settings over Hydra: native env/`.env`/secret handling and one typed model matter more here than Hydra's multirun/sweep composition; layered YAML profiles cover the composition we need.)*
3. **Observability / event taxonomy** → align events to the **OpenTelemetry GenAI semantic conventions** (`gen_ai.*` spans: model, input/output tokens, latency, finish reason, cost) as the canonical trace; domain events (operator-applied, eval-run, gate-decision) as child spans; files-as-truth event log is the durable backing store. Cost/token/latency are first-class on every model/tool event.
4. **Human-in-the-loop** → **approvals are command events** (reuse [ADR-1](03-decisions.md)): engine writes `pending_approval` and halts the thread; human appends `approval_granted|rejected|amended`; engine resumes from the log (identical to crash-resume). Default gates: plan approval (toggle), network egress, budget-threshold, winner-promotion.
5. **Error / retry / resume** → classify failures (transient→exponential backoff+jitter, capped; deterministic→repair/escalate; fatal→checkpoint+stop); **idempotent replay** via content-addressed step ids; resume = replay event log + skip committed steps; a failed parallel thread emits `thread_failed` and becomes a dead branch, never a global abort.
6. **Determinism** → target **reproduce-with-variance** (LLM sampling and GPU FP are not bitwise-reproducible). Record a full reproduction manifest (seeds, `torch.use_deterministic_algorithms`, CUDA/lib versions, dataset hashes, model snapshot + sampling params); report **mean ± std over N seeds** (this is why the variance gate exists, [§8](02-architecture.md)).
7. **Engine self-testing** → **mocked-LLM smoke test** in CI (record/replay) + a versioned **golden-task** regression suite + nightly **canary** on live models + **adversarial fixtures** (planted leakage, known variance) that unit-test the evaluator/gate. Dashboard "engine quality over time" (solve rate, regret vs baseline, $/solved).
8. **Security of agent code** → beyond the sandbox: **deny-by-default egress**; **cgroup/ulimit** CPU/mem/disk/time caps + wall-clock kill; install only from a **pinned lockfile / allowlisted index** (no arbitrary runtime installs); treat all ingested/web content as **untrusted data, not instructions** (delimit + injection-scan + approval gate before any egress/install). Prompt injection from ingested artifacts is a real vector ([ADR-3](03-decisions.md)).
9. **Cost governance** → **hierarchical, gateway-enforced budgets**: run-level cap subdivided per role/thread; in-engine accountant trips an approval gate at 80%, hard-kills at 100%; cost attributed per `(run_id, thread_id, role, model, operator)`; report $/result.
10. **Multi-tenancy / isolation** → **one run = one `run_id` namespace**: dedicated run dir + per-thread **git worktree** + per-thread sandbox container + scoped gateway token + one MLflow run; `run_id`+`thread_id` are required fields on every event and the prefix of every path.
- **Also added (not previously listed):** **data/artifact lifecycle & retention** (GC of old run dirs, large-artifact storage policy, PII in ingested data) and **provenance-for-publishable-results** (a "winner" must carry config snapshot + seeds + data hashes + model snapshot + event-log id so it's defensible/reproducible).

---

## How ADR-6…ADR-11 change the architecture (summary)

| ADR | Adds/changes | Where |
|-----|--------------|-------|
| 8 | Prompt store (`prompts/`, MD+frontmatter, hot-reload, optional Langfuse); AGENTS.md generated per-run | [02 §3.15](02-architecture.md), [04](04-file-layout.md) |
| 9 | Capability layer: MCP bus (`archive`/`ml-tools`/`sandbox`/`web`) + Agent Skills (seed-knowledge migrates) | [02 §3.14](02-architecture.md) |
| 10 | Unified knowledge/memory (CoALA tiers, router, cross-run case library + lessons, files-as-truth) — replaces the disconnected §3.8/§3.10/§3.12 stores | [02 §3.10](02-architecture.md), [04](04-file-layout.md) |
| 11 | Cross-cutting hardening (secrets/config/observability/HITL/resume/determinism/testing/security/cost/isolation) | [02 §18](02-architecture.md) |
