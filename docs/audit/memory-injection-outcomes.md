# Memory Injection Outcomes — protocol, no result

**Status:** protocol for the related open item in doc 83; no experiment has run. Citation utility already
exists and is an input, not the outcome being claimed here.

## Question

Does a specified memory/prior/skill injection improve verified outcomes enough to justify its context and
execution cost, and under which task and evidence scopes? A cited, recent or frequently reused lesson may
still be wrong or harmful.

## Controlled design

- Freeze model, Harness Revision, task suite, initial state, evaluator and budgets. Pair identical tasks with
  the declared injection `on` and `off`; add leave-one-out runs for individual lessons where warranted.
- Keep a source tier: observation, independently verified, convention. Record the original evidence and any
  scope/fingerprint match. Do not rewrite shared KB during the benchmark.
- Use held-out task families for transfer conclusions. Development tasks may choose retrieval policy; final
  tasks cannot be used to tune the same policy.

## Measurements

Primary is frozen-scorer success and validity, not citation. Secondary outcomes are quality, time and cost,
context tokens, tool errors, duplicate work, contradictions and negative transfer. Report the citation rate
beside—not instead of—these outcomes. Cluster uncertainty by task and disclose repeated executions.

Compare irrelevant, stale, contradicted and useful memory separately. A no-effect or harmful injection is a
valid result and may justify narrower retrieval or read-side retirement, not deletion of source evidence.

## Acceptance

A retrieval/adoption decision cites paired outcomes and coverage, distinguishes lack of evidence from lack of
benefit, and leaves a no-injection control. Convention-level behavior changes require separate authorization.
No automatic shared-KB edit, skill promotion or production prompt mutation is authorized by this protocol.
