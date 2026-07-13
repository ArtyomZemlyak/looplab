"""Background command manager for the assistant (Claude-Code `run_in_background` / `BashOutput`).

A long command (a full pytest, a training run, a build) shouldn't block the chat turn. `start()` spawns
the process detached, streaming its combined stdout+stderr to a log file; `read()` returns only the
NEW output since the last read (a byte cursor) plus the live/exit status, so a later turn can poll it.
One bounded chunk per read (backpressure): the cursor advances ONLY by what was returned, so output
past the budget is delivered by the NEXT poll instead of being consumed and truncated away. Each poll
is an INCREMENTAL seek-read (open, seek(cursor), read one chunk) — never a whole-log `read_bytes()`,
whose per-poll cost grew with the log. The unread backlog is BOUNDED: when a chatty child outruns the
polls by more than `_BACKLOG_CAP` bytes, the cursor jumps forward and the chunk STARTS with an explicit
'…(N bytes of older output skipped — full log: <path>)…' note (honest truncation — the model is told
what was dropped and where the full log lives) instead of doomed catch-up polls over megabytes. The
manager is process-global so tasks survive across turns within the server.

Env is scrubbed of secret-looking vars (same rule as sandbox._run_argv) so a background process can't
leak the LLM key etc. into its log.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from looplab.core.gitenv import git_config_env
from looplab.runtime.sandbox import SECRET_ENV, _kill_tree
from looplab.core.context_budget import RESULT_CAP   # the agent loop's per-result cap (core home —
# runtime must not import tools/: tools sits ABOVE runtime and already imports back into it)

# Per-poll chunk budget. The old 8000 was DOUBLE the loop's result cap: the loop cut the reply's tail
# while the cursor had already advanced past the WHOLE log — mid-log output was consumed and
# unrecoverable, with a truncation marker advising a 'narrower range' this tool doesn't have. Stay
# under the cap (headroom for the shell tool's status head + '(more output pending)' note) and let
# the cursor backpressure deliver the rest on the next poll.
_MAX_READ = RESULT_CAP - 400

# Unread-backlog bound. Without one, a chatty child (a verbose training loop) outruns the ~4KB polls
# without limit and the model faces megabytes of doomed catch-up reads. When the backlog exceeds this,
# `read()` advances the cursor to the last _BACKLOG_CAP bytes and PREPENDS an explicit skip note
# (honest truncation: say what was dropped and where the full log lives — never drop silently).
_BACKLOG_CAP = 262_144

# Wall-clock lifetime bound for a background command. The assistant's background shell is for BOUNDED
# helpers (a full test run, a build, a short training) — the engine's real ML training goes through the
# sandbox eval path with its own timeout, not here — so a generous 2h cap can't abort legitimate work,
# but it reaps a hung/runaway child that would otherwise leak a process (+ its growing log) for the life
# of the server. Enforced by the always-on watcher thread (below) AND opportunistically on read()/list().
_BG_MAX_SECONDS = 7200.0
# Bound retained FINISHED tasks (and their tmp log files): a long server session would else accumulate
# one log per background command forever. Running tasks are never evicted.
_MAX_FINISHED = 32
# Cadence of the always-on deadline watcher (below). Lazy read()/list() enforcement alone let a hung
# task NOBODY polls leak a process (+ growing log) for the whole server lifetime; a daemon thread now
# sweeps deadlines on this interval so a wedged child is reaped within ~one tick of its budget even
# with zero polls. Small vs the 2h budget, generous vs. per-task cost (a lock-guarded snapshot + poll).
_WATCH_INTERVAL = 30.0


def _child_env(argv) -> dict:
    base = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    if argv and argv[0] == "git":     # restore ONLY git config + identity (not credential-bearing vars)
        base.update(git_config_env())
    base.setdefault("PYTHONUNBUFFERED", "1")
    return base


class BackgroundManager:
    def __init__(self, max_seconds: float = _BG_MAX_SECONDS, max_finished: int = _MAX_FINISHED,
                 watch_interval: float = _WATCH_INTERVAL):
        self._tasks: dict = {}
        self._lock = threading.Lock()
        self._max_seconds = max_seconds
        self._max_finished = max_finished
        # Always-on deadline watcher (lazily spawned on first start()): a daemon thread so merely
        # IMPORTING this module (tests, short CLI paths) never starts a thread, and it never blocks
        # interpreter exit. `watch_interval=0`/None disables it, falling back to lazy-only enforcement.
        self._watch_interval = watch_interval
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, argv, cwd: str, wrap=None) -> str:
        run_argv = wrap(argv, cwd) if wrap else list(argv)
        tid = secrets.token_hex(6)
        log = Path(tempfile.gettempdir()) / f"looplab-bg-{tid}.log"
        f = open(log, "wb")
        kwargs = {}
        if os.name == "nt":
            # A process GROUP on Windows too (arch-review §4 P1-4): without it a child's grandchildren
            # (workers/nested trains) orphan. _kill_tree's taskkill /T reaps the tree either way, but
            # the group keeps signalling coherent — matching run_argv's own creationflags.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
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
                                "cmd": " ".join(argv), "cwd": cwd,
                                "timed_out": False,
                                # Serialize deadline enforcement per task without blocking readers:
                                # watcher/read/list may sweep concurrently, but only one may act on
                                # this PID at a time (the second kill would carry a PID-reuse risk).
                                "deadline_lock": threading.Lock(),
                                "deadline": (time.monotonic() + self._max_seconds
                                             if self._max_seconds else None)}
        self._evict_finished()   # bound retained finished tasks + their log files
        self._ensure_watcher()   # start the always-on deadline sweeper on first use
        return tid

    def _ensure_watcher(self) -> None:
        """Start the deadline watcher thread on first use (double-checked under the lock). Idempotent:
        one thread per manager for its whole life. Skipped when the interval is disabled."""
        if not self._watch_interval or self._watcher is not None:
            return
        with self._lock:
            if self._watcher is None:
                self._watcher = threading.Thread(target=self._watch_loop,
                                                 name="looplab-bg-deadline", daemon=True)
                self._watcher.start()

    def _watch_loop(self) -> None:
        # Sweep on a fixed cadence until shutdown(). Wrapped so one bad task never kills the watcher —
        # it just retries next tick; the whole point is that enforcement no longer depends on a poll.
        while not self._stop.wait(self._watch_interval):
            try:
                self._sweep_deadlines()
            except Exception:  # noqa: BLE001 — a watchdog must survive any single-task error
                pass

    def _sweep_deadlines(self) -> None:
        """Enforce deadlines + reap exited children across ALL tasks, then bound retained logs — the
        same work read()/list() do lazily, now driven by the watcher. Snapshot under the lock, then act
        outside it: `_enforce_deadline`/`_reap` operate on the handle directly and must not re-enter it."""
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            self._enforce_deadline(t)
            self._reap(t)
        self._evict_finished()

    def shutdown(self) -> None:
        """Stop the watcher thread (idempotent). For clean teardown/tests; the global MANAGER never
        needs it because the thread is a daemon."""
        self._stop.set()
        w = self._watcher
        if w is not None:
            w.join(timeout=5)

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

    @staticmethod
    def _enforce_deadline(t) -> None:
        """Reap a background task that outlived its wall-clock budget (SIGTERM to the process group,
        like `kill`). Driven by the always-on watcher thread and also called opportunistically on
        read()/list(). Idempotent; a None deadline (timeout disabled) is a no-op. Operates on the
        handle `t` directly (never re-enters the lock)."""
        dl = t.get("deadline")
        if dl is None or time.monotonic() <= dl or t["proc"].poll() is not None:
            return
        deadline_lock = t["deadline_lock"]
        if not deadline_lock.acquire(blocking=False):
            return
        try:
            # Re-check after acquiring: another sweep may have killed/reaped the process while this
            # caller was racing for the task. Never issue a second kill against a stale/reused PID.
            if t["proc"].poll() is not None:
                return
            # Escalating WHOLE-TREE kill (psutil / taskkill /T / killpg -9), not a single SIGTERM a
            # stuck child can ignore forever — the old code sent one TERM and then latched timed_out,
            # permanently suppressing any retry (arch-review §4 P1-4). _kill_tree force-kills the tree.
            # Publish the timeout before killing: Windows can expose the process exit while taskkill
            # is still returning, and observers must not see an exited task without its final status.
            # Do not use this flag as an early-return latch: if a best-effort tree kill was a no-op,
            # the next watcher/read sweep must retry while the process remains alive.
            t["timed_out"] = True
            _kill_tree(t["proc"])
        finally:
            deadline_lock.release()

    def _evict_finished(self) -> None:
        """Drop the OLDEST finished tasks (insertion order) once more than `max_finished` are retained,
        unlinking their tmp log files. Running tasks are never evicted; a later read() of an evicted id
        just returns 'no such background task' (graceful), same as any unknown id."""
        if not self._max_finished:
            return
        with self._lock:
            finished = [tid for tid, t in self._tasks.items() if t["proc"].poll() is not None]
            for tid in finished[:-self._max_finished] if len(finished) > self._max_finished else []:
                t = self._tasks.pop(tid, None)
                if t is None:
                    continue
                try:
                    t["fh"].close()
                except OSError:
                    pass
                try:
                    Path(t["log"]).unlink()
                except OSError:
                    pass

    def read(self, tid: str) -> dict:
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return {"ok": False, "error": f"no such background task {tid!r}"}
            # The WHOLE cursor-read → chunk-slice → cursor-advance sequence runs under the lock: two
            # concurrent polls that both read the cursor, then both `cursor += len(chunk)`, would
            # jointly advance it past a chunk only ONE of them returned — output permanently skipped
            # (the pre-incremental absolute assignment was race-benign; `+=` is not). The file read
            # itself is bounded (one seek + ≤ _MAX_READ+4 bytes), so holding the lock across it is fine.
            skip_note = ""
            try:
                size = os.stat(t["log"]).st_size
            except OSError:
                size = t["cursor"]
            if size - t["cursor"] > _BACKLOG_CAP:
                # Bounded backlog: jump the cursor to the newest _BACKLOG_CAP bytes and SAY SO in the
                # chunk (honest truncation) — the older output stays recoverable in the log file.
                skipped = size - t["cursor"] - _BACKLOG_CAP
                t["cursor"] = size - _BACKLOG_CAP
                skip_note = f"…({skipped} bytes of older output skipped — full log: {t['log']})…\n"
            try:
                # Incremental seek-read: only the next chunk, never the whole log. +4 bytes of slack
                # so a chunk that exactly fills the budget is distinguishable from one that was cut
                # (the UTF-8 boundary strip below keys on `len(chunk) < len(new)`).
                with open(t["log"], "rb") as lf:
                    lf.seek(t["cursor"])
                    new = lf.read(_MAX_READ + 4)
            except OSError:
                new = b""
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
            pending = max(0, size - t["cursor"])
            text = skip_note + chunk.decode("utf-8", "replace")
        self._enforce_deadline(t)   # reap a task past its wall-clock budget before reporting status
        self._reap(t)
        rc = t["proc"].poll()
        return {"ok": True, "task_id": tid, "cmd": t["cmd"],
                "status": "running" if rc is None else "exited", "exit_code": rc,
                "new_output": text, "pending": pending, "timed_out": t.get("timed_out", False)}

    def list(self) -> list:
        with self._lock:
            items = list(self._tasks.items())
        out = []
        for tid, t in items:
            self._enforce_deadline(t)
            self._reap(t)
            rc = t["proc"].poll()
            out.append({"task_id": tid, "cmd": t["cmd"],
                        "status": "running" if rc is None else "exited", "exit_code": rc,
                        "timed_out": t.get("timed_out", False)})
        return out

    def kill(self, tid: str) -> dict:
        with self._lock:
            t = self._tasks.get(tid)
        if not t:
            return {"ok": False, "error": f"no such background task {tid!r}"}
        proc = t["proc"]
        # Serialize an explicit kill with deadline enforcement too. Otherwise the user-facing kill
        # can race the watcher exactly like two sweeps and issue a second tree-kill for a stale PID.
        with t["deadline_lock"]:
            if proc.poll() is None:
                # Robust TREE kill + VERIFY (arch-review §4 P1-4): the old kill sent one
                # SIGTERM/terminate, never waited, never escalated, and ALWAYS reported success — a
                # parent could die while its children lived on. _kill_tree escalates to
                # SIGKILL/taskkill /F over the whole tree; then wait so we report the ACTUAL outcome.
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001 — still alive; reported below
                    pass
        try:
            t["fh"].close()
        except OSError:
            pass
        t["closed"] = True
        rc = proc.poll()
        if rc is None:                       # still alive after tree-kill + wait — do NOT claim success
            return {"ok": False, "task_id": tid, "status": "kill_failed",
                    "error": "process did not exit after tree-kill"}
        return {"ok": True, "task_id": tid, "status": "killed", "exit_code": rc}


# Process-global manager so background tasks persist across assistant turns.
MANAGER = BackgroundManager()
