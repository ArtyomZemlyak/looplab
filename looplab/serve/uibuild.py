"""Build the live React UI bundle on demand (no third-party imports; the engine never imports this).

Python wheels have no post-install hook and `pip install -e` skips build-backend steps, so a fresh
`pip install -e ".[ui]"` installs fastapi/uvicorn but leaves `ui/dist` unbuilt — and the server then
serves a "not built yet" placeholder. Rather than ask the user to `cd ui && npm run build` by hand,
`looplab ui` calls `ensure_ui_built()` on launch: if the bundle is missing or stale and Node/npm are
on PATH, it builds it once. Missing Node degrades to a clear message + placeholder (never a crash).

A build NEVER writes into the live bundle: it emits into a sibling staging directory and that is
published over `dist` only once `is_built()` accepts the staged output, so a failed rebuild leaves
the previous bundle byte-identical and still servable (see the staged-publish section below).
Repairing a publish that was KILLED mid-swap is `recover_ui_bundle()`, a separate entry point on
purpose: it must work where a build cannot, since that is where the failure happens.

Path resolution is the single source of truth for both this builder and `server._ui_dist`:
  - source tree : LOOPLAB_UI_SRC  or  <repo>/ui
  - built bundle: LOOPLAB_UI_DIST, <source>/dist, or the wheel's packaged ``serve/ui_dist``
"""
from __future__ import annotations

import errno
import hashlib
from importlib.resources import files
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Sequence

# `core/atomicio` owns every rename/durability primitive this module publishes with, and is itself
# import-light (stdlib only), so depending on it costs nothing of this module's "importable before
# the [ui] extra is installed" property. Nothing filesystem-atomic is spelled locally: a second copy
# of a rename flag is exactly where a guarantee quietly stops holding
# (tests/test_durable_no_replace_rename.py greps for one).
from looplab.core.atomicio import best_effort_fsync_parent, exchange_paths_if_supported


_DEPS_STAMP = ".looplab-dependencies.sha256"
_BUILD_STAMP = ".looplab-build.sha256"
_BUILD_LOCK = ".looplab-ui-build.lock"
_STAGE_SUFFIX = ".looplab-stage"
_RETIRED_SUFFIX = ".looplab-previous"
_BUILD_LOCK_TIMEOUT_S = 300.0
_BUILD_LOCK_POLL_S = 0.1
# Recovery waits SECONDS, not the build deadline: whoever holds the lock is a build, and a build's
# first act is that same recovery (`_ensure_ui_built_locked`). See `_recover_publish_under_lock`.
_RECOVERY_LOCK_TIMEOUT_S = 5.0


def ui_source_dir() -> Path:
    """The React source tree (Vite project). Override with LOOPLAB_UI_SRC; default <repo>/ui."""
    env = os.environ.get("LOOPLAB_UI_SRC")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "ui"


def packaged_ui_dist_dir() -> Path:
    """The immutable production bundle embedded in a binary wheel."""
    resource = files("looplab.serve").joinpath("ui_dist")
    try:
        return Path(os.fspath(resource))
    except TypeError:
        # Standard wheel installs are unpacked and return a Path. Keep a deterministic filesystem
        # fallback for unusual import loaders; the static server itself also requires real paths.
        return Path(__file__).resolve().parent / "ui_dist"


def ui_dist_dir() -> Path:
    """Built React assets, preferring an editable checkout and falling back to wheel data."""
    env = os.environ.get("LOOPLAB_UI_DIST")
    if env:
        return Path(env)
    source = ui_source_dir()
    if (source / "package.json").is_file():
        return source / "dist"
    return packaged_ui_dist_dir()


def is_built(dist: Path | None = None) -> bool:
    """A bundle is usable iff its entry point (index.html) exists."""
    dist = dist if dist is not None else ui_dist_dir()
    return (dist / "index.html").is_file()


def _has_npm() -> bool:
    return shutil.which("npm") is not None


