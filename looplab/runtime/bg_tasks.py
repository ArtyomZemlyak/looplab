"""Background command manager for the assistant (Claude-Code `run_in_background` / `BashOutput`).

A long command (a full pytest, a training run, a build) shouldn't block the chat turn. `start()` spawns
the process detached, streaming its combined stdout+stderr to a log file; `read()` returns only the
NEW output since the last read (a byte cursor) plus the live/exit status, so a later turn can poll it.
One bounded chunk per read (backpressure): the cursor advances ONLY by what was returned, so output
past the budget is delivered by the NEXT poll instead of being consumed and truncated away. The
manager is process-global so tasks survive across turns within the server.

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

from looplab.runtime.sandbox import SECRET_ENV
from looplab.tools._base import RESULT_CAP   # the agent loop's per-result cap (tools/_base.py)

# Per-poll chunk budget. The old 8000 was DOUBLE the loop's result cap: the loop cut the reply's tail
# while the cursor had already advanced past the WHOLE log — mid-log output was consumed and
# unrecoverable, with a truncation marker advising a 'narrower range' this tool doesn't have. Stay
# under the cap (headroom for the shell tool's status head + '(more output pending)' note) and let
# the cursor backpressure deliver the rest on the next poll.
_MAX_READ = RESULT_CAP - 400


def _child_env(argv) -> dict:
    base = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    if argv and argv[0] == "git":     # restore ONLY git config + identity (not credential-bearing vars)
        from looplab.tools.shell_tools import git_config_env
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
        try:
            proc = subprocess.Popen(run_argv, cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                                    env=_child_env(argv), **kwargs)
        except OSError:
            # e.g. binary not found: don't leak the open fd + stray log file on the failed start.
            f.close()
            try:
                log.unlink()
            except OSError:
                pass
            raise
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
        chunk = new[:_MAX_READ]
        if len(chunk) < len(new):
            # Don't split a multi-byte UTF-8 char at the budget edge: back up over the (≤3)
            # continuation bytes and the lead byte so the next poll re-reads the whole char (both
            # halves would otherwise decode to U+FFFD). Bounded strip — arbitrary binary output can't
            # walk the chunk empty and stall the cursor.
            for _ in range(3):
                if chunk and (chunk[-1] & 0xC0) == 0x80:
                    chunk = chunk[:-1]
            if chunk and chunk[-1] >= 0xC0:
                chunk = chunk[:-1]
        # Advance the cursor ONLY by what we return (backpressure): the next poll continues exactly
        # where this reply ended, so nothing past the budget is consumed-then-truncated away.
        t["cursor"] += len(chunk)
        pending = len(new) - len(chunk)
        text = chunk.decode("utf-8", "replace")
        self._reap(t)
        rc = t["proc"].poll()
        return {"ok": True, "task_id": tid, "cmd": t["cmd"],
                "status": "running" if rc is None else "exited", "exit_code": rc,
                "new_output": text, "pending": pending}

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
