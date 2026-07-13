"""Engine-process plumbing for the UI server: liveness probing (`_engine_alive`), spawning
detached engine runs (`_spawn_engine`), and the JupyterHub-only reaper that stops spawned engines
when the single-user server shuts down. Extracted verbatim from `serve/server.py` (BACKLOG §4);
`looplab.serve.server` re-exports `_engine_alive`/`_kill_process_tree` so the historical
`looplab.server._engine_alive` import path keeps working for tests and callers."""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _on_shared_hub() -> bool:
    """True when this process looks like a JupyterHub single-user server reached through
    `jupyter-server-proxy` (https://hub/user/<name>/proxy/<port>/). That is a SHARED origin: the
    same-origin policy is per-ORIGIN, not per-path, so a same-origin page on a *different path*
    (another proxied app, a file the user opens under /user/<name>/files/...) can read anything
    served on this origin — including an injected UI token. Detected via env JupyterHub sets in
    every single-user server; absent on the default local single-user path."""
    return bool(os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
                or os.environ.get("JUPYTERHUB_API_TOKEN"))


def _engine_alive(rd: Path) -> bool:
    """True iff a LIVE engine process currently drives this run. The engine holds an exclusive OS lock on
    <run_dir>/engine.lock for its whole lifetime (cli._engine_singleton) and the OS frees it on exit —
    even on crash — so this is a race-free, staleness-free liveness signal: a non-blocking acquire that
    FAILS means a process holds it (alive); one that SUCCEEDS means none does (a finished run, or a
    ZOMBIE whose engine died without emitting run_finished — the bug this distinguishes from "thinking").

    Probe-and-release: we never hold the lock past this call, and close the handle in `finally` so even a
    mid-probe error can't leak a lock that would block a real resume. Best-effort — any error → False."""
    lock = rd / "engine.lock"
    if not lock.exists():
        return False                     # no engine has ever locked this dir (or it predates the lock)
    try:
        f = open(lock, "a+")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True              # byte held by a live engine
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)   # we got it → no engine; release at once
            return False
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True              # genuinely HELD by a live engine (EWOULDBLOCK)
            except OSError:
                # flock UNSUPPORTED on this filesystem (FUSE/S3 like geesefs, some NFS) raises ENOTSUP/
                # EINVAL — NOT a held lock. Treat as "can't tell -> not alive" (best-effort, matches the
                # docstring) so it doesn't falsely report every run as live and, e.g., block deleting a
                # stalled run forever. (Locking simply degrades on such mounts — same as the engine
                # singleton; it's a property of the FS.)
                return False
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False                     # platform without file locking → can't tell → assume not alive
    finally:
        f.close()


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
            err_f = open(run_dir / "engine.stderr.log", "ab")
            err = err_f
        except OSError:
            err = subprocess.DEVNULL
    pid: Optional[int] = None
    try:
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
