# LoopLab — Architecture & Code Review (2026-07-11, revalidated)

> Current-state report for `2ce82fdb05e6be27bd6e91d943d378dbcd73860c`.
> This revision supersedes the first-pass verdict in commit `0009837`. The original report remains
> available in Git history; this file deliberately separates historical findings, applied fixes,
> surviving defects, and newly discovered regressions.

## 0. Scope, method, and priority model

The review covers the whole current repository, with extra scrutiny on the 96 commits made from
2026-07-09 through 2026-07-11 (`01c5febc..2ce82fdb`): 220 changed files and
20,832 insertions / 8,009 deletions.

Current size and structure:

- 170 production Python modules under `looplab/`, totaling approximately 39.8k physical lines;
- 156 Python `test_*.py` files and one JavaScript test file;
- 77 registered event types, 64 fold handlers, and 13 currently unhandled types manually classified
  in this review as diagnostic (there is no explicit diagnostic registry);
- 81 Python functions above Ruff's default C901 complexity threshold of 10;
- a React UI whose largest components remain `panels.jsx` (~1,100 lines), `Inspector.jsx`
  (~1,000), `AssistantBar.jsx` (~685), and `Dock.jsx` (~637).

This revalidation combined:

1. a complete reread of the original report and its implementation/follow-up ledgers;
2. line-by-line review of all current subsystem boundaries and the July 9–11 diff;
3. independent adversarial passes over state/replay, architecture/contracts, security/runtime,
   platform/CLI, and UI/control semantics;
4. executable event-sequence, API, process, filesystem, Docker, and property-style reproductions;
5. Linux Python 3.11/3.12, Windows-targeted, UI, static-analysis, and selected regression suites.

Priority meanings in this document:

- **P0 — release blocker:** silent state/result corruption, unrecoverable durability failure, or a
  bypass of an explicit trust/permission boundary. Fix before relying on unattended long runs.
- **P1 — high:** serious correctness, security, resource, or supported-platform failure. Fix in the
  stabilization cycle immediately following P0.
- **P2 — medium:** bounded operational risk, broken extensibility/compatibility contract, or
  architectural debt already producing secondary defects.
- **P3 — low:** latent edge, documentation/compatibility cleanup, or bounded UX issue.

The word **confirmed** below means the code path was traced end-to-end and, where meaningful,
reproduced. **Conditional** means the defect is real but requires the stated environment or mode.

---

## 1. Executive summary

The original executive conclusion — “the architecture is sound, the load-bearing invariants hold,
and no reproducible data-corruption/replay-divergence bug exists” — is **refuted**.

The foundation remains worth keeping: the lower dependency direction is clean, the append-only
event log plus pure fold is a strong projection model, host-side scoring is thoughtful, and the
July refactors substantially improved navigation. However, the current model lacks four identities
that the runtime now requires:

- a **node attempt** identity for reset/re-evaluation;
- a **search/finalization epoch** for reopen/add-nodes and winner promotion;
- a **request/subject revision** for approval, confirmation, abort, and forced operations;
- a **source/run manifest identity** for task/config/workspace bytes.

Without those identities, current code accepts late results from abandoned attempts, preserves stale
confirmation/approval/trust state across changed code, can reopen a run while retaining its old
confirmed champion, and can mix a new task snapshot with an old event log. These are related state-
model failures rather than isolated missing `if` statements.

The second major theme is **policy enforcement by distributed convention**. Auth is a sensitive-GET
blacklist; tool permissions are implemented separately by providers; control “needs engine” metadata
exists independently in Python and JavaScript; budgets are checked separately by normal evaluation,
confirmation, and ablation; process termination has separate sandbox/background implementations.
The July fixes closed several individual holes but did not remove the duplication that keeps creating
new ones.

Current priority order:

1. node-attempt, search-epoch, approval/request identity, and resume/finalization coordination;
2. fail-closed event-log/setup/run/workspace durability;
3. auth, permission, MCP/background-process, and eval path boundaries;
4. a shared hard `BudgetLedger` and one fitness/promotion model;
5. task/developer/tool contracts, Windows Docker, and client control semantics;
6. component decomposition, compatibility, UI coverage, and documentation cleanup.

No wholesale rewrite is recommended. The event log and public flat read model can stay; the fix is an
additive event-schema evolution plus explicit services/value objects behind the existing facade.

---

## 2. How the system currently works

End-to-end execution is:

1. `adapters/tasks.py::normalize_task` maps composable and legacy task spellings into a concrete
   Pydantic task adapter.
2. CLI/server composition builds Researcher, Developer, Strategist, SearchPolicy, tool providers,
   sandbox, and `Engine`.
3. `Engine.__init__` resolves roughly 70 options and caches optional task hooks such as assets,
   repo/eval specs, host grader, and onboarding capability.
4. Setup writes `run_started`, then materializes AGENTS.md, provenance, profiling, host-grading, and
   leakage information.
5. Each loop iteration folds `events.jsonl` into a new `RunState`; the policy returns untyped action
   dictionaries.
6. On the normal proposal path, node creation is sequential: Researcher → Developer →
   `node_created`; inject, fork, ablate, and reset paths intentionally bypass parts of that chain.
7. Evaluation may be parallel: workspace materialization → sandbox/command stages → metric readers,
   constraints, drift/trust checks → a terminal node event.
8. When search actions end, confirmation, holdout, approval, finalization, read-model export, and
   cross-run memory run.
9. Resume reconstructs a new object graph from task/config snapshots, folds the log, and re-enters
   pending work. Stage reuse additionally depends on node workdirs still existing.

The intended dependency direction holds narrowly:

```text
core  <-  events  <-  engine  <-  cli/serve/UI composition
```

There is no direct `engine -> serve` import. `core` remains below the application layer and `events`
imports only `core`.

The middle band is not actually a DAG. Lazy imports hide a conceptual strongly connected component:

```text
adapters <-> agents <-> search <-> tools
```

- `adapters/tasks.py` normalizes schemas *and* builds agents, policies, and tool providers;
- `search` imports concrete agent wrappers and tool-backed merge helpers;
- `agents/tool_loop.py` imports tools;
- `tools/machine_runs_tools.py` reaches back into task validation.

### What is genuinely strong

- `fold` is pure and deterministic for a **fixed ordered log**.
- Unknown event types are ignored, and the first terminal event is correctly idempotent **within one
  node attempt**.
