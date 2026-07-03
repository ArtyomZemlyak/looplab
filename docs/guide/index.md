# LoopLab — User Guide

LoopLab is an **autonomous ML/DS research engine**. You give it a goal; it runs a closed loop —
**invent → implement → test → improve → merge** — and returns the best *verified* result. Every
step is appended to an event log that is the single source of truth, so runs are reproducible and
crash-resumable by replay.

This guide is the practical, how-to-use documentation. For the design rationale (architecture,
decision records, roadmap), see [`../00-INDEX.md`](../00-INDEX.md).

## Start here

| Guide | What it covers |
|---|---|
| **[Installation](installation.md)** | Requirements, install extras, optional backends |
| **[Quickstart](quickstart.md)** | Your first run, offline → LLM-driven, reading results |

## Reference

| Guide | What it covers |
|---|---|
| **[CLI reference](cli-reference.md)** | Every command (`run`, `resume`, `replay`, `inspect`, `smoke`, `approve`, `bench`, `ui`, `export-*`) and its options |
| **[Configuration](configuration.md)** | Every `LOOPLAB_*` setting, grouped by topic, with defaults |
| **[Tasks](tasks.md)** | All nine task kinds and their JSON fields |
| **[Generating train & test code](generating-code.md)** | Every "let the agent write the code" case + how to point at your data |

## How it works

| Guide | What it covers |
|---|---|
| **[Concepts](concepts.md)** | Event log & replay, sandbox & trust tiers, operators, gates, confirmation, cross-run memory, search policies |
| **[Memory & knowledge](memory.md)** | Every memory type (cases, lessons, meta-notes, skills, KB, hypotheses, research), what each is for, the methodologies, and agentic retrieval |
| **[LLM & coding agents](llm-and-agents.md)** | OpenAI-compatible backends, external coding agents, per-role models, reasoning, knowledge & skills |

## Operating it

| Guide | What it covers |
|---|---|
| **[Web UI](ui.md)** | The live React control plane |
| **[Deployment](deployment.md)** | Docker Compose stack, the untrusted sandbox tier |
| **[MLE-bench runbook](../MLEBENCH.md)** | Running real Kaggle competitions end-to-end |
| **[Live scenarios](live-scenarios.md)** | Situational end-to-end tests of the main features (stagnation, novelty, trust gate, repair, …) — a returnable collection |

## The loop in one picture

```
            ┌──────────────────────────────────────────────────────────┐
            │                       Orchestrator                        │
            │   (anyio control loop · sole writer of the event log)     │
            └──────────────────────────────────────────────────────────┘
                 │            │             │             │
            ┌────▼────┐  ┌────▼─────┐  ┌────▼────┐   ┌────▼─────┐
            │Researcher│ │Developer │  │ Sandbox │   │ Evaluator│
            │ proposes │ │ writes   │  │  runs   │   │  scores  │
            │  ideas   │ │  code    │  │  code   │   │ (CV/gate)│
            └────┬────┘  └────┬─────┘  └────┬────┘   └────┬─────┘
                 │            │             │             │
                 └────────────┴──────┬──────┴─────────────┘
                                     ▼
                          append to events.jsonl
                       (source of truth · replayable)
                                     │
                  policy picks the next node → repeat
                                     │
                       confirm top-k → champion
```

Read the [Concepts](concepts.md) guide for what each box does and why the event log is the spine.
