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

import hmac
import logging
import os
import subprocess  # noqa: F401 — kept as a module attribute: tests patch `looplab.server.subprocess.Popen`
from pathlib import Path
from typing import Optional

# The wire-protocol names this server shares with the TUI + React UI live in `serve/protocol.py`.
# CONTROL_EVENTS and POLL_SECONDS are re-exported here (imported, not aliased) so the historical
# `looplab.serve.server.CONTROL_EVENTS` import path keeps working for tests and callers.
from looplab.serve.protocol import CONTROL_EVENTS, POLL_SECONDS  # noqa: F401 — re-exports
# The LLM client factory is re-exported as a module attribute ON PURPOSE: tests (and operator
# tooling) monkeypatch `looplab.server.make_llm_client`, and every router resolves it late through
# this module (`AppState.make_llm_client`), so the single historical patch point still covers them.
from looplab.adapters.tasks import make_llm_client  # noqa: F401 — patchable re-export
from looplab.serve.engine_proc import (  # noqa: F401 — _engine_alive/_kill_process_tree re-exported
    _engine_alive, _kill_process_tree, _on_shared_hub, install_reap_hooks)
from looplab.serve.projects import ProjectStore
from looplab.serve.reviews import REVIEW_HEADER, ReviewError, ReviewStore, review_request_allowed
from looplab.serve.settings_store import SettingsStore

_log = logging.getLogger("looplab.server")

# These imports require the [ui] extra; importing this module without it raises a clear error.
try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as e:  # pragma: no cover - exercised only without the extra
    raise ModuleNotFoundError(
        "The LoopLab UI server needs the [ui] extra: pip install 'looplab[ui]' "
        "(fastapi + uvicorn)."
    ) from e

from looplab.serve.appstate import AppState
from looplab.serve.jobs import JobRegistry
from looplab.serve import jobs as _jobs_router
from looplab.serve.routers import (
    assistant as _assistant_router, boss as _boss_router, control as _control_router,
    genesis as _genesis_router, misc as _misc_router, org as _org_router,
    reports as _reports_router, reviews as _reviews_router, runs as _runs_router)
# Historical `looplab.server.<name>` import paths for the boss/genesis models + mappers (tests and
# operator tooling import these from here; the definitions moved with their routes).
from looplab.serve.routers.boss import (  # noqa: F401 — re-exports
    _Action, _Plan, _action_to_control, _plan_to_actions)
from looplab.serve.routers.genesis import _GenesisSpec  # noqa: F401 — re-export
# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from looplab.events.digest import theme_rollup as _theme_rollup  # noqa: F401 — re-export


