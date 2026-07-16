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
import json
import logging
import os
import re
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
from looplab.serve.reviews import REVIEW_HEADER, ReviewError, ReviewStore, review_request_allowed
from looplab.serve.schemas import _GenesisSpec  # noqa: F401 — historical pure-model re-export
from looplab.serve.settings_store import SettingsStore

_log = logging.getLogger("looplab.server")

# These imports require the [ui] extra; importing this module without it raises a clear error.
try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as e:  # allow importing pure re-exports without the [ui] extra
    if e.name != "fastapi":
        raise
    FastAPI = Request = CORSMiddleware = GZipMiddleware = FileResponse = HTMLResponse = JSONResponse = StaticFiles = None  # type: ignore[assignment,misc]

# Historical `looplab.server.<name>` import paths for the boss/genesis models + mappers (tests and
# operator tooling import these from here; the definitions moved with their routes).
from looplab.serve.routers.boss import (  # noqa: E402, F401 — re-exports after optional UI probe
    _Action, _Plan, _action_to_control, _plan_to_actions)
# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from looplab.events.digest import theme_rollup as _theme_rollup  # noqa: E402, F401 — re-export


def _ui_dist() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <repo>/ui/dist.

    Single source of truth shared with `uibuild` (the on-launch auto-builder)."""
    from looplab.serve.uibuild import ui_dist_dir
    return ui_dist_dir()


_IMMUTABLE_ASSET_CACHE = "public, max-age=31536000, immutable"
_REVALIDATE_ASSET_CACHE = "no-cache"
_CONTENT_HASHED_ASSET = re.compile(r"-[A-Za-z0-9_-]{8,}\.[^/]+$")
_API_REQUEST_BODY_MAX = 2_000_000


def _is_live_sse_path(path: str) -> bool:
    """The two streaming routes that must bypass compression on every supported Starlette version."""
    parts = str(path or "").strip("/").split("/")
    # Match an exact route suffix so deployments that leave a proxy/root-path prefix in
    # ``scope["path"]`` retain the same no-buffering contract without accepting lookalike routes.
    return ((len(parts) >= 4 and parts[-4:-2] == ["api", "runs"] and parts[-1] == "events")
            or (len(parts) >= 5 and parts[-5:-2] == ["api", "assistant", "sessions"]
                and parts[-1] == "message_stream"))


def _scope_route_path(scope) -> str:
    """Return the route-local path for stripping and non-stripping ASGI ``root_path`` setups."""
    path = str(scope.get("path") or "")
    root_path = str(scope.get("root_path") or "").rstrip("/")
    if root_path and (path == root_path or path.startswith(root_path + "/")):
        return path[len(root_path):] or "/"
    return path


class _SSESafeGZipMiddleware:
    """Use Starlette gzip while guaranteeing known live streams never enter its responder.

    Current Starlette also excludes ``text/event-stream`` by content type. The route guard keeps the
    no-buffering invariant true on the project's older supported FastAPI/Starlette combinations too.
    """

    def __init__(self, app, *, minimum_size: int = 500, compresslevel: int = 6):
        self.app = app
        self.compressed = GZipMiddleware(
            app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and _is_live_sse_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        await self.compressed(scope, receive, send)


class _APIRequestBodyLimitMiddleware:
    """Bound every API request body, including chunked requests without Content-Length.

    The UI has no binary upload endpoint; its largest legitimate payloads are authoring text and
    structured launch/chat requests. Buffering at most two megabytes before dispatch gives every
    router the same hard memory envelope and prevents a handler that calls ``request.json()`` or
    ``request.body()`` from allocating an attacker-controlled body first. Static assets and HTML do
    not pass through this path.
    """

    def __init__(self, app, *, max_bytes: int = _API_REQUEST_BODY_MAX):
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def _reject(self, send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not _scope_route_path(scope).startswith("/api/"):
            await self.app(scope, receive, send)
            return

        headers = {bytes(k).lower(): bytes(v) for k, v in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except (TypeError, ValueError):
                await self._reject(send, 400, "invalid Content-Length")
                return
            if declared < 0:
                await self._reject(send, 400, "invalid Content-Length")
                return
            if declared > self.max_bytes:
                await self._reject(send, 413, "API request body is too large")
                return

        buffered = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                # Preserve the disconnect for the downstream request object; no response is fabricated.
                async def disconnected():
                    return {"type": "http.disconnect"}
                await self.app(scope, disconnected, send)
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body") or b""
            # Reject before copying an attacker-sized ASGI chunk. Extending first
            # defeats the advertised memory envelope even though the eventual response is 413.
            if len(chunk) > self.max_bytes - len(buffered):
                await self._reject(send, 413, "API request body is too large")
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


def _vite_manifest_assets(directory: Path) -> set[str]:
    """Return asset-relative paths that Vite's build manifest marks as versioned outputs."""
    manifest_path = directory.parent / ".vite" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(manifest, dict):
        return set()

    assets: set[str] = set()
    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        candidates = [entry.get("file")]
        for key in ("css", "assets"):
            values = entry.get(key)
            if isinstance(values, list):
                candidates.extend(values)
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = candidate.replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            normalized = normalized.lstrip("/")
            if normalized.startswith("assets/"):
                relative = normalized[len("assets/"):]
                # Manifest membership proves the file belongs to this build, but a custom Vite output
                # can still emit a stable URL such as assets/runtime.js. A one-year immutable policy is
                # safe only when the URL itself also carries Vite's content fingerprint.
                if _CONTENT_HASHED_ASSET.search(relative):
                    assets.add(relative)
    return assets


