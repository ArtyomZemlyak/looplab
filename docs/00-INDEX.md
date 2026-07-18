# LoopLab — Documentation Index

**Project:** LoopLab — an open, backend-flexible **autonomous ML/DS research engine** (an LLM agent that invents → implements → tests → improves ML solutions in a loop, returning the best *verified* result).
**Status:** current documentation authority map · **Created:** 2026-06-20 ·
**Validated/consistency-checked:** 2026-07-18 · **Runtime authority:** current `master` source and tests

> 📖 **Looking for how to *use* LoopLab?** This index covers the *design* (the why). For practical,
> task-oriented documentation — install, quickstart, CLI, configuration, tasks — see the
> **[User Guide](guide/index.md)** and the [README](../README.md).

> **Current implementation authority (2026-07-18):** current `master` source and tests decide runtime truth;
> [doc 16](16-architecture-code-review-2026-07-11.md)
> is the finding/reproduction ledger; [doc 17](17-project-review-and-directions-2026-07-11.md) is the
> canonical priority, dependency, and release-gate plan; [doc 18](18-ui-ux-review-2026-07-11.md) is
> authoritative for UI/UX observations and UI-specific acceptance criteria. Doc 18 is subordinate to
> doc 17's overall ordering; it explicitly distinguishes landed, narrowed, open and still-unvalidated
> findings rather than implying that the whole audit is fixed. [Doc 19](19-ide-integration-and-remote-development-2026-07-12.md)
> is authoritative for IDE-integration and secure remote-workspace option criteria; it is likewise
> subordinate to doc 17 and does not claim that an IDE or remote-access path is implemented.
> [Doc 20](20-looplab-unified-ds-workspace-and-distributed-execution-2026-07-12.md) is the
> multi-user workspace and distributed-execution specialization: it evaluates one
> OIDC-authenticated LoopLab with JupyterHub, experiment-linked IDE sessions, sealed workspace
> revisions, shared/dedicated GPU entitlements, autonomous batch fill, both managed and externally
> operated LLMs, Kubernetes/dedicated-server backends, and multiple execution domains without
> bypassing doc 17's prerequisite gates. [Doc 21](21-full-functionality-review-2026-07-13.md) is the
> detailed implementation/review chronology and latest post-fix validation ledger. **Round 25** is the dated
> `4d1218c` checkpoint; use the **post-Round-25 integration ledger** for explicitly newer deltas. For
> Part-IV/V integration ordering use **doc 17 §22.11**; for current UI semantics and acceptance use
> **doc 18 §37**. Earlier rounds/sections remain dated evidence, not competing current authorities.

---

## Read in this order

