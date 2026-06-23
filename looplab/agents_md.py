"""AGENTS.md generation (I18, ADR-8): a small context file describing the task and the
solution contract, written into the run dir for external coding-agent backends (and as
human-readable provenance).
"""
from __future__ import annotations


def generate_agents_md(task) -> str:
    direction = "minimize" if getattr(task, "direction", "min") == "min" else "maximize"
    return f"""# AGENTS.md — {task.id}

## Task
{task.goal}

## Objective
{direction.capitalize()} the reported metric (lower is better for `min`, higher for `max`).

## Solution contract
- A solution is a self-contained Python script.
- It MUST print exactly one final line of JSON: `{{"metric": <float>}}`.
- Runtime: Python standard library + numpy. No network access.
- Datasets (if any) are provided as files in the working directory (e.g. `data.json`).

## Notes for agents
- Prefer simple, correct solutions; the loop will iterate and refine.
- Evaluate honestly (use held-out/cross-validation); leakage is checked and penalized.
"""
