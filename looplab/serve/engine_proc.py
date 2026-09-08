"""Engine-process plumbing for the UI server: SPAWNING detached engine runs (`_spawn_engine`), the
resume claim/reconcile machinery, the HTTP-shaped lock wrappers, and the JupyterHub-only reaper that
stops spawned engines when the single-user server shuts down. Extracted verbatim from
`serve/server.py` (BACKLOG §4); `looplab.serve.server` re-exports `_engine_alive`/`_kill_process_tree`
so the historical `looplab.server._engine_alive` import path keeps working for tests and callers.

The run-directory LIFECYCLE FENCES themselves — `engine.lock` liveness, the per-run lifecycle lock,
the launch-pending predicates and their launch marker — moved DOWN to
`looplab/engine/run_lifecycle.py` on 2026-09-08 (doc 25 XP-03) so `tools/machine_runs_tools.py` can
take its `RunLifecycleFns` defaults DOWNWARD instead of importing this package upward. They are
re-exported below, unchanged: every `looplab.serve.engine_proc.<name>` import path and every
`monkeypatch.setattr(engine_proc, "_engine_alive", …)` seam still resolves here, and this module's
own callers still read them out of this module's globals, so a patch of either name still lands."""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

# Re-exported, not redefined (see the module docstring): these are the run-lifecycle fences, and
# their home is now below both `serve` and `tools`. They are PUBLIC there and keep their historical
# `_`-prefixed spelling here, so every existing import site and every
# `monkeypatch.setattr(engine_proc, "_engine_alive", …)` still resolves — and this module's own
# functions still read them out of this module's globals, so such a patch still lands.
from looplab.core.atomicio import file_identity
from looplab.engine import run_lifecycle
from looplab.engine.run_lifecycle import (  # noqa: F401 - re-exported for the historical import path
    RESUME_RECONCILE_GRACE_S as _RESUME_RECONCILE_GRACE_S,
    RUN_LAUNCH_MARKER as _RUN_LAUNCH_MARKER,
    clear_run_launching as _clear_run_launching,
    engine_alive as _engine_alive,
    engine_liveness as _engine_liveness,
    fresh_resume_launch_pending as _fresh_resume_launch_pending,
    fresh_run_launch_pending as _fresh_run_launch_pending,
    launch_claim_is_fresh as _launch_claim_is_fresh,
    mark_run_launching as _mark_run_launching,
    run_launch_marker_path as _run_launch_marker_path,
    run_lifecycle_key as _run_lifecycle_key,
    run_lifecycle_lock as _run_lifecycle_lock,
    run_lifecycle_lock_path as _run_lifecycle_lock_path,
    sweep_stale_lifecycle_locks,
    within_resume_grace as _within_resume_grace,
)


def _on_shared_hub() -> bool:
    """True when this process looks like a JupyterHub single-user server reached through
    `jupyter-server-proxy` (https://hub/user/<name>/proxy/<port>/). That is a SHARED origin: the
    same-origin policy is per-ORIGIN, not per-path, so a same-origin page on a *different path*
    (another proxied app, a file the user opens under /user/<name>/files/...) can read anything
    served on this origin — including an injected UI token. Detected via env JupyterHub sets in
    every single-user server; absent on the default local single-user path."""
    return bool(os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
                or os.environ.get("JUPYTERHUB_API_TOKEN"))


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


class EngineSpawnOutcomeUnknown(RuntimeError):
    """A spawner was entered, so an exception cannot prove that no child was created."""


@contextmanager
def run_lifecycle_lock_http(rd: Path):
    """`_run_lifecycle_lock` for HTTP routes: an unavailable lock backend is a 503, not a 500.

    The lock is REQUIRED (see above), so a filesystem that cannot provide it now raises instead of
    silently degrading. For an operator that is a retryable infrastructure condition — the run was not
    touched — so it must read as one. Mirrors the `run_config_lock_unavailable` contract on
    `PUT /api/runs/{id}/config`. Non-HTTP callers (the agent run tools) keep the raw error."""
    # Both imported LAZILY: this module spawns engines and must stay importable without the [ui]
    # extra, so fastapi cannot be a module-level dependency here.
    from fastapi import HTTPException
    from looplab.events.eventstore import EventStoreLockError

    try:
        with _run_lifecycle_lock(rd):
            yield
    except EventStoreLockError as exc:
        raise HTTPException(503, {
            "code": "run_lifecycle_lock_unavailable",
            "message": "Run lifecycle locking is unavailable; the run was not modified.",
        }) from exc


