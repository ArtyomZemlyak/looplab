"""OFFLINE RECORD REPAIRS: `backfill-applied-params`.

WHAT THIS GROUP DOES TO THE WORLD, stated first because that is what the split is for (doc 25
CT-01). Every command here **APPENDS EVENTS TO A RUN'S OWN LOG**. Nothing is rewritten, nothing is
deleted, no model is called and no money is spent — but this is not a read-only group, and it is not
`inspect_cmds`.

Nor is it `governance_cmds`. That group's contract is the CROSS-RUN store — durable claims, concept
aliases, paid LLM stewards, an owner token. What is repaired here is a single run's account of
ITSELF: a node whose durable record kept the proposal and lost what actually ran. Different subject,
different blast radius, different failure mode.

Every command in this group must:
  * refuse a run a LIVE engine holds, and say so;
  * be idempotent, preferably by construction rather than by a check that can drift;
  * offer a DRY RUN that prints the real rows it would write, not a summary of them;
  * record what it could NOT recover as an explicit absence, never as an empty result — an empty
    record is a claim, and absence is the honest answer when nothing could be read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from looplab.cli import app


@app.command(name="backfill-applied-params")
def backfill_applied_params(
    run_root: Path = typer.Argument(..., help="The run root (e.g. runs/) — every run under it."),
    only: Optional[str] = typer.Option(None, "--only", help="One run directory name."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually append. Without it this is a DRY RUN."),
):
    """Repair the historical record: what a node PROPOSED is not what it RAN.

    `Idea.params` is a PROPOSAL. Under `params_style: "none"` the engine applies nothing — the
    Developer realises the idea by EDITING THE REPO — so a deviation is legitimate. What is not
    legitimate is that the record keeps only the proposal, and every reader downstream (the
    distilled lessons, the context handed to the next proposer, the report, the champion card, the
    UI) presents it as the parameters that produced the metric.

    Measured over every run on disk: **457 comparisons, 41 diverged (9.0%), 18 of them on nodes that
    produced a metric.** The e5 champion at 0.793426 is recorded as batch 8192 / accum 2 / 15 epochs
    and ran **512 / 32 / 3** — and that record is what put 8192 into the v3 task goal, which then
    died with three nodes and no metric.

    `metric_provenance.applied_params` records this for every eval from 2026-08-20 on. Every node
    evaluated before it has none, and the engine cannot retro-fit its own past — the evals are over.
    This command goes back and reads the workdirs that survive.

    APPEND-ONLY. It writes one `applied_params_backfilled` event per node and rewrites nothing; the
    fold applies it at read time and only where the node has no record of its own, so a live
    measurement can never be overwritten by a reconstruction — which is also what makes a second run
    a no-op. It NEVER guesses: two carriers that disagree are recorded as a CONFLICT with both
    readings and their file:line, and a node whose workdir is gone gets a row that SAYS SO, because
    "the workdir is gone" and "the proposal is what ran" are opposite statements and the second is
    the one every reader currently makes by default.

    Refuses any run a live engine holds — asked by contending for `engine.lock`, which is how the
    engine itself asks, since the file is empty and holds an flock rather than a pid.
    """
    from looplab.maintenance.backfill_applied_params import backfill
    typer.echo(backfill(Path(run_root), dry_run=not apply, only=only))
