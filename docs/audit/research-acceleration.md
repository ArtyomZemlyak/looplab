# Research Acceleration Attribution — protocol, no result

**Status:** protocol for the related open item in doc 83; no acceleration estimate exists. A faster run,
fewer tokens or higher benchmark score alone does not prove faster research progress.

## Goal

Measure whether a declared harness/process revision increases the rate of obtaining verified, decision-relevant
results across comparable campaigns, and identify where time and cost actually go. The counterfactual is a
matched current harness, not a person's retrospective estimate of how long work might have taken.

## Campaign design

Use multiple comparable task families and paired current/candidate revisions with frozen evaluator, model and
budgets. Development and held-out tasks are distinct. Record model/version changes separately; model and
harness effects must not be conflated. Campaigns with different quality or validity are not ranked by speed.

## Ledger

- External verified improvement and its scientific/evaluation contract; negative and no-action outcomes stay.
- Time to first valid candidate and to first verified improvement; total campaign duration and follow-up lag.
- Agent/LLM tokens and cost, GPU time, human review time when available, and unknown costs explicitly.
- Evaluation occupancy and waiting causes: agent reasoning, tool/IO, preparation, scorer, resource queue,
  human decision, dependency, known failure and unattributed. Waiting is not automatically waste.
- Duplicate/repeated work, repairs, discarded builds and downstream reuse of tools/skills.

## Analysis

Report quality-adjusted outcomes per wall hour, agent dollar and GPU hour, plus distributions by task family.
Distinguish instantaneous progress from cumulative progress and state the chosen breakpoints before reading
the final comparison. Use task-clustered uncertainty and sensitivity analyses; do not convert individual
self-reported speedup into program acceleration.

A claim of acceleration requires comparable verified quality, adequate coverage, full costs and a credible
counterfactual. Otherwise report observed timings only. Results on well-specified benchmarks are not
automatically evidence of improvement on messy production research.