- The event log is a useful source of truth for the state projection and audit trail.
- EventStore serializes ordinary local writers and repairs/ignores a torn final line correctly.
- `RunResult` unifies subprocess, Docker, command-eval, and stage outcomes.
- Host-side scoring keeps held-out labels out of the candidate workspace.
- `Node.robust_metric`, handler extraction, shared JSONL helpers, `materialized_stages`, and
  `default_agent_control()` removed real semantic duplication.
- Golden-state and prefix-replay tests are a valuable foundation.

### What “source of truth” does not currently include

Continuation is not reproducible from `events.jsonl` alone. It also depends on task/config snapshots,
the current source repo/data, mutable node workdirs, role `last_*` side channels, process-global
background tasks, and shared memory files. `config_hash` hashes only the task model, not Engine options,
code revision, environment/image digest, or the exact bytes seeded into nodes.

---

## 3. P0 findings — fix before release confidence

### P0-1 · Node lifecycle events have no attempt identity

**Anchors:** `events/replay.py:159-213,231-290,313-335,440-441,498-521,535-538,588-590,631-634`;
`engine/evaluate.py:42-140`; `engine/orchestrator.py:822-860,948-988`.

`node_reset` reuses the same node ID and manually clears fields. A late event from work started before
the reset is indistinguishable from the new attempt.

Confirmed sequence:

```text
node_created(0)
node_reset(0)
late node_evaluated(0, attempt_id="old", metric=9)
=> node 0 becomes evaluated; metric and cost are accepted
```

`Event.data` preserves the extra `attempt_id`, but the fold handlers do not interpret or validate it.
The same defect accepts late `confirm_eval`, `node_confirmed`, and `best_confirmed` after reset. A
reproduced run ended with raw metric `1`, confirmed/robust metric `9`, and `confirmed_done=True`.

Node-keyed state also survives reset:

- abort immediately aborts the replacement attempt;
- reward-hack/leakage evidence for old code can exclude newly implemented code after an
  `implement|propose` reset (preserving it for a plain eval reset of identical code can be valid);
- after one completed forced-confirm, a **new explicit** request for the same node ID is ignored
  because completion is stored by node ID rather than request ID.

**Fix:** add `attempt_id` to all node lifecycle, evaluation, confirmation, abort, holdout, trust, and
forced-operation events. Reset increments the attempt. Fold accepts effects only for the active
attempt. Old v1 events migrate as `attempt_id=0`. Preserve old trust evidence as audit history, but
scope enforcement to `(node_id, attempt_id, code_hash)`.

### P0-2 · Confirmation, holdout, and approval have no search epoch or subject revision

**Anchors:** `events/replay.py:373-387,498-521,751-796`; `engine/confirm_phase.py:35-64`;
`engine/orchestrator.py:577-599`; `search/policy.py:61-66,324-347,423-435,530-532`.

Confirmed sequence:

```text
node 0 raw=.80, confirmed=.75
run_finished
resume + add_nodes
node 1 raw=.10 (better, direction=min)
=> fold best remains node 0; confirmed_done remains true
```

Once any confirmed pool exists, new unconfirmed candidates are excluded from final selection and no
new confirmation pass runs. Policies simultaneously disagree: Greedy follows the folded champion,
while Evolutionary/ASHA/MCTS use raw metric and may expand a different node.

Approval is global rather than subject-bound. `approval_granted(node_id=999)` sets `approved=True`;
after reopen and a different best, no second approval is required. `spec_approved` is also accepted
without a proposal, setting `spec_confirmed=True` while `proposed_spec=None` and skipping onboarding.

**Fix:** introduce `search_epoch` and subject-bound promotion records:
`{request_id, epoch, node_id, attempt_id, subject_hash}`. Reopen/add-nodes begins a new epoch and
invalidates confirmation, holdout promotion, and approval completion for the old candidate set.
Transition validation must reject approval without a matching pending request/proposal hash. A
holdout that has already been surfaced through state/UI is no longer unseen: continuing search must
use an epoch-specific still-hidden holdout/secondary holdout, or start a new run rather than reusing
the disclosed final signal.

One concrete consequence is the apparent search/promotion fitness split: fold uses confirmed/holdout
promotion fitness, while Evolutionary, ASHA, and MCTS rank raw metrics and Greedy often follows the
folded best. Separate `SearchFitness` and `PromotionFitness` are valid between explicit phases; they
become contradictory only when reopen leaves the prior promotion epoch active. This is a consequence
of P0-2, not an additional independent finding. One owner should expose typed search-rank and
promotion APIs, and a new epoch should re-confirm the current candidate set.

### P0-3 · Setup completion is inferred from `run_id`, creating a permanent crash window

**Anchor:** `engine/orchestrator.py:664-765`, especially `:673` and `:698-758`.

`run_started` is appended before AGENTS.md, data provenance, host-grading metadata, dataset profiling,
and the leakage hard-stop. Resume gates the whole setup phase on `not state.run_id`; diagnostic
`setup_*` events are not folded.

A crash immediately after `run_started` therefore makes every later resume permanently skip the
remaining preflight, including leakage enforcement.

**Fix:** fold an explicit setup state machine:
`setup_started(step_hash) -> setup_step_completed -> setup_completed`. Search may begin only after
`setup_completed`. Make each step idempotent/content-addressed and add crash injection after every
append boundary.

### P0-4 · Mid-file event-log corruption is detected but allowed to grow an invisible tail

**Anchors:** `events/eventstore.py:298-371`; `cli/run_cmds.py:329-339`.

Confirmed sequence:

```text
valid seq0
corrupt line
valid seq2
read_all() => [seq0]
append resume seq3
read_all() => [seq0]
log_divergence() => two dropped records
```

The CLI warns, then resumes and appends behind the unreadable boundary. Every new event is durable on
disk but invisible to fold. Torn **final** lines are handled correctly; this finding is specifically
about corruption in the middle.

**Fix:** fail closed in EventStore itself before any append/resume. Provide an explicit operator
`repair-log` workflow that backs up the original, quarantines or salvages the tail, atomically
truncates to the last valid boundary, and records the repair provenance.

### P0-5 · Run/task/workspace identity can silently mix different experiments

**Anchors:** `cli/run_cmds.py:271-313`; `engine/triage.py:16-40`;
`engine/workspace.py:103-145,172-213`; `adapters/tasks.py:133-136`.

Confirmed failures:

