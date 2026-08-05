"""Read-only run diagnostics: `replay` / `speculation-gate` / `timings` / `inspect` / `tensorboard`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2), then split again (doc 25 CT-01):
the module had grown to 1700 lines across three unrelated domains while its docstring still claimed
four commands. What remains here is what the docstring always described — pure folds of a single
run's event log plus viewers over that run's sidecars. The Part IV concept/novelty diagnostics moved
to `concept_cmds.py`; the cross-run governance commands (durable writes and paid LLM stewards) moved
to `governance_cmds.py`, where the fact that they MUTATE is stated in the header rather than buried
200 lines below a "read-only" claim.

Read-only EXCEPT `speculation-gate`, which atomically writes a local quality receipt without
changing any source run.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_BUDGET
from looplab.cli import _RUN_DIR_HINT, _print_result, _require_run_dir, app


@app.command()
def replay(run_dir: Path = typer.Argument(...)):
    """Pure fold of the event log -> current state (read-only)."""
    store = _require_run_dir(run_dir)
    state = fold(store.read_all())
    typer.echo(orjson.dumps(state.model_dump(mode="json"),
                            option=orjson.OPT_INDENT_2).decode())


@app.command(name="speculation-gate")
def speculation_gate(
    run_dirs: list[Path] = typer.Argument(
        ...,
        help=("Exactly three alternating BASELINE TREATMENT pairs (six directories), one "
              "pair for each fixed seed 0, 1 and 2. Each baseline uses depth 0 and each "
              "treatment the same positive depth."),
    ),
    output: Path = typer.Option(
        Path("speculation-quality.receipt.json"),
        "--output", "-o",
        help="Local receipt path to write atomically after every fixed gate passes.",
    ),
):
    """Run scorer-fidelity plus paired real-GPU search-quality gates for Card speculation."""
    if len(run_dirs) != 6:
        typer.echo(
            "speculation-gate requires exactly three alternating BASELINE TREATMENT pairs",
            err=True,
        )
        raise typer.Exit(2)

    from looplab.search.speculation_quality import (
        publish_speculation_gate_receipt,
        speculation_quality_gate,
    )

    pairs = list(zip(run_dirs[0::2], run_dirs[1::2]))
    report = speculation_quality_gate(pairs, require_gpu=True)
    if report.get("passed") is not True:
        typer.echo(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
        typer.echo("speculation quality gate failed; no receipt was written", err=True)
        raise typer.Exit(2)
    try:
        # PUBLISH the report just computed rather than calling `write_speculation_gate_receipt`,
        # which would run the whole gate a second time — six run directories re-parsed and the
        # scorer matrix re-executed, purely to reproduce a body this frame is already holding
        # (doc 25 SE-01). The published bytes are byte-identical either way.
        receipt = publish_speculation_gate_receipt(output, report)
    except (OSError, ValueError) as exc:
        typer.echo(f"could not publish speculation gate receipt: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(orjson.dumps({
        "passed": True,
        "receipt": str(output.resolve()),
        "self_digest": receipt["self_digest"],
        "implementation_digest": receipt["implementation_digest"],
        "environment_sha256": receipt["environment_sha256"],
        "gpu_inventory": receipt["gpu_inventory"],
        "policy_scope": receipt["policy_scope"],
        "workload_scope": receipt["workload_scope"],
        "calibration_seeds": receipt["calibration_seeds"],
        "task_profile_sha256": receipt["task_profile_sha256"],
        "admitted_depth": receipt["admitted_depth"],
        "admitted_max_nodes": receipt["admitted_max_nodes"],
        "runtime_scope_sha256": receipt["runtime_scope_sha256"],
        "calibration_profile_digest": receipt["calibration_profile_digest"],
        "aggregates": receipt["aggregates"],
    }, option=orjson.OPT_INDENT_2).decode())


def _span_category(sp: dict) -> str:
    """The report row a span belongs to. Same vocabulary for the per-node and the run-level section,
    deliberately: an operator reading `LLM` / `tools` / `eval` / `repair` / `op:<name>` under a node
    must not have to learn a second vocabulary to read the run-level block below it."""
    k = sp.get("kind")
    if k == "generation":
        return "LLM"
    if k == "tool":
        return "tools"
    if k == "operation":
        nm = str(sp.get("name") or "")
        if "eval" in nm:
            return "eval"
        if "repair" in nm:
            return "repair"
        return f"op:{nm}" if nm else "op"
    return k or "other"


def _span_seconds(value) -> float:
    """A span's `duration_s`/`start` as a usable non-negative float, else 0.0.

    A span line can be a well-formed JSON object with a junk field — `spans.jsonl` is written by a
    tracer that promises never to raise into the operation it observes (`default=str` in its
    exporter), so a stray non-numeric duration reaches a reader intact. A bare `float()` on it would
    take down the whole report over one bad span, which is the failure this command's lenient read
    exists to prevent.
    """
    if type(value) not in (int, float):        # `type`, not isinstance: `float(True)` is 1.0
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _traced_seconds(intervals: list) -> float:
    """Wall-clock seconds during which AT LEAST ONE span was open — the union of `[start, start+dur)`.

    Not the sum: the engine builds, evaluates and researches concurrently (`llm_parallel`,
    `eval_parallel`, the background research task), so summing self-time can exceed the run's own
    wall clock and would make the residual below go negative. A union cannot, so `wall - traced` is
    always a real, non-negative quantity: the time no span covered.
    """
    total = 0.0
    cur_start = cur_end = None
    for start, end in sorted(intervals):
        if cur_start is None:
            cur_start, cur_end = start, end
        elif start > cur_end:                 # a genuine gap — bank the block and open a new one
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_start is not None:
        total += cur_end - cur_start
    return total


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def _echo_section(title: str, cats: dict, note: str = "") -> None:
    """One `node N`/`run-level` block: total, then its rows biggest-first with a share of the block."""
    total = sum(v[0] for v in cats.values()) or 1.0
    typer.echo(f"\n{title} — {_minutes(total)} min:" + (f"   {note}" if note else ""))
    for cat, (secs, n) in sorted(cats.items(), key=lambda x: -x[1][0]):
        typer.echo(f"  {cat:10} {_minutes(secs):>6} min  ({n} spans, {round(100*secs/total)}%)")


@app.command()
def timings(run_dir: Path = typer.Argument(...),
            node: Optional[int] = typer.Option(None, help="only this node id")):
    """Where the wall-clock went: per node, then RUN-LEVEL, reconciled against the run's real duration.

    Rows are LLM generations vs eval vs repair vs tools, charged from each span's `duration_s` in
    `spans.jsonl`. The run-level section is the work no node owns — Researcher, Strategist, lesson
    passes, run report, Card-build producer. The reconciliation prints `attributed` (the rows above,
    which can exceed 100% under concurrency), `traced` (wall clock with any span open) and
    `untraced`, the honestly-unattributable remainder.

    Needs `spans.jsonl` for the breakdown; without it the run's duration is still reported, then
    exit 2. Damaged span lines are stepped over and counted.
    """
    # Two things this used to get wrong, both measured on real runs — kept here rather than in the
    # docstring, which Typer prints as `--help`:
    #
    # * It DROPPED every span with no `node_id`. That is not a rare edge — it is all the work that
    #   belongs to the RUN rather than to any one node (a Card producer's node does not exist yet,
    #   which is exactly why per-node attribution is wrong for it). On a preserved 27.9-minute run
    #   this hid 143 of 174 spans: the report totalled 2.7 minutes and its LLM rows 0.2 minutes
    #   against a cost ledger of 139 calls / 780k tokens.
    # * It reconciled against nothing, so nobody could see what it was still missing. The denominator
    #   is now the run's own wall clock from `events.jsonl` (`run_wall_clock_seconds` — correct
    #   across a stop-then-`finalize` process boundary, unlike `budget.elapsed_s` on an old log), and
    #   the residual is PRINTED rather than left implicit. It is real: work with no span at all,
    #   engine bookkeeping, provider waits, and the idle gap of a stopped run awaiting its wrap-up.
    #
    # `spans.jsonl` is a high-volume sidecar (the reason `events/span_index.py` exists). It is read
    # WHOLE, so peak memory tracks the file: the accelerated index is deliberately not used here
    # because building it WRITES `spans.index.jsonl`, and this command is read-only.
    import json as _json
    from collections import defaultdict

    from looplab.events.eventstore import read_jsonl_lenient_with_health
    from looplab.events.replay import run_wall_clock_seconds

    ev_path = run_dir / "events.jsonl"
    sp_path = run_dir / "spans.jsonl"
    # Deliberately NOT `_require_run_dir` (doc 25 CT-13): this is the one read command whose input is
    # EITHER file — it reported timings for a spans-only directory before the event log became its
    # denominator, and requiring `events.jsonl` would retire that. It borrows the shared `_RUN_DIR_HINT`
    # so the ADVICE is still spelled once, and keeps the shared message shape.
    if not ev_path.exists() and not sp_path.exists():
        typer.echo(f"no run found at {run_dir} (no events.jsonl or spans.jsonl). {_RUN_DIR_HINT}")
        raise typer.Exit(2)

    # The DENOMINATOR, and the one number here that does not come from spans: the log's own first-to-
    # last timestamp. Read before the spans so a run with tracing off still learns how long it took.
    wall = None
    budget_elapsed = None
    if ev_path.exists():
        events = EventStore(ev_path).read_all()
        wall = run_wall_clock_seconds(events)
        for e in events:                      # the durable receipt, for cross-check (last one wins)
            if e.type == EV_BUDGET:
                budget_elapsed = (e.data or {}).get("elapsed_s")
    if wall is not None:
        cross = ""
        # A pre-fix log's receipt measured the finalizing PROCESS, so it can be orders of magnitude
        # short. Showing both is what makes that visible instead of leaving two numbers to disagree
        # in different places. Only flagged when they actually differ by more than rounding.
        if type(budget_elapsed) in (int, float) and math.isfinite(budget_elapsed):
            cross = (f"   (budget.elapsed_s says {_minutes(float(budget_elapsed))} min)"
                     if abs(float(budget_elapsed) - wall) > 1.0
                     else "   (budget.elapsed_s agrees)")
        typer.echo(f"run wall clock {_minutes(wall)} min "
                   f"({round(wall, 1)} s, events.jsonl first -> last timestamp)" + cross)
    elif ev_path.exists():
        typer.echo("run wall clock unknown (no event carries a usable timestamp).")

    if not sp_path.exists():
        typer.echo(f"no spans.jsonl at {run_dir} (tracing off or pre-tracing run).")
        raise typer.Exit(2)

    # skip-and-continue (not iter_jsonl's stop-at-first-bad): a mid-file corrupt span line must
    # cost one span, not truncate every later span out of the report. Keep dicts_only=True (the
    # default): a valid-JSON-but-NON-dict corrupt line (e.g. a bare `123`) must be SKIPPED like any
    # other damaged line — with dicts_only=False it'd survive and the `sp.get(...)` accesses below
    # would raise AttributeError, crashing the whole command (worse than the truncation this avoids).
    # `_with_health` is the same read plus the receipt for what it stepped over: this command now
    # publishes a residual, and an unreported dropped line would be charged to the run as idle time.
    spans, health = read_jsonl_lenient_with_health(sp_path, loads=_json.loads, errors="replace")
    # An operation span's recorded duration INCLUDES every nested span (create_node ⊃ implement ⊃
    # stages/plan ⊃ the generations inside them), so summing raw durations counted the nested
    # phases twice or thrice and skewed every percentage. Charge each op its SELF time only
    # (duration minus its DIRECT children); leaf generations/tools keep their full duration.
    child_sum: dict = defaultdict(float)
    for sp in spans:
        parent = sp.get("parent_id")
        if isinstance(parent, str) and parent:   # a non-string id is damage, not a parent link
            child_sum[parent] += _span_seconds(sp.get("duration_s"))

    per_node: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    # A separate dict, NOT a sentinel node id: `-1` is a REAL node id here (the run-setup span uses
    # it), so any in-band marker would silently merge run-level work into a node's row.
    run_level: dict = defaultdict(lambda: [0.0, 0])
    attributed = 0.0
    intervals: list = []
    no_start = 0
    for sp in spans:
        raw = _span_seconds(sp.get("duration_s"))
        dur = raw
        if sp.get("kind") == "operation":
            span_id = sp.get("span_id")
            dur = max(0.0, raw - child_sum.get(span_id if isinstance(span_id, str) else "", 0.0))
        attributed += dur
        # The union needs the span's absolute placement, which is `start` (a wall clock, while
        # `duration_s` is a monotonic delta — see core/tracing.py), and its RAW duration: the span
        # was genuinely open that long, and its children's coverage is already inside that window.
        # An old/degraded span without a `start` still contributes its self time above; it just
        # cannot say WHEN, so it is counted and named rather than quietly dropped from both.
        start = _span_seconds(sp.get("start"))
        if start:
            intervals.append((start, start + raw))
        else:
            no_start += 1
        nid = (sp.get("attributes") or {}).get("node_id")
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            nid = None
        if nid is None:
            if node is None:
                cell = run_level[_span_category(sp)]
                cell[0] += dur
                cell[1] += 1
            continue
        if node is not None and nid != node:
            continue
        cell = per_node[nid][_span_category(sp)]
        cell[0] += dur
        cell[1] += 1

    for nid in sorted(per_node):
        _echo_section(f"node {nid}", per_node[nid])
    if run_level:
        _echo_section("run-level", run_level,
                      note="(no node owns it: researcher / strategist / card producer / wrap-up)")

    if node is not None:
        typer.echo(f"\n(--node {node}: run-level work and the reconciliation below are run-scope "
                   f"and are omitted; drop --node to see them.)")
        return
    if health.get("invalid_lines"):
        typer.echo(f"\nspans.jsonl: {health['invalid_lines']} of {health['source_lines']} lines "
                   f"unreadable and skipped — their time falls into `untraced` below.")
    if no_start:
        typer.echo(f"spans.jsonl: {no_start} spans carry no `start` — counted in `attributed`, "
                   f"absent from `traced`.")
    if wall is None or wall <= 0:
        return
    traced = min(_traced_seconds(intervals), wall)
    untraced = max(0.0, wall - traced)
    typer.echo(f"\nreconciliation vs {_minutes(wall)} min wall clock:")
    typer.echo(f"  attributed {_minutes(attributed):>6} min  ({round(100*attributed/wall)}%)  "
               f"sum of the rows above; overlaps under concurrency")
    typer.echo(f"  traced     {_minutes(traced):>6} min  ({round(100*traced/wall)}%)  "
               f"wall clock with at least one span open")
    typer.echo(f"  untraced   {_minutes(untraced):>6} min  ({round(100*untraced/wall)}%)  "
               f"no span open — not attributable from spans.jsonl")


@app.command()
def inspect(run_dir: Path = typer.Argument(...)):
    """Show the raw launch config snapshot + the run's current folded best result.

    Seven selection-treatment settings are committed by ``run_started`` and can therefore differ from
    an old or hand-edited snapshot. The owner config API overlays those effective folded values;
    this diagnostic deliberately prints the on-disk snapshot verbatim for inspection.
    """
    snap = run_dir / "config.snapshot.json"
    events = run_dir / "events.jsonl"
    # Tolerate a run that crashed after writing config.snapshot.json but before its first event: still
    # show the config. Only error when the dir is neither — a typo'd path, not a real (if partial) run.
    if not snap.exists() and not events.exists():
        typer.echo(f"no run found at {run_dir} (no config.snapshot.json or events.jsonl).")
        raise typer.Exit(2)
    if snap.exists():
        typer.echo(snap.read_text(encoding="utf-8"))
    if events.exists():
        _print_result(fold(EventStore(events).read_all()))


@app.command()
def tensorboard(
    run_dir: Path = typer.Argument(..., help="Run dir; its nodes/ hold each experiment's training logs."),
    port: int = typer.Option(6006, help="Port to serve on."),
    host: str = typer.Option(
        "127.0.0.1",
        help="Bind address. Defaults to localhost — TensorBoard has NO auth, so an experiment's "
             "training logs (and any secret a script printed into them) must not be exposed on all "
             "interfaces by default. Pass --host 0.0.0.0 explicitly to bind all interfaces."),
):
    """Serve TensorBoard over a run's per-node training logs — online curves for ALL metrics the
    training framework logged (loss, recall@k, grad norms, lr, …), one comparable run per experiment.
    RepoTask training scripts (e.g. PyTorch Lightning's TensorBoardLogger) write event files under each
    node's workdir; this points TensorBoard at nodes/ so every node shows up."""
    import shutil
    import subprocess
    import sys
    logdir = run_dir / "nodes"
    if not logdir.exists():
        logdir = run_dir
    exe = shutil.which("tensorboard")
    cmd = ([exe] if exe else [sys.executable, "-m", "tensorboard.main"]) + \
          ["--logdir", str(logdir), "--port", str(port), "--host", host]
    typer.echo(f"Serving TensorBoard for {run_dir} on http://{host}:{port}  (logdir={logdir})")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
