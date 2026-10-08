"""Evaluation TRACKS on a LIVE run (doc 73 §1.4, stage 1 completed).

WHY (2026-10-06/07). An operator's questions about a running search — the champions at @200 where
the search scores @20, the same checkpoints on drift weeks w00..w07 — are evaluations the search
never runs. `looplab evaluate-track` (`maintenance/evaluate_track.py`) answers them on a STOPPED run:
it holds `engine.lock`, so asking meant stopping the search. This is the same evaluation served BY
the live engine, on the run's own GPU pool, without a stop.

THE LANE. A `track_requested {track, node_ids}` control intent (UI / API, the Assistant's
`evaluate_track` tool, `looplab evaluate-track --live`) is folded into `RunState.track_requests`.
Each loop turn `serve_track_requests` either starts the queue head in ONE background worker or, once
that worker is done, has the MAIN task append its results — one `extra_metrics_imported` per measured
node (`source: track <name>`, the row the offline command writes) and the positional `track_done`
receipt. The worker runs the operator's argv per node and returns values; it appends nothing
(invariant #1). A request whose receipt never landed (the engine died, a pause stopped it) is re-run
by the next engine; the fold ignores a key a node already carries, so nothing is recorded twice
(invariant #3).

GPUs. A track that declares `gpus: N` takes N devices from the SAME pool the evaluations lease
(`engine/resources.py::_acquire_gpus`), waiting while they are busy, and runs with
`CUDA_VISIBLE_DEVICES` fenced to them; `gpus: 0` (the default) runs with no device visible on a run
that has a pool, so a CPU scorer cannot land on a device an evaluation holds.

WHAT A TRACK MEASURES, and the refusal that keeps it honest (`track_refusal`): the node must be
evaluated in its CURRENT lifecycle and its workdir's manifest stamp must be that lifecycle's code —
a reset or rebuilt workdir is refused, never measured. The stamp is read when the request is
ACCEPTED (`_start`, the main task's fold) and compared again when the node's turn comes and after its
command ran, the copy-out's rule (`engine/artifact_sync.py`): a node reset while the job waited for
GPUs or for earlier nodes, or during its own command, is refused rather than recorded under the
lifecycle it no longer is. The tool log and every failure detail are masked of the passthrough
values in every spelling and at the capture cut (`artifact_sync.py::mask_tool_text`).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

_FAILED_DETAIL = 200
_RECEIPT_ROWS = 64
# The two refusals of a workdir that moved after the request was accepted (`_run_job`).
_REBUILT_BEFORE = "its workdir was rebuilt after the request was accepted"
_REBUILT_DURING = "its workdir was rebuilt while the track command ran"


def parse_track_output(stdout: str, *, keys=None, prefix: str = "") -> dict[str, float]:
    """The finite numbers of the LAST JSON object stdout printed (one per line), filtered to `keys`
    when declared, each named `prefix + key`. `{}` when no line parses."""
    found: Optional[dict] = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found = obj
            break
    if found is None:
        return {}
    wanted = set(keys) if keys else None
    out: dict[str, float] = {}
    for key, value in found.items():
        if wanted is not None and key not in wanted:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        out[f"{prefix}{key}"] = float(value)
    return out


def track_refusal(run_dir, node) -> Optional[str]:
    """Why this node's workdir may NOT be measured, or None."""
    from looplab.engine.artifact_sync import workdir_stamp
    from looplab.engine.evaluate import workdir_manifest_digest
    if getattr(node.status, "value", node.status) != "evaluated" or node.task_metric is None:
        return "not evaluated in its current lifecycle"
    workdir = Path(run_dir) / "nodes" / f"node_{node.id}"
    try:
        if workdir.is_symlink() or not workdir.is_dir():
            return "its workdir is gone"
    except OSError:
        return "its workdir is gone"
    # THE ONE READER OF A FILE THE CANDIDATE CAN WRITE (critic 2026-10-08): the stamp is written
    # before the candidate's eval runs in that same workdir, and a FIFO there made a plain
    # `read_text` block forever.
    raw = workdir_stamp(workdir)
    if raw is None:
        return "its workdir carries no readable manifest stamp"
    if raw.decode("ascii", errors="replace").strip() != workdir_manifest_digest(node):
        return "its workdir holds another lifecycle's or code's files (manifest stamp differs)"
    return None


