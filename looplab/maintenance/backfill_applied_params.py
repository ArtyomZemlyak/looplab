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
  reading taken mid-write describes nothing. Asked by the engine's own liveness rule, and `--apply`
  HOLDS `engine.lock` from that verdict through its last append (`offline_run`), so no engine can
  start mid-pass and share the log with it.
"""
from __future__ import annotations

import json
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Optional

from looplab.engine.run_lifecycle import engine_liveness
from looplab.events.eventstore import (EventStore, EventStoreLockError, InterprocessLockContended,
                                       interprocess_lock)
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
              "diverged": 0, "conflicted": 0, "skipped_live": 0, "bounded": 0}
    for run_dir in run_dirs(root):
        if only and run_dir.name != only:
            continue
        # The whole pass over this run happens inside ONE fence: the liveness verdict, the horizon,
        # the plan and — under `--apply` — every append (review 2026-09-22, EVT-14; `offline_run`).
        with offline_run(run_dir, hold=not dry_run) as refusal:
            if skip_live and refusal:
                out.append(f"{run_dir.name}: SKIPPED — {refusal}. A workdir being written to "
                           "cannot be read as what ran.")
                totals["skipped_live"] += 1
                continue
            # NAMED BEFORE THE EARLY RETURN, not after it. A run whose rows are all already
            # backfilled produces NO rows and `continue`s below — so the one combination a reader
            # most needs ("nothing to do here" AND "only 20 of 1,624 lines are readable") printed
            # nothing at all. Found by running it, not by reading it.
            #
            # WHAT THIS PASS COULD NOT SEE. `EventStore.read_all` serves the log's dense prefix and
            # stops at the first logical-sequence gap — correct for a fold, invisible to a coverage
            # claim. This command shipped without saying so, and on `rubertlite-dense-retrieval` the
            # fence bites at event 20 of 1,624 lines, so a run with 81 `node_created` rows folded to
            # two nodes and the report read as if that were the run. Named rather than fixed:
            # repairing a gapped log is a different question from backfilling, and smuggling it in
            # here would answer neither well.
            served, lines = run_store(run_dir).readable_horizon()
            if lines and served < lines:
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
               + (f", {totals['skipped_live']} run(s) skipped as live" if totals["skipped_live"] else "")
               + (f", {totals['bounded']} run(s) READ ONLY TO A SEQUENCE GAP" if totals["bounded"] else ""))
    out.append("DRY RUN — nothing was written." if dry_run
               else f"WROTE {totals['written']} backfill event(s).")
    return "\n".join(out)


def run_store(run_dir: Path) -> EventStore:
    """This run's store. One spelling, so the horizon and the plan cannot read different files."""
    return EventStore(str(run_dir / "events.jsonl"))


@contextmanager
def offline_run(run_dir: Path, *, hold: bool) -> Iterator[Optional[str]]:
    """Fence one run for a backfill pass: yield None to proceed, or the reason to skip it.

    With ``hold`` (an ``--apply`` pass) the run's `engine.lock` is HELD while the block runs, so no
    engine can start until the last append is on disk — an engine starts by taking that lock without
    waiting (`cli/__init__.py::_engine_singleton`), and `repair-log` holds it the same way for its
    rewrite. A dry run writes nothing, so it only asks.

    THE VERDICT IS THE ENGINE'S OWN RULE, `engine/run_lifecycle.py::engine_liveness`, not a copy
    (review 2026-09-22, EVT-14). This module used to carry one, `_lock_is_live`, and it had drifted:
    it asked `Path.exists`, which follows links, so a dangling `engine.lock` symlink read as "no
    engine here" and the pass wrote. The shared rule answers a link or special inode as
    INCONCLUSIVE, and an append-only writer refuses on inconclusive: the cost of a false "live" is
    that the operator runs the command again, the cost of a false "idle" is a second writer of
    FOLDED events into a log the engine owns (invariant #1). The copy's one hard-won lesson is kept
    because the shared rule already embodies it: `engine.lock` is an EMPTY file holding an flock,
    not a pid file, so only contending for the lock answers the question — a first version parsed
    it for a pid, failed on every zero-byte file and, failing closed, reported all eight runs as
    live. And the shared rule contends with the ENGINE's primitive on each platform — `msvcrt` on
    Windows, where the copy's `import fcntl` had refused every run (WIN-BACKFILL, the Windows CI leg).

    AND THE VERDICT IS HELD, NOT JUST TAKEN. The copy contended for the lock and released it at
    once — right for a probe, which "must never itself become the thing that blocks an engine from
    starting", and wrong for a WRITER: the pass then planned and appended with nothing held, so an
    engine that started in that window (a `resume`, the UI's auto-reopen of a finished run) shared
    the log with it. The lock is now taken non-blocking right after the verdict — a contended take
    is an engine that started in between — and released only when the block exits.
    """
    liveness = engine_liveness(run_dir)
    if liveness is True:
        yield "a live engine holds this run"
        return
    if liveness is None:
        yield ("its engine.lock could not be verified (a link, a special file, or a probe that "
               "failed), which is treated as a live engine")
        return
    if not hold:
        yield None
        return
    with ExitStack() as fence:
        # Only the TAKE is guarded here. The block's own exceptions must propagate through the
        # `yield` untouched — catching them would make this generator yield twice.
        try:
            fence.enter_context(
                interprocess_lock(run_dir / "engine.lock", required=True, blocking=False))
        except InterprocessLockContended:
            refusal: Optional[str] = "a live engine holds this run (it started after the check)"
        except EventStoreLockError as exc:
            refusal = (f"engine.lock cannot be held here ({exc}), so an engine starting mid-pass "
                       "could not be fenced")
        else:
            refusal = None
        yield refusal
