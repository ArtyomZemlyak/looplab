---
hide:
  - toc
---

# Architecture at a glance

Two views of the **whole engine**: a **high-level one-pager** for the mental model, and the
**full process diagram** — every stage, agent, memory tier and trust gate with the exact numbers,
cadences and thresholds that ship **enabled by default**. Both are kept in sync with the code (the
diagram's numbers are verified against `looplab/`).

## The one-pager

Three planes and their connections. **Magenta = where the LLM / agent is invoked** (Genesis,
Strategist, Researcher, Developer, Critic, Reflector); the **engine** plane is deterministic
(select · execute · gate · log); and the **stores** — Search, Memory, Knowledge — feed the loop,
all over the append-only `events.jsonl` spine.

[![LoopLab architecture — one-pager schema](../infographic/architecture-one-pager.svg)](../infographic/architecture-one-pager.svg)

## The full process diagram

A boxes-and-arrows flowchart of one turn of the engine and everything around it. Read the top row
left→right: **Propose → Novelty gate → Implement → Evaluate → Score · Trust → Refine**, then loop.
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
    an agentic paraphrase merge), prioritized (foresight), and tracked to a verdict. Cross-run memory
    is **on by default** (`~/.looplab/memory` + `~/.looplab/knowledge`): a run distills lessons,
    a KB case, a causal meta-note and auto-skills, then the next similar run reads them back.

## Where each piece lives in the code

| Concept | Module |
|---|---|
| Control loop + crash-resume | `engine/orchestrator.py` |
| Append-only log · pure fold · SQLite read-model | `events/eventstore.py`, `events/replay.py`, `events/readmodel.py` |
| Researcher / Developer / unified agent | `agents/roles.py`, `agents/unified_agent.py` |
| Foresight (hypothesis prioritization, predict-before-execute) | `search/foresight.py` |
| Hybrid retrieval + agent-decided merge (lessons & hypothesis board) | `search/hybrid_merge.py` |
| Search policies · operators | `search/policy.py`, `search/operators.py` |
| Repo Developer: env-inspector + auto-validate | `tools/env_inspect.py`, `adapters/repo_developer.py` |
| Sandbox seam (subprocess / Docker) | `runtime/sandbox.py` |
| Variance gate · multi-seed confirmation · CV · leakage · reward-hack | `trust/gate.py`, `trust/confirm.py`, `trust/cv.py`, `trust/leakage.py`, `trust/reward_hack.py` |
| Cross-run memory · retrieval · harmonic index | `engine/memory.py`, `engine/lessons.py`, `tools/memora.py` |
| Trace span exporter | `core/tracing.py` |

For the narrative behind each box, read **[Concepts](concepts.md)**; for the full design rationale and
decision records, see the **[Architecture spec](../02-architecture.md)** and the
**[Design records index](../00-INDEX.md)**.
