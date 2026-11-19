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


@app.command(name="research-map")
def research_map_cli(
    run_dir: str = typer.Argument(..., help="Run directory on this host."),
    generation: str = typer.Option(..., "--generation", help="Current run generation."),
    offset: int = typer.Option(0, "--offset", min=0),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    collection: list[str] = typer.Option([], "--collection", help="Collection or summary to read; repeatable."),
    cursors: Optional[str] = typer.Option(None, "--cursors", help="JSON object of exact collection next_cursor tokens."),
    max_bytes: int = typer.Option(65536, "--max-bytes", min=4096, max=65536),
    max_items: int = typer.Option(40, "--max-items", min=1, max=100),
    briefing: bool = typer.Option(False, "--briefing", help="Show current action context instead of the map page."),
) -> None:
    """Read the bounded research map without starting or changing work."""
    from pathlib import Path
    from fastapi import HTTPException
    from looplab.harness.research_map import snapshot
    try:
        if briefing:
            from looplab.harness.briefing import snapshot as briefing_snapshot
            result = briefing_snapshot(Path(run_dir), generation)
        else:
            try:
                continuation = json.loads(cursors) if cursors is not None else None
            except (ValueError, TypeError) as exc:
                raise CliRefusal("Invalid research-map cursors JSON") from exc
            result = snapshot(Path(run_dir), generation, offset, limit,
                              collections=collection or None, cursors=continuation,
                              max_bytes=max_bytes, max_items=max_items)
    except HTTPException as exc:
        raise CliRefusal(str(exc.detail)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


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
    codex: bool = typer.Option(False, "--codex", help="Switch on the Codex preset (hook `codex`: "
                               "`codex queue --thread {thread} --message {message}`); each run's "
                               "agent reports its own session as the run's `thread`."),
    antigravity: bool = typer.Option(False, "--antigravity", help="Switch on the Antigravity CLI "
                                     "preset (hook `antigravity`: `agy --conversation {thread} "
                                     "--prompt {message}`); each run supplies `thread`."),
    thread: str = typer.Option(None, "--thread", help="With --codex or --antigravity: default session/conversation id."),
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
    if codex and antigravity:
        raise CliRefusal("choose either --codex or --antigravity, not both")
    if codex:
        # doc 82 П5: one flag, a fixed argv; the agent supplies its thread per run.
        from looplab.serve.agent_events import CODEX_HOOK, codex_preset
        try:
            write_notify_config(root, codex_preset(thread or None), CODEX_HOOK)
        except ValueError as exc:
            raise CliRefusal(str(exc)) from exc
        typer.echo(json.dumps(notify_status(root), ensure_ascii=False, indent=1))
        return
    if antigravity:
        from looplab.serve.agent_events import ANTIGRAVITY_HOOK, antigravity_preset
        try:
            write_notify_config(root, antigravity_preset(thread or None), ANTIGRAVITY_HOOK)
        except ValueError as exc:
            raise CliRefusal(str(exc)) from exc
        typer.echo(json.dumps(notify_status(root), ensure_ascii=False, indent=1))
        return
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


@app.command(name="skill-distill")
def skill_distill(
    run_id: str = typer.Argument(..., help="Run containing the current supported lesson."),
    repo: str = typer.Option(..., "--repo", help="Existing local external repository; no fetch."),
    revision: str = typer.Option(..., "--revision", help="Exact full Git commit SHA."),
    source_file: list[str] = typer.Option(..., "--source-file", help="Selected pinned text blob; repeatable."),
    label: str = typer.Option(..., "--label", help="Public source label."),
    generation: str = typer.Option(..., "--generation"),
    action_id: str = typer.Option(..., "--action-id"),
    lesson_action_id: str = typer.Option(..., "--lesson-action-id"),
    body_file: str = typer.Option(..., "--body-file", help="Agent-authored transferable technique, applicability and limits."),
    smoke_file: str = typer.Option(..., "--smoke-file", help="CPU-only Python assertions against selected source files."),
    exclusion_review: str = typer.Option(..., "--exclusion-review", help="Authored review of task-specific contamination."),
    exclude: list[str] = typer.Option([], "--exclude", help="Additional exact task/article/artifact term; repeatable."),
    source_url: Optional[str] = typer.Option(None, "--source-url", help="Public HTTPS citation only, never fetched."),
    publish: bool = typer.Option(False, "--publish", help="Validate and publish through the native skill-candidates API."),
    url: Optional[str] = typer.Option(None, "--url", help="Existing LoopLab server; required with --publish."),
) -> None:
    """Package a pinned external technique; native execution and evidence govern admission.

    Drafting is done by the external reasoning owner; this command makes no model call.
    Default output is a request preview, not a validation or publication receipt.
    """
    import re
    from pathlib import Path
    from looplab.engine.external_skill_source import collect_git_source
    from looplab.serve.routers.runs import ExternalSkillBody
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", run_id):
            raise ValueError("invalid_run_id")
        def bounded_text(path, cap):
            with Path(path).open("rb") as stream:
                raw = stream.read(cap + 1)
            if len(raw) > cap:
                raise ValueError("external_skill_input_exceeds_byte_budget")
            return raw.decode("utf-8")
        body = ExternalSkillBody(expected_generation=generation, action_id=action_id,
            lesson_action_id=lesson_action_id, body=bounded_text(body_file, 64000),
            external_source=collect_git_source(repo, revision, source_file, label=label, url=source_url),
            validation_code=bounded_text(smoke_file, 32000), excluded_terms=exclude,
            exclusion_review=exclusion_review)
        from looplab.harness.skills import parse_external_skill_request
        parse_external_skill_request(body)
        route = f"/api/runs/{run_id}/skill-candidates"
        payload = body.model_dump(exclude_none=True)
        if publish:
            if not url:
                raise ValueError("publish_requires_explicit_existing_server_url")
            from looplab.harness.mcp_server import HarnessAPI, stdio_credential
            api = HarnessAPI(url, stdio_credential())
            try:
                result = api.request("POST", route, body=payload)
            finally:
                api.client.close()
            typer.echo(json.dumps(result, ensure_ascii=False))
            receipt = result.get("body")
            skill = receipt.get("skill") if isinstance(receipt, dict) else None
            source_ack = skill.get("external_source") if isinstance(skill, dict) else None
            if (result.get("status") != 200 or result.get("outcome") == "unknown"
                    or not isinstance(receipt, dict) or receipt.get("ok") is not True
                    or not isinstance(source_ack, dict) or source_ack.get("passed") is not True
                    or source_ack.get("source_sha256") != payload["external_source"]["snapshot_sha256"]
                    or not isinstance(source_ack.get("artifact_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", source_ack["artifact_sha256"]) is None
                    or not isinstance(source_ack.get("artifact_ref"), str)
                    or re.fullmatch(r"external_skill_sources/[0-9a-f]{64}\.json", source_ack["artifact_ref"]) is None):
                raise typer.Exit(2)
        else:
            typer.echo(json.dumps({"validated": False, "published": False,
                "route": route, "body": payload}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        # Paths, Git stderr and validation-source text are not a usage-error payload.
        raise CliRefusal("External skill request refused: check pinned source, bounded inputs and required options") from exc
