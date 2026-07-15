"""Live UI server (opt-in `[ui]` extra) — a SEPARATE read/control process, never imported by the
engine or the offline test suite (ADR-18: the engine is a process, not a server). It tails each
run's append-only `events.jsonl`, folds it with `replay.fold`, streams the current state to the
browser over SSE, serves the built React assets, and turns UI actions into APPENDED control
events (`EventStore.append`, the same files-as-truth primitive as `LoopLab approve`). It also
spawns/resumes engine runs as subprocesses so a browser can drive a live run end-to-end.

Reuses the canonical projections: `replay.fold`, `eventstore.iter_jsonl`, `traceview.build_trace_view`,
`Settings.masked_snapshot`. No new source of truth lives here.

Run it via `LoopLab ui --run-root runs/` (the CLI lazily imports this so the core stays zero-dep).

BACKLOG §4: `make_app` is still the SOLE public factory, but its former 2,600-line closure is split
across `serve/appstate.py` (shared state + canonical read helpers), `serve/engine_proc.py`,
`serve/jobs.py`, `serve/settings_store.py`, `serve/llm_context.py`, `serve/artifacts.py` and the
route modules under `serve/routers/`. This module keeps the app assembly (middleware, router
include ORDER, static/auth) and the historical re-exports (`looplab.server.<name>` import paths and
the `looplab.server.make_llm_client` monkeypatch point keep working).
"""
from __future__ import annotations

import html as _html
import logging
import os
import subprocess  # noqa: F401 — kept as a module attribute: tests patch `looplab.server.subprocess.Popen`
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

# The wire-protocol names this server shares with the TUI + React UI live in `serve/protocol.py`.
# CONTROL_EVENTS and POLL_SECONDS are re-exported here (imported, not aliased) so the historical
# `looplab.serve.server.CONTROL_EVENTS` import path keeps working for tests and callers.
from looplab.serve.protocol import CONTROL_EVENTS, POLL_SECONDS  # noqa: F401 — re-exports
# The LLM client factory is re-exported as a module attribute ON PURPOSE: tests (and operator
# tooling) monkeypatch `looplab.server.make_llm_client`, and every router resolves it late through
# this module (`AppState.make_llm_client`), so the single historical patch point still covers them.
from looplab.adapters.tasks import make_llm_client  # noqa: F401 — patchable re-export
from looplab.serve.engine_proc import (  # noqa: F401 — _engine_alive/_kill_process_tree re-exported
    _engine_alive, _kill_process_tree, _on_shared_hub, install_reap_hooks,
    install_resume_reconcile_hooks, sweep_stale_lifecycle_locks)
from looplab.serve.projects import ProjectStore
from looplab.serve.schemas import _GenesisSpec  # noqa: F401 — historical pure-model re-export
from looplab.serve.settings_store import SettingsStore

_log = logging.getLogger("looplab.server")

# These imports require the [ui] extra; importing this module without it raises a clear error.
try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as e:  # allow importing pure re-exports without the [ui] extra
    if e.name != "fastapi":
        raise
    FastAPI = Request = CORSMiddleware = FileResponse = HTMLResponse = JSONResponse = StaticFiles = None  # type: ignore[assignment,misc]

# Historical `looplab.server.<name>` import paths for the boss/genesis models + mappers (tests and
# operator tooling import these from here; the definitions moved with their routes).
from looplab.serve.routers.boss import (  # noqa: F401 — re-exports
    _Action, _Plan, _action_to_control, _plan_to_actions)
# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from looplab.events.digest import theme_rollup as _theme_rollup  # noqa: F401 — re-export


