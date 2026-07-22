# LoopLab — Concrete Build Decisions (frameworks & libraries)

**Version:** 0.1 · **Date:** 2026-06-21
**Companion docs:** [00-INDEX.md](00-INDEX.md) · [01-product-design.md](01-product-design.md) · [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) · [04-file-layout.md](04-file-layout.md) · research basis: [autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)

> **Historical build-decision record; not a shipped-dependency inventory (2026-07-16).** The tables below
> preserve the implementation choices made at design time; some entries were simplified, deferred, or replaced.
> Current source, tests, [the user guide](guide/index.md), and docs 16–18/21 govern present behavior. In
> particular, `events.jsonl` is the replay authority for `RunState`; original sidecars/snapshots remain
> authoritative for data absent from the fold, and authenticated UI-server controls use serialized event
> appends rather than a separate `commands.jsonl` reducer.
>
> Docs 01–04 fix the **what/why** (principles, components, interfaces, ADR-1…11). This doc fixes the **with-what**: for every component that was still "our code" or carried an *unresolved* choice, it names the concrete library/framework (or scopes the custom code), with the rejected alternatives and the Windows-11 caveat. It adds **ADR-12…ADR-17** (one per subsystem cluster) and ends with the **step-3 validation**: requirements coverage, conflict resolution, and a buildability matrix. Where this doc resolves an earlier open choice or tension, **05 wins** and the change is folded back into 01/02/04 (see §C).

---

## A. Concreteness scorecard — every component, before → now

Legend: **L** = library/framework chosen · **C** = our code (algorithm scoped here) · **L+C** = library wrapped by our thin code.

| Component (arch §) | Before this doc | Now concrete as | Kind | ADR |
|---|---|---|---|---|
| **Engine core / runtime** | (implicit) | **Python package + Typer CLI *process*; hand-rolled engine on anyio + event log; NO agent framework** | C | 18 |
| LLM backend | LiteLLM (named) | LiteLLM + **LiteLLM proxy** as the OpenAI-compatible routing point | L | — |
| Structured outputs (Idea/Patch/Verdict) | "BAML only for Evaluator" | **native tool calling** default (LiteLLM→pydantic) · **BAML (SAP)** secondary fallback + Evaluator · **outlines** on vLLM — per-role `parser` strategy | L | 14 |
| Orchestrator / control loop | "asyncio + bounded pool" | **anyio** (asyncio backend) + `CapacityLimiter` | L | 12 |
| Crash-resume / durability | "replay events.jsonl" | **hand-rolled** event-replay (no Temporal/Prefect) | C | 12 |
| Git / worktrees | git commit per node (no lib) | **git CLI via subprocess** (dulwich fallback) | L+C | 12 |
| Sandbox (prod) | "Docker/Podman" | **Docker + docker-py + NVIDIA toolkit**; **gVisor** escalation; **Sysbox** for agent-runs-Docker | L | 13 |
| Sandbox (Windows dev) | "subprocess fallback" | **Docker Desktop + WSL2** (primary); **Job-Object subprocess** (dev only, *not* a security boundary) | L+C | 13 |
| GPU capping | (unspecified) | `CUDA_VISIBLE_DEVICES` + CDI; **MIG** for untrusted sub-GPU packing | L+C | 13 |
| Resource/timeout enforce | "caps, kill on breach" | cgroups/Job-Object (hard) + **psutil** watchdog + process-group/Job kill | L+C | 13 |
| Network-off | "network off by default" | Docker `--network none` (the boundary); **not** Windows Firewall for WSL2 | L | 13 |
| Patch representation | "diff against parent" | **unified-diff text** → worktree → **unidiff** allow-list gate (reject) → `git apply --include` | L+C | 14 |
| Leakage checker | "our code" (differentiator) | **custom** core (temporal/target/contamination) + **cleanlab** for label/dup/outlier | L+C | 15 |
| Cross-validation | "robust CV" | **scikit-learn** splitters + **custom** consistent-eval harness + **custom** purged/embargoed walk-forward | L+C | 15 |
| Variance gate stats | "p<0.01 rejected; >1 SE" | **scipy.stats.bootstrap (BCa) + numpy**; >1-SE rule; multi-seed top-k | L+C | 15 |
| Data profiling | "doubles as leakage front-end" | **custom** JSON profiler (pandas/numpy/scipy) | C | 15 |
| Vector index | **"FAISS/sqlite-vec" (unresolved)** | **pluggable `VectorStore`; LanceDB default** (Qdrant/FAISS/Chroma plugins) | L+C | 16 |
| Lexical/BM25 | "contextual-BM25" | **LanceDB native FTS** (bm25s fallback) | L | 16 |
| Reranker | "+ reranking" | **FlashRank** (local ONNX), optional/default-off | L | 16 |
| GraphRAG | "GraphRAG entity graph" | **DEFER/CUT** → lightweight **[[wikilinks]] → networkx** | C | 16 |
| Retrieval interface | RAG pipeline (implied) | **agentic toolset** (grep/glob/read + vector/hybrid/web) over MCP, **agent-chosen** | L+C | 16 |
| RAG orchestration | (implied framework) | **thin custom** over LiteLLM + LanceDB (one tool in the toolset; gated by 200k-token check) | C | 16 |
| MCP servers/clients | "MCP bus" | **FastMCP v2** servers + thin **MCP→tool-schema adapter** (`async_mcp_tool`) | L+C | 16 |
| Agent Skills loader | "SKILL.md" | **skills-ref** + small progressive-disclosure loader | L+C | 16 |
| Embeddings | "via LiteLLM" | LiteLLM embeddings, default **`ollama/nomic-embed-text`** | L | 16 |
| Atomic writes | "temp→rename" | **hand-rolled `os.replace`+fsync** helper | C | 17 |
| Event store + read-model | "JSONL + optional SQLite" | hand-rolled JSONL (**orjson**) + **SQLite** rebuildable read-model | L+C | 17 |
| File watching | "watchfiles" | **watchfiles** + forced polling on network mounts | L | 17 |
| JSON Schema | "schema per kind" | **pydantic v2** `model_json_schema()` + **jsonschema** validator; upcast-on-read | L | 17 |
| Structured logging | "structlog" | **structlog** (diagnostics) — hard split from `events.jsonl` (domain truth) | L | 17 |
| Observability | "OTel GenAI" | **opentelemetry-sdk** + **custom JSONL SpanExporter** (files-as-truth) | L+C | 17 |
| CLI | (unspecified) | **Typer** | L | 17 |
| Report templating | "report.md generation" | **Jinja2** | L | 17 |
| Cost/budget watchdog | "gateway budgets" | in-proc accountant over LiteLLM cost + **psutil** two-phase tree-kill (OS-branched) | L+C | 17 |
| Config | pydantic-settings (ADR-11) | unchanged | L | — |
| Tracking | MLflow optional exporter (ADR-4/6) | unchanged | L | — |
| Ingestion parsers | Docling/GROBID/trafilatura (ADR-3) | unchanged | L | — |
| UI renderers | static HTML → Textual → React Flow (ADR-1/6) | unchanged | L | — |

