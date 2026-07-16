# LoopLab — Architecture Specification

**Version:** 0.1 (design) · **Date:** 2026-06-20
**Companion docs:** [01-product-design.md](01-product-design.md) · [03-decisions.md](03-decisions.md) · [04-file-layout.md](04-file-layout.md) · [05-build-decisions.md](05-build-decisions.md) · **Research basis:** [autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)

> This document defines **how LoopLab works**: principles, components, data model, control flow, the search and trust mechanisms, extension points, rules/invariants, and a tech stack. It is a from-scratch design (no fork) that **synthesizes the best ideas** from the surveyed systems — each major decision cites its source.

> **Current runtime authority boundary (2026-07-16).** `events.jsonl` is authoritative for the replayable
> `RunState`, not for every byte the product displays. One live engine is fenced by `engine.lock`; the UI
> server can also append control events through `EventStore`'s cross-process serialization. Task/config
> snapshots, diagnostic spans, chat, durable command records and cross-run memory are separate sidecars;
> `replay.fold` deliberately does not reconstruct them. Later sections retain some original design wording,
> but this shipped boundary takes precedence.

---

## 1. Guiding principles

> **⚑ 2026 re-prioritization ([ADR-6](03-decisions.md)).** A fresh SOTA review (Meta FAIR's AIRA study + MLE-STAR + the 2026 leaderboard) shifted our emphasis: **operators, evaluation rigor, and ensembling move results — search machinery and a hardened evaluator do not.** Principle 0 below is now the most load-bearing; where older sections emphasize the diversity archive / co-evolving evaluator / MLflow-core / early UI, ADR-6 demotes them.

0. **Operators beat search policy.** Invest in *what each step does* (draft, debug, ablation-targeted refine, merge/ensemble) before *how nodes are picked*. With weak operators, MCTS/evolutionary gain nothing over greedy; greedy is best at long budgets. *(AIRA [IND-leaning]; [ADR-6](03-decisions.md).)*
1. **Search, don't iterate linearly.** A solution is a *node*; improvements spawn children; the system searches a tree (greedy default), not a single chain. *(AIDE; AIRA: greedy is a strong default.)*
2. **Evaluation rigor is the #1 lever.** Consistent, leakage-proof validation + selecting on a trustworthy validation metric is worth +10–15 points — the largest measured gain in the field. Trust no single noisy run; but spend here on **leakage detection + robust CV**, not on a hardened adversarial evaluator (which only matters when the agent controls its own metric). *(AIRA / AIRA_2; MLE-STAR leakage checker; [ADR-6](03-decisions.md).)*
2b. **Ensemble the winners.** An LLM-proposed, iteratively-refined merge/ensemble of top solutions is the best-evidenced single lift (MLE-STAR, KompeteAI). It is a first-class operator, not a terminal afterthought. *([ADR-6](03-decisions.md).)*
3. **Separate roles; route models per role.** A reasoning model proposes ideas (Researcher); a cheaper instruction-follower writes/repairs code (Developer). *(R&D-Agent: o3 Researcher + GPT-4.1 Developer.)*
4. **Backend-agnostic.** One LLM abstraction over LiteLLM → any API or local model; per-role override. *(R&D-Agent default backend; AIDE's OpenAI-compatible plumbing.)*
5. **Constrained edit surface + commit-per-experiment.** The agent edits a well-defined surface; every accepted change is a git commit → lineage, rollback, reproducibility. *(Karpathy `autoresearch`.)*
6. **Memory is first-class.** A persistent archive of results/summaries/lineage conditions every new proposal. *(AlphaEvolve archive; R&D-Agent's "context from prior experiments".)*
7. **Config over code; explicit plugin seams.** Common changes are config; deep changes implement a documented interface. *(AIDE's "swap in heuristics/evaluators/backends".)*
8. **Sandbox everything the agent writes.** Isolated, resource-limited, network-off by default.
9. **The objective is a reward function.** A benchmark/metric harness with a hard budget returns a scalar + validity flags. *(Karpathy BPB-in-5-min; NanoGPT speedrun; SOL-ExecBench.)*
10. **Ground before you experiment.** Given prior artifacts, analyze them against an immutable goal anchor and build a ranked experiment backlog *before* the search loop. *(MLE-STAR web grounding; co-scientist; [ADR-3](03-decisions.md).)*
11. **Lineage is a DAG; value is tree-like.** Nodes form a "tree-with-merge" DAG (`parent_ids` is a list), but scores are evaluator-derived and standalone — never back-propagated across a merge. *(Evidence: graph topology ≠ better results; archive+diversity and gated merge do help. [ADR-5](03-decisions.md).)*
12. **The event log owns replayable run state; writes are serialized.** Domain and control events share the append-only event-store contract; `engine.lock` permits one live engine while server-owned control appends are cross-process serialized. The UI is a decoupled projection and submits durable intents rather than mutating folded state. Sidecars keep their own authority. *(Event sourcing + local-first + CQRS; [ADR-1](03-decisions.md).)*
13. **Track for reproducibility, separately from observability.** The event log is the replay authority for `RunState`; original diagnostic and snapshot sidecars keep their own authority. **MLflow is an optional headless exporter** (not a core dependency). *([ADR-4](03-decisions.md), demoted by [ADR-6](03-decisions.md).)*

> **Added requirements (2026-06-20):** decoupled UI, pluggable algorithm, artifact ingestion pre-phase, reproducibility/tracking, and graph-vs-tree are specified as decision records in **[03-decisions.md](03-decisions.md)** and integrated below.
> **Further requirements (2026-06-21):** pluggable role backends incl. external coding agents ([ADR-7](03-decisions.md)); prompt management + AGENTS.md ([ADR-8](03-decisions.md)); MCP capability bus + Agent Skills ([ADR-9](03-decisions.md)); unified knowledge/memory ([ADR-10](03-decisions.md)); cross-cutting hardening — secrets/config/observability/HITL/resume/security/cost/isolation ([ADR-11](03-decisions.md)). Folded into §3.4b, §3.10, §3.14, §3.15, and §18.

---

## 2. High-level architecture

```
                          ┌──────────────────────────────────────────┐
                          │              ORCHESTRATOR                  │
                          │  loop · budget · model routing · threads   │
                          └───────────────┬──────────────────────────┘
                                          │ selects node to expand
                  ┌───────────────────────┼───────────────────────────┐
                  ▼                        ▼                           ▼
        ┌──────────────────┐    ┌────────────────────┐     ┌────────────────────┐
        │   RESEARCHER     │    │     DEVELOPER       │     │   SEARCH POLICY    │
        │ propose ideas    │    │ idea→patch, repair  │     │ select/expand/      │
        │ (reasoning model)│    │ (coder model)       │     │ combine (tree+evo) │
        └────────┬─────────┘    └─────────┬──────────┘     └─────────┬──────────┘
                 │   ▲                     │                          │
        archive  │   │ context             ▼ patch                    │
        query    │   │            ┌──────────────────┐                │
                 ▼   │            │     SANDBOX      │                │
        ┌────────────┴───┐        │ run · budget ·   │                │
        │    ARCHIVE /   │        │ seed · capture   │                │
        │  MEMORY/JOURNAL│        └────────┬─────────┘                │
        │ results·lineage│                 │ RunResult                │
        │  ·embeddings   │                 ▼                          │
        └────────▲───────┘        ┌──────────────────┐               │
                 │                 │   EVALUATOR      │◄──────────────┘
                 │   Verdict       │ objective+valid+ │
                 └─────────────────│ anti-hack +      │
                                   │ VARIANCE GATE    │
                                   └──────────────────┘
   ┌───────────────┐   ┌─────────────────────┐   ┌──────────────────────────┐
   │ TASK ADAPTER  │   │ LLM BACKEND (LiteLLM)│   │ SEED-KNOWLEDGE LIBRARY    │
   │ MLE-bench /   │   │ per-role routing,    │   │ Muon, modded-nanogpt      │
   │ single-file / │   │ API + local          │   │ tricks, DS heuristics     │
   │ kernel/SOL    │   └─────────────────────┘   └──────────────────────────┘
   └───────────────┘
```

---

## 3. Components

### 3.1 Orchestrator (Controller)
**Responsibility:** owns the main loop, the budget, model routing, parallel threads, and stop conditions. The only component that mutates global run state.
**Key interface:**
```python
class Orchestrator:
    def run(self, task: Task, config: Config) -> ResearchReport: ...
    # internally: while not stop(): node = policy.select(); expand(node)
```
**Stop conditions:** budget exhausted (tokens or compute), target metric reached, K consecutive non-improving rounds ("dry"), or wall-clock limit.

### 3.2 Task & Benchmark Adapter (plugin)
**Responsibility:** normalize a task and define the **reward function** + validity rules. This is where "what does success mean" lives.
```python
class TaskAdapter(Protocol):
    def prepare(self) -> Workspace: ...          # data, fixed harness, mutable surface
    def edit_surface(self) -> list[Path]: ...    # files the agent may modify
    def run(self, solution, seed, budget) -> RunResult: ...
    def score(self, run: RunResult) -> Metric: ...      # the scalar objective
    def validate(self, run: RunResult) -> ValidityReport: ...  # baseline correctness
```
Built-ins: `SingleFileAdapter` (Karpathy-style), `MLEBenchAdapter`, `KernelSOLAdapter`.

### 3.3 Researcher agent (reasoning model)
**Responsibility:** propose **diverse, novel** improvement ideas conditioned on task + archive + seed knowledge.
```python
class Researcher:
    def propose(self, ctx: ProposalContext) -> list[Idea]: ...
# ctx = {task, current_best, archive_summaries, seed_knowledge, failed_ideas}
```
Rules: must not re-propose a semantically-tried-and-failed idea (novelty filter via archive embeddings); returns N ranked ideas with a rationale + expected-gain estimate.

### 3.4 Developer agent (coder model **or external coding-agent CLI**; [ADR-7](03-decisions.md))
**Responsibility:** turn an idea into a concrete patch; repair runtime errors.
```python
class Developer:
    def implement(self, parent: Solution, idea: Idea) -> Patch: ...   # calls the configured RoleBackend
    def repair(self, sol: Solution, err: ExecError) -> Patch: ...
```
Patches are diffs against the parent node, restricted to the adapter's `edit_surface()`. **The Developer does not hard-code "one LLM call" — it calls a pluggable `RoleBackend` (§3.4b)**, so the same role can be a raw LLM *or* a full external coding agent (OpenHands/Aider/SWE-agent/Claude Code) without changing the rest of the loop.

### 3.4b Role backends (raw-LLM ↔ full-agent-CLI; [ADR-7](03-decisions.md))
**Responsibility:** make "do this coding step" interchangeable across a single LLM call and a complete external coding agent. Seam sits **one level above `LLM.complete()`** (which returns *text*; an agent returns a *workspace mutation*).
```python
class RoleBackend(Protocol):
    def run(self, req: RoleRequest) -> RoleResult: ...
    def capabilities(self) -> BackendCaps: ...   # multi_turn, runs_code, self_evaluates, supports_seed
# RoleRequest  = {role, workspace, task, goal_anchor, edit_surface[], read_context[], budget, backend_cfg, parent_ref}
# RoleResult   = {status, patch(surface-filtered diff), summary, events[], cost, raw_log_ref}
```
Impls (selected per role by `config.roles.<role>.backend`):
- `LLMBackend` — wraps `LLM.complete()` (today's behavior; cheap fallback tier).
- `CliAgentBackend` — subprocess wrapper + per-agent adapter (OpenHands `--headless --json`, Aider `--message --yes-always`, SWE-agent `run`, Claude Code `-p --output-format json`).
- `LibAgentBackend` — in-process (SWE-agent `RunSingle`, OpenHands SDK).

**Two load-bearing rules ([ADR-7](03-decisions.md)):**
1. **Delegate the *step*, own the *loop*.** The agent backs only the inner implement/refine/debug step (one bounded invocation per operator application, `scope: step`); the search, operators, evaluator, leakage checker, variance gate, and budget stay ours. Don't let one agent own a whole node/search (cost 10–100×; bypasses rigor).
2. **Constraints by construction + adjudication, not instruction:** throwaway **git worktree** → `git diff` + reject out-of-surface hunks; run the **agent process inside the sandbox** (§10); **we** make the lineage commit; **fold the agent's event stream** into `events.jsonl` (namespaced, raw in `store/`); pin `{agent_version, model, temp, seed}`; budget via CLI caps **+** external SIGKILL watchdog. *(The committed diff is the reproducible artifact, not the trajectory.)*

### 3.5 Execution Sandbox / Runner
**Responsibility:** run a candidate solution in isolation, enforce budgets, capture everything.
```python
class Sandbox:
    def run(self, solution: Solution, seed: int, budget: Budget) -> RunResult: ...
# RunResult = {metric_raw, logs, stdout, stderr, artifacts, wall_time, resources, exit}
```
Isolation: container or constrained subprocess; CPU/GPU/mem/time caps; **network off by default**; read-only fixed files, writable scratch only.

### 3.6 Evaluator + Leakage Checker + Variance Gate — the trust layer
**Responsibility:** decide whether a run is a *real* improvement. **(Re-prioritized by [ADR-6](03-decisions.md): lead with leakage + consistent evaluation; the co-evolving adversarial evaluator is demoted to an optional mode.)**
```python
class Evaluator:
    def evaluate(self, run: RunResult) -> Verdict: ...
class LeakageChecker:                                            # PRIMARY trust mechanism (ADR-6)
    def check(self, solution: Solution, run: RunResult) -> LeakageReport: ...  # train/test + temporal + target
class VarianceGate:
    def confirm(self, solution: Solution, n_seeds: int) -> GateResult: ...
class CoEvolvingEvaluator(Evaluator):                           # OPTIONAL — only for open-ended/self-metric mode
    def add_exploit_rule(self, rule: ExploitRule) -> None: ...
```
Pipeline per candidate:
1. **Validity** — did it run, train, produce correct-shape output?
2. **Leakage check (primary)** — auto-detect **train/test leakage, temporal leakage, and target leakage** (our genuine differentiation — only MLE-STAR ships even a partial one). Fail or auto-correct on detection.
3. **Objective on a trustworthy validation metric** — compute via the adapter; **consistent-evaluation protocol** (same fixed splits/seeds for the metric across candidates) so scores are comparable — the single biggest lever (AIRA: +9–15 pts).
4. **Variance gate (cheap default, strict only at the frontier)** — use **robust CV** for the validation metric everywhere; reserve **multi-seed confirmation for the top-k promotion frontier** (k≈3) before final selection. See §8.
- **Optional (open-ended mode only):** the co-evolving adversarial evaluator + growing reward-hack `ExploitRule` suite — load-bearing *only* when LoopLab defines its own success metric, not for fixed-harness tasks like MLE-bench. *(See §7.)*

A `Verdict` is `{accepted, metric, ci, validity, leakage, reason}`.

### 3.6b Operators (first-class — the real performance levers; [ADR-6](03-decisions.md))
The `SearchPolicy` invokes a set of **operators**; operator quality dominates the policy choice. Built-ins:
```python
class Operators:
    def draft(self, ctx) -> Patch: ...                          # initial solution (optionally web-seeded, §3.12)
    def debug(self, sol, err, max_depth=10) -> Patch: ...       # depth-bounded iterative repair
    def improve(self, sol, idea) -> Patch: ...                  # incremental refinement
    def ablate(self, sol) -> CodeBlock: ...                     # find highest-impact block (MLE-STAR style)
    def refine_block(self, sol, block) -> Patch: ...            # deeply optimize that block
    def ensemble(self, sols: list[Solution], rounds=5) -> Patch:# LLM-proposed, iteratively-refined fusion (best-evidenced lift)
```
`ablate→refine_block` (ablation-driven targeted refinement) and `ensemble` are the two highest-ROI additions; `debug` is depth-capped. *(MLE-STAR, KompeteAI, AIRA operator set.)*

### 3.7 Search Policy (plugin — fully overridable; [ADR-2](03-decisions.md))
**Responsibility:** decide *where to expand next*, *how to branch*, and *when to merge*. **Selected by `config.search.policy` and injected by the orchestrator — the core loop knows only this protocol, so a new algorithm is a plugin, never a fork.**
```python
class SearchPolicy(Protocol):
    def select(self, dag: ExperimentDAG, archive: Archive) -> Node: ...        # where to expand
    def expand(self, node: Node) -> list[Idea]: ...                            # how to branch (optional)
    def should_merge(self, archive: Archive) -> tuple[Node, ...] | None: ...   # branch-and-combine trigger
    def stop(self, state: RunState) -> bool: ...                               # custom stop condition
```
Built-ins:
- `GreedyTree` (AIDE-style: expand best node, rich operators →children). **Default — validated as a strong, overfit-resistant choice at long budgets (AIRA).**
- `Evolutionary` (diversity archive, fitness+novelty selection, merge) — **ablation-gated experiment**, not core (unproven on MLE-bench).
- `MCTS` (SELA-style: UCT over pipeline nodes) — for AutoML-pipeline-shaped tasks; note AIRA found MCTS overfits at long budgets.

> **Search topology = tree-with-merge DAG ([ADR-5](03-decisions.md)), re-prioritized by [ADR-6](03-decisions.md).** Default is a **greedy best-first tree with strong operators** (§3.6b). The high-value graph feature is the **gated Merge/ensemble operator** (combine top nodes from different lineages → a multi-parent child) — KompeteAI's merge is worth ~9 pts. **The diversity archive and fancier policies (MCTS/evolutionary) are demoted to opt-in experiments**: AIRA shows that with good operators they tie greedy (~1–2 pts) and can overfit. Node scores stay **standalone/evaluator-derived, never back-propagated across a merge** (sidesteps DAG credit-assignment). **Net: spend on operators (§3.6b) and evaluation rigor (§3.6), not on search bookkeeping.**

### 3.8 Archive / Memory / Journal
**Responsibility:** persistent record + the proposer's memory.
- **Archive**: every node's `{patch, metric, verdict, lineage, summary, embedding}`; queryable by similarity; supports `merge(a, b) -> Idea` (the branch-and-combine helper behind `SearchPolicy.should_merge`).
- **Journal**: the append-only event stream of proposals/runs/verdicts/costs — **persisted to `events.jsonl`**, the authority for replayable `RunState` in [04-file-layout.md](04-file-layout.md). Search-state projections fold it; trace, task/config, chat, commands and cross-run views also join their documented sidecars.
- Conditions every `Researcher.propose` call (closes the loop). *(AlphaEvolve archive; R&D-Agent prior-experiment context.)*

### 3.9 LLM Backend Layer
**Responsibility:** one call surface, routed per role, over LiteLLM.
```python
class LLM:
    def complete(self, role: Role, messages, **opts) -> Response: ...
# config: roles.researcher.model = "anthropic/claude-..."  (or "openai/o3", "ollama/qwen2.5", "openai/<vllm-endpoint>")
#         roles.developer.model  = "ollama/qwen2.5-coder"   etc.
```
Per-role model assignment is a first-class config concept. Failover + retry + cost accounting live here.

### 3.10 Knowledge & Memory architecture (unifies seed-knowledge + ingested + archive; [ADR-10](03-decisions.md))
**Responsibility:** one knowledge system with four memory tiers (CoALA), a unified representation + retrieval interface, and **cross-run experience** — replacing the previously disconnected seed-knowledge (§3.10-old), ingested KnowledgeStore (§3.12), and archive (§3.8).
- **Tiers:** **working** (live run = the in-run archive/journal), **episodic** (past runs as a *case library* — new), **semantic** (ML facts/tricks + *distilled lessons*), **procedural** (recipes + executable code = **Agent Skills**, §3.14).
- **One representation, separate indices, one router:** shared note schema `{content, frontmatter(provenance, type, task_fingerprint, confidence, status), embedding, tags, [[links]]}`; a **role/goal-conditioned router** retrieves from the right index. **Curated knowledge is never merged with distractor-rich ingested RAG** (context-rot).
- **Cross-run accumulation, gated:** retain a case **only if it beat the prior best**; tiered promotion `candidate→distilled→trusted`; decaying confidence; contradiction = mark-invalid + append-only ledger; poisoning-aware (filter before injecting). Feeds the Researcher via **progressive disclosure** (manifest → full note → detail).
- **On disk (files-as-truth):** `knowledge/{seed,tasks,experiments,lessons}/*.md` canonical; `knowledge/index/` is a derived, rebuildable vector/graph projection ([04-file-layout.md](04-file-layout.md)).

### 3.14 Capability layer — MCP tools + Agent Skills ([ADR-9](03-decisions.md))
**Responsibility:** give every role *and* every external agent backend the same tools/skills.
- **MCP is the internal capability bus.** One server per trust boundary: `knowledge-mcp` (**agentic retrieval toolset** — `grep`/`glob`/`ls`/`read` + `vector_search`/`hybrid_search`; the LLM chooses lexical-nav vs semantic per query, [ADR-16](05-build-decisions.md)), `archive-mcp` (query past runs/metrics), `ml-tools-mcp` (`profile_data`, `check_leakage`), `sandbox-mcp` (`run_code`), `web-mcp`. Our roles consume them via function-calling (`async_mcp_tool`); external agents (OpenHands/Claude Code, ADR-7) consume the *same* servers by URL → the leakage checker exists in one place, identical results everywhere. Hot, role-private helpers stay in-process.
- **Agent Skills** (`SKILL.md` + progressive disclosure) carry ML *recipes* (K-fold CV, Muon, time-series leakage) — the **procedural** memory tier (§3.10). Skills orchestrate; MCP tools execute. The old seed-knowledge library migrates to this format.
- **Per-role tool allow-lists** are a security boundary; dangerous actions are typed tools (not raw bash); MCP servers are pinned/audited like dependencies.

### 3.15 Prompt & instruction store ([ADR-8](03-decisions.md))
**Responsibility:** versioned, UI-editable prompts; per-run instructions for external agents.
- **Prompts = Markdown + YAML frontmatter files in git**, one per role×operator (`prompts/<role>/<operator>.md`); engine hot-reloads on change; an optional Langfuse-style cache (TTL + fallback-to-file) lets UI edits take effect without redeploy. **One canonical store** (files by default; UI commits via PR) — no two-way sync. structured output defaults to **standard tool calling** (LiteLLM→pydantic), with **BAML (SAP) as the secondary fallback** for weak local models + the Evaluator `Verdict` (per-role `parser` strategy, [ADR-14](05-build-decisions.md)). Optimize Evaluator/Developer prompts later with `dspy.GEPA`.
- **AGENTS.md** is generated per-run into the agent's worktree as the conventions bridge for external backends (ADR-7): conventions in `AGENTS.md`, task in the prompt arg, hard constraints in CLI flags. Bridge to Claude Code via `CLAUDE.md` `@import`.

### 3.11 Observability / UI layer (decoupled; [ADR-1](03-decisions.md))
**Responsibility:** let the user explore traces, the flow, the DAG, per-node/experiment info — and act (pause/resume/fork/edit) — as a **separate layer**.
- **Replay authority is explicit.** The engine and server-owned control path append to one serialized event log; one live engine is fenced separately by `engine.lock`. The UI reads folded state and sidecars, then submits durable command intents instead of mutating the fold — avoiding UI↔engine races without pretending every product store is event-derived.
- **Live updates** expose freshly folded file state over SSE, with bounded polling/recovery paths for buffered
  or dropped streams. File reads remain the backend; SSE is a delivery mechanism, not a competing state store.
- **Renderers, layered on the same files (build order revised by [ADR-6](03-decisions.md)):** **(first)** an **AIDE-style static HTML lineage tree** — cheap, enough to debug the search; **(then, once the agent gets results)** a **Textual TUI** (`Tree`/`DataTable`/log pane/key-bindings) + `textual-serve` for a browser view; **(later)** a React Flow + ELK web UI. Don't build the decoupled UI before the agent works — that's premature infra.
- **Write-back (shipped correction):** the UI calls authenticated server command/control routes. Durable
  control events and command records carry generation/idempotency fences and use the serialized event-store
  path; there is no shipped `commands.jsonl`/`desired_state.json` reducer.
- File contract + event/command schema: see [ADR-1](03-decisions.md) and §4. **Full on-disk file-layer spec (data classes, formats, run/project layout, content-addressed artifact store, atomic-write rules): [04-file-layout.md](04-file-layout.md).**

### 3.12 Ingestion / Knowledge pre-phase (new; [ADR-3](03-decisions.md))
**Responsibility:** given pre-existing artifacts, analyze them against the goal *before* the search loop, and emit a ranked experiment backlog.
```python
class Ingestion:
    def goal_anchor(self, task: Task) -> GoalConfig: ...                 # immutable metric/constraints/criteria
    def ingest(self, artifacts: list[Artifact]) -> KnowledgeStore: ...   # Docling/GROBID/trafilatura → chunks+provenance
    def retrieve_more(self, goal: GoalConfig) -> None: ...               # web (MLE-STAR style) + literature
    def analyze(self, goal: GoalConfig) -> Analysis: ...                 # goal-conditioned summaries + novelty gate
    def plan(self, analysis: Analysis) -> Backlog: ...                   # hypotheses → filter → Elo-rank → backlog
```
- **Store:** hybrid vector + contextual-BM25 (Anthropic Contextual Retrieval) + GraphRAG for global "what's known/missing"; **skip RAG if corpus < ~200k tokens** (load in-context).
- **Output contract → search loop:** `{KnowledgeStore (queryable as an agentic toolset — `grep`/`glob`/`read` + `vector_search`/`hybrid_search`/`web`, agent-chosen per query, [ADR-16](05-build-decisions.md)), immutable GoalConfig, ranked Backlog}`. The Backlog seeds the Researcher; retrieval tools stay available during the loop.
- **In-loop re-grounding:** after results, append a meta-review critique to the planner prompt and re-rank the backlog (no retraining).
- **The `KnowledgeStore` is the *semantic (ephemeral, per-task)* tier of the unified knowledge architecture (§3.10, [ADR-10](03-decisions.md))** — kept on a separate index from curated seed knowledge to avoid context-rot/distractors.

### 3.13 Tracking / Reproducibility layer (new; [ADR-4](03-decisions.md), demoted by [ADR-6](03-decisions.md))
**Responsibility:** durable, queryable, reproducible record of every experiment — distinct from the live event log.
> **[ADR-6](03-decisions.md): the event log is the replay authority for `RunState`; MLflow is an *optional exporter* (thin adapter over the event log), not a core dependency.** No surveyed top system uses MLflow — keep it swappable and off the hot path.
```python
class Tracker:                                  # thin wrapper over MlflowClient (headless, sqlite backend)
    def log_run(self, node: Node, run: RunResult, verdict: Verdict) -> str: ...
    def set_lineage(self, run_id: str, parent_ids: list[str]) -> None: ...   # JSON tag = the DAG
    def reproduce(self, run_id: str) -> Solution: ...                        # commit + params + artifacts
    def branch_from(self, run_id: str, overrides: dict) -> Solution: ...     # reproduce → new child
```
- **MLflow core, headless:** `sqlite:///<root>/mlflow.db` + local artifact root; `MlflowClient`; auto git-commit capture; `autolog` + model signatures + registry. No server, no MLflow UI.
- **Lineage we own:** `parent_ids` JSON tag (1 = branch, N = merge) — the multi-parent DAG MLflow doesn't model natively. Mirrors the `Node.parent_ids` data model and the event-log `parent_ids[]`.
- **Fall-back / branch:** `search_runs` → `get_run` (config) → `download_artifacts` → new run copying params + recorded commit, `parent_ids=[old]`, mutate delta. (The "reproduce, then branch" flow.)

---

## 4. Data model

```python
Task        = {id, adapter, metric, direction, budget, constraints, threshold}
GoalConfig  = {metric, direction, constraints, quality_criteria}   # IMMUTABLE goal anchor (ADR-3)
Artifact    = {id, kind:web|pdf|table|file|repo|prior_run, uri}
Backlog     = [{hypothesis, method, expected_outcome, metric, provenance, elo}]  # ranked (ADR-3)
Solution    = {id, code_ref(git), parent_ids:[id], patch, created_from_op, created_by}
RunResult   = {solution_id, seed, metric_raw, logs, artifacts, resources, exit}
Verdict     = {solution_id, accepted, metric, ci, validity, leakage, reason}   # 'leakage' replaces 'hacks_triggered' (ADR-6)
Node        = {solution, parent_ids:[id], runs:[RunResult], verdict,
               eval_score, approach_tags:[...], novelty_score}   # DAG node; score standalone, NOT back-propagated
ArchiveEntry= {node, summary, embedding, lineage}
Budget      = {tokens_max, compute_max, walltime_max, per_run_timeout}
Config      = {roles:{researcher,developer,judge}, backend, search:{policy,...}, evaluator, budget, tracker,
               prompts:{dir, registry?}, capability:{mcp_servers[], skills_dir, allow_lists}, knowledge:{dirs, index},
               secrets:{gateway_url}, observability, isolation}   # ADR-8/9/10/11; secrets are references, never values
RunState    = {dag, archive, best, budget_spent, status}              # passed to SearchPolicy.stop()
ResearchReport = {best_solution, metric_ci, experiment_log, lineage, cost, hacks_caught}  # Orchestrator.run() output
RoleRequest = {role, workspace, task, goal_anchor, edit_surface[], read_context[], budget, backend_cfg, parent_ref}  # ADR-7
RoleResult  = {status, patch, summary, events[], cost, raw_log_ref}   # ADR-7 — patch is a surface-filtered diff
BackendConfig= {backend:llm|cli_agent|lib_agent, agent, model, temperature, seed, scope:step|node, limits}  # ADR-7
```
*(Helper types referenced in interfaces — `Idea`, `Patch`, `Metric`, `ValidityReport`, `ExploitRule`, `GateResult`, `KnowledgeStore`, `Analysis` — are defined inline at their components; the list above is the core persisted/passed state.)*
**Graph note ([ADR-5](03-decisions.md)):** `parent_ids` is a **list** — 1 parent = a tree edge, >1 = a merge node. That single field *is* the entire graph capability (an adjacency list in a table; no graph-search engine). The same `parent_ids` appears in the MLflow lineage tag (§3.13) and the event log (§3.11) — one lineage concept, three stores.

Invariants: a `Solution` is always reconstructable from its git ref; a `Node`'s metric is always a `Verdict` (gated), never a raw single run; a node's `eval_score` is **standalone** (evaluator-derived, never backed up through ancestors).

---

## 5. Control flow — the main loop

```
1.  task = load_task(spec); ws = adapter.prepare()
2.  root = Solution(baseline code); evaluate(root); archive.add(root)
3.  while not stop(budget, dag, target):
4.      node   = policy.select(dag, archive)                  # explore/exploit
5.      ctx    = build_context(node, archive, seed_knowledge) # memory feeds proposer
6.      ideas  = researcher.propose(ctx)                      # reasoning model
7.      idea   = pick(ideas)                                  # diversity/novelty filter
8.      patch  = developer.implement(node.solution, idea)     # coder model
9.      sol    = apply(patch)                                 # new child (git commit)
10.     run    = sandbox.run(sol, seed0, budget.per_run)
11.     if run.failed: patch = developer.repair(sol, run.err); goto 9 (bounded retries)
12.     verdict = evaluator.evaluate(run)                     # validity + objective + anti-hack
13.     if verdict.improves(best):
14.         gate = variance_gate.confirm(sol, n_seeds)        # multi-seed significance
15.         if gate.passed: promote(sol); archive.add(sol); git_commit(sol)
16.         else: record_as_noise(sol)
17.     else: archive.add(sol)                                # remember failures too
18.     if policy.should_merge(archive): merge_nodes()        # branch-and-combine (gated merge, ADR-5)
19.  return report.build(dag, archive, best)
```
Parallelism: steps 4–17 run as **multiple concurrent threads** sharing one archive (lineages compete; budget allocator favors promising ones).

---

## 6. Search strategy

- **Default: tree search with evolutionary upgrade.** Start AIDE-style (greedy expansion of the best node; each patch is a child). Layer on AlphaEvolve-style population behavior: keep an **archive of diverse high-performers**, and periodically **combine** two complementary nodes into a new idea.
- **Selection** balances explore/exploit: exploit the current best lineage, but reserve budget for diverse/under-explored branches (quality-diversity, MAP-Elites-flavored). Prevents the classic "stuck on a local optimum" failure of greedy loops.
- **Merge** (`should_merge` → `merge_nodes`, the "branch-and-combine" operator) = ask the Researcher to combine the *distinct* improvements of 2–3 archived nodes from different lineages into one idea, then implement+evaluate as a new multi-parent node (ADR-5 gating: only across disjoint pipeline parts).
- Pluggable: `GreedyTree` (P1) → `Evolutionary` (P3) → optional `MCTS` (SELA-style) for AutoML-pipeline-shaped tasks.

---

## 7. Trust mechanisms — leakage-first; co-evolving evaluator optional

> **Re-prioritized by [ADR-6](03-decisions.md).** For **fixed-metric tasks (MLE-bench-style)** the harness owns the metric, so the high-ROI trust work is **leakage detection + consistent evaluation** (§3.6), *not* a hardened adversarial evaluator. The co-evolving evaluator below is **load-bearing only in open-ended / self-metric mode** (where the agent defines its own success), e.g. reward hacking like editing timeouts or reading the grader — documented in self-evaluating agents (Sakana AI-Scientist, METR/o3). Keep it on the shelf for that mode; do not run it on fixed-harness tasks.

**Primary (always on): leakage + consistent evaluation.** Auto-detect train/test, **temporal**, and **target** leakage; hold splits/seeds fixed across candidates so validation scores are comparable. This is the +9–15 pt lever.

**Optional (open-ended mode) — co-evolving adversarial evaluator. Design:**
- A **static base** of validity + sanity checks (compiles, trains, output shape, no test-set access, no metric tampering).
- A **growing regression suite**: every time a human or a meta-check discovers the search exploited the benchmark, that exploit becomes a permanent `ExploitRule`. The suite only ever grows → the searcher can't re-use an old hack.
- **Optional LLM-judge check** (adversarial): a separate model is asked "does this diff achieve the goal *legitimately*, or does it exploit the harness?" — used as a soft signal, never the sole gate.
- **Hardening trigger:** when acceptance rate spikes or a result looks too good (large outlier vs trend), flag for an exploit audit before promotion.

**Rule:** a result is promoted only if `validity ∧ ¬any(exploit_rules) ∧ variance_gate.passed`.

---

## 8. Variance gating (cheap-by-default; strict only at the frontier — [ADR-6](03-decisions.md))

A single good run is *a hypothesis, not a result* — but **p<0.01 on every promotion is too expensive** (can need ~36 runs to detect ~1%) and would starve the search. Tiered approach:
- **Everywhere (cheap):** use **robust CV / repeated-holdout** for the *validation metric itself* — directly attacks the winner's-curse without N full re-runs. This is the default selection signal.
- **Promotion frontier only (strict):** re-run the **top-k candidates (k≈3) on 3–5 seeds** before final submission selection (AIRA's top-k hedging recovered ~10%).
- **Practical significance, not p<0.01:** promote a frontier candidate only if mean improvement **> 1 SE** (or > observed seed std). Store the CI; the final report always shows metric ± CI.
*(AIRA top-k hedging + validation-overfit findings; multi-seed reporting from MLE-STAR/AI-Scientist-v2.)*

---

## 9. LLM backend abstraction

- **One layer over LiteLLM** → 100+ providers incl. local (vLLM/Ollama) and all major APIs.
- **Per-role routing AND per-role backend** is config, not code ([ADR-7](03-decisions.md)):
  ```yaml
  roles:
    researcher: { backend: llm,       model: "openai/o3",            effort: high }
    developer:  { backend: cli_agent, agent: openhands, model: "anthropic/claude-opus-4-8",
                  temperature: 0, seed: 1234, scope: step, limits: {max_turns: 30, walltime_s: 1800, usd: 4.0} }
    # or a plain local-LLM developer:
    # developer:{ backend: llm, model: "ollama/qwen2.5-coder", base_url: "http://localhost:11434/v1" }
    judge:      { backend: llm,       model: "anthropic/claude-..." }
  ```
- A role is either a raw LLM call (`backend: llm`) or a full external coding agent (`backend: cli_agent|lib_agent`) — the rest of the loop is unchanged (§3.4b).
- Switch all-API ↔ all-local ↔ hybrid by editing config only. Backend layer handles retries, failover, and **token-cost accounting** (feeds the budget manager).
- **Lesson from R&D-Agent Issue #1016:** local-model config is the friction point — so we validate the backend config at startup with a smoke-test call per role and fail fast with a clear message.

---

## 10. Execution sandbox & rules

- Each run in a fresh isolated environment. **Prod (Linux/NVIDIA): Docker + docker-py + NVIDIA toolkit, with gVisor escalation and Sysbox for agent-runs-Docker. Windows-11 dev: Docker Desktop + WSL2 is the *primary* path (same docker-py code, GPU-PV, `--network none`); a native Job-Object subprocess is a trusted-dev convenience only — NOT a security boundary, so untrusted/agent code always routes through WSL2/Docker.** *([ADR-13](05-build-decisions.md).)*
- **Resource caps:** GPU/CPU/mem/time per run, enforced (kill on breach).
- **Network off by default** for agent-written code (prevents data exfiltration *and* cheating by downloading answers).
- **File policy:** fixed harness + data are read-only; agent writes only to the declared `edit_surface()` + a scratch dir.
- **External coding agents run *inside* the sandbox ([ADR-7](03-decisions.md)).** When the Developer backend is a full agent CLI, the **agent process itself** is hosted in the sandbox (it executes code mid-run): network-off (also anti-cheat), capped, in a throwaway git worktree; out-of-surface edits are diff-rejected afterward. Nest our sandbox around the agent's own Docker runtime.
- Every run is reproducible: pinned deps (lockfile), recorded seed, git ref of the solution; for agent backends also the pinned `{agent_version, model, temp, seed}` (the committed diff is the reproducible artifact).

---

## 11. Rules & invariants (hard constraints)

1. **No promotion without a gated `Verdict`** — raw runs never become "the best."
2. **The exploit regression suite only grows** — never silently removed.
3. **The agent may only edit the declared edit-surface** — enforced, not trusted.
4. **Every accepted node is a git commit** — lineage is always reconstructable.
5. **All agent code is sandboxed, network-off, resource-capped** — no exceptions for "trusted" ideas.
6. **Budget is hard** — when tokens/compute hit the cap, the loop stops; no overruns.
7. **Failures are archived, not discarded** — the proposer learns from them (novelty filter).
8. **Metrics are reported with variance** — never a bare point estimate.
9. **Config changes can't bypass safety** — sandbox/budget/evaluator are not user-disableable in normal mode.
10. **Atomic writes only** — write to `.tmp/` then `rename()`; the UI must never see a torn file (Maildir guarantee). *([04-file-layout.md](04-file-layout.md))*
11. **Name every authority and derive caches** — `events.jsonl` owns replayable run state; snapshots and original sidecars own the data that the fold does not contain. UI caches (SQLite/Parquet/index) are rebuildable from their declared inputs and gitignored; do not make a derived cache a competing authority. *([04-file-layout.md](04-file-layout.md))*
12. **Version every file** — embed `apiVersion`/`kind`/`v` + a JSON Schema per kind; UI upcasts on read.
13. **Big bytes by reference** — weights/data live in a content-addressed `store/` and are referenced by hash+path; never inlined into docs/manifests/git.

---

## 12. Concurrency, persistence, reproducibility

- **Concurrency:** N research threads (config), each a select→propose→implement→run→evaluate chain, sharing one archive + one budget manager. Cap by available GPUs/CPU.
- **Persistence:** canonical inputs are **human-readable files** (`events.jsonl` for replayable run state plus task/config and original sidecars) + a git repo (solutions/lineage); MLflow is an optional export. SQLite/Parquet under `_derived/` is a **rebuildable projection**, never authority. Resume additionally honors the task/config snapshots and the documented command/finalization recovery records; it is not a claim that every external side effect can be recreated from the event log. *([04-file-layout.md](04-file-layout.md))*
- **Reproducibility:** any reported result ships with `{git_ref, seeds, deps_lock, mlflow_run, exact_command}` so a third party can rerun it (this is also our #2 success metric).

---

## 13. Extension points (how to extend without forking core)

| Want to change | Implement | No core edit because |
|----------------|-----------|----------------------|
| New task/benchmark | `TaskAdapter` | Orchestrator depends on the protocol, not the impl |
| **New research algorithm** | **`SearchPolicy`** (`config.search.policy`) | **`select`/`expand`/`should_merge`/`stop` are injected ([ADR-2](03-decisions.md))** |
| New anti-hack check | `ExploitRule` → `add_exploit_rule` | Evaluator iterates a registry |
| New model/provider | config (`roles.*.model`) | LiteLLM layer is generic |
| **Raw-LLM ↔ external coding-agent for a role** | config (`roles.*.backend`/`agent`) + a `RoleBackend` adapter | role calls `RoleBackend`, not `LLM` directly ([ADR-7](03-decisions.md)) |
| New variance test | `VarianceGate` strategy | gate is a strategy object |
| New artifact type / parser | `Ingestion` parser plugin | Ingestion dispatches by `Artifact.kind` ([ADR-3](03-decisions.md)) |
| New UI / renderer | read the file contract (§3.11) | files are the contract; engine is unaware of renderers ([ADR-1](03-decisions.md)) |
| Different tracker | `Tracker` impl | MLflow is wrapped behind the protocol ([ADR-4](03-decisions.md)) |
| New tool / capability | add an **MCP server** (or tool) | roles + external agents consume MCP uniformly ([ADR-9](03-decisions.md)) |
| New ML recipe | add an **Agent Skill** (`SKILL.md`) | progressive-disclosure load; no loader to write ([ADR-9](03-decisions.md)) |
| Edit a prompt | edit `prompts/<role>/<op>.md` (or UI) | prompts are hot-reloaded config, not code ([ADR-8](03-decisions.md)) |
| New knowledge / lesson | add a note under `knowledge/` | unified schema + router; index rebuilds ([ADR-10](03-decisions.md)) |
| **Different vector DB** | **`VectorStore` impl** (`config.knowledge.index.backend`) | router depends on the protocol; LanceDB default, Qdrant/FAISS/Chroma are plugins ([ADR-16](05-build-decisions.md)) |

Everything is wired by dependency injection from `Config`; the core loop knows only the protocols.

---

## 14. Recommended tech stack

- **Language:** Python 3.11+.
- **LLM backend:** **LiteLLM** (matches R&D-Agent; gives local + API + cost hooks).
- **Role backends ([ADR-7](03-decisions.md)):** raw LLM (LiteLLM) **or** external coding-agent CLI — **OpenHands** (MIT, best fit; `--headless --json` / SDK), **Aider** (Apache-2.0, simplest; git-diff artifact), **SWE-agent/mini-swe-agent** (MIT; clean `.patch`), optionally **Claude Code** (proprietary, API-shaped only). Wrapped via worktree + diff-reject + sandboxed process + event-fold.
- **Sandbox ([ADR-13](05-build-decisions.md)):** Docker + **docker-py** + NVIDIA Container Toolkit (prod); **gVisor/runsc** escalation for untrusted single-script runs; **Sysbox** when a full agent backend spawns its own Docker. GPU caps via `CUDA_VISIBLE_DEVICES`+CDI (MIG for untrusted sub-GPU packing); resource/timeout via cgroups + a **psutil** watchdog; network-off via `--network none`. **Windows dev = Docker Desktop + WSL2** (primary); native **Job-Object** subprocess (`pywin32`) for trusted dev only.
- **Storage ([04-file-layout.md](04-file-layout.md)):** human-readable canonical files (Markdown+frontmatter docs, YAML config, JSON summaries, CSV/JSONL metrics, JSONL event/command/log streams) + a content-addressed `store/` for big binaries (safetensors weights, datasets) referenced by sha256 + git repo for solution lineage + `_derived/` rebuildable UI projections (SQLite/Parquet/index, gitignored) + a vector index (**LanceDB** — embedded, Windows-friendly, vectors + native FTS) for novelty/similarity. Atomic writes (temp→rename) throughout. **Concrete library choices for every component are pinned in [05-build-decisions.md](05-build-decisions.md).**
- **Tracking/reproducibility:** **MLflow as an optional headless exporter** over the event log, `sqlite:///mlflow.db` backend (`MlflowClient`, autolog, git capture, registry) — no MLflow server/UI; swappable, off the hot path ([ADR-4](03-decisions.md)/[ADR-6](03-decisions.md)).
- **Ingestion:** **Docling** (general), **GROBID** (scholarly PDFs), **trafilatura** (web), **Table Transformer** (scanned tables), **repomix** (repos); hybrid vector+BM25 with contextual retrieval + GraphRAG ([ADR-3](03-decisions.md)).
- **Capability layer ([ADR-9](03-decisions.md)):** **MCP** servers (`archive`/`ml-tools`/`sandbox`/`web`) as the tool bus; **Anthropic Agent Skills** (`SKILL.md`) for ML recipes; per-role tool allow-lists.
- **Prompts ([ADR-8](03-decisions.md)):** Markdown + YAML frontmatter in git (`prompts/<role>/<op>.md`), hot-reloaded; optional **Langfuse** (MIT) for UI editing/versioning; **BAML** for structured-output prompts; `dspy.GEPA` for later optimization; **AGENTS.md** generated per-run for agent backends.
- **Knowledge/memory ([ADR-10](03-decisions.md)):** Markdown+frontmatter notes (canonical) + a derived vector index behind a **pluggable `VectorStore`** (**LanceDB** default — vectors + FTS hybrid in one embedded store; Qdrant/FAISS/Chroma are config-swappable plugins) + optional **[[wikilinks]]→networkx** graph (GraphRAG deferred, [ADR-16](05-build-decisions.md)); role/goal-conditioned router; cross-run case library + lessons.
- **Secrets/config/observability ([ADR-11](03-decisions.md)):** **LiteLLM gateway** (per-role tokens, budgets) + secret manager; **`pydantic-settings`** typed config (layered: YAML < `.env` < env < CLI; `SecretStr`) with a secret-masked resolved snapshot; **OpenTelemetry GenAI** event conventions.
- **Orchestration/concurrency ([ADR-12](05-build-decisions.md)):** **`anyio`** (asyncio backend) + `CapacityLimiter` for bounded fan-out; **hand-rolled crash-resume by event-replay** (no Temporal/Prefect — would conflict with files-as-truth); **`git` CLI via subprocess** for commits/worktrees (not GitPython/pygit2); per-run/`thread` isolation via git worktrees + sandbox containers.
- **Structured LLM outputs ([ADR-14](05-build-decisions.md)):** **standard tool calling** by default (via LiteLLM→pydantic — the *same channel* as MCP/agent tools), with **BAML (SAP)** as the secondary fallback for weak local models + the Evaluator `Verdict`, and **outlines** as the self-hosted-vLLM guarantee tier — a per-role `parser` strategy with auto-fallback; patches as **unified-diff** applied via `git apply` with a **unidiff** allow-list gate.
- **Trust layer ([ADR-15](05-build-decisions.md)):** **scikit-learn** splitters + custom consistent-eval harness + custom purged/embargoed walk-forward; **cleanlab** (label/dup/outlier) + **custom** temporal/target/contamination leakage; **scipy.stats.bootstrap** + numpy for the >1-SE gate; custom JSON data profiler.
- **Plumbing ([ADR-17](05-build-decisions.md)):** **orjson** JSONL + rebuildable **SQLite** read-model; hand-rolled **`os.replace`+fsync** atomic writes; **watchfiles**; **jsonschema** (+ pydantic-emitted schemas, upcast-on-read); **structlog**; **opentelemetry-sdk** + custom JSONL SpanExporter; **Typer** CLI; **Jinja2** reports; **psutil** budget-watchdog/tree-kill.
- **UI:** files-first ([ADR-1](03-decisions.md)) — **Textual** TUI now (`Tree`/`DataTable`), `textual-serve` for browser, **React Flow + ELK** web UI later; `watchfiles` for live tailing. (Not Streamlit-in-process — that couples UI to the engine.)
- **Config:** **`pydantic-settings`** typed `BaseSettings` (YAML + `.env` + env + CLI, layered) + startup smoke-test of each role's backend.

---

## 15. Failure modes & mitigations

| Failure mode | Mitigation |
|--------------|-----------|
| Reward hacking | Co-evolving evaluator + growing exploit regression suite (§7) |
| Lucky single run | Multi-seed variance gate (§8) |
| Local optimum / mode collapse | Quality-diversity selection + branch-and-combine (§6) |
| Forgetting / re-trying failures | Archive-conditioned proposing + novelty filter (§3.8) |
| Runaway cost | Hard token+compute budget, per-thread allocation (§3.1) |
| Unsafe / cheating code | Sandbox: network-off, edit-surface lock, resource caps (§10) |
| Local-model config friction | Startup per-role smoke test, fail-fast clear errors (§9, R&D-Agent #1016 lesson) |
| Irreproducible "win" | git ref + seed + deps lock + MLflow run attached to every result (§12, §3.13) |
| Broken DAG credit assignment | Scores standalone/evaluator-derived; never back-prop across merges; merge only across disjoint pipeline parts (§3.7, [ADR-5](03-decisions.md)) |
| Planner drifts off-task on re-grounding | Immutable goal anchor held fixed for the whole run (§3.12, [ADR-3](03-decisions.md)) |
| UI↔engine write race | Single-writer: engine is sole state writer; UI only appends command intents (§3.11, [ADR-1](03-decisions.md)) |
| Vague/unrunnable experiment specs | Planner must emit concrete runnable specs (guards the AI-Scientist 8%-edit / 5-of-12-fail failure mode) (§3.12) |
| **Validation overfitting (perceived↑, true↓)** | Consistent-evaluation protocol + robust CV + select on validation not test; top-k frontier hedging (§3.6, §8; worth +9–15 pts per AIRA) |
| **Data leakage (train/test, temporal, target)** | First-class LeakageChecker; fail/auto-correct (§3.6 — only MLE-STAR ships even a partial one) |
| **Weak operators capping performance** | Invest in rich operators (ablation-refine, ensemble, depth-bounded debug) before search policy (§3.6b, §0; AIRA) |
| **Premature infra (UI/MLflow before results)** | Static HTML tree first; TUI/web + MLflow exporter deferred to P3 (§3.11, §3.13, §17) |
| **External agent escapes constraints / overspends** | Throwaway worktree + diff-reject out-of-surface; sandbox the agent process (network-off); CLI caps + external SIGKILL budget watchdog; pin agent version (§3.4b, §10, [ADR-7](03-decisions.md)) |
| **Delegating the loop to an opaque agent (loses moat/cost blowup)** | `scope: step` — agent backs only the inner edit; our loop/operators/evaluator stay ours ([ADR-7](03-decisions.md)) |

---

## 16. Provenance — which idea came from where

| Mechanism | Source |
|-----------|--------|
| Tree search over code (node = solution, patch = child) | **AIDE** |
| Dual roles + per-role model routing (reasoning vs coder) | **R&D-Agent** |
| LiteLLM backend (local + API) | **R&D-Agent** (+ AIDE OpenAI-compat) |
| Single mutable edit-surface + git-commit-per-experiment | **Karpathy `autoresearch`** |
| Co-evolving evaluator + anti-reward-hack | **Recursive** |
| Multi-seed variance gating (tiered; idea from p<0.01 gates) | **Recursive** / NanoGPT speedrun; tiered per [ADR-6](03-decisions.md) |
| Archive + branch-and-combine + quality-diversity | **AlphaEvolve** / Recursive |
| MCTS option for AutoML-pipeline tasks | **SELA** |
| Adversarial LLM-judge verification | **deep-research** harness pattern |
| Tree-with-merge DAG + diversity archive + standalone scores | **AlphaEvolve/ShinkaEvolve/KompeteAI** + [ADR-5](03-decisions.md) (evidence-driven) |
| Files-as-truth event log + decoupled UI | event sourcing / local-first / DVC / OpenHands event stream + [ADR-1](03-decisions.md) |
| Ingest-analyze-plan pre-phase + web grounding | **MLE-STAR / co-scientist / PaperQA2** + [ADR-3](03-decisions.md) |
| MLflow optional exporter + own DAG tag | **MLflow** + [ADR-4](03-decisions.md)/[ADR-6](03-decisions.md) |
| **Operators > search policy** (greedy default; rich operators) | **AIRA** (Meta FAIR) + [ADR-6](03-decisions.md) |
| **Ablation-driven targeted refinement + leakage checker + ensembling** | **MLE-STAR** + [ADR-6](03-decisions.md) |
| **Merge operator (multi-parent) high value** | **KompeteAI** + [ADR-6](03-decisions.md) |
| **Consistent evaluation + throughput test-time scaling** | **AIRA / AIRA_2** + [ADR-6](03-decisions.md) |
| **Pluggable role backend: raw-LLM ↔ external coding-agent CLI** | **OpenHands / Aider / SWE-agent / Claude Code** + [ADR-7](03-decisions.md) |
| **Prompts-as-files + UI-edit + AGENTS.md bridge** | Langfuse / Dotprompt / **AGENTS.md** (Linux Fdn) + [ADR-8](03-decisions.md) |
| **MCP capability bus + Agent Skills for recipes** | **MCP** (Linux Fdn) + **Anthropic Agent Skills** + [ADR-9](03-decisions.md) |
| **CoALA memory tiers + cross-run case library + distilled lessons** | **DS-Agent / ML-Master / AIBuildAI-2 / mem0 / A-MEM** + [ADR-10](03-decisions.md) |
| **Hardening: OTel-GenAI events, HITL-as-events, gateway budgets, run isolation** | OpenTelemetry / LangGraph / LiteLLM + [ADR-11](03-decisions.md) |

---

## 17. Phased build (maps to product roadmap; re-ordered by [ADR-6](03-decisions.md) — operators/eval-rigor/ensembling first, infra later)

- **P0 — Working loop:** Orchestrator + `SingleFileAdapter` + per-role Researcher/Developer + Sandbox + `GreedyTree` + LiteLLM + git commits + **`events.jsonl`** + a **static HTML lineage tree**. *(Karpathy/AIDE-class, backend-flexible, observable — minimal infra.)*
- **P1 — The levers that move results:** **rich operators** (`draft`/`debug`(depth-bounded)/`improve`/`ablate`→`refine_block`) + **leakage checker** (train/test+temporal+target) + **consistent-evaluation protocol** + robust-CV selection + **`RoleBackend` seam with a `CliAgentBackend(openhands|aider)` Developer** (worktree + diff-reject + sandbox + event-fold). *(AIRA + MLE-STAR + [ADR-7](03-decisions.md); this is where medal-rate is won.)*
- **P2 — Ensembling + frontier rigor:** first-class **`ensemble`/merge operator** (iteratively refined) + **top-k multi-seed confirmation** at the promotion frontier + DAG data model (`parent_ids`). *(MLE-STAR/KompeteAI; best-evidenced lift.)*
- **P3 — Scale + reproducibility:** **parallel/throughput test-time scaling** + cheap proxy evals (subset/reduced-epoch) + **MLflow optional exporter** + Textual TUI. *(AIRA_2 throughput; reproducibility.)*
- **P3.5 — Grounding:** **lightweight** retrieve-and-seed (≈4 candidates) + data profiling; optional heavier ingestion (Docling/web) behind a flag. *(MLE-STAR-style, kept light per ADR-6.)*
- **P3.6 — Knowledge & capability layer:** **MCP capability bus** (`archive`/`ml-tools`/`sandbox`) + **Agent Skills** (migrate seed-knowledge) + **prompt store** (`prompts/*.md`, hot-reload) + **AGENTS.md** generation for agent backends. *([ADR-8](03-decisions.md), [ADR-9](03-decisions.md).)*
- **P3.7 — Cross-run memory:** episodic **case library** + post-run **distilled lessons** with retain-on-improvement + confidence/promotion gates. *([ADR-10](03-decisions.md) — a top-system differentiator.)*
- **P4 — Breadth + opt-in research machinery:** `MLEBenchAdapter` + `KernelSOLAdapter` + **React Flow web UI** + the hardening pass ([ADR-11](03-decisions.md): gateway secrets/budgets, OTel events, HITL gates, run isolation); **opt-in experiments**: `Evolutionary`/`MCTS` policy, diversity archive, co-evolving evaluator (open-ended mode). *(Production breadth; the demoted bets become measurable experiments.)*

---

## 18. Cross-cutting hardening ([ADR-11](03-decisions.md))

One decision per concern (detail + sources in [ADR-11](03-decisions.md)):
- **Secrets:** front providers with a **LiteLLM gateway**; roles/sandbox get short-lived tokens, never provider keys; redaction + `gitleaks` over the event log.
- **Config:** **`pydantic-settings`** typed `BaseSettings`, layered sources (YAML < `.env` < env < CLI), `SecretStr` for secrets; secret-masked **resolved-config snapshot** per run = the reproduction key.
- **Observability:** events follow **OpenTelemetry GenAI** conventions (model/tokens/latency/cost/finish-reason); domain events as child spans; cost/token/latency first-class.
- **Human-in-the-loop:** **approvals are command events** (reuse [ADR-1](03-decisions.md)) — `pending_approval` → human appends decision → resume. Default gates: plan, egress, budget-threshold, winner-promotion.
- **Error/resume:** classify (transient/deterministic/fatal); **idempotent replay** via content-addressed step ids; failed thread → dead branch, not global abort.
- **Determinism:** target **reproduce-with-variance**; full repro manifest; report mean ± std over N seeds (§8).
- **Engine self-testing:** mocked-LLM smoke test in CI + **golden-task** regression suite + nightly **canary** + adversarial fixtures for the evaluator/gate.
- **Agent-code security:** deny-by-default egress; cgroup/ulimit caps; install only from pinned lockfile; treat ingested/web content as **untrusted data, not instructions** (injection defense).
- **Cost governance:** **gateway-enforced hierarchical budgets**; accountant gates at 80%, kills at 100%; cost attributed per run/thread/role/operator.
- **Isolation:** one `run_id` namespace = run dir + per-thread **git worktree** + per-thread sandbox + scoped token + one MLflow run; `run_id`+`thread_id` required on every event/path.
- **Also:** data/artifact **retention/GC** policy; **provenance-for-publishable-results** (config snapshot + seeds + data hashes + model snapshot + event-log id travels with every "winner").
