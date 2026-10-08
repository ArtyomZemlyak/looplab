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

* IN A BACKGROUND THREAD, after the terminal. A copy of gigabytes is minutes of wall clock, and doing
  it inside `_evaluate` would hold the eval slot — the GPU lease — for an upload. The thread is
  NON-daemon, so the interpreter waits for an in-flight copy at exit (bounded by its `timeout`)
  instead of killing the copy of the run's last node.
* A RECEIPT, NEVER A VERDICT. The `artifact_synced` row is DIAGNOSTIC (invariant #1: appendable from a
  thread, fold-ignored, fence-neutral). A failed copy is reported in it and in `artifact_sync.log`; it
  never fails, pauses or re-runs the node, whose metric is already on its terminal.
* WHAT THE OPERATOR DECLARED, ONLY. The engine never picks a destination or a tool; an absent
  declaration runs nothing.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from looplab.events.types import EV_ARTIFACT_SYNCED

_LOG = logging.getLogger(__name__)

PLACEHOLDERS: tuple[str, ...] = ("workdir", "run_dir", "run_id", "node_id", "generation")
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_STDERR_TAIL = 400

# The threads started in THIS process, so a test (or a caller that must know the copies are done)
# can wait on them. Pruned of finished threads on every start.
_INFLIGHT: list = []
_INFLIGHT_LOCK = threading.Lock()


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


def sync_cwd(workdir, run_dir, env) -> str:
    """Where an operator copy-out or track runs: the node's WORKDIR, as documented (a relative
    `./` or `score.py` names the node's files) — except when it is handed credentials
    (`env_passthrough`), which run from the RUN directory (critic 2026-10-08): the workdir is the
    candidate's, and a `python -m <tool>` started there imports whatever module the candidate left
    beside the key. Two rounds of review: always-run-dir broke every workdir-relative command."""
    return str(run_dir if env else workdir)


def workdir_stamp(workdir: Path) -> Optional[bytes]:
    """The bytes of a node workdir's `.looplab-manifest` stamp, or None. Through THE reader of a file
    a candidate can write (`core/node_evidence.py::read_bounded_regular_file`): the stamp is written
    before the candidate's eval runs in that same directory, which may replace it with a FIFO or a
    multi-gigabyte file. Shared with `maintenance/evaluate_track.py`."""
    from looplab.core.node_evidence import read_bounded_regular_file
    return read_bounded_regular_file(Path(workdir) / ".looplab-manifest", 256)


def start_artifact_sync(engine, node_id: int, generation: int) -> Optional[threading.Thread]:
    """Start the declared copy-out of node `node_id`'s workdir in a background thread, or None
    (nothing declared, or no workdir to copy). Never raises."""
    spec = sync_spec(getattr(engine, "_eval_spec", None))
    if spec is None:
        return None
    run_dir = Path(engine.run_dir)
    workdir = run_dir / "nodes" / f"node_{node_id}"
    try:
        if not workdir.is_dir():
            return None
    except OSError:
        return None
    values = {"workdir": str(workdir), "run_dir": str(run_dir), "run_id": run_dir.name,
              "node_id": node_id, "generation": generation}
    argv = render_argv(spec["command"], values)
    try:
        timeout = float(spec.get("timeout") or 1800.0)
    except (TypeError, ValueError):
        timeout = 1800.0
    worker = threading.Thread(target=_run, args=(engine, node_id, generation, argv, workdir,
                                                  run_dir, timeout, passthrough_env(spec)),
                              name=f"looplab-artifact-sync:{node_id}", daemon=False)
    with _INFLIGHT_LOCK:
        _INFLIGHT[:] = [t for t in _INFLIGHT if t.is_alive()]
        _INFLIGHT.append(worker)
    worker.start()
    return worker


def wait_for_inflight(timeout: Optional[float] = None) -> bool:
    """Join every copy this process started; True when none is still running."""
    with _INFLIGHT_LOCK:
        threads = list(_INFLIGHT)
    deadline = None if timeout is None else time.monotonic() + timeout
    for t in threads:
        t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in threads)


def _run(engine, node_id, generation, argv, workdir, run_dir, timeout, env=None) -> None:
    from looplab.runtime.sandbox import _run_argv
    redact = getattr(engine, "_redact", None) or (lambda text: text)
    started = time.monotonic()
    before = workdir_stamp(workdir)
    try:
        rc, _out, err, timed = _run_argv(argv, sync_cwd(workdir, run_dir, env), timeout,
                                         env=dict(env or {}),
                                         log_path=str(run_dir / "artifact_sync.log"))
    except (OSError, ValueError) as exc:          # no such tool, an unusable cwd
        rc, err, timed = -1, f"{type(exc).__name__}: {exc}", False
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
            "command": [redact(a) for a in argv], "exit_code": rc, "timed_out": bool(timed),
            **({"workdir_changed": True} if changed else {}),
            "seconds": seconds,
            # Redacted WHOLE, then cut (`engine/audit.py::Engine._redact`): a cut first can leave
            # the tail of a secret that straddled it unmasked.
            "stderr_tail": redact(str(err or ""))[-_STDERR_TAIL:]})
    except OSError:
        _LOG.warning("artifact sync of node %s: could not record its receipt", node_id,
                     exc_info=True)