**Result: no component remains "TBD."** Every box is a named library or our-code with the algorithm scoped below.

---

## ADR-12 — Orchestration, concurrency, durability & git

**Concurrency model — `anyio` on the asyncio backend.** Structured concurrency (clean cancellation when a thread blows its budget) with `CapacityLimiter(N)` for bounded fan-out across research threads; richer cancel/timeout than raw `asyncio.TaskGroup` while staying on the asyncio ecosystem (httpx/LLM SDKs work). Blocking work stays **off the loop**: git/IO via `anyio.to_thread.run_sync`; each sandboxed experiment is a **real OS subprocess** (`anyio.run_process`), not a process pool — the per-thread worktree+sandbox is already the isolation/kill boundary, and a subprocess avoids pickling and pool-poisoning. *Rejected:* raw asyncio (thinner cancel/timeout, can't list/cancel children); trio (splits off the asyncio ecosystem); Celery/external workers (broker daemon breaks local-first/files-as-truth). *Windows:* drive cancellation through anyio cancel scopes + budget checks, **never POSIX signals**.

**Durability — hand-rolled resume-by-event-replay; NO external durable-execution engine.** The design already *is* a durable-execution substrate (append-only log + single writer + replay = what Temporal/Restate do internally). Adopting one would create a **second authoritative state store** (Temporal history / Postgres) and require a server — a direct conflict with files-as-truth + local-first. Rules: persist each step transition as an event **before** its side effect; every step gets an idempotency key; on restart, replay the log to rebuild in-memory state, then resume threads whose last event is non-terminal; record external side effects (subprocess launch, git commit) as completion events so replay never re-runs them. *Rejected:* Temporal/`temporalio` (heavy, redundant), Restate/DBOS (separate stateful service / mandates Postgres), Prefect (retries tasks, does not resume mid-workflow from exact state — doesn't even solve the need). *Risk:* correctness is on us → ship a **replay test harness** (also satisfies ADR-11 §7 engine self-testing).

**Git — shell out to the real `git` CLI** (thin wrapper module), **dulwich** as optional pure-Python fallback only when `git` is absent. The CLI is the reference impl for `worktree add/remove`, `diff`, commit, branch — fastest to spawn many worktrees, identical across platforms, and consistent with "experiments are already subprocesses." *Rejected:* **pygit2** (libgit2 `worktree_prune` removes metadata only — you must `rmtree` the dir yourself, the exact Windows file-lock trap; compiled dep); **GitPython** (maintenance-mode, documented FD/process leaks in long-running daemons, Windows-flaky — wrong for a many-worktree long-lived engine). *Windows:* ensure `git` on PATH at startup; set `core.longpaths=true`; avoid symlinks.

**Worktree lifecycle (concurrent, Windows-safe).** Each thread owns a uniquely-named worktree+branch under a per-run temp root: `git worktree add --detach <wt/thread-id> <base-sha>` → agent/operator edits → `git diff` surface-filter (ADR-14) → stage allowed paths → `git commit` → **record commit sha as an event** (lineage reconstructable from the log). Removal is the Windows-critical step: reap the sandbox subprocess **first**, then `git worktree remove --force`; on lock failure retry with backoff, then `shutil.rmtree(onerror=chmod+retry)` + `git worktree prune`. **On engine startup, always `git worktree prune` + sweep the temp root** → crash-resume self-heals orphaned worktrees. Serialize ref-touching ops (commits into the shared object store) behind a lightweight async lock if contention appears; per-worktree working-dir ops need no lock (unique paths).

---

## ADR-13 — Execution sandbox & isolation (tiered by *trust mode*, not by environment)

**The sandbox tier is a function of the TRUST MODEL, which is set by the deployment mode — not a blanket "Docker everywhere" mandate (corrected 2026-06-22).** What boundary you need depends on *whose* code runs on *whose* infrastructure:

| Trust mode | Who runs what, where | Required boundary | Sandbox tier |
|---|---|---|---|
| **`trusted_local`** *(default — the CLI)* | You run your own research on your own box. LLM-generated code is in *your* trust domain, same as any `pip install` or script you'd run. | Process isolation + resource limits (timeout, tree-kill, output caps, cwd scratch). **No security boundary required — and none claimed.** | **`SubprocessSandbox`** (no Docker) |
| **`untrusted`** *(hosted / web-UI / multi-tenant / running a third party's task)* | You execute code on infrastructure that must protect **other users or the host**. | A real boundary: filesystem + network + kernel isolation. | **Docker `--network none` + cgroups → gVisor** |

**Consequence:** **Docker is required precisely — and only — when the deployment serves untrusted code (the hosted/UI-as-a-service scenario). The local CLI engine, and the entire test suite, need no Docker and no daemon.** This is the local-first stance applied to isolation: the default path has zero infra dependencies; containers are an *opt-in escalation* you turn on via `trust_mode: untrusted` when (and only when) the threat model changes. The sandbox is a `Sandbox` Protocol with a `make_sandbox(trust_mode)` factory, so swapping subprocess→Docker→gVisor is a config change, never a code change.

Everything below describes the **`untrusted` tier** (the hard case). GPU is its binding constraint — it eliminates most managed "agent-sandbox" SaaS and Firecracker (no GPU passthrough) up front.

**Prod (Linux/NVIDIA) — two tiers, one control plane (docker-py):**
- **Default: Docker Engine + docker-py + NVIDIA Container Toolkit.** The only mature path giving `--gpus` + `--network none` + full cgroup caps (`--memory`/`--cpus`/`--pids-limit`/`--ulimit`/blkio) **and** hosting a long-running multi-step agent process (it's just PID 1). docker-py drives lifecycle/stats/kill programmatically.
- **Escalation: gVisor (`runsc`) as the Docker runtime** for maximally-untrusted single-script runs — its `nvproxy` is the only user-space-kernel isolator with working CUDA passthrough in 2026 (what Modal ships). Per-run `--runtime=runsc`; no stack change.
- **Nesting (agent spawns its own Docker): Sysbox runtime.** gVisor's docker-in-gVisor is limited; Sysbox is purpose-built for secure rootless Docker-in-container. Trade-off you accept: agent-with-own-Docker gets strong-container (Sysbox) **or** kernel-isolation (gVisor), not both — single-script ML runs → gVisor, agent-runs-Docker → Sysbox.
- *Rejected:* Firecracker (no GPU; PCIe work unfunded), Podman (rootless GPU/cgroup friction vs docker-py's mature API — acceptable substitute, not primary), nsjail/bubblewrap (process sandboxes, no clean GPU/cgroup-v2 ML story — keep nsjail only as inner defense-in-depth), E2B/Daytona/Cloudflare/microsandbox (CPU-only or block GPU). **Modal** is the *one* credible GPU-in-sandbox SaaS → keep as an **optional burst backend**, not core (breaks air-gapped/our-hardware). *Footgun:* prefer **CDI** (`--device nvidia.com/gpu=<UUID>`) over legacy `--gpus` to dodge the NVIDIA-hook cgroup-drop segfault on container update.

**Windows 11 dev — Docker Desktop + WSL2 is the PRIMARY path, not a "subprocess fallback."** WSL2 + NVIDIA GPU-PV is mature in 2026: CUDA/PyTorch + the full NVIDIA Container Toolkit run inside WSL2, giving the **same docker-py code path** as prod (`--gpus`, `--network none`, cgroup caps); nested agent-Docker also works. **Native subprocess under a Windows Job Object** (`pywin32` `win32job`: memory cap + `KILL_ON_JOB_CLOSE` for atomic tree-kill + CPU-rate) is **dev-convenience only and explicitly NOT a security boundary** — it cannot isolate filesystem or network. *Therefore: untrusted/agent code MUST go through WSL2/Docker; native subprocess is for fast inner-loop iteration on trusted code only.* This **supersedes** the older "constrained subprocess fallback on Windows" framing (§C).

**GPU capping.** Baseline: whole-GPU-per-run via `CUDA_VISIBLE_DEVICES` + CDI device pinning (robust on single-node multi-GPU). Sub-GPU packing of *untrusted* runs: **MIG** (Ampere+) — the only partitioning with hardware memory+fault isolation. *Rejected for untrusted:* MPS / time-slicing (no fault/memory isolation). *Windows:* MIG unsupported under WSL2 → whole-GPU only on the dev box.

**Resource/timeout enforcement.** Hard limit = OS-native (**cgroups** via Docker on Linux; **Job Object** on Windows). **psutil** is the cross-platform *monitor* (CPU%, RSS, disk; GPU via NVML/`nvidia-smi`) that a **separate watchdog process** (never a thread in the workload) uses to kill on any cap breach. Process-group launch (`start_new_session=True`) + `os.killpg` on Linux. POSIX `resource.setrlimit` as inner belt-and-suspenders. *Caveat:* psutil-kill alone is racy for untrusted code — the cgroup/Job-Object hard limit is the real boundary; psutil covers GPU/disk and fast wall-time reaction.

**Network-off.** Docker `--network none` is *the* boundary (loopback only, zero egress) — anti-exfil + anti-cheat. Inner defense: drop `NET_RAW`/`NET_ADMIN`, host-firewall deny the sandbox subnet. **Critical Windows caveat:** WSL2 outbound traffic historically **bypasses Windows Defender Firewall** — do **not** rely on it; containment comes from `--network none` inside Docker (works identically in Docker Desktop). Native-Windows subprocess has **no reliable per-process network-off** — another reason untrusted runs route through containers.

**Summary table:**

| Concern | Prod (Linux/NVIDIA) | Dev/fallback (Win 11) |
|---|---|---|
| Sandbox | Docker+docker-py → **gVisor** escalation; **Sysbox** for agent-runs-Docker | Docker Desktop+WSL2 (primary); Job-Object subprocess (trusted dev only) |
| GPU cap | CUDA_VISIBLE_DEVICES+CDI; **MIG** for untrusted packing | whole-GPU only (no MIG in WSL2) |
| Resource/timeout | cgroups (hard) + psutil watchdog + killpg | Job Object (hard) + psutil watchdog |
| Network-off | `--network none` | `--network none` (never trust Windows Firewall for WSL2) |

Escalate to **microVM-grade (Cloud Hypervisor / QEMU + VFIO GPU passthrough)** only if gVisor `nvproxy` driver-compat blocks a required ML stack *and* a true VM boundary is needed. Not day one.

---

## ADR-14 — Structured outputs & patch/diff handling

**Structured outputs — standard tool calling is the DEFAULT; BAML (SAP) is the SECONDARY fallback; outlines is the self-hosted guarantee tier.** The parser is a per-role strategy (`roles.<role>.parser: tool_call | baml | outlines`), same plugin philosophy as the rest of the system. Prompt *bodies* stay MD+frontmatter throughout (ADR-8 intact — the parser is orthogonal to the prompt store).
- **Default = `tool_call`: native provider tool/function calling via LiteLLM's unified interface**, validated to pydantic with bounded retries (instructor is the ergonomic wrapper for this path; or LiteLLM `response_format`/tools directly). Chosen as the base because it is the **interoperable industry standard** and the *same mechanism* MCP tools and the external coding-agent backends already speak ([ADR-7](03-decisions.md)/[ADR-9](03-decisions.md)) — so **tool use and structured output travel one channel** — and frontier APIs + modern local servers (vLLM tool parsers, recent Ollama) support it well. Used for `Researcher → list[Idea]`, the `Developer` patch envelope, and the Evaluator `Verdict` whenever the backend does tool calls reliably.
- **Secondary = `baml`: BAML Schema-Aligned Parsing** — the robustness net where the standard is weak: weak/older local models that don't emit clean tool calls, and as a pinned choice for the **safety-critical Evaluator `Verdict`** where a wrong parse is most dangerous. SAP parses prose/markdown-wrapped JSON from raw text without needing native tool-calling. The engine **auto-falls back `tool_call → baml` on repeated parse/validation failures**; `baml` can also be pinned per role. Drive BAML against the **LiteLLM proxy** URL so routing stays single-sourced.
- **Optional = `outlines`: constrained decoding** on self-hosted vLLM roles for a hard token-level guarantee.
- *Why this order:* tool calling is the standard, unifies tools+outputs on one channel, and is what the agent/MCP layers already use; BAML is reserved as the fallback exactly where native tool calling is unreliable — not a dependency on every local model doing tool calls perfectly. *2026 gotcha it plans around:* LiteLLM↔local tool-calling can still misbehave (Ollama returns NL or raw-JSON-in-content; `json_schema` unsupported on some adapters) → the per-role fallback to `baml` (or `outlines` on vLLM) is the mitigation. *Accepted cost:* two parsers in the codebase, but behind one `StructuredOutput` strategy interface so callers are agnostic.

**Patch representation — unified-diff text + summary, applied via `git apply` in the worktree.** Unified diff is native to both LLMs (well-represented in training) and tooling, makes per-file path filtering trivial/auditable, and is the reproducible files-as-truth artifact (store the `.patch`). Weak-model mitigation: return the diff as a typed `Patch` field (instructor/BAML repairs surrounding prose) → `git apply --check` dry-run → on failure retry with the rejection as feedback → `--3way` fallback → per-file **whole-file-rewrite escape hatch** for very weak models (we synthesize the diff). *Rejected:* whole-file-rewrite as default (token-heavy, clobbers concurrent edits), `diff-match-patch` (char-level, no hunk/path concept → can't filter surface), pure structured-edit JSON (brittle anchors, loses git's safety net).

**Out-of-surface rejection — double-gated, reject (don't strip).** (1) Run Developer in an isolated worktree. (2) Parse with **unidiff** `PatchSet`, and **reject the whole patch** if any target path ∉ edit-surface allow-list or escapes it (`..`, absolute, symlink) — a forbidden-file touch is a *signal*, fed back into the retry loop, not noise to trim (stripping can yield half-applied broken files and hides misbehavior). (3) Apply with `git apply --include='<glob>' --exclude='*' --check` then for real (`--unsafe-paths` OFF) — kernel-grade enforcement so a parser bug can't leak. (4) Verify `git diff --name-only -- <allowed>` changed nothing outside surface. *Why both unidiff + git pathspec:* independent gates — testable Python logic + git enforcement. *Caveat:* normalize `-p1`/canonicalize paths before allow-list compare or weak-model relative prefixes false-reject. *Risk:* unidiff is stable but stale (2023) — use it for **parsing only**; `git apply` does the applying (or swap `whatthepatch` if active upstream matters).

---

## ADR-15 — Trust layer (the differentiator) — concrete implementation

Honest scope: **no library models ML-pipeline leakage or temporal look-ahead** — that is the moat and is **custom**. Libraries cover only the data-quality primitives.

**Leakage detection — custom core + cleanlab (Apache-2.0) for primitives; do NOT depend on deepchecks.**
- **From cleanlab (library):** label errors (confident learning), near-duplicate detection (reused for cross-split duplicate hunting), outlier/OOD. Healthy (v2.9.x, 2026), safe license, covers tabular/vision/NLP.
- **Custom (no library does these properly):**
  - *Train/test contamination* — row-hash + embedding near-dup match across the **fixed** splits (cleanlab dup scores as input). Deterministic, ~tens of LOC.
  - *Target leakage* — per-feature mutual-information / single-feature AUC vs target with a high-threshold flag (heuristic) **plus** a per-feature **time-of-availability annotation** (a feature unavailable at prediction time is the only reliable signal — no library can infer it).
  - *Temporal / look-ahead* — **fully custom:** enforce time-ordered splits with `train.max_time < test.min_time + embargo`; reject global preprocessing (scaler/encoder/imputer/target-encoding) fit before the split (detect transformers fit outside the fold); rolling windows must not cross the boundary. **Highest-value, lowest-coverage → spend real engineering here.**
- *Rejected as a runtime dep:* **deepchecks** — best *named* checks but **AGPL-3.0** + stalled (0.19.1, Dec 2024); unwise to license-couple a promotion gate to AGPL. Mine its check *designs*, reimplement the cheap ones MIT-clean.

**Cross-validation — scikit-learn splitters + custom consistent-eval harness.** `StratifiedKFold` / `StratifiedGroupKFold` / `TimeSeriesSplit` / `RepeatedStratifiedKFold` (the robust-CV default, e.g. 5×5, to fight the winner's curse); `mlxtend.GroupTimeSeriesSplit` + a **custom purged+embargoed walk-forward** splitter (sklearn's `TimeSeriesSplit` doesn't purge/embargo — required to make temporal-leakage enforcement real). Nested CV only when a candidate runs its own HPO. **Custom (the trust guarantee):** generate splits **once** from a fixed seed, **persist the split indices**, score every candidate on the identical indices = the consistent-evaluation protocol (the +9–15 pt AIRA lever).

**Variance gate — scipy.stats.bootstrap (BCa) + numpy only.** Headline CI per candidate via BCa bootstrap on per-fold/per-sample scores (distribution-free, correct for AUC/F1). Gate SE = `std(fold_scores)/sqrt(n_folds)`. Promote B over incumbent A only if `mean(B) − mean(A) > 1·sqrt(SE_A²+SE_B²)`. Top-k frontier: re-run on 3–5 seeds, report **mean ± std**, require the >1-SE margin to hold across seeds. *Rejected:* statsmodels (overkill), p<0.01-everywhere (design-rejected, too expensive). Keep the corrected-resampled-t-test as an optional, not default.

**Data profiling — custom lightweight JSON profiler (pandas/numpy/scipy), not ydata-profiling.** Emits schema/dtypes/cardinality/missingness/distribution + **per-feature target correlation / single-feature predictive power** — this JSON **is** the leakage checker's front-end (target-leakage heuristic reads it directly), deterministic and fast. *Rejected:* ydata-profiling (heavy/slow; **renamed→`fg-data-profiling` April 2026, original frozen** — identity/maintenance risk; HTML not built to feed a gate). Optionally emit `fg-data-profiling` HTML for human eyeballing only, never as gate input.

---

## ADR-16 — Knowledge / RAG / capability layer — concrete stack

Coherent theme: at this corpus size (hundreds–low-thousands of notes; design skips RAG under ~200k tokens), **embedded + thin-custom beats every server/framework**. The only real library commitments are LanceDB, FlashRank, FastMCP, skills-ref, LiteLLM.

**Vector store — pluggable `VectorStore` backend; LanceDB is the default.** The store sits behind a protocol (same plugin philosophy as `TaskAdapter`/`SearchPolicy`/`RoleBackend`), selected by `knowledge.index.backend`, so a deployment can swap in Qdrant/FAISS/Chroma/pgvector without touching the retrieval router or any caller:
```python
class VectorStore(Protocol):                       # selected by config.knowledge.index.backend
    def upsert(self, index: str, items: list[Note]) -> None: ...
    def search(self, index: str, query: Vector, k: int, where: Filter | None = None) -> list[Hit]: ...
    def hybrid(self, index: str, query: Vector, text: str, k: int, where: Filter | None = None) -> list[Hit]: ...
    def delete(self, index: str, ids: list[str]) -> None: ...
    def rebuild(self, index: str) -> None: ...     # re-derive from canonical knowledge/*.md
```
Built-ins: **`LanceDBStore` (default)**, `QdrantStore` (server, for scale-out), `FaissStore` (raw-ANN, in-memory/embedded), `ChromaStore`. A backend that lacks native FTS falls back to a `bm25s` sidecar for `hybrid()`.

**Why LanceDB is the default** (resolves the prior "FAISS/sqlite-vec" vs "sqlite-vec/Chroma" split). Only candidate hitting every *base* constraint at once: truly embeddable (no server), directory-based persistence (clean gitignored derived index), reliable **Windows wheels**, native metadata filtering (`where()` — needed for separate-index routing), and **built-in hybrid vector+BM25 FTS**. Apache-2.0, active. *Rejected as the default:* **sqlite-vec** (stock CPython on Windows ships without `enable_load_extension` + DLL load failures → **fails the hard Windows requirement**), FAISS (no metadata filter, no FTS — kept as a plugin for raw-ANN cases), Chroma (heavier, cloud-pivot — kept as a plugin), Qdrant-embedded (dev-toy; the real Qdrant is a server — kept as the **scale-out plugin**), DuckDB-VSS (HNSW persistence experimental).

**Why not Qdrant/FAISS for the base (speed is not the binding axis at our scale).** Third-party benchmarks at *million-vector* scale do favor them — GIST-1M: Qdrant HNSW ~20–30 ms / ~95% recall@1 vs LanceDB IVF_PQ ~40–60 ms / ~88% (old versions, non-matched index), and FAISS is the fastest raw engine. But (a) our corpus is **hundreds–low-thousands** of vectors (design skips RAG under ~200k tokens), where even **flat/exact search is sub-ms at 100% recall** and the ANN-algorithm choice is moot; (b) a vector lookup is **<0.1%** of a step dominated by the following 2–60 s LLM call; (c) Qdrant's published numbers are server-over-network, not the embedded mode we'd use. So the speed crown is won on an axis that doesn't bind us, in a deployment we won't run. The `VectorStore` plugin is the escape hatch: if scale ever makes speed bind (millions of vectors, multi-tenant), set `backend: qdrant` and `rebuild()` — a config change, not a rewrite. *Risk:* low; pin the version and **store the embedding model id beside the index** (re-embed on model change is detectable since the index is rebuildable).

**Lexical/BM25 — LanceDB native FTS** (one engine, no second index to sync). bm25s is the only acceptable standalone fallback if FTS proves inadequate (never rank-bm25 — unmaintained/slow). *Rejected:* Tantivy/tantivy-py (Rust-build risk on Windows + second index, overkill).

**Reranker — FlashRank (local ONNX), built as a toggle, default OFF.** At hundreds–low-thousands of chunks reranking is marginal; build the hook, measure before enabling. Wrap behind a thin adapter (or `rerankers`) so FlashRank↔CrossEncoder↔hosted-API is config-only. *Rejected as default:* sentence-transformers CrossEncoder (heavier, keep as local quality tier), Cohere/Voyage APIs (violate local-first — escape hatch only).

**GraphRAG — DEFER/CUT for v1 → lightweight [[wikilinks]] → networkx.** GraphRAG solves global sensemaking over corpora too big for context — a problem we mostly don't have. The wikilinks graph already exists in canonical Markdown: parse links+frontmatter into networkx (~150 LOC, **zero LLM calls**, instant rebuild, files-as-truth-native); dangling links = human-asserted "what's missing" for free; optional one-LLM-call-per-cluster summaries (Louvain/Leiden) give ~80% of community-summary value on demand. *Rejected:* Microsoft graphrag ("demonstration, not supported," Azure-trending, $50–200+ indexing, parallel parquet state violates files-as-truth), LlamaIndex PropertyGraph (couples to a rejected framework), nano-graphrag (single-maintainer — if you'd vendor it, write the 150 lines instead). Reconsider a real lib only past tens-of-thousands of chunks with measured failure. *Windows:* prefer pure-Python networkx community detection (igraph/leidenalg wheels finicky on Windows).

**RAG orchestration — thin custom over LiteLLM + LanceDB.** chunk→embed→store→top-k→stuff (~200–300 LOC), gated by the corpus-size check (<~200k tokens → skip retrieval, load in-context). *Rejected:* LlamaIndex (Document-as-source-of-truth fights files-as-truth; API churn), Haystack (enterprise ceremony). Re-implement minor conveniences à la carte only if quality demands.

**Retrieval is an agentic TOOLSET (grep/find/read + RAG), agent-chosen — not a fixed RAG pipeline.** Expose retrieval as a set of MCP tools and let the role pick per query; for code and structured/Markdown corpora, lexical + navigation tools routinely beat embedding-RAG (the Claude-Code lesson), while vector search wins for fuzzy semantic recall over prose — so ship both and let the model choose. Toolset (a `knowledge-mcp` server):
- `grep(pattern, path, flags)` — fast regex over `knowledge/*.md`, experiment notes, code (**ripgrep** via subprocess; the fastest, most-trained-on interface);
- `glob(pattern)` / `ls(path)` / `read(path, offset?, limit?)` — filename search + directory listing + bounded file read (our code over `pathlib`);
- `vector_search` / `hybrid_search(query, k, where)` — semantic + hybrid recall via the `VectorStore` (LanceDB default);
- `query_archive` (structured past-runs/metrics, `archive-mcp`) and `web` (`web-mcp`) round it out.
Guidance lives in a Skill (ADR-9, procedural tier): *prefer grep/glob/read for exact names, symbols, file structure, and small corpora; use vector/hybrid for "something like X"; use web only when the local corpus is dry.* This **subsumes** the corpus-size rule — under ~200k tokens the agent just greps/reads files (no index needed); vector search is for when semantic recall over a larger prose corpus actually pays. *Rejected:* a single fixed retrieve-then-stuff RAG step (forces embedding recall even when exact lexical match is what's wanted; the dominant agentic-retrieval failure mode). *Risk:* tool-choice quality depends on the model — mitigate with the Skill guidance + per-role allow-lists.

**MCP — FastMCP v2 servers + thin MCP→tool-schema adapter for `async_mcp_tool`.** FastMCP v2 (`pip install fastmcp`) is the de-facto standard built on the official SDK (decorators, auto-schema-from-typehints, HTTP+stdio, in-memory test client). Consumption: `fastmcp.Client.list_tools()` + a ~30-line mapper converting MCP `inputSchema`→Anthropic/OpenAI tool schema (no standard helper ships — the tiny adapter *is* the expected pattern). *Relationship:* FastMCP v1 folded into the official `mcp` SDK; **v2 is the separate, ahead, maintained project** — pin it (single-maintainer-led, fast-moving); official SDK is the bounded fallback. ***Windows-critical:*** stdio transport has process-spawn/encoding quirks → **use streamable-HTTP transport** for both servers and client (identical Win11↔Linux). *Rejected:* bare `mcp` SDK (low-level — max-control fallback), langchain-mcp-adapters (drags in LangChain), LiteLLM MCP bridge (less flexible than owning the adapter).

**Agent Skills — skills-ref + a ~130-LOC progressive-disclosure loader.** SKILL.md is now an open standard (agentskills.io); `skills-ref` (`pip install skills-ref`) is the spec authors' parser/validator (`read_properties`/`validate`/`to_prompt`). Wrap it: inject name+description for all → read full body on selection → read referenced files on demand. *Rejected:* skillkit (heavier, Linux-centric script security), hand-rolled PyYAML (drifts from spec). *Windows caveat:* skill **scripts** (`.sh`) often need WSL/Git-Bash — validate script execution on Windows; parsing itself is pure-Python and fine.

**Embeddings — LiteLLM, default `ollama/nomic-embed-text`.** `litellm.embedding()` routes Ollama/vLLM/HF + all APIs through one call; per-role swap is a one-line model-string change. Default: nomic-embed-text (tiny/fast CPU, 768-dim/8K ctx, identical Win11↔Linux via Ollama). Upgrade paths: Qwen3-Embedding (quality/multilingual), bge-m3 (multilingual hybrid), EmbeddingGemma-300M (best small **code** embeddings). *Windows:* Ollama is the cleanest local path; **vLLM is WSL2/Linux-only** (fine for prod, not native Win11). Pin a versioned Ollama tag, not `latest`.

---

## ADR-17 — Files / event-sourcing / observability / plumbing

**Atomic writes — hand-rolled `os.replace` + fsync helper (~30 LOC).** temp in the *same dir* → `flush` → `os.fsync(fileno)` → `os.replace(tmp, target)`; POSIX-only parent-dir fsync branch. *Rejected:* `python-atomicwrites` (unmaintained/deprecated 2022, pulled from PyPI), `filelock` (cross-process locking, not torn-write prevention — unneeded, engine is sole writer). ***Windows:*** `os.rename` raises on existing target → **must use `os.replace`** (atomic only same-volume → always tmp in the target dir); parent-dir fsync unsupported → branch on `os.name`.

**Event store + read-model — hand-rolled JSONL (orjson) + rebuildable SQLite.** Append one orjson line + `\n`, flush/fsync; the engine is the **sole writer**. An ordinary physical line is one logical event; bounded `append_many` writes one reserved envelope line so a torn transaction exposes no members, and every event-aware reader expands it before replay/projection. The guarded batch marker deliberately violates the old `Event.type: str` shape, making pre-batch binaries fail closed rather than advance past invisible nested actions; the current reader retains the initial string-marker compatibility path. Logical sequences must be dense from zero across ordinary and batch rows. Generic JSONL readers remain raw and format-agnostic. The query read-model is **SQLite**, a disposable cache rebuilt by replaying logical events (track last-applied seq/physical offset; truncate+replay if stale). *Rejected:* `eventsourcing` pkg (wants to own persistence — conflicts with files-as-truth), DuckDB (heavier, weaker incremental/concurrent-write — documented upgrade path since the read-model is swappable), stdlib json (3–15× slower — fallback only). ***Windows:*** open `events.jsonl` **binary-append (`"ab"`)**, write `b"...\n"` (avoid CRLF mangling); UI's SQLite connection `mode=ro`.

**File watching — watchfiles + forced polling on network mounts.** Rust/notify-based, pydantic-team maintained. ***Windows-critical:*** native `ReadDirectoryChangesW` **silently misses remote-originated writes on mapped SMB/UNC drives** (likewise Linux NFS) — expose `ui.watch_force_polling: bool|None` in config and set `force_polling=True` for any network path. *Rejected:* watchdog (heavier/quirkier), raw OS APIs (non-portable).

**JSON Schema — pydantic v2 `model_json_schema()` for all file-kinds; validate with `jsonschema`.** One source of truth for parse+validate+schema (Draft 2020-12). In normal load paths pydantic *is* the validator — use `jsonschema` only for external schemas / CI assertions (don't double-validate). **Upcast-on-read:** every doc carries required `apiVersion`+`kind`; on load, read raw → apply a migration chain `{(kind,from_ver): fn}` to current → parse current model; old `events.jsonl` bytes stay immutable (upcast each line in memory); unknown future version → hard error. *Rejected:* fastjsonschema (stuck at draft-04/06/07 — chokes on `prefixItems`/`unevaluatedProperties`).

**Structured logging — structlog, hard-split from domain events.** Dual render: `ConsoleRenderer` (dev) + `JSONRenderer`→`logs/run-<id>.jsonl` (prod). **Architectural rule:** structlog = lossy diagnostics; `events.jsonl` = domain truth written only by the single writer — **never route domain events through the logging framework** (it drops/reorders/buffers, breaking single-writer). Funnel third-party stdlib logging *into* structlog one-way via `ProcessorFormatter`; that bridge never touches `events.jsonl`. *Windows:* `colorama` for console color; JSONL `encoding="utf-8", newline=""`.

**Observability — opentelemetry-sdk + custom JSONL SpanExporter (files-as-truth).** Emit `gen_ai.*` attributes by hand around LiteLLM calls (model/tokens/latency/cost/finish-reason); set `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`; isolate the `gen_ai.*` mapping behind **one module** (the GenAI semconv is still experimental/churning in 2026 — one-file blast radius). Optional OTLP-to-collector only when configured (off by default). *Rejected:* OpenLLMetry/Traceloop (acquired by ServiceNow March 2026 — steward risk; heavy monkeypatch auto-instrumentors), OpenInference (vendor-biased schema — borrow attribute *names* only). *Windows:* exporter JSONL UTF-8 append, `newline=""`, flush on shutdown.

**CLI — Typer.** Type-hint subcommands (run/resume/inspect/rebuild), Rich help; vendors Click internally (no version skew). *Rejected:* Click direct (boilerplate), argparse (verbose, no typing/Rich).

**Report templating — Jinja2.** Loops/conditionals/includes for multi-section Markdown (`autoescape=False`, `trim_blocks`/`lstrip_blocks`); write through the atomic writer so the UI never sees a half-rendered report. *Rejected:* f-strings (unmaintainable for structured docs), Mako/Mustache (overkill / too weak).

**Cost/budget watchdog — in-proc accountant over LiteLLM cost + psutil two-phase tree-kill (OS-branched).** Accountant reads per-call cost (`response._hidden_params["response_cost"]`/`litellm.completion_cost`), nested run→thread→role counters, **warn 80% / hard-stop 100%** via a budget-exceeded exception aborting the offending scope; cost events persisted to `events.jsonl`. Kill: single psutil abstraction — `children(recursive=True)` → `terminate()` → `wait` → `kill()` survivors; **Linux** `start_new_session=True` + `os.killpg(SIGKILL)`; **Windows** Job Object with `KILL_ON_JOB_CLOSE` for guaranteed whole-tree kill (no SIGKILL/`killpg`; `start_new_session` is a no-op). *Rejected:* `Popen.kill()` alone (leaks grandchildren), LiteLLM Proxy BudgetManager (needs a server — violates local-first). *Caveat:* LiteLLM pricing tables can lag new models → guard `None`/0 cost, allow a pricing override.

---

## ADR-18 — Core runtime shape: library + CLI process, hand-rolled engine, no agent framework

**This was implicit across ADR-1/6/7/12; making it explicit because it's the most load-bearing structural choice.**

**Decision — the core is an importable Python package + a thin Typer CLI, executed as a local long-lived *process* (not a server), and it is NOT built on any agent-orchestration framework.** The engine is our own code: a control loop on **anyio** structured concurrency, state as the **files-as-truth event log** (resume = replay; ADR-1/12), DI from **pydantic-settings** (ADR-11).

- **Script/server:** run like `pytest`/`git` — `LoopLab run task.yaml` spawns one async orchestrator process per run (which spawns sandboxed experiment subprocesses + per-thread git worktrees). **No server is required.** Servers exist only as optional *peripheral projections*: the later web-UI FastAPI (reads files) and the MCP capability servers (local tool processes, streamable-HTTP). The engine core never needs one.
- **Tree/search logic = our `SearchPolicy`** (greedy tree + gated merge) over an in-memory **DAG that is a fold of the event log** — plain Python data structures (+ `networkx` only for lineage graph ops, since `parent_ids` is an adjacency list). Not a framework graph engine.
- **Agent/role logic = our `Researcher`/`Developer`/`Operators`/`Evaluator`**, each calling a pluggable backend (LLM via LiteLLM, or an external coding-agent CLI via `RoleBackend`, ADR-7); native tool calling is the channel (ADR-14).

**Why no agent framework (LangGraph / CrewAI / AutoGen·AG2 / LlamaIndex Workflows / OpenAI Agents SDK / Google ADK / smolagents):**
1. **The loop is the moat** (ADR-6/7) — a framework would own exactly the search-loop+operators+evaluator we must own.
2. **State-model conflict** — every framework brings its own checkpoint/state store = a second source of truth; same reason we rejected Temporal (ADR-12), `eventsourcing` (ADR-17), LlamaIndex Document (ADR-16). Files-as-truth + single-writer + replay is the spine.
3. **Impedance mismatch** — our "tree" is a search tree over *solutions/experiments* (merge, diversity, variance gating, lineage), not a generic agent control-flow graph; mapping onto LangGraph nodes is lossy translation + lock-in.
4. **Reproducibility + minimal magic** — replay-and-explain-every-step is a core goal; hidden framework control flow works against it.
- *LangGraph specifically* (closest-looking — graphs + checkpoint + resume): overlap is superficial. Its checkpointer wants to own persistence (#2) and its graph is *control-flow*, not our *solution-search* DAG (#3). We already take its **HITL-as-events** idea as a *pattern* (ADR-11), without the dependency.

**What we DO reuse (own the core, reuse the edges — the ADR-7 principle applied everywhere):** LiteLLM (providers), external coding agents (the coding step), FastMCP (tool bus), instructor/BAML/outlines (parsing), anyio/Typer/structlog/watchfiles (plumbing). Don't reimplement what isn't the moat.

**Per-flow LangGraph re-evaluation (the two flows differ).**
- *(1) Experiments tree (outer loop)* — **no**, confirmed and strengthened: long-running, our **event log is canonical state**, and it searches over *solutions* (not control flow). LangGraph's headline value (durable execution / resume) is what our replay already provides → it'd be a competing checkpointer (files-as-truth conflict) or a custom-checkpointer adapter for parity-not-gain, **plus** per-concurrent-execution memory overhead (~50–150 MB each × many threads) on a local/Windows box.
- *(2) Researcher inner flow (per `propose()` call)* — closer, because retrieval (step 1) and self-critique (step 4) are genuine **cycles** (LangGraph's strength) and the flow is ephemeral, so LangGraph could run **stateless** (no checkpointer → no second store). **Still our code by default:** step 1's loop is already native via tool-calling + MCP (ADR-9/14), step 4 is a ~30-LOC bounded loop, the novelty filter (step 3) is a deterministic gate that doesn't belong in an agent graph, and a second paradigm (LangChain runnables + its tool/state model) would fight the one-tool-channel decision (ADR-14). The rich Researcher is a short pipeline with two bounded cycles (~150–250 LOC reusing what we have).
- **Escape hatch (no core change):** the Researcher is a role behind the role/`RoleBackend` seam (ADR-7), so a **LangGraph-based Researcher is a legal plugin** — adopt it only if/when the Researcher grows into a complex **multi-agent deliberation** (co-scientist-style generation/reflection/tournament/meta-review with persistent cross-step state). Even then run it **stateless** or with a **custom checkpointer over our event log**, never LangGraph's default SqliteSaver/Postgres.

**Deeper "why not just reuse LangGraph's engine" (the build-vs-buy call, ratified 2026-06-21 → *custom now, LangGraph plugin when a role gets complex*):**
- *It replaces the easy 5%, not the hard 95%.* LangGraph would only supply the control-flow skeleton (`while not stop: select→expand`) + checkpoint/fork/HITL. Operators, trust layer (leakage/CV/gate), sandbox, git lineage, archive, roles — the hard 95% — are ours regardless. The trade is "save the easiest part, in exchange for a framework's state model governing the hard parts."
- *Graph-kind / state-shape mismatch (outer loop).* LangGraph is a **control-flow graph with a single flowing state object, checkpoint-snapshotted per super-step**. Our DAG is a **dynamically-generated search frontier of experiments**, with state spread across an **append-only event log + git + CAS**. Putting the loop in LangGraph would snapshot the whole archive every tick (snapshot-oriented, not append-delta) → a competing canonical store + the files-as-truth conflict. Our fork (git ref + `parent_ids`) and resume (event replay) are *better-fitted* to experiments than chat-state snapshots.
- *Tracing caveat.* LangGraph's headline tracing is **LangSmith (paid SaaS / separate self-host)** — not local-first; its OSS-local path is plain OTel, which we already have.
- *Non-negotiable:* if LangGraph ever appears, it is **stateless inside a role plugin** — its checkpointer never governs run state.

**Reconsider-trigger:** only if we ever needed *distributed multi-node* orchestration (explicit non-goal in [01](01-product-design.md)) would a durable-execution engine (Temporal) earn its place — and even then as the *transport*, with the event log still canonical.

---

## B. Step-3 validation — is everything OK, covered, conflict-free, buildable?

### B.1 Business-requirement coverage (every requirement → where it's satisfied, now concretely)

| Requirement (01) | Concrete satisfaction |
|---|---|
| Autonomous ML experimentation | anyio control loop (ADR-12) + operators (§3.6b) + sandbox (ADR-13) |
| Best **verified** results | trust layer fully scoped (ADR-15) — leakage/CV/gate now buildable |
| Pluggable LLM backend (API + local, per-role) | LiteLLM + proxy (ADR-14); embeddings via LiteLLM (ADR-16) |
| Customizable LLM backend incl. external coding agents | ADR-7 backends + structured-output layer (ADR-14) + sandboxed agent process (ADR-13) |
| Trustworthy / reproducible | hand-rolled replay (ADR-12) + atomic writes + upcast-on-read + SQLite read-model (ADR-17) + config snapshot (ADR-11) |
| Decoupled UI / files-as-truth | events.jsonl (orjson) + watchfiles + SQLite projection (ADR-17); single-writer preserved |
| Pluggable algorithm / extensibility | unchanged (ADR-2); DI from config |
| Artifact ingestion + grounding | Docling/etc (ADR-3) + thin RAG over LanceDB (ADR-16) |
| Capability layer (tools/skills) | FastMCP v2 + skills-ref (ADR-16) |
| Knowledge/memory | LanceDB indices + wikilinks graph + LiteLLM embeddings (ADR-16) |
| Prompts UI-editable/hot-reload | **preserved** by choosing instructor-default over BAML-everywhere (ADR-14) |
| Hardening (secrets/config/obs/HITL/cost/isolation) | ADR-11 + concrete obs/cost/kill (ADR-17) + isolation (ADR-13) |

→ **All 01/02 requirements now map to a named library or scoped custom code.**

### B.2 Conflicts found & resolved (see §C for the actual edits)
1. **Vector store unresolved** (02 §14 "FAISS/sqlite-vec"; 04 "sqlite-vec/Chroma") → **pluggable `VectorStore`, LanceDB default** (Qdrant/FAISS/Chroma plugins; sqlite-vec dropped — fails the hard Windows requirement). *Edit 02 §13/§14 + 04.*
2. **"BAML only for Evaluator" vs "BAML everywhere"** → resolved to **instructor default + BAML for Evaluator** to preserve ADR-8's hot-reload/UI-edit prompts. *Note added to ADR-8.*
3. **"subprocess fallback on Windows" implies it's a safe boundary** → corrected: **WSL2/Docker is the primary Windows path**; native subprocess is trusted-dev-only, not a security boundary. *Edit 02 §10 + §14.*
4. **GraphRAG implied as built** (02/04) → **deferred/cut**, replaced by lightweight wikilinks graph. *Note added; aligns with ADR-6 "lighten ingestion."*
5. **Durable-execution ambiguity** ("resumable from events.jsonl") → confirmed **hand-rolled, no external engine** — no conflict, made explicit (ADR-12).

No remaining contradictions detected across 00–05 after these edits.

### B.3 Buildability matrix — do we know how to build each part?

| Subsystem | Buildable now? | Residual unknown / first risk to retire |
|---|---|---|
| Control loop + resume | ✅ | replay-determinism correctness → replay test harness (P0) |
| Git worktrees | ✅ | Windows lock-on-remove → retry+prune sweep (proven pattern) |
| Sandbox prod | ✅ | gVisor nvproxy driver-compat for exotic CUDA kernels (escalate to QEMU+VFIO if hit) |
| Sandbox Windows | ✅ | requires Docker Desktop+WSL2 installed (document as a dev prereq) |
| Structured outputs | ✅ | weak-local tool-call reliability → per-role auto-fallback `tool_call → baml` (or outlines on vLLM) |
| Patch apply + surface gate | ✅ | weak-model diff malformation → check→retry→3way→rewrite ladder |
| Leakage checker | ✅ (custom) | temporal/target leakage is genuinely hard → the deliberate engineering investment (P1) |
| CV + variance gate | ✅ | none material (sklearn+scipy stable) |
| Knowledge/RAG/MCP | ✅ | FastMCP v2 churn → pin; reranker value → measure before enabling |
| Files/obs/cost | ✅ | OTel GenAI semconv churn → isolated one-module mapping; Windows Job-Object CI runner |

→ **Every subsystem is buildable with a named approach.** Residual risks are *known* and each has a first-step mitigation — none is a design unknown.

### B.4 Net new dependency set (all permissive-licensed unless noted)
Core: `litellm` (tool calling default), `anyio`, `instructor` (tool-call→pydantic wrapper), `baml` (SAP secondary fallback + Evaluator), `outlines` (opt, self-hosted vLLM), `pydantic`/`pydantic-settings`, `typer`, `jinja2`, `orjson`, `jsonschema`, `structlog`, `watchfiles`, `opentelemetry-sdk`, `psutil`, `docker` (docker-py). Trust: `scikit-learn`, `scipy`, `numpy`, `cleanlab` (Apache-2.0), `mlxtend`, `pandas`. Knowledge: `lancedb`, `flashrank` (opt), `fastmcp`, `skills-ref`, `networkx`, **`ripgrep`** (system binary, for the `grep` retrieval tool — bundle/check on PATH like `git`). Patch: `unidiff`. Windows: `pywin32` (Job Objects). Tracking/ingestion/UI: per ADR-3/4/6 (`mlflow` opt, `docling`/`grobid`/`trafilatura`, `textual`). **Avoid:** deepchecks (AGPL+stalled), ydata-profiling (frozen→fork), python-atomicwrites (deprecated), rank-bm25 (unmaintained), GitPython/pygit2 (as primary), Microsoft graphrag (over-scoped).

---

## C. Changes folded back into 01/02/04

- **[02 §13/§14](02-architecture.md):** vector index `FAISS/sqlite-vec` / `sqlite-vec/Chroma` → **pluggable `VectorStore` (LanceDB default)**; new extension-points row; added pointer to this doc for the full concrete stack.
- **[02 §10](02-architecture.md) / §14:** Windows sandbox reframed — **Docker Desktop+WSL2 primary**, native subprocess = trusted-dev-only (not a security boundary).
- **[04 §2 / §5](04-file-layout.md):** `index/` derived projection named as **LanceDB**.
- **[03 ADR-8](03-decisions.md):** note that structured-output parsing standardizes on **instructor (default) + BAML (Evaluator)**, preserving hot-reload/UI-edit prompts (full rationale in ADR-14 here).
- **[00-INDEX.md](00-INDEX.md):** doc 05 added to the reading order; ADR-12…17 listed.

> This doc is the bridge from architecture to a P0 repo skeleton: the scorecard (§A) is effectively the dependency manifest + module map, and §B.3 is the de-risking order.