@contextmanager
def engine_write_lock_http(rd: Path):
    """Own ``engine.lock`` for an offline HTTP rewrite without waiting on a racing CLI.

    The lifecycle lock serializes server launches, but a direct ``looplab run/resume`` acquires only
    ``engine.lock``. A liveness probe followed by a whole-file rewrite therefore still has a race
    unless the route itself owns that writer lock for the rewrite. Contention is an immediate 409,
    while an unsupported lock backend is a 503; neither condition authorizes an unlocked mutation.
    """
    from fastapi import HTTPException
    from looplab.events.eventstore import (
        EventStoreLockError, InterprocessLockContended, interprocess_lock)

    try:
        with interprocess_lock(rd / "engine.lock", required=True, blocking=False):
            yield
    except InterprocessLockContended as exc:
        raise HTTPException(409, {
            "code": "engine_running",
            "message": "The run engine acquired write ownership before this operation.",
            "remediation": "Wait for the run to stop, refresh, and try again.",
        }) from exc
    except EventStoreLockError as exc:
        raise HTTPException(503, {
            "code": "engine_lock_unavailable",
            "message": "Engine write locking is unavailable; the run was not modified.",
            "remediation": "Inspect engine.lock and storage locking before retrying.",
        }) from exc


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
    # The engine receives no ambient secret merely because its server parent had one. The caller's
    # explicit overlay contains only source-verified shared/profile pairs plus ordinary run values.
    from looplab.runtime.sandbox import is_secret_env
    clean_parent = {key: value for key, value in os.environ.items()
                    if not is_secret_env(key, value)}
    kw["env"] = {**clean_parent, **(env or {})}
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


def _claim_and_spawn_resume(rd: Path, cli_args: list[str], *, env: Optional[dict] = None,
                            now: Optional[float] = None,
                            cancel_event: Optional[threading.Event] = None,
                            wait_on_alive: bool = False,
                            spawn_engine: Optional[Callable[..., Optional[int]]] = None,
                            liveness: Optional[Callable[[Path], Optional[bool]]] = None,
                            on_spawn: Optional[Callable[[Optional[int]], None]] = None,
                            before_spawn: Optional[Callable[[], Optional[dict]]] = None,
                            launch_env: Optional[Callable[[], Any]] = None) -> bool:
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
    liveness_probe = liveness or _spawn_liveness
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
                ownership = liveness_probe(rd)
                if wait_on_alive and ownership is True:
                    should_wait = True
                    break
                return False
            ownership = liveness_probe(rd)
            if ownership is not False:
                if ownership is None:
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
            ownership = liveness_probe(rd)
            if ownership is not False:
                if ownership is None:
                    # Keep the just-written launch claim as durable quarantine.  A later healthy
                    # probe can reconcile it; uncertainty is never permission to Popen.
                    return False
                # Another CLI acquired engine.lock after the claim. It can already be unwinding, so
                # retain a waiter instead of assuming it will necessarily fold/serve this request.
                should_wait = True
                break
            # Acquire the settings publication context BEFORE the engine gate. Every direct engine
            # spawn uses launch -> engine-gate order; entering UI/secret/launch while holding the gate
            # would deadlock with a settings writer waiting for a different launch to finish Popen.
            popen_boundary_entered = False

            def _spawn(secret_env: Optional[dict]) -> bool:
                nonlocal popen_boundary_entered
                # The just-in-time source-validated pair (including empty revocation tombstones)
                # must outrank an ordinary overlay captured before the resume wait began.
                spawn_env = {**(env or {}), **(secret_env or {})}
                # Keep the router's historical spawn patch seam without changing the default: direct
                # callers and the reconciler still resolve this module's live `_spawn_engine` binding.
                spawner = spawn_engine or _spawn_engine
                # Cancellation and Popen share this gate with shutdown/reaping. Either the child is
                # fully registered before cancellation, or cancellation wins and no child is created.
                with _engine_spawn_gate:
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    popen_boundary_entered = True
                    pid = spawner(waiter_args, env=spawn_env or None, run_dir=rd)
                    # Deliberately after Popen: persistence failure is not safe-to-retry pre-spawn.
                    if on_spawn is not None:
                        on_spawn(pid)
                    return True

            if cancel_event is not None and cancel_event.is_set():
                return False
            try:
                if launch_env is not None:
                    with launch_env() as secret_env:
                        spawned = _spawn(secret_env)
                else:
                    spawned = _spawn(before_spawn() if before_spawn is not None else None)
            except BaseException as exc:
                if popen_boundary_entered:
                    raise EngineSpawnOutcomeUnknown(
                        "resume process creation may have succeeded") from exc
                raise
            return spawned
        else:
            return False       # a hot writer won every CAS; the intent stays durably pending
    if should_wait and wait_on_alive and not (cancel_event is not None and cancel_event.is_set()):
        _spawn_engine_after_exit(
            waiter_args, run_dir=rd, env=env, cancel_event=cancel_event,
            before_spawn=before_spawn, launch_env=launch_env)
    return False