| # | Doc | What it answers |
|---|-----|-----------------|
| 21 | **[21-full-functionality-review-2026-07-13.md](21-full-functionality-review-2026-07-13.md#post-round-25-integration-ledger-pending-final-commit-2026-07-18)** | **Current implementation and validation chronology.** Successive integration/review rounds, applied fixes, superseded findings and exact checkpoint evidence. Round 25 is pinned to `4d1218c`; the post-Round-25 certificate records the validated/published `f956685` source/UI package and the exact remaining browser/provider boundaries. |
| 20 | **[20-looplab-unified-ds-workspace-and-distributed-execution-2026-07-12.md](20-looplab-unified-ds-workspace-and-distributed-execution-2026-07-12.md)** | **Current multi-user workspace and distributed-execution analysis.** One logical LoopLab entry point with JupyterHub/SSO, experiment-linked IDE and immutable snapshot flow, tenant entitlements/priorities, autonomous GPU fill, managed or external LLMs, Kubernetes/dedicated-server/multi-domain execution, failure semantics, and gated rollout. |
| 19 | **[19-ide-integration-and-remote-development-2026-07-12.md](19-ide-integration-and-remote-development-2026-07-12.md)** | **Current IDE and remote-development analysis.** Code-grounded explanation of JupyterLab/web-VS-Code lag, embedded CodeMirror/Monaco architecture, full-functionality SSH/WSS/Teleport/Coder options, strict-no-SSH Kubernetes Attach, security boundaries, benchmark plan, and go/no-go gates. |
| 18 | **[18-ui-ux-review-2026-07-11.md](18-ui-ux-review-2026-07-11.md#37-post-checkpoint-ui-integration-reconciliation-2026-07-18)** | **Current UI/UX audit.** §36 preserves the dated `4d1218c` checkpoint; §37 records the validated/published Concepts/Atlas integration, corrected paid/CAS boundaries and remaining product gaps. The companion **[desktop Concepts review](18-desktop-concepts-ui-ux-review-2026-07-16.md)** contains the focused graph/table concept and gap analysis. |
| 17 | **[17-project-review-and-directions-2026-07-11.md](17-project-review-and-directions-2026-07-11.md#2211-post-checkpoint-integration-release-reconciliation-2026-07-18)** | **Current canonical delivery plan.** §22.11 starts from the bounded per-run concept surface plus explicit post-checkpoint lifecycle/tool deltas, then orders final integration and Research Space graduation in parallel with the unchanged Atlas G0–G6 truth gates. Start here for “what next.” |
| 16 | **[16-architecture-code-review-2026-07-11.md](16-architecture-code-review-2026-07-11.md)** | **Current finding ledger.** Reproductions and evidence for the P0/P1 blockers that determine doc 17's order. Read this for issue-level detail. |
| 0 | **[autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)** | *Research basis.* Survey + ranking of existing OSS systems (R&D-Agent, AIDE, SELA, AI-Scientist-v2, Karpathy `autoresearch`, Recursive, …) and the recommendation. Every design choice traces back here. |
| 1 | **[01-product-design.md](01-product-design.md)** | *What we're building.* Vision, goals/non-goals, users, feature groups, functional + non-functional requirements, success metrics, phased delivery. |
| 2 | **[02-architecture.md](02-architecture.md)** | *How it works.* Principles, components + interfaces, data model, control loop, search/trust mechanisms, extension points, tech stack, failure modes, provenance. |
| 3 | **[03-decisions.md](03-decisions.md)** | *Why (the hard calls).* ADR-1…5 (UI, pluggable algorithm, ingestion, tracking, graph-vs-tree); **ADR-6 — 2026 SOTA re-review** (re-prioritizes everything: operators/eval-rigor/ensembling over search machinery); **ADR-7** pluggable role backends (external coding agents); **ADR-8** prompts + AGENTS.md; **ADR-9** MCP tools + Agent Skills; **ADR-10** knowledge/memory architecture; **ADR-11** cross-cutting hardening. **Read ADR-6 first if short on time.** |
| 4 | **[04-file-layout.md](04-file-layout.md)** | *Historical on-disk design.* Data-class → format decisions, canonical-vs-derived rule, proposed run/project layout, content-addressed artifact store, and atomic-write rules. Its opening banner points to the smaller shipped contract. |
| 5 | **[05-build-decisions.md](05-build-decisions.md)** | *Historical build choices.* Per-component scorecard + **ADR-12** (orchestration/concurrency/durability/git), **ADR-13** (sandbox/isolation), **ADR-14** (structured outputs/patches), **ADR-15** (trust layer), **ADR-16** (knowledge/RAG/MCP), **ADR-17** (files/obs/plumbing), and **ADR-18** (core runtime shape). It is not a current dependency inventory. |
| 7 | **[07-architecture-review.md](07-architecture-review.md)** | *Audit (2026-06-22).* Design↔code consistency (ADR alignment), intentional deviations, the 7 bugs found & fixed, the I10 gate-semantics correction, and residual risks/recommendations. Read after 06 to see what was verified and hardened. |
| 6 | **[06-implementation-plan.md](06-implementation-plan.md)** | *Historical implementation ledger.* The 22 iterations (I0–I22) remain useful for shipped-module traceability, but its live-status and next-step claims are superseded by docs 16–18 where they conflict. Running code is in `looplab/` (see [README.md](../README.md)). |
| 10 | **[10-autoresearch-improvement-research.md](10-autoresearch-improvement-research.md)** | *Improvement research (2026-07-02).* Code-verified status + engine/planning/memory/UI gaps + the 2025–26 MLE-bench SOTA sweep, prioritized (T/P/M/U series; many items since shipped). |
| 11 | **[11-agent-systems-research.md](11-agent-systems-research.md)** | *Historical deep research (2026-07-02).* A useful research input for D1–D14; doc 17 now governs feature prerequisites and promotion criteria. |
| 12 | **[12-phased-plan-2026-07.md](12-phased-plan-2026-07.md)** | *Historical phased plan (2026-07-02).* Its six-phase ordering is superseded by doc 17's R0–R5 dependency graph where they conflict. |

---

## The system in five sentences *(post-[ADR-6](03-decisions.md))*

1. A **Researcher** (reasoning model) proposes ideas; a **Developer** implements them — each role is a **pluggable backend**: a raw LLM call *or* a complete external coding agent (OpenHands/Aider/SWE-agent/Claude Code), over **LiteLLM** (API *or* local). *(R&D-Agent per-role routing + [ADR-7](03-decisions.md) — reuse best-in-class agents, don't reimplement)*
2. The win comes from **rich operators**: draft · depth-bounded **debug** · improve · **ablation-driven targeted refinement** · **ensemble/merge** — operators beat search policy, so the default is a **greedy tree** with a multi-parent merge. *(AIRA, MLE-STAR, KompeteAI)*
3. The trust layer is **leakage-first**: train/test+temporal+target **leakage detection** + **consistent evaluation** + tiered variance gating (robust CV everywhere, multi-seed only at the frontier) — the +9–15 pt lever. *(AIRA, MLE-STAR)*
4. Given prior artifacts, a **lightweight grounding pre-phase** (retrieve-and-seed + data profiling) sets up the loop against an immutable goal anchor. *([ADR-3](03-decisions.md))*
5. State lives in **human-readable files**: `events.jsonl` is authoritative for replayable `RunState`, while task/config, tracing, chat and cross-run stores keep explicit sidecar contracts. One live engine is fenced by `engine.lock`; server control events use the event store's serialized append path. The UI reads these projections and submits durable control intents; **MLflow is an optional exporter**, not the core. *([ADR-1](03-decisions.md), [ADR-4](03-decisions.md)/[ADR-6](03-decisions.md), [04](04-file-layout.md))*

## Top recommendation (from the research)

To **learn the loop**, read **Karpathy `autoresearch`** then fork **AIDE**; **R&D-Agent** is the most capable validated OSS engine (per-role routing). **But raw-results SOTA has moved to ~60–70% on MLE-bench** driven by frontier base models + the techniques in [ADR-6](03-decisions.md). The *architecture to build toward* (this doc set) = **AIDE-style greedy tree + AIRA-class operators + MLE-STAR ablation-refinement/ensembling + leakage-safe consistent evaluation + R&D-Agent per-role routing + a reproducible event-log spine** — a combination no single OSS system ships. See [the exploration doc](autoresearch-systems-exploration.md) (with its 2026 update box) and [ADR-6](03-decisions.md).

---

## Conventions across the docs

- **ADR-N** = a decision record: ADR-1…11 in [03-decisions.md](03-decisions.md), **ADR-12…18 in [05-build-decisions.md](05-build-decisions.md)** (concrete libraries + core runtime shape). **§N** = a section in [02-architecture.md](02-architecture.md).
- Evidence tags: **[IND]** independent · **[SR]** self-reported · **[BENCH]** standardized benchmark.
- File-class labels (doc 04): **[HC]** human-canonical · **[MA]** machine-append-only · **[BIN]** large-binary-artifact · **[DUI]** derived-UI-projection (regenerable).
- All benchmark numbers are **time-sensitive and vendor-reported** unless tagged [IND] — see the exploration doc's caveats.

> **Note:** docs 01–05 are the original *design* documents. For current implementation risk and
> sequencing, use [doc 16](16-architecture-code-review-2026-07-11.md) and
> [doc 17](17-project-review-and-directions-2026-07-11.md); for UI/UX findings and acceptance criteria,
> use [doc 18](18-ui-ux-review-2026-07-11.md); for IDE integration, JupyterHub performance constraints,
> and secure remote-workspace choices, use
> [doc 19](19-ide-integration-and-remote-development-2026-07-12.md). Current code is the runtime source of truth;
> `docs/guide/` describes intended use, while verified discrepancies in docs 16–21 take precedence. Use only
> the latest explicitly superseding ledger in doc 21 for current status: the post-Round-25 integration ledger,
> paired with doc 17 §22.11 and doc 18 §37. Round 25 remains the dated `4d1218c` checkpoint.
