# Harness Revision Benchmark — protocol, no result

**Status:** preregistered protocol for the related open item in doc 83; no campaign has run and no result is
claimed. The item's proof requires this file to exist. Do not add a result without naming the exact Harness
Revision IDs, task/scorer digests, model, budgets and evidence coverage.

## Question

Does one explicit harness revision improve verified task outcomes over the current harness at the same base
model, task set, scorer and resource budget, and at what additional agent cost? Behavioral contrast alone does
not establish correctness or transfer.

## Frozen inputs

- Task suite and per-task train/dev/test roles; benchmark answers and private graders are not exposed to the
  agent. Network policy prevents access to answers and grading criteria.
- Base model and endpoint parameters, exact CLI version unless the CLI itself is the declared revision,
  initial code/data and evaluator source digests.
- Agent/LLM/GPU budgets, language, resource limits and stopping rules. A smaller workload is not a win.
- Current Harness Revision and exactly one candidate revision. A revision records prompts, skills, tools,
  memory, orchestration and authority scopes; secrets and raw chain-of-thought are excluded.

## Arms

1. `current`: unmodified production harness, actually executed from the same initial snapshot in shadow mode.
2. `candidate`: one declared revision, with an immutable diff and reason, executed under the same conditions.
3. Optional later ablations remove one adopted component from the already selected candidate without rerunning
   improvement. They do not estimate what would have happened during improvement.

Tasks are paired and stratified by domain and difficulty. Development tasks select keep-current/candidate;
held-out tasks are touched only after that choice is frozen. Repeated executions are reported separately and
are not confused with independent tasks.

## Measurements

- Primary: external verified success rate using the task's frozen scorer and validity checks.
- Secondary: task score, protocol violations, tool errors, retries, incomplete artifacts, time to first valid
  result, agent tokens/cost, GPU time and total wall time.
- Behavioral diffs are explanatory only: planning, memory reads, tool use, context resets and duplicate work.
- Record unknown costs explicitly; zero is not a substitute. Report both per-task values and cluster-by-task
  uncertainty, with task—not API call—as the independent unit.

Report negative and no-change outcomes when they occur; `keep_current` is always allowed. Preregister a
non-inferiority margin and the smallest meaningful improvement instead of a universal significance claim.
Adoption requires reproducible evidence within those margins and no unreviewed protocol regression. More
calls, higher score alone, a model-judged preference or one successful task is insufficient.

## Rollout

1. CPU/schema and small executable fixture with no production mutation.
2. Shadow paired run with read-only production references.
3. Bounded canary with its own budget and no production prompt/default edits.
4. Explicit owner-approved adoption at a safe boundary; previous revision and all evidence remain available
   for rollback. A healthy active ML job is not interrupted for harness changes.

## Required report

Report exact coverage, missing arms, model/harness identity, quality and costs, confidence intervals,
limitations and contamination checks. Distinguish observed provider usage from agent-declared usage. Do not
claim universal optimality, component additivity or transfer to unseen domains.