- `looplab run` on an existing output directory writes the new task snapshot before folding/reopening
  the old event log. A real reproduction produced `task.snapshot=poly_regression` while
  `run_started.task_id=toy_quadratic`, then added regression evaluations to the toy run.
- For a git repo, the workspace fingerprint is only `HEAD`. Dirty tracked bytes are copied by the
  seeder but do not change the fingerprint. A tracked file changed from `AAAA` to `BBBB`; fingerprint
  stayed `git:<same sha>` while the node received `BBBB`.
- Detected workspace drift is audit-only; the run continues comparing nodes built from different
  baselines.
- `{repo: NEW, editable_path: OLD}` silently keeps `OLD`, because `repo` is only popped when the
  legacy alias is absent.
- Relative repo path `.` may be validated in the server cwd and later interpreted under the package
  cwd of the child Engine.
- HTTP preflight checks only the legacy `editable_path`, not every `editables[].path`, reference, or
  data source; relative paths in those collections are likewise child-cwd dependent.

**Fix:** an immutable `RunManifest` must include canonical task/config hash, code revision, environment
identity, and an exact `WorkspaceSnapshot` of the bytes/materialized file set. `run` must refuse a
non-empty run directory; continuation is `resume`. Conflicting aliases are errors. Resolve relative
paths once against the config/task-file source and persist the canonical absolute form. Workspace
drift requires an explicit new epoch/run, not an audit-only flag.

### P0-6 · Assistant permission decisions are not centrally enforced

**Anchors:** `serve/routers/assistant.py:90-156`; `serve/assistant.py:269-314`;
`tools/shell_tools.py:152-159`; `agents/tool_loop.py:27-52`.

Three independent bypasses survived:

1. Cancel marks a pending permission as denied/resolved, but a stale resolve request does not check
   state ownership and overwrites it with allow. A deterministic reproduction wrote a file after the
   user cancelled the turn.
2. MCP providers are added even in `plan` mode and CompositeTools dispatches them without a policy
   wrapper. Building the toolset may also start a configured stdio MCP server before any decision.
3. `kill_background` checks only `deny`; in default `ask` mode it never calls the approver and kills
   the process-global task directly.

The original report listed the third item as “refuted”. That refutation was incorrect: plan-mode deny
does not satisfy ask-mode approval semantics.

**Fix:** introduce immutable `ToolSpec(effect=read|write|process|network|run_control)` and a central
policy decorator around every provider, including MCP. Permission resolution must be an atomic CAS
`pending -> allowed|denied|cancelled`, keyed by turn/request revision; stale resolution returns 409.
MCP connections are lazy and happen only after authorization.

### P0-7 · Command-eval paths cross the declared workspace boundary

**Anchors:** `runtime/command_eval.py:41-64,104-152,253-290,475,521-548`.

Confirmed:

