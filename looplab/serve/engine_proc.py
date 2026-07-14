"""Engine-process plumbing for the UI server: liveness probing (`_engine_alive`), spawning
detached engine runs (`_spawn_engine`), and the JupyterHub-only reaper that stops spawned engines
when the single-user server shuts down. Extracted verbatim from `serve/server.py` (BACKLOG §4);
`looplab.serve.server` re-exports `_engine_alive`/`_kill_process_tree` so the historical
`looplab.server._engine_alive` import path keeps working for tests and callers."""
from __future__ import annotations

import atexit
import errno
import hashlib
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional


def _on_shared_hub() -> bool:
    """True when this process looks like a JupyterHub single-user server reached through
    `jupyter-server-proxy` (https://hub/user/<name>/proxy/<port>/). That is a SHARED origin: the
    same-origin policy is per-ORIGIN, not per-path, so a same-origin page on a *different path*
    (another proxied app, a file the user opens under /user/<name>/files/...) can read anything
    served on this origin — including an injected UI token. Detected via env JupyterHub sets in
    every single-user server; absent on the default local single-user path."""
    return bool(os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
                or os.environ.get("JUPYTERHUB_API_TOKEN"))


def _engine_liveness(rd: Path) -> Optional[bool]:
    """True when held, False when definitively free/absent, None when the probe is inconclusive."""
    lock = rd / "engine.lock"
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

    def _is_reparse(entry) -> bool:
        attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
        return bool(reparse_flag and attributes & reparse_flag)

    try:
        run_entry = rd.lstat()
    except FileNotFoundError:
        return False  # required for a not-yet-materialized, validated new-start path
    except OSError:
        return None
    if (stat.S_ISLNK(run_entry.st_mode) or not stat.S_ISDIR(run_entry.st_mode)
            or _is_reparse(run_entry)):
        return None
    try:
        canonical_run = rd.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None

    def _run_dir_unchanged() -> bool:
        try:
            current = rd.lstat()
            return bool(
                stat.S_ISDIR(current.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and not _is_reparse(current)
                and (current.st_dev, current.st_ino, current.st_mode)
                == (run_entry.st_dev, run_entry.st_ino, run_entry.st_mode)
                and rd.resolve(strict=True) == canonical_run
            )
        except (FileNotFoundError, OSError):
            return False

    try:
        # ``Path.exists`` follows links, so checking it first misclassified a dangling
        # ``engine.lock`` symlink as authoritative absence.  Inspect the directory entry itself:
        # any link/reparse/special inode is untrusted ownership evidence, never permission to
        # mutate the run or launch another writer.
        entry = lock.lstat()
    except FileNotFoundError:
        # Revalidate the directory identity before authorizing a no-lock verdict; it may have been
        # swapped to a symlink/reparse point between the directory and lock metadata probes.
        return False if _run_dir_unchanged() else None
    except OSError:
        return None
    try:
        if (stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode)
                or _is_reparse(entry)):
            return None
        if lock.resolve(strict=True).parent != canonical_run:
            return None
    except FileNotFoundError:
        # The lock entry changed or became dangling after lstat. It was observed, so this is not
        # proof of absence.
        return None
    except OSError:
        return None
    fd = None
    try:
        # Open an existing inode only and refuse a link swap on platforms with O_NOFOLLOW.  The
        # fstat identity check closes the regular-file replacement race on the remaining platforms.
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock, flags)
        opened = os.fstat(fd)
        if ((entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode)):
            os.close(fd)
            fd = None
            return None
        f = os.fdopen(fd, "r+b", buffering=0)
        fd = None
    except FileNotFoundError:
        # Unlike a clean initial lstat miss, disappearance after an observed entry is a race.
        return None
    except OSError:
        if fd is not None:
            os.close(fd)
        return None

    def _lock_entry_unchanged() -> bool:
        try:
            current = lock.lstat()
            return bool(
                stat.S_ISREG(current.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and not _is_reparse(current)
                and (current.st_dev, current.st_ino, current.st_mode)
                == (entry.st_dev, entry.st_ino, entry.st_mode)
                == (opened.st_dev, opened.st_ino, opened.st_mode)
                and lock.resolve(strict=True).parent == canonical_run
            )
        except (FileNotFoundError, OSError):
            return False

    def _ownership_paths_unchanged() -> bool:
        return _run_dir_unchanged() and _lock_entry_unchanged()

    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return True if _ownership_paths_unchanged() else None
                return None
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            return False if _ownership_paths_unchanged() else None
        import fcntl
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True if _ownership_paths_unchanged() else None
        except OSError:
            return None
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False if _ownership_paths_unchanged() else None
    except OSError:
        return None
    finally:
        f.close()


def _engine_alive(rd: Path) -> bool:
    """Conservative boolean compatibility API: only a proven-free lock is treated as stopped."""
    return _engine_liveness(rd) is not False


def _spawn_liveness(rd: Path) -> Optional[bool]:
    """Tri-state spawn probe with the historical bool monkeypatch seam preserved.

    Production's bool wrapper is conservative, so the second probe can only turn a raced exact-False
    into a safe True. Tests/downstream callers that monkeypatch `_engine_alive` retain their existing
    live-flip/cancellation seam; an initial None is never delegated or collapsed.
    """
    liveness = _engine_liveness(rd)
    if liveness is False and _engine_alive(rd):
        return True
    return liveness


def _kill_process_tree(pid: int) -> None:
    """Best-effort terminate a spawned engine + its eval descendants. Guards against PID RECYCLING (a
    finished engine's pid reused by an unrelated process) by confirming the process still looks like a
    looplab engine before signalling — so the JupyterHub-cull reaper can never kill an innocent
    bystander. psutil (in the [proc]/[jupyterhub] extra) is the reliable recursive path; the POSIX
    process-group fallback (the engine leads its own session) is used only when psutil is absent."""
    try:
        import psutil  # optional extra
        proc = psutil.Process(pid)
        if "looplab" not in " ".join(proc.cmdline()).lower():
            return                       # pid recycled to something else — do NOT kill it
        victims = proc.children(recursive=True) + [proc]
        for p in victims:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(victims, timeout=3)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        return
    except ImportError:
        pass                             # no psutil — fall through to the POSIX group signal
    except Exception:                    # noqa: BLE001 - psutil: process already gone / access denied
        return
    if os.name == "nt":
        return                           # no psutil on Windows → can't safely reap a detached group
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"looplab" not in f.read():
                return                   # PID-recycle guard: not our engine anymore
    except OSError:
        return                           # no /proc, or the pid is already gone — nothing to reap
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


# PIDs of engines THIS server spawned — reaped on shutdown ONLY under JupyterHub (see below).
_spawned_engine_pids: set[int] = set()

# Serialize process creation against ASGI shutdown/reaping.  A resume timer used to be able to pass
# its ``shutdown.is_set()`` check, lose the CPU, and Popen *after* the JupyterHub reaper had taken its
# PID snapshot.  Holding this gate for check+Popen, and setting the shutdown event under the same
# gate, gives the two operations an unambiguous order.  RLock lets `_claim_and_spawn_resume` perform
# the guarded cancellation check around `_spawn_engine`, which also uses the gate for every spawn.
_engine_spawn_gate = threading.RLock()

# Resume, reset, and delete are one lifecycle transaction per run.  engine.lock fences a RUNNING
# engine, but it does not cover the claim -> Popen -> child-lock startup gap.  Pair a process-local
# RLock with a sibling interprocess lock whose inode survives deletion of the run directory; this
# prevents another server worker from archiving/removing a run after a durable launch claim but
# before its child owns engine.lock.
_run_lifecycle_locks_guard = threading.Lock()
_run_lifecycle_locks: dict[str, threading.RLock] = {}


def _run_lifecycle_key(rd: Path) -> str:
    return os.path.normcase(str(rd.resolve()))


def _run_lifecycle_lock_path(rd: Path) -> Path:
    digest = hashlib.sha256(_run_lifecycle_key(rd).encode("utf-8")).hexdigest()[:24]
    return rd.resolve().parent / f".looplab-lifecycle-{digest}.lock"


@contextmanager
def _run_lifecycle_lock(rd: Path):
    """Cross-thread/process fence for resume-claim, reset, and delete of one run."""
    from looplab.events.eventstore import _interprocess_lock

    key = _run_lifecycle_key(rd)
    with _run_lifecycle_locks_guard:
        local = _run_lifecycle_locks.setdefault(key, threading.RLock())
    with local, _interprocess_lock(_run_lifecycle_lock_path(rd)):
        yield


def sweep_stale_lifecycle_locks(root: Path, *, max_age_s: float = 3600.0) -> int:
    """Best-effort startup GC of orphaned per-run lifecycle lock files (F22). These live in the runs
    root and are deliberately never deleted inline (their inode is the fence during a run's own delete),
    so a long-lived server slowly accumulates one `.looplab-lifecycle-*.lock` dot-file per run ever
    resumed/reset/deleted. Remove one ONLY when it is (a) OLD — untouched for `max_age_s`, while a real
    lifecycle op touches its lock within seconds — AND (b) not currently held (a non-blocking flock
    acquires cleanly). Removing an unheld stale lock never breaks locking: a later op recreates the file
    on demand. Skips silently on any error or a mount without flock. Returns the count removed."""
    import time
    try:
        candidates = list(root.glob(".looplab-lifecycle-*.lock"))
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for lp in candidates:
        try:
            if now - lp.stat().st_mtime < max_age_s:
                continue                       # recently touched → an op may be using it; leave it
        except OSError:
            continue
        if os.name == "nt":
            try:                               # Windows refuses to unlink an open/locked file → skip
                lp.unlink()
                removed += 1
            except OSError:
                pass
            continue
        try:
            import fcntl
            with open(lp, "a") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue                   # held by a live op (or flock unsupported) → leave it
                try:
                    lp.unlink()                # unlink WHILE holding the flock: no op can be mid-write
                    removed += 1
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            continue
    return removed

# A resume can arrive after ``run_finished`` has landed but before the old engine releases its
# singleton lock (final read-model/trace writes still run).  Returning ``already_running`` in that
# window loses the wake-up: the old loop has already broken and no replacement is spawned.  Keep one
# in-process waiter per run so the accepted resume becomes a spawn immediately after lock release.
_resume_after_exit: set[str] = set()
_resume_after_exit_lock = threading.Lock()
_resume_waiter_threads: dict[str, tuple[threading.Thread, Optional[threading.Event]]] = {}


def _spawn_engine(cli_args: list[str], env: Optional[dict] = None,
                  run_dir: Optional[Path] = None) -> Optional[int]:
    cmd = [sys.executable, "-m", "looplab.cli", *cli_args]
    kw: dict = {"cwd": str(Path(__file__).resolve().parents[2])}
    if env:
        kw["env"] = {**os.environ, **env}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # detached, survives request
    else:
        kw["start_new_session"] = True
    # Capture the spawned engine's stderr to <run_dir>/engine.stderr.log instead of discarding it:
    # an engine that dies BEFORE its first event (a FUSE-degraded lock that bails, a tool missing
    # from PATH, no egress to the LLM) otherwise leaves a "phantom never-started run" with zero
    # diagnostics. stdout stays discarded — the engine's truth is events.jsonl, not stdout.
    err = subprocess.DEVNULL
    err_f = None
    if run_dir is not None:
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            # P1-4 bounded logs: engine.stderr.log is append-only across resumes, so a run whose engine
            # keeps crashing on startup (esp. one the P1-1 reconciler re-spawns) could grow it without
            # bound. Cap it: past the ceiling, keep only the most-recent half (the recent crash is what
            # matters) with a truncation marker. Best-effort — a stat/rewrite failure just skips it.
            _errlog = run_dir / "engine.stderr.log"
            try:
                if _errlog.exists() and _errlog.stat().st_size > _ENGINE_STDERR_CAP:
                    _tail = _errlog.read_bytes()[-(_ENGINE_STDERR_CAP // 2):]
                    _errlog.write_bytes(b"...(engine.stderr.log truncated to the recent tail)...\n" + _tail)
            except OSError:
                pass
            err_f = open(_errlog, "ab")
            err = err_f
        except OSError:
            err = subprocess.DEVNULL
    pid: Optional[int] = None
    try:
        with _engine_spawn_gate:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, **kw)
            raw_pid = getattr(proc, "pid", None)   # tests may stub Popen without a real integer pid
            pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            if pid is not None:
                _spawned_engine_pids.add(pid)
    finally:
        if err_f is not None:
            try:
                err_f.close()   # the child inherited its own dup; release the parent's handle
            except OSError:
                # Popen may already have succeeded. A FUSE close/flush error must not make callers
                # cancel the pre-spawn lease (or reset archives) underneath that live child.
                pass
    return pid


# P1-1 recoverable-intent reconciler grace: wait this long after a durable resume_requested before
# re-spawning it, so the ORIGINAL detached spawn has time to acquire the lock + append resume_served.
# Only a resume that stays unserved past this window is treated as a died-on-startup zombie.
_RESUME_RECONCILE_GRACE_S = 30.0

# P1-4 bounded logs: ceiling for the append-only engine.stderr.log before `_spawn_engine` truncates it
# to its recent tail — so a crash-looping (or reconciler-re-spawned) engine can't grow it without bound.
_ENGINE_STDERR_CAP = 8 * 1024 * 1024


def _resolve_task_file(rd: Path) -> Optional[str]:
    """Resolve the immutable run snapshot, with a safe legacy ``ui_meta`` fallback."""
    import json
    # The snapshot is the resolved, immutable task the run actually started with. ui_meta points at
    # mutable user input and is retained only for pre-snapshot legacy runs.
    snap = rd / "task.snapshot.json"
    if snap.is_file():
        return str(snap)
    meta = rd / "ui_meta.json"
    if meta.is_file():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
            tf = payload.get("task_file") if isinstance(payload, dict) else None
            if tf and Path(tf).is_file():
                return str(tf)
        except (OSError, UnicodeError, ValueError, TypeError):
            pass
    return None


def _within_resume_grace(ts: float, now: float) -> bool:
    """A wall-clock lease is fresh only when its age is non-negative and below the grace."""
    elapsed = now - float(ts or 0.0)
    return 0.0 <= elapsed < _RESUME_RECONCILE_GRACE_S


def _launch_claim_is_fresh(state, now: float) -> bool:
    """Whether a detached CLI launch is already in flight for this unserved intent."""
    return (state.last_resume_launch_seq > state.last_resume_served_seq
            and _within_resume_grace(state.last_resume_launch_ts, now))


def _resume_request_mode(state) -> str:
    """Return the durable command attached to the latest unserved UI handoff."""
    return ("finalize"
            if state.last_resume_request_mode == "finalize"
            else "resume")


def _cli_args_for_resume_state(rd: Path, cli_args: list[str], state) -> list[str]:
    # A handoff raised while run_abort was pending must remain a FINALIZATION handoff. By the time a
    # post-exit waiter owns the lock, run_finished may already have landed; ordinary ``resume`` would
    # then reopen completed search. The accepted event's mode is authoritative across that tail.
    if _resume_request_mode(state) == "finalize":
        args = ["finalize", str(rd)]
        task_file = _resolve_task_file(rd)
        if task_file:
            args.extend(["--task-file", str(task_file)])
        return args
    return list(cli_args)


def _fresh_resume_launch_pending(rd: Path, *, now: Optional[float] = None) -> bool:
    """Whether reset/delete must fence a newly accepted resume before child engine.lock ownership.

    Callers hold ``_run_lifecycle_lock`` around this check and their mutation. The request grace
    covers append -> claim; the claim grace covers claim -> Popen -> child lock. An abandoned old
    request eventually expires, so a zombie run remains operator-deletable.
    """
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    now = time.time() if now is None else now
    try:
        store = EventStore(rd / "events.jsonl")
        if store.divergence is not None:
            return False
        state = fold(store.read_all())
    except Exception:  # noqa: BLE001 - corrupt/legacy zombies remain operator-deletable
        return False
    return bool(state.resume_pending()
                and (_launch_claim_is_fresh(state, now)
                     or _within_resume_grace(state.last_resume_request_ts, now)))


_RUN_LAUNCH_MARKER = ".looplab-launching"


def _run_launch_marker_path(rd: Path) -> Path:
    return rd / _RUN_LAUNCH_MARKER


def _mark_run_launching(rd: Path) -> None:
    """Stamp the fresh-run launch marker just before a reset/replay Popen (F9), held under the lifecycle
    lock. Reset spawns a fresh `run` engine on an ARCHIVED (emptied) event log, so a resume-style
    launch claim in the log can't fence it; this short-lived FILE bridges the same gap — Popen -> the
    detached child acquiring engine.lock — so a concurrent delete/reset can't rmtree the dir out from
    under a starting engine. Best-effort: if it can't be written the reset still proceeds (today's
    behavior), just without the extra fence."""
    try:
        _run_launch_marker_path(rd).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _clear_run_launching(rd: Path) -> None:
    """Drop the launch marker (a failed Popen: no child is starting, so nothing to fence)."""
    try:
        _run_launch_marker_path(rd).unlink()
    except OSError:
        pass


def _fresh_run_launch_pending(rd: Path, *, now: Optional[float] = None) -> bool:
    """Whether a fresh-run (reset/replay) launch is in flight: the marker exists and is within the same
    grace the resume claim uses. Once the child holds engine.lock `_engine_alive` takes over; an engine
    that died on startup lets the marker expire so the run stays operator-deletable (F9)."""
    marker = _run_launch_marker_path(rd)
    # A just-closed file on Windows/network storage can briefly expose inaccessible or slightly
    # future metadata. Retry that ambiguous publication once; an actually future timestamp remains
    # rejected, while ordinary/expired markers stay on the zero-sleep path.
    for attempt in range(2):
        try:
            ts = marker.stat().st_mtime
        except OSError:
            if attempt == 0:
                time.sleep(0.001)
                continue
            return False
        observed_now = time.time() if now is None else now
        if observed_now >= ts:
            return _within_resume_grace(ts, observed_now)
        if attempt == 0 and now is None:
            time.sleep(0.001)
            continue
        return False
    return False


def _claim_and_spawn_resume(rd: Path, cli_args: list[str], *, env: Optional[dict] = None,
                            now: Optional[float] = None,
                            cancel_event: Optional[threading.Event] = None,
                            wait_on_alive: bool = False,
                            spawn_engine: Optional[Callable[..., Optional[int]]] = None,
                            on_spawn: Optional[Callable[[Optional[int]], None]] = None) -> bool:
    """Atomically claim one pending resume in the event log, then launch its detached CLI.

    The additive `resume_requested(launch_claim=True)` record is a process-wide bounded lease. It
    closes waiter/worker races before engine.lock is acquired; if the claimant dies, reconciliation
    can claim again after the normal grace window.
    """
    from looplab.events.eventstore import (
        EventLogCorruptionError, EventStore, EventStoreConcurrencyError)
    from looplab.events.replay import fold
    from looplab.events.types import EV_RESUME_REQUESTED
    import time as _time

    now = _time.time() if now is None else now
    should_wait = False
    waiter_args = list(cli_args)
    with _run_lifecycle_lock(rd):
        if cancel_event is not None and cancel_event.is_set():
            return False
        store = EventStore(rd / "events.jsonl")
        if store.divergence is not None:
            return False
        for _attempt in range(8):
            try:
                events = store.read_all()
                state = fold(events)
            except Exception:  # noqa: BLE001 - the durable intent remains for a later healthy read
                return False
            if not state.resume_pending():
                return False
            waiter_args = _cli_args_for_resume_state(rd, cli_args, state)
            if _launch_claim_is_fresh(state, now):
                # A claimant can acquire engine.lock between the caller's liveness probe and this
                # fold. Preserve a post-exit waiter on that live flip; otherwise a tail-exiting owner
                # can strand the accepted intent indefinitely.
                liveness = _spawn_liveness(rd)
                if wait_on_alive and liveness is True:
                    should_wait = True
                    break
                return False
            liveness = _spawn_liveness(rd)
            if liveness is not False:
                if liveness is None:
                    return False
                should_wait = True
                break
            last_seq = events[-1].seq if events else -1
            try:
                store.append(
                    EV_RESUME_REQUESTED,
                    {"launch_claim": True, "request_seq": state.last_resume_request_seq,
                     "mode": _resume_request_mode(state)},
                    expected_last_seq=last_seq,
                )
            except EventStoreConcurrencyError:
                continue
            except (EventLogCorruptionError, OSError):
                return False
            liveness = _spawn_liveness(rd)
            if liveness is not False:
                if liveness is None:
                    # Keep the just-written launch claim as durable quarantine.  A later healthy
                    # probe can reconcile it; uncertainty is never permission to Popen.
                    return False
                # Another CLI acquired engine.lock after the claim. It can already be unwinding, so
                # retain a waiter instead of assuming it will necessarily fold/serve this request.
                should_wait = True
                break
            # Cancellation and Popen share this gate with shutdown/reaping. Either the child is fully
            # registered before cancellation, or cancellation wins and no child is created.
            with _engine_spawn_gate:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                # Keep the router's historical spawn patch seam without changing the default: direct
                # callers and the reconciler still resolve this module's live `_spawn_engine` binding.
                spawner = spawn_engine or _spawn_engine
                pid = spawner(waiter_args, env=env, run_dir=rd)
                # This callback is deliberately after Popen. A persistence failure here is therefore
                # not a safe-to-retry pre-spawn failure; the caller must retain its quarantine claim.
                if on_spawn is not None:
                    on_spawn(pid)
            return True
        else:
            return False       # a hot writer won every CAS; the intent stays durably pending
    if should_wait and wait_on_alive and not (cancel_event is not None and cancel_event.is_set()):
        _spawn_engine_after_exit(
            waiter_args, run_dir=rd, env=env, cancel_event=cancel_event)
    return False


def reconcile_pending_resume(rd: Path, *, now: Optional[float] = None,
                             cancel_event: Optional[threading.Event] = None) -> bool:
    """P1-1 on-load reconciler (NO standing daemon): re-spawn the engine for a run whose durable resume
    intent was recorded but never served — either a detached spawn died before the engine ran or the
    request landed in an old engine's post-finish tail. Returns True if it re-spawned. Idempotent
    and safe to over-call: a second engine no-ops on the singleton lock. Conservative gates, ALL required:
      * `resume_pending()` — a resume_requested seq newer than the last resume_served (unfulfilled);
      * the request is older than the grace window (the real spawn had its chance to acquire the lock);
      * no engine currently holds the lock (a genuine zombie or post-finish wake-up);
      * the run is resumable (a task file exists)."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    import time as _time
    now = _time.time() if now is None else now
    if cancel_event is not None and cancel_event.is_set():
        return False
    try:
        store = EventStore(rd / "events.jsonl")
        if store.divergence is not None:
            return False
        st = fold(store.read_all())
    except Exception:  # noqa: BLE001 — a corrupt/absent log is not reconcilable; never crash the list
        return False
    if not st.resume_pending():
        return False
    if _launch_claim_is_fresh(st, now):
        return False                      # another worker already launched a CLI for this intent
    if _within_resume_grace(st.last_resume_request_ts, now):
        return False                      # give the in-flight spawn time to acquire the lock + serve
    if _spawn_liveness(rd) is not False:
        return False                      # an engine IS running -> the intent is being served
    task_file = _resolve_task_file(rd)
    if not task_file:
        return False                      # not resumable (predates self-describing runs)
    # The CAS launch claim below resets the grace window, so a crash-looping engine retries at most
    # once per grace rather than on every dashboard refresh.
    cli_args = _cli_args_for_resume_state(
        rd, ["resume", str(rd), "--task-file", str(task_file)], st)
    try:
        return _claim_and_spawn_resume(
            rd, cli_args, now=now,
            cancel_event=cancel_event, wait_on_alive=True)
    except Exception:  # noqa: BLE001 - best-effort recovery must not break startup or the run list
        return False


def _spawn_engine_after_exit(cli_args: list[str], *, run_dir: Path,
                             env: Optional[dict] = None,
                             cancel_event: Optional[threading.Event] = None) -> bool:
    """Spawn once after the current owner exits iff a durable resume intent remains pending."""
    key = str(run_dir.resolve())
    with _resume_after_exit_lock:
        if key in _resume_after_exit:
            return False
        _resume_after_exit.add(key)

    def _pending() -> Optional[bool]:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        try:
            store = EventStore(run_dir / "events.jsonl")
            if store.divergence is not None:
                return None
            return fold(store.read_all()).resume_pending()
        except Exception:  # noqa: BLE001 - unreadable state stays recoverable; keep waiting
            return None

    def _log_sig() -> Optional[tuple[int, int]]:
        try:
            st = (run_dir / "events.jsonl").stat()
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None

    def _wait_then_spawn() -> None:
        try:
            last_sig = None
            while True:
                while _spawn_liveness(run_dir) is not False:
                    sig = _log_sig()
                    if sig != last_sig:
                        last_sig = sig
                        # A live owner explicitly served the wake-up. Stop probing its lock for the
                        # rest of a potentially hours-long run; a later request installs a new waiter.
                        if _pending() is False:
                            return
                    if cancel_event is not None and cancel_event.wait(0.05):
                        return
                    if cancel_event is None:
                        time.sleep(0.05)
                if cancel_event is not None and cancel_event.is_set():
                    return
                if _pending() is False:
                    return
                if _claim_and_spawn_resume(
                        run_dir, cli_args, env=env, cancel_event=cancel_event,
                        wait_on_alive=False):
                    return
                # A different CLI can acquire engine.lock between our dead probe and claim. Keep
                # this same registered waiter through that handoff rather than recursively trying to
                # register a duplicate under our own key.
                if _spawn_liveness(run_dir) is not False:
                    continue
                return
        finally:
            with _resume_after_exit_lock:
                _resume_after_exit.discard(key)
                current = _resume_waiter_threads.get(key)
                if current is not None and current[0] is threading.current_thread():
                    _resume_waiter_threads.pop(key, None)

    thread = threading.Thread(
        target=_wait_then_spawn,
        name=f"looplab-resume-{run_dir.name}",
        daemon=True,
    )
    with _resume_after_exit_lock:
        _resume_waiter_threads[key] = (thread, cancel_event)
    try:
        thread.start()
    except RuntimeError:
        # Thread creation can fail during interpreter shutdown/resource exhaustion. Never leave the
        # dedupe key wedged forever; the durable intent remains available to later reconciliation.
        with _resume_after_exit_lock:
            _resume_after_exit.discard(key)
            _resume_waiter_threads.pop(key, None)
        return False
    return True


def install_resume_reconcile_hooks(app, root: Path) -> threading.Event:
    """Recover durable resume intents on startup, without requiring a dashboard list poll."""
    timers: list[threading.Timer] = []
    shutdown = threading.Event()

    def _scan_startup() -> None:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        now = time.time()
        try:
            run_dirs = list(root.iterdir()) if root.exists() else []
        except OSError:
            return
        for rd in run_dirs:
            if not (rd / "events.jsonl").is_file():
                continue
            try:
                store = EventStore(rd / "events.jsonl")
                if store.divergence is not None:
                    continue
                state = fold(store.read_all())
            except Exception:  # noqa: BLE001 - one corrupt run cannot block server startup recovery
                continue
            if not state.resume_pending():
                continue
            task_file = _resolve_task_file(rd)
            if not task_file:
                continue
            cli_args = _cli_args_for_resume_state(
                rd, ["resume", str(rd), "--task-file", str(task_file)], state)
            startup_liveness = _spawn_liveness(rd)
            if startup_liveness is True:
                # A server restart loses the old in-memory tail waiter; reinstall it while the
                # engine still owns the run. The durable launch claim arbitrates multiple workers.
                _spawn_engine_after_exit(cli_args, run_dir=rd, cancel_event=shutdown)
                continue
            if startup_liveness is None:
                # Do not create one 20 Hz waiter thread per malformed/reparse/unsupported run at
                # startup. Unknown ownership remains quarantined until a later healthy observation
                # or server restart can prove an exact state.
                continue
            latest_ts = max(float(state.last_resume_request_ts or 0.0),
                            float(state.last_resume_launch_ts or 0.0))
            elapsed = now - latest_ts
            delay = (_RESUME_RECONCILE_GRACE_S - elapsed
                     if 0.0 <= elapsed < _RESUME_RECONCILE_GRACE_S else 0.0)
            if delay <= 0:
                try:
                    reconcile_pending_resume(rd, now=now, cancel_event=shutdown)
                except Exception:  # noqa: BLE001 - one broken run cannot abort server startup
                    pass
                continue
            def _reconcile_unless_shutdown(run_dir=rd):
                if not shutdown.is_set():
                    reconcile_pending_resume(run_dir, cancel_event=shutdown)
            timer = threading.Timer(delay + 0.01, _reconcile_unless_shutdown)
            timer.daemon = True
            timers.append(timer)
            timer.start()

    @app.on_event("startup")
    def _recover_resumes_on_startup():
        _scan_startup()

    @app.on_event("shutdown")
    def _cancel_resume_timers():
        # Ordered against claim+Popen: once this returns no callback can create a new engine that the
        # following JupyterHub reaper fails to see.
        with _engine_spawn_gate:
            shutdown.set()
        for timer in timers:
            timer.cancel()
        deadline = time.monotonic() + 2.0
        for timer in timers:
            if timer is not threading.current_thread():
                timer.join(timeout=max(0.0, deadline - time.monotonic()))
        timers.clear()
        with _resume_after_exit_lock:
            waiters = [thread for thread, event in _resume_waiter_threads.values()
                       if event is shutdown]
        for thread in waiters:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    return shutdown


def _reap_spawned_engines() -> None:
    # Reap engines THIS server spawned — but ONLY under JupyterHub. A detached engine (own session,
    # so it survives an HTTP request) ALSO survives the single-user server's process-group SIGTERM
    # when the hub idle-culler stops the pod: it's orphaned (reparented to PID 1), keeps consuming
    # the GPU/CPU JupyterHub bills the user, AND keeps engine.lock held so the run shows "live"
    # forever (masking the zombie-detect / auto-resume recovery). Locally we must NOT do this — a
    # detached engine is deliberately meant to outlive a UI restart — so we guard on the JH env.
    # _kill_process_tree re-checks each pid is still a looplab engine (PID-recycle safe).
    if not _on_shared_hub():
        return
    with _engine_spawn_gate:
        for pid in list(_spawned_engine_pids):
            _kill_process_tree(pid)
            _spawned_engine_pids.discard(pid)


def install_reap_hooks(app) -> None:
    """Wire the JupyterHub reaper to this app's lifecycle: an ASGI shutdown hook plus — on a shared
    hub only — an atexit backstop. Called once per `make_app`, at the same construction point the
    inline registration used to occupy."""
    @app.on_event("shutdown")
    def _reap_on_shutdown():
        _reap_spawned_engines()

    if _on_shared_hub():            # backstop for a hard exit where the ASGI shutdown hook doesn't fire
        atexit.register(_reap_spawned_engines)
