"""Background command manager for the assistant (Claude-Code `run_in_background` / `BashOutput`).

A long command (a full pytest, a training run, a build) shouldn't block the chat turn. `start()` spawns
the process detached, streaming its combined stdout+stderr to a log file; `read()` returns only the
NEW output since the last read (a byte cursor) plus the live/exit status, so a later turn can poll it.
The manager is process-global so tasks survive across turns within the server.

Env is scrubbed of secret-looking vars (same rule as sandbox._run_argv) so a background process can't
leak the LLM key etc. into its log.
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from .sandbox import _SECRET_ENV

_MAX_READ = 8000


def _child_env(argv) -> dict:
    base = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    if argv and argv[0] == "git":     # restore ONLY git config + identity (not credential-bearing vars)
        from .shell_tools import git_config_env
        base.update(git_config_env())
    base.setdefault("PYTHONUNBUFFERED", "1")
    return base


class BackgroundManager:
    def __init__(self):
        self._tasks: dict = {}
        self._lock = threading.Lock()

    def start(self, argv, cwd: str, wrap=None) -> str:
        run_argv = wrap(argv, cwd) if wrap else list(argv)
        tid = secrets.token_hex(6)
        log = Path(tempfile.gettempdir()) / f"looplab-bg-{tid}.log"
        f = open(log, "wb")
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(run_argv, cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                                env=_child_env(argv), **kwargs)
        with self._lock:
            self._tasks[tid] = {"proc": proc, "log": log, "fh": f, "cursor": 0,
                                "cmd": " ".join(argv), "cwd": cwd}
        return tid

    @staticmethod
    def _reap(t) -> None:
        """Close our copy of the log write-handle once the child has exited (else one fd leaks per
        background command for the life of the process)."""
        if t["proc"].poll() is not None and not t.get("closed"):
            try:
                t["fh"].close()
            except OSError:
                pass
            t["closed"] = True

    def read(self, tid: str) -> dict:
        with self._lock:
            t = self._tasks.get(tid)
        if not t:
            return {"ok": False, "error": f"no such background task {tid!r}"}
        try:
            data = t["log"].read_bytes()
        except OSError:
            data = b""
        new = data[t["cursor"]:]
        t["cursor"] = len(data)
        text = new.decode("utf-8", "replace")
        if len(text) > _MAX_READ:
            text = "…(truncated)…\n" + text[-_MAX_READ:]
        self._reap(t)
        rc = t["proc"].poll()
        return {"ok": True, "task_id": tid, "cmd": t["cmd"],
                "status": "running" if rc is None else "exited", "exit_code": rc, "new_output": text}

    def list(self) -> list:
        with self._lock:
            items = list(self._tasks.items())
        out = []
        for tid, t in items:
            self._reap(t)
            rc = t["proc"].poll()
            out.append({"task_id": tid, "cmd": t["cmd"],
                        "status": "running" if rc is None else "exited", "exit_code": rc})
        return out

    def kill(self, tid: str) -> dict:
        with self._lock:
            t = self._tasks.get(tid)
        if not t:
            return {"ok": False, "error": f"no such background task {tid!r}"}
        proc = t["proc"]
        if proc.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except (OSError, ProcessLookupError):
                pass
        try:
            t["fh"].close()
        except OSError:
            pass
        t["closed"] = True
        return {"ok": True, "task_id": tid, "status": "killed"}


# Process-global manager so background tasks persist across assistant turns.
MANAGER = BackgroundManager()