def _immutable_static_files(directory: Path):
    """StaticFiles with immutable caching only for outputs named by Vite's build manifest."""
    if StaticFiles is None:  # pragma: no cover - make_app rejects the missing UI extra first
        raise _ui_extra_error("fastapi")
    immutable_paths = _vite_manifest_assets(directory)

    class _ImmutableStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            # Preserve the policy on conditional hits too; a 304 updates the cached representation's
            # metadata and must not silently downgrade it to heuristic browser caching.
            if response.status_code in (200, 304):
                normalized = str(path).replace("\\", "/").lstrip("/")
                response.headers["Cache-Control"] = (
                    _IMMUTABLE_ASSET_CACHE
                    if normalized in immutable_paths
                    else _REVALIDATE_ASSET_CACHE
                )
            return response

    return _ImmutableStaticFiles(directory=str(directory))


# P1-3 default-deny route scoping: when a UI token is set, EVERY /api/ request needs it (reads too,
# not just mutations + an enumerated sensitive list — a new sensitive route that wasn't added to that
# list used to leak; deny-default closes that whole class). The ONLY exceptions are the light,
# non-sensitive routes an untokened monitor legitimately needs — kept to zero-model liveness. The
# authenticated UI attaches the token to every request (ui/src/api.js::_authHeaders), so this never
# affects it, only an untokened same-origin caller. (The old `_RAW_GET_*` sensitive enumeration is now
# subsumed by deny-default; kept below only as documentation of the raw surface.)
_SAFE_UNAUTH_API = (
    "/api/health",
    # The tokenless owner shell must learn whether it needs to show the unlock gate. This endpoint
    # exposes only two booleans and performs no model/disk work.
    "/api/auth/status",
)


def _unauth_api_ok(p: str) -> bool:
    """Whether an /api/ path is safe to serve WITHOUT the UI token.

    Run-state SSE is intentionally not exempt: even its redacted projection is private portfolio state,
    and the React client now consumes it through authenticated fetch-SSE rather than headerless
    ``EventSource``.
    """
    return (p in _SAFE_UNAUTH_API
            # The share route (assistant.py::assistant_shared) is INTENTIONALLY untokened and returns
            # only the read-only, separately-redacted transcript (_shared_message / _shared_text) — so a
            # share link works for a non-token holder. Default-deny would otherwise 401 it (F21).
            or p.startswith("/api/assistant/shared/"))
