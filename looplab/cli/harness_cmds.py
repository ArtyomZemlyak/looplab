"""Machine-readable discovery for external agents controlling LoopLab."""
from __future__ import annotations

import json

import typer

from looplab.cli import app
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

    Requires `pip install 'looplab[harness,ui]'` and a running `looplab ui`.
    Set LOOPLAB_HARNESS_URL for another local/proxied UI and LOOPLAB_HARNESS_TOKEN
    for the scoped agent credential (LOOPLAB_UI_TOKEN remains a legacy owner fallback).
    """
    from looplab.harness.mcp_server import run_stdio
    run_stdio()