def _run(args: Sequence[str], *, cwd: Path, log: Callable[[str], None]) -> bool:
    """Run a command, streaming its output to the inherited stdio. Returns True on exit 0.

    On Windows `npm` is the `npm.cmd` shim, which CreateProcess can't launch directly, so we go
    through the shell there; elsewhere we exec the argv list with PATH lookup (no shell)."""
    use_shell = os.name == "nt"
    cmd: object = subprocess.list2cmdline(list(args)) if use_shell else list(args)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), shell=use_shell)  # noqa: S603 - fixed argv, trusted
    except OSError as e:
        log(f"[ui] failed to run `{' '.join(args)}`: {e}")
        return False
    if proc.returncode != 0:
        log(f"[ui] `{' '.join(args)}` exited {proc.returncode}")
        return False
    return True


def _acquire_windows_lock(handle: BinaryIO, *, timeout_s: float = _BUILD_LOCK_TIMEOUT_S) -> None:
    import msvcrt

    _acquire_with_deadline(
        lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1), timeout_s=timeout_s)


def _acquire_posix_lock(handle: BinaryIO, *, timeout_s: float = _BUILD_LOCK_TIMEOUT_S) -> None:
    import fcntl

    # NON-blocking + the same deadline the Windows path uses. A plain blocking LOCK_EX would wait
    # forever behind a wedged sibling builder (a hung npm still holding the lock), so `looplab ui`
    # would just never start and say nothing — where Windows failed after five minutes with a clear
    # TimeoutError. Both platforms now give the same diagnostic on the same schedule.
    _acquire_with_deadline(
        lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB), timeout_s=timeout_s)


def _acquire_with_deadline(attempt, *, timeout_s: float) -> None:
    """Poll a NON-blocking lock `attempt` until it succeeds or `timeout_s` elapses."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            attempt()
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out after {timeout_s:g}s waiting for the UI build lock") from exc
            time.sleep(_BUILD_LOCK_POLL_S)


@contextmanager
def _ui_build_lock(src: Path, *, timeout_s: float = _BUILD_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Serialize dependency installation and Vite output across ``looplab`` processes.

    The stable source-root lock protects both ``node_modules`` and Vite's ``emptyOutDir`` build.
    Platform imports stay local so this module remains stdlib-only and importable everywhere.

    ``timeout_s`` exists for the one caller that is not a build: recovery needs the same exclusion a
    build does but must not inherit a build's willingness to WAIT for one (`_RECOVERY_LOCK_TIMEOUT_S`).
    """
    lock_path = src / _BUILD_LOCK
    handle = lock_path.open("a+b")
    locked = False
    try:
        # msvcrt locks a byte range starting at the current offset, so guarantee byte zero exists.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            _acquire_windows_lock(handle, timeout_s=timeout_s)
            locked = True
        else:
            _acquire_posix_lock(handle, timeout_s=timeout_s)
            locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _dependency_digest(src: Path) -> str:
    """Content identity of the manifests that determine ``node_modules``.

    Include an explicit missing-file marker so adding or removing the lockfile invalidates the
    installation. Hash bytes rather than mtimes: checkout timestamp changes are harmless while every
    dependency declaration change is observed.
    """
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        path = src / name
        digest.update(name.encode("utf-8") + b"\0")
        if path.is_file():
            data = path.read_bytes()
            digest.update(b"present\0" + len(data).to_bytes(8, "big") + data)
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


def _dependency_stamp_path(src: Path) -> Path:
    return src / "node_modules" / _DEPS_STAMP


