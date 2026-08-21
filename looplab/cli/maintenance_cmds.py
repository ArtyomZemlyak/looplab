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


@app.command(name="backfill-score-metrics")
def backfill_score_metrics(
    run_root: Path = typer.Argument(..., help="The run root (e.g. runs/) — every run under it."),
    only: Optional[str] = typer.Option(None, "--only", help="One run directory name."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually append. Without it this is a DRY RUN."),
):
    """Recover the objectives the score stage MEASURED and the record threw away.

    A vecsearch score stage computes a whole IR suite and prints it — Recall@k, nDCG@k, MAP@k,
    MRR@k and Precision@k at seven cutoffs, 36 numbers — and the run keeps ONE. Measured over every
    `*.jsonl` under `runs/` (131 files, 15 run directories): 400 records carry a non-empty
    `extra_metrics`, all 1,600 values the engine's own CUDA-probe telemetry in the `specgate*` toys.
    NO experiment node has ever recorded a second OBJECTIVE.

    Not a bug — an unused door. `auto` capture fires only when `metric.kind == "stdout_json"` and
    these tasks read by `stdout_regex`, so it is structurally off; the `declared` channel
    (`eval.metrics`) no task on disk has ever populated. The numbers were computed, printed,
    preserved in `score.log`, and never entered the record.

    WHAT IT COSTS, CONCRETELY: v4 node 3 scored 0.790898 against node 1's 0.764853, and nothing
    could say whether that was a recall@100 artifact. Its own score.log answers — MAP@100 0.34 vs
    0.29, nDCG@100 0.46 vs 0.41, MRR@100 0.41 vs 0.35: it leads every family, not one.

    APPEND-ONLY, and a live record always wins, so a second run is a no-op by construction. It
    writes NO direction: nobody declared which way was better when those evals ran, orientation is a
    forward-looking declaration in `eval.metrics`, and asserting it retroactively would present a
    reconstruction as a measurement — so a ranking surface leaves these axes unranked and they are
    audit. It reports PRECISION per key, because the suite prints 2 decimals while the primary is
    read at 6 and "these two nodes are equal on nDCG" is a different claim from "the print statement
    cannot tell them apart". And it names its own HORIZON: `EventStore.read_all` stops at the first
    logical-sequence gap, which on `rubertlite-dense-retrieval` is event 20 of 1,624 lines, so that
    run's 81 `node_created` rows fold to two nodes — a bounded pass that does not say what it
    bounded reads as complete coverage.
    """
    from looplab.maintenance.backfill_score_metrics import backfill
    typer.echo(backfill(run_root, dry_run=not apply, only=only))