_RAW_GET_SUFFIX = ("/artifact", "/artifacts", "/log", "/log-page", "/logs", "/agents_md", "/chat-log",
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
        assistant as _assistant_router, attention as _attention_router,
        boss as _boss_router, collaboration as _collaboration_router,
        control as _control_router, cross_run as _cross_run_router,
        genesis as _genesis_router, misc as _misc_router, org as _org_router,
        reports as _reports_router, reviews as _reviews_router, runs as _runs_router)

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
    app.add_middleware(_APIRequestBodyLimitMiddleware, max_bytes=_API_REQUEST_BODY_MAX)
    # Enforce the same compressed-transfer contract the manifest bundle gate measures while keeping live
    # EventSource and assistant token streams unbuffered across every supported Starlette version.
    app.add_middleware(_SSESafeGZipMiddleware, minimum_size=500, compresslevel=6)
    reviews = ReviewStore(root / ".reviews")
    # Owner auth is entered through the SPA unlock gate and never embedded in public HTML.
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
        route_path = _scope_route_path(request.scope)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                and route_path.startswith("/api/")):
            origin = request.headers.get("origin")
            if origin:
                target = _origin_tuple(str(request.base_url))
                supplied = _origin_tuple(origin)
                if supplied is None or (supplied != target and supplied not in allowed_origins):
                    return JSONResponse({"detail": "cross-origin mutation rejected"}, status_code=403)
        return await call_next(request)
    # Owner auth: when LOOPLAB_UI_TOKEN is set, default-deny every owner API request unless it carries
    # a matching X-LoopLab-Token; only the explicit zero-model/status and review-share
    # exceptions above remain open. The token is never embedded in HTML: the owner enters it through
    # the SPA unlock gate and it remains in that tab's sessionStorage. NOTE: a shared origin (notably
    # jupyter-server-proxy paths under one host) is still one browser principal, so this static token is
    # a per-DEPLOYMENT credential rather than user identity or RBAC. See the deployment guide. Unset
    # (the default local single-user mode) leaves the API unauthenticated, preserving existing behavior.
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
            # A non-stripping reverse proxy leaves its mount prefix in scope.path while Starlette
            # routes against root_path-relative URLs. Security policy must use that same identity.
            p = _scope_route_path(request.scope)
            review_token = request.headers.get(REVIEW_HEADER, "")
            if review_token:
                try:
                    review = reviews.resolve(review_token)
                except ReviewError as exc:
                    code = 410 if exc.kind in {"expired", "revoked", "generation"} else 401
                    return _review_denial(str(exc), exc.kind, code)
                if not review_request_allowed(review, request.method, p):
                    return _review_denial(
                        "read-only review capability does not permit this request",
                        "review_read_only", 403)
                # A review identity is independently scoped even if an owner header is also present:
                # bearer composition must never promote a read-only link into the owner plane.
                response = await call_next(request)
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Vary"] = REVIEW_HEADER
                return response

            # Default-deny every owner API request except the tiny explicit unauthenticated surface.
            # OPTIONS is a side-effect-free CORS preflight and must reach CORSMiddleware.
            if (request.method != "OPTIONS" and p.startswith("/api/")
                    and not _unauth_api_ok(p)
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
                code = 410 if exc.kind in {"expired", "revoked", "generation"} else 401
                return _review_denial(str(exc), exc.kind, code)
            if not review_request_allowed(
                    review, request.method, _scope_route_path(request.scope)):
                return _review_denial(
                    "read-only review capability does not permit this request",
                    "review_read_only", 403)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Vary"] = REVIEW_HEADER
            return response

    @app.middleware("http")
    async def _volatile_api_no_store(request: "Request", call_next):
        """Never cache lifecycle observations or generation-bound log pages, including errors.

        Route-injected headers only cover successful handler returns: FastAPI replaces that Response
        for HTTPException, and the owner-auth middleware can return before routing.  Keeping this
        middleware outermost makes accepted/executing, 4xx, and early 401 responses share one cache
        contract, so polling cannot be frozen by a browser or intermediary.
        """
        response = await call_next(request)
        route_path = _scope_route_path(request.scope)
        parts = route_path.split("/")
        is_command = (len(parts) >= 5 and parts[1] == "api" and parts[2] == "runs"
                      and parts[4] == "commands")
        is_start_status = (len(parts) == 5 and parts[1] == "api" and parts[2] == "start"
                           and parts[4] == "status")
        is_log_page = (len(parts) == 5 and parts[1] == "api" and parts[2] == "runs"
                       and parts[4] == "log-page")
        is_report_refresh = (len(parts) == 5 and parts[1] == "api" and parts[2] == "runs"
                             and parts[4] == "report_refresh")
        # CODEX AGENT: scope reports are live, membership-bound observations. A cached GET or paid
        # generation result can claim authority for a scope snapshot that no longer exists.
        is_scope_report = (route_path == "/api/scope-report"
                           or route_path.startswith("/api/scope-report/"))
        is_job = len(parts) == 4 and parts[1] == "api" and parts[2] == "jobs"
        is_comments = (route_path == "/api/review/comments"
                       or (len(parts) >= 5 and parts[1] == "api" and parts[2] == "runs"
                           and parts[4] == "comments"))
        is_attention = route_path in {"/api/attention", "/api/assistant/permissions"}
        if (is_command or is_start_status or is_log_page or is_report_refresh or is_scope_report
                or is_job or is_comments or is_attention):
            response.headers["Cache-Control"] = "no-store"
            vary = {item.strip() for item in response.headers.get("Vary", "").split(",") if item.strip()}
            vary.update({"X-LoopLab-Token", "Authorization"})
            if is_log_page or is_comments:
                vary.add(REVIEW_HEADER)
            elif is_attention:
                vary.update({"X-LoopLab-Token", REVIEW_HEADER})
            elif not is_job:
                vary.add("Idempotency-Key")
            response.headers["Vary"] = ", ".join(sorted(vary, key=str.lower))
        return response
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
                   reviews=reviews, resume_cancel=resume_cancel)
    srv.owner_auth_enabled = bool(ui_token)
    # Explicit app-state handle for lifecycle integrations/tests; routers still close over the same
    # AppState instance, so replacing a dependency such as srv.commands is immediately observed.
    app.state.looplab = srv

    @app.on_event("startup")
    def _recover_restart_command_workers():
        # A process can die after publishing an accepted restart record but before its worker appends
        # the folded restart intent. Once appended, install_resume_reconcile_hooks is independently
        # sufficient; this hook closes the earlier reserve->append window without any browser poll.
        srv.commands.recover_pending_restarts()

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
    #   2. attention — owner-only redacted event/liveness projection (observation-only)
    #   3. collaboration — owner-only bounded comment current/history projections
    #   4. reviews   — owner link management + the token-scoped reviewer manifest
    #   5. org       — projects / super-tasks / label / delete-run
    #   6. control   — /control appends + resume/reset//api/start engine spawns
    #   7. genesis   — /api/research + /api/genesis (reads srv.list_tasks_fn at request time)
    #   8. assistant — sessions + the HITL permission registry
    #   9. boss      — chat-log / chat / suggest / command / report_refresh
    #  10. jobs      — GET /api/jobs/{id} over the shared JobRegistry
    #  11. reports   — cross-run scope reports (reads srv.list_runs_fn at request time)
    #  12. misc      — settings/secret/tasks/health/gpu, then the generic `GET /api/{kind}`
    #                  authoring route, which MUST register after every other /api route it would
    #                  otherwise shadow (and before /api/memory, preserving the original order).
    # The static mounts + the SPA catch-all `GET /{path:path}` come after ALL /api routers.
    for _build in (_runs_router.build_router, _attention_router.build_router,
                   _collaboration_router.build_router,
                   _reviews_router.build_router, _org_router.build_router,
                   _control_router.build_router, _genesis_router.build_router,
                   _assistant_router.build_router, _boss_router.build_router,
                   _jobs_router.build_router, _reports_router.build_router,
                   _cross_run_router.build_router, _misc_router.build_router):
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
        app.mount("/assets", _immutable_static_files(dist / "assets"), name="assets")

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
            if path == "api" or path.startswith("api/"):
                # Unknown API paths are protocol errors, never client-side routes. In particular an
                # allowed review bearer must not turn `/api/review/typo` into a misleading 200 SPA.
                return JSONResponse({"detail": "no such API route"}, status_code=404)
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