def reconcile_pending_resume(rd: Path, *, now: Optional[float] = None,
                             cancel_event: Optional[threading.Event] = None,
                             before_spawn: Optional[Callable[[], Optional[dict]]] = None,
                             launch_env: Optional[Callable[[], Any]] = None) -> bool:
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
            cancel_event=cancel_event, wait_on_alive=True, before_spawn=before_spawn,
            launch_env=launch_env)
    except Exception:  # noqa: BLE001 - best-effort recovery must not break startup or the run list
        return False


def _spawn_engine_after_exit(cli_args: list[str], *, run_dir: Path,
                             env: Optional[dict] = None,
                             cancel_event: Optional[threading.Event] = None,
                             before_spawn: Optional[Callable[[], Optional[dict]]] = None,
                             launch_env: Optional[Callable[[], Any]] = None) -> bool:
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

    def _log_sig() -> Optional[tuple[int, ...]]:
        # `file_identity`, not the (size, mtime_ns) pair this used to spell: the waiter is asking
        # "has anything happened to the log since I last looked", and a REPLACEMENT (a reset that
        # atomically swapped a fresh events.jsonl in) is the loudest thing that can happen to it —
        # invisible to size+mtime when the new file happens to match, and exactly the case where
        # continuing to wait is wrong (doc 25 SC-11).
        try:
            return file_identity((run_dir / "events.jsonl").stat())
        except OSError:
            return None

    def _wait_then_spawn() -> None:
        try:
            last_sig = None
            last_pending_check = 0.0        # monotonic stamp of the last full-log `_pending()` fold
            while True:
                while _spawn_liveness(run_dir) is not False:
                    sig = _log_sig()
                    # RATE-LIMITED, because `_pending()` re-reads and re-folds the ENTIRE log and the
                    # signature moves on every append: a live owner writing a long finalization tail
                    # made this waiter refold the whole log at the poll rate for the rest of the run.
                    #
                    # Deliberately NOT made incremental. `CommandObservationIndex` would parse only
                    # the new suffix, but `resume_pending()` is derived from a FOLD, and the fold is
                    # re-run whole on every revision — so the observation index moves the parse cost
                    # and leaves the O(n) fold. A tail-only `resume_served` probe would be O(1) but
                    # cannot see a `resume_requested` that lands after it, which is exactly the
                    # comparison `resume_pending()` exists to make. The interval below is bounded by
                    # a pinned contract: `test_live_owner_explicitly_serves_resume_before_finish`
                    # gives the waiter 0.75s to notice an explicit serve, and that latency is the
                    # point of the waiter — so this trades a constant factor, not the complexity.
                    now = time.monotonic()
                    if sig != last_sig and now - last_pending_check >= _PENDING_RECHECK_S:
                        last_sig = sig
                        last_pending_check = now
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
                        wait_on_alive=False, before_spawn=before_spawn,
                        launch_env=launch_env):
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