def track_spec(eval_spec, track: str) -> Optional[dict]:
    """The declared `eval.tracks.<track>` from the run's eval spec dump, or None."""
    tracks = eval_spec.get("tracks") if isinstance(eval_spec, dict) else None
    entry = tracks.get(track) if isinstance(tracks, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), list) or not entry["command"]:
        return None
    return entry


def requested_node_ids(state, node_ids) -> list[int]:
    """`"all"` is every node evaluated in its current lifecycle when the request is SERVED."""
    if node_ids == "all":
        return sorted(n.id for n in state.evaluated_nodes())
    return sorted({n for n in (node_ids or []) if type(n) is int and n >= 0})


@dataclass
class TrackJob:
    """One served request in flight: its queue position, the worker, and what it found."""
    idx: int
    track: str
    thread: Optional[threading.Thread] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    rows: list = field(default_factory=list)        # [(node_id, generation, values)]
    refused: dict = field(default_factory=dict)     # {node_id: why}
    failed: dict = field(default_factory=dict)      # {node_id: why}

    @property
    def done(self) -> bool:
        return self.thread is not None and not self.thread.is_alive()


class TrackLane:
    """The engine's ONE in-flight track job (`Engine._track_lane`). Requests are served in order,
    one at a time: a track is an operator's question, never part of the search's throughput."""

    def __init__(self) -> None:
        self.job: Optional[TrackJob] = None

    def cancel(self) -> None:
        job = self.job
        if job is not None:
            job.cancel.set()


def _run_job(engine, job: TrackJob, spec: dict, targets: list) -> None:
    """The worker: run the operator's argv for each target node; RETURNS values on `job`, appends
    nothing. `targets` is `[(node_id, generation, refusal, accepted stamp)]`, decided on the main
    task's fold."""
    from looplab.engine.artifact_sync import (append_tool_log, mask_tool_text, passthrough_env,
                                              render_argv, sync_cwd, workdir_stamp)
    from looplab.runtime.sandbox import run_argv
    run_dir = Path(engine.run_dir)
    try:
        gpus = max(0, int(spec.get("gpus") or 0))
    except (TypeError, ValueError):
        gpus = 0
    held: list = []
    try:
        if gpus and getattr(engine, "_acquire_gpus", None) is not None:
            while not job.cancel.is_set():
                epoch = engine._gpu_pool_epoch()
                got = engine._acquire_gpus(gpus)
                if got is not None:
                    held = got
                    break
                engine._wait_for_gpu_change(epoch)
        if job.cancel.is_set():
            return
        declared = passthrough_env(spec)
        base_env = dict(declared)
        pool = list(getattr(engine, "_gpu_ids", None) or [])
        if held or pool:
            # Fenced to the devices this job holds — none at all for a `gpus: 0` track on a run with
            # a pool, so it cannot land on a device an evaluation is using.
            base_env = {**base_env,
                        "CUDA_VISIBLE_DEVICES": ",".join(engine._physical_gpu_ids(held)) if held else ""}
        timeout = float(spec.get("timeout") or 3600.0)
        for node_id, generation, refusal, accepted_stamp in targets:
            if job.cancel.is_set():
                return
            if refusal:
                job.refused[node_id] = refusal
                continue
            workdir = run_dir / "nodes" / f"node_{node_id}"
            if workdir_stamp(workdir) != accepted_stamp:
                job.refused[node_id] = _REBUILT_BEFORE
                continue
            argv = render_argv(spec["command"], {
                "workdir": str(workdir), "run_dir": str(run_dir), "run_id": run_dir.name,
                "node_id": node_id, "generation": generation})
            try:
                # The working directory the DECLARATION implies and the log written once, MASKED of
                # the passthrough values and every known secret — the offline command's rules
                # (`artifact_sync.py::sync_cwd`, `append_tool_log`).
                rc, out, err, timed = run_argv(argv, sync_cwd(workdir, run_dir, spec), timeout,
                                               env=dict(base_env), cancel=job.cancel)
                append_tool_log(run_dir / f"track_{job.track}.log", argv, out, err, declared)
            except (OSError, ValueError) as exc:
                job.failed[node_id] = mask_tool_text(
                    f"could not run: {type(exc).__name__}: {exc}", declared)[:_FAILED_DETAIL]
                continue
            if workdir_stamp(workdir) != accepted_stamp:
                job.refused[node_id] = _REBUILT_DURING
                continue
            values = parse_track_output(out, keys=spec.get("keys"),
                                        prefix=str(spec.get("key_prefix") or ""))
            if rc != 0 or timed or not values:
                tail = mask_tool_text(err or "", declared).strip().splitlines()[-1:] or [""]
                job.failed[node_id] = (f"exit {rc}{', timed out' if timed else ''}, "
                                       f"{len(values)} value(s): {tail[0]}")[:_FAILED_DETAIL]
                continue
            job.rows.append((node_id, generation, values))
    finally:
        if held:
            engine._release_gpus(held)


