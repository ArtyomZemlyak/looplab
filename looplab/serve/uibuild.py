"""Build the live React UI bundle on demand (stdlib-only; the engine never imports this).

Python wheels have no post-install hook and `pip install -e` skips build-backend steps, so a fresh
`pip install -e ".[ui]"` installs fastapi/uvicorn but leaves `ui/dist` unbuilt — and the server then
serves a "not built yet" placeholder. Rather than ask the user to `cd ui && npm run build` by hand,
`looplab ui` calls `ensure_ui_built()` on launch: if the bundle is missing or stale and Node/npm are
on PATH, it builds it once. Missing Node degrades to a clear message + placeholder (never a crash).

Path resolution is the single source of truth for both this builder and `server._ui_dist`:
  - source tree : LOOPLAB_UI_SRC  or  <repo>/ui
  - built bundle: LOOPLAB_UI_DIST or  <source>/dist
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Sequence


_DEPS_STAMP = ".looplab-dependencies.sha256"
_BUILD_STAMP = ".looplab-build.sha256"
_BUILD_LOCK = ".looplab-ui-build.lock"
_BUILD_LOCK_TIMEOUT_S = 300.0
_BUILD_LOCK_POLL_S = 0.1


def ui_source_dir() -> Path:
    """The React source tree (Vite project). Override with LOOPLAB_UI_SRC; default <repo>/ui."""
    env = os.environ.get("LOOPLAB_UI_SRC")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "ui"


def ui_dist_dir() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <ui-source>/dist."""
    env = os.environ.get("LOOPLAB_UI_DIST")
    if env:
        return Path(env)
    return ui_source_dir() / "dist"


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

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out after {timeout_s:g}s waiting for the UI build lock") from exc
            time.sleep(_BUILD_LOCK_POLL_S)


@contextmanager
def _ui_build_lock(src: Path) -> Iterator[None]:
    """Serialize dependency installation and Vite output across ``looplab`` processes.

    The stable source-root lock protects both ``node_modules`` and Vite's ``emptyOutDir`` build.
    Platform imports stay local so this module remains stdlib-only and importable everywhere.
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
            _acquire_windows_lock(handle)
            locked = True
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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


def _ensure_ui_built_locked(
    src: Path,
    dist: Path,
    *,
    force: bool,
    log: Callable[[str], None],
) -> bool:
    """Recheck freshness and perform one build while the source-root process lock is held."""
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
    log("[ui] building the React bundle (npm run build)…")
    if not _run(["npm", "run", "build"], cwd=src, log=log):
        # A previous dist/index.html may still be present when the build command fails before Vite's
        # emptyOutDir phase.  It remains available for an operator who deliberately serves it with
        # --no-build, but it cannot satisfy this requested build/rebuild: reporting success here would
        # silently keep the stale UI after a dependency or source upgrade.
        log("[ui] build failed; the requested UI bundle was not produced.")
        return False

    if _build_digest(src) != requested_build_digest:
        log("[ui] UI build inputs changed while the bundle was building; refusing to stamp a mixed output.")
        return False

    ok = is_built(dist)
    if ok:
        try:
            _write_build_stamp(dist, requested_build_digest)
        except OSError as exc:
            log(f"[ui] build output could not be freshness-stamped: {exc}")
            return False
        log("[ui] build complete.")
    else:
        log(f"[ui] build did not produce {dist / 'index.html'}.")
        log("[ui] if the output above showed EACCES executing a file under node_modules, the "
            "filesystem is likely mounted `noexec` (common for JupyterHub / NFS data volumes). "
            "Build on an exec filesystem and point LOOPLAB_UI_DIST at the result — see "
            "docs/guide/ui.md (Troubleshooting).")
    return ok


def ensure_ui_built(*, force: bool = False, log: Callable[[str], None] = print) -> bool:
    """Make sure a built React bundle is present, building it if needed. Returns True iff a usable
    bundle exists afterwards.

    No-op (returns True) when the output freshness stamp matches current build inputs and not forced.
    When the bundle is missing/stale and a build isn't possible (LOOPLAB_UI_DIST points elsewhere,
    no React sources, no npm, or the cross-process build lock cannot be used) it logs guidance and
    returns False. A caller may still serve an old bundle only by choosing ``--no-build`` explicitly.
    """
    dist = ui_dist_dir()

    # An explicit LOOPLAB_UI_DIST means "use this prebuilt bundle" (e.g. the Docker image). Never try
    # to build into a path the user pinned — just report whether it's there.
    if os.environ.get("LOOPLAB_UI_DIST"):
        if not is_built(dist):
            log(f"[ui] LOOPLAB_UI_DIST={dist} has no index.html — nothing to serve.")
        return is_built(dist)

    src = ui_source_dir()
    if not (src / "package.json").is_file():
        log(f"[ui] no React sources at {src} — cannot build "
            "(install from a source checkout, or set LOOPLAB_UI_DIST to a prebuilt bundle).")
        return is_built(dist)

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
