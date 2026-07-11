"""UI / control-plane commands: `ui` / `tui` / `build-ui`.

Split verbatim out of the flat `looplab/cli.py` (docs/15 §P5.2). Every serve-side import stays
LAZY inside the command bodies (uibuild / server / tui), so `import looplab.cli` never pulls
fastapi/uvicorn — the [ui] extra is only needed when one of these commands actually runs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from looplab.cli import app


@app.command()
def ui(run_root: Path = typer.Option(
           Path(os.environ.get("LOOPLAB_RUN_ROOT", "runs")),
           help="Directory containing run subdirs. Defaults to $LOOPLAB_RUN_ROOT or ./runs — under "
                "JupyterHub set LOOPLAB_RUN_ROOT to a persistent home path (e.g. ~/looplab-runs) so "
                "runs survive a pod cull/restart instead of landing in an ephemeral CWD."),
       host: str = typer.Option("127.0.0.1", help="Bind host."),
       port: int = typer.Option(8765, help="Bind port."),
       root_path: str = typer.Option(
           "", help="ASGI root_path for a NON-prefix-stripping proxy (e.g. /user/<name>/proxy/8765). "
                    "Auto-derived from JUPYTERHUB_SERVICE_PREFIX when unset; harmless for a stripping "
                    "proxy. Lets `looplab ui` work behind both proxy styles without raw uvicorn."),
       build: bool = typer.Option(True, "--build/--no-build",
                                  help="Auto-build the React bundle if it's missing (needs Node/npm)."),
       rebuild: bool = typer.Option(False, "--rebuild",
                                    help="Force a fresh `npm run build` even if a bundle exists.")):
    """Serve the live React UI over the run dirs (needs the [ui] extra: pip install 'looplab[ui]').

    A separate read/control process (ADR-18): tails events.jsonl -> SSE, serves the built React
    app, and turns UI actions into appended control events. Does not change the engine.

    On launch the React bundle is built automatically when it's missing and Node/npm are on PATH,
    so a fresh `pip install -e ".[ui]"` needs no manual `npm run build`. Use --no-build to skip or
    --rebuild to force one."""
    if build or rebuild:
        from looplab.serve.uibuild import ensure_ui_built  # stdlib-only; fine to import before the [ui] check
        ensure_ui_built(force=rebuild, log=typer.echo)
    try:
        from looplab.serve.server import serve  # lazy: keeps the core import-free of fastapi/uvicorn
    except ModuleNotFoundError as e:
        typer.echo(f"UI extra not installed: {e}")
        raise typer.Exit(1)
    jh_prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
    # Default root_path to the JH proxied prefix when unset, so `looplab ui` works behind a
    # NON-stripping proxy without dropping to raw uvicorn. A stripping proxy is unharmed: root_path
    # only affects URL generation, and the SPA derives its own prefix from the served page path.
    if not root_path and jh_prefix:
        root_path = f"{jh_prefix.rstrip('/')}/proxy/{port}"
    if jh_prefix:
        # Behind JupyterHub the UI is reached through jupyter-server-proxy at the user's service
        # prefix, NOT the bind address — advertising http://127.0.0.1:8765 would send the operator to
        # an unreachable URL. Point them at the proxied path instead.
        typer.echo(f"LoopLab UI — open it via your Jupyter proxy: "
                   f"{jh_prefix.rstrip('/')}/proxy/{port}/  (run-root={run_root})")
    else:
        typer.echo(f"LoopLab UI on http://{host}:{port}  (run-root={run_root})")
    serve(run_root, host=host, port=port, root_path=root_path)


@app.command()
def tui(server: Optional[str] = typer.Option(
            None, help="URL of a running LoopLab UI server (e.g. http://127.0.0.1:8765). When omitted, "
                       "reuses a local server if one is up, else auto-launches one (needs the [ui] extra)."),
        run_root: Path = typer.Option(
            Path(os.environ.get("LOOPLAB_RUN_ROOT", "runs")),
            help="Directory of run subdirs — used only when auto-launching a server. Defaults to "
                 "$LOOPLAB_RUN_ROOT or ./runs.")):
    """Drive LoopLab from the terminal: a chat-first TUI to start runs, watch what's running, and steer
    the boss — the most-used slice of the web UI, no browser needed.

    Describe a goal and the boss plans + launches a run; pick a running experiment to see its status at a
    glance and chat with the boss to change course (its actions apply to the live run). It is a thin
    client of the same control plane `looplab ui` serves, so a server is auto-started when none is found
    (API only — no React build); point it at a remote one with --server."""
    from looplab.serve.tui import main as tui_main
    raise typer.Exit(tui_main(server, str(run_root)))


@app.command("build-ui")
def build_ui(force: bool = typer.Option(False, "--force",
                                        help="Rebuild even if a bundle already exists.")):
    """Build the React UI bundle (ui/dist) so `looplab ui` can serve it.

    Runs `npm ci` (first build) + `npm run build` in the UI source tree. Normally you don't need
    this — `looplab ui` builds on demand — but it's handy for CI or a warm-up step."""
    from looplab.serve.uibuild import ensure_ui_built, ui_dist_dir
    ok = ensure_ui_built(force=force, log=typer.echo)
    if ok:
        typer.echo(f"UI bundle ready at {ui_dist_dir()}")
    else:
        raise typer.Exit(1)
