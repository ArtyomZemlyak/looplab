# LoopLab as an external agent harness

If you are Codex or another coding agent working on a user's ML experiment, begin with
`looplab harness` for the implemented capability contract. Use `looplab harness --settings`
only when you need the complete settings schema. For a live run, use `looplab inspect RUN_DIR`
and `looplab replay RUN_DIR`; read `docs/guide/external-harness.md` before steering it.

The agent may decide whether a node needs a separate stages declaration or a detailed plan.
If the operator supplied `cmd.stages`, that pipeline wins. For a single `cmd.command`, an
agent authored `looplab_stages.json` can declare preceding work only when the task's editable
surface permits that file. Never duplicate training already performed inside an opaque score
command. Never edit protected scoring files, manufacture a metric, or treat a partial checkpoint
as a completed run. LoopLab owns patch validation, stage execution, score provenance, event
history and replay.

For an externally driven run, start with `looplab harness` and connect
`looplab harness-mcp` to the running UI. Launch with `--backend toy -s
external_harness=true`. Read live state, task and config through MCP, submit
ready-made candidates using durable `inject_node` commands, then inspect measured
evidence before the next decision. Pause or finalize explicitly. The old
`developer_backend=codex|claude` mode delegates only candidate editing and keeps
LoopLab's internal Researcher/search roles.

Read `GET /api/runs/{run_id}/harness-contract` before proposing. Enabled
settings impose obligations on the external agent. For a live run, also read
`GET /api/runs/{run_id}/harness-progress`
with `expected_generation` from `/state`: it lists current admission and finish
requirements, pending evaluation questions, and paged decision/review/checkpoint
history. Refresh after events or responses; inspect `source_health` if the event
log or a sidecar journal is incomplete. Commands and measured results remain in
the event timeline. External obligation settings are fixed for a launched run;
change operational settings on resume or start another run to change obligations.
Search `policy` is an agent choice: submit `set_strategy` to switch the live
evaluation policy (including `greedy` or `mcts`), or leave the current policy.
Read `harness-progress.policy_preview` for the active policy's suggested next
action and parent IDs; treat it as advice and record your own candidate choice.
Only the external agent proposes new candidates; the policy schedules evaluation
of submitted nodes. For a greedy next experiment, choose a parent using the
measured state and submit its implementation yourself. An MCTS value review is
due only while MCTS is active with positive value weight. The engine records
each applied switch as `strategy_decision`.
Read `harness-contract.delegated_semantics` before claiming parity with the
built-in novelty, foresight or listwise model judgments.
In particular, when
`concept_pivot`, `concept_run_base`, or `cross_run_concepts` is enabled, every
candidate must carry nonempty effective concept tags; the server rejects an
unannotated candidate. A phase is skippable only when its effective run policy
allows it. Operator-declared stages and protected evaluation always win.
Enabled research, hypothesis, novelty, foresight, best-of-N, strategy, report
and cross-run knowledge settings have admission or finalization checkpoints.
Enabled `report_every` also requires a current report at each node interval.
When the open pure-belief board reaches four Cards, the external agent must
review duplicates through `/api/runs/{run_id}/harness-hypotheses` before the next
candidate. A merge aliases real live Cards atomically; `no_merge` records a
reason and is invalidated when the board or measured outcomes change.
If `select_verifier` exposes a tie, score its complete evidence-bound group via
`harness-selection/verify`. With active MCTS `value_weight`, estimate all current
branches via `harness-selection/values`; both block the next candidate when due.
With `lessons_every`, record skill reviews at each configured node window;
lesson reviews are also due there when `comparative_lessons` is enabled. Both
reviews are due at finish, including a reason when no conclusion is supported.
Use `harness-decisions` and `harness-reviews` to record idea-bound reviews and
justified no-action outcomes where a change is not supported by evidence.
While evaluating, poll `/api/runs/{run_id}/harness-checkpoints` with the run
generation and answer any pending stage or live monitor question. A checked
stage cannot advance without a verdict; an opened live question holds the
terminal until answered. `abort` is valid only when that checkpoint grants
early-stop authority. A fast command evaluation with an attributed training
log may open its first monitor question after the evaluator completes, before
the node becomes terminal. At a command deadline, an enabled `deadline_grace`
checkpoint requires `extend` or `stop`; the runtime caps the extension. A
disabled monitor opens no questions.

Search MCP `phases` for the relevant entity before each decision. It lists the
same domain writes used by the built-in roles; `phase_info` shows what to read and
which commands to submit. Research memos use `research_completed`, reports use
`report_generated`, and hypotheses, Cards and concepts have their own controls.
Check each phase's `write_access`: task launch and global settings writes require
the operator credential. The scoped harness token also refuses owner model
workflows such as the legacy chat/suggest/report routes, cross-run stewards and
scope-report generation. Author the corresponding run decisions and reports
through the durable commands and guarded knowledge APIs.
When the evidence supports a reusable conclusion, publish it through `/lessons`
with terminal node IDs; never include a claimed score in place of evaluation.

After each terminal node and finalized run, read generation-fenced `/result-notices`.
Follow `next_cursor` with the same generation to recover older current receipts.
Refresh the latest page if cursor evidence changed, and after draining for new completions.
For results without current commentary, POST a brief interpretation in the user's language
(max 700 characters), retaining exact bodies for lost replies. Include
the returned receipt_id and evidence_token plus a stable action_id. Explain what
changed, caveats and the next decision; LoopLab supplies the measured numbers.
Retry lost responses with the exact same body/action_id. This appears in Assistant
chat without writing the owner's chat log or executing actions. It adds no hidden
engine wait and never replaces checkpoints or report obligations.
