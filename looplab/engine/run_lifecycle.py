"""The run directory's LIFECYCLE FENCES: is an engine process holding this run, and is a launch in
flight — plus the config-write transaction every whole-run writer takes.

Moved DOWN out of `serve/` on 2026-09-08 (doc 25 XP-03, the
`run-lifecycle-primitives-cannot-move-down` item). These five primitives are what a run-MUTATING
agent tool needs — `tools/run_control_tools.py::RunLifecycleFns` names them one by one — and while
they lived in `serve/engine_proc.py` + `serve/run_files.py` the only way for `tools/` to reach its
own defaults was a function-local import UPWARD into `serve/`: a package cycle held open by nothing
but import timing. The injection seam (`RunLifecycleFns`) made the dependency explicit but did not
remove it, because the DEFAULT still reached up whenever nothing injected.

`engine/` is the home rather than a new package because it is the lowest unit BOTH `tools` and
`serve` may already import, and because the subject is the ENGINE's own process and run directory:
`engine.lock` is the engine's singleton lock, the launch markers fence the engine's own
claim -> Popen -> child-lock startup gap, and `engine/resources.py` already keeps the sibling
cross-process lease here. Nothing in this module imports `serve`, and nothing in it spawns: spawning,
the JupyterHub reaper and the HTTP-shaped wrappers (`run_lifecycle_lock_http`,
`engine_write_lock_http`) stay in `serve/engine_proc.py`, which re-exports every name below so the
historical `looplab.serve.engine_proc.<name>` import and monkeypatch paths keep working unchanged.

The leading underscores came off in the move, and that is the point rather than tidiness: doc 25
XP-01's recommendation for exactly this shape is "promote the functions … into a public read-model
API (drop the underscore) so the boundary is explicit", and `tests/test_cross_package_private_seams
.py` counts a private name imported across a package boundary as debt. `serve/engine_proc.py`
re-exports each one under its historical `_`-prefixed spelling (`engine_alive as _engine_alive`, …),
so no caller and no monkeypatch site had to move; only the DECLARED boundary is public.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from looplab.core.atomicio import same_file_entry
from looplab.core.pathsafe import is_reparse
from looplab.core.run_deletion import assert_run_deletion_write_allowed
from looplab.core.run_reset import assert_run_reset_write_allowed
from looplab.events.eventstore import _interprocess_lock


# --------------------------------------------------------------------------- engine.lock liveness
def engine_liveness(rd: Path) -> Optional[bool]:
    """True when held, False when definitively free/absent, None when the probe is inconclusive."""
    lock = rd / "engine.lock"
    try:
        run_entry = rd.lstat()
    except FileNotFoundError:
        return False  # required for a not-yet-materialized, validated new-start path
    except OSError:
        return None
    if is_reparse(run_entry) or not stat.S_ISDIR(run_entry.st_mode):
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
                and not is_reparse(current)
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
        if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
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
        if (same_file_entry(entry) != same_file_entry(opened)
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
                and not is_reparse(current)
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


def engine_alive(rd: Path) -> bool:
    """Conservative boolean compatibility API: only a proven-free lock is treated as stopped."""
    return engine_liveness(rd) is not False


# ------------------------------------------------------------- the per-run lifecycle transaction
# Resume, reset, and delete are one lifecycle transaction per run.  engine.lock fences a RUNNING
# engine, but it does not cover the claim -> Popen -> child-lock startup gap.  Pair a process-local
# RLock with a sibling interprocess lock whose inode survives deletion of the run directory; this
# prevents another server worker from archiving/removing a run after a durable launch claim but
# before its child owns engine.lock.
_run_lifecycle_locks_guard = threading.Lock()
_run_lifecycle_locks: dict[str, threading.RLock] = {}


def run_lifecycle_key(rd: Path) -> str:
    return os.path.normcase(str(rd.resolve()))


def run_lifecycle_lock_path(rd: Path) -> Path:
    digest = hashlib.sha256(run_lifecycle_key(rd).encode("utf-8")).hexdigest()[:24]
    return rd.resolve().parent / f".looplab-lifecycle-{digest}.lock"


@contextmanager
def run_lifecycle_lock(rd: Path):
    """Cross-thread/process fence for resume-claim, reset, and delete of one run."""
    # DELIBERATELY function-local, even though this module imports the same name at module scope for
    # `run_config_write_lock` below: the lock backend is a monkeypatch SEAM
    # (`tests/test_review_fixes.py::test_lifecycle_lock_is_required_and_reports_503` replaces
    # `eventstore._interprocess_lock` to prove an unavailable backend becomes a 503), and a
    # module-level binding here would freeze the original at import time and make that patch inert.
    from looplab.events.eventstore import _interprocess_lock

    key = run_lifecycle_key(rd)
    with _run_lifecycle_locks_guard:
        local = _run_lifecycle_locks.setdefault(key, threading.RLock())
    # REQUIRED, not best-effort. Without it, `_interprocess_lock` swallows an unsupported lock backend
    # and this degrades to the in-process RLock alone — so two server processes (or two startup
    # reconcilers) could claim and spawn the SAME resume, and race event appends before engine.lock
    # exists to catch them. reset/delete are pure check-then-act around `fresh_resume_launch_pending`,
    # so they have no CAS to fall back on. Callers map the resulting EventStoreLockError to a 503; the
    # same fail-closed contract `_put_run_config_locked` already uses for run config.
    with local, _interprocess_lock(run_lifecycle_lock_path(rd), required=True):
        yield


def sweep_stale_lifecycle_locks(root: Path, *, max_age_s: float = 3600.0) -> int:
    """Best-effort startup GC of orphaned per-run lifecycle lock files (F22). These live in the runs
    root and are deliberately never deleted inline (their inode is the fence during a run's own delete),
    so a long-lived server slowly accumulates one `.looplab-lifecycle-*.lock` dot-file per run ever
    resumed/reset/deleted. Remove one ONLY when it is (a) OLD — untouched for `max_age_s`, while a real
    lifecycle op touches its lock within seconds.

    POSIX is a deliberate NO-OP (see the loop): there is no way to remove a `flock` pathname without
    risking two lock domains over one run, and the accumulation it would clean is bounded by run count.
    Windows can unlink safely because the OS refuses to remove a file that is open/locked, which is
    exactly the check this GC needs. Skips silently on any error. Returns the count removed."""
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
        # POSIX: DO NOT UNLINK. `flock` is per-INODE, and holding the lock while unlinking is not
        # enough — a lifecycle op already blocked in `flock(LOCK_EX)` on this inode can still be
        # waiting (`flock` gives no FIFO fairness, so the sweeper's LOCK_NB can win the race the
        # instant a holder releases). It then acquires the now-unlinked inode, while the very next
        # `run_lifecycle_lock` caller's `open(lp, "a+")` creates a FRESH inode and locks that: two
        # live lock domains over one run, i.e. reset/delete/resume-claim running concurrently with the
        # single fence they rely on. The `max_age_s` filter does not help — it excludes recently
        # TOUCHED files, not waiters. The leak this was cleaning is one empty dot-file per run ever
        # resumed/reset/deleted (bounded by run count); the split-brain is unbounded corruption.
        continue
    return removed


# ------------------------------------------------------------------- launch-pending predicates
# P1-1 recoverable-intent reconciler grace: wait this long after a durable resume_requested before
# re-spawning it, so the ORIGINAL detached spawn has time to acquire the lock + append resume_served.
# Only a resume that stays unserved past this window is treated as a died-on-startup zombie.
RESUME_RECONCILE_GRACE_S = 30.0


def within_resume_grace(ts: float, now: float) -> bool:
    """A wall-clock lease is fresh only when its age is non-negative and below the grace."""
    elapsed = now - float(ts or 0.0)
    return 0.0 <= elapsed < RESUME_RECONCILE_GRACE_S


def launch_claim_is_fresh(state, now: float) -> bool:
    """Whether a detached CLI launch is already in flight for this unserved intent."""
    return (state.last_resume_launch_seq > state.last_resume_served_seq
            and within_resume_grace(state.last_resume_launch_ts, now))


def fresh_resume_launch_pending(rd: Path, *, now: Optional[float] = None) -> bool:
    """Whether reset/delete must fence a newly accepted resume before child engine.lock ownership.

    Callers hold ``run_lifecycle_lock`` around this check and their mutation. The request grace
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
                and (launch_claim_is_fresh(state, now)
                     or within_resume_grace(state.last_resume_request_ts, now)))


