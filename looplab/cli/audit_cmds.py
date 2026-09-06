"""`looplab` post-run AUDIT instruments (doc 52 row 22): paid judges over ONE finished run that RECORD.

A command here reads one run directory, may spend money on a judge at the run's own endpoint, and
writes a sidecar of that run — never an event, never a cross-run store, never a champion or a metric.
The MLE-bench extras live here (`mlebench-extras`: the official rule-violation detector and the Dolos
plagiarism check); the bait-task hack-rate judge is the next instrument of the same shape.

Why a fourth money-spending group and not `governance_cmds`: governance authors cross-run memory
CONTENT (claims, concepts, facets) and was eleven lines under its file ceiling when `memory_cmds`
arrived; an audit record of one run is a different contract, and `tests/test_cli_command_groups.py`
holds the line between the three. The Typer app and the patchable builders (`_make_llm_client`,
`_settings_for_run`) live in `looplab/cli/__init__.py`, imported here like every other group.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.cli import _make_llm_client, app


@app.command(name="mlebench-extras")
def mlebench_extras_cmd(
    run_dir: Path = typer.Argument(..., help="A finished run directory (events.jsonl + task.snapshot.json)."),
    kernels_dir: Optional[Path] = typer.Option(None, "--kernels-dir",
                                               help="Public kernels for the Dolos plagiarism pass "
                                                    "(default: the task's own `kernels_dir`)."),
    no_judge: bool = typer.Option(False, "--no-judge",
                                  help="Skip the paid rule-violation judge; run only the plagiarism pass."),
    model: Optional[str] = typer.Option(None, "--model", help="Override the judge model for this call."),
    as_json: bool = typer.Option(False, "--json", help="Print the whole record as JSON."),
):
    """The two official MLE-bench extras over a finished run's champion (doc 52 row 22): the LLM
    RULE-VIOLATION detector (code + transcript against the competition rules; a PAID call to the
    run's own endpoint) and the Dolos PLAGIARISM check against downloaded public kernels. Records
    only — `<run_dir>/mlebench_extras.json` — and moves no champion, metric or selection."""
    from looplab.adapters.mlebench_extras import EXTRAS_SIDECAR, extras_report
    from looplab.cli import _settings_for_run

    run_dir = Path(run_dir)
    if not (run_dir / "events.jsonl").is_file():
        typer.echo(f"no events.jsonl under {run_dir}")
        raise typer.Exit(1)
    client, parser = None, "tool_call"
    if not no_judge:
        settings = _settings_for_run(run_dir, model=model)
        client = _make_llm_client(settings)
        parser = str(getattr(settings, "llm_parser", "tool_call") or "tool_call")
    record = extras_report(run_dir, client=client, kernels_dir=kernels_dir, judge=not no_judge,
                           parser=parser)
    if as_json:
        typer.echo(orjson.dumps(record, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"mlebench-extras: {record['status']} -> {run_dir / EXTRAS_SIDECAR}")
    if record["status"] == "ok":
        rv, pl = record["rule_violation"], record["plagiarism"]
        typer.echo(f"  champion node {record['node_id']} metric={record['metric']} "
                   f"tool_calls={record['tool_calls']}")
        if rv.get("status") == "ok":
            typer.echo(f"  rule violation: {rv['verdict']} ({len(rv['findings'])} finding(s))")
            for f in rv["findings"]:
                typer.echo(f"    - {f['rule']} @{f['confidence']}: {f['evidence'][:100]}")
        else:
            typer.echo(f"  rule violation: {rv.get('status')} — {rv.get('reason', '')}")
        if pl.get("status") == "ok":
            typer.echo(f"  plagiarism: max similarity {pl['max_similarity']} over {pl['kernels']} kernel(s)")
        else:
            typer.echo(f"  plagiarism: {pl.get('status')} — {pl.get('reason', '')}")