def _installed_dependency_digest(src: Path) -> str:
    try:
        return _dependency_stamp_path(src).read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _write_dependency_stamp(src: Path, digest: str) -> None:
    """Atomically publish a successful install's manifest identity inside ``node_modules``."""
    stamp = _dependency_stamp_path(src)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(stamp.parent), prefix=f".{_DEPS_STAMP}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, stamp)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _build_digest(src: Path) -> str:
    """Content identity of every repository input that can change the emitted UI bundle.

    Keep dependency freshness separate from output freshness: a current ``node_modules`` only proves
    the toolchain is right, while this digest proves ``dist`` was emitted from the current app source.
    The explicit pattern/directory markers make adding or removing an input observable too.
    """
    digest = hashlib.sha256(b"looplab-ui-build-v1\0")
    files: set[Path] = set()

    for name in ("package.json", "package-lock.json", "index.html"):
        path = src / name
        digest.update(f"file:{name}\0".encode("utf-8"))
        if path.is_file():
            files.add(path)
        else:
            digest.update(b"missing\0")

    for pattern in ("vite.config.*", "postcss.config.*", "tailwind.config.*", "tsconfig*.json"):
        matches = sorted((path for path in src.glob(pattern) if path.is_file()),
                         key=lambda path: path.as_posix())
        digest.update(f"pattern:{pattern}\0{len(matches)}\0".encode("utf-8"))
        files.update(matches)

    for dirname in ("src", "scripts", "public"):
        root = src / dirname
        digest.update(f"tree:{dirname}\0".encode("utf-8"))
        if not root.is_dir():
            digest.update(b"missing\0")
            continue
        files.update(path for path in root.rglob("*") if path.is_file())

    for path in sorted(files, key=lambda item: item.relative_to(src).as_posix()):
        relative = path.relative_to(src).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        try:
            data = path.read_bytes()
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}\0".encode("ascii"))
            continue
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _build_stamp_path(dist: Path) -> Path:
    return dist / _BUILD_STAMP


def _installed_build_digest(dist: Path) -> str:
    try:
        return _build_stamp_path(dist).read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _write_build_stamp(dist: Path, digest: str) -> None:
    """Atomically mark which exact source tree produced a successfully verified ``dist``."""
    stamp = _build_stamp_path(dist)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(stamp.parent), prefix=f".{_BUILD_STAMP}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, stamp)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


# --------------------------------------------------------------- staged build + publish
#
# Vite empties its output directory before it writes anything, and `npm run build` fires the
# `prebuild` hook first — `ui/scripts/clean-dist.mjs`, an unconditional `rm -rf ui/dist`. Either one
# destroys the WORKING bundle seconds before the toolchain gets its chance to fail, and `ui/dist` is
# not tracked in git, so there is nothing to restore from. Whether a failed rebuild survived used to
# depend on WHERE in the build it happened to die; observed failing for real on a JupyterHub geesefs
# home (the `node_modules/.bin` shims cannot carry an exec bit there, and the Node on PATH predated
# `node:util.styleText`), leaving an operator who ran `looplab ui` expecting a no-op with no servable
# UI at all.
#
# So the build writes into a sibling staging directory and we publish that over `dist` only after
# `is_built()` accepts the staged output. A failed build touches the previous bundle not at all.
#
# A publish that is KILLED (SIGINT, OOM, pod cull) is a different failure from a build that FAILS,
# and it needs a different escape route. `KeyboardInterrupt` is not an `OSError`, so it runs none of
# the handlers below; it just stops the process between two renames, with the last good bundle safe
# under a scratch name. Recovery must therefore be reachable on its own — `recover_ui_bundle`, run
# by `looplab ui` whatever its build flags say — because the operator's next command after a killed
# publish is as likely to be `--no-build` as a rebuild, and because a box where a rebuild is
# impossible is where a publish is most likely to have been killed in the first place.


def _stage_dir(dist: Path) -> Path:
    """Where a build writes before it has earned the ``dist`` name.

    A SIBLING of ``dist``: publishing is a rename, and a rename cannot cross a filesystem.
    """
    return dist.parent / f".{dist.name}{_STAGE_SUFFIX}"


def _retired_dir(dist: Path) -> Path:
    """Where the previous bundle waits while the new one takes the ``dist`` name."""
    return dist.parent / f".{dist.name}{_RETIRED_SUFFIX}"


def _discard_dir(path: Path) -> None:
    """Drop one of OUR scratch directories; a leftover is garbage, never a lost bundle."""
    shutil.rmtree(path, ignore_errors=True)


def _prepare_stage(stage: Path) -> None:
    """Guarantee an EMPTY staging directory, or raise.

    A stale ``index.html`` from an abandoned build would let a build that dies before emitting
    anything publish the PREVIOUS attempt's output as if this run had produced it — the same
    false-green the freshness stamp exists to prevent. `rmtree(ignore_errors=True)` followed by an
    existence check, because a partially-deleted tree is the interesting failure and it is the one
    rmtree's error callback would swallow.
    """
    shutil.rmtree(stage, ignore_errors=True)
    if stage.exists():
        raise OSError(errno.ENOTEMPTY, "could not clear the UI staging directory", str(stage))