RUN_LAUNCH_MARKER = ".looplab-launching"


def run_launch_marker_path(rd: Path) -> Path:
    return rd / RUN_LAUNCH_MARKER


def mark_run_launching(rd: Path) -> None:
    """Stamp the fresh-run launch marker just before a reset/replay Popen (F9), held under the lifecycle
    lock. Reset spawns a fresh `run` engine on an ARCHIVED (emptied) event log, so a resume-style
    launch claim in the log can't fence it; this short-lived FILE bridges the same gap — Popen -> the
    detached child acquiring engine.lock — so a concurrent delete/reset can't rmtree the dir out from
    under a starting engine. Best-effort: if it can't be written the reset still proceeds (today's
    behavior), just without the extra fence."""
    try:
        run_launch_marker_path(rd).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def clear_run_launching(rd: Path) -> None:
    """Drop the launch marker (a failed Popen: no child is starting, so nothing to fence)."""
    try:
        run_launch_marker_path(rd).unlink()
    except OSError:
        pass


def fresh_run_launch_pending(rd: Path, *, now: Optional[float] = None) -> bool:
    """Whether a fresh-run (reset/replay) launch is in flight: the marker exists and is within the same
    grace the resume claim uses. Once the child holds engine.lock `engine_alive` takes over; an engine
    that died on startup lets the marker expire so the run stays operator-deletable (F9)."""
    marker = run_launch_marker_path(rd)
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
            return within_resume_grace(ts, observed_now)
        if attempt == 0 and now is None:
            time.sleep(0.001)
            continue
        return False
    return False


