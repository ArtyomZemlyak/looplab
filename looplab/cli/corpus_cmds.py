"""`looplab` CORPUS instruments: read-only measurements over a RUNS ROOT that answer a deferred
design decision's own TRIGGER — `belief-key-split`, `card-ladder`, `asha-rungs`.

THE DOMAIN, and why it is a group rather than three commands scattered across the existing ones.
Each command here exists because a `docs/BACKLOG.md` marker says, in its own words, "do not decide
this until the corpus says X" — and until now every one of those X's had to be re-derived by hand
with a throwaway script, which is how a number written on 2026-08-26 became the thing everyone
quoted and nobody could re-run. The subject is therefore the CORPUS: every command takes a runs
ROOT, folds each run's own `events.jsonl`, aggregates, and prints the reading. Not one of them
calls a model, writes a file, appends an event or reads a cross-run store, and — the part that
makes them instruments rather than rules — **none of them makes the decision it enables**. They
report; the operator decides, and the marker names what is still owed.

That is not `inspect_cmds`' contract (one run's account of ITSELF — `comparability` is multi-run but
still per-run facts about two named directories), not `concept_cmds`' (the concept taxonomy's own
content, agentic by default), not `memory_cmds`' (the shared cross-run stores), not
`governance_cmds`' (durable cross-run writes and paid stewards) and not `audit_cmds`' (a paid judge
over one finished run, recording a sidecar). The two whose subject is nearest — `inspect_cmds` and
`governance_cmds` — are also both at the line ceiling
`tests/test_cli_command_groups.py::test_no_group_is_a_god_module_again` holds them to, and that
guard's own stated norm is that the answer to an overrun is a new home, never a raise. Both reasons
point the same way, which is why this is a domain split like `memory_cmds` and `audit_cmds` rather
than a drift.

The Typer app and the shared patchable builders live in `looplab/cli/__init__.py`, imported here
like every other group. The reports themselves are pure modules under `looplab/events/`
(`belief_key_split.py`, `card_ladder.py`, `asha_curve.py`) so a test can drive them from folded
state without a filesystem; this file owns only the corpus walk and the rendering.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import orjson
import typer

from looplab.cli import app

# What a run directory IS, for the walk: the event log. Every instrument here folds it, so a
# directory without one is not a run this command can say anything about.
_EVENTS = "events.jsonl"
_RUNS_ROOT_HINT = ("Pass the runs ROOT (the directory holding one subdirectory per run), or a "
                   "single run directory.")


def _run_dirs(runs_root: Path) -> list[Path]:
    """Every run directory under `runs_root`, sorted by name; the root itself if IT is a run.

    Deliberately ONE level deep and not a recursive walk: a run's own per-node workdirs sit under it
    and a nested `events.jsonl` there would be counted as a second run. The single-directory case is
    admitted because "does this run have a curve?" is a fair question to ask of one run, and
    refusing it would push the operator into inventing a temporary root.
    """
    if (runs_root / _EVENTS).exists():
        return [runs_root]
    if not runs_root.is_dir():
        return []
    return sorted((child for child in runs_root.iterdir()
                   if child.is_dir() and (child / _EVENTS).exists()), key=lambda p: p.name)


def _folded(runs_root: Path) -> Iterator[tuple[str, object, Path, list]]:
    """Fold each run under the root, yielding `(label, RunState, run_dir, events)`.

    ONE fold per run through `events/replay.py::fold` — the same fold `looplab replay` prints, never
    a second reader. A run whose log cannot be read is skipped with a line on stderr rather than
    aborting the corpus: an instrument that answers nothing because one of forty runs is damaged is
    an instrument nobody runs.
    """
    from looplab.events.eventstore import EventLogCorruptionError, EventStore
    from looplab.events.replay import fold

    for run_dir in _run_dirs(runs_root):
        try:
            events = EventStore(run_dir / _EVENTS).read_all()
        except (OSError, EventLogCorruptionError) as exc:
            typer.echo(f"  ! {run_dir.name}: unreadable event log ({type(exc).__name__}) — skipped",
                       err=True)
            continue
        yield run_dir.name, fold(events), run_dir, events


def _emit_json(payload: dict) -> None:
    typer.echo(orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode())


def _histogram(counts: dict) -> str:
    return " ".join(f"{k}x{v}" for k, v in counts.items()) or "(none)"


@app.command(name="belief-key-split")
def belief_key_split_cmd(
    runs_root: Path = typer.Argument(Path("runs"), help="Runs root (or one run directory)."),
    limit: int = typer.Option(20, "--limit", help="How many split groups to print, widest first."),
    as_json: bool = typer.Option(False, "--json", help="Emit the whole report as JSON."),
):
    """Where the seed-TEXT belief key and a CONCEPT key would disagree, and what a merge would pool.

    The read the belief-identity backlog entry asks for and refuses to decide without: cards are
    grouped by (run, concept set, direction), the groups the text digest SPLITS are listed with the
    statements a concepts-keyed identity would merge, and the evidence and verdicts that merge would
    pool are printed beside them. It changes no key — `belief_id` is still
    `hypothesis_statement_digest(seed)` and `engine/card_reservation.py` still reads it — and it
    names no group a restatement or a distinction. That reading is a human's; this is the corpus.
    """
    from looplab.events.belief_key_split import belief_key_split_report

    runs = [(label, state) for label, state, _dir, _events in _folded(runs_root)]
    if not runs:
        typer.echo(f"no runs found under {runs_root}. {_RUNS_ROOT_HINT}")
        raise typer.Exit(2)
    report = belief_key_split_report(runs)
    if as_json:
        _emit_json(report)
        return
    typer.echo(f"{report['groups']} concept-equal group(s) over {report['cards']} card(s) in "
               f"{report['runs']} run(s) — {report['tagged']} tagged, {report['untagged']} untagged, "
               f"{report['unkeyed']} with no seed statement (excluded)")
    typer.echo(f"distinct belief ids per group: {_histogram(report['distinct_belief_ids'])}")
    typer.echo(f"{report['split_groups']} group(s) SPLIT by the seed-TEXT key, covering "
               f"{report['split_cards']} card(s); {report['conflicting_verdict_groups']} of them "
               f"would pool CONFLICTING verdicts")
    typer.echo(f"rule: {report['rule']}")
    for merge in report["merges"][:max(0, limit)]:
        flag = "  CONFLICTING VERDICTS" if merge["conflicting_verdicts"] else ""
        typer.echo(f"\n{merge['run']} [{merge['direction']}] {'+'.join(merge['concepts'])} — "
                   f"{merge['belief_ids']} belief id(s), {merge['cards']} card(s), "
                   f"{merge['evidence_nodes']} evidence node(s){flag}")
        for belief in merge["beliefs"]:
            verdicts = ",".join(f"{k}={v}" for k, v in sorted(belief["verdicts"].items()))
            typer.echo(f"  {belief['belief_id']}  {belief['card_count']} card(s)  "
                       f"{belief['evidence_count']} evidence  {verdicts}")
            typer.echo(f"      {belief['statement'] or '(no seed statement)'}")
    if report["split_groups"] > limit:
        typer.echo(f"\n... (+{report['split_groups'] - limit} more split group(s); --limit)")


@app.command(name="card-ladder")
def card_ladder_cmd(
    runs_root: Path = typer.Argument(Path("runs"), help="Runs root (or one run directory)."),
    limit: int = typer.Option(20, "--limit", help="How many runs to list in the per-run table."),
    as_json: bool = typer.Option(False, "--json", help="Emit the whole report as JSON."),
):
    """The direction -> experiment ladder over a corpus, and whether the UNDERCUT rule's trigger fired.

    Rule 3 of the card propagation rules — refutation flows DOWN as undercut — was deferred against
    a measurement (691 cards, ladder depth `{0: 690, 1: 1}`, zero parents carrying own-level
    evidence), and its trigger was written down: build it when a fold produces a card with BOTH
    `child_card_ids` and a non-empty `evidence`. This evaluates that sentence over a real runs root.
    It implements no propagation, marks no card and changes no verdict.
    """
    from looplab.events.card_ladder import card_ladder_report

    runs = [(label, state) for label, state, _dir, _events in _folded(runs_root)]
    if not runs:
        typer.echo(f"no runs found under {runs_root}. {_RUNS_ROOT_HINT}")
        raise typer.Exit(2)
    report = card_ladder_report(runs)
    if as_json:
        _emit_json(report)
        return
    typer.echo(f"{report['cards']} card(s) over {report['runs']} run(s); {report['edges']} "
               f"parent/child edge(s); max depth {report['max_depth']}")
    typer.echo(f"ladder depth histogram: {_histogram(report['depth_histogram'])}")
    typer.echo(f"{report['parents']} card(s) with children, of which "
               f"{report['parents_without_own_evidence']} carry NO own-level evidence")
    trigger = report["trigger"]
    if trigger["fired"]:
        typer.echo(f"\nTRIGGER FIRED — {trigger['cards']} card(s) carry both children and "
                   f"own-level evidence:")
        for row in trigger["rows"][:max(0, limit)]:
            typer.echo(f"  {row['run']}  {row['card_id']}  depth {row['depth']}  "
                       f"{row['children']} child(ren)  {row['evidence_nodes']} evidence  "
                       f"verdict={row['verdict']}")
            typer.echo(f"      {row['statement'] or '(no statement)'}")
            typer.echo(f"      children: {', '.join(row['child_card_ids']) or '(none)'}")
    else:
        typer.echo("\nTRIGGER NOT FIRED — no card in this corpus carries both `child_card_ids` and "
                   "a non-empty `evidence`, so the undercut rule would have zero possible firings.")
    typer.echo(f"rule: {trigger['rule']}")
    shown = sorted(report["runs_detail"], key=lambda r: (-r["trigger_cards"], -r["parents"], r["run"]))
    for row in shown[:max(0, limit)]:
        typer.echo(f"  {row['run']:<34} {row['cards']:>5} cards  {row['edges']:>4} edges  "
                   f"{row['parents']:>4} parents  depth {row['max_depth']}  "
                   f"trigger {row['trigger_cards']}")
    if len(shown) > limit:
        typer.echo(f"  ... (+{len(shown) - limit} more run(s); --limit)")


@app.command(name="asha-rungs")
def asha_rungs_cmd(
    runs_root: Path = typer.Argument(Path("runs"), help="Runs root (or one run directory)."),
    limit: int = typer.Option(20, "--limit", help="How many runs to list."),
    as_json: bool = typer.Option(False, "--json", help="Emit the whole report as JSON."),
):
    """Did any run produce a rung CURVE the ASHA watchdog could have halved?

    Successive halving needs a sequence of intermediate objective observations at increasing
    training, plus siblings at the same rung to rank against; the corpus that motivated the deferral
    printed its objective once, at the end. This reads each run's own record — the `asha_rank` /
    `asha_verdict` rows in `events.jsonl`, the `asha_monitor` / `asha_judge` spans in `spans.jsonl`
    (including the `inert_reason` the engine stamps where it DECIDES the kill is unreachable), and
    the ASHA settings its launch snapshot pinned — and reports the two rungs separately. It arms
    nothing: `Settings.asha_live_kill`, the min-siblings floor and the missing `resource_key`
    declaration are all exactly where they were.

    A run with no readable `spans.jsonl` is reported UNREADABLE, never as a run without a curve.
    """
    from looplab.core.jsonlio import read_jsonl_lenient_with_health
    from looplab.events.asha_curve import asha_curve_report, asha_run_curve

    rows = []
    for label, state, run_dir, events in _folded(runs_root):
        # `spans.jsonl` is a high-volume sidecar and is read WHOLE, exactly as `looplab timings`
        # reads it: the accelerated index is not used because building it WRITES
        # `spans.index.jsonl`, and every command in this group is read-only.
        span_path = run_dir / "spans.jsonl"
        spans = None
        if span_path.exists():
            try:
                spans, _health = read_jsonl_lenient_with_health(span_path)
            except OSError:
                # A sidecar that will not open is UNREADABLE, which the report states as such — an
                # exception here would answer "does this corpus have a curve?" with a traceback.
                spans = None
        settings = None
        snapshot = run_dir / "config.snapshot.json"
        if snapshot.exists():
            try:
                settings = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A snapshot that will not parse costs the three settings lines and nothing else —
                # the event log is what the reading is built from.
                settings = None
        rows.append(asha_run_curve(label, state=state, events=events, spans=spans,
                                   settings=settings if isinstance(settings, dict) else None))
    if not rows:
        typer.echo(f"no runs found under {runs_root}. {_RUNS_ROOT_HINT}")
        raise typer.Exit(2)
    report = asha_curve_report(rows)
    if as_json:
        _emit_json(report)
        return
    typer.echo(f"{report['runs']} run(s), {report['evals']} eval terminal(s); "
               f"{report['runs_unreadable_spans']} run(s) have no readable spans.jsonl")
    typer.echo(f"published observations: {report['monitor_ticks']} monitor tick(s), "
               f"{report['rank_rows']} asha_rank row(s), {report['verdict_rows']} asha_verdict "
               f"row(s), {report['kills']} kill(s)")
    for reason, count in report["inert_reasons"].items():
        typer.echo(f"  inert x{count}: {reason}")
    for row in sorted(rows, key=lambda r: (-r["curve_node_count"], r["run"]))[:max(0, limit)]:
        launch = " ".join(f"{k}={v}" for k, v in sorted((row["settings"] or {}).items()))
        typer.echo(f"  {row['run']:<34} {row['evals']:>4} evals  "
                   f"{row['nodes_with_samples']:>3} node(s) sampled  "
                   f"{row['curve_node_count']:>3} with a curve  "
                   f"max same-rung siblings {row['max_rung_siblings']}  "
                   f"{'spans' if row['spans_available'] else 'NO SPANS'}  {launch}")
    if len(rows) > limit:
        typer.echo(f"  ... (+{len(rows) - limit} more run(s); --limit)")
    if report["curve_found"]:
        typer.echo(f"\nCURVE FOUND — {report['runs_with_curve']} run(s) published "
                   f"{report['curve_nodes']} node curve(s) of >= {report['curve_min_points']} "
                   f"distinct resource coordinates; the largest same-rung sibling set is "
                   f"{report['max_rung_siblings']} (a kill also needs `asha_live_min_siblings` "
                   "finished peers at that coordinate).")
    else:
        typer.echo("\nNO CURVE — no run published two observations at distinct resource "
                   "coordinates, so ASHA still has nothing to halve on this corpus.")
    typer.echo(f"rule: {report['rule']}")