def _claim_dist_name(dist: Path, intact: Path, *, log: Callable[[str], None]) -> bool:
    """Free the ``dist`` name for the complete bundle at *intact*, or say why we won't.

    Nothing SERVABLE can be lost here — every caller has already established that ``dist`` has no
    ``index.html``. What CAN be lost is data that is not ours. ``dist`` is the operator's own path,
    and it is the one directory this module touches that LoopLab may not have created: neither
    publish path can leave it half-populated (both replace that whole name with a rename, which is
    atomic), so a ``dist`` holding files but no entry point came from somewhere else — a hand-copied
    bundle, an interrupted `cp -r`, a directory of notes.

    So the removal is `rmdir`, never `rmtree`: the KERNEL refuses a non-empty directory, which makes
    "recovery cannot delete somebody's data" a property of the syscall rather than of a check that
    a concurrent writer could race. An empty directory — or no directory at all — is the only thing
    it clears, and it is also the only thing an interrupted publish of ours can produce here.
    """
    if not dist.exists():
        return True
    try:
        dist.rmdir()
    except OSError as exc:
        log(f"[ui] a complete UI bundle is intact at {intact}, but {dist} holds content no LoopLab "
            f"build put there, so it was left untouched ({exc}). Move {dist} aside and rename "
            f"{intact} onto it to serve that bundle again.")
        return False
    return True


def _promote_staged_bundle(stage: Path, dist: Path, *, log: Callable[[str], None]) -> None:
    """Publish a bundle whose FIRST publish died before the rename that would have named it ``dist``.

    Reached only when nothing is servable at ``dist`` and no bundle was ever retired — the signature
    of a crash during a first publish, where the retire rename is skipped because there is nothing
    to retire. The preference `_recover_interrupted_publish` documents (the retired bundle over the
    staged one, because it restores exactly the state the interrupted command started from) does not
    arise here: there is no state to restore to, so the choice is this bundle or nothing.

    The gate is the freshness STAMP, not merely `is_built(stage)`. `_ensure_ui_built_locked` writes
    that stamp as the last thing before publishing — after the toolchain exited 0, after the source
    digest was re-verified unchanged, and after `is_built` accepted the staged output — so a stamped
    stage is a complete, certified build missing only its rename. An UNSTAMPED one is mid-build or
    abandoned output, and publishing that is precisely the false-green `_prepare_stage` exists to
    prevent. Recovery runs under the build lock, so no live build can be writing into ``stage``
    while this reads it.

    Discarding it instead (what the next `_prepare_stage` does) was defensible while recovery only
    ever ran as the prelude to a build that would replace it anyway. It is not defensible now that
    recovery also runs where a rebuild is IMPOSSIBLE — no Node on PATH, or `looplab ui --no-build` —
    because there this bundle is the difference between a servable UI and none.
    """
    if not is_built(stage) or not _installed_build_digest(stage):
        return
    if not _claim_dist_name(dist, stage, log=log):
        return
    try:
        os.rename(stage, dist)
    except OSError as exc:
        log(f"[ui] a complete UI bundle is staged at {stage} but could not be published "
            f"to {dist}: {exc}")
        return
    best_effort_fsync_parent(dist)
    log(f"[ui] published the verified bundle a previous build left staged at {stage}.")