# ------------------------------------------------------- the per-run config write transaction
# Moved verbatim from `serve/run_files.py` ("Shared serialization for mutable per-run snapshot
# files"), which now re-exports both names: it is the fifth `RunLifecycleFns` primitive, so leaving
# it behind in `serve/` would have kept the cycle open for the sake of one file.
_RUN_CONFIG_LOCK_STRIPES = tuple(threading.Lock() for _ in range(64))


def run_config_thread_lock(snapshot_path: Path) -> threading.Lock:
    """Bound same-process serialization without retaining a lock for every historical run."""
    identity = os.path.normcase(os.path.abspath(snapshot_path)).encode(
        "utf-8", errors="surrogatepass")
    stripe = int.from_bytes(hashlib.sha256(identity).digest()[:2], "big")
    return _RUN_CONFIG_LOCK_STRIPES[stripe % len(_RUN_CONFIG_LOCK_STRIPES)]


@contextmanager
def run_config_write_lock(
        snapshot_path: Path, *, operation_id: Optional[str] = None,
        deletion_operation_id: Optional[str] = None) -> Iterator[None]:
    """Own the config transaction and enforce every whole-run writer fence."""
    with (run_config_thread_lock(snapshot_path),
          _interprocess_lock(Path(str(snapshot_path) + ".lock"), required=True)):
        assert_run_reset_write_allowed(snapshot_path.parent, operation_id)
        assert_run_deletion_write_allowed(snapshot_path.parent, deletion_operation_id)
        yield
