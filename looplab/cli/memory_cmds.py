"""Cross-run memory HYGIENE — the one command that removes rows nobody's deletion will.

Its own module, and the reason is the guard that forced it: `governance_cmds` sits eleven lines
under the line ceiling `tests/test_cli_command_groups.py::test_no_group_is_a_god_module_again`
holds it to, and that bound exists because 1,701 lines is how three domains once hid in one file.
Squeezing a fourth in — or lifting the ceiling because a change tripped it — is the accretion the
guard is there to refuse, so this took the answer the guard was actually giving.

The DOMAIN is distinct from governance's, which is why this is not a workaround. `governance_cmds`
records DECISIONS about cross-run evidence (a merge, a split, a claim verdict, a paid steward) and
every one of them ADDS. This removes rows whose writing run is gone, decides nothing about their
content, and is the only command here that can destroy evidence — so it is report-only unless the
operator says `--apply`, and every rule it obeys (attribution by `run_uid` then `run_id`, the tier
predicates that keep shared evidence, the `blind` refusal) belongs to `serve/memory_cascade.py`
rather than to this file.
"""
from __future__ import annotations

from pathlib import Path

import orjson
import typer

from looplab.cli import app


@app.command(name="memory-orphans")
def memory_orphans_cmd(
    memory_dir: Path = typer.Argument(..., help="Cross-run memory dir (holds cases.jsonl, lessons.jsonl, …)."),
    runs_root: Path = typer.Option(Path("runs"), "--runs-root",
                                   help="The run root that decides which runs still exist."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually purge. Without it, nothing is written. IRREVERSIBLE."),
    limit: int = typer.Option(25, "--limit", help="How many contributing runs to list."),
    as_json: bool = typer.Option(False, "--json", help="Emit the survey as JSON."),
):
    """Report — and only with `--apply`, remove — cross-run memory rows whose run no longer exists.

    NOT run automatically by anything, and deliberately so: the five stores are SHARED and the purge
    is irreversible, so it shows the whole answer before it writes anything. A run's deletion
    cascades only when the operator asks; a store full of rows from runs removed OUTSIDE the UI (a
    `rm -rf`, a temp dir, a worktree) has no deletion to hang off at all, and this is the sweep for
    that case. The attribution, the tier predicates that keep shared evidence, and the `blind` rule
    that refuses to call a uid-carrying row orphaned when a surviving run cannot be read are all
    `serve/memory_cascade.py`'s — see `purge_orphan_identities` and `orphan_survey` there.
    """
    from looplab.serve.memory_cascade import (orphan_survey, purge_orphan_identities,
                                              render_orphan_survey)

    survey = orphan_survey(memory_dir, runs_root)
    if as_json and not apply:
        typer.echo(orjson.dumps(survey, option=orjson.OPT_INDENT_2).decode())
        raise typer.Exit(0)
    if not survey["available"]:
        typer.echo(f"{memory_dir}: no such cross-run store")
        raise typer.Exit(1)
    for line in render_orphan_survey(survey, limit=limit):
        typer.echo(line)
    if not apply:
        typer.echo("\nNothing was written. Re-run with --apply to purge. THIS IS IRREVERSIBLE.")
        raise typer.Exit(0)
    receipt = purge_orphan_identities(memory_dir, survey["identities"])
    deleted, kept, failures = receipt["deleted"], receipt["kept"], receipt["failures"]
    typer.echo(f"\npurged {deleted} row(s); {kept} row(s) kept by a tier rule; "
               f"{len(failures)} failure(s)")
    for failure in failures[:20]:
        typer.echo(f"  FAILED {failure.get('store')}: {failure.get('error')}")
    raise typer.Exit(1 if failures else 0)
