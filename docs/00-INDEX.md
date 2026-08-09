# LoopLab — Documentation Index

**Project:** LoopLab — an open, backend-flexible **autonomous ML/DS research engine** (an LLM agent that invents → implements → tests → improves ML solutions in a loop, returning the best *verified* result).
**Status:** current documentation authority map · **Created:** 2026-06-20 ·
**Validated/consistency-checked:** 2026-08-09 · **Runtime authority:** current `master` source and tests

> 📖 **Looking for how to *use* LoopLab?** This index covers the design and historical review record.
> For practical install, quickstart, CLI, configuration and task documentation, use the
> **[User Guide](guide/index.md)** and [README](https://github.com/ArtyomZemlyak/looplab/blob/master/README.md).

> **Authority rule (2026-08-08).** Current source and tests decide runtime behavior; README and
> `docs/guide/` are the maintained user contract. [Doc 27](27-agent-system-mega-review-2026-08-09.md)
> is the current dated agent-system/SOTA review. [Doc 24](24-ui-phase3-validation.md) is the current
> Phase-3 UI acceptance contract, while [doc 25](25-architecture-modularity-review-2026-08-01.md) is a
> dated 188-finding architecture baseline whose per-finding status was reconciled against current
> `master`. All other numbered documents are point-in-time designs, research, audits or chronology
> unless a section explicitly says otherwise. A historical “current,” “canonical,” checkmark or open
> item never outranks newer code/tests or prove that work still has the same status.

---

## Read in this order

| # | Doc | Authority and scope |
|---|-----|---------------------|
| 27 | **[27-agent-system-mega-review-2026-08-09.md](27-agent-system-mega-review-2026-08-09.md)** | Current dated production-agent/capability inventory, business-contract audit, compatible fixes and primary-source SOTA comparison. Its forward architecture is a recommendation; shipped behavior remains source/tests/guide. |
| 26 | **[26-ouroboros-airi-analysis-2026-08-02.md](26-ouroboros-airi-analysis-2026-08-02.md)** | Dated external-works analysis of Ouroboros/AIRI. Research input; it does not flip defaults or prove integration. |
| 25 | **[25-architecture-modularity-review-2026-08-01.md](25-architecture-modularity-review-2026-08-01.md)** | 188-finding structural-debt baseline at `756ad13`, with per-finding dispositions reconciled through current `master` on 2026-08-08: **147 resolved, 37 partial, 2 deferred, 2 open**. The baseline evidence remains historical; the explicit status on each finding is current. |
| 24 | **[24-ui-phase3-validation.md](24-ui-phase3-validation.md)** | Current Phase-3 UI product and automated-acceptance contract, including 2026-08-08 bundle measurements. Browser-matrix and moderated-usability gates remain separately unevidenced until artifacts exist. |
| 23 | **[23-hypothesis-card-kanban-2026-07-20.md](23-hypothesis-card-kanban-2026-07-20.md)** | Historical Card/Kanban design and implementation ledger, with visible later corrections for current selector reachability and the public compatibility projection. |
| 22 | **[22-agent-parallelism-2026-07-19.md](22-agent-parallelism-2026-07-19.md)** | Implemented/historical parallel-build plan. The shipped shape is bulk-synchronous build fan-out; Phase-4 two-wide golden verification remains partial. |
| 21 | **[21-full-functionality-review-2026-07-13.md](21-full-functionality-review-2026-07-13.md)** | Historical branch/integration chronology. Each round speaks only for its named checkpoint. |
| 20 | **[20-looplab-unified-ds-workspace-and-distributed-execution-2026-07-12.md](20-looplab-unified-ds-workspace-and-distributed-execution-2026-07-12.md)** | Dated multi-user workspace/distributed-execution options analysis; not a claim that the proposed platform ships. |
| 19 | **[19-ide-integration-and-remote-development-2026-07-12.md](19-ide-integration-and-remote-development-2026-07-12.md)** | Dated IDE/remote-development options and security criteria; not an implementation inventory. |
| 18A | **[18-ui-ux-review-2026-07-11.md](18-ui-ux-review-2026-07-11.md)** | Historical UI/UX audit and acceptance record through its named 2026-07 checkpoints. Current UI source/tests, guide and doc 24 win on status. |
| 18B | **[18-desktop-concepts-ui-ux-review-2026-07-16.md](18-desktop-concepts-ui-ux-review-2026-07-16.md)** | Focused, dated desktop Concepts graph/table review; companion to 18A. The duplicate number is retained to preserve existing links. |
| 17 | **[17-project-review-and-directions-2026-07-11.md](17-project-review-and-directions-2026-07-11.md)** | Historical canonical delivery plan at its checkpoint, not current runtime authority. Its dependency/gate rationale remains useful. |
| 16 | **[16-architecture-code-review-2026-07-11.md](16-architecture-code-review-2026-07-11.md)** | Dated finding/reproduction ledger. Reproduce an item against current code before treating it as open. |
| 15 | **[15-mega-refactor-review-2026-07-10.md](15-mega-refactor-review-2026-07-10.md)** | Historical mega-refactor plan and baseline; later code/doc 25 supersede present-tense status. |
| 14 | **[14-agent-framework-mega-review-2026-07-10.md](14-agent-framework-mega-review-2026-07-10.md)** | Historical agent-framework review; findings are pinned to its tree. |
| 13 | **[13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md)** | Point-in-time external-works research and recommendations, not a live feature ledger. |
| 12 | **[12-phased-plan-2026-07.md](12-phased-plan-2026-07.md)** | Historical feature-branch plan; its implementation/default claims do not describe current `master`. |
| 11 | **[11-agent-systems-research.md](11-agent-systems-research.md)** | Historical deep-research input for D1–D14. |
| 10 | **[10-autoresearch-improvement-research.md](10-autoresearch-improvement-research.md)** | Historical improvement research and gap inventory, pinned to 2026-07-02. |
| 09 | — | No document was allocated this number; the gap is intentional and retained for stable numbering. |
| 08 | **[08-tracing-architecture.md](08-tracing-architecture.md)** | Accepted tracing ADR. Current `core/tracing.py`, tests and deployment guide decide exact exporter behavior. |
| 07 | **[07-architecture-review.md](07-architecture-review.md)** | Point-in-time 2026-06-22 architecture audit. |
| 06 | **[06-implementation-plan.md](06-implementation-plan.md)** | Historical I0–I22 implementation ledger with a 2026-08-08 reconciliation of the formerly missing major surfaces. |
| 05 | **[05-build-decisions.md](05-build-decisions.md)** | Historical build choices/ADRs 12–18; not a current dependency inventory. |
| 04 | **[04-file-layout.md](04-file-layout.md)** | Historical on-disk design; its opening correction describes the smaller shipped authority contract. |
| 03 | **[03-decisions.md](03-decisions.md)** | Historical ADRs 1–11 plus visible current-runtime corrections. |
| 02 | **[02-architecture.md](02-architecture.md)** | Original target architecture, not a byte-accurate implementation inventory; its reconciliation banner wins over target prose. |
| 01 | **[01-product-design.md](01-product-design.md)** | Original product/design target, with a current shipped-boundary banner. |
| 00 | **[autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)** | Research basis and survey that informed the original design. |

---

## The system in five sentences *(post-[ADR-6](03-decisions.md))*

1. A **Researcher** proposes ideas and a **Developer** implements them. Shipped in-house roles use an OpenAI-compatible `/v1` client; the Developer can instead use an external coding-agent CLI. `LiteLLMClient` is optional and not selected by Settings. *(R&D-Agent per-role routing + [ADR-7](03-decisions.md))*
2. The win comes from **rich operators**: draft · depth-bounded **debug** · improve · **ablation-driven targeted refinement** · **ensemble/merge** — operators beat search policy, so the default is a **greedy tree** with a multi-parent merge. *(AIRA, MLE-STAR, KompeteAI)*
3. The trust layer combines adapter-owned evaluation, optional audit/gate/block signals, holdout and confirmation paths. Exact train/test-row leakage is checked at setup only for adapters that expose `leakage_inputs()`; temporal/target detector utilities are not yet wired through a real adapter. *(AIRA, MLE-STAR)*
4. Genesis/task snapshots, data profiling and bounded memory/knowledge retrieval ground the loop before and during proposals; their exact availability is task- and setting-dependent. *([ADR-3](03-decisions.md))*
5. State lives in **human-readable files**: `events.jsonl` is authoritative for replayable `RunState`, while task/config, tracing, chat and cross-run stores keep explicit sidecar contracts. One live engine is fenced by `engine.lock`; server control events use the event store's serialized append path. The UI reads these projections and submits durable control intents; **MLflow is an optional exporter**, not the core. *([ADR-1](03-decisions.md), [ADR-4](03-decisions.md)/[ADR-6](03-decisions.md), [04](04-file-layout.md))*

## Top recommendation (from the research)

To **learn the loop**, read **Karpathy `autoresearch`** then fork **AIDE**; **R&D-Agent** is the most capable validated OSS engine (per-role routing). **But raw-results SOTA has moved to ~60–70% on MLE-bench** driven by frontier base models + the techniques in [ADR-6](03-decisions.md). The *architecture to build toward* (this doc set) = **AIDE-style greedy tree + AIRA-class operators + MLE-STAR ablation-refinement/ensembling + leakage-safe consistent evaluation + R&D-Agent per-role routing + a reproducible event-log spine** — a combination no single OSS system ships. See [the exploration doc](autoresearch-systems-exploration.md) (with its 2026 update box) and [ADR-6](03-decisions.md).

---

## Conventions across the docs

- **ADR-N** = a decision record: ADR-1…11 in [03-decisions.md](03-decisions.md), **ADR-12…18 in [05-build-decisions.md](05-build-decisions.md)** (concrete libraries + core runtime shape). **§N** = a section in [02-architecture.md](02-architecture.md).
- Evidence tags: **[IND]** independent · **[SR]** self-reported · **[BENCH]** standardized benchmark.
- File-class labels (doc 04): **[HC]** human-canonical · **[MA]** machine-append-only · **[BIN]** large-binary-artifact · **[DUI]** derived-UI-projection (regenerable).
- All benchmark numbers are **time-sensitive and vendor-reported** unless tagged [IND] — see the exploration doc's caveats.

> **Reading rule:** design, research and review documents preserve why and historical evidence. They do
> not become current merely because they say “current” internally. For shipped behavior use current source,
> tests, README and `docs/guide/`; for Phase-3 UI acceptance use doc 24; for the reconciled architecture
> finding ledger use the explicit per-finding statuses in doc 25.