def _ui_dist() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <repo>/ui/dist.

    Single source of truth shared with `uibuild` (the on-launch auto-builder)."""
    from looplab.serve.uibuild import ui_dist_dir
    return ui_dist_dir()


# Raw-content GET routes the UI-token middleware gates (as sensitive as mutations). Module-level
# constants (not rebuilt inside `_require_token` on every request): `_RAW_GET_SUFFIX` match by
# path suffix, `_RAW_GET_EXACT` by exact path. See `_require_token` for the per-route rationale.
_RAW_GET_SUFFIX = ("/artifact", "/artifacts", "/log", "/logs", "/agents_md", "/chat-log",
                   "/conversation", "/assistant/permissions", "/assistant/progress")
_RAW_GET_EXACT = ("/api/prompts", "/api/skills", "/api/knowledge", "/api/memory", "/api/llm/health")


def make_app(run_root: str | os.PathLike) -> "FastAPI":
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
    reviews = ReviewStore(root / ".reviews")
    # Owner auth: LOOPLAB_UI_TOKEN is entered through the SPA's unlock gate and sent as a request
    # header.  It is never embedded in public HTML.  A reviewer can therefore navigate to `/` but
    # cannot promote the read-only capability into owner access.  Unset preserves local open mode;
    # creation of true share links is refused there because the ambient owner API is anonymous.
    ui_token = os.environ.get("LOOPLAB_UI_TOKEN")

    def _owner_authenticated(request: "Request") -> bool:
        supplied = request.headers.get("X-LoopLab-Token", "")
        return bool(ui_token) and hmac.compare_digest(supplied, ui_token)

    def _review_denial(detail: str, kind: str, status_code: int) -> "JSONResponse":
        """A review denial is capability-specific and must never be reused for another bearer.

        In particular, an early revoked/expired response used to bypass the success-path header
        hardening below.  Browsers could then cache that 410 for the shared `/api/review/state` URL
        and replay it after the owner created a fresh link.  `no-store` is authoritative; `Vary` is
        defense in depth for intermediaries that key their caches before applying that directive.
        """
        return JSONResponse(
            {"detail": detail, "kind": kind}, status_code=status_code,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Vary": REVIEW_HEADER,
            },
        )

    if _on_shared_hub():
        if ui_token:
            _log.warning(
                "LoopLab UI is on a SHARED JupyterHub origin (jupyter-server-proxy). LOOPLAB_UI_TOKEN "
                "is a PER-DEPLOYMENT owner secret, NOT per-user identity. It is no longer embedded in "
                "HTML, but a shared origin is still not RBAC: use a private origin or authenticated "
                "reverse proxy for per-user isolation. See docs/guide/deployment.md (Shared JupyterHub).")
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
            review_token = request.headers.get(REVIEW_HEADER, "")
            if review_token:
                try:
                    review = reviews.resolve(review_token)
                except ReviewError as exc:
                    code = 410 if exc.kind in {"expired", "revoked"} else 401
                    return _review_denial(str(exc), exc.kind, code)
                if not review_request_allowed(review, request.method, p):
                    return _review_denial(
                        "read-only review capability does not permit this request",
                        "review_read_only", 403)
                # A valid review capability is independently authorized for this exact GET.  Do not
                # require or expose the owner UI token, including on sensitive evidence reads that
                # the link creator explicitly opted into.
                response = await call_next(request)
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Vary"] = REVIEW_HEADER
                return response
            # Raw-content / captured-output / model-transcript reads are as sensitive as mutations, so
            # gate every GET that returns something OTHER than a light folded projection (events/state/
            # nodes/metrics/cost/config-masked). The raw surface is large — enumerate it exhaustively:
            #   - /artifact(s): repo files a run produced (a checked-in .env, credentials, a log key)
            #   - /assistant/sessions: the full model-facing transcript incl. pasted $HOME file contents
            #   - /log, /nodes/*/logs: RAW event envelopes / captured stdout+stderr (solution code + the
            #     "a log that printed a key" risk) ; /chat-log: the operator↔boss chat transcript
            #   - /spans/{sid}: the UNCAPPED span I/O — the FULL LLM prompt (repo source + pasted files +
            #     printed secrets) and model output ; /trace*, /conversation: the (capped) model transcript
            #   - /agents_md: the repo's AGENTS.md, served verbatim
            #   - /api/{prompts,skills,knowledge}: operator-authored files served verbatim
            #   - /api/memory: cross-run memory rows (goals / rationales / lessons)
            #   - /assistant/permissions: a preview snippet of the file the assistant is about to write
            # (The /assistant/shared/{sid} route STRIPS raw and stays open for sharing.) The UI attaches
            # the token to EVERY request (ui/src/api.js::_authHeaders), so gating these never affects the
            # authenticated UI — only an untokened same-origin caller, which is who the token defends against.
            # (_RAW_GET_SUFFIX / _RAW_GET_EXACT are module-level constants — see above make_app.)
            # /api/runs/{id}/spans/... and /api/runs/{id}/trace[...] carry the raw span I/O. Match the
            # sub-resource by PATH SEGMENT (parts[4]), not a bare `/trace`/`/spans` substring, so a run
            # literally named "trace" or "spans" isn't over-gated on its projection routes.
            _parts = p.split("/")
            _run_scoped_raw = (len(_parts) > 4 and _parts[1] == "api" and _parts[2] == "runs"
                               and _parts[4] in ("spans", "trace"))
            # Node DETAIL (/api/runs/{id}/nodes/{nid}) returns full code, files, persisted stdout_tail,
            # trials, AND parent code — as sensitive as /logs (already gated), but it was open because
            # it has no raw suffix (arch-review §4 P1-3: node detail leaked 200 to an untokened caller).
            # Match the EXACT 6-part path so the LIGHTER /nodes/{nid}/metrics projection (7 parts) and
            # /nodes/{nid}/logs|conversation (gated by suffix) are unaffected.
            _node_detail = (len(_parts) == 6 and _parts[1] == "api" and _parts[2] == "runs"
                            and _parts[4] == "nodes")
            # Job / genesis-job results carry model output + synthesized scope reports. Random ids make
            # them less enumerable (the review rates this P2), but gate them too — defense in depth.
            _job_result = (len(_parts) >= 4 and _parts[1] == "api" and _parts[2] in ("jobs", "genesis"))
            _review_admin = (len(_parts) >= 5 and _parts[1] == "api" and _parts[2] == "runs"
                             and _parts[4] == "reviews")
            _command_status = (len(_parts) >= 5 and _parts[1] == "api" and _parts[2] == "runs"
                               and _parts[4] == "commands")
            sensitive_get = (request.method == "GET"
                             and (p.endswith(_RAW_GET_SUFFIX) or p in _RAW_GET_EXACT
                                  or _run_scoped_raw or _node_detail or _job_result
                                  or _review_admin or _command_status
                                  or "/assistant/sessions" in p))
            mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")
            if ((mutating or sensitive_get) and p.startswith("/api/")
                    and not _owner_authenticated(request)):
                return JSONResponse({"detail": "unauthorized (missing/invalid UI token)"},
                                    status_code=401)
            response = await call_next(request)
            # Keep authenticated API responses out of shared/browser caches, but do not defeat the
            # immutable cache policy of Vite's content-hashed /assets.  The owner and review HTML
            # shells already carry their own stricter headers in the handlers below.
            if p.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
            return response
    else:
        # Even on the default local/open control plane, a request that presents a review identity is
        # capability-scoped and read-only.  This protects the actual review UI from accidental writes;
        # deployment auth is still required if unrelated anonymous callers can reach the owner API.
        @app.middleware("http")
        async def _scope_review_capability(request: "Request", call_next):
            review_token = request.headers.get(REVIEW_HEADER, "")
            if not review_token:
                return await call_next(request)
            try:
                review = reviews.resolve(review_token)
            except ReviewError as exc:
                code = 410 if exc.kind in {"expired", "revoked"} else 401
                return _review_denial(str(exc), exc.kind, code)
            if not review_request_allowed(review, request.method, request.url.path):
                return _review_denial(
                    "read-only review capability does not permit this request",
                    "review_read_only", 403)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Vary"] = REVIEW_HEADER
            return response

    @app.middleware("http")
    async def _command_status_no_store(request: "Request", call_next):
        """Never cache lifecycle observations, including auth/validation exception responses.

        Route-injected headers only cover successful handler returns: FastAPI replaces that Response
        for HTTPException, and the owner-auth middleware can return before routing.  Keeping this
        middleware outermost makes accepted/executing, 4xx, and early 401 responses share one cache
        contract, so polling cannot be frozen by a browser or intermediary.
        """
        response = await call_next(request)
        parts = request.url.path.split("/")
        is_command = (len(parts) >= 5 and parts[1] == "api" and parts[2] == "runs"
                      and parts[4] == "commands")
        if is_command:
            response.headers["Cache-Control"] = "no-store"
            vary = {item.strip() for item in response.headers.get("Vary", "").split(",") if item.strip()}
            vary.update({"X-LoopLab-Token", "Authorization"})
            response.headers["Vary"] = ", ".join(sorted(vary, key=str.lower))
        return response
    projects = ProjectStore(root / "projects.json")   # ClearML-style run organization (UI-only)
    # JupyterHub reaper hooks (ASGI shutdown + atexit backstop) — registered before the secret
    # priming below, matching the original make_app construction side-effect order.
    install_reap_hooks(app)
    settings_store = SettingsStore(root)
    settings_store.prime_env()   # apply stored secrets to this process's env (env/.env still wins)

    srv = AppState(root=root, projects=projects, settings=settings_store, jobs=JobRegistry(),
                   reviews=reviews)
    srv.owner_auth_enabled = bool(ui_token)
    # Explicit app-state handle for lifecycle integrations/tests; routers still close over the same
    # AppState instance, so replacing a dependency such as srv.commands is immediately observed.
    app.state.looplab = srv

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"required": bool(ui_token),
                "authenticated": not ui_token or _owner_authenticated(request)}

    @app.post("/api/auth/verify")
    def auth_verify():
        # When auth is enabled the middleware has already validated the owner header.
        return {"ok": True, "required": bool(ui_token)}

    # Router include ORDER (load-bearing for the overlapping patterns — see routers/__init__.py):
    #   1. runs      — the runs list + per-run read model (also late-binds srv.list_runs_fn)
    #   2. reviews   — owner link management + the token-scoped reviewer manifest
    #   3. org       — projects / super-tasks / label / delete-run
    #   4. control   — /control appends + resume/reset//api/start engine spawns
    #   5. genesis   — /api/research + /api/genesis (reads srv.list_tasks_fn at request time)
    #   6. assistant — sessions + the HITL permission registry
    #   7. boss      — chat-log / chat / suggest / command / report_refresh
    #   8. jobs      — GET /api/jobs/{id} over the shared JobRegistry
    #   9. reports   — cross-run scope reports (reads srv.list_runs_fn at request time)
    #  10. misc      — settings/secret/tasks/health/gpu, then the generic `GET /api/{kind}`
    #                  authoring route, which MUST register after every other /api route it would
    #                  otherwise shadow (and before /api/memory, preserving the original order).
    # The static mounts + the SPA catch-all `GET /{path:path}` come after ALL /api routers.
    for _build in (_runs_router.build_router, _reviews_router.build_router, _org_router.build_router,
                   _control_router.build_router, _genesis_router.build_router,
                   _assistant_router.build_router, _boss_router.build_router,
                   _jobs_router.build_router, _reports_router.build_router,
                   _misc_router.build_router):
        app.include_router(_build(srv))

    # ------------------------------------------------------------------ static React app
    dist = _ui_dist()

    def _index_response(request: "Optional[Request]" = None):
        # Never inject the owner secret into HTML.  The SPA asks the operator to unlock the owner
        # control plane and keeps the token only for this browser tab (sessionStorage).
        html_path = dist / "index.html"
        if not ui_token:
            # `no-cache` = the browser MUST revalidate index.html every load (it's tiny). Without it a
            # shared cache / the jupyter-server-proxy can pin a stale index that references an OLD hashed
            # bundle, so rebuilt UI never loads. The hashed /assets are immutable and stay cacheable.
            return FileResponse(str(html_path), headers={"Cache-Control": "no-cache"})
        html = html_path.read_text(encoding="utf-8")
        headers = {
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
            "Cache-Control": "no-store",
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
        }
        return HTMLResponse(html, headers=headers)

    def _review_index_response():
        """Serve a review SPA without ever embedding the owner UI token.

        ``../`` resolves from ``<mount>/review`` back to ``<mount>/`` and therefore keeps
        Vite's relative asset URLs working behind an arbitrary proxy prefix.  Referrers are disabled
        because the bearer lives in the fragment.
        """
        html = (dist / "index.html").read_text(encoding="utf-8")
        html = html.replace("<head>", '<head><base href="../">', 1)
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        })

    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/")
        def index(request: Request):
            return _index_response(request)

        @app.get("/review")
        def review_spa():
            # Credential validity is rendered by GET /api/review so expired/revoked links get the
            # product's accessible error state rather than a bare server error page.
            return _review_index_response()

        @app.get("/{path:path}")
        def spa(path: str, request: Request):
            # SPA fallback for client-side routes; never shadow /api. Resolve-guard the path so a
            # traversal (`/..%2f..%2fwin.ini`) can't read a file outside the built assets dir — the
            # other file routes guard, this one used to serve any readable file (review C3).
            if path in ("", "index.html"):
                # Keep /index.html and the bare root on the same tokenless owner shell.
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
    import uvicorn
    # root_path: ASGI mount prefix for a NON-stripping proxy (JupyterHub non-strip / reverse-proxy
    # subpath). Empty for local + the common prefix-stripping jupyter-server-proxy (the SPA derives
    # its own prefix from the page path), so this is a no-op there.
    uvicorn.run(make_app(run_root), host=host, port=port, root_path=root_path, log_level="info")
