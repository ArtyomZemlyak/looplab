"""Recover the objectives the score stage MEASURED and the record threw away.

THE DEFECT
----------
A vecsearch score stage computes a whole IR suite and prints it — Recall@k, nDCG@k, MAP@k, MRR@k and
Precision@k at seven cutoffs, **36 numbers** — and the run keeps ONE. Measured over every `*.jsonl`
under `runs/` (131 files, 15 run directories): 400 records carry a non-empty `extra_metrics`, 1,600
values, 4 keys — `speculation_cuda_probe_v`, `device_count`, `alloc_bytes`, `device_ordinal` — all
of them the engine's own CUDA-probe telemetry in the `specgate*` toys. **No experiment node has ever
recorded a second OBJECTIVE.** All 8 evaluated nodes across the two real task families record
`extra_metrics == {}`.

The mechanism is not a bug, it is an unused door. `runtime/command_eval.py` fills `extra_metrics`
from two channels: `auto`, which scrapes every other numeric key off the metric's own JSON line —
and fires ONLY when `metric.kind == "stdout_json"`, while these tasks read by `stdout_regex`, so it
is structurally off — and `declared`, the operator's own `eval.metrics` reader specs, which no task
on disk has ever populated. So the numbers were computed, printed, preserved in `score.log`, and
never entered the record.

WHAT THIS COSTS, CONCRETELY. `e5small-dr-unified-v4` node 3 scored 0.790898 against node 1's
0.764853, and until this ran, nothing could say whether that was a recall@100 artifact. Its own
score.log answers: MAP@100 0.34 vs 0.29, nDCG@100 0.46 vs 0.41, MRR@100 0.41 vs 0.35 — it leads
every family, not one. That is a fact the record already owned and could not state.

WHAT IT WILL AND WILL NOT DO
----------------------------
* APPEND-ONLY, and a LIVE RECORD ALWAYS WINS. One `score_metrics_backfilled` event per node; the
  fold applies it only where the node's `extra_metrics` is empty, so a measurement taken while the
  run was happening can never be overwritten by a reconstruction — which is also what makes a
  second run of this command a no-op, by construction rather than by a check that could drift.
* NEVER ORIENTS AN AXIS. It writes values and NO `extra_metrics_direction`, deliberately. Nobody
  declared which way was better when those evals ran, and orientation is a forward-looking
  declaration an operator makes in `eval.metrics`; asserting it retroactively would be exactly the
  reconstruction-presented-as-measurement failure this family of tools exists to refuse. The
  consequence is intended and safe: `ui/src/panels.jsx::paretoFront` declines to RANK an axis it
  cannot orient, so these values are audit — readable everywhere, deciding nothing.
* IS HONEST ABOUT PRECISION. The suite is printed to TWO DECIMALS while the primary is read at six,
  so neighbouring nodes tie: v4 nodes 0 and 1 differ by 0.006 on recall@100 and are identical on
  every recovered metric. Recorded as `precision_decimals` on every row, because "these two nodes
  are equal on nDCG" and "the print statement cannot tell them apart" are different claims and a
  reader must not have to guess which one they are reading.
* REFUSES A LIVE RUN, for the same reason its sibling does: a `score.log` still being written
  describes nothing yet.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

# Why a node can have no answer. A closed vocabulary, for the same reason
# `backfill_applied_params` keeps one: "unrecoverable" with no reason is the vacuous record this
# family of tools exists to abolish.
NO_SCORE_LOG = "score_log_absent"
NO_METRICS_IN_LOG = "score_log_reports_no_metric_block"
ALREADY_RECORDED = "node_already_carries_extra_metrics"

# `NAME: 0.42`, with the loguru prefix (`... | INFO | mod:fn:56 - `) stripped by the ` - ` split.
# Anchored at both ends so a sentence mentioning a metric cannot match, and the name must carry an
# explicit cutoff (`_at_100` / `@100`) so a stray `Processing: 41` — 100 of which sit in every one
# of these logs — cannot be read as an objective. That exclusion is not hypothetical: it is the
# single largest source of numeric lines in the file.
_ROW = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*(?:_at_|@)\d+)\s*:\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_score_log(text: str) -> tuple[dict[str, float], dict[str, int]]:
    """Every `NAME_at_K: value` row of a score log, and the decimals EACH was printed at.

    LAST WRITER WINS on a repeated name, which is the same rule the trajectory readers apply to a
    repaired stage log: these files are opened append-mode, so a re-run's block follows its
    predecessor's and the final block is the one describing the artifact that survives.

    PER-KEY AND NOT ONE NUMBER FOR THE FILE, because one number is a lie here and the lie flatters
    the data. These logs print the suite at 2 decimals and the PRIMARY at 6 — `Recall_at_100: 0.79`
    and `RECALL@100: 0.790898` are the same quantity twice — so a file-level maximum reports "6
    decimals" about a record that is 2 decimals wide almost everywhere. That difference is exactly
    what a reader needs: v4 nodes 0 and 1 differ by 0.006 on the primary and are IDENTICAL on every
    2-decimal row, and "these nodes are equal on nDCG" is a different claim from "the print
    statement cannot tell them apart".
    """
    found: dict[str, float] = {}
    decimals: dict[str, int] = {}
    for line in text.splitlines():
        body = line.split(" - ")[-1] if " - " in line else line
        m = _ROW.match(body)
        if not m:
            continue
        try:
            found[m.group(1)] = float(m.group(2))
        except ValueError:
            continue
        decimals[m.group(1)] = len(m.group(2).partition(".")[2])
    return found, decimals


def _score_log(run_dir: Path, node_id: int) -> Optional[Path]:
    """This node's preserved score log, or None. `nodes/node_<id>/score.log` is where the stage
    tees; a node that never reached `score` simply has none."""
    path = run_dir / "nodes" / f"node_{node_id}" / "score.log"
    return path if path.is_file() else None


def readable_horizon(run_dir: Path) -> tuple[int, int]:
    """This run's `(events served, lines on disk)`, from the STORE that owns the question.

    Thin on purpose. `EventStore.readable_horizon` is where the rule lives — `read_all` stops at the
    first logical-sequence gap, and a bounded reader that does not name its bound reads as complete
    coverage. Two copies of that number is the drift `tests/test_shared_identity_rules.py` refuses.
    """
    return EventStore(str(run_dir / "events.jsonl")).readable_horizon()


def plan_run(run_dir: Path) -> list[dict]:
    """One row per node that WOULD be written, without writing anything.

    Rows carry the exact event payload, so `--dry-run` shows the real thing rather than a summary
    of it. A node whose `extra_metrics` is already non-empty is skipped here AND declined by the
    fold: the two agree on purpose, so the dry run cannot promise a write the fold would refuse.
    """
    store = EventStore(str(run_dir / "events.jsonl"))
    state = fold(store.read_all())
    rows: list[dict] = []
    for node_id in sorted(state.nodes):
        node = state.nodes[node_id]
        if node.metric is None:
            continue                      # no metric, no scored artifact to describe
        row = {"node_id": node_id, "generation": getattr(node, "attempt", 0),
               "read_at": time.time(), "extra_metrics": {}, "precision_decimals": {},
               "unrecoverable": ""}
        if node.extra_metrics:
            row["unrecoverable"] = ALREADY_RECORDED
            rows.append(row)
            continue
        log = _score_log(run_dir, node_id)
        if log is None:
            row["unrecoverable"] = NO_SCORE_LOG
            rows.append(row)
            continue
        found, decimals = parse_score_log(log.read_text(errors="replace"))
        if not found:
            row["unrecoverable"] = NO_METRICS_IN_LOG
        else:
            row["extra_metrics"] = found
            row["precision_decimals"] = decimals
        rows.append(row)
    return rows


def apply_run(run_dir: Path, rows: list[dict]) -> int:
    """Append the planned rows. Returns how many were written."""
    from looplab.events.types import EV_SCORE_METRICS_BACKFILLED
    store = EventStore(str(run_dir / "events.jsonl"))
    for row in rows:
        store.append(EV_SCORE_METRICS_BACKFILLED, row)
    return len(rows)


def summarize(rows: list[dict]) -> dict:
    """The numbers, counted the way the record allows them to be counted."""
    recovered = [r for r in rows if r.get("extra_metrics")]
    values = sum(len(r["extra_metrics"]) for r in recovered)
    coarse = sorted({n for r in recovered for n in r.get("precision_decimals", {}).values()})
    return {
        "considered": len(rows),
        "recovered": len(recovered),
        "values": values,
        "unrecoverable": len([r for r in rows if r.get("unrecoverable")]),
        "already": len([r for r in rows if r.get("unrecoverable") == ALREADY_RECORDED]),
        "decimals_seen": coarse,
        "recovered_nodes": [r["node_id"] for r in recovered],
    }


def render(run_name: str, rows: list[dict], summary: dict) -> str:
    out = [f"{run_name}: {summary['considered']} scored node(s), {summary['recovered']} with a "
           f"recoverable metric block, {summary['values']} value(s) total"]
    if summary["decimals_seen"]:
        out.append(f"    printed at {summary['decimals_seen']} decimal place(s) — the coarse end is "
                   "the suite, the fine end is the primary; two nodes equal at 2 decimals are not "
                   "known to be equal")
    for row in rows:
        why = row.get("unrecoverable")
        if why == ALREADY_RECORDED:
            continue                       # a live record. Not a failure — the reason it is skipped.
        if why:
            out.append(f"    node {row['node_id']}: — ({why})")
            continue
        keys = sorted(row["extra_metrics"])
        out.append(f"    node {row['node_id']}: {len(keys)} metrics, e.g. " +
                   ", ".join(f"{k}={row['extra_metrics'][k]}" for k in keys[:3]))
    return "\n".join(out)


def run_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if (p / "events.jsonl").is_file())


def backfill(root: Path, *, dry_run: bool = True, only: Optional[str] = None,
             skip_live: bool = True) -> str:
    """Walk every run under `root`. Returns the report."""
    from looplab.maintenance.backfill_applied_params import _lock_is_live
    out: list[str] = []
    totals = {"considered": 0, "recovered": 0, "values": 0, "written": 0,
              "skipped_live": 0, "bounded": 0}
    for run_dir in run_dirs(root):
        if only and run_dir.name != only:
            continue
        if skip_live and (run_dir / "engine.lock").exists() and _lock_is_live(run_dir):
            out.append(f"{run_dir.name}: SKIPPED — a live engine holds this run. A score log still "
                       "being written describes nothing yet.")
            totals["skipped_live"] += 1
            continue
        # NAMED BEFORE THE EARLY RETURN, not after it. A run whose rows are all already
        # backfilled produces NO rows and would `continue` below — so the one combination a reader
        # most needs ("nothing to do here" AND "only 20 of 1,624 lines are readable") was exactly
        # the one that printed nothing at all. Found by running it, not by reading it.
        served, lines = readable_horizon(run_dir)
        bounded = bool(lines and served < lines)
        if bounded:
            out.append(f"{run_dir.name}: ** BOUNDED — the event store serves {served} of {lines} "
                       "lines; it stops at the first logical-sequence gap. Nodes recorded past that "
                       "point were NOT considered, and any count below is the prefix's, not the "
                       "run's. **")
            totals["bounded"] += 1
        rows = plan_run(run_dir)
        if not rows:
            continue
        summary = summarize(rows)
        out.append(render(run_dir.name, rows, summary))
        for k in ("considered", "recovered", "values"):
            totals[k] += summary[k]
        if not dry_run:
            totals["written"] += apply_run(run_dir, rows)
    out.append("")
    out.append(f"TOTAL: {totals['considered']} scored node(s), {totals['recovered']} recovered, "
               f"{totals['values']} value(s) the record had computed and thrown away"
               + (f", {totals['skipped_live']} run(s) skipped as live" if totals["skipped_live"] else "")
               + (f", {totals['bounded']} run(s) READ ONLY TO A SEQUENCE GAP" if totals["bounded"] else ""))
    out.append("DRY RUN — nothing was written." if dry_run
               else f"WROTE {totals['written']} backfill event(s).")
    return "\n".join(out)
