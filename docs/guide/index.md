# LoopLab — User Guide

LoopLab evaluates ML experiments and returns the best measured result. Most people start by
describing a goal to **Assistant in the web UI**. A coding agent can instead propose candidates
through the external harness while LoopLab runs and scores them.

This guide is the practical, how-to-use documentation. For the design rationale (architecture,
decision records, roadmap), see [`../00-INDEX.md`](../00-INDEX.md).

## Start here

| Guide | What it covers |
|---|---|
| **[Assistant quickstart](quickstart.md#assistant-in-the-web-ui)** | Set up the UI, describe a goal, review and start the run, read the result |
| **[External-agent quickstart](external-harness.md#first-external-run)** | Let Codex or Claude Code propose candidates; LoopLab evaluates them |
| **[Installation](installation.md)** | Requirements, source install on Windows/POSIX, optional extras |
| **[Offline CLI walkthrough](cli-walkthrough.md)** | `looplab run examples/demo.yaml`: the engine working without a model |
| **[Web UI](ui.md)** | Assistant, the run workspace, Report, and how results are compared |
| **[JupyterHub onboarding](jupyterhub-onboarding.md)** | Setup inside a hub single-user server, then work through Assistant |

## Reference

| Guide | What it covers |
|---|---|
| **[CLI reference](cli-reference.md)** | Every command and its options; `looplab --help` groups them, everyday commands first |
| **[Configuration](configuration.md)** | Every `LOOPLAB_*` setting, grouped by topic, with defaults |
| **[HTTP API reference](api-reference.md)** | Every route of `looplab ui`'s server, generated from its own OpenAPI schema and pinned by a test |
| **[Tasks](tasks.md)** | All nine task kinds and their JSON fields |
| **[Generating train & test code](generating-code.md)** | Every "let the agent write the code" case + how to point at your data |

## How it works

| Guide | What it covers |
|---|---|
| **[Concepts](concepts.md)** | Event log & replay, sandbox & trust tiers, operators, gates, confirmation, cross-run memory, search policies |
| **[Memory & knowledge](memory.md)** | Every memory type (cases, lessons, meta-notes, skills, KB, belief cards, research), what each is for, the methodologies, and agentic retrieval |
| **[LLM & coding agents](llm-and-agents.md)** | OpenAI-compatible backends, external coding agents, per-role models, reasoning, knowledge & skills |
| **[External harness reference](external-harness.md)** | Agent capabilities, obligations, reconnect, and the separate delegated Developer mode |

## Operating it

| Guide | What it covers |
|---|---|
| **[Deployment](deployment.md)** | Docker Compose stack, the untrusted sandbox tier |
| **[MLE-bench runbook](../MLEBENCH.md)** | Running real Kaggle competitions end-to-end |
| **[Live scenarios](live-scenarios.md)** | Situational end-to-end tests of the main features (stagnation, novelty, trust gate, repair, …) — a returnable collection |

## The loop in one picture

```
            ┌──────────────────────────────────────────────────────────┐
            │                       Orchestrator                        │
            │   (anyio control loop · one live engine per run)          │
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
                       (RunState authority · serialized)
                                     │
             Card queue → agent pilot → policy fallback → repeat
                                     │
                       confirm top-k → champion
```

Read the [Concepts](concepts.md) guide for what each box does and why the event log is the spine.
