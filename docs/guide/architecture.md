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
Score, …); the **hypothesis kanban**, **cross-run memory** (write → hygiene → the five tiers) and the
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
    child). The **hypothesis board** is *derived on every fold* — beliefs are deduped (exact hash +
    an agentic paraphrase merge), prioritized (foresight), and tracked to a verdict. The base
    cross-run memory paths and reflection priors are **on by default** (`~/.looplab/memory` +
    `~/.looplab/knowledge`); Part-IV concept, advisory and structured-claim reads remain opt-in.
    A run with an eligible best result can write a case and reflection artifacts, while only a
    supported improving hypothesis can seed an auto-skill. Later matching runs may retrieve the
    applicable records. A model-authored meta-note is an explanatory hypothesis over recorded
    observations, not causal proof.

## Where each piece lives in the code

| Concept | Module |
|---|---|
| Control loop + crash-resume | `engine/orchestrator.py` |
| Append-only log · pure fold · SQLite read-model | `events/eventstore.py`, `events/replay.py`, `events/readmodel.py` |
| Researcher / Developer / unified agent | `agents/roles.py`, `agents/unified_agent.py` |
| Foresight (hypothesis prioritization, predict-before-execute) | `search/foresight.py` |
| Hybrid retrieval + agent-decided merge (lessons & hypothesis board) | `search/hybrid_merge.py` |
| Search policies · operators | `search/policy.py`, `search/operators.py` |
| Repo Developer: env-inspector + auto-validate | `tools/env_inspect.py`, `adapters/repo_write_tools.py` (re-exported via `repo_developer.py`) |
| Sandbox seam (subprocess / Docker) | `runtime/sandbox.py` |
| Variance gate · multi-seed confirmation · CV · leakage · reward-hack | `trust/gate.py`, `trust/confirm.py`, `trust/cv.py`, `trust/leakage.py`, `trust/reward_hack.py` |
| Cross-run memory · retrieval · harmonic index | `engine/memory.py`, `engine/lessons.py`, `tools/memora.py` |
| Trace span exporter | `core/tracing.py` |

For the narrative behind each box, read **[Concepts](concepts.md)**; for the full design rationale and
decision records, see the **[Architecture spec](../02-architecture.md)** and the
**[Design records index](../00-INDEX.md)**.
