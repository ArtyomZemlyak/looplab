"""Machine-readable discovery for external agents controlling LoopLab."""
from __future__ import annotations

import json
from typing import Optional

import typer

from looplab.cli import CliRefusal, app
from looplab.core.errors import OperatorRefusal
# The manifest lives in the harness package (the MCP server serves it too); re-exported here as the
# SAME object, so `looplab.cli.harness_cmds.harness_manifest` keeps working for every caller.
from looplab.harness.manifest import harness_manifest  # noqa: F401


@app.command(name="harness")
def harness(
    settings: bool = typer.Option(False, "--settings", help="Include full Settings JSON Schema and curated field help."),
) -> None:
    """Print the external-agent capability contract as JSON (read-only)."""
    typer.echo(json.dumps(harness_manifest(include_settings=settings), ensure_ascii=False))


@app.command(name="harness-mcp")
def harness_mcp() -> None:
    """Serve all live UI API operations to a coding agent over stdio MCP.

    Requires `pip install 'looplab[ui]'` and a running `looplab ui`. Register once with
    `codex mcp add looplab -- looplab harness-mcp`. On an open UI (the default) no token is
    needed. Set LOOPLAB_HARNESS_URL for another local/proxied UI and, on a protected UI,
    LOOPLAB_HARNESS_TOKEN for the scoped agent credential. LOOPLAB_UI_TOKEN is never used:
    an environment carrying only it is refused before connection.
    """
    from looplab.harness.mcp_server import run_stdio
    try:
        run_stdio()
    except ValueError as exc:
        # A missing or unusable harness credential is a `ConfigRefusal` (doc 75 UX-01): it reaches
        # the CLI boundary as one `Refused:` line, not a usage frame. Any other ValueError keeps
        # the historical mapping.
        if isinstance(exc, OperatorRefusal):
            raise
        raise typer.BadParameter(str(exc)) from exc


# ------------------------------------------------------------------ cross-run orchestration (doc 78)
@app.command(name="gpu-gate")
def gpu_gate(
    run_dir: str = typer.Argument(..., help="The run directory (may not exist yet: write the gate "
                                            "BEFORE `looplab run --out` to start the run held)."),
    hold: Optional[bool] = typer.Option(None, "--hold/--release", help="Hold all GPU work, or release a hold."),
    start_after: list[str] = typer.Option([], "--start-after", help="Run directory whose engine "
                                          "must have exited before this run takes a GPU. Repeatable."),
    reason: str = typer.Option("", "--reason", help="Why (shown in the UI and GPU queue)."),
    clear: bool = typer.Option(False, "--clear", help="Remove the gate entirely."),
    after_node: list[str] = typer.Option([], "--after-node", help="DIR or DIR:NODE — start once "
                                         "that run evaluated NODE (or its next node). Repeatable."),
    after_stage: list[str] = typer.Option([], "--after-stage", help="DIR:STAGE or DIR:NODE:STAGE — "
                                          "start once that stage finished. Repeatable."),
    after_release: list[str] = typer.Option([], "--after-release", help="DIR — start once that "
                                            "run drains and lets go of the GPUs. Repeatable."),
    drain: Optional[bool] = typer.Option(None, "--drain/--undrain", help="Finish the GPU work "
                                         "already running, take no new (a hold that says so)."),
) -> None:
    """Hold a run's GPU work or order it after other runs (`<run>/gpu_gate.json`).

    The engine re-reads the gate on every GPU admission poll, so `--release` takes effect within a
    second. Only GPU admission waits; CPU-only stages and the agent's commands do not. Prints the
    resulting gate and what it still waits for."""
    from pathlib import Path

    from looplab.engine.gpu_queue import clear_gate, gate_blockers, read_gate, write_gate

    rd = Path(run_dir).expanduser().resolve()

    def _abs(p: str) -> str:
        return str(Path(p).expanduser().resolve())

    def _conditions() -> list:
        out = [_abs(p) for p in start_after]
        for spec in after_node:
            path, _, node = spec.rpartition(":") if spec.rpartition(":")[2].isdigit() else (spec, "", "")
            out.append({"run": _abs(path), "until": "node_evaluated",
                        **({"node_id": int(node)} if node else {})})
        for spec in after_stage:
            parts = spec.split(":")
            if len(parts) == 3 and parts[1].isdigit():
                out.append({"run": _abs(parts[0]), "until": "stage_done", "node_id": int(parts[1]),
                            "stage": parts[2]})
            elif len(parts) == 2 and parts[1]:
                out.append({"run": _abs(parts[0]), "until": "stage_done", "stage": parts[1]})
            else:
                raise CliRefusal(f"--after-stage {spec!r} must be DIR:STAGE or DIR:NODE:STAGE")
        for path in after_release:
            out.append({"run": _abs(path), "until": "gpu_released"})
        return out

    if clear:
        clear_gate(rd)
    elif (hold is not None or start_after or reason or after_node or after_stage
          or after_release or drain is not None):
        current = read_gate(rd) or {"hold": False, "start_after": []}
        fresh = _conditions()
        after = fresh if fresh else [c if c["until"] != "engine_exit" else c["run"]
                                     for c in current.get("conditions") or []]
        if any(Path(c if isinstance(c, str) else c["run"]) == rd for c in after):
            raise CliRefusal("a run cannot start after itself")
        draining = current.get("drain", False) if drain is None else drain
        held = current.get("hold", False) if hold is None else hold
        if hold is False:
            draining = False                 # --release drops a drain too: it is a hold
        elif drain is False and hold is None:
            held = False                     # --undrain releases the hold the drain was
        try:
            write_gate(rd, hold=held and not draining, start_after=after,
                       reason=reason or current.get("reason", ""), updated_by="cli",
                       drain=draining)
        except ValueError as exc:
            raise CliRefusal(str(exc)) from exc
    gate = read_gate(rd)
    typer.echo(json.dumps({"run_dir": str(rd), "gate": gate, "blockers": gate_blockers(gate)},
                          ensure_ascii=False, indent=1))