# Minimum seconds between two full-log `_pending()` folds in the tail waiter. The log signature
# moves on every append, so without this the waiter refolds the whole log at its 20 Hz poll rate for
# as long as the owner keeps writing. Kept well inside the 0.75s an explicit serve is given to be
# noticed (see the waiter's comment); the liveness probe itself keeps its original cadence.
_PENDING_RECHECK_S = 0.25


def install_resume_reconcile_hooks(
        app, root: Path, *,
        before_spawn: Optional[Callable[[Path], Optional[dict]]] = None,
        launch_env: Optional[Callable[[Path], Any]] = None) -> threading.Event:
    """Recover durable resume intents on startup, without requiring a dashboard list poll."""
    timers: list[threading.Timer] = []
    shutdown = threading.Event()

    # ADJUDICATED, kept SYNCHRONOUS. This folds the complete event log of every run under the root,
    # so a workspace of many large runs does hold up server readiness — the cost is real. But
    # "startup has recovered by the time startup returns" is the guarantee, not an implementation
    # detail: `test_server_startup_recovers_restart_after_command_worker_loss` asserts the re-spawn
    # has happened once the app has started, and moving the scan to a daemon thread makes recovery
    # race the first request and the shutdown hook (a server stopped early would silently skip it).
    # The cheap-check alternative is not available either: only `resume_pending()` is needed, but it
    # is derived from the FOLD, and a tail probe cannot make that comparison (see the tail waiter's
    # note). Losing an unserved resume is worse than a slow start, so the slow start stays.
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
            prepare_spawn = ((lambda run_dir=rd: before_spawn(run_dir))
                             if before_spawn is not None else None)
            prepare_launch = ((lambda run_dir=rd: launch_env(run_dir))
                              if launch_env is not None else None)
            startup_liveness = _spawn_liveness(rd)
            if startup_liveness is True:
                # A server restart loses the old in-memory tail waiter; reinstall it while the
                # engine still owns the run. The durable launch claim arbitrates multiple workers.
                _spawn_engine_after_exit(
                    cli_args, run_dir=rd, cancel_event=shutdown,
                    before_spawn=prepare_spawn, launch_env=prepare_launch)
                continue
            if startup_liveness is None:
                # Do not create one 20 Hz waiter thread per malformed/reparse/unsupported run at
                # startup. Unknown ownership remains quarantined until a later healthy observation
                # or server restart can prove an exact state.
                continue
            latest_ts = max(float(state.last_resume_request_ts or 0.0),
                            float(state.last_resume_launch_ts or 0.0))
            elapsed = now - latest_ts
            # Read THROUGH the owning module, not off the re-exported copy above: the grace is one
            # number, and `within_resume_grace` (which decides whether the same intent is still
            # fresh) reads it from `run_lifecycle`. A bound copy here would give a test that lowers
            # the grace two different answers in the same reconcile pass (doc 25 XP-03).
            grace = run_lifecycle.RESUME_RECONCILE_GRACE_S
            delay = (grace - elapsed if 0.0 <= elapsed < grace else 0.0)
            if delay <= 0:
                try:
                    reconcile_pending_resume(
                        rd, now=now, cancel_event=shutdown, before_spawn=prepare_spawn,
                        launch_env=prepare_launch)
                except Exception:  # noqa: BLE001 - one broken run cannot abort server startup
                    pass
                continue
            def _reconcile_unless_shutdown(run_dir=rd):
                if not shutdown.is_set():
                    prepare = ((lambda: before_spawn(run_dir))
                               if before_spawn is not None else None)
                    launch = ((lambda: launch_env(run_dir))
                              if launch_env is not None else None)
                    reconcile_pending_resume(
                        run_dir, cancel_event=shutdown, before_spawn=prepare,
                        launch_env=launch)
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