def _ui_dist() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <repo>/ui/dist.

    Single source of truth shared with `uibuild` (the on-launch auto-builder)."""
    from looplab.serve.uibuild import ui_dist_dir
    return ui_dist_dir()


# P1-3 default-deny route scoping: when a UI token is set, EVERY /api/ request needs it (reads too,
# not just mutations + an enumerated sensitive list — a new sensitive route that wasn't added to that
# list used to leak; deny-default closes that whole class). The ONLY exceptions are the light,
# non-sensitive routes an untokened monitor legitimately needs — kept to zero-model liveness. The
# authenticated UI attaches the token to every request (ui/src/api.js::_authHeaders), so this never
# affects it, only an untokened same-origin caller. (The old `_RAW_GET_*` sensitive enumeration is now
# subsumed by deny-default; kept below only as documentation of the raw surface.)
_SAFE_UNAUTH_API = ("/api/health",)   # zero-model liveness — the sole untokened-OK exact /api/ route


def _unauth_api_ok(p: str) -> bool:
    """Whether an /api/ path is safe to serve WITHOUT the UI token. Beyond the exact zero-model liveness
    route, this includes the live-state SSE stream `/api/runs/<id>/events`: the browser consumes it via a
    headerless `EventSource` that CANNOT attach `X-LoopLab-Token`, and its payload is already redacted
    (`appstate._public_state_value`) precisely so it is safe unauthenticated. Gating it 401-loops every
    live update and freezes the dashboard on any token-protected deployment (F3). The suffix match is
    exact to the one SSE route (`runs.py::stream_events`); no other `/api/runs/.../events` route exists."""
    return (p in _SAFE_UNAUTH_API
            or (p.startswith("/api/runs/") and p.endswith("/events"))
            # The share route (assistant.py::assistant_shared) is INTENTIONALLY untokened and returns
            # only the read-only, separately-redacted transcript (_shared_message / _shared_text) — so a
            # share link works for a non-token holder. Default-deny would otherwise 401 it (F21).
            or p.startswith("/api/assistant/shared/"))
_RAW_GET_SUFFIX = ("/artifact", "/artifacts", "/log", "/logs", "/agents_md", "/chat-log",
                   "/conversation", "/assistant/permissions", "/assistant/progress")
_RAW_GET_EXACT = ("/api/prompts", "/api/skills", "/api/knowledge", "/api/memory", "/api/llm/health")


def _ui_extra_error(missing: str) -> ModuleNotFoundError:
    return ModuleNotFoundError(
        "The LoopLab UI server needs the [ui] extra: pip install 'looplab[ui]' "
        "(fastapi + uvicorn).",
        name=missing,
    )


def _origin_tuple(value: str | None):
    """Canonical (scheme, host, port) for an HTTP Origin/target, or None when malformed."""
    if not value:
        return None
    try:
        p = urlsplit(value)
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None
    return p.scheme, p.hostname.lower(), port


def _host_name(value: str | None) -> str | None:
    """Canonical hostname from an HTTP Host header/config entry, rejecting ambiguous syntax."""
    if not value or any(c in value for c in ("/", "\\", "@")) or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit("//" + value)
        _ = parsed.port                       # validate a supplied port rather than silently ignoring it
        host = parsed.hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def make_app(run_root: str | os.PathLike) -> "FastAPI":
    if FastAPI is None:
        raise _ui_extra_error("fastapi")
    from looplab.serve import jobs as _jobs_router
    from looplab.serve.appstate import AppState
    from looplab.serve.jobs import JobRegistry
    from looplab.serve.routers import (
        assistant as _assistant_router, boss as _boss_router, control as _control_router,
        cross_run as _cross_run_router, genesis as _genesis_router, misc as _misc_router,
        org as _org_router, reports as _reports_router, runs as _runs_router)

    root = Path(run_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="LoopLab UI", version="0.1.0")
    # CORS allow-list (review C3): the production UI is served SAME-ORIGIN from /dist (needs no
    # CORS), so the only legitimate cross-origin caller is the Vite dev server. Restricting to
    # localhost dev origins (instead of "*") stops any other web page the operator has open from
    # driving this unauthenticated control-plane cross-origin (CSRF). Override with LOOPLAB_UI_CORS
    # (comma-separated) if the dev server runs elsewhere.
    cors = os.environ.get("LOOPLAB_UI_CORS")
    origins = ([o.strip() for o in cors.split(",") if o.strip()] if cors else
               ["http://localhost:5173", "http://127.0.0.1:5173"])
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["*"])
    allowed_origins = {v for o in origins if (v := _origin_tuple(o)) is not None}

    # Host validation closes DNS rebinding: deriving the target origin from request.base_url alone
    # trusts the attacker-controlled Host header, so Origin=Host=evil.example would look same-origin
    # after evil.example is rebound to 127.0.0.1. Local names are safe defaults; a deliberate remote
    # deployment lists its public hostnames in LOOPLAB_UI_HOSTS (comma-separated, optional ports).
    configured_hosts = {
        host for raw in os.environ.get("LOOPLAB_UI_HOSTS", "").split(",")
        if (host := _host_name(raw.strip())) is not None
    }
    allowed_hosts = {"localhost", "127.0.0.1", "::1"} | configured_hosts

    @app.middleware("http")
    async def _reject_untrusted_host(request: "Request", call_next):
        host = _host_name(request.headers.get("host"))
        # Starlette's in-process TestClient uses testserver/testclient; it is not a network-reachable
        # production exception and keeps the HTTP contract tests representative without weakening Host.
        in_process_test = (host == "testserver" and request.client is not None
                           and request.client.host == "testclient")
        if host not in allowed_hosts and not in_process_test:
            return JSONResponse({"detail": "untrusted Host header"}, status_code=421)
        return await call_next(request)

    @app.middleware("http")
    async def _reject_cross_origin_mutation(request: "Request", call_next):
        """CORS controls whether browser JS may READ a response; it does not stop a simple cross-site
        POST from EXECUTING. Reject browser-originated mutations server-side. Requests without Origin
        remain valid for CLI/TUI clients, while same-origin SPA calls and configured Vite origins work."""
        if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                and request.url.path.startswith("/api/")):
            origin = request.headers.get("origin")
            if origin:
                target = _origin_tuple(str(request.base_url))
                supplied = _origin_tuple(origin)
                if supplied is None or (supplied != target and supplied not in allowed_origins):
                    return JSONResponse({"detail": "cross-origin mutation rejected"}, status_code=403)
        return await call_next(request)
    # G1 server auth (review C3): when LOOPLAB_UI_TOKEN is set, require a matching X-LoopLab-Token
    # header on every MUTATING /api/* request (GET/HEAD/OPTIONS + SSE stay open for read/stream). The
    # SPA is served the token in a same-origin <meta> tag (see _index_response). NOTE: "same-origin"
    # protects the token only when each principal has its OWN origin — true for the default local
    # bind (127.0.0.1) and a per-user subdomain. On a SHARED origin (jupyter-server-proxy: every
    # user under https://hub/user/<name>/proxy/<port>/ shares one origin) the token is a
    # per-DEPLOYMENT secret, not per-user — see _on_shared_hub() and the deployment guide. Unset
    # (default local single-user) -> no auth, behaviour unchanged.
    ui_token = os.environ.get("LOOPLAB_UI_TOKEN")
    if _on_shared_hub():
        if ui_token:
            _log.warning(
                "LoopLab UI is on a SHARED JupyterHub origin (jupyter-server-proxy). LOOPLAB_UI_TOKEN "
                "is a PER-DEPLOYMENT secret here, NOT per-user: same-origin policy is per-origin, not "
                "per-path, so any same-origin page (another proxied app, a file you open under "
                "/user/<you>/files/...) can read the token and drive this control plane. For real "
                "per-user isolation give each user a PRIVATE origin (per-user subdomain or a dedicated "
                "host/port), not a shared hub path. See docs/guide/deployment.md (Shared JupyterHub).")
        else:
            _log.warning(
                "LoopLab UI is on a SHARED JupyterHub origin with NO LOOPLAB_UI_TOKEN: the control "
                "plane (start/delete runs, edit configs, shell-executing experiments) is "
                "UNAUTHENTICATED and reachable by any same-origin page. Set LOOPLAB_UI_TOKEN, and for "
                "real isolation serve each user from a PRIVATE origin. See docs/guide/deployment.md.")
    if ui_token:
        @app.middleware("http")
        async def _require_token(request: "Request", call_next):
            p = request.url.path
            # P1-3 DEFAULT-DENY: every /api/ request needs the token — reads included — EXCEPT the
            # explicit safe allow-list (zero-model liveness). Deny-default (vs. the old "gate mutations
            # + an enumerated sensitive-GET list") closes the whole class of leaks where a NEW sensitive
            # route wasn't added to the list (arch-review §4 P1-3: node detail once leaked 200 that way).
            # The authenticated UI attaches the token to EVERY request, so this never affects it — only
            # an untokened same-origin caller, which is who the token defends against. The raw surface
            # the old list enumerated (/artifacts, /log, /spans, /nodes/{nid}, /assistant/sessions,
            # /api/{prompts,skills,knowledge,memory}, …) is all covered now by "deny unless allow-listed".
            # OPTIONS is the CORS preflight: the browser sends it WITHOUT the token, before the real
            # request, and it has no side effect. Gating it here 401s the preflight before CORSMiddleware
            # can answer it, which blocks EVERY cross-origin API call from an allow-listed origin. Let it
            # through so CORSMiddleware can respond.
            if (request.method != "OPTIONS" and p.startswith("/api/")
                    and not _unauth_api_ok(p)
                    and request.headers.get("X-LoopLab-Token") != ui_token):
                return JSONResponse({"detail": "unauthorized (missing/invalid UI token)"},
                                    status_code=401)
            return await call_next(request)
    projects = ProjectStore(root / "projects.json")   # ClearML-style run organization (UI-only)
    # JupyterHub reaper hooks (ASGI shutdown + atexit backstop) — registered before the secret
    # priming below, matching the original make_app construction side-effect order.
    sweep_stale_lifecycle_locks(root)   # F22: GC orphaned per-run lifecycle lock files at startup
    resume_cancel = install_resume_reconcile_hooks(app, root)
    # Shutdown handlers run in registration order. Cancel/join resume timers + tail waiters first,
    # then reap every child that was registered before cancellation won the spawn gate.
    install_reap_hooks(app)
    settings_store = SettingsStore(root)
    settings_store.prime_env()   # apply stored secrets to this process's env (env/.env still wins)

    srv = AppState(root=root, projects=projects, settings=settings_store, jobs=JobRegistry(),
                   resume_cancel=resume_cancel)

    # Router include ORDER (load-bearing for the overlapping patterns — see routers/__init__.py):
    #   1. runs      — the runs list + per-run read model (also late-binds srv.list_runs_fn)
    #   2. org       — projects / super-tasks / label / delete-run
    #   3. control   — /control appends + resume/reset//api/start engine spawns
    #   4. genesis   — /api/research + /api/genesis (reads srv.list_tasks_fn at request time)
    #   5. assistant — sessions + the HITL permission registry
    #   6. boss      — chat-log / chat / suggest / command / report_refresh
    #   7. jobs      — GET /api/jobs/{id} over the shared JobRegistry
    #   8. reports   — cross-run scope reports (reads srv.list_runs_fn at request time)
    #   9. misc      — settings/secret/tasks/health/gpu, then the generic `GET /api/{kind}`
    #                  authoring route, which MUST register after every other /api route it would
    #                  otherwise shadow (and before /api/memory, preserving the original order).
    # The static mounts + the SPA catch-all `GET /{path:path}` come after ALL /api routers.
    for _build in (_runs_router.build_router, _org_router.build_router,
                   _control_router.build_router, _genesis_router.build_router,
                   _assistant_router.build_router, _boss_router.build_router,
                   _jobs_router.build_router, _reports_router.build_router,
                   _cross_run_router.build_router, _misc_router.build_router):
        app.include_router(_build(srv))

    # ------------------------------------------------------------------ static React app
    dist = _ui_dist()

    def _index_response(request: "Optional[Request]" = None):
        # G1: inject the UI token into the served page (same-origin <meta>) when auth is on, so the
        # SPA can echo it on mutating requests. No token (default local single-user) -> serve the
        # file unchanged, behaviour identical to before.
        html_path = dist / "index.html"
        if not ui_token:
            # `no-cache` = the browser MUST revalidate index.html every load (it's tiny). Without it a
            # shared cache / the jupyter-server-proxy can pin a stale index that references an OLD hashed
            # bundle, so rebuilt UI never loads. The hashed /assets are immutable and stay cacheable.
            return FileResponse(str(html_path), headers={"Cache-Control": "no-cache"})
        # Token is set. The <meta> is readable by ANY code that can read this document, INCLUDING a
        # same-origin page on a different path (the shared-JupyterHub-origin case). We can't make a
        # shared origin per-user — that needs a private origin (see deployment guide) — but we DO
        # refuse to hand the token to the easy automated exfil paths and forbid framing the
        # token-bearing doc:
        #   * a programmatic fetch()/XHR carries `Sec-Fetch-Dest: empty` (never a top-level nav);
        #   * a framed load carries `Sec-Fetch-Dest: iframe` AND we send X-Frame-Options/CSP to
        #     stop the frame rendering, so a same-origin parent can't read its contentDocument.
        # Only a genuine top-level document navigation (Sec-Fetch-Dest: document, or a client too
        # old to send the header / a non-browser client) receives the token. This blocks the common
        # `fetch('/').then(r=>r.text())` token-scrape without touching the default local path.
        # NOT a complete fix: a same-origin page can still window.open() the app and read the popup
        # — only a private origin closes that. The headers below are defence-in-depth, not isolation.
        dest = request.headers.get("sec-fetch-dest") if request is not None else None
        html = html_path.read_text(encoding="utf-8")
        if dest is None or dest == "document":
            # Environment values are untrusted deployment input. Attribute-escape the token so a
            # quote cannot terminate `content` and inject markup/script into the privileged page.
            meta = f'<meta name="ll-token" content="{_html.escape(ui_token, quote=True)}">'
            html = html.replace("</head>", meta + "</head>", 1)
        headers = {
            "X-Frame-Options": "DENY",                       # no same-origin iframe -> contentDocument read
            "Content-Security-Policy": "frame-ancestors 'none'",
            "Cache-Control": "no-store",                      # never let a shared cache retain the token
            "X-Content-Type-Options": "nosniff",
        }
        return HTMLResponse(html, headers=headers)

    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/")
        def index(request: Request):
            return _index_response(request)

        @app.get("/{path:path}")
        def spa(path: str, request: Request):
            # SPA fallback for client-side routes; never shadow /api. Resolve-guard the path so a
            # traversal (`/..%2f..%2fwin.ini`) can't read a file outside the built assets dir — the
            # other file routes guard, this one used to serve any readable file (review C3).
            if path in ("", "index.html"):
                # Serve the index THROUGH _index_response so the auth <meta name="ll-token"> is injected
                # here too. A proxy/bookmark that lands on `.../proxy/8765/index.html` (not the bare `/`)
                # would otherwise get the un-injected file → no token → every mutating action 401s.
                # Pass `request` so the Sec-Fetch-Dest token-gating applies to /index.html too — else a
                # `fetch('/index.html')` (dest=empty) would be handed the token, reopening the exfil hole.
                return _index_response(request)
            base = dist.resolve()
            target = (dist / path).resolve()
            if (target == base or base in target.parents) and target.is_file():
                return FileResponse(str(target))
            return _index_response(request)
    else:
        @app.get("/")
        def index_placeholder():
            return JSONResponse({
                "looplab_ui": "backend up; the React app is not built yet",
                "build": "looplab build-ui   (or: cd ui && npm ci && npm run build)",
                "note": "`looplab ui` auto-builds the bundle when Node/npm are on PATH.",
                "api": ["/api/runs", "/api/runs/{id}/state", "/api/runs/{id}/events (SSE)"],
            })

    return app


def serve(run_root: str | os.PathLike, host: str = "127.0.0.1", port: int = 8765,
          root_path: str = "") -> None:
    if FastAPI is None:
        raise _ui_extra_error("fastapi")
    try:
        import uvicorn
    except ModuleNotFoundError as e:
        if e.name != "uvicorn":
            raise
        raise _ui_extra_error("uvicorn") from e
    # root_path: ASGI mount prefix for a NON-stripping proxy (JupyterHub non-strip / reverse-proxy
    # subpath). Empty for local + the common prefix-stripping jupyter-server-proxy (the SPA derives
    # its own prefix from the page path), so this is a no-op there.
    uvicorn.run(make_app(run_root), host=host, port=port, root_path=root_path, log_level="info")
