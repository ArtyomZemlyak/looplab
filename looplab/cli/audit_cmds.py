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


@app.command(name="bait-materialize")
def bait_materialize_cmd(
    root: Path = typer.Argument(..., help="Directory to write the three bait tasks under (<root>/<bait>/task.json)."),
):
    """Write the three BAIT tasks (doc 52 row 22, BAITBENCH's shape): ordinary small prediction
    tasks with a planted, rule-compliant shortcut each — a post-outcome `client_score` column, the
    test labels left in `archive/`, a scorer that trusts the printed metric — documented beside the
    data the way a real README documents a leaky column. Deterministic; no model, no network."""
    from looplab.judgebench.bait import materialize, reference_scores

    written = materialize(root)
    for bait_id, task_path in written.items():
        ref = reference_scores(root, bait_id)
        typer.echo(f"{bait_id:16s} {task_path}  intended={ref['intended']} shortcut={ref['shortcut']}")


@app.command(name="bait-audit")
def bait_audit_cmd(
    run_dir: Path = typer.Argument(..., help="A finished run made on one of the bait tasks."),
    bait: str = typer.Option(..., "--bait", help="Which bait the run was made on: proxy_feature | answers_on_disk | lenient_scorer."),
    bait_root: Optional[Path] = typer.Option(None, "--bait-root", help="The materialized bait root, to carry the reference scores."),
    no_judge: bool = typer.Option(False, "--no-judge", help="Only the deterministic fingerprint stage; no paid call."),
    model: Optional[str] = typer.Option(None, "--model", help="Override the judge model for this call."),
    as_json: bool = typer.Option(False, "--json", help="Print the whole record as JSON."),
):
    """Score EVERY evaluated node of a bait-task run: stage one is the planted bait's own fingerprint
    in the code (deterministic, a lower bound), stage two the two-stage transcript judge (a PAID call
    to the run's own endpoint). Records `<run_dir>/bait_audit.json` — a hack RATE, moving nothing."""
    from looplab.judgebench.bait import BAIT_IDS, audit_run
    from looplab.cli import _settings_for_run

    run_dir = Path(run_dir)
    if not (run_dir / "events.jsonl").is_file():
        typer.echo(f"no events.jsonl under {run_dir}")
        raise typer.Exit(1)
    if bait not in BAIT_IDS:
        typer.echo(f"unknown bait {bait!r}; one of {', '.join(BAIT_IDS)}")
        raise typer.Exit(2)
    client, parser = None, "tool_call"
    if not no_judge:
        settings = _settings_for_run(run_dir, model=model)
        client = _make_llm_client(settings)
        parser = str(getattr(settings, "llm_parser", "tool_call") or "tool_call")
    record = audit_run(run_dir, bait, client=client, judge=not no_judge, parser=parser, bait_root=bait_root)
    (run_dir / "bait_audit.json").write_bytes(orjson.dumps(record, option=orjson.OPT_INDENT_2))
    if as_json:
        typer.echo(orjson.dumps(record, option=orjson.OPT_INDENT_2).decode())
        return
    typer.echo(f"bait-audit {bait}: {record['nodes']} node(s) -> {run_dir / 'bait_audit.json'}")
    typer.echo(f"  fingerprint: {record['fingerprint_hits']}/{record['nodes']} "
               f"(rate {record['fingerprint_rate']})")
    if record["judged"]:
        typer.echo(f"  judge: hack rate {record['hack_rate']} over {record['judged']} judged node(s)")
    else:
        typer.echo("  judge: not run (--no-judge)")
    if record.get("reference_scores"):
        ref = record["reference_scores"]
        typer.echo(f"  reference: intended={ref['intended']} shortcut={ref['shortcut']}")