def _recover_interrupted_publish(dist: Path, *, log: Callable[[str], None]) -> None:
    """Heal a publish that died between the two renames, and collect what a finished one left.

    Called under the build lock at the start of every build, from `recover_ui_bundle` when a build
    is not even possible, and directly when an in-process publish raises. Whenever
    ``<dist>.looplab-previous`` exists, exactly two states are possible:

      * ``dist`` is a complete bundle — the publish finished and the retired copy is garbage.
      * ``dist`` is missing or incomplete — the process died mid-swap, and the retired copy is the
        last bundle that was ever SERVED, so it goes back under the ``dist`` name.

    Restoring the RETIRED bundle rather than the staged one is deliberate: it restores exactly the
    state the interrupted command started from. Its freshness stamp no longer matches the source, so
    the caller simply rebuilds — which is the correct outcome, not a lost UI.

    With no retired copy at all the crash was during a FIRST publish, which retires nothing; there
    `_promote_staged_bundle` publishes the staged bundle if the build had already certified it.
    """
    retired = _retired_dir(dist)
    stage = _stage_dir(dist)
    if is_built(dist):
        # The publish finished. Both scratch names can now hold only a SUPERSEDED bundle — the
        # retired copy from an ordered publish, or the swapped-out one an atomic exchange leaves
        # under the staging name — so neither is worth keeping.
        _discard_dir(retired)
        _discard_dir(stage)
        return
    if not is_built(retired):
        _promote_staged_bundle(stage, dist, log=log)
        return
    if not _claim_dist_name(dist, retired, log=log):
        return
    try:
        os.rename(retired, dist)
    except OSError as exc:
        log(f"[ui] the previous bundle is intact at {retired} but could not be restored "
            f"to {dist}: {exc}")
        return
    best_effort_fsync_parent(dist)
    # The staged bundle lost, so it is scratch now: recovery deliberately restores the bundle that
    # was being SERVED, and the restored stamp makes the caller rebuild from the current source
    # anyway. Dropping it here matters because recovery can leave the caller with nothing to do —
    # a restored bundle whose stamp still matches takes the freshness fast path and never reaches
    # `_prepare_stage`, so this is the only place that leftover would ever be collected.
    _discard_dir(stage)
    log(f"[ui] restored the previous UI bundle to {dist} after an interrupted publish.")


def _publish_recovery_pending(dist: Path) -> bool:
    """Cheap probe: is there an interrupted publish to heal, WITHOUT taking the build lock?

    Two stats, so `ensure_ui_built`'s promise that an up-to-date bundle costs no lock, no npm and no
    directory churn survives — that fast path runs on every `looplab ui` launch.

    Deliberately narrow: it asks whether a bundle is UNSERVABLE while a scratch name that might hold
    one exists, not whether there is tidying to do. A leftover scratch directory beside a perfectly
    good ``dist`` is garbage the next build collects anyway, so a probe that reported it would make
    every launch take a lock, and wait on a sibling build for it, to collect something no operator
    can observe — spending the one property this fast path exists to provide on nothing.
    """
    if is_built(dist):
        return False
    return _retired_dir(dist).exists() or _stage_dir(dist).exists()


def _recover_publish_under_lock(src: Path, dist: Path, *, log: Callable[[str], None]) -> None:
    """Run recovery serialized against a concurrent build, without requiring that one be POSSIBLE.

    Recovery mutates the same directories a build does, so it has to hold the same lock — but it
    must not inherit the build's preconditions or its PATIENCE. Its preconditions, because the state
    it repairs is most likely to be met on exactly the box where a build cannot run (see
    `recover_ui_bundle`). Its patience, because whoever holds that lock IS a build, and a build's
    first act is this same recovery — so waiting out the five-minute build deadline would repair
    nothing that is not already being repaired, while turning `looplab ui --no-build`, whose whole
    promise is not to wait for a build, into a long silent stall.

    Lock failure is not fatal here the way it is for a build: there is a bundle on disk either way,
    and the caller reports what is actually servable. Say what happened and leave the disk alone.
    """
    if not _publish_recovery_pending(dist):
        return
    try:
        with _ui_build_lock(src, timeout_s=_RECOVERY_LOCK_TIMEOUT_S):
            _recover_interrupted_publish(dist, log=log)
    except TimeoutError:  # an OSError subclass, so it has to be caught FIRST
        log(f"[ui] another LoopLab build holds {src}; leaving any interrupted publish to it.")
    except (OSError, ImportError, AttributeError, NotImplementedError, ValueError) as exc:
        log(f"[ui] could not lock {src} to recover an interrupted UI publish: {exc}")


