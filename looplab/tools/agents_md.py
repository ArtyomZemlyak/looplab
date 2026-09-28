"""Generate the run-level, human-readable task-contract manifest (I18, ADR-8).

The file is provenance served by the run API.  An external repo backend receives the
task-specific brief directly and keeps any ``AGENTS.md`` owned by the seeded repository;
this generator must therefore describe the same contract without pretending that the
run-level file is copied over those repository instructions.
"""
from __future__ import annotations


def generate_agents_md(task, *, runtime_caps: str | None = None,
                       external_harness: bool = False) -> str:
    direction = "minimize" if getattr(task, "direction", "min") == "min" else "maximize"
    repo_task = callable(getattr(task, "repo_spec", None))
    harness_note = "" if not external_harness else """
## External agent control
This run waits for an external agent. Inspect its live state/config and the evaluation
contract through `looplab harness-mcp`. Choose whether proposing, stages and a plan
are useful; submit ready-made code/files through the durable `inject_node` command.
Use MCP `phases` and `phase_info` to inspect every decision's entity, evidence,
prompt keys, accepted command fields and corresponding built-in owner.
Read `/api/runs/{run_id}/harness-contract` for this run's actual obligations.
Follow enabled run settings: a concept workflow requires effective concepts
on every candidate, checked on admission. Other phases may be skipped only
when the run contract and operator settings permit it.
With `concept_run_base`, seed `run_concepts` from the first scored candidate's
authored tags before submitting another candidate.
Enabled research and hypothesis settings require a current memo and candidate
hypothesis. Novelty, foresight and best-of-N require idea-bound reviews through
`/api/runs/{run_id}/harness-decisions`; compare distinct alternatives when the
configured breadth exceeds one. Enabled knowledge phases require final
`/api/runs/{run_id}/harness-reviews` receipts, including justified no-action
decisions. An enabled report must cover the final candidate count.
With `report_every`, publish the report at its configured node interval before
submitting another candidate.
When four or more pure belief Cards are open, review the board through
`GET/POST /api/runs/{run_id}/harness-hypotheses` before the next candidate;
merge genuine duplicates or record `no_merge` with a reason.
If `select_verifier` yields a tie, GET `/api/runs/{run_id}/harness-selection`
and POST complete evidence-bound samples to `/harness-selection/verify`.
When MCTS `value_weight` is active, POST headroom for all listed branches to
`/harness-selection/values`. With `lessons_every`, review lessons and skills
at each configured node interval through `harness-reviews`, citing a write or
explaining why no action applies. These are admission gates when due.
During command evaluation, poll `GET /api/runs/{run_id}/harness-checkpoints`
with the current `expected_generation`. A checked or asserted stage waits for
your verdict before the next stage runs. Enabled live training and ASHA observers
may open questions; answer each with `POST` to the same route. An opened live
question must be answered before the node's terminal can be recorded.
After LoopLab measures a candidate, inspect its metric or failure before branching,
repairing as a new child candidate, or finalizing. Respect the edit surface and
protected scorer; no internal Researcher/Developer will complete a code-less idea.
You own novelty and knowledge decisions. Preview an idea with
`POST /api/runs/{run_id}/novelty-preview`, inspect prior lessons via `/api/memory`
and `/api/cross-run/claims`, then choose whether the experiment merits evaluation.
Write evidence-linked lessons with `POST /api/runs/{run_id}/lessons` and a stable
`action_id`; LoopLab stamps the run/task identity and node outcome signatures.
You may draft a procedural skill from a supported lesson with
`POST /api/runs/{run_id}/skill-candidates`; LoopLab checks live evidence and
derives candidate/promotion status from distinct task fingerprints.
Author a `research_completed` memo or `report_generated` report through the same
durable command API; LoopLab sanitizes and folds them into its normal projections.
Author concept tags in the injected idea or use `concept_tag_edited` and
`run_concepts` commands. Review `/api/runs/{run_id}/concepts` and
`/api/cross-run/concept-policy` before applying a governed cross-run
`concept-merge`, split, purge or alias-clear. No internal model will perform
reflection, taxonomy stewardship or automatic concept ratification in this mode.
Cross-run task facets have a revision-fenced `/api/cross-run/task-facets` ledger.
"""
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
""" + harness_note
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
""" + harness_note