def _start(engine, state, idx: int) -> TrackJob:
    req = state.track_requests[idx]
    job = TrackJob(idx=idx, track=str(req.get("track")))
    spec = track_spec(getattr(engine, "_eval_spec", None), job.track)
    if spec is None:
        job.failed[-1] = f"the task declares no eval.tracks.{job.track}"
        job.thread = threading.Thread(target=lambda: None, daemon=True)
        job.thread.start()
        return job
    from looplab.engine.artifact_sync import workdir_stamp
    targets = []
    for nid in requested_node_ids(state, req.get("node_ids")):
        node = state.nodes.get(nid)
        why = "no such node" if node is None else track_refusal(engine.run_dir, node)
        # The stamp the refusal just vouched for, kept for the worker's two re-reads (`_run_job`).
        stamp = None if why else workdir_stamp(Path(engine.run_dir) / "nodes" / f"node_{nid}")
        targets.append((nid, getattr(node, "attempt", 0), why, stamp))
    job.thread = threading.Thread(target=_run_job, args=(engine, job, spec, targets),
                                  name=f"looplab-track:{job.track}", daemon=True)
    job.thread.start()
    return job


async def _harvest(engine, job: TrackJob) -> None:
    """The MAIN task's half: append the measured rows and the receipt (invariant #1)."""
    from looplab.events.types import EV_EXTRA_METRICS_IMPORTED, EV_TRACK_DONE
    now = round(time.time(), 3)
    async with engine._write_lock:
        for node_id, generation, values in job.rows:
            engine.store.append(EV_EXTRA_METRICS_IMPORTED, {
                "node_id": node_id, "generation": generation, "extra_metrics": values,
                "source": f"track {job.track}"[:400], "imported_at": now})
        engine.store.append(EV_TRACK_DONE, {
            "idx": job.idx, "track": job.track, "recorded": len(job.rows),
            **({"refused": {str(k): v for k, v in list(job.refused.items())[:_RECEIPT_ROWS]}}
               if job.refused else {}),
            **({"failed": {str(k): engine._redact(v) for k, v in
                           list(job.failed.items())[:_RECEIPT_ROWS]}} if job.failed else {})})


def cancel_track_lane(engine) -> None:
    """Cancel the in-flight track job, if this engine has a lane (an engine stand-in built without
    `Engine.__init__` has none, and its `run` must still end cleanly)."""
    lane = getattr(engine, "_track_lane", None)
    if lane is not None:
        lane.cancel()


async def serve_track_requests(engine, state) -> bool:
    """One loop turn's look at the track queue. True when it appended (the caller re-folds)."""
    lane = getattr(engine, "_track_lane", None)
    if lane is None:
        return False
    job = lane.job
    if job is not None:
        if not job.done:
            return False
        lane.job = None
        if job.cancel.is_set() or job.idx != state.tracks_done:
            return False            # cancelled, or the queue moved under it: the next engine re-runs
        await _harvest(engine, job)
        return True
    if state.tracks_done < len(state.track_requests) and not state.halted:
        lane.job = _start(engine, state, state.tracks_done)
    return False


async def drain_track_requests(engine, fold_fn) -> None:
    """Serve the WHOLE queue before the run finishes: an operator's question asked of a running
    search is answered even when the search ends first. Each request runs to completion here."""
    import anyio
    lane = getattr(engine, "_track_lane", None)
    if lane is None:
        return
    for _ in range(64):
        state = fold_fn(engine.store.read_all())
        if lane.job is None and state.tracks_done >= len(state.track_requests):
            return
        if lane.job is None:
            lane.job = _start(engine, state, state.tracks_done)
        while not lane.job.done:
            await anyio.sleep(0.05)
        await serve_track_requests(engine, state)