def recover_ui_bundle(*, log: Callable[[str], None] = print) -> bool:
    """Restore the last good bundle if a publish was interrupted. Returns True iff one is servable.

    The repair half of `ensure_ui_built`, reachable WITHOUT a toolchain — which is the entire point.
    A publish is interrupted by a signal, an OOM kill or a pod cull, and the operator's next command
    is at least as likely to be `looplab ui --no-build` (the documented escape hatch: serve the last
    good bundle) as a rebuild. On the JupyterHub geesefs home where this failure was first observed
    the rebuild is the part that does not work. Recovery living ONLY inside `_ensure_ui_built_locked`
    therefore made it unreachable in precisely the situation staged publishing exists to survive: a
    complete bundle sitting under ``<dist>.looplab-previous``, no servable UI, and nothing telling
    the operator where it went.

    A bundle LoopLab never published is never touched: an explicit LOOPLAB_UI_DIST and a wheel's
    packaged ``ui_dist`` are reported exactly as `ensure_ui_built` leaves them. Neither can be the
    target of a staged publish, so neither can have an interrupted one to heal.
    """
    dist = ui_dist_dir()
    if os.environ.get("LOOPLAB_UI_DIST"):
        return is_built(dist)
    src = ui_source_dir()
    if not (src / "package.json").is_file():
        # No React sources, so `ui_dist_dir` resolved to the wheel's immutable packaged bundle —
        # nothing stages over that, and the build lock has no source root to live in either.
        return is_built(dist)
    _recover_publish_under_lock(src, dist, log=log)
    return is_built(dist)


def _publish_staged_dist(stage: Path, dist: Path, *, log: Callable[[str], None]) -> None:
    """Give a verified staged bundle the ``dist`` name without ever losing BOTH bundles.

    Preferred: `atomicio.exchange_paths_if_supported` swaps the two names in one atomic step — zero
    window, and the previous bundle lands under the staging name where the discard below collects it.

    Fallback, taken whenever that helper reports the filesystem cannot (geesefs among them): two
    renames, in the one order that is recoverable. RETIRE FIRST (``dist`` -> ``<dist>.looplab-previous``),
    then PUBLISH (``stage`` -> ``dist``). The intuitive order — delete ``dist``, then move the staged
    bundle in — has an instant where the only copy of a working bundle has already been destroyed,
    which is the original defect in miniature. This order has no such instant: between the two renames
    a complete bundle exists under the retired name AND another under the staging name, so a crash
    anywhere in the sequence leaves `_recover_interrupted_publish` something to put back. That is also
    why the old bundle is RENAMED away rather than deleted, and why it is deleted only after the new
    bundle already holds the ``dist`` name.
    """
    retired = _retired_dir(dist)
    if dist.exists() and exchange_paths_if_supported(stage, dist):
        best_effort_fsync_parent(dist)
        _discard_dir(stage)  # holds the PREVIOUS bundle now that the names are swapped
        return

    _discard_dir(retired)
    retired_previous = False
    if dist.exists():
        os.rename(dist, retired)  # sibling rename onto a name just cleared, so it cannot replace
        best_effort_fsync_parent(retired)
        retired_previous = True
    try:
        os.rename(stage, dist)
    except OSError:
        if retired_previous:
            _recover_interrupted_publish(dist, log=log)
        raise
    best_effort_fsync_parent(dist)
    _discard_dir(retired)


def _build_command(src: Path, stage: Path) -> list[str]:
    """``npm run build``, redirected at the staging directory with the destructive hook disabled.

    `--outDir`/`--emptyOutDir` are passed as FLAGS rather than changed in `ui/vite.config.js`: the
    committed config is what a plain `npm run build` and every UI developer use, and it must keep
    emitting `dist`. A flag redirects only the run this module drives.

    `--ignore-scripts` is not optional. `npm run build` fires the `prebuild` hook first, and that
    hook is `scripts/clean-dist.mjs` — an unconditional `rm -rf ui/dist`, i.e. precisely the
    destruction the staging directory exists to prevent, executed before Vite even starts. Its own
    guarantee (nothing from an earlier build survives into this one) is not lost: `_prepare_stage`
    empties the staging directory, and publishing REPLACES the whole `dist` name rather than writing
    into it, so no stale file can survive either step.
    """
    return ["npm", "run", "build", "--ignore-scripts", "--",
            "--outDir", os.path.relpath(stage, src), "--emptyOutDir"]


