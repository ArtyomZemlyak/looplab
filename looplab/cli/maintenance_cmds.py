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

    Refuses any run a live engine holds — asked by the engine's own liveness rule, which contends
    for `engine.lock` (the file is empty and holds an flock rather than a pid) — and with `--apply`
    HOLDS that lock until its last append, so no engine can start mid-pass.
    """
    from looplab.maintenance.backfill_applied_params import backfill
    typer.echo(backfill(Path(run_root), dry_run=not apply, only=only))


@app.command(name="evaluate-track")
def evaluate_track_cmd(
    run_dir: Path = typer.Argument(..., help="The run directory."),
    track: str = typer.Argument(..., help="A name under the task's eval.tracks."),
    nodes: str = typer.Option("all", "--nodes", help="'all' evaluated nodes, or ids: 3,5,7."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually run and record. Without it this is a DRY RUN."),
):
    """Run a declared evaluation TRACK over settled nodes' preserved workdirs (doc 73 §1.4).

    `eval.tracks.<name>` is an operator argv (`{workdir}`, `{node_id}`, … placeholders) whose last
    stdout JSON object holds the numbers: @200 for nodes scored @20, drift weeks, a second ruler.
    Each node must be evaluated in its current lifecycle with its workdir's manifest stamp matching
    its code — otherwise it is refused, never measured. Results are recorded beside the live
    metrics (`extra_metrics_imported`, `source: track <name>`), never over them; `--apply` holds
    `engine.lock`. A recorded key can then be the objective (`metric_retarget`).
    """
    from looplab.core.errors import ConfigRefusal
    from looplab.maintenance.evaluate_track import evaluate_track
    from looplab.maintenance.import_metrics import MetricsInputRefusal
    try:
        typer.echo(evaluate_track(run_dir, track, nodes, apply=apply))
    except MetricsInputRefusal as exc:
        raise ConfigRefusal(f"evaluate-track: {exc}") from None


@app.command(name="import-metrics")
def import_metrics_cmd(
    run_dir: Path = typer.Argument(..., help="The run directory."),
    file: Path = typer.Argument(..., help="JSON {node_id: {metric: value}} measured after the run."),
    source: str = typer.Option(..., "--source",
                               help="What measured these numbers (kept on every row)."),
    precision: Optional[int] = typer.Option(None, "--precision",
                                            help="Decimals the values were printed to, if coarse."),
    apply: bool = typer.Option(False, "--apply",
                               help="Actually append. Without it this is a DRY RUN."),
):
    """Import metrics measured AFTER the run, beside each node's live ones (2026-10-06: nodes
    scored at @20, re-scored at @200 by a service, to be ranked by @200 via `metric_retarget`).

    One folded `extra_metrics_imported` row per node, bound to its current lifecycle, holding only
    the keys the node does not already carry: a live value is never overwritten. The values ride the
    `declared` channel with NO direction and are marked reconstructed key by key; `--apply` holds the
    run's `engine.lock`, so it never writes beside an engine.
    """
    from looplab.core.errors import ConfigRefusal
    from looplab.maintenance.import_metrics import MetricsInputRefusal, import_metrics
    try:
        typer.echo(import_metrics(run_dir, file, source=source, apply=apply, precision=precision))
    except MetricsInputRefusal as exc:
        raise ConfigRefusal(f"import-metrics: {exc}") from None


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
