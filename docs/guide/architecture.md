---
hide:
  - toc
---

# Architecture at a glance

Two **capability maps** of the engine: a **high-level one-pager** for the mental model, and a
**detailed process diagram** covering the main stages, agents, memory tiers and trust controls.
They are navigation aids, not an exhaustive executable specification. Where shown, status labels
distinguish shipped defaults from opt-in and off-by-default capabilities; the configuration and
event contracts remain the authority for a particular run.

## The one-pager

Three planes and their connections. **Magenta = where the LLM / agent is invoked** (Genesis,
Strategist, Researcher, Developer, Critic, Reflector); the **engine** plane owns deterministic
selection/folding around the append-only `events.jsonl` spine; and the **stores** — Search, Memory,
Knowledge — feed the loop through explicit event, snapshot and sidecar contracts. The event log is
authoritative for replayable `RunState`, not for every value shown in the product.

[![LoopLab architecture — one-pager schema](../infographic/architecture-one-pager.svg)](../infographic/architecture-one-pager.svg)

## The full process diagram

A boxes-and-arrows flowchart of one turn of the engine and its main adjacent systems. Read the top row
left→right: **Propose → Novelty stage → Implement → Evaluate → Score · Trust → Refine**, then loop.
Under each stage sit its detail boxes (the memory funnel under Propose, the trust/confirm stack under
Score, …); the **Card lifecycle board** (1 card = 1 hypothesis), **cross-run memory**
(write → hygiene → the five tiers) and the
**event spine** hang below. Colour = which agent acts.

[:material-open-in-new: Open the diagram full-screen](../infographic/agent-architecture.html){ .md-button .md-button--primary .ll-open target="_blank" }

<div class="ll-frame">
  <iframe src="../../infographic/agent-architecture.html"
          title="LoopLab full process diagram"
          loading="lazy"></iframe>
</div>

!!! note "How to read it"

    **Solid teal arrows** are the main loop; **thin dashed arrows** are feedback / memory reads &
    writes. Two edges break the circle: a **repair ↺** loop (a crash/timeout is fed back with its
    stderr, fixed in place) and a **merge** branch (two strong lineages fused into one multi-parent
    child). The **research board** (cards; 1 card = 1 hypothesis) is *derived on every fold* — beliefs
    are deduped (exact hash + an agentic paraphrase merge), prioritized (foresight), and tracked to a
    verdict. The base
    cross-run memory paths and reflection priors are **on by default** (`~/.looplab/memory` +
    `~/.looplab/knowledge`). Product `Settings` also enable the Part-IV concept, advisory and
    structured-claim reads by default; only callers that construct bare `EngineOptions` directly
    retain the lower-level opt-in defaults.
    A run with an eligible best result can write a case and reflection artifacts, while only a
    supported improving hypothesis can seed an auto-skill. Later matching runs may retrieve the
    applicable records. A model-authored meta-note is an explanatory hypothesis over recorded
    observations, not causal proof.

    Parallel work has two independent canonical ceilings: `eval_parallel` admits experiments, while
    `llm_parallel` governs provider calls and build fan-out. A run-local broker further divides the
    LLM total among `build`, `deep_research`, `novelty_dedup`, `enrichment`, and the fail-safe `engine`
    lane. The Strategist can durably reallocate both totals and that lane map; operator pins win.

    GPU packing is concurrent inside one Run. Separate local Engine processes that share an OS-user
    filesystem namespace conservatively serialize GPU ownership through one crash-released pool lease;
    this avoids treating ordinal, GPU-UUID, and MIG aliases as different hardware. Different OS users,
    containers, or hosts do not share that lease and require an external scheduler.

## Where each piece lives in the code

| Concept | Module |
|---|---|
| Control loop + crash-resume | `engine/orchestrator.py` |
| Append-only log · pure fold · SQLite read-model | `events/eventstore.py`, `events/replay.py`, `events/readmodel.py` |
| Researcher / Developer / unified agent | `agents/roles.py`, `agents/unified_agent.py` |
| Canonical eval/LLM concurrency + named-lane broker | `engine/orchestrator.py`, `core/llm_broker.py`, `engine/strategy.py` |
| Card model · replay/public projection · selection | `core/models.py`, `events/replay.py`, `serve/public_cards.py`, `search/card_selection.py` |
| Resource admission · GPU lifecycle reservations | `engine/resources.py`, `core/hardware.py` |
| Speculative Card producer/consumer · freshness/quality gates | `engine/speculation.py`, `search/speculation_quality.py`, `search/speculation_calibration.py` |
| Foresight (belief-card prioritization, predict-before-execute) | `search/foresight.py` |
| Hybrid retrieval + agent-decided merge (lessons & Card belief board) | `search/hybrid_merge.py` |
| Search policies · operators | `search/policy.py`, `search/operators.py` |
| Part IV/V concept materialization · graph · bounded frame | `core/concepts.py`, `search/concept_projection.py`, `search/concept_graph.py`, `serve/concept_frame.py` |
| Repo Developer: env-inspector + auto-validate | `tools/env_inspect.py`, `adapters/repo_write_tools.py` (re-exported via `repo_developer.py`) |
| Sandbox seam (subprocess / Docker) · built-in eval watchdogs (loss/grad divergence · stall) | `runtime/sandbox.py` |
| Training-log monitor (product `Settings`: watcher on; bare `EngineOptions`: off; verdict advisory, early-kill separately opt-in) | `engine/train_monitor.py` |
| Variance gate · multi-seed confirmation · CV · leakage · reward-hack | `trust/gate.py`, `trust/confirm.py`, `trust/cv.py`, `trust/leakage.py`, `trust/reward_hack.py` |
| Cross-run memory · retrieval · harmonic index | `engine/memory.py`, `engine/lessons.py`, `tools/memora.py` |
| Cross-run index · claims · taxonomy/claim governance | `engine/cross_run_index.py`, `engine/claims.py`, `engine/concept_registry.py`, `engine/governance_health.py` |
| Paid proposal steward lifecycle | `engine/steward_invocation.py`, `engine/concept_steward.py`, `engine/claim_steward.py`, `engine/task_facets.py` |
| Research Atlas / owner governance API · UI | `serve/routers/cross_run.py`, `ui/src/ResearchAtlas.jsx`, `ui/src/researchAtlasModel.js` |
| Trace span exporter | `core/tracing.py` |

For the narrative behind each box, read **[Concepts](concepts.md)**; for the full design rationale and
decision records, see the **[Architecture spec](../02-architecture.md)** and the
**[Design records index](../00-INDEX.md)**.