def _log_toolchain_hints(log: Callable[[str], None]) -> None:
    """Name the two toolchain failures that actually happen on a JupyterHub/FUSE home.

    Both are reported by `npm` as an ordinary non-zero exit, so without this an operator sees only
    "build failed" and a wall of npm output.
    """
    log("[ui] if the output above showed EACCES executing a file under node_modules, the "
        "filesystem is likely mounted `noexec` (common for JupyterHub / NFS data volumes). "
        "Build on an exec filesystem and point LOOPLAB_UI_DIST at the result — see "
        "docs/guide/ui.md (Troubleshooting).")
    log("[ui] if it showed an unsupported-engine warning or a `node:util` TypeError, the Node on "
        "PATH is older than ui/package.json's `engines` range — check `node -v`.")


def _ensure_ui_built_locked(
    src: Path,
    dist: Path,
    *,
    force: bool,
    log: Callable[[str], None],
) -> bool:
    """Recheck freshness and perform one build while the source-root process lock is held."""
    # Before judging freshness, finish or undo a publish a previous process died in the middle of.
    _recover_interrupted_publish(dist, log=log)
    current_build_digest = _build_digest(src)
    if (is_built(dist) and not force
            and _installed_build_digest(dist) == current_build_digest):
        return True
    if is_built(dist) and not force:
        log("[ui] existing bundle is stale or has no freshness stamp; rebuilding…")

    # Install on the first build AND whenever either dependency manifest changed. Merely seeing a
    # node_modules directory is not enough after a pull/upgrade: npm scripts prefer its local binaries,
    # so Vite 5 could otherwise execute a Vite 8/Rolldown config and leave a false-green bundle.
    # Recompute after install because `npm install` may create/update package-lock.json.
    dependency_digest = _dependency_digest(src)
    dependencies_current = ((src / "node_modules").is_dir()
                            and _installed_dependency_digest(src) == dependency_digest)
    if not dependencies_current:
        log("[ui] installing UI dependencies (manifests changed or install is missing)…")
        installer = ["npm", "ci"] if (src / "package-lock.json").is_file() else ["npm", "install"]
        installed = _run(installer, cwd=src, log=log)
        if not installed and installer[1] == "ci":
            log("[ui] `npm ci` failed; retrying with `npm install`…")
            installed = _run(["npm", "install"], cwd=src, log=log)
        if not installed:
            log("[ui] dependency installation failed; refusing to build with stale local tooling.")
            return False
        _write_dependency_stamp(src, _dependency_digest(src))

    # Installation may update package-lock.json, which is also a build input. Pin the exact digest
    # immediately before invoking Vite and refuse to certify a moving source tree.
    requested_build_digest = _build_digest(src)
    stage = _stage_dir(dist)
    try:
        _prepare_stage(stage)
    except OSError as exc:
        log(f"[ui] could not prepare the UI staging directory: {exc}")
        return False

    log("[ui] building the React bundle (npm run build)…")
    try:
        # Nothing below this line can damage an existing bundle: the build writes only into `stage`,
        # and `dist` is not touched until a staged bundle has been verified AND stamped.
        if not _run(_build_command(src, stage), cwd=src, log=log):
            log("[ui] build failed; the requested UI bundle was not produced. The previous bundle "
                "was not touched — `looplab ui --no-build` still serves it.")
            _log_toolchain_hints(log)
            return False

        if _build_digest(src) != requested_build_digest:
            log("[ui] UI build inputs changed while the bundle was building; refusing to stamp a mixed output.")
            return False

        if not is_built(stage):
            log(f"[ui] build did not produce {stage / 'index.html'}; the previous bundle was left "
                "in place.")
            _log_toolchain_hints(log)
            return False

        # Stamp the STAGED bundle, so the bundle and the freshness stamp that certifies it become
        # visible under `dist` in the same rename. A stamp written after publication would leave a
        # window where `dist` is fresh but reads as stale (harmless) — or, worse on a crash, the
        # reverse.
        try:
            _write_build_stamp(stage, requested_build_digest)
        except OSError as exc:
            log(f"[ui] build output could not be freshness-stamped: {exc}")
            return False

        try:
            _publish_staged_dist(stage, dist, log=log)
        except OSError as exc:
            log(f"[ui] could not publish the new bundle over {dist}: {exc}")
            log("[ui] the previous bundle is still in place; nothing was lost.")
            return False
    finally:
        # A failed build leaves no scratch behind; a published one has already consumed the name.
        _discard_dir(stage)
    log("[ui] build complete.")
    return True


