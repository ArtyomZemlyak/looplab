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
