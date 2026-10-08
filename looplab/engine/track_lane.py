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
(invariant #3). The worker is NON-daemon and joined, bounded, when its engine ends
(`cancel_track_lane`): a cancel tree-kills its command (`runtime/sandbox.py::run_argv`), and a
cancelled command writes no log, so nothing of it outlives the engine's `engine.lock`.

A FINISHING RUN ANSWERS ITS QUEUE FIRST, before it claims its finish (review 2026-10-08): a search-
ended finish is refused while a track is queued (`refuse_finish_over_track_queue`, the adopted-eval
refusal's shape), and the next turn's head serves the queue (`drain_track_requests`). It used to be
drained AFTER the loop, inside the open finalize scope, where its folded rows made the staged finish
read as abandoned (`events/finalize_scope.py::finalize_scope_quiescent`). A stop, a pause, a refusal,
the wall clock or a guarded abort finishes WITHOUT it: the drain stops on them too, between and during
jobs, and what is left stays queued, with no receipt, for an engine that has the time.

GPUs. A track that declares `gpus: N` takes N devices from the SAME pool the evaluations lease
(`engine/resources.py::_acquire_gpus`), waiting while they are busy, and runs with
`CUDA_VISIBLE_DEVICES` fenced to them; `gpus: 0` (the default) runs with no device visible on a run
that has a pool, so a CPU scorer cannot land on a device an evaluation holds. While it holds devices
the dispatcher does not take a drained eval pool for "the head can never fit"
(`track_gpus_held`, `engine/eval_dispatch.py`).

WHAT A TRACK MEASURES, and the refusal that keeps it honest (`track_refusal`): the node must be
evaluated in its CURRENT lifecycle and its workdir's manifest stamp must be that lifecycle's code —
THE rule `engine/eval_dispatch.py::produced_workdir` applies to a parent's or a used artifact's
workdir, a linked workdir included. The lifecycle is fixed when the request is ACCEPTED (`_start`, on
the main task's fold, no I/O there) and the workdir is held to it when the node's turn comes and
again after its command ran, the copy-out's rule (`engine/artifact_sync.py`): a node reset while the
job waited for GPUs or for earlier nodes, or during its own command, is refused rather than recorded
under the lifecycle it no longer is. The tool log and every failure detail are masked of the
passthrough values in every spelling and at the capture cut (`artifact_sync.py::mask_tool_text`).
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
# The most node ids one request may name — the server's intake bound
# (`serve/control_validation.py::_normalize_track_requested`), which the Assistant's tool and the
# CLI's `--live` refuse by too rather than truncating to it.
MAX_TRACK_NODE_IDS = 256
# How long an ending engine waits for its cancelled worker: the cancel tree-kills the command at the
# drain loop's next poll, so this bounds a kill that hangs, never a measurement.
_JOIN_S = 30.0
# The two refusals of a workdir that is not the accepted lifecycle's (`_run_job`).
_NOT_ITS_FILES = ("its workdir does not hold this lifecycle's files (its manifest stamp is missing "
                  "or names other code)")
_REBUILT_DURING = "its workdir was rebuilt while the track command ran"
# `produced_workdir`'s `why`, in the words a receipt and the offline report print.
_WHY_TEXT = {"missing": "no such node",
             "rebuilt": "it was rebuilt after the request was accepted",
             "not_evaluated": "not evaluated in its current lifecycle",
             "workdir_missing": "its workdir is gone",
             "workdir_changed": _NOT_ITS_FILES}
# The finishes a queued track does NOT hold back (`refuse_finish_over_track_queue`): a stop, a
# refusal, the wall clock — and every `stuck: …` / `systemic failure: …` reason, matched by prefix
# because those reasons carry their own diagnosis. A guarded abort is `is_guarded_abort`'s.
_STOP_REASONS = frozenset({"aborted", "leakage", "time_budget"})
_STOP_PREFIXES = ("stuck", "systemic failure")


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
        except (ValueError, RecursionError):
            # RecursionError too (review 2026-10-08): a line nested past the decoder's stack is the
            # tool's malformed output, not a crash of the lane — `runtime/command_eval.py`'s rule.
            # Not `core/jsonutil.py::strict_json_loads`: it refuses a WHOLE line over one `NaN`
            # value, and a scorer's NaN on one key must not cost its other numbers.
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


def track_refusal(run_dir, node, generation=None) -> Optional[str]:
    """Why this node's workdir may NOT be measured (in lifecycle `generation`, its current one when
    None), or None. THE ONE RULE (`engine/eval_dispatch.py::produced_workdir`: evaluated in that
    lifecycle, both workdir components real directories, the stamp read through the bounded reader
    and equal to that lifecycle's digest) plus the task metric a track is measured beside."""
    from types import SimpleNamespace

    from looplab.engine.eval_dispatch import produced_workdir
    if node is None:
        return _WHY_TEXT["missing"]
    if node.task_metric is None:
        return _WHY_TEXT["not_evaluated"]
    _wd, why = produced_workdir(SimpleNamespace(nodes={node.id: node}), run_dir, node.id,
                                generation)
    return _WHY_TEXT.get(why, why) if why else None


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


def track_argv(spec: dict, run_dir: Path, node_id: int, generation) -> list[str]:
    """The declared argv for one node, rendered over the ABSOLUTE run directory (review 2026-10-08:
    a relative `--out runs/demo` rendered `{workdir}` relative to a command whose cwd IS that
    workdir, so the path resolved twice)."""
    from looplab.engine.artifact_sync import render_argv
    run_dir = Path(run_dir).absolute()
    return render_argv(spec["command"], {
        "workdir": str(run_dir / "nodes" / f"node_{node_id}"), "run_dir": str(run_dir),
        "run_id": run_dir.name, "node_id": node_id, "generation": generation})


@dataclass
class TrackRun:
    """One node's track command, as both the live lane and the offline command read it:
    `outcome` is `ok` (with `values`), `failed` / `refused` (with `detail`) or `cancelled`."""
    outcome: str
    values: dict = field(default_factory=dict)
    detail: str = ""
    seconds: float = 0.0


def run_track_on_node(spec: dict, track: str, run_dir, node_id: int, generation, *, env: dict,
                      declared: dict, cancel=None, check=None) -> TrackRun:
    """Run the track's argv for ONE node and read its numbers — the per-node step the live worker
    and `maintenance/evaluate_track.py` share. `env` is the child's environment, `declared` the
    passthrough values every written text is masked of; `check()` re-asks the node's refusal AFTER
    the command (a reset during it is refused, never recorded); a set `cancel` writes nothing.

    The LOG is written apart from the run (review 2026-10-08): a log that cannot be written must not
    discard a measurement the command already made."""
    from looplab.engine.artifact_sync import append_tool_log, mask_tool_text, sync_cwd
    from looplab.runtime.sandbox import run_argv
    run_dir = Path(run_dir).absolute()
    workdir = run_dir / "nodes" / f"node_{node_id}"
    argv = track_argv(spec, run_dir, node_id, generation)
    try:
        timeout = float(spec.get("timeout") or 3600.0)
    except (TypeError, ValueError):
        timeout = 3600.0
    started = time.monotonic()
    try:
        # The working directory the DECLARATION implies, the copy-out's rule
        # (`artifact_sync.py::sync_cwd`).
        rc, out, err, timed = run_argv(argv, sync_cwd(workdir, run_dir, spec), timeout,
                                       env=dict(env), cancel=cancel)
    except (OSError, ValueError) as exc:                  # no such tool, an unusable cwd
        return TrackRun("failed", detail=mask_tool_text(
            f"could not run: {type(exc).__name__}: {exc}", declared,
            cut_edges=False)[:_FAILED_DETAIL])
    seconds = round(time.monotonic() - started, 1)
    if cancel is not None and cancel.is_set():
        return TrackRun("cancelled", seconds=seconds)
    try:
        # Written once, MASKED of the passthrough values and every known secret
        # (`artifact_sync.py::append_tool_log`).
        append_tool_log(run_dir / f"track_{track}.log", argv, out, err, declared)
    except OSError:
        _LOG.warning("track %s, node %s: could not write track_%s.log; the measurement stands",
                     track, node_id, track, exc_info=True)
    why = check() if check is not None else None
    if why:
        return TrackRun("refused", detail=why, seconds=seconds)
    values = parse_track_output(out, keys=spec.get("keys"), prefix=str(spec.get("key_prefix") or ""))
    if rc != 0 or timed or not values:
        tail = mask_tool_text(err or "", declared).strip().splitlines()[-1:] or [""]
        return TrackRun("failed", values=values, seconds=seconds, detail=(
            f"exit {rc}{', timed out' if timed else ''}, {len(values)} value(s): "
            f"{tail[0]}")[:_FAILED_DETAIL])
    return TrackRun("ok", values=values, seconds=seconds)


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
    # Why the WORKER ended before its last node (review 2026-10-08): an exception past every
    # per-node handler used to end the thread silently and the receipt read `recorded: 0`.
    crashed: str = ""
    gpus_held: int = 0                              # devices this job holds from the run's pool

    @property
    def done(self) -> bool:
        return self.thread is not None and not self.thread.is_alive()


class TrackLane:
    """The engine's ONE in-flight track job (`Engine._track_lane`). Requests are served in order,
    one at a time: a track is an operator's question, never part of the search's throughput."""

    def __init__(self) -> None:
        self.job: Optional[TrackJob] = None
        # Set by a finish this lane refused (`refuse_finish_over_track_queue`); the next loop turn's
        # head serves the queue (`drain_track_requests`) and clears it.
        self.drain_requested = False

    def cancel(self) -> None:
        job = self.job
        if job is not None:
            job.cancel.set()


def _lease(engine, job: TrackJob, gpus: int) -> list:
    """The devices this job runs on, from the run's pool, waiting while they are busy."""
    if not gpus or getattr(engine, "_acquire_gpus", None) is None:
        return []
    while not job.cancel.is_set():
        epoch = engine._gpu_pool_epoch()
        got = engine._acquire_gpus(gpus)
        if got is not None:
            job.gpus_held = len(got)
            return got
        engine._wait_for_gpu_change(epoch)
    return []


def _run_job(engine, job: TrackJob, spec: dict, targets: list) -> None:
    """The worker: run the operator's argv for each target node; RETURNS values on `job`, appends
    nothing. `targets` is `[(node_id, generation, refusal, node)]`, decided on the main task's fold
    (`node` is that fold's copy, the lifecycle the request was accepted for)."""
    from looplab.engine.artifact_sync import mask_tool_text, passthrough_env
    run_dir = Path(engine.run_dir).absolute()
    declared = passthrough_env(spec)
    held: list = []
    try:
        try:
            gpus = max(0, int(spec.get("gpus") or 0))
        except (TypeError, ValueError):
            gpus = 0
        held = _lease(engine, job, gpus)
        if job.cancel.is_set():
            return
        base_env = dict(declared)
        pool = list(getattr(engine, "_gpu_ids", None) or [])
        if held or pool:
            # Fenced to the devices this job holds — none at all for a `gpus: 0` track on a run with
            # a pool, so it cannot land on a device an evaluation is using.
            base_env = {**base_env,
                        "CUDA_VISIBLE_DEVICES": ",".join(engine._physical_gpu_ids(held)) if held else ""}
        for node_id, generation, refusal, node in targets:
            if job.cancel.is_set():
                return
            if refusal:
                job.refused[node_id] = refusal
                continue
            try:
                # Held to the ACCEPTED lifecycle when its turn comes: a reset while the job waited
                # for its GPUs or for earlier nodes is refused, never measured.
                why = track_refusal(run_dir, node, generation)
                if why:
                    job.refused[node_id] = why
                    continue
                run = run_track_on_node(
                    spec, job.track, run_dir, node_id, generation, env=base_env, declared=declared,
                    cancel=job.cancel,
                    check=lambda: _REBUILT_DURING if track_refusal(run_dir, node, generation) else None)
            except Exception as exc:  # noqa: BLE001 — one node's defect is its receipt row
                job.failed[node_id] = mask_tool_text(
                    f"{type(exc).__name__}: {exc}", declared, cut_edges=False)[:_FAILED_DETAIL]
                continue
            if run.outcome == "cancelled":
                return
            if run.outcome == "ok":
                job.rows.append((node_id, generation, run.values))
            elif run.outcome == "refused":
                job.refused[node_id] = run.detail
            else:
                job.failed[node_id] = run.detail
    except Exception as exc:  # noqa: BLE001 — the outcome is a VALUE; an escape lost its cause
        job.crashed = mask_tool_text(f"the track worker stopped: {type(exc).__name__}: {exc}",
                                     declared, cut_edges=False)[:_FAILED_DETAIL]
        _LOG.warning("track %s: the worker stopped early", job.track, exc_info=True)
    finally:
        if held:
            engine._release_gpus(held)
        job.gpus_held = 0


def _start(engine, state, idx: int) -> TrackJob:
    """Start the queue's request `idx` in its worker. Decides on the main task's FOLD only — no
    workdir is read here (review 2026-10-08): the worker holds each node's workdir to the lifecycle
    this fold names when the node's turn comes."""
    req = state.track_requests[idx]
    job = TrackJob(idx=idx, track=str(req.get("track")))
    spec = track_spec(getattr(engine, "_eval_spec", None), job.track)
    if spec is None:
        job.failed[-1] = f"the task declares no eval.tracks.{job.track}"
        job.thread = threading.Thread(target=lambda: None, name=f"looplab-track:{job.track}")
        job.thread.start()
        return job
    targets = []
    for nid in requested_node_ids(state, req.get("node_ids")):
        node = state.nodes.get(nid)
        if node is None:
            targets.append((nid, 0, _WHY_TEXT["missing"], None))
        elif getattr(node.status, "value", node.status) != "evaluated" or node.task_metric is None:
            targets.append((nid, node.attempt, _WHY_TEXT["not_evaluated"], None))
        else:
            # A COPY: the worker reads it off this thread, and a folded node is mutable.
            targets.append((nid, node.attempt, None, node.model_copy()))
    # NON-daemon, and joined when the engine ends (`cancel_track_lane`): a daemon worker was cut
    # mid-kill at interpreter exit, orphaning the command its cancel was tree-killing.
    job.thread = threading.Thread(target=_run_job, args=(engine, job, spec, targets),
                                  name=f"looplab-track:{job.track}", daemon=False)
    job.thread.start()
    return job


async def _harvest(engine, job: TrackJob, state) -> None:
    """The MAIN task's half: append the measured rows and the receipt (invariant #1). Each row holds
    what the fold will KEEP (`core/models.py::plan_extra_metrics_import`, on this turn's fold), so
    `recorded` counts the nodes that gained a key, and a node that gained none says why."""
    from looplab.core.models import plan_extra_metrics_import
    from looplab.events.types import EV_EXTRA_METRICS_IMPORTED, EV_TRACK_DONE
    now = round(time.time(), 3)
    refused = dict(job.refused)
    failed = dict(job.failed)
    if job.crashed:
        failed[-1] = job.crashed
    recorded = 0
    async with engine._write_lock:
        for node_id, generation, values in job.rows:
            node = state.nodes.get(node_id)
            if node is None or node.attempt != generation or node.task_metric is None:
                refused[node_id] = "it was rebuilt before its numbers were recorded"
                continue
            added, kept, _dropped = plan_extra_metrics_import(node.extra_metrics, values)
            if not added:
                refused[node_id] = ("it already carries every key measured" if kept
                                    else "its metric map is full (256 keys)")
                continue
            engine.store.append(EV_EXTRA_METRICS_IMPORTED, {
                "node_id": node_id, "generation": generation, "extra_metrics": added,
                "source": f"track {job.track}"[:400], "imported_at": now})
            recorded += 1
        engine.store.append(EV_TRACK_DONE, {
            "idx": job.idx, "track": job.track, "recorded": recorded,
            **({"refused": {str(k): v for k, v in list(refused.items())[:_RECEIPT_ROWS]}}
               if refused else {}),
            **({"failed": {str(k): engine._redact(v) for k, v in
                           list(failed.items())[:_RECEIPT_ROWS]}} if failed else {})})


def cancel_track_lane(engine, join_s: float = _JOIN_S) -> None:
    """Cancel the in-flight track job, if this engine has a lane (an engine stand-in built without
    `Engine.__init__` has none, and its `run` must still end cleanly), and wait up to `join_s` for
    its worker: the cancel tree-kills the command, and a worker still running when the engine
    releases its run would outlive `engine.lock`. `join_s=0` only cancels."""
    lane = getattr(engine, "_track_lane", None)
    if lane is None:
        return
    lane.cancel()
    thread = lane.job.thread if lane.job is not None else None
    if join_s and thread is not None and thread.is_alive() \
            and thread is not threading.current_thread():
        thread.join(join_s)
        if thread.is_alive():
            _LOG.warning("track worker still running %.0fs after its cancel; its request stays "
                         "queued for the next engine", join_s)


def track_gpus_held(engine) -> bool:
    """Whether a track job holds devices of the run's pool right now — the dispatcher's
    "every slot is free and the head still does not fit" test must not read a pool a TRACK drained
    as one too small for the head (`engine/eval_dispatch.py`)."""
    lane = getattr(engine, "_track_lane", None)
    job = getattr(lane, "job", None)
    return bool(job is not None and job.gpus_held and not job.done)


def _search_ended(reason) -> bool:
    """Is a finish with this `reason` the SEARCH's end (budget spent, no action left, a plateau) —
    the finishes a queued track holds back — rather than a stop, a refusal or a ceiling?"""
    from looplab.events.finalize_scope import is_guarded_abort
    text = str(reason or "")
    return not (text in _STOP_REASONS or is_guarded_abort(text) or text.startswith(_STOP_PREFIXES))


def refuse_finish_over_track_queue(engine, state, data) -> bool:
    """Refuse a search-ended finish while an operator's track request is queued or running, and ask
    the next loop turn to serve the queue (`drain_track_requests`) — the shape of
    `Engine._refuse_finish_over_adopted_evals`. The rows land BEFORE the finish claims its scope,
    never inside it (review 2026-10-08). A halted run or a stop-class finish is never held back."""
    lane = getattr(engine, "_track_lane", None)
    if lane is None or state.halted or not _search_ended((data or {}).get("reason")):
        return False
    if lane.job is None and state.tracks_done >= len(state.track_requests):
        return False
    lane.drain_requested = True
    return True


def track_drain_due(engine) -> bool:
    """Did a finish this turn's predecessor attempted leave the queue to be served first?"""
    lane = getattr(engine, "_track_lane", None)
    return bool(lane is not None and lane.drain_requested)


def _wall_clock_spent(engine, state, started_at) -> bool:
    """The run loop's own wall-clock gate (`Engine._apply_control_overrides`' `max_s`), asked
    without its side effects: an operator's `budget_extend` first, else the launch ceiling."""
    from looplab.engine.width_settling import budget_ceiling
    if started_at is None:
        return False
    max_s = budget_ceiling(state.budget_overrides, "max_seconds", getattr(engine, "max_seconds", None))
    return max_s is not None and (time.time() - started_at) >= max_s


async def _stop_job(lane: TrackLane) -> None:
    """Cancel the lane's job and wait, bounded, for its worker; its request stays queued."""
    import anyio
    job = lane.job
    if job is None:
        return
    job.cancel.set()
    waited = 0.0
    while not job.done and waited < _JOIN_S:
        await anyio.sleep(0.05)
        waited += 0.05
    lane.job = None


async def drain_track_requests(engine, fold_fn, *, started_at: Optional[float] = None) -> None:
    """Serve the queue a refused finish left (`refuse_finish_over_track_queue`), one request at a
    time, to the end — unless the run halts (a pause, a stop, an abort) or its wall clock is spent,
    checked between jobs and while one runs: the job in flight is then cancelled and every request
    left stays queued with NO receipt, for an engine with the time. `started_at` is the run loop's
    own start (`time.time()`), the clock `max_seconds` is measured on."""
    import anyio
    lane = getattr(engine, "_track_lane", None)
    if lane is None:
        return
    lane.drain_requested = False
    while True:
        state = fold_fn(engine.store.read_all())
        if state.halted or _wall_clock_spent(engine, state, started_at):
            await _stop_job(lane)
            return
        if lane.job is None:
            if state.tracks_done >= len(state.track_requests):
                return
            lane.job = _start(engine, state, state.tracks_done)
        polls = 0
        while not lane.job.done:
            await anyio.sleep(0.05)
            polls += 1
            # The clock every poll (free); the log once a second (a fold).
            if _wall_clock_spent(engine, state, started_at) or (
                    polls % 20 == 0 and fold_fn(engine.store.read_all()).halted):
                await _stop_job(lane)
                return
        await serve_track_requests(engine, fold_fn(engine.store.read_all()))


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
        await _harvest(engine, job, state)
        return True
    if state.tracks_done < len(state.track_requests) and not state.halted:
        lane.job = _start(engine, state, state.tracks_done)
    return False
