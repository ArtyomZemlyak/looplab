"""jupyter-server-proxy registration — run the LoopLab UI as a first-class app inside a JupyterHub
single-user server.

With ``pip install looplab[jupyterhub]`` the ``jupyter_serverproxy_servers`` entry point (see
pyproject.toml) points jupyter-server-proxy at :func:`setup_looplab`. JH then shows a **LoopLab tile
in the Launcher**; one click auto-launches ``looplab ui`` on a free port (``{port}`` is substituted
by jsp) and proxies it at ``/user/<name>/proxy/<port>/`` — no terminal, no hand-typed URL. A
token-protected owner shell opens in a new tab because its clickjacking policy deliberately forbids
framing; an anonymous local shell can retain the usual in-frame Launcher experience.

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
- the launched server alone carries ``REAP_ON_EXIT_ENV``: its stop (a pod cull) takes the engines it
  spawned down with it, while a ``looplab ui``/``looplab tui`` server started by hand in the same pod
  leaves its runs running when it stops, exactly as on a laptop.
"""
from __future__ import annotations

import os
from pathlib import Path

# The marker that makes a UI server take the engines IT spawned down with it when it stops
# (`engine_proc._reap_spawned_engines`). Set by THIS launcher and nothing else (review 2026-09-22,
# SRV1-02): the server jsp starts from the Launcher tile is the one whose lifetime IS the pod's, so an
# idle cull that stops it must not orphan a detached engine that keeps billing GPU/CPU and holds its
# run's lock. The reaper used to arm on the JupyterHub environment instead, which EVERY process in the
# pod inherits — so quitting `looplab tui` (whose private child server started the run) or Ctrl-C on
# a hand-started `looplab ui` in a hub terminal killed the operator's runs. `tui_format.ensure_server`
# and `engine_proc._spawn_engine` drop the marker from their children's environment, so it stays with
# the one process this spec launches. Defined here, not in `engine_proc`, because jupyter-server
# imports this module at startup and it must stay as cheap to import as it is today.
REAP_ON_EXIT_ENV = "LOOPLAB_UI_REAP_ON_EXIT"


def _run_root() -> str:
    """Persistent run-root on the user's home volume (overridable). Avoid ``~/data`` — that's often
    the geesefs/S3 FUSE mount, which lacks atomic rename and would corrupt the append-only event log;
    the JH home itself is the right persistent place for run state."""
    return os.environ.get("LOOPLAB_RUN_ROOT") or str(Path.home() / "looplab-runs")


def _launched_shell_is_protected() -> bool:
    """Will the `looplab ui` this spec launches enforce an owner token — and therefore refuse framing?

    The CHILD decides, in `serve/owner_token.py::resolve_owner_token`: a supplied `LOOPLAB_UI_TOKEN`,
    or — on the shared JupyterHub origin, which is where this launcher serves from — a token it MINTS
    unless the operator set `LOOPLAB_UI_ANONYMOUS`. This used to read only the PARENT's
    `LOOPLAB_UI_TOKEN` (review 2026-09-22, SRV1-08): the default hub deployment sets none, the child
    minted one and sent `X-Frame-Options: DENY`, and the Launcher framed it anyway — a blank tile on
    exactly the box LoopLab is deployed on. Spelled here with the same three environment reads rather
    than by importing `owner_token`, which pulls the serving layer into jupyter-server's startup;
    `tests/test_jh_compat.py` drives both deciders over every combination and pins that they agree.
    """
    if os.environ.get("LOOPLAB_UI_TOKEN"):
        return True
    shared_hub = bool(os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
                      or os.environ.get("JUPYTERHUB_API_TOKEN"))
    anonymous = str(os.environ.get("LOOPLAB_UI_ANONYMOUS", "")).strip().lower() in {
        "1", "true", "yes", "on"}
    return shared_hub and not anonymous


def setup_looplab():
    """Return the jupyter-server-proxy launch spec for LoopLab. jsp fills ``{port}`` with a free port
    and proxies it; we keep ``absolute_url=False`` so jsp strips the prefix and the backend still sees
    plain ``/api/...`` (the SPA joins the served prefix itself)."""
    run_root = _run_root()
    protected_shell = _launched_shell_is_protected()
    launcher = {"title": "LoopLab", "enabled": True}
    # Optional Launcher icon — only set when the asset actually exists (jsp tolerates its absence).
    icon = Path(__file__).resolve().parents[2] / "ui" / "public" / "looplab.svg"
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
        # Protected owner/review shells set X-Frame-Options: DENY and CSP frame-ancestors 'none'.
        # Opening those in-frame would leave the Launcher on a browser error page; use a real tab
        # without weakening the server's clickjacking boundary. Anonymous local mode can stay framed.
        "new_browser_tab": protected_shell,
        "launcher_entry": launcher,
        # Belt-and-suspenders so a manual `looplab ui` in this pod resolves the same run-root.
        # REAP_ON_EXIT_ENV marks THIS server as the pod-lifetime one whose stop reaps its engines;
        # a manual `looplab ui` in this pod does not get it, so stopping one leaves its runs running.
        "environment": {"LOOPLAB_RUN_ROOT": run_root, REAP_ON_EXIT_ENV: "1"},
    }