@app.command(name="gpu-queue")
def gpu_queue_cmd() -> None:
    """Show who holds the host GPU pool and who waits for it, in FIFO order (read-only)."""
    from looplab.engine import resources
    from looplab.engine.gpu_queue import queue_status

    typer.echo(json.dumps(queue_status(resources.default_gpu_host_lease_path()),
                          ensure_ascii=False, indent=1))


@app.command(name="agent-notify")
def agent_notify(
    run_root: str = typer.Option("runs", "--run-root", help="The UI's run root."),
    command: str = typer.Option(None, "--command", help="Argv template, e.g. "
                                "'codex queue --thread {thread} --message {message}' (no shell)."),
    var: list[str] = typer.Option([], "--var", help="Template variable NAME=VALUE. Repeatable."),
    debounce: float = typer.Option(None, "--debounce", help="Seconds of quiet before a batch is sent."),
    kinds: str = typer.Option(None, "--kinds", help="Comma list of event kinds to deliver."),
    disable: bool = typer.Option(False, "--disable", help="Keep the config but stop delivering."),
    clear: bool = typer.Option(False, "--clear", help="Remove the notification hook."),
    hook: str = typer.Option(None, "--hook", help="A NAMED agent connection (e.g. codex, claude) "
                             "with its own command, vars and delivery; omitted = the default hook."),
) -> None:
    """Configure the command the UI server runs to wake an external agent (operator only).

    Without options prints the current hook and its recent delivery ledger. The server batches
    actionable events, runs the command without a shell, retries failures and records every
    attempt; messages carry only ids, kinds and short redacted summaries."""
    from looplab.serve.agent_events import (clear_notify_config, notify_status,
                                            read_notify_config, write_notify_config)

    from looplab.serve.agent_events import check_hook_name

    root = run_root
    try:
        hook = check_hook_name(hook)
    except ValueError as exc:
        raise CliRefusal(str(exc)) from exc
    if clear:
        clear_notify_config(root, hook)
    elif command or var or debounce is not None or kinds or disable:
        body = dict(read_notify_config(root, hook) or {})
        if command:
            body["command"] = command
        if not body.get("command"):
            raise CliRefusal("--command is required for a new hook")
        variables = dict(body.get("vars") or {})
        for item in var:
            name, sep, value = item.partition("=")
            if not sep:
                raise CliRefusal(f"--var {item!r} must be NAME=VALUE")
            variables[name] = value
        body["vars"] = variables
        if debounce is not None:
            body["debounce_s"] = debounce
        if kinds:
            body["kinds"] = [k.strip() for k in kinds.split(",") if k.strip()]
        body["enabled"] = not disable
        try:
            write_notify_config(root, body, hook)
        except ValueError as exc:
            raise CliRefusal(str(exc)) from exc
    typer.echo(json.dumps(notify_status(root), ensure_ascii=False, indent=1))


@app.command(name="agent-events")
def agent_events_cmd(
    run_root: str = typer.Option("runs", "--run-root", help="The UI's run root."),
    after: int = typer.Option(-1, "--after", help="Print events with id greater than this."),
    run: str = typer.Option(None, "--run", help="Only this run's events (plus host events)."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Print the actionable-event outbox the UI server derives for external agents (read-only)."""
    from looplab.serve.agent_events import read_outbox

    typer.echo(json.dumps(read_outbox(run_root, after=after, run_id=run, limit=limit),
                          ensure_ascii=False, indent=1))