def ensure_ui_built(*, force: bool = False, log: Callable[[str], None] = print) -> bool:
    """Make sure a built React bundle is present, building it if needed. Returns True iff a usable
    bundle exists afterwards.

    No-op (returns True) when the output freshness stamp matches current build inputs and not forced.
    When the bundle is missing/stale and a build isn't possible (LOOPLAB_UI_DIST points elsewhere,
    no React sources, no npm, or the cross-process build lock cannot be used) it logs guidance and
    returns False. A caller may still serve an old bundle only by choosing ``--no-build`` explicitly.

    Returning False never means the previous bundle was damaged: every build path emits into a
    staging directory and publishes it only once verified, so an existing ``dist`` is either left
    exactly as it was or wholly replaced by a bundle that passed `is_built`. It does not mean the
    previous bundle is UNREACHABLE either: an interrupted publish is healed before the toolchain is
    consulted, so a False from the no-npm path below still leaves the last good bundle under
    ``dist`` for `--no-build` (`recover_ui_bundle`).
    """
    dist = ui_dist_dir()

    # An explicit LOOPLAB_UI_DIST means "use this prebuilt bundle" (e.g. the Docker image). Never try
    # to build into a path the user pinned — just report whether it's there.
    if os.environ.get("LOOPLAB_UI_DIST"):
        if not is_built(dist):
            log(f"[ui] LOOPLAB_UI_DIST={dist} has no index.html — nothing to serve.")
        return is_built(dist)

    src = ui_source_dir()
    # A binary wheel carries immutable, already-minified assets. There is intentionally nothing to
    # install or rebuild at runtime, even for `--rebuild`; producing a new wheel is the build step.
    if not (src / "package.json").is_file() and is_built(dist):
        return True
    if not (src / "package.json").is_file():
        log(f"[ui] no React sources at {src} — cannot build "
            "(install from a source checkout, or set LOOPLAB_UI_DIST to a prebuilt bundle).")
        return is_built(dist)

    # Finish or undo a publish a previous process died in the middle of BEFORE anything below can
    # return early — the npm check especially, which is exactly where an operator whose publish was
    # killed on a box with no working toolchain used to be told "cannot build" and left with a
    # complete bundle under a scratch name they were never shown. Costs two stats when there is
    # nothing to heal (`_publish_recovery_pending`), and it runs before the freshness check so a
    # restored bundle that IS current still takes the free fast path.
    #
    # This re-locks rather than deferring to `_ensure_ui_built_locked`'s own call, which stays the
    # authoritative one: that call is unreachable from every early return here, and a sibling
    # process can crash a publish in the window between the two acquisitions regardless.
    _recover_publish_under_lock(src, dist, log=log)

    # Fast path before opening the lock. Every stale/missing path repeats this exact check after
    # acquiring it, so a waiter observes the first process's stamp and does not rebuild the same tree.
    current_build_digest = _build_digest(src)
    if (is_built(dist) and not force
            and _installed_build_digest(dist) == current_build_digest):
        return True

    if not _has_npm():
        log("[ui] Node/npm not found on PATH — cannot build the UI automatically.")
        log(f"[ui] build it manually:  cd {src}  &&  npm ci  &&  npm run build")
        return False

    try:
        with _ui_build_lock(src):
            return _ensure_ui_built_locked(src, dist, force=force, log=log)
    except (OSError, ImportError, AttributeError, NotImplementedError, ValueError) as exc:
        log(f"[ui] could not safely lock or complete the UI build at {src}: {exc}")
        return False
