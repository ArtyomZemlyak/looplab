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
settings impose obligations on the external agent. In particular, when
`concept_pivot`, `concept_run_base`, or `cross_run_concepts` is enabled, every
candidate must carry nonempty effective concept tags; the server rejects an
unannotated candidate. A phase is skippable only when its effective run policy
allows it. Operator-declared stages and protected evaluation always win.
Enabled research, hypothesis, novelty, foresight, best-of-N, strategy, report
and cross-run knowledge settings have admission or finalization checkpoints.
Use `harness-decisions` and `harness-reviews` to record idea-bound reviews and
justified no-action outcomes where a change is not supported by evidence.
While evaluating, poll `/api/runs/{run_id}/harness-checkpoints` with the run
generation and answer any pending stage or live monitor question. A checked
stage cannot advance without a verdict; an opened live question holds the
terminal until answered. `abort` is valid only when that checkpoint grants
early-stop authority. A disabled monitor opens no questions.

Search MCP `phases` for the relevant entity before each decision. It lists the
same domain writes used by the built-in roles; `phase_info` shows what to read and
which commands to submit. Research memos use `research_completed`, reports use
`report_generated`, and hypotheses, Cards and concepts have their own controls.
When the evidence supports a reusable conclusion, publish it through `/lessons`
with terminal node IDs; never include a claimed score in place of evaluation.
