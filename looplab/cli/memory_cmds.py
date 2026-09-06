"""Cross-run memory HYGIENE and its UTILITY INSTRUMENT — the command that removes rows nobody's
deletion will, and the one that reports whether the rows a run was shown reached its proposals.

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

`prior-citations` (2026-09-06, doc 52 row 17) joined it for the same domain reason: it is the READ
side of the same stores — a pure projection over one run's `prior_injected` + `memory_read`
diagnostic rows (`events/prior_citations.py`) that says which lessons the store pushed and which
the proposals cited, i.e. the number the forgetting rung and the utility rank term are keyed on.
It writes nothing and calls no model; the ledger it explains (`lesson_utility.jsonl`) is written
by the run's own finalize pass, never by this command.
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
    # AVAILABILITY FIRST, in BOTH modes. The JSON branch used to exit 0 above this check, so a
    # missing or mistyped `memory_dir` printed `{"available": false, ...}` and exited 0 while the
    # human path exited 1 — and a scripted health check keyed on the exit code (the only reason
    # --json exists) read a misconfigured store as healthy. The JSON BODY is still emitted, because
    # a machine caller needs the reason and not just the code.
    if not survey["available"]:
        if as_json:
            typer.echo(orjson.dumps(survey, option=orjson.OPT_INDENT_2).decode())
        else:
            typer.echo(f"{memory_dir}: no such cross-run store")
        raise typer.Exit(1)
    if as_json and not apply:
        typer.echo(orjson.dumps(survey, option=orjson.OPT_INDENT_2).decode())
        raise typer.Exit(0)
    if not as_json:
        for line in render_orphan_survey(survey, limit=limit):
            typer.echo(line)
    if not apply:
        typer.echo("\nNothing was written. Re-run with --apply to purge. THIS IS IRREVERSIBLE.")
        raise typer.Exit(0)
    receipt = purge_orphan_identities(memory_dir, survey["identities"])
    deleted, kept, failures = receipt["deleted"], receipt["kept"], receipt["failures"]
    if as_json:
        # `--json --apply` used to fall through to prose, so a caller that asked for JSON got
        # neither the plan nor a receipt — for the one invocation in this command that WRITES.
        typer.echo(orjson.dumps({"applied": True, **receipt},
                                option=orjson.OPT_INDENT_2).decode())
    else:
        typer.echo(f"\npurged {deleted} row(s); {kept} row(s) kept by a tier rule; "
                   f"{len(failures)} failure(s)")
        for failure in failures[:20]:
            typer.echo(f"  FAILED {failure.get('store')}: {failure.get('error')}")
    raise typer.Exit(1 if failures else 0)


@app.command(name="prior-citations")
def prior_citations_cmd(
    run_dir: Path = typer.Argument(..., help="A run directory (holds events.jsonl)."),
    limit: int = typer.Option(30, "--limit", help="How many lessons to list, most-shown first."),
    as_json: bool = typer.Option(False, "--json", help="Emit the whole report as JSON."),
):
    """Did the cross-run priors this run was shown reach its proposals? (doc 52 row 17)

    A pure projection over the run's `prior_injected` + `memory_read` diagnostic rows joined to the
    `node_created` rows that followed them (`events/prior_citations.py` states the join and the
    lexical citation rule). This is the INSTRUMENT of the citation-rate audit; the audit itself is
    a number over real runs on the box.
    """
    from looplab.events.eventstore import EventStore
    from looplab.events.prior_citations import prior_citation_report

    report = prior_citation_report(EventStore(run_dir / "events.jsonl").read_all())
    if as_json:
        typer.echo(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
        return
    rate = report["citation_rate"]
    typer.echo(f"proposals {report['proposals']} · prior injections {report['injections']} · "
               f"memory reads {report['reads']}")
    typer.echo(f"shown pairs {report['shown_pairs']} · cited {report['cited_pairs']} · "
               f"citation rate {('%.1f%%' % (100 * rate)) if rate is not None else 'n/a'}")
    typer.echo(f"rule: {report['rule']}")
    rows = sorted(report["lessons"].items(), key=lambda kv: (-kv[1]["shown"], -kv[1]["shown_tool"], kv[0]))
    for lesson_id, entry in rows[:max(0, limit)]:
        typer.echo(f"{entry['shown']:>4} shown {entry['cited']:>4} cited  "
                   f"{entry['shown_tool']:>3} read {entry['cited_tool']:>3} cited  {lesson_id}  "
                   f"{entry['statement'][:70]}")
    if len(rows) > limit:
        typer.echo(f"... (+{len(rows) - limit} more lessons; --limit)")
