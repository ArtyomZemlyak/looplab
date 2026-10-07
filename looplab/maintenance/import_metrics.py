"""Import metrics an operator measured AFTER the run, beside each node's live ones.

WHY (incident 2026-10-06). A run's nodes were scored at @20 while it ran; afterwards a scoring
service re-scored their checkpoints at @200, and the operator wanted to rank the run by @200
(`metric_retarget`). Nothing could carry those numbers into the record: `backfill-score-metrics`
recovers only what the run's OWN `score.log` printed, and only into an EMPTY map — every one of these
nodes already carried its @20 extras. A retarget then unranked every node scored before the scorer
printed @200.

WHAT IT DOES. `looplab import-metrics RUN FILE --source "…"` reads `{node_id: {key: value}}` (or
`{"nodes": {…}}`) and plans one `extra_metrics_imported` row per node with the keys that node does NOT
already carry, bound to its CURRENT lifecycle. A DRY RUN by default; `--apply` appends while holding
the run's `engine.lock` (`backfill_applied_params.offline_run`, the engine's own liveness rule), so it
never writes beside an engine.

WHAT IT WILL NOT DO.
* OVERWRITE a live value: a key the node carries is skipped, here and again in the fold.
* ORIENT an axis: no direction is written, as with every reconstruction (the backfill docstring).
* PRETEND IT WAS MEASURED LIVE: the fold marks each imported key in the reconstruction marker, and
  `--source` (what produced the numbers) is required and kept on the row.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def read_metrics_file(path: Path) -> dict[int, dict[str, float]]:
    """`{node_id: {key: value}}` from FILE. Raises ValueError naming the first bad entry."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("nodes"), dict):
        data = data["nodes"]
    if not isinstance(data, dict):
        raise ValueError("the file must be a JSON object {node_id: {metric: value}}")
    out: dict[int, dict[str, float]] = {}
    for raw_id, metrics in data.items():
        try:
            node_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError(f"node id {raw_id!r} is not an integer") from None
        if not isinstance(metrics, dict) or not metrics:
            raise ValueError(f"node {node_id}: expected a non-empty {{metric: value}} object")
        clean: dict[str, float] = {}
        for key, value in metrics.items():
            if (not isinstance(key, str) or not key.strip() or isinstance(value, bool)
                    or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"node {node_id}: {key!r}={value!r} is not a finite number")
            clean[key.strip()] = float(value)
        out[node_id] = clean
    return out


def plan_import(run_dir: Path, metrics: dict[int, dict[str, float]], *, source: str,
                precision: Optional[int] = None) -> tuple[list[dict], list[str]]:
    """`(rows, notes)`: the rows an `--apply` appends, and one line per node it skips and why."""
    state = fold(EventStore(str(Path(run_dir) / "events.jsonl")).read_all())
    rows, notes = [], []
    now = time.time()
    for node_id in sorted(metrics):
        node = state.nodes.get(node_id)
        if node is None:
            notes.append(f"node {node_id}: no such node")
            continue
        if node.task_metric is None:
            notes.append(f"node {node_id}: not evaluated in its current lifecycle")
            continue
        live = set(node.extra_metrics or {})
        fresh = {k: v for k, v in metrics[node_id].items() if k not in live}
        kept = sorted(set(metrics[node_id]) & live)
        if kept:
            notes.append(f"node {node_id}: already carries {', '.join(kept)} — kept, not overwritten")
        if not fresh:
            continue
        row = {"node_id": node_id, "generation": node.attempt, "extra_metrics": fresh,
               "source": source[:400], "imported_at": round(now, 3)}
        if precision is not None:
            row["precision_decimals"] = {k: int(precision) for k in fresh}
        rows.append(row)
    return rows, notes


def import_metrics(run_dir: Path, file: Path, *, source: str, apply: bool,
                   precision: Optional[int] = None) -> str:
    """Plan (and with `apply`, append) the import. Returns the report."""
    from looplab.events.types import EV_EXTRA_METRICS_IMPORTED
    from looplab.maintenance.backfill_applied_params import offline_run
    if not source or not source.strip():
        raise ValueError("--source is required: say what measured these numbers")
    metrics = read_metrics_file(file)
    with offline_run(Path(run_dir), hold=apply) as refusal:
        if refusal:
            return f"{run_dir}: skipped — {refusal}"
        rows, notes = plan_import(Path(run_dir), metrics, source=source.strip(),
                                  precision=precision)
        if apply and rows:
            store = EventStore(str(Path(run_dir) / "events.jsonl"))
            for row in rows:
                store.append(EV_EXTRA_METRICS_IMPORTED, {
                    "node_id": row["node_id"], "generation": row["generation"],
                    "extra_metrics": row["extra_metrics"], "source": row["source"],
                    "imported_at": row["imported_at"],
                    **({"precision_decimals": row["precision_decimals"]}
                       if "precision_decimals" in row else {})})
    verb = "imported" if apply else "would import (dry run; --apply to write)"
    lines = [f"{run_dir}: {verb} {sum(len(r['extra_metrics']) for r in rows)} value(s) "
             f"on {len(rows)} node(s)"]
    lines += [f"  {n}" for n in notes]
    return "\n".join(lines)
