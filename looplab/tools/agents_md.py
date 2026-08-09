"""Generate the run-level, human-readable task-contract manifest (I18, ADR-8).

The file is provenance served by the run API.  An external repo backend receives the
task-specific brief directly and keeps any ``AGENTS.md`` owned by the seeded repository;
this generator must therefore describe the same contract without pretending that the
run-level file is copied over those repository instructions.
"""
from __future__ import annotations


def generate_agents_md(task, *, runtime_caps: str | None = None) -> str:
    direction = "minimize" if getattr(task, "direction", "min") == "min" else "maximize"
    repo_task = callable(getattr(task, "repo_spec", None))
    if repo_task:
        # A repository task owns its evaluation environment: it may install declared requirements,
        # run another language, or use hardware described by the task brief.  The conservative
        # numpy/no-network fallback belongs only to self-contained script tasks and would be false
        # provenance here when no runtime capability summary is available.
        runtime = (runtime_caps or
                   "Operator-declared repository evaluation environment; the task-specific brief "
                   "and evaluation configuration are authoritative.")
        brief = task.agent_brief() if callable(getattr(task, "agent_brief", None)) else str(task.goal)
        return f"""# AGENTS.md — {task.id}

## Task
{task.goal}

## Objective
{direction.capitalize()} the task's configured evaluation metric.

## Repository-task contract
- Improve the existing seeded repository; this is not the self-contained script/JSON-line task.
- The task-specific editable surface, protected files, data permissions and evaluation command are authoritative.
- Runtime: {runtime}

## Task-specific agent brief
{brief}

## Provenance note
This is the run-level contract record. External coding backends receive the task-specific brief
directly, while any repository-owned `AGENTS.md` remains part of the seeded repository.
"""
    # Honest runtime line: real script tasks with auto-install get the capability sentence
    # (torch/xgboost + hardware); offline/synthetic tasks fall back to numpy+stdlib.
    runtime = runtime_caps or "Python standard library + numpy. No network access."
    return f"""# AGENTS.md — {task.id}

## Task
{task.goal}

## Objective
{direction.capitalize()} the reported metric (lower is better for `min`, higher for `max`).

## Solution contract
- A solution is a self-contained Python script.
- It MUST print exactly one final line of JSON: `{{"metric": <float>}}`.
- Runtime: {runtime}
- Datasets (if any) are provided as files in the working directory (e.g. `data.json`).

## Notes for agents
- Prefer simple, correct solutions; the loop will iterate and refine.
- Evaluate honestly (use held-out/cross-validation); leakage is checked and penalized.
"""
