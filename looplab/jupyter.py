"""jupyter-server-proxy registration — run the LoopLab UI as a first-class app inside a JupyterHub
single-user server.

With ``pip install looplab[jupyterhub]`` the ``jupyter_serverproxy_servers`` entry point (see
pyproject.toml) points jupyter-server-proxy at :func:`setup_looplab`. JH then shows a **LoopLab tile
in the Launcher**; one click auto-launches ``looplab ui`` on a free port (``{port}`` is substituted
by jsp) and proxies it at ``/user/<name>/proxy/<port>/`` — no terminal, no hand-typed URL.

Design choices that make this robust on a typical JH pod:
- ``--no-build``: the user's home is frequently a noexec / object-store FUSE mount (geesefs) where the
  esbuild native binary can't run, so an on-launch ``npm run build`` would hang/fail and time the
  proxy out. The JH image should bake a prebuilt bundle and set ``LOOPLAB_UI_DIST`` (see
  Dockerfile.jupyterhub); a plain pip-install without a bundle degrades to the backend's placeholder
  page rather than a doomed build.
- run-root pinned to a persistent home path (``$LOOPLAB_RUN_ROOT`` or ``~/looplab-runs``) so runs
  survive a pod cull/restart instead of landing in an ephemeral CWD.
- ``root_path`` is NOT templated here: ``looplab ui`` auto-derives it from ``JUPYTERHUB_SERVICE_PREFIX``
  (inherited from the single-user server env), so it works behind both the prefix-stripping (default)
  and non-stripping proxy styles without a fragile ``{base_url}`` substitution.
"""
from __future__ import annotations

import os
from pathlib import Path


def _run_root() -> str:
    """Persistent run-root on the user's home volume (overridable). Avoid ``~/data`` — that's often
    the geesefs/S3 FUSE mount, which lacks atomic rename and would corrupt the append-only event log;
    the JH home itself is the right persistent place for run state."""
    return os.environ.get("LOOPLAB_RUN_ROOT") or str(Path.home() / "looplab-runs")


def setup_looplab():
    """Return the jupyter-server-proxy launch spec for LoopLab. jsp fills ``{port}`` with a free port
    and proxies it; we keep ``absolute_url=False`` so jsp strips the prefix and the backend still sees
    plain ``/api/...`` (the SPA joins the served prefix itself)."""
    run_root = _run_root()
    launcher = {"title": "LoopLab", "enabled": True}
    # Optional Launcher icon — only set when the asset actually exists (jsp tolerates its absence).
    icon = Path(__file__).resolve().parents[1] / "ui" / "public" / "looplab.svg"
    if icon.is_file():
        launcher["icon_path"] = str(icon)
    return {
        "command": [
            "looplab", "ui",
            "--port", "{port}",
            "--no-build",                 # never build on the (noexec/FUSE) home — bake a bundle instead
            "--run-root", run_root,       # persistent runs across pod restarts
        ],
        "timeout": 60,                    # first launch (+ build-check) can be slow on a FUSE home
        "absolute_url": False,            # jsp strips the prefix; backend sees /api/... (SPA self-prefixes)
        "new_browser_tab": False,         # open in-frame, like other JH apps
        "launcher_entry": launcher,
        # Belt-and-suspenders so a manual `looplab ui` in this pod resolves the same run-root.
        "environment": {"LOOPLAB_RUN_ROOT": run_root},
    }
