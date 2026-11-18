# Completed evaluation and advisory review — acceptance and local result

The audited baseline deliberately held a successful node's terminal for every opened monitor question.
`looplab/engine/external_watch.py::settle_external_observations` already closed moot observations for a
failed attempt, but waited without a bound for successful scalar/artifact evaluations. The implementation
below changes that contract; the baseline behavior matched its historical specification.

## Question

Can a completed command settle its execution and resource receipts while an advisory anomaly review
remains explicitly unresolved, without upgrading a missing verdict to approval or hiding evidence?

## Required cases

Use genuine synthetic CPU commands through the existing external-harness pipeline; no neural run or
production mutation is required.

1. An anomaly opens `train_monitor` with `kill_enabled=false`; the command then exits successfully while
   the agent is unavailable. Settle completed execution within a declared finite bound. Preserve the exact
   observation and unresolved review disposition, such as `ended_before_decision`; never fabricate `healthy`,
   an agent answer, or a scientific quality verdict.
2. Cover an artifact with no scalar, a successful scalar, and a stall-salvaged result. Reuse the engine's
   actual success classifier. A stdout number alone does not prove the process exited.
3. The command finishes while `ask` publishes, or before the first monitor tick. No untracked question,
   duplicate terminal or silent loss of final evidence is allowed.
4. Keep live enforceable early-stop questions actionable while their process exists. Explicit stage checks,
   deadline-grace decisions and other authority gates do not become advisory merely because this proposal
   exists. An unknown process owner or incomplete result is not proven completion.
5. Reset/reclaim/abort and late answers retain generation, attempt and claim fencing. A stale verdict cannot
   affect the new attempt. Existing failed-attempt behavior remains covered.
6. A damaged question/answer journal reports unavailable evidence and preserves recovery obligations. It
   cannot become a forged completed review or silently healthy result.
7. UI/API/export distinguish command execution time, review wait and unknown resource occupancy. Retain
   historical charged costs. Do not relabel a native wall-time difference as pure GPU compute or waste.

## Acceptance

Exercise the public lifecycle, restart and replay, with the external agent deliberately absent. Report
elapsed time and all outstanding reviews after command completion. Compare a routine rule-answered case
with the anomaly case. An unattended auto-ACK script, suppressed anomaly, removed monitor or one unit test
does not satisfy this protocol.

## RESULT 2026-11-11 — isolated CPU acceptance

`settle_external_observations` no longer waits for an advisory decision once its evaluator has returned
with an actual result. Original live questions remain immutable. A separate append-only `closure` records
`ended_before_decision` and `unreviewed`, without an answer or healthy verdict. Completed execution can
reach terminal; stage checks and deadline-grace gates remain explicit. Missing/unknown results supply no
completion authority. Failed attempts retain their previous engine-authored moot-question behavior.

The shared checkpoint projection, fresh HTTP server, owner attention and UI preserve the distinction.
Progress lists unresolved completed observations separately from actionable questions. A late response to
closed execution is refused; reset/reclaim/incarnation fences remain enforced. Damaged checkpoint sources
remain unavailable rather than approved. Invocation receipts add evaluator-return and post-return elapsed
times; UI/API/export state that resource occupancy was not measured. Existing `eval_seconds` charging and
historical costs are unchanged; these elapsed numbers are not pure GPU compute measurements.

Validated with `tests/test_completed_monitor_observations.py` plus existing external checkpoints,
monitor/ASHA authority, monitor rules, source health/attention/progress, invocation receipt, eval phase and
artifact suites: **101 passed**. Genuine CPU subprocesses cover scalar and artifact completion, live tick and
final-pass paths, and a question publication deliberately held until the actual evaluator returned.
Each produces one terminal and one preserved observation, with no forged healthy event. A new server
re-derives the unreviewed record and timing; original observations and stage/deadline gates survive.
Stall-salvaged classification is covered by the existing engine success truth table and settlement tests.
Mounted UI acceptance: **8 passed**, including incomplete reads and unavailable history. The combined
final API/MCP/monitor/event/index suite passed **132 tests**; replay/documentation/claim/index/budget
checks passed **286 tests**, and strict MkDocs build passed. These suites overlap; their counts are not
independent samples.

The actual `benchmarks.external_session_smoke --monitor --obligations` also passed with a disposable
CLI engine, real MCP processes and private UI restarts. Three completed observations stayed unreviewed,
zero monitor answers were fabricated, the protected scorer stayed unchanged and inspect/replay passed.
It retained 3 research, 1 concept-base, 4 report and 8 review receipts across reconnect/reset. A first probe
exposed child imports resolving an installed baseline after cwd changed; the fixture now explicitly binds
its checkout in inherited `PYTHONPATH`. A second probe exposed a fixture comparison before the terminal's
joined timing existed; it now retains the final event prefix before exact reconnect comparison. Failed
probe evidence is preserved in the implementation package rather than counted as a pass.

Scope: local source and isolated CPU fixtures. No native campaign, neural work, live service or published
master was changed. Runtime deployment and operational storage health are not claimed by these tests.
