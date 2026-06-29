"""Build the live React UI bundle on demand (stdlib-only; the engine never imports this).

Python wheels have no post-install hook and `pip install -e` skips build-backend steps, so a fresh
`pip install -e ".[ui]"` installs fastapi/uvicorn but leaves `ui/dist` unbuilt — and the server then
serves a "not built yet" placeholder. Rather than ask the user to `cd ui && npm run build` by hand,
`looplab ui` calls `ensure_ui_built()` on launch: if the bundle is missing and Node/npm are on PATH,
it builds it once. Missing Node degrades to a clear message + placeholder (never a crash).

Path resolution is the single source of truth for both this builder and `server._ui_dist`:
  - source tree : LOOPLAB_UI_SRC  or  <repo>/ui
  - built bundle: LOOPLAB_UI_DIST or  <source>/dist
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence


def ui_source_dir() -> Path:
    """The React source tree (Vite project). Override with LOOPLAB_UI_SRC; default <repo>/ui."""
    env = os.environ.get("LOOPLAB_UI_SRC")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "ui"


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


def ensure_ui_built(*, force: bool = False, log: Callable[[str], None] = print) -> bool:
    """Make sure a built React bundle is present, building it if needed. Returns True iff a usable
    bundle exists afterwards.

    No-op (returns True) when already built and not forced. When the bundle is missing and a build
    isn't possible (LOOPLAB_UI_DIST points elsewhere, no React sources, or no npm) it logs guidance
    and returns False so the caller can still start the server (which serves its placeholder)."""
    dist = ui_dist_dir()
    if is_built(dist) and not force:
        return True

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

    if not _has_npm():
        log("[ui] Node/npm not found on PATH — cannot build the UI automatically.")
        log(f"[ui] build it manually:  cd {src}  &&  npm ci  &&  npm run build")
        return is_built(dist)

    # First build only: install dependencies. Prefer the reproducible `npm ci` (needs the lockfile),
    # falling back to `npm install` when there's no package-lock.json.
    if not (src / "node_modules").is_dir():
        log("[ui] installing UI dependencies (first build only)…")
        installer = ["npm", "ci"] if (src / "package-lock.json").is_file() else ["npm", "install"]
        if not _run(installer, cwd=src, log=log) and installer[1] == "ci":
            log("[ui] `npm ci` failed; retrying with `npm install`…")
            _run(["npm", "install"], cwd=src, log=log)

    log("[ui] building the React bundle (npm run build)…")
    _run(["npm", "run", "build"], cwd=src, log=log)

    ok = is_built(dist)
    if ok:
        log("[ui] build complete.")
    else:
        log(f"[ui] build did not produce {dist / 'index.html'}.")
        log("[ui] if the output above showed EACCES executing a file under node_modules, the "
            "filesystem is likely mounted `noexec` (common for JupyterHub / NFS data volumes). "
            "Build on an exec filesystem and point LOOPLAB_UI_DIST at the result — see "
            "docs/guide/ui.md (Troubleshooting).")
    return ok
