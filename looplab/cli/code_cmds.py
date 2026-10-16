"""Code identity commands (doc 79): `code-status` / `code-worktree` / `code-export`.

What each touches: `code-status` reads the run and (with `--sync`) writes ONLY the run's private
code repository (`RUN_DIR/code/repo.git`) — after a mirror restore, also the agent worktrees its
`code/agent_backup/` carries — appending no event; `code-worktree` adds a worktree of that
private repository under `RUN_DIR/code/worktrees/`; `code-export` writes NEW objects and ONE NEW
branch into the operator-named task repository and never touches its HEAD, index, working tree,
existing branches or remotes, and never pushes. No command calls a model.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from looplab.cli import _require_run_dir, app


def _refuse(message: str):
    typer.echo(message, err=True)
    raise typer.Exit(2)


@app.command(name="code-status")
def code_status_cmd(
    run_dir: Path = typer.Argument(..., help="Run directory."),
    sync: bool = typer.Option(False, "--sync", help="First build commits the log implies but no "
                                                   "code_snapshot recorded (old runs)."),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON status."),
):
    """Show the run's code: bases, per-attempt commits, what is pending upstream.

    Every evaluated attempt is a commit in the run's private repository `RUN_DIR/code/repo.git`
    (branches `base`, `nodes/n<id>`, `champion`); `git --git-dir RUN_DIR/code/repo.git show <commit>`
    is the review of one attempt."""
    from looplab.engine.code_identity import code_status, sync_run
    _require_run_dir(run_dir)
    if sync:
        agents = sync_run(run_dir).get("agent_worktrees")
        if agents:                       # a mirror restore's agent-worktree backup was consumed
            typer.echo("agent worktrees restored: " + json.dumps(agents, default=str), err=True)
    from looplab.adapters.tasks import load_task
    from looplab.core.config import read_config_snapshot
    from looplab.core.errors import OperatorRefusal
    task = settings = None
    try:
        task = load_task(run_dir / "task.snapshot.json", existing_run=True)
    except (OperatorRefusal, OSError, ValueError):       # no task: no nominations, said below
        task = None
    try:
        settings = read_config_snapshot(run_dir / "config.snapshot.json")
    except (OperatorRefusal, OSError, ValueError):       # the mode is then printed as unknown
        settings = None
    status = code_status(run_dir, task=task, settings=settings)
    if as_json:
        typer.echo(json.dumps(status, indent=1, default=str))
        return
    repo = run_dir / status["repo"]["path"]
    typer.echo(f"code repository: {repo}" + ("" if status["repo"]["exists"] else "  (not built — run with --sync)"))
    typer.echo("bases:")
    for b in status["bases"]:
        mark = "*" if b["active"] else " "
        extra = ""
        if b.get("upstream"):
            u = b["upstream"]
            extra = f"  upstream from node {u.get('source_node_id')}: {str(u.get('summary') or '')[:80]}"
        typer.echo(f" {mark} {b['digest'][:12]} {str(b['commit'] or '-')[:12]} {b['reason']}{extra}")
    typer.echo("attempts:")
    for a in status["attempts"]:
        changed = ", ".join(c["path"] for c in (a.get("changed") or [])[:4])
        more = a.get("changed_count", 0) - 4
        changed += f" (+{more})" if more > 0 else ""
        cfg = a.get("effective_config") or {}
        mism = cfg.get("mismatches") or []
        flags = (" CHAMPION" if a["champion"] else "") + (f" MISMATCH:{len(mism)}" if mism else "")
        typer.echo(f"  node {a['node_id']} g{a['generation']} s{a['seed_event_seq']} "
                   f"{str(a.get('commit') or '-')[:12]} {a['status']} metric={a.get('metric')}"
                   f"{flags}  {changed}")
    up = status["upstream"]
    typer.echo(f"upstream: mode={up.get('mode')} lane={'configured' if up['lane_configured'] else 'not configured'}")
    for p in up.get("in_flight") or []:
        gate = p.get("gate") or {}
        typer.echo(f"  pending {p['proposal_id']} {p['status']} from node {p.get('source_node_id')} "
                   f"(metric {p.get('source_metric')}) gate={'pass' if gate.get('passed') else gate.get('code') or '-'}: "
                   f"{str(p.get('summary') or '')[:80]}")
    for n in (up.get("nominations") or [])[:20]:
        typer.echo(f"  nomination node {n['node_id']} {n['path']} {n['classification']}/{n['origin']} metric={n.get('metric')}")
    for n in up.get("unlaned_changes") or []:
        typer.echo(f"  code change without a lane: node {n['node_id']} metric={n.get('metric')} {', '.join(n['paths'][:4])}")
    if status.get("unsynced_attempts"):
        typer.echo(f"{status['unsynced_attempts']} attempt(s) have no commit yet: rerun with --sync")


@app.command(name="code-worktree")
def code_worktree_cmd(
    run_dir: Path = typer.Argument(..., help="Run directory."),
    name: str = typer.Argument(..., help="Worktree name (branch agent/<name>)."),
    start: str = typer.Option("base", help="`base` (the active base), `node:<id>` or a commit."),
):
    """Give an external agent a git worktree of the run's code to edit.

    Checked out from the run's PRIVATE repository (never the operator's) at the active base, which
    holds the seeded working tree including uncommitted edits. Submit the result with inject_node
    `code_from: {"worktree": "<name>"}`."""
    from looplab.engine.code_repo import CodeRepoError
    from looplab.engine.code_worktree import add_worktree
    _require_run_dir(run_dir)
    try:
        meta = add_worktree(run_dir, name, start=start)
    except CodeRepoError as exc:
        _refuse(f"code-worktree: {exc}")
    typer.echo(json.dumps(meta, indent=1))


@app.command(name="code-export")
def code_export_cmd(
    run_dir: Path = typer.Argument(..., help="Run directory."),
    repo: Path = typer.Option(..., "--repo", help="The task's git repository to export into."),
    branch: Optional[str] = typer.Option(None, help="New branch name (default looplab/<run_id>); must not exist."),
    onto: Optional[str] = typer.Option(None, help="Start commit (default: the source commit the run "
                                                  "recorded at start if the repo has it, else HEAD)."),
    prefix: Optional[str] = typer.Option(None, help="Path of the run's editable root inside the repo."),
    node: str = typer.Option("champion", help="`champion`, a node id, or `none` for bases only."),
    no_base: bool = typer.Option(False, "--no-base", help="Skip the 'code the run started from' commit."),
    no_upstream: bool = typer.Option(False, "--no-upstream", help="Skip upstream base advances."),
    patches: Optional[Path] = typer.Option(None, help="Also write the branch as a format-patch series here."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build the commits but create no branch."),
):
    """Export the run's code into the task repository as a new branch, ready for a merge request.

    Commits, in order: the code the run started from (its uncommitted/unpublished edits relative to
    --onto), each upstream base advance with its gate evidence, then the chosen node's change with
    its metric. Only new objects and the new branch are written; HEAD, the index, the working tree,
    other branches and remotes are untouched, and nothing is pushed."""
    from looplab.engine.code_export import export_code
    from looplab.engine.code_repo import CodeRepoError
    _require_run_dir(run_dir)
    try:
        report = export_code(run_dir, repo, branch=branch, onto=onto, prefix=prefix,
                             include_base=not no_base, include_upstream=not no_upstream,
                             node=None if node == "none" else node, patches_dir=patches,
                             dry_run=dry_run)
    except CodeRepoError as exc:
        _refuse(f"code-export: {exc}")
    typer.echo(json.dumps(report, indent=1, ensure_ascii=False))
    if report["status"] == "conflict":
        raise typer.Exit(3)
