---
name: looplab-deep-research
description: Investigate a scientific question for an external LoopLab run using primary sources, explicit claims, and discriminating experiments.
metadata:
  workflow_version: "1"
  contract_version: "1"
---

# LoopLab deep research

Use this skill when the external agent must investigate literature, compare approaches, or synthesize a research direction. It complements `looplab-research`; neither skill grants execution, publication, GPU, or shared-memory authority.

## Establish the actual question

1. Read the current task, goal, run UID/generation, `run_progress`, `research_briefing`, experiment registrations, recent terminal receipts, and operator directions. Reconcile fresh instructions before historical handoffs.
2. Write one decision-relevant question and its intended decision. Identify the actual data, target, cohort, objective, scorer, feature-availability time, model and baseline. A literature result under another protocol is not directly comparable.
3. Inspect what this run already tested. Separate scientific experiments from preparation, profiling, recovery and scoring jobs. Do not repeat an accepted experiment merely because an old plan mentions it.

## Read and reason

1. Find relevant primary sources with the available search/fetch tools. Read the actual paper, documentation or implementation, not only a forwarded abstract. Record a stable URL and enough setup detail to check applicability.
2. For each important source extract its task, data split, target, candidate space, model, baseline, metric/denominator, main result, ablations and limitations. Distinguish author claims, independent measurements and your own inference. Do not transfer absolute scores across incompatible datasets or policies.
3. Compare sources with the current run explicitly. Name contradictions, missing evidence and plausible alternative explanations. A citation supports only the specific linked claim; a link is not verification.
4. Form a falsifiable hypothesis and feasible alternatives. Prefer the smallest experiment that distinguishes explanations over a list of fashionable techniques.

## Publish a usable memo

Publish through the native `research_completed` action in the briefing's `authoring_language`, preserving source titles, IDs, code and measurements. Include:

- `summary`: the supported conclusion and the decision it informs, not a telemetry dump;
- `reasoning`: comparison with the current setup, uncertainty and competing explanations;
- `open_questions`: what remains unknown;
- `sources`: the primary sources actually read;
- `claims`: individual statements linked through `urls` and/or terminal `node_ids`, with limitations and an authored status where useful;
- `next_experiments` or `recommended_directions`: control, one meaningful change, genuine metric/test, discriminating outcomes, and what each outcome changes.

Use `review_research_memo` for advisory link/completeness feedback before publication. Inspect its result and the actual command receipt. After meaningful results, add run-local lessons with their evidence and limitations. Publish shared lessons or skill candidates only when the evidence and delegated authority support them; empty shared memory is not a reason to invent a lesson.

For a technical retry, a score-only check or an operator-requested audit, keep the real context and state why literature/novelty is not applicable. Do not manufacture a scientific hypothesis, source or result to fill a template.

## Close the loop

Connect the proposed experiment to an existing question card using `question_card_id` and candidates using `parent_card_id`. Record scientific code lineage with `parent_id`/`parent_ids`; use `uses` only for artifact dependencies. Use staged train → protected score within one scientific candidate. Declare genuine secondary metric readers and never invent a primary scalar. After evaluation, inspect the genuine outputs and comparisons, update the cumulative overview without repeating the local result verbatim, and choose the next step from current evidence.
