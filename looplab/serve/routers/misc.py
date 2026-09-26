"""Miscellaneous routes: UI default settings (+ the secret store), the task catalogue, LLM health,
the GPU monitor, files-as-truth authoring and the memory viewer. Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4).

ORDER IS LOAD-BEARING inside this router and for its placement: the generic authoring route
`GET /api/{kind}` full-matches ANY single-segment /api GET, so every such literal route must be
registered BEFORE it — this router therefore registers settings/tasks/health/gpu AND `/api/memory`
first (memory before `/api/{kind}`, else it's swallowed as an unknown kind → 404, the empty-Memory-
panel bug), and is included LAST among the /api routers by `make_app`.

ROUTES, NOT PROTOCOLS (review 2026-09-22, SRV2-13; doc 50 SR-04). The two durable-protocol
subsystems and the read model this router used to host are services it calls: the authoring
operation store is `serve/authoring_store.py`, the paid LLM-health probe `serve/llm_probe.py`, and
the Memory panel's projection `serve/memory_projection.py`. What stays here is each route's
declaration, its parameter validation and HTTP translation, and its `Cache-Control` line.
`tests/test_misc_router_split.py` holds the rule on the AST: no class here but a pydantic
request/response shape, and no durability primitive (a lock, a durable write, a process key)
reached from this module."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from looplab.core.config import Settings
from looplab.serve.authoring_store import (
    _AUTHOR_KINDS, _AUTHOR_MAX_BYTES, _AUTHOR_MISSING_REVISION, _AUTHOR_OPERATION_RE,
    _AUTHOR_REVISION_RE, _AUTHOR_TARGET_ROOT_ID_RE, _AUTHOR_WRITABLE_KINDS, _AuthoringFailure,
    _author_target_root_id, _configured_author_root, _current_author_directory,
    _lookup_author_operation, _run_author_operation, _run_legacy_author_write, _valid_author_name,
    author_inventory,
)
from looplab.serve.http import if_none_match, json_object, request_body_contract
from looplab.serve.launch import task_file_roots
from looplab.serve.llm_probe import LLMHealthRegistry, LLMHealthRequest, llm_health_operation
from looplab.serve.memory_projection import memory_view
from looplab.serve.settings_store import (
    _ALLOWED_FIELDS, _SECRET_API_FIELDS, _SECRET_FIELDS, LAUNCH_ONLY_FIELDS,
)
from looplab.serve.settings_ui_schema import (
    SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_ETAG, SETTINGS_UI_SCHEMA_VERSION,
)


_SECRET_KEY_PATTERN = rf"^(?:{'|'.join(re.escape(key) for key in sorted(_SECRET_API_FIELDS))})$"


def _require_writable_author_kind(kind: str) -> None:
    """Refuse a write route for an unknown kind (404) or a READ-ONLY one (405), never the same way.

    `memory_skills` is a real Authoring root — it lists, and every one of its rows says
    `read_only` — so answering its PUTs with "unknown kind" would tell a client the resource it
    just read does not exist. 405 is the honest answer: the resource is there, this method is not
    for it, and the reason is the lifecycle frontmatter named at `_AUTHOR_KINDS`.
    """
    if kind not in _AUTHOR_KINDS:
        raise HTTPException(404, "unknown kind")
    if kind not in _AUTHOR_WRITABLE_KINDS:
        raise HTTPException(405, f"the {kind} store is written by the engine and is read-only here")


class SettingsUIField(BaseModel):
    """One inert editor-field descriptor served to the React settings surfaces."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str
    options: list[str] | None = None
    help: str | None = None
    placeholder: str | int | float | None = None
    warning: str | None = None
    warningTitle: str | None = None
    warningTone: str | None = None
    agents: list[str] | None = None
    minimum: int | float | None = None
    exclusiveMinimum: int | float | None = None
    maximum: int | float | None = None
    # The catalogue loader (settings_ui_schema._load_schema) maps a Settings `lt=` bound to
    # `exclusiveMaximum` and carries an `essential` bool; this forbid-extras response model must accept
    # both, or the first field using either turns GET /api/settings/schema/{v} into a 500.
    exclusiveMaximum: int | float | None = None
    essential: bool | None = None
    nullable: bool
    # The default this row's copy was reviewed against — pinned in the catalogue and verified against
    # the live model at load (settings_ui_schema._check_pinned_default), so it is display data the
    # browser can trust rather than a second, drifting copy. Every row carries one, but it stays
    # OPTIONAL here because a Settings field may legitimately default to None, which serializes
    # identically to an absent key: requiring it at this boundary would buy nothing the loader does
    # not already enforce precisely. Declared explicitly because this model forbids extras.
    default: bool | int | float | str | list | dict | None = None


class SettingsUIGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    sub: str
    fields: list[SettingsUIField]


class SettingsUIRolePill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short: str
    title: str


class SettingsUISchemaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(alias="schema")
    groups: list[SettingsUIGroup]
    agent_role_pills: dict[str, SettingsUIRolePill]
    revision: str


class CredentialStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["none", "stored", "environment", "dotenv"]
    stored: bool
    effective: bool
    active: bool
    clearable: bool
    status: Literal[
        "active", "missing", "unbound", "incomplete", "endpoint_mismatch", "ambient_override",
    ]


class SettingsSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    overrides: dict[str, Any]
    defaults: dict[str, Any]
    settings_revision: str
    secret_revision: str
    credential: CredentialStatusResponse


class SettingsUpdateRequest(BaseModel):
    """Preferred CAS-aware request envelope; legacy flat dictionaries remain accepted."""

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    # Optional so the published OpenAPI is nullable (not the self-contradictory {"type":"string",
    # "default":null}); _expected_revision treats an explicit null the same as absent (no CAS).
    expected_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None


class LegacySettingsUpdateRequest(BaseModel):
    """Compatibility body with setting keys directly at the top level."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={"not": {"required": ["settings"]}},
    )

    expected_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None


class SettingsUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    settings: dict[str, Any]
    overrides: dict[str, Any]
    settings_revision: str
    secret_revision: str
    credential: CredentialStatusResponse


class SecretUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=_SECRET_KEY_PATTERN)
    value: str | None = None
    expected_settings_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None
    expected_secret_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None
    # Compatibility with the previous secret-only CAS field.
    expected_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None


class SecretUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    key: str
    set: bool
    settings_revision: str
    secret_revision: str
    credential: CredentialStatusResponse


class AuthoringOperationRequest(BaseModel):
    """One replayable file mutation bound to the exact source revision the editor displayed."""

    model_config = ConfigDict(extra="forbid")

    text: str
    expected_revision: str = Field(
        min_length=7, max_length=71,
        pattern=r"^(?:missing|sha256:[0-9a-f]{64})$",
    )
    expected_target_root_id: str = Field(
        min_length=76, max_length=76,
        pattern=r"^root-sha256:[0-9a-f]{64}$",
    )


class AuthoringOperationResponse(BaseModel):
    """Public, content-free receipt; the submitted text is represented only by its revision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(alias="schema")
    operation_id: str
    kind: Literal["prompts", "skills", "knowledge"]
    name: str
    target_root_id: str
    expected_revision: str
    desired_revision: str
    status: Literal["prepared", "succeeded", "conflict"]
    result_revision: str | None
    code: str | None
    created_at: int
    updated_at: int
    ok: bool
    replayable: bool


def _expected_revision(body: dict) -> Optional[str]:
    """Parse the optional opaque CAS token without assigning it client-visible semantics."""
    if "expected_revision" not in body:
        return None
    revision = body["expected_revision"]
    if revision is None:      # explicit null == absent (no CAS), matching the now-nullable request schema
        return None
    if not isinstance(revision, str) or not revision or len(revision) > 256:
        raise HTTPException(400, "expected_revision must be a non-empty opaque string")
    return revision


def _expected_named_revision(body: dict, name: str) -> Optional[str]:
    """Parse one optional named opaque CAS token."""
    if name not in body or body[name] is None:
        return None
    revision = body[name]
    if not isinstance(revision, str) or not revision or len(revision) > 256:
        raise HTTPException(400, f"{name} must be a non-empty opaque string")
    return revision


def _revision_conflict(resource: str, expected: str, current: str) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": f"{resource}_revision_conflict",
        "resource": resource,
        "message": f"{resource.capitalize()} changed after this form was loaded; reload and retry.",
        "expected_revision": expected,
        "current_revision": current,
    })


