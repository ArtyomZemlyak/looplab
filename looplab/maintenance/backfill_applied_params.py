"""Repair the historical record: what a node PROPOSED is not what it RAN.

THE DEFECT
----------
`Idea.params` is a PROPOSAL. Under `params_style: "none"` the engine applies nothing — the Developer
realises the idea by EDITING THE REPO — so a deviation is legitimate and expected. What is not
legitimate is that the durable record keeps only the proposal, and every reader downstream (the
distilled lessons, the LLM context handed to the next proposer, the run report, the champion card,
the UI) presents it as the parameters that produced the metric.

Measured over every run on disk: **457 comparisons, 41 diverged (9.0%), 18 of them on nodes that
produced a metric.** The e5 champion at 0.793426 is recorded as batch 8192 / accum 2 / 15 epochs and
ran batch 512 / accum 32 / 3 epochs. That record is what put 8192 into the v3 task goal, and v3 died
with three nodes and no metric. On `rubertlite-dr-unified-v8`'s champion the carrier is not even the
document everyone assumed: `config.yaml` says 8192 while the assignment in `vectorsearch/train.py` says 4096, with
the Developer's reasoning inline — R-Drop's second forward pass makes 8192 OOM even on a 140 GB
H200, so it halves the batch and doubles accumulation, deliberately leaving the document untouched
so the completed `mine` stage stays reusable.

`metric_provenance.applied_params` (merged 2026-08-20) records this for every eval from now on.
Every node evaluated BEFORE that has none, and nothing on disk can be retro-fitted by the engine
itself — the eval is over. This module goes back and reads the workdirs that survive.

WHAT IT WILL AND WILL NOT DO
----------------------------
* APPEND-ONLY. It writes one `applied_params_backfilled` event per node and rewrites nothing. The
  fold applies it at read time and ONLY where the node has no record of its own, so a live
  measurement can never be overwritten by a reconstruction — which is also what makes a second run
  of this command a no-op.
* HONEST ABOUT WHAT IT CANNOT RECOVER. A node whose workdir is gone gets a row saying so. That row
  is the point: "the workdir is gone" and "the proposal is what ran" are opposite statements, and
  the second is the one every reader currently makes by default.
* NEVER GUESSES. `bind_applied_params` reports a coordinate two carriers disagree about as a
  CONFLICT rather than picking one — on the v8 champion that is exactly `train.training.batch_size`,
  where the config document says 8192 and the training script's own assignment says 4096 — and this
  module passes that through untouched, each reading with the file and line it was read at.
* REFUSES A LIVE RUN. The workdir of a node that is training right now is being written to, and a
  reading taken mid-write describes nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_APPLIED_PARAMS_BACKFILLED
from looplab.runtime.applied_params import bind_applied_params

# Why a node can have no answer. A closed vocabulary, because "unrecoverable" with no reason is the
# same vacuous record this whole exercise exists to abolish — a reader has to be able to tell "the
# directory was reaped" from "the node declared nothing numeric to compare".
NO_WORKDIR = "workdir_absent"
NO_DECLARATION = "node_declares_no_numeric_params"
NO_CARRIER = "no_readable_carrier_in_workdir"
NO_METRIC = "node_produced_no_metric"


def _workdir(run_dir: Path, node_id: int) -> Optional[Path]:
    """The node's workdir, or None. `nodes/node_<id>` is the engine's own layout."""
    p = run_dir / "nodes" / f"node_{node_id}"
    return p if p.is_dir() else None


def _digest(workdir: Path) -> str:
    """A cheap identity for the tree the reading was taken from, so a later reader can tell whether
    it is looking at the same bytes. Not a content hash of the tree — the carriers' own digests are
    already inside the record; this only has to change when the tree does."""
    try:
        st = workdir.stat()
        return f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}"
    except OSError:
        return ""


def plan_run(run_dir: Path) -> list[dict]:
    """One row per node that WOULD be written, without writing anything.

    Rows carry `{node_id, generation, applied_params, unrecoverable, read_at, workdir_digest}` —
    exactly the event payload — so `--dry-run` shows the real thing rather than a summary of it.
    """
    store = EventStore(str(run_dir / "events.jsonl"))
    state = fold(store.read_all())
    rows: list[dict] = []
    for node_id in sorted(state.nodes):
        node = state.nodes[node_id]
        prov = node.metric_provenance
        if node.metric is None or not isinstance(prov, dict):
            continue                                   # nothing this node's metric says about itself
        if prov.get("applied_params") is not None:
            continue                                   # already answered — live or already backfilled
        row = {"node_id": node_id, "generation": getattr(node, "attempt", 0),
               "read_at": time.time(), "applied_params": None, "unrecoverable": "",
               "workdir_digest": ""}
        params = getattr(getattr(node, "idea", None), "params", None)
        if not isinstance(params, dict) or not params:
            row["unrecoverable"] = NO_DECLARATION
            rows.append(row)
            continue
        workdir = _workdir(run_dir, node_id)
        if workdir is None:
            row["unrecoverable"] = NO_WORKDIR
            rows.append(row)
            continue
        row["workdir_digest"] = _digest(workdir)
        # The carrier set is the node's OWN file list — the files the Developer wrote — because
        # under `params_style: "none"` the carrier is whatever the Developer chose, and the engine
        # has no other way to know which document is the one that counts.
        record = bind_applied_params(params, workdir, carriers=list(node.files or {}))
        if not record:
            row["unrecoverable"] = NO_CARRIER
        else:
            row["applied_params"] = record
        rows.append(row)
    return rows


def apply_run(run_dir: Path, rows: list[dict]) -> int:
    """Append the planned rows. Returns how many were written."""
    store = EventStore(str(run_dir / "events.jsonl"))
    for row in rows:
        store.append(EV_APPLIED_PARAMS_BACKFILLED, row)
    return len(rows)


def summarize(rows: list[dict]) -> dict:
    """The numbers the operator asked for, counted the way the record allows them to be counted."""
    recovered = [r for r in rows if isinstance(r.get("applied_params"), dict)]
    diverged, conflicted = [], []
    for r in recovered:
        rec = r["applied_params"]
        if rec.get("diverged"):
            diverged.append(r["node_id"])
        if rec.get("conflicts"):
            conflicted.append(r["node_id"])
    return {"considered": len(rows), "recovered": len(recovered),
            "unrecoverable": len(rows) - len(recovered),
            "reasons": {reason: sum(1 for r in rows if r.get("unrecoverable") == reason)
                        for reason in (NO_WORKDIR, NO_DECLARATION, NO_CARRIER, NO_METRIC)
                        if any(r.get("unrecoverable") == reason for r in rows)},
            "diverged_nodes": diverged, "conflicted_nodes": conflicted}


def render(run_name: str, rows: list[dict], summary: dict) -> str:
    out = [f"{run_name}: {summary['considered']} node(s) with a metric and no applied-params record",
           f"  recovered {summary['recovered']}, unrecoverable {summary['unrecoverable']}"
           + (f" ({', '.join(f'{k}={v}' for k, v in summary['reasons'].items())})"
              if summary["reasons"] else "")]
    for r in rows:
        rec = r.get("applied_params")
        if not isinstance(rec, dict):
            out.append(f"  node {r['node_id']}: NOT RECOVERABLE — {r['unrecoverable']}")
            continue
        head = (f"  node {r['node_id']}: {rec.get('authority')} authority, "
                f"{rec.get('checked')} of {rec.get('declared')} declared coordinates answered")
        out.append(head)
        for d in (rec.get("diverged") or [])[:8]:
            out.append(f"      DIVERGED {d.get('param')}: declared {d.get('declared')}, "
                       f"applied {d.get('applied')} at {d.get('match') or d.get('line')}")
        for c in (rec.get("conflicts") or [])[:8]:
            readings = "; ".join(f"{x.get('file')}:{x.get('line')}={x.get('applied')}"
                                 for x in (c.get("readings") or [])[:4])
            out.append(f"      CONFLICT {c.get('param')}: declared {c.get('declared')} — {readings}")
    return "\n".join(out)


def run_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if (p / "events.jsonl").is_file())


def backfill(root: Path, *, dry_run: bool = True, only: Optional[str] = None,
             skip_live: bool = True) -> str:
    """Walk every run under `root`. Returns the report."""
    out: list[str] = []
    totals = {"considered": 0, "recovered": 0, "unrecoverable": 0, "written": 0,
              "diverged": 0, "conflicted": 0, "skipped_live": 0}
    for run_dir in run_dirs(root):
        if only and run_dir.name != only:
            continue
        if skip_live and (run_dir / "engine.lock").exists() and _lock_is_live(run_dir):
            out.append(f"{run_dir.name}: SKIPPED — a live engine holds this run. A workdir being "
                       "written to cannot be read as what ran.")
            totals["skipped_live"] += 1
            continue
        rows = plan_run(run_dir)
        if not rows:
            continue
        summary = summarize(rows)
        out.append(render(run_dir.name, rows, summary))
        totals["considered"] += summary["considered"]
        totals["recovered"] += summary["recovered"]
        totals["unrecoverable"] += summary["unrecoverable"]
        totals["diverged"] += len(summary["diverged_nodes"])
        totals["conflicted"] += len(summary["conflicted_nodes"])
        if not dry_run:
            totals["written"] += apply_run(run_dir, rows)
    out.append("")
    out.append(f"TOTAL: {totals['considered']} considered, {totals['recovered']} recovered, "
               f"{totals['unrecoverable']} unrecoverable, {totals['diverged']} with a coordinate "
               f"that diverged from the proposal, {totals['conflicted']} with carriers that "
               f"disagree with each other"
               + (f", {totals['skipped_live']} run(s) skipped as live" if totals["skipped_live"] else ""))
    out.append("DRY RUN — nothing was written." if dry_run
               else f"WROTE {totals['written']} backfill event(s).")
    return "\n".join(out)


def _lock_is_live(run_dir: Path) -> bool:
    """Is a live engine holding this run?

    ASKED THE WAY THE ENGINE ASKS IT — by trying to take the lock. `engine.lock` is an EMPTY file
    holding an **flock**, not a pid file (`cli/__init__.py`: "The OS frees the lock when the process
    exits (even on crash), so there's no stale-lock problem"), so reading it tells you nothing at
    all. A first version of this function parsed it as JSON for a pid, failed on every run because
    the file is zero bytes, and — failing closed — reported all EIGHT runs as live, including seven
    that had been finished for days. It looked like a careful safety check and was a total refusal.
    Contending for the lock is the only question with an answer.

    Non-blocking, and the lock is released immediately: this asks whether someone else holds it, and
    must never itself become the thing that blocks an engine from starting. Fails CLOSED — an
    unopenable or unlockable path reads as LIVE, because the cost of a false "live" is that the
    operator runs the command again, and the cost of a false "idle" is a reading taken from a
    directory being written to.
    """
    lock = run_dir / "engine.lock"
    if not lock.exists():
        return False
    try:
        import fcntl
        with open(lock, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True                      # someone holds it — a live engine
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
    except Exception:  # noqa: BLE001 — no flock on this mount, or no permission: fail closed
        return True