- stage names accept `../escape`, `..\escape`, `C:\temp\escape`, `\`, control characters, and NUL;
  the name is interpolated into a log path, and an escaped log was written outside the expected dir.
  Embedded NUL can raise `ValueError` while opening the log **after** the child was spawned, outside
  the current OSError-only guard and without guaranteed tree cleanup;
- `adapter.path="../outside/adapter.py"` executes external Python and returned a metric;
- `host_score.predictions="../preds.json"` reads stale predictions outside the attempt workdir and
  produced a perfect score.

The file readers already have a containment helper, but adapter and prediction paths do not share it.

**Fix:** a single `PathPolicy` resolves all task, stage, adapter, metric, prediction, artifact, and log
paths. Stage names use a short slug allowlist with no separators, drives, control/NUL, or dot segments.
Prediction input must be a newly created regular file under the current `(node, attempt)` workdir.
Stage/eval timeouts also need a finite, positive, bounded value object rather than raw floats.

---

## 4. P1 findings — stabilization blockers

### P1-1 · Resume/finalization can leave a zombie run

**Anchors:** `serve/routers/control.py:128-156`; `engine/finalize.py:29-65`;
`cli/run_cmds.py:367-378`.

While an old Engine holds the singleton lock during finalization, UI/CLI appends `resume`; the spawn
endpoint returns `already_running`. The old process then exits without re-entering the loop, leaving
`finished=False` and no Engine. Confirmed with TestClient plus the real OS lock.

**Fix:** a server-side `RunCommandService.append_and_ensure_engine()` and `EngineSupervisor` must
coordinate intent, epoch, lock acquisition, and pending wakeup. Clients should never assemble the
two-step transaction.

### P1-2 · `max_eval_seconds` is not a hard cumulative ceiling

**Anchors:** `engine/orchestrator.py:948-988`; `engine/confirm_phase.py:85-141`;
`engine/ablation.py:27-66,120-152`.

Confirmed:

- `max_parallel=4`, cap `.05` launched four evaluations and spent `.20`;
- confirmation launched six seed evaluations despite starting above its cap;
- ablation probes did not change `total_eval_seconds` at all.

Parallel overrun is bounded to a batch, but the documented “hard cumulative ceiling” is false.

**Fix:** a shared atomic `BudgetLedger.reserve(kind, estimate)` before every eval/confirm/ablate
operation, followed by actual-cost commit/release. Parallel work reserves before spawn. Host-only
holdout scoring should still be accounted, but may use a separate resource bucket unless the public
`max_eval_seconds` contract is explicitly broadened to include it.

### P1-3 · Sensitive GET auth remains a default-open blacklist

**Anchors:** `serve/server.py:80-85,126-160`; `serve/routers/runs.py:195-225`;
`serve/routers/misc.py:116-132`; `serve/jobs.py:76-85`;
`serve/routers/assistant.py:175-182`.

With `LOOPLAB_UI_TOKEN` enabled:

- unauthenticated node detail returned 200 with full code, files, persisted `stdout_tail`, trials,
  and parent code;
- the same caller received 401 for `/log`;
- unauthenticated `/api/llm/health` performed a real model completion. A fake client counted a new
  completion for missing and bad tokens; those calls are billable when the configured backend is.

`/api/jobs/{id}` and assistant progress also return sensitive data without the token, though random
IDs make them less enumerable (P2 capability-style mitigation). Public `/state`/SSE projections keep
the first 160 characters of each node's stdout/error, which can still expose a secret printed near the
start. The route census must also classify genesis-job results, synthesized scope reports, and `/prov`
rationale/params rather than assuming every projection is harmless.

**Fix:** auth dependencies/scopes should be attached to routes. Default-deny all `/api` data and
effects; explicitly mark only lightweight public projections. Convert model health to authenticated
POST or a transport-only probe without generation.

### P1-4 · Background process termination is not reliable

**Anchor:** `runtime/bg_tasks.py:73-128,216-235`.

BackgroundManager is process-global and session-unowned. On Windows it creates no process group;
deadline enforcement happens lazily only on read/list; timeout sends one terminate and permanently
suppresses retries; kill neither waits nor escalates and reports success immediately. A parent/child
reproduction killed the parent while the child remained alive.

The generic sandbox path is not affected: it uses the robust `_kill_tree` implementation.

**Fix:** one session-owned `ProcessSupervisor`, using process groups/job objects, deadline watcher,
TERM → bounded wait → tree KILL, verified exit, and bounded/rotated logs.

### P1-5 · Resource caps still have CPU, RAM, and disk bypasses

**Anchors:** `runtime/sandbox.py:253-284`; `runtime/command_eval.py:41-64,96-103,127-133`;
`serve/routers/runs.py:230-287`.

- `_tee_drain` uses `readline`, so one no-newline output line is fully allocated before truncation.
- Live logs are unbounded on disk.
- BackgroundManager's temp log has no byte quota, and its lazy deadline is never enforced if no caller
  polls `read/list`.
- file JSON and host-score JSON inputs are read wholly into memory.
- `run_setup.log` ignores the node-log API tail cap.
- stage count is unbounded, while the log endpoint caps each stage separately, allowing authenticated
  response amplification.
- Python `re` can catastrophically backtrack; clipping text to 200k characters does not bound CPU.
  `^(a+)+$` showed exponential growth by input length.
- stage timeout accepts negative, NaN, and infinity. A NaN deadline makes the trusted-local timeout
  comparison permanently false.

**Fix:** chunked binary drains, log quotas/rotation, stage-count and size checks, finite positive
bounded timeouts, streaming/bounded JSON parsing, and RE2/non-backtracking regex or a separately timed
worker process. Any failure after spawn, including NUL path errors, must still kill/reap the tree.

### P1-6 · Workdir trust audit fails open

**Anchors:** `engine/audit.py:76-107`; `trust/reward_hack.py:50-60`.

Missing protected files are skipped, invalid UTF-8 becomes `got=None` without a signal, and a broad
exception returns an empty finding list. Confirmed deletion and unreadable-byte cases both reported
clean. Static scanning also misses ordinary deletion APIs such as `os.remove`.

**Fix:** return `clean | tampered | unavailable`. Missing/unreadable protected inputs are hard
signals in gate/block modes; audit failure must never be represented as clean.

### P1-7 · Leakage detector remains unsafe as a hard gate

**Anchor:** `trust/leakage.py:51-113`.

The July token anchor fixed the original `X_trainval`, `X_interval`, and `X_latest` examples, but the
current regex still:

- false-positives on `train_values`, `values`, and benign `validation_split`;
- misses multiline `.fit(X_test)` and a second `.fit()` on the same line;
- can hide target leakage when NaN propagates through the correlation calculation.

Because `data_leakage:*` can exclude a node from winning and breeding, a heuristic regex is too
authoritative.

**Fix:** AST/token analysis with structured evidence and confidence. Only high-confidence/verified
signals may hard-gate; heuristic results remain advisory until corroborated.

### P1-8 · Windows Docker extra mounts are malformed

**Anchor:** `runtime/command_eval.py:384-418`.

Additional data/reference mounts use the same absolute Windows path as host and container target,
producing `C:/host:C:/host:ro`. Docker Desktop 29.5.3 rejects this as “too many colons”. The main
`C:/...:/work` mount works; untrusted Windows RepoTasks with extra inputs fail.

**Fix:** map inputs to stable Linux container targets such as `/looplab-inputs/<hash>` and rewrite
the in-container path/symlink map. Prefer `--mount type=bind,src=...,dst=...,readonly` over colon
syntax.

### P1-9 · Developer wrappers lose failures and parent semantics

**Anchors:** `adapters/repo_developer.py:464-494,830-881`;
`search/best_of_n.py:128-193`; `agents/roles.py:512-585`; `engine/node_build.py:95-141`.

- RepoDeveloper returns step errors as ordinary strings; the caller ignores them. If every step
  fails, it can return empty output/parent files and evaluate the baseline as a new experiment.
- BestOfN implements only `implement/repair`, so outer capability detection cannot see the inner
  `implement_from/repair_from`; improve/repair loses the parent-aware path.

**Fix:** `DevelopmentRequest(base_node, mode, idea)` → immutable
`DevelopmentResult(code, files, deleted, report, step_results, status)`. Explicit capability
protocols or one complete protocol must be preserved by all wrappers.

### P1-10 · Client control semantics are duplicated and already diverged

**Anchors:** `ui/src/AssistantBar.jsx:32-40,299-302`; `ui/src/api.js:58-80`;
`serve/tui_format.py:179-188`; `ui/src/Dock.jsx:553-557`.

AssistantBar `/resume`, `/finalize` (and its `/abort` alias), `/approve`, and `/ratify` append a control
event and show success without spawning the Engine. Dock correctly performs control + resume.
`actionNeedsEngine` exists but is unused. Both the JavaScript and TUI registries omit
`approval_granted`, `spec_approved`, `node_reset`, and `run_reopened`; the TUI path is active while the
JS helper is currently latent.

**Fix:** one server-side command service and one `ControlSpec` registry containing validation,
authorization, transition effect, and `needs_engine`; generate/export the client metadata.

### P1-11 · The latest strategy governance fix still applies forbidden params

**Anchor:** `engine/strategy.py:184-207` (`2ce82fd`).

`name_ok` and `params_ok` are calculated independently, but when policy-name change is allowed and
parameter change is locked, the code still builds `pp` from the raw input and passes it to
`make_policy`.

Confirmed:

```text
agent_control = {policy: [strategist]}; policy_params locked
apply {policy: mcts, policy_params: {c: 9}}
=> MCTSPolicy.c == 9
```

**Fix:** authorize into a new filtered `StrategyDelta`; mutation consumes only that object. Add both
asymmetric tests: name locked/params allowed and name allowed/params locked.

### P1-12 · Fail-open filesystem locks permit duplicate event sequence numbers

**Anchors:** `events/eventstore.py:27-56,298-329`; `cli/__init__.py:109-159`.

On normal NTFS/ext4 the lock works. On the documented unsupported/FUSE fail-open path, two
EventStore instances can both derive and write the same next sequence number. State-sensitive
control endpoints also validate one fold and append later without expected-revision/CAS semantics.

**Fix:** do not degrade a multi-writer source-of-truth log to unlocked append. Use a single writer
actor/separate control journal or storage CAS, and require `expected_seq` on state-sensitive commands.

---

## 5. P2/P3 operational and architectural findings

### P2 operational risks

- **`run_setup` “exactly once” is process-local.** `engine/eval_dispatch.py:32-63` uses an in-memory
  flag; a new Engine repeats successful setup, while a second call on the same failed object can skip
  it. The pre-run flag correctly prevents concurrent workers in one process, but not crash-safe
  exactly-once behavior. Fold completion **should be** keyed by command/environment hash.
- **Fold-handler coverage is no longer protected by its test.** `events/replay.py:646-712` dispatches
  through `_HANDLERS`, while `tests/test_event_types.py:100-125` still scans old string comparisons.
  Other registry tests still catch unregistered literal emissions and invalid control membership,
  and this review manually classifies all 13 currently unhandled types as diagnostic; the missing
  protection is specifically an explicit folded/diagnostic partition. Current census: 77 registered,
  64 handlers, 16 golden-log types, and 11 handled types in the golden log.
- **Foreign/hand-edited event cost can poison replay.** `eval_seconds="3"` raises `TypeError` and a
  negative value reduces the cumulative budget (`events/replay.py:159-213,313-328`). Normal Engine
  emitters do not produce these values, so this is a P2 persisted-input validation defect rather than
  a current normal-path P1. Use typed payloads with finite non-negative constrained floats.
- **Context compaction does not enforce its aggregate target.** `core/context_budget.py:37-93`
  (`bc56bb6`, `2ce82fd`) correctly keeps a replacement `tool_calls.arguments` blob valid JSON, but
  skips every message at or below `per_msg_cap` even when their aggregate exceeds `max_chars`. A
  reproduction remained 7,983 characters before and after a 2,000-character target. Compact complete
  assistant/tool turn groups until the aggregate postcondition is met and report any irreducible
  protected head/tail separately.
- **Background research can change future steering by scheduler order.** `events/types.py:149-161`
  labels the allowed events selection-neutral and order-tolerant and explicitly acknowledges the hint
  race; it does not claim full order neutrality. A fixed log still replays deterministically, and the
  already folded champion is unchanged. However, `human replace -> background hint` retains both
  hints while the inverse order retains only the human hint (`engine/research_cadence.py:92-136`;
  `events/replay.py:571-578`; `engine/node_build.py:67-74`), so later proposals are schedule-dependent.
  Return background values to the main writer at a deterministic causal slot, or add logical
  revision/priority. This is P2 because current impact is bounded to future steering.
- **`trusted_local edit:false` is a data-integrity footgun.** `engine/workspace.py:146-160,215-231`
  creates writable symlinks. This is not a container escape — docs reserve hard RO for
  untrusted/hostile — but candidate code can still mutate the original. Prefer copy/reflink/ACL or
  advertise the weaker contract explicitly.
- **Capability-like GET leaks.** `/api/jobs/{id}` and assistant progress are untokened. Random IDs
  reduce exploitability but results/live model text remain sensitive.
- **The SPA token is not per-user isolation on a shared JupyterHub origin.** It is embedded in a meta
  tag and is a per-deployment secret readable by other same-origin content. Route scopes close API
  omissions, but real multi-user isolation still requires upstream/per-user auth or a private origin.
- **Case-insensitive reserved run IDs.** `ASSISTANT` passes the exact-case check and aliases the
  service directory on Windows.
- **TensorBoard exposure.** `cli/inspect_cmds.py:114` defaults to `0.0.0.0` without authentication.
- **Windows TUI loses live refresh.** `select` on a console fd fails and falls back to blocking
  `readline`; this is degraded UX, not a permanent production deadlock.
- **Numeric ablation is not a paired experiment.** `engine/ablation.py:55-62` asks a stochastic
  Developer to regenerate code per parameter, so the measured delta mixes parameter effect and
  arbitrary implementation changes. Use the same artifact plus overrides or `ablate_from`.

### P2 contract and decomposition debt

- `Engine` is still a distributed god object: 12 mixins, 121 method definitions across Engine and its
  mixins, and approximately 111 fields
  assigned in its constructor. File navigation materially improved and composition objects such as
  `LessonMemory`, `HoldoutGrader`, and `WorkspaceSeeder` are real progress; state ownership is still
  largely shared through the Engine namespace.
- `RunState` has 74 flat fields and `Node` 30. Lifecycle, control requests, promotion gates, budgets,
  and audit sidecars share one mutable projection, which is why reset invalidates local fields but
  misses run-level collections.
- `Event.data` is untyped and `Event.v` is written but never read/migrated. This is not a version-skew
  bug while only v1 exists, but it must be fixed before the first schema migration.
- `TaskAdapter` promises four required members, yet Engine requires serialization and the LLM path
  calls `llm_roles`; a structurally conforming third-party adapter can pass `isinstance` and fail.
- `SearchPolicy` promises only `next_actions`, yet Engine mutates `max_nodes` and
  `ablation_capable`; a conforming frozen/slotted third-party policy can break.
- `Developer` promises only `implement`; repair, parent-aware implementation, output files, deletes,
  reports, and audit evidence are duck-typed side channels.
- Task/Developer source-scan registries do mitigate accidental hook renames; they do not express or
  preserve undeclared capabilities such as parent-aware implementation through wrappers.
- `ValidatingDeveloper`, like BestOfN, exposes only implement/repair and does not forward parent-aware
  hooks. BestOfN is the reproduced P1 path; the validating wrapper is a P2 contract omission today.
- `ToolProvider` returns strings and has no effect, permission, cancellation, or session metadata.
- `AppState` uses late-bound route callbacks and router registration order is load-bearing because of
  a generic `/api/{kind}` route.
- Docker hardening policy is built independently in `sandbox.py` and `command_eval.py`; it already
  drifted once and was manually resynchronized by the July fixes.
- `Settings`, `EngineOptions`, constructor `_opt`, UI schema, and docs remain parallel representations.
  The deliberate product/library default profile is valid, but `**knobs` removed signature-level
  checking while leaving manual unpacking.
- Repeated full fold keeps purity but remains O(events²) over a long run's iterations; EventStore's
  incremental parser saves parsing, not reconstruction of the Pydantic projection.
- Final LLM-cost aggregation follows the current object graph and can miss replaced/auxiliary clients;
  resume records a latest rollup rather than a guaranteed lifetime ledger.

### P3 compatibility and cleanup

- The July split preserved flat imports for most modules but removed `looplab.runs_tools` and
  `looplab.tools.runs_tools` in favor of `machine_runs_tools`. `_LAYOUT` tests are self-referential and
  cannot detect deletion of a historical alias. Treat the impact as downstream compatibility risk,
  not an internal runtime defect.
- Flat `looplab.htmlview` and `looplab.traceview` still work. Missing intermediate
  `looplab.serve.htmlview` was not part of the promised flat compatibility contract.
- Moved functions retain their defining module globals. Assignment-based patches to re-exported
  helper/constants such as `llm.BACKOFF_CAP_S`/`_chunk_has_content` do not affect those globals;
  patching a re-exported callable itself can still work at call sites, and `agent.run_phase` was
  intentionally preserved as a patch seam. The gap is narrow, not a blanket monkeypatch failure.
- `ui/src/Dock.jsx` contains duplicate `resume` and `run_abort` object keys; the later definitions win.
- Conservative fallback defaults still differ from `Settings` in a few incremental-construction paths
  (`auto_install_deps`, `foresight_panel`, strategist backend).
- `seed_mode`, `eval_trust_mode`, and `strategist_backend` still accept some invalid strings at config
  construction time; downstream behavior is mostly fail-safe/later-loud, so this remains low priority.
- Deferred toy/latent edges remain: multiplicative ridge lambda cannot escape zero in one offline
  baseline, MLE-bench docs assume `train.csv/test.csv` despite a wider asset glob, and fork/inject
  retains an effect-before-gate SIGKILL window.

---

## 6. Revalidation of the original H/M findings

This table is the authoritative disposition of the first-pass IDs. “Fixed” means the original
failure no longer reproduces; it does not imply the subsystem has no newer issue.

| Original ID | Current disposition | Revalidation |
|---|---|---|
| H1 ablation no-progress spin | **Fixed** | Engine stamps `ablation_capable`; policy/cadence honors it. |
| H2 bare-substring leakage gate | **Partially fixed / reopened** | `X_trainval/X_latest` fixed, but `train_values`/`validation_split` false-positive and multiline/second-fit false-negative remain; see P1-7. |
| H3 untrusted command-eval hardening | **Fixed** | The original H3 is fixed: caps, no-new-privileges, resources, and env forwarding are present. A separate Windows extra-bind defect remains; see P1-8. |
| M1 dataset `min` objective negation | **Fixed** | Direction-conditional instruction and tests present. |
| M2 unbounded max MCTS reward | **Fixed** | Bounded continuous `_mcts_reward` present. |
| M3 partial strategy lost on resume | **Fixed** | Active strategy is merged/reapplied. |
| M4 governance mostly unenforced | **Partially fixed / reopened** | Broad per-field gating exists, but the inverse `policy`/`policy_params` case bypasses it; see P1-11. |
| M5 lessons read outside lock | **Fixed** | Authoritative reread occurs inside interprocess lock. |
| M6 reflection cap 3 vs 8 | **Fixed** | Explicit limit 8 is threaded. |
| M7 carried stage mismatch | **Fixed** | Shared `materialized_stages` and prompt/eval parity. |
| M8 raw GET auth omissions | **Partially fixed / reopened** | Original raw routes are gated; node detail, paid health, jobs/progress omissions remain; see P1-3. |
| M9 `repo_read` false EOF | **Fixed** | Full-file pagination/resume marker test present. |
| M10 context budget docs | **Fixed** | `None` fallback vs `0=off` now documented correctly. |
| M11 missing prompt override keys | **Fixed** | Repo Developer keys are documented. |
| M12 Docker env/seed drop | **Fixed** | Per-call env forwarded into container. |

### Original low/nit groups

The following original low/nit items were source-checked and remain fixed: defensive `run_started`
reads, unkeyed confirm-cost guard, PEP-604 coercion, GPU comma parsing, path resolve guard, reasoning
retry guard, sweep parameter/emit handling, developer-crash batch stop, deep-research cadence,
reconcile hash reset, harmonic query abstraction, shared secret fields, streaming error bubble,
TUI composable preview, atomic delete-node rewrite and pre-parse, grader glob precision, MCP late-boot
unwind/result cap, sandbox docstring, perfect-metric equality, robust projections, paused `run`, timings
non-dict guard, imported Node annotation, stderr floor, README/mkdocs counts, and the documented
trust/novelty/stages/router/memory/MLE-bench wording corrections.

Items still open or reopened:

- aggregate context truncation — P2 operational risk;
- fork/inject effect-before-gate crash window — P3;
- `_strategy_core` omits timeout/max_parallel, but no shipped producer currently emits them — P3;
- assistant SSE holds a worker on blocking `q.get(timeout=10)` — P3;
- remaining enum validation gaps — P3;
- toy lambda and MLE-bench filename assumptions — P3.

The old “`fidelity` is a dead Settings seam” finding is refuted: it is intentionally a governance
dial rather than a Settings field.

### Revalidation of the original “refuted” list

| Old refutation | Current status |
|---|---|
| `kill_background` does not bypass approver | **Old refutation was wrong.** Ask mode bypass confirmed; see P0-6. |
| Strategy timeout/max-parallel whitelist is a bug | Still refuted; intentional forward seam, no producer. |
| `_ablate` needs the writer lock | Still refuted; sequential main-loop path. |
| `trust_gate_changed` is undocumented | Still refuted; exception is documented. |

---

## 7. Review of the July 9–11 changes

### Changes that introduced or exposed current regressions

- **`2ce82fd`** independently gated policy name/params but consumed raw params, creating P1-11.
- **`bc56bb6` / `2ce82fd`** fixed counting and valid-JSON replacement for large tool arguments but
  did not enforce the aggregate history target (P2 operational risk).
- **`abc3632`** made fold handlers much clearer, but left the old comparison-scanning fold-handler
  coverage test ineffective (P2). It moved reset logic; reset itself was introduced July 7 and its missing
  attempt model predates this refactor.
- **`3eebbd6`** fixed the exact old leakage examples but the replacement regex introduced/retained the
  P1-7 false-positive/false-negative set.
- **`80986d8`** expanded the sensitive-GET blacklist, **`29258b4`** refined trace/span segment matching,
  and **`2ce82fd`** hoisted the constants; all preserved the default-open architecture that omitted
  node detail and model-completion health (P1-3).
- **`d9e4e23`** added kill/background timeout/retention but did not reuse the robust process supervisor
  or permission flow (P0-6, P1-4).
- **`eecd3ea` / `3e89272`** split UI/CLI/TUI utilities, preserving duplicated control metadata and
  creating/retaining the client divergence in P1-10.
- **`3e89272`** promised historical monkeypatch compatibility through re-exports, but assignment to
  re-exported helper/constants cannot change the defining globals of moved functions.
- **`f6e5b0a`** improved the `core/events/engine` dependency direction. It deliberately renamed
  `runs_tools` after verifying no internal importers; downstream compatibility still needs an explicit
  immutable manifest if it is to be guaranteed. The middle-band lazy cycle remains.
- The Windows extra-bind form was introduced July 8; the main Docker hardening fixes did not exercise
  Windows host-path grammar.

### Changes that were good and should be preserved

- projection modules moved out of `serve` into `events`;
- fold dispatch is easier to audit than a 63-arm conditional;
- shared robust metric, materialized stage parsing, JSONL helpers, and agent-control factory;
- golden exact-state/prefix replay fixtures;
- stronger Docker flags/env forwarding on Linux;
- numerous real prompt, docs, pagination, secret, memory, and atomic-write fixes listed in §6.

The right conclusion is not “the refactor failed”. It improved locality and removed real duplication,
but mechanical/verbatim moves preserved hidden contracts. The next phase should change ownership and
types, not repeat another file split.

---

## 8. Architecture remediation plan

### Phase 0 — fail-closed containment (small PRs, first)

1. Filter `StrategyDelta`; add inverse governance tests.
2. Replace sensitive-GET blacklist with route scopes; protect node/job/progress; change LLM health.
3. CAS permission resolution; centrally gate MCP and `kill_background`.
4. Add `StageName` and shared PathPolicy for adapter/prediction/log paths.
5. Make EventStore reject mid-file divergence before append; add explicit repair command.
6. Enforce aggregate context postcondition.
7. Add the minimal atomic `RunCommandService.append_and_ensure_engine()` plus pending wakeup so
   resume cannot be lost during finalization.
8. Replace the fold-handler source-scan with an exact folded/diagnostic registry partition.
9. Immediately make heuristic leakage evidence advisory until a safe parser lands; make missing or
   unreadable protected inputs `unavailable/tampered`, never clean.
10. Reject non-finite/non-positive stage/eval timeouts before process spawn.

**Gate:** no behavioral API change without regression tests; all current v1 logs continue to fold.

### Phase 1 — event/state identity

Add, without rewriting old logs:

- `attempt_id` on node lifecycle/eval/trust/holdout events;
- `search_epoch` on reopen, confirmation, promotion, approval, and all finalization outputs
  (`run_finished`, report, budget/archive, and derived final artifacts);
- `request_id` on forced/approval/control operations;
- `subject_hash` and `expected_seq` on state-sensitive decisions;
- typed payload models and an explicit v1 migration/default layer.

Keep the external flat RunState JSON as a compatibility projection. Internally split reducers into
`RunLifecycle`, `NodeAttempt`, `PromotionState`, and `RequestLedger`.

**Gate:** model-based/random event-sequence tests; reset during eval; repeated reset; stale terminal;
reopen after confirmation; new best requires new promotion/approval; old logs produce the same public
projection before the new events appear.

### Phase 2 — durability and reproducibility

- explicit folded setup completion and content-addressed run-setup completion;
- immutable `RunManifest` and `WorkspaceSnapshot`/CAS;
- exact separation of `run` and `resume`;
- fail/explicit rebase on source drift;
- no unlocked multi-writer fallback on actual deployment targets, including FUSE/geesefs: use a
  single-writer service/control journal, storage CAS, or fail startup for multi-writer mode when
  locking is unavailable.

**Gate:** crash after every setup append; corrupt-middle repair; dirty tracked repo resume; task alias
conflicts; run directory reuse; original data unchanged by `edit:false` policy where promised.

### Phase 3 — execution infrastructure

- atomic `BudgetLedger` for eval/confirm/ablate/holdout;
- one `ProcessSupervisor` for foreground/background/sandbox work;
- process group/job object, deadline watcher, verified kill escalation;
- chunked output, disk quotas, bounded readers, timed regex;
- stable Windows Docker input mapping.

**Gate:** budgets never over-reserve across parallel work; parent+child processes are gone before kill
returns; bounded memory/disk adversarial tests; Windows and Linux Docker matrix.

### Phase 4 — typed domain contracts and layer boundaries

- `TaskSpec` plus backend capability protocols;
- `DevelopmentRequest/DevelopmentResult` and parent-aware semantics;
- `ToolSpec/ToolResult/ExecutionContext` with central policy;
- `SearchFitness/PromotionFitness`;
- `NodeLifecycleService`, `EvaluationService`, `PromotionService`, `StrategyController`;
- composition root above adapters/agents/search/tools to break the middle cycle.

Keep `Engine` as a compatibility facade/coordinator; remove mixins incrementally only after service
tests exist.

### Phase 5 — server/UI/compatibility

- finish the server-side `RunCommandService`/`EngineSupervisor` abstraction begun by the minimal
  Phase-0 wakeup fix;
- one generated/exported `ControlSpec` for Python and JavaScript;
- UI e2e coverage for AssistantBar, Dock, TUI, auth, and zombie recovery;
- immutable `LEGACY_ALIASES` manifest and behavioral monkeypatch tests;
- one schema source for Settings/EngineOptions/UI/docs profiles.

---

## 9. Required validation matrix

### State machine and durability

- crash injection after every state-changing append;
- reset during real parallel evaluation;
- late eval/confirm/holdout events from prior attempts;
- repeated force-confirm/abort/approval requests with distinct IDs;
- premature `spec_approved` without a proposal;
- finish → resume during finalization;
- late `run_finished`/report/archive from an old epoch;
- reopen/add-nodes after confirmation/holdout/approval;
- mid-file corruption, torn tail, and explicit repair;
- unsupported-lock filesystem behavior.
- malformed, non-finite, string, and negative persisted cost payloads;
- repeated explicit forced request after an earlier completion;
- background-hint interleavings followed by actual proposal generation;
- dirty tracked bytes with unchanged git HEAD;
- BestOfN preservation of parent-aware capabilities;
- duplicate sequence plus stale `expected_seq` under no-lock mode.

### Security and trust

- route census: every `/api` endpoint declares auth/public scope;
- unauthenticated health performs zero model calls; `/state` contains no stdout/error snippets; genesis,
  scope-report, provenance, job, and progress routes have explicit scopes;
- stale permission resolve after cancel/delete/new turn;
- every tool provider, including MCP, exercised in plan/ask/auto modes; constructing a plan-mode
  toolset must not spawn an MCP stdio process;
- `kill_background` must invoke the approver in ask mode;
- a stubborn child that ignores TERM and a task never polled after its deadline must still be reaped;
- stage/path fuzzing (separators, drives, UNC, NUL, Unicode, symlinks);
- NaN/infinite/negative timeouts and NUL-after-spawn cleanup;
- adapter/prediction files must remain inside the current attempt;
- protected file missing/unreadable/modified states;
- regex CPU timeout and bounded JSON/log readers.

### Platform and clients

- Windows/Linux Docker mounts with data/reference inputs;
- Windows job-object and POSIX process-group tree termination;
- Windows case-insensitive `ASSISTANT` run ID and TensorBoard bind/auth behavior;
- CLI run-dir/task identity;
- direct AssistantBar, Dock, TUI control actions against stopped/finalizing runs;
- control-registry parity including run-reopened, approval, spec approval, and reset;
- legacy imports and assignment-based monkeypatch behavior.

### Current test evidence

- Separate full Linux runs under Python 3.11 and Python 3.12 at this HEAD each produced **1,711 passed,
  33 skipped, 1 failed**; both interpreters had the same stale keepalive test failure.
- A fresh focused Windows run of the 11 most relevant suites passed with two skips after running
  outside the filesystem sandbox; the first sandboxed attempt failed only because pytest could not
  create Windows temp directories.
- A distinct broader platform-targeted matrix produced Linux 281 passed / 2 skipped and Windows 278
  passed / 2 skipped / two platform failures (a POSIX `/tmp` assumption and the real Docker bind
  grammar).
- UI grouping tests: 8/8; production build succeeds with duplicate-key and ~636 KB chunk warnings.
- Ruff C901 reports 81 functions over complexity 10; Ruff's fatal-name checks found no undefined-name
  class of runtime errors.
- The current event census is independently reproduced: 77 types / 64 handlers / 16 golden types /
  11 golden handled types.

The lone full-suite failure, `test_complete_text_stream_bails_on_a_keepalive_stall`, patches the old
`urllib` seam while the implementation uses OpenAI SDK/httpx and sets `timeout=0`, disabling the guard.
Current keepalive watchdog/raw-httpx fallback tests pass. Treat it as stale test isolation, not a
runtime keepalive regression.

---

## 10. False positives and explicit downgrades

These were rechecked so they do not re-enter the priority list incorrectly:

- generic Windows sandbox tree-kill works; only BackgroundManager has the parent/child bug;
- Windows TUI falls back to blocking input and loses live refresh; it is not an infinite production
  hang once the user presses Enter;
- writable `trusted_local` symlinks are a data-integrity/design risk, not a container escape;
- random job/session IDs reduce the exploitability of jobs/progress endpoints relative to enumerable
  node detail, though the data should still be authenticated;
- Bandit MD5/SHA1 findings are predominantly non-cryptographic stable identifiers;
- the plan-mode `remember` operation is an intentional scoped knowledge-base write exception;
- the read-only wheel smoke failure came from setuptools trying to update `egg-info` on a read-only
  source bind, not from a product runtime path;
- flat `looplab.htmlview/traceview` compatibility works; intermediate `serve.*` paths were not the
  promised compatibility surface;
- first-terminal-wins is correct within an attempt; the defect is the absence of attempt identity;
- background events do not directly change an already folded champion; they change causal inputs to
  future proposals;
- `run_setup` is neither globally “never retried” nor crash-safe exactly once: a fresh Engine repeats
  it, while the in-process flag usefully prevents concurrent workers;
- preserving reward-hack evidence is reasonable for a plain eval reset of identical code and stale
  when implement/propose replaces the code;
- the forced-operation defect is a new explicit request being ignored after prior completion, not an
  obligation to replay an old request automatically;
- fold is deterministic for a fixed ordered log, not commutative/order-independent;
- confirmed-only promotion is valid at the original finalization; it becomes wrong only after a new
  epoch/candidate set without re-promotion;
- workspace dirty-*tracked* bytes are unconditionally reproduced; untracked bytes matter only under
  `seed_mode=all` or non-git copytree;
- parallel budget overrun is bounded to a batch; ablation remains wholly outside accounting;
- `Event.v` is pre-migration debt, not an active version-skew failure while only v1 exists.
- physical extraction materially improved navigation; the remaining problem is logical ownership,
  not that the refactor had no value;
- source-scan Task/Developer registries reduce rename drift, but cannot preserve capabilities they do
  not declare;
- duplicate sequence numbers are specific to the documented no-lock fail-open path; ordinary
  NTFS/ext4 locking works.

---

## 11. Documentation and diagram status

The infographic's concrete spot values still match current Settings/code: novelty default/mode and
thresholds, seed/holdout counts, confirmation default/base seed, and deep-research/strategist cadences.
The “Engine + 12 mixin files” statement also remains literally true.

The old universal “architecture concepts and process diagram are clean” conclusion is too strong.
`docs/guide/concepts.md` still describes novelty as effectively opt-in/agent-decided while the default
LLM novelty adjudicator is a separate active path. More importantly, the diagrams do not model node
attempts, search epochs, subject-bound approval, setup completion, or the hybrid non-log inputs needed
for resume. Concrete values can be correct while the lifecycle model is incomplete.

Documentation should be updated in the same PRs that introduce the new state/event contracts, with
the diagrams generated or at least checked from the same registries where possible.

---

## 12. Final current-state verdict

LoopLab has a strong experimental foundation and substantial, thoughtful hardening. The July fixes
resolved most of the original H/M list and the refactors improved code navigation. The system is not,
however, at the point where event replay alone guarantees safe reset/reopen/resume under concurrency.

The next engineering work should be a stabilization program centered on identity, transition
validation, fail-closed durability, and centralized effects — not another broad mechanical split.
Once attempts/epochs/requests/manifests are explicit, the existing event log, fold, policies, UI
projections, and compatibility facade can remain and become considerably easier to reason about.
