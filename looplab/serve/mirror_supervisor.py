"""The UI server's run mirror: `looplab run-mirror --loop` as a SUPERVISED CHILD (doc 82 П6).

WHY A CHILD, NOT A THREAD. A destination call on the S3 FUSE mount can hang in the kernel for days
(`events/run_mirror.py`). Such a call can only ever hold up the process that made it, so the mirror
stays its own process: the server starts it, watches it, and restarts it — a `--exit-on-stuck`
exit (3) after a hung pass, a crash, anything but a deliberate refusal. Nothing here copies a byte.

WHEN. Only with `LOOPLAB_RUN_MIRROR_DST` set (doc 82 Р7: no default destination). Without it the
banner (`GET /api/run-mirror`) tells the operator a tmpfs run root is unprotected.

ONE MIRROR PER ROOT. The child takes `src_root/.looplab-mirror/mirror.lock`; when an operator's own
`run-mirror --loop` already holds it, the child refuses (exit 2) and the supervisor records
`external` and asks again only every `busy_retry_s` — it never runs a second copier beside it.

STOPPING. Shutdown sends SIGTERM and waits: the mirror publishes every file by an atomic rename, so
a pass cut short leaves the last good copy, never half a file. The child is started in its own
session without the hub's reap-on-exit marker, like every other process this server owns.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from looplab.serve.jupyter import REAP_ON_EXIT_ENV

_log = logging.getLogger("looplab.server")

MIRROR_DST_ENV = "LOOPLAB_RUN_MIRROR_DST"
REFUSAL_EXIT = 2          # `cli.REFUSAL_EXIT_CODE`: another mirror holds the lock, or a bad --dst
STUCK_EXIT = 3            # `run_mirror.mirror_loop`: a destination call hung


class MirrorSupervisor:
    def __init__(self, src_root, dst_root, *, interval_s: float = 300.0, poll_s: float = 2.0,
                 busy_retry_s: float = 300.0, max_backoff_s: float = 300.0,
                 popen=subprocess.Popen, clock=time.monotonic):
        self.src_root = Path(src_root)
        self.dst_root = Path(dst_root)
        self.interval_s, self.poll_s = float(interval_s), float(poll_s)
        self.busy_retry_s, self.max_backoff_s = float(busy_retry_s), float(max_backoff_s)
        self._popen, self._clock = popen, clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.child = None
        self.state = "starting"
        self.restarts = 0
        self.last_exit: Optional[int] = None
        self.started_at: Optional[float] = None
        self._next_start = 0.0
        self._failures = 0

    def argv(self) -> list[str]:
        return [sys.executable, "-m", "looplab.cli", "run-mirror", "--src", str(self.src_root),
                "--dst", str(self.dst_root), "--loop", "--exit-on-stuck",
                "--interval", str(self.interval_s)]

    # lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="looplab-run-mirror", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            child = self.child
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.state = "stopped"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a supervisor fault must not take the UI server down
                _log.warning("run mirror supervisor tick failed", exc_info=True)
            self._stop.wait(self.poll_s)

    # one decision
    def tick(self) -> None:
        """Observe the child; (re)start it when it is gone and its wait is over."""
        now = self._clock()
        with self._lock:
            if self._stop.is_set():
                return
            child = self.child
            if child is not None:
                code = child.poll()
                if code is None:
                    self.state = "running"
                    return
                self.child, self.last_exit = None, code
                if code == REFUSAL_EXIT:
                    # Another mirror holds the lock (or the destination is refused): never spin.
                    self.state, self._next_start = "external", now + self.busy_retry_s
                else:
                    self._failures += 1
                    self.state = "stuck" if code == STUCK_EXIT else "restarting"
                    self._next_start = now + min(self.max_backoff_s, 5.0 * 2 ** min(self._failures - 1, 20))
                return
            if now < self._next_start:
                return
            env = {k: v for k, v in os.environ.items() if k != REAP_ON_EXIT_ENV}
            try:
                self.child = self._popen(self.argv(), env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
            except OSError:
                self._failures += 1
                self.state = "restarting"
                self._next_start = now + min(self.max_backoff_s, 5.0 * 2 ** min(self._failures - 1, 20))
                return
            if self.started_at is not None:
                self.restarts += 1
            self.started_at, self.state = time.time(), "running"

    def status(self) -> dict:
        with self._lock:
            child = self.child
        return {"state": self.state, "pid": getattr(child, "pid", None) if child else None,
                "restarts": self.restarts, "last_exit_code": self.last_exit,
                "started_at": self.started_at}


def mirror_view(run_root, supervisor: Optional[MirrorSupervisor]) -> dict:
    """`GET /api/run-mirror`: is the run root in memory, is a mirror configured, when did it last
    finish a pass. Reads the source root and the mirror's state file only — never the destination."""
    from looplab.core.volatile_fs import is_volatile
    from looplab.events.run_mirror import MirrorConfig, mirror_status

    root = Path(run_root)
    dst = os.environ.get(MIRROR_DST_ENV) or None
    out = {"run_root": str(root), "volatile": is_volatile(root), "dst_root": dst,
           "supervisor": supervisor.status() if supervisor is not None else None,
           "last_pass_finished_at": None, "stuck": None, "errors": 0,
           "env": MIRROR_DST_ENV, "status_readable": False}
    if dst:
        try:
            status = mirror_status(MirrorConfig(src_root=root, dst_root=Path(dst)))
        except (OSError, ValueError):
            return out
        out.update(status_readable=True, last_pass_finished_at=status.get("last_pass_finished_at"),
                   stuck=status.get("stuck"), errors=len(status.get("errors") or []))
    return out


def mirror_diagnostic(view: dict, *, now: float | None = None) -> dict:
    """Report observations separately; a running child does not prove a successful mirror."""
    now = time.time() if now is None else now
    last = view.get("last_pass_finished_at")
    age = max(0, now - last) if isinstance(last, (int, float)) and not isinstance(last, bool) else None
    supervisor = view.get("supervisor") or {}
    if not view.get("dst_root"):
        state = "unconfigured"
    elif view.get("stuck") or view.get("errors") or supervisor.get("state") in {"stuck", "restarting", "failed"}:
        state = "degraded"
    elif not view.get("status_readable") or age is None:
        state = "unknown"
    elif age > 180:
        state = "stale"
    else:
        state = "observed_recent"
    return {"state": state, "last_pass_finished_at": last, "last_pass_age_seconds": age,
            "stuck": bool(view.get("stuck")), "errors": view.get("errors"),
            "supervisor_state": supervisor.get("state"), "supervisor_restarts": supervisor.get("restarts"),
            "status_readable": view.get("status_readable", False),
            "meaning": "Local mirror metadata only; recent pass and readback do not prove durable storage flush."}
