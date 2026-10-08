"""The operator's COPY-OUT of a finished node's workdir (`eval.artifact_sync`).

WHY (incident 2026-10-06). A 1.5B training ran ten hours on an 8-GPU box; its checkpoint lived on a
network mount that went away, and the only way forward was to train again from zero. LoopLab kept
every byte of the run's RECORD, and none of the artifacts the record is about: `export-bundle`
deliberately leaves node workdirs out ("data mounts and checkpoints are the box's"), and nothing else
ever copied one anywhere.

WHAT. When the task declares `eval.artifact_sync: {command: [...], timeout: …}`, the engine runs that
argv after a node's terminal — the operator's own `mc cp` / `aws s3 cp` / `rsync`, with the host
environment `run_setup` gets, so the tool finds its own credentials — over the node's workdir. Each
argument may name `{workdir}`, `{run_dir}`, `{run_id}`, `{node_id}` and `{generation}`; nothing else in
it is interpreted (no shell, no `str.format`: a literal brace in an argument stays a brace).

THREE PROPERTIES, each the reason for a choice:

* IN A BOUNDED BACKGROUND POOL, after the terminal. A copy of gigabytes is minutes of wall clock, and
  doing it inside `_evaluate` would hold the eval slot — the GPU lease — for an upload. At most
  `MAX_CONCURRENT_SYNCS` copies run at once (round 3: every finished node used to start its own
  thread, so a wide run fanned out as many concurrent uploads as it had evals finishing, all on one
  uplink); the rest wait in this process's queue, in terminal order. The workers are NON-daemon, so
  the interpreter waits for the queued and in-flight copies at exit (each bounded by its `timeout`)
  instead of killing the copy of the run's last node; a worker exits as soon as the queue is empty.
* A RECEIPT, NEVER A VERDICT, OPENED BEFORE IT IS CLOSED. The `artifact_sync_started` row is written
  when the copy is accepted (round 3), the `artifact_synced` row when it ends, both keyed by
  `sync_id`; a started row with no synced row is a copy an engine death interrupted, queued or
  running (`unfinished_syncs`). Both rows are DIAGNOSTIC (invariant #1: appendable from a thread,
  fold-ignored, fence-neutral). A failed copy is reported in it and in `artifact_sync.log`; it never
  fails, pauses or re-runs the node, whose metric is already on its terminal — and an interrupted
  copy is NOT retried on resume: the operator's command is not known to be idempotent, so the
  started row is what tells them to run it again.
* WHAT THE OPERATOR DECLARED, ONLY. The engine never picks a destination or a tool; an absent
  declaration runs nothing.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from looplab.core.node_evidence import node_workdir
from looplab.events.types import EV_ARTIFACT_SYNC_STARTED, EV_ARTIFACT_SYNCED

_LOG = logging.getLogger(__name__)

PLACEHOLDERS: tuple[str, ...] = ("workdir", "run_dir", "run_id", "node_id", "generation")
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_STDERR_TAIL = 400

# How many operator copy-outs run at once in one process. Two, not one: a slow upload of one
# checkpoint must not hold every later node's copy behind it, and not more, because the copies share
# the box's uplink and the operator's object store — N concurrent multi-gigabyte uploads finish no
# sooner than two at a time and each one is N times as likely to hit its own `timeout`. A module
# constant rather than a `Settings` field, like `eval_dispatch.py::_MAX_NODE_DEP_SYNCS`: a runaway
# bound, not a knob; the knob is whether `eval.artifact_sync` is declared at all.
MAX_CONCURRENT_SYNCS = 2

# The copies accepted in THIS process and not yet finished (queued + running), the queue the
# workers drain, and the live worker count — one condition guards all three, so `wait_for_inflight`
# can wait for "nothing pending" without polling.
_QUEUE: collections.deque = collections.deque()
_PENDING = 0
_WORKERS = 0
_COND = threading.Condition()


def render_argv(argv, values: dict) -> list[str]:
    """`argv` with every KNOWN placeholder replaced; anything else is left exactly as written."""
    return [_PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), str(arg)) for arg in argv]


def sync_spec(eval_spec) -> Optional[dict]:
    """The declared `eval.artifact_sync`, or None when there is none to run."""
    spec = (eval_spec or {}).get("artifact_sync") if isinstance(eval_spec, dict) else None
    if not isinstance(spec, dict):
        return None
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        return None
    return spec


def passthrough_env(spec) -> dict:
    """`{NAME: value}` for every name the declaration's `env_passthrough` lists that the ENGINE's
    environment holds (`adapters/repo_task.py::ArtifactSyncSpec.env_passthrough`). Shared with
    `maintenance/evaluate_track.py`; a value never leaves this dict for a record."""
    names = spec.get("env_passthrough") if isinstance(spec, dict) else None
    if not isinstance(names, list):
        return {}
    return {n: os.environ[n] for n in names if isinstance(n, str) and n in os.environ}


def sync_cwd(workdir, run_dir, spec) -> str:
    """Where an operator copy-out or track runs: the node's WORKDIR, as documented (a relative
    `./` or `score.py` names the node's files) — except when its declaration hands it credentials
    (`env_passthrough`), which run from the RUN directory (critic 2026-10-08): the workdir is the
    candidate's, and a `python -m <tool>` started there imports whatever module the candidate left
    beside the key. Two rounds of review: always-run-dir broke every workdir-relative command.

    Decided by the DECLARATION, never by which of the declared names this host's environment holds
    (round 3): keyed on `passthrough_env(spec)`, the same task ran from the run dir on a box that
    had the key and from the workdir on one that did not, and a relative argv failed on one of them
    with a misleading "file not found". `spec` is the declaration (`eval.artifact_sync` or a track)."""
    names = spec.get("env_passthrough") if isinstance(spec, dict) else None
    declared = isinstance(names, list) and any(isinstance(n, str) and n for n in names)
    return str(run_dir if declared else workdir)


def workdir_stamp(workdir: Path) -> Optional[bytes]:
    """The bytes of a node workdir's `.looplab-manifest` stamp, or None. Through THE reader of a file
    a candidate can write (`core/node_evidence.py::read_bounded_regular_file`): the stamp is written
    before the candidate's eval runs in that same directory, which may replace it with a FIFO or a
    multi-gigabyte file. Shared with `maintenance/evaluate_track.py`."""
    from looplab.core.node_evidence import read_bounded_regular_file
    return read_bounded_regular_file(Path(workdir) / ".looplab-manifest", 256)


def unfinished_syncs(events) -> list[dict]:
    """The `artifact_sync_started` rows no `artifact_synced` row closed, in log order: copies an
    engine death interrupted (or, on a live engine, copies still queued or running). Pure."""
    closed = {e.data.get("sync_id") for e in events
              if e.type == EV_ARTIFACT_SYNCED and isinstance(e.data, dict) and e.data.get("sync_id")}
    return [dict(e.data) for e in events
            if e.type == EV_ARTIFACT_SYNC_STARTED and isinstance(e.data, dict)
            and e.data.get("sync_id") not in closed]


def start_artifact_sync(engine, node_id: int, generation: int) -> Optional[str]:
    """Queue the declared copy-out of node `node_id`'s workdir on the bounded pool and return its
    `sync_id`, or None (nothing declared, or no workdir to copy). Never raises."""
    spec = sync_spec(getattr(engine, "_eval_spec", None))
    if spec is None:
        return None
    run_dir = Path(engine.run_dir)
    # A REAL directory at both components, never a link (review 2026-10-08): `is_dir()` followed a
    # linked `nodes/node_N`, and the operator's command — run with the host's credentials — would
    # have uploaded whatever the link names. `node_workdir` is the refusal the log readers and
    # `_parent_workdirs_env` already apply; a missing workdir is None too. Never raises.
    try:
        workdir = node_workdir(run_dir, node_id)
    except (OSError, TypeError, ValueError):
        return None
    if workdir is None:
        return None
    values = {"workdir": str(workdir), "run_dir": str(run_dir), "run_id": run_dir.name,
              "node_id": node_id, "generation": generation}
    argv = render_argv(spec["command"], values)
    try:
        timeout = float(spec.get("timeout") or 1800.0)
    except (TypeError, ValueError):
        timeout = 1800.0
    sync_id = uuid.uuid4().hex[:16]
    # THE START ROW FIRST, then the queue: a copy the log does not say was accepted must never run,
    # and one it does say was accepted is visible as unfinished from this instant — queued behind
    # the bound or running — until its `artifact_synced` row lands.
    try:
        engine.store.append(EV_ARTIFACT_SYNC_STARTED, {
            "node_id": node_id, "generation": generation, "sync_id": sync_id})
    except OSError:
        _LOG.warning("artifact sync of node %s: could not record its start; not running it",
                     node_id, exc_info=True)
        return None
    _submit((engine, node_id, generation, argv, workdir, run_dir, timeout,
             passthrough_env(spec), sync_id, sync_cwd(workdir, run_dir, spec)))
    return sync_id


def _submit(job) -> None:
    """Queue `job` and start a worker when fewer than `MAX_CONCURRENT_SYNCS` are alive."""
    global _PENDING, _WORKERS
    with _COND:
        _QUEUE.append(job)
        _PENDING += 1
        if _WORKERS >= MAX_CONCURRENT_SYNCS:
            return
        _WORKERS += 1
    _spawn()


def _spawn() -> None:
    """Start one worker, whose slot the caller already counted in `_WORKERS`."""
    try:
        threading.Thread(target=_worker, name="looplab-artifact-sync", daemon=False).start()
    except RuntimeError:          # no thread to be had (interpreter shutting down): drain it here
        _worker()


def _worker() -> None:
    """Drain the queue, then exit — an idle worker holds nothing, so exit never waits on one.

    The worker gives its slot back IN THE SAME critical section that finds the queue empty, so a
    `_submit` racing that moment either sees the job taken or sees a free slot — never a full pool
    of workers that are all on their way out. A copy that raises past `_run`'s own handlers ends
    this worker; its `finally` hands any queued copy to a replacement rather than stranding it."""
    global _PENDING, _WORKERS
    clean = False
    try:
        while True:
            with _COND:
                if not _QUEUE:
                    _WORKERS -= 1
                    clean = True
                    _COND.notify_all()
                    return
                job = _QUEUE.popleft()
            try:
                _run(*job[:8], sync_id=job[8], cwd=job[9])
            finally:
                with _COND:
                    _PENDING -= 1
                    _COND.notify_all()
    finally:
        if not clean:
            with _COND:
                _WORKERS -= 1
                restart = bool(_QUEUE) and _WORKERS < MAX_CONCURRENT_SYNCS
                if restart:
                    _WORKERS += 1
            if restart:
                _spawn()


# One writer at a time on a shared tool log: two pool workers finish together and append to the same
# `artifact_sync.log`.
_LOG_WRITE_LOCK = threading.Lock()
_MASK = "***REDACTED_ENV***"


def mask_tool_text(text, env=None) -> str:
    """`text` as it may be WRITTEN anywhere: the passthrough VALUES this command was handed masked by
    identity — whatever their names, which `core/redact.py`'s env screen judges by shape and so would
    miss for `MC_HOST_minio` — then the always-on screen (`redact_output_tail`: this box's own secret
    env values and every known credential shape; the entropy pass is not applied to a tool log, whose
    paths and hashes it would eat). A value under 4 characters is not masked: it is no credential,
    and replacing it everywhere would garble the log."""
    from looplab.core.redact import redact_output_tail
    text = str(text or "")
    values = {v for v in (env or {}).values() if isinstance(v, str) and len(v) >= 4}
    for value in sorted(values, key=len, reverse=True):
        if value in text:
            text = text.replace(value, _MASK)
    return redact_output_tail(text, entropy=False)


def append_tool_log(path, argv, out, err, env=None) -> None:
    """Append one finished run of an operator tool to its log, MASKED (`mask_tool_text`).

    WHY NOT `run_argv(log_path=…)` (round 3): that mirrors the child's raw bytes live, so a tool that
    echoes its environment or a URL carrying the key (`set -x`, an `MC_HOST_*` in an error) left the
    passthrough secret in a run-directory file every later eval may read. The output is captured
    (bounded, as `run_argv` returns it) and written once, after the process exits, before anything
    else can read it; the price is that the log is no longer tail-able while the copy runs."""
    body = (f"$ {' '.join(str(a) for a in argv)}\n"
            + (f"{out}\n" if out else "") + (f"{err}\n" if err else ""))
    with _LOG_WRITE_LOCK, open(path, "a", encoding="utf-8") as fh:
        fh.write(mask_tool_text(body, env))


def wait_for_inflight(timeout: Optional[float] = None) -> bool:
    """Wait until every copy this process accepted has finished; True when none is pending."""
    with _COND:
        return _COND.wait_for(lambda: _PENDING == 0, timeout)


def _run(engine, node_id, generation, argv, workdir, run_dir, timeout, env=None,
         sync_id=None, cwd=None) -> None:
    from looplab.runtime.sandbox import _run_argv
    _engine_redact = getattr(engine, "_redact", None) or (lambda text: text)

    def redact(text):
        # The passthrough values first (`mask_tool_text`), then the engine's own funnel.
        return _engine_redact(mask_tool_text(text, env))

    started = time.monotonic()
    before = workdir_stamp(workdir)
    out = ""
    try:
        rc, out, err, timed = _run_argv(argv, cwd or str(workdir), timeout,
                                        env=dict(env or {}))
    except (OSError, ValueError) as exc:          # no such tool, an unusable cwd
        rc, err, timed = -1, f"{type(exc).__name__}: {exc}", False
    try:
        append_tool_log(run_dir / "artifact_sync.log", argv, out, err, env)
    except OSError:
        _LOG.warning("artifact sync of node %s: could not write artifact_sync.log", node_id,
                     exc_info=True)
    seconds = round(time.monotonic() - started, 3)
    # A reset of this node re-materializes the workdir while the copy reads it; the receipt says so
    # instead of vouching for a tree that may mix two lifecycles (critic 2026-10-08).
    changed = before is not None and workdir_stamp(workdir) != before
    if rc != 0 or timed:
        _LOG.warning("artifact sync of node %s failed (exit %s%s); the node is unaffected — see "
                     "artifact_sync.log", node_id, rc, ", timed out" if timed else "")
    try:
        engine.store.append(EV_ARTIFACT_SYNCED, {
            "node_id": node_id, "generation": generation,
            **({"sync_id": sync_id} if sync_id else {}),
            "command": [redact(a) for a in argv], "exit_code": rc, "timed_out": bool(timed),
            **({"workdir_changed": True} if changed else {}),
            "seconds": seconds,
            # Redacted WHOLE, then cut (`engine/audit.py::Engine._redact`): a cut first can leave
            # the tail of a secret that straddled it unmasked.
            "stderr_tail": redact(str(err or ""))[-_STDERR_TAIL:]})
    except OSError:
        _LOG.warning("artifact sync of node %s: could not record its receipt", node_id,
                     exc_info=True)