def _authoring_http_failure(exc: _AuthoringFailure, operation_id: str | None = None) -> HTTPException:
    detail: dict[str, Any] = {
        "code": exc.code,
        "message": str(exc),
        "retryable": exc.retryable,
    }
    if operation_id is not None:
        detail["operation_id"] = operation_id
    return HTTPException(
        exc.status_code, detail, headers={"Cache-Control": "private, no-store"})


def build_router(srv) -> APIRouter:
    router = APIRouter()
    store = srv.settings
    # The paid LLM-health probe's replay registry (`serve/llm_probe.py`), built once per app: the
    # lifetime the three closure cells it replaced had.
    llm_health_registry = LLMHealthRegistry()

    # ------------------------------------------------------------------ settings (UI defaults)
    # The engine has no settings server (ADR-18); these are UI-chosen DEFAULTS for new runs,
    # persisted at <run-root>/ui_settings.json and applied to a spawned run as LOOPLAB_* env.
    @router.get(
        "/api/settings/schema/{version}",
        response_model=SettingsUISchemaResponse,
        response_model_exclude_unset=True,
        responses={304: {"description": "The cached semantic schema revision is still current."}},
    )
    def get_settings_schema(version: int, request: Request, response: Response):
        """Revalidated display metadata for the versioned Settings/Config form contract.

        The envelope version describes the wire format. Labels and bounds are derived from the
        deployed Settings model and may change without another format bump, so this stable URL must
        consult its semantic ETag instead of being cached as immutable.
        """
        if version != SETTINGS_UI_SCHEMA_VERSION:
            raise HTTPException(404, "unknown settings UI schema version")
        headers = {
            "Cache-Control": "private, no-cache, max-age=0, must-revalidate",
            "ETag": SETTINGS_UI_SCHEMA_ETAG,
            "X-LoopLab-Schema-Version": str(SETTINGS_UI_SCHEMA_VERSION),
        }
        if if_none_match(request.headers.get("if-none-match"), SETTINGS_UI_SCHEMA_ETAG):
            return Response(status_code=304, headers=headers)
        for name, value in headers.items():
            response.headers[name] = value
        return SETTINGS_UI_SCHEMA

    @router.get("/api/settings", response_model=SettingsSnapshotResponse)
    def get_settings():
        # Bind each returned resource snapshot to the revision checked by its next PUT. The locks
        # prevent a concurrent rename (or secret env mutation) between payload and token reads.
        # This is the only two-resource lock site; its fixed UI-then-secret order avoids lock cycles.
        with store.ui_settings_transaction(), store.secret_transaction():
            overrides = store.load_ui_settings()
            s, credential = store.settings_and_credential(overrides)
            defaults = Settings().model_dump()
            defaults.pop("llm_api_key", None)
            defaults.pop("llm_api_key_base_url", None)
            response = {
                "settings": s.masked_snapshot(),
                "overrides": overrides,
                "defaults": defaults,
                "settings_revision": store.ui_settings_revision(),
                "secret_revision": store.secret_revision(),
                "credential": credential,
            }
        return response

    @router.put(
        "/api/settings",
        response_model=SettingsUpdateResponse,
        openapi_extra=request_body_contract(SettingsUpdateRequest, LegacySettingsUpdateRequest),
    )
    async def put_settings(request: Request):
        body = await json_object(request, "settings payload")
        expected_revision = _expected_revision(body)
        incoming = body.get("settings", body)
        if not isinstance(incoming, dict):
            raise HTTPException(400, "settings must be a JSON object")
        # A LAUNCH FACT is never a default (`serve/settings_store.py::LAUNCH_ONLY_FIELDS`): refused
        # rather than dropped, so a form that shows it saved is never a form that ignored it. The
        # blank value the form echoes back is not a value.
        launch_only = sorted(key for key in LAUNCH_ONLY_FIELDS
                             if key in incoming and str(incoming[key] or "").strip())
        if launch_only:
            raise HTTPException(422, {
                "code": "launch_only_setting",
                "message": (", ".join(launch_only) + " describes one launch and is not saved as a "
                            "default for every run; set it on the launch itself"),
                "field_errors": {key: "set per launch, not saved" for key in launch_only},
            })
        # Keep only known, non-secret fields whose value differs from the engine default — the file
        # stays a small, readable diff rather than a full mirror of every Settings field. Diff
        # against the PROFILE-expanded defaults: the form echoes the expanded snapshot back, and
        # diffing against bare defaults would persist every profile value as an explicit override
        # (a one-way ratchet the profile selector could never undo) while dropping an explicit
        # knob that happens to equal the bare default (breaking "explicit knob wins").
        # Atomic rename prevents torn JSON but cannot protect this larger load→merge→write cycle:
        # two concurrent disjoint PUTs must observe one another instead of losing the first rename.
        # OFF the event loop. `ui_settings_transaction()` takes a threading.Lock plus
        # `interprocess_lock(required=True)` — a blocking `fcntl.flock` with NO timeout — and then
        # does load / merge / `Settings()` validation / atomic write inline. Run inline on the ASGI
        # loop, a lock another server process holds froze every SSE stream and poll on this worker
        # until it was released. Same offload `/control` and `submit_command` already use; the JSON
        # parse and shape checks above stay on the loop because they are cheap and need `await`.
        def _apply() -> dict:
            # Settings and credential status are one UI→secret snapshot. Endpoint-only saves can
            # deactivate an old binding, and the response must say so immediately (no stale green UI).
            with store.settings_write_transaction():
                current_revision = store.ui_settings_revision()
                if expected_revision is not None and expected_revision != current_revision:
                    raise _revision_conflict("settings", expected_revision, current_revision)
                current = store.load_ui_settings()
                prev = store.resolved_settings()
                candidate = dict(current)
                for k, v in incoming.items():
                    if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS or k in LAUNCH_ONLY_FIELDS:
                        continue
                    if k == "agent_control" and isinstance(v, dict):
                        # Governance is a nested sparse PATCH too. Start from the resolved map so
                        # the first customization retains shipped defaults; sparse edits from stale tabs
                        # then merge by governed setting instead of replacing one another wholesale.
                        old_control = prev.get("agent_control")
                        merged_control = dict(old_control) if isinstance(old_control, dict) else {}
                        for setting_key, roles in v.items():
                            if roles is None:
                                merged_control.pop(setting_key, None)
                            else:
                                merged_control[setting_key] = roles
                        candidate[k] = merged_control
                    elif v is None:
                        candidate.pop(k, None)
                    else:
                        candidate[k] = v
                profile = candidate.get("profile") or "default"
                try:
                    base = Settings(profile=profile).model_dump()
                except Exception:  # noqa: BLE001 — unknown profile: fall back to bare defaults
                    base = Settings().model_dump()
                # Fields the form merely ECHOES from the previous resolved snapshot are not user edits:
                # when the profile changes, those echoes must fall away with the old profile, not stick.
                overrides = {}
                profile_changed = "profile" in incoming and profile != prev.get("profile")
                for k, v in candidate.items():
                    if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS:
                        continue
                    if k == "profile":
                        if v != Settings.model_fields["profile"].default:
                            overrides[k] = v
                        continue
                    if base.get(k) == v:
                        continue
                    if profile_changed and k in incoming and k not in current and prev.get(k) == v:
                        continue                       # unchanged echo of the old profile's expansion
                    overrides[k] = v
                try:
                    Settings(**overrides)
                except Exception as exc:  # noqa: BLE001 - reject before persisting a poison configuration
                    raise HTTPException(422, f"invalid settings: {exc}") from exc
                # PATCH-like contract: omission preserves opaque overrides; explicit null/default removes one.
                revision = store.write_ui_settings(overrides)
                resolved, credential = store.settings_and_credential(overrides)
                return {"ok": True, "settings": resolved.masked_snapshot(), "overrides": overrides,
                        "settings_revision": revision,
                        "secret_revision": store.secret_revision(), "credential": credential}
        return await anyio.to_thread.run_sync(_apply)

    @router.put(
        "/api/settings/secret",
        response_model=SecretUpdateResponse,
        openapi_extra=request_body_contract(SecretUpdateRequest),
    )
    async def put_secret(request: Request):
        """Store (or clear) a secret credential securely. The value is written owner-only to
        secrets.json (never ui_settings.json / a run snapshot), atomically bound to the current
        endpoint, and exposed only to an authorized child spawn. The response never returns it."""
        body = await json_object(request, "secret payload")
        allowed_body = {
            "key", "value", "expected_revision",
            "expected_settings_revision", "expected_secret_revision",
        }
        unknown = sorted(str(name) for name in body if name not in allowed_body)
        if unknown:
            raise HTTPException(400, "unknown secret payload field(s): " + ", ".join(unknown))
        expected_settings_revision = _expected_named_revision(body, "expected_settings_revision")
        expected_secret_revision = _expected_named_revision(body, "expected_secret_revision")
        legacy_secret_revision = _expected_revision(body)
        key = body.get("key")
        if key not in _SECRET_API_FIELDS:
            raise HTTPException(
                400, f"unknown secret {key!r} (known: {sorted(_SECRET_API_FIELDS)})")
        value = body.get("value")
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, "value must be a string (or null to clear)")
        clean_value = (value or "").strip()
        if clean_value and (
                expected_settings_revision is None or expected_secret_revision is None):
            raise HTTPException(428, detail={
                "code": "credential_revision_precondition_required",
                "message": (
                    "Saving a credential requires both the Settings and credential revisions "
                    "from the latest Settings snapshot."),
                "required": ["expected_settings_revision", "expected_secret_revision"],
            })
        # The old one-token contract remains accepted only for clear/backcompat. A nonempty write
        # must never bind a key to an endpoint snapshot the caller did not prove it reviewed.
        if not clean_value and expected_secret_revision is None:
            expected_secret_revision = legacy_secret_revision

        def _apply_secret() -> dict:
            # Fixed UI -> secret -> launch order matches PUT /settings. Endpoint, both CAS tokens,
            # pair publication and returned status are one logical transaction, and the write cannot
            # cross a child process's credential-snapshot -> Popen boundary.
            with store.settings_write_transaction():
                settings_revision = store.ui_settings_revision()
                secret_revision = store.secret_revision()
                if (expected_settings_revision is not None
                        and expected_settings_revision != settings_revision):
                    raise _revision_conflict(
                        "settings", expected_settings_revision, settings_revision)
                if (expected_secret_revision is not None
                        and expected_secret_revision != secret_revision):
                    raise _revision_conflict("secret", expected_secret_revision, secret_revision)
                endpoint = None
                if clean_value:
                    from looplab.core.llm import normalize_llm_base_url
                    try:
                        endpoint = normalize_llm_base_url(
                            store.resolve_settings(store.load_ui_settings()).llm_base_url)
                    except Exception as exc:  # noqa: BLE001 - do not reflect URL parser details
                        raise HTTPException(
                            422, "current LLM endpoint is not valid for credential binding") from exc
                new_secret_revision = store._store_secret_locked(
                    key, clean_value, binding=endpoint)
                _settings, credential = store.settings_and_credential(store.load_ui_settings())
                return {
                    "ok": True,
                    "key": key,
                    "set": bool(clean_value),
                    "settings_revision": settings_revision,
                    "secret_revision": new_secret_revision,
                    "credential": credential,
                }

        return await anyio.to_thread.run_sync(_apply_secret)

    # ------------------------------------------------------------------ task catalogue
    # DEPRECATED in OpenAPI only — no behaviour change (review 2026-09-22, SRV2-09): no first-party
    # caller; absent from ui/src, the TUI and the CLI (grep-verified). Kept, not deleted: it is a
    # PUBLIC route, and the flag is the notice a caller this repository cannot see receives.
    @router.get("/api/tasks", deprecated=True)
    def list_tasks():
        """Discover runnable task JSON files (the `examples/` catalogue by default, plus any in the
        run-root) so the launch dialog can offer a pick-list instead of a raw path.

        The directory list comes from `launch.task_file_roots`, which is also the `POST /api/start`
        ALLOW-LIST (backlog C3). One derivation on purpose: this catalogue is what declares a file
        launchable, so a second copy here would let the pick-list offer paths the launcher refuses —
        or, worse, let the launcher accept paths this list never offered."""
        dirs = task_file_roots(srv.root)
        seen, out = set(), []
        for d in dirs:
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict) or not ("goal" in data or "id" in data):
                    continue
                out.append({"path": rp, "name": p.name,
                            "kind": data.get("kind", "quadratic"),
                            "id": data.get("id"), "goal": data.get("goal", ""),
                            "direction": data.get("direction")})
        return {"tasks": out}

    # Late-bind the catalogue for the genesis boss (it grounds its plan on the task list), breaking
    # the route-calls-route dependency between this router and `routers/genesis.py`.
    srv.list_tasks_fn = list_tasks

    @router.get("/api/health")
    def health():
        """P1-3 zero-model liveness: the ONE /api/ route that stays open without a UI token, so a
        monitor can probe process reachability WITHOUT the X-LoopLab-Token AND without triggering a
        billable model completion (that is /api/llm/health, which stays token-gated under deny-default).
        Pure process-liveness — never touches the LLM, a run, or any sensitive state."""
        return {"ok": True, "service": "looplab"}

    @router.post("/api/llm/health")
    def llm_health(body: LLMHealthRequest):
        """One revision-fenced, idempotent and output-capped provider reachability mutation."""
        return llm_health_operation(srv, llm_health_registry, body)

    # ------------------------------------------------------------------ GPU monitor
    @router.get("/api/gpu")
    def gpu():
        try:
            from looplab.core.hardware import query_nvidia_smi
            from looplab.core.parse import to_float as _f
            rows = query_nvidia_smi(
                "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                timeout=4)
            if rows is None:
                return {"available": False}
            gpus = []
            for p in rows:
                if len(p) >= 6:
                    gpus.append({"name": p[0], "util": _f(p[1]), "mem_used": _f(p[2]),
                                 "mem_total": _f(p[3]), "temp": _f(p[4]), "power": _f(p[5])})
            return {"available": True, "gpus": gpus}
        except Exception:  # noqa: BLE001 - no GPU / no nvidia-smi -> soft fail
            return {"available": False}

    @router.get("/api/memory")
    def memory(run_id: str = Query("", max_length=500)):
        # Cross-run memory dir holds several tiers in separate .jsonl files — SPLIT them by filename so
        # the UI can show cases / lessons / notes each with their own shape. MUST be declared BEFORE the
        # `/api/{kind}` catch-all below, else it's swallowed as an unknown kind (→ 404, the reason the
        # Memory panel was silently empty). `cases` stays populated for back-compat.
        #
        # `run_id` is OPTIONAL and additive: absent, this is byte-for-byte the whole-store projection
        # the Memory panel has always read. Supplied, it narrows all three tiers to rows that name that
        # run, which is what makes "what did this run/experiment teach us" affordable from a per-node
        # Inspector — the unfiltered read is a 2 MiB tail scan of every tier plus a concept-shelf build
        # over every run summary, and paying that on each node selection would not be acceptable. It is
        # a FILTER over the same bounded window, never a wider read: an older run can still be outside
        # the window entirely, which is what the per-tier `source_window_truncated` receipt says.
        return memory_view(srv, run_id=run_id)

    # ------------------------------------------------------------------ authoring (files-as-truth)
    def _author_dir(kind: str) -> Optional[Path]:
        return _current_author_directory(srv, kind)

    @router.get("/api/{kind}")
    def list_author(kind: str, response: Response):
        if kind not in _AUTHOR_KINDS:
            raise HTTPException(404, "unknown kind")
        # The returned digest is a write precondition, never a cacheable display hint.
        response.headers["Cache-Control"] = "private, no-store"
        d = _author_dir(kind)
        if d is None:
            return {"dir": None, "target_root_id": None, "files": [],
                    "truncated_files": 0, "inventory_incomplete": False}
        try:
            root = _configured_author_root(d, kind)
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc) from exc
        target_root_id = _author_target_root_id(root)
        if not root.exists():
            return {"dir": str(d), "target_root_id": target_root_id, "files": [],
                    "truncated_files": 0, "inventory_incomplete": False}
        if not root.is_dir():
            raise HTTPException(409, f"configured {kind} path is not a directory")
        files, truncated_files, inventory_incomplete = author_inventory(root, kind)
        return {"dir": str(d), "target_root_id": target_root_id, "files": files,
                # `truncated_files` counts observed candidates omitted/failed; the separate flag is
                # required when traversal caps mean the number of unseen package files is unknown.
                "truncated_files": truncated_files,
                "inventory_incomplete": inventory_incomplete}

    @router.get(
        "/api/{kind}/{name}/operations/{operation_id}",
        response_model=AuthoringOperationResponse,
        responses={404: {"description": "No receipt exists for that exact operation."}},
    )
    def get_author_operation(kind: str, name: str, operation_id: str,
                             expected_target_root_id: str, expected_revision: str,
                             desired_revision: str, response: Response):
        _require_writable_author_kind(kind)
        if not _valid_author_name(name):
            raise HTTPException(400, "bad name (expected a plain <file>.md)")
        if _AUTHOR_OPERATION_RE.fullmatch(operation_id) is None:
            raise HTTPException(400, "operation_id must be a lowercase UUIDv4")
        if _AUTHOR_TARGET_ROOT_ID_RE.fullmatch(expected_target_root_id) is None:
            raise HTTPException(400, "expected_target_root_id is invalid")
        if _AUTHOR_REVISION_RE.fullmatch(expected_revision) is None:
            raise HTTPException(400, "expected_revision is invalid")
        if (_AUTHOR_REVISION_RE.fullmatch(desired_revision) is None
                or desired_revision == _AUTHOR_MISSING_REVISION):
            raise HTTPException(400, "desired_revision is invalid")
        # Observation is deliberately read-only: it neither creates the receipt directory/lock nor
        # advances a prepared write. The exact PUT is the sole recovery transition.
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return _lookup_author_operation(
                srv, kind=kind, name=name, operation_id=operation_id,
                expected_target_root_id=expected_target_root_id,
                expected_revision=expected_revision, desired_revision=desired_revision)
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc, operation_id) from exc

    @router.put(
        "/api/{kind}/{name}/operations/{operation_id}",
        response_model=AuthoringOperationResponse,
        responses={409: {"description": "Operation identity or target conflict."}},
    )
    async def write_author_operation(kind: str, name: str, operation_id: str,
                                     body: AuthoringOperationRequest, response: Response):
        _require_writable_author_kind(kind)
        if not _valid_author_name(name):
            raise HTTPException(400, "bad name (expected a plain <file>.md)")
        if _AUTHOR_OPERATION_RE.fullmatch(operation_id) is None:
            raise HTTPException(400, "operation_id must be a lowercase UUIDv4")
        try:
            text_bytes = body.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(400, "text must be valid UTF-8") from exc
        if len(text_bytes) > _AUTHOR_MAX_BYTES:
            raise HTTPException(
                400, f"file too large: {len(text_bytes)}b > {_AUTHOR_MAX_BYTES}b")
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return await anyio.to_thread.run_sync(lambda: _run_author_operation(
                srv, kind=kind, name=name, operation_id=operation_id,
                text_bytes=text_bytes, expected_revision=body.expected_revision,
                expected_target_root_id=body.expected_target_root_id))
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc, operation_id) from exc

    @router.put("/api/{kind}/{name}")
    async def write_author(kind: str, name: str, request: Request):
        _require_writable_author_kind(kind)
        d = _author_dir(kind)
        if d is None:
            raise HTTPException(400, f"no {kind} dir configured (set LOOPLAB_{kind.upper()}_DIR)")
        # Name allow-list BEFORE mkdir: the traversal guard below only confines the DIRECTORY, so a
        # bare `.env` / `x.py` would land in prompt_dir/skills_dir/knowledge_dir — write-only-invisible,
        # since `list_author` globs `*.md` and would never show it again. These dirs are hot-reloaded
        # into agent context, so only the authored markdown surface belongs here.
        if not _valid_author_name(name):
            raise HTTPException(400, "bad name (expected a plain <file>.md)")
        # BOUNDED BY THE READ CAP, not the server-wide 2 MB body cap: `list_author` only ever shows
        # the first `_AUTHOR_MAX_BYTES`, so a larger PUT was persisted whole and then displayed
        # truncated forever — while the oversized file still got hot-reloaded into agent context in
        # full. And an undecodable body raised UnicodeDecodeError into an unhandled 500; a client
        # sending non-UTF-8 made a bad REQUEST, so say so.
        body = await request.body()
        if len(body) > _AUTHOR_MAX_BYTES:
            raise HTTPException(400, f"file too large: {len(body)}b > {_AUTHOR_MAX_BYTES}b "
                                     "(the editor only displays the first that many bytes)")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, f"file must be UTF-8 text: {exc}") from exc
        # Compatibility only. New clients use the operation route above; this branch has no
        # caller-supplied source revision and therefore remains explicitly last-writer-wins. It is
        # nevertheless atomic and shares the operation lock, so it cannot split a CAS transaction.
        try:
            return await anyio.to_thread.run_sync(lambda: _run_legacy_author_write(
                srv, kind=kind, name=name, text=text))
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc) from exc

    return router
