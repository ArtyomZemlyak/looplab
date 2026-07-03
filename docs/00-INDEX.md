# LoopLab — Documentation Index

**Project:** LoopLab — an open, backend-flexible **autonomous ML/DS research engine** (an LLM agent that invents → implements → tests → improves ML solutions in a loop, returning the best *verified* result).
**Status:** design v0.1 · **Date:** 2026-06-20 · **Validated/consistency-checked:** 2026-06-20

> 📖 **Looking for how to *use* LoopLab?** This index covers the *design* (the why). For practical,
> task-oriented documentation — install, quickstart, CLI, configuration, tasks — see the
> **[User Guide](guide/index.md)** and the [README](../README.md).

---

## Read in this order

| # | Doc | What it answers |
|---|-----|-----------------|
| 0 | **[autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)** | *Research basis.* Survey + ranking of existing OSS systems (R&D-Agent, AIDE, SELA, AI-Scientist-v2, Karpathy `autoresearch`, Recursive, …) and the recommendation. Every design choice traces back here. |
| 1 | **[01-product-design.md](01-product-design.md)** | *What we're building.* Vision, goals/non-goals, users, feature groups, functional + non-functional requirements, success metrics, phased delivery. |
| 2 | **[02-architecture.md](02-architecture.md)** | *How it works.* Principles, components + interfaces, data model, control loop, search/trust mechanisms, extension points, tech stack, failure modes, provenance. |
| 3 | **[03-decisions.md](03-decisions.md)** | *Why (the hard calls).* ADR-1…5 (UI, pluggable algorithm, ingestion, tracking, graph-vs-tree); **ADR-6 — 2026 SOTA re-review** (re-prioritizes everything: operators/eval-rigor/ensembling over search machinery); **ADR-7** pluggable role backends (external coding agents); **ADR-8** prompts + AGENTS.md; **ADR-9** MCP tools + Agent Skills; **ADR-10** knowledge/memory architecture; **ADR-11** cross-cutting hardening. **Read ADR-6 first if short on time.** |
| 4 | **[04-file-layout.md](04-file-layout.md)** | *On-disk contract.* Data-class → format decisions, the canonical-vs-derived rule, full run/project directory layout, content-addressed artifact store, atomic-write rules. |
| 5 | **[05-build-decisions.md](05-build-decisions.md)** | *With-what (concrete libs/frameworks).* Per-component concreteness scorecard + **ADR-12** (orchestration/concurrency/durability/git), **ADR-13** (sandbox/isolation), **ADR-14** (structured outputs/patches), **ADR-15** (trust layer), **ADR-16** (knowledge/RAG/MCP), **ADR-17** (files/obs/plumbing), **ADR-18** (core runtime shape — library+CLI process, hand-rolled engine, no agent framework) + the buildability validation. **Read this when moving from design to implementation.** |
| 7 | **[07-architecture-review.md](07-architecture-review.md)** | *Audit (2026-06-22).* Design↔code consistency (ADR alignment), intentional deviations, the 7 bugs found & fixed, the I10 gate-semantics correction, and residual risks/recommendations. Read after 06 to see what was verified and hardened. |
| 6 | **[06-implementation-plan.md](06-implementation-plan.md)** | *Status board / ToDo (what's built vs left).* The 22 iterations (I0–I22) across P0–P4 as a **checklist with live status** — ✅ done / 🟡 partial / ⬜ todo / 🧱 infra-seam — each pointing at its module + test. Has a snapshot table and a recommended next-steps order. **Read this to see where the implementation actually stands.** Running code is in `LoopLab/` (see [README.md](README.md)). |
| 10 | **[10-autoresearch-improvement-research.md](10-autoresearch-improvement-research.md)** | *Improvement research (2026-07-02).* Code-verified status + engine/planning/memory/UI gaps + the 2025–26 MLE-bench SOTA sweep, prioritized (T/P/M/U series; many items since shipped). |
| 11 | **[11-agent-systems-research.md](11-agent-systems-research.md)** | *Deep research: agentic / deep-research / AI-R&D systems (2026-07-02).* Fresh 3-stream verified web sweep (deep-research architectures, agentic best practices, 2026 Q1–Q2 AI-R&D frontier incl. Arbor/FML-bench/ShinkaEvolve/co-evolving evaluators/misevolution) × a code-level gap analysis → prioritized directions D1–D14. **Read this for the current improvement frontier.** |
| 12 | **[12-phased-plan-2026-07.md](12-phased-plan-2026-07.md)** | *Phased implementation plan (2026-07-02).* D1–D14 + the open backlog sequenced into 6 phases (risk retirement → selection integrity → search/novelty → memory depth → verified research/evaluator hardening → scale & proof), with per-item seams, efforts, dependency graph, and exit criteria. **The actionable "do next" order.** |

---

## The system in five sentences *(post-[ADR-6](03-decisions.md))*

1. A **Researcher** (reasoning model) proposes ideas; a **Developer** implements them — each role is a **pluggable backend**: a raw LLM call *or* a complete external coding agent (OpenHands/Aider/SWE-agent/Claude Code), over **LiteLLM** (API *or* local). *(R&D-Agent per-role routing + [ADR-7](03-decisions.md) — reuse best-in-class agents, don't reimplement)*
2. The win comes from **rich operators**: draft · depth-bounded **debug** · improve · **ablation-driven targeted refinement** · **ensemble/merge** — operators beat search policy, so the default is a **greedy tree** with a multi-parent merge. *(AIRA, MLE-STAR, KompeteAI)*
3. The trust layer is **leakage-first**: train/test+temporal+target **leakage detection** + **consistent evaluation** + tiered variance gating (robust CV everywhere, multi-seed only at the frontier) — the +9–15 pt lever. *(AIRA, MLE-STAR)*
4. Given prior artifacts, a **lightweight grounding pre-phase** (retrieve-and-seed + data profiling) sets up the loop against an immutable goal anchor. *([ADR-3](03-decisions.md))*
5. State lives in **human-readable files** (event log = source of truth; engine is sole writer); a UI reads files (**static HTML tree first, TUI/web later**); **MLflow is an optional exporter**, not the core. *([ADR-1](03-decisions.md), [ADR-4](03-decisions.md)/[ADR-6](03-decisions.md), [04](04-file-layout.md))*

## Top recommendation (from the research)

To **learn the loop**, read **Karpathy `autoresearch`** then fork **AIDE**; **R&D-Agent** is the most capable validated OSS engine (per-role routing). **But raw-results SOTA has moved to ~60–70% on MLE-bench** driven by frontier base models + the techniques in [ADR-6](03-decisions.md). The *architecture to build toward* (this doc set) = **AIDE-style greedy tree + AIRA-class operators + MLE-STAR ablation-refinement/ensembling + leakage-safe consistent evaluation + R&D-Agent per-role routing + a reproducible event-log spine** — a combination no single OSS system ships. See [the exploration doc](autoresearch-systems-exploration.md) (with its 2026 update box) and [ADR-6](03-decisions.md).

---

## Conventions across the docs

- **ADR-N** = a decision record: ADR-1…11 in [03-decisions.md](03-decisions.md), **ADR-12…18 in [05-build-decisions.md](05-build-decisions.md)** (concrete libraries + core runtime shape). **§N** = a section in [02-architecture.md](02-architecture.md).
- Evidence tags: **[IND]** independent · **[SR]** self-reported · **[BENCH]** standardized benchmark.
- File-class labels (doc 04): **[HC]** human-canonical · **[MA]** machine-append-only · **[BIN]** large-binary-artifact · **[DUI]** derived-UI-projection (regenerable).
- All benchmark numbers are **time-sensitive and vendor-reported** unless tagged [IND] — see the exploration doc's caveats.

> **Note:** these are design documents, not yet an implementation. Phased build order is in [01 §10](01-product-design.md) / [02 §17](02-architecture.md) (kept in sync). A natural next step is a P0 repo skeleton + JSON Schemas for the file contract.
