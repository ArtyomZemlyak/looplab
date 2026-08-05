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

from pathlib import Path
from typing import Optional

import orjson
import typer

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.cli import _print_result, _require_run_dir, app


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
        speculation_quality_gate,
        write_speculation_gate_receipt,
    )

    pairs = list(zip(run_dirs[0::2], run_dirs[1::2]))
    report = speculation_quality_gate(pairs, require_gpu=True)
    if report.get("passed") is not True:
        typer.echo(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
        typer.echo("speculation quality gate failed; no receipt was written", err=True)
        raise typer.Exit(2)
    try:
        receipt = write_speculation_gate_receipt(
            output,
            pairs,
            require_gpu=True,
        )
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


def _run_wall_seconds(run_dir: Path) -> float:
    """First-to-last event timestamp, or 0.0 when the log is missing/unreadable.

    Deliberately the EVENT log rather than the span file: spans exist only where the code was
    instrumented, so asking them how long the run took can only ever return "as long as the parts I
    measured", which is the exact circularity that hid the gap this number exists to expose.
    """
    log = run_dir / "events.jsonl"
    if not log.exists():
        return 0.0
    first = last = None
    try:
        from looplab.events.eventstore import read_jsonl_lenient
        for row in read_jsonl_lenient(log, errors="replace"):
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            if first is None:
                first = ts
            last = ts
    except OSError:
        return 0.0
    if first is None or last is None or last <= first:
        return 0.0
    return float(last - first)


@app.command()
def timings(run_dir: Path = typer.Argument(...),
            node: Optional[int] = typer.Option(None, help="only this node id")):
    """Where the wall-clock went, per node: LLM generations vs eval vs repair vs tools (from spans.jsonl
    `duration_s`). Answers 'what is this run actually spending time on right now' at a glance."""
    import json as _json
    from collections import defaultdict
    sp_path = run_dir / "spans.jsonl"
    if not sp_path.exists():
        typer.echo(f"no spans.jsonl at {run_dir} (tracing off or pre-tracing run).")
        raise typer.Exit(2)

    def _cat(sp: dict) -> str:
        k = sp.get("kind")
        if k == "generation":
            return "LLM"
        if k == "tool":
            return "tools"
        if k == "operation":
            nm = str(sp.get("name") or "")
            # `evaluate` is the node's ROOT span: it wraps seed_workspace, every stage, triage and
            # every repair, so after the self-time subtraction below its remainder is scheduling
            # overhead and nothing else. Bucketing it as "eval" printed `eval 0.0 min` next to a
            # measured 100.1 s evaluation (rubert-dr-0805 node 0, whose real subprocess time is the
            # `op:train` row) — a number that read as "the eval was free". Name it for what the row
            # actually holds and leave "eval" to the stage rows that hold the eval.
            if nm == "evaluate":
                return "op:evaluate(self)"
            if "eval" in nm:
                return "eval"
            if "repair" in nm:
                return "repair"
            return f"op:{nm}" if nm else "op"
        return k or "other"

    def _mins(secs: float) -> str:
        """Minutes for anything a human reads in minutes, seconds below that.

        `round(secs/60, 1)` alone printed `0.0 min` for EVERY row of a run that took six seconds,
        and for the sub-3 s rows of long runs — a resolution failure that is indistinguishable from
        "no work happened", which is exactly the reading this command exists to prevent.
        """
        if secs >= 60.0:
            return f"{round(secs / 60, 1)} min"
        # A positive duration must never print as a flat `0`: 94 lessons spans really do sum to
        # under a millisecond, and "0" is the one rendering that says they did not happen at all.
        if 0.0 < secs < 0.01:
            return "<0.01 s"
        return f"{round(secs, 2)} s"

    from looplab.events.eventstore import read_jsonl_lenient
    # skip-and-continue (not iter_jsonl's stop-at-first-bad): a mid-file corrupt span line must
    # cost one span, not truncate every later span out of the report. Keep dicts_only=True (the
    # default): a valid-JSON-but-NON-dict corrupt line (e.g. a bare `123`) must be SKIPPED like any
    # other damaged line — with dicts_only=False it'd survive and the `sp.get(...)` accesses below
    # would raise AttributeError, crashing the whole command (worse than the truncation this avoids).
    spans = read_jsonl_lenient(sp_path, loads=_json.loads, errors="replace")
    # An operation span's recorded duration INCLUDES every nested span (create_node ⊃ implement ⊃
    # stages/plan ⊃ the generations inside them), so summing raw durations counted the nested
    # phases twice or thrice and skewed every percentage. Charge each op its SELF time only
    # (duration minus its DIRECT children); leaf generations/tools keep their full duration.
    child_sum: dict = defaultdict(float)
    for sp in spans:
        if sp.get("parent_id"):
            child_sum[sp["parent_id"]] += float(sp.get("duration_s") or 0.0)

    # Attribution is `traceview.effective_node_id` — the ONE rule the server already applies, not a
    # private re-derivation. Reading only `attributes.node_id` and dropping everything else silently
    # discarded 616 of rubert-dr-0805's 881 spans (915.8 s, 41% of that run's wall clock): the
    # Researcher/Boss tool loops, propose, report and lessons stamp their node on the TRACE ROOT, so
    # under the old rule the command that answers "where did the time go" was the one place that time
    # went nowhere. Spans with neither id are real work too, so they get a visible `run` bucket
    # instead of vanishing.
    from looplab.events.traceview import effective_node_id, trace_root_node_id
    by_trace: dict = defaultdict(list)
    for sp in spans:
        by_trace[sp.get("trace_id")].append(sp)
    root_nid = {tid: trace_root_node_id(sps) for tid, sps in by_trace.items()}

    per_node: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    charged = 0.0
    for sp in spans:
        nid = effective_node_id(sp, root_nid.get(sp.get("trace_id")))
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            nid = None                    # run-level work: no node stamped anywhere in its trace
        if node is not None and nid != node:
            continue
        dur = float(sp.get("duration_s") or 0.0)
        if sp.get("kind") == "operation":
            dur = max(0.0, dur - child_sum.get(sp.get("span_id") or "", 0.0))
        cell = per_node[nid][_cat(sp)]
        cell[0] += dur
        cell[1] += 1
        charged += dur

    # `-1` is the real setup pseudo-node; `None` is "no node anywhere in the trace". Sort with None
    # last rather than letting `sorted` raise on the mixed key type.
    for nid in sorted(per_node, key=lambda x: (x is None, x)):
        cats = per_node[nid]
        total = sum(v[0] for v in cats.values()) or 1.0
        label = "run (no node)" if nid is None else f"node {nid}"
        typer.echo(f"\n{label} — {_mins(total)}:")
        for cat, (secs, n) in sorted(cats.items(), key=lambda x: -x[1][0]):
            typer.echo(f"  {cat:20} {_mins(secs):>10}  ({n} spans, {round(100*secs/total)}%)")

    # The headline this command's docstring promises. Spans cover only the instrumented parts of a
    # run — measured, 22-31% of real wall clock — and printing the covered part alone as "where the
    # wall-clock went" is what made an unmeasured hour look like it never happened. Name the gap.
    if node is None:
        wall = _run_wall_seconds(run_dir)
        if wall:
            typer.echo(f"\nrun wall clock — {_mins(wall)} (first to last event)")
            typer.echo(f"  measured by spans      {_mins(charged)}  ({round(100*charged/wall)}%)")
            typer.echo(f"  not instrumented       {_mins(max(0.0, wall - charged))} "
                       f"({round(100*max(0.0, wall - charged)/wall)}%)  "
                       "— setup, queueing, pauses and any phase without a span")


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
