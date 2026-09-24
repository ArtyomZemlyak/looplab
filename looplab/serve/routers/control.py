"""Control-plane routes: the durable command lifecycle (/commands) and the reset/start/clear-trace
engine operations. Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Literal, Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from looplab.serve import engine_proc as _engine_proc
from looplab.core.atomicio import atomic_write_bytes, atomic_write_text
from looplab.core.config import Settings
from looplab.core.errors import LLMError
from looplab.events.eventstore import EventStoreLockError
from looplab.serve.appstate import _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX
from looplab.serve.http import json_object
from looplab.serve.engine_proc import _engine_alive, _engine_liveness
from looplab.serve.launch import (
    idempotency_key_digest,
    launch_request_digest,
    preflight_response,
    preflight_start,
    validate_launch,
    safe_run_dir,
    validate_idempotency_key,
)
from looplab.serve.protocol import EXPECTED_RUN_GENERATION_FIELD, GENESIS_CHAT_SEQ_BASE
from looplab.serve.reset_route import durable_reset_run
from looplab.serve.settings_store import SettingsRevisionConflict
from looplab.serve.start_record import (
    inspect_keyed_start, raise_existing_start, reconcile_start,
    release_unspawned_start_namespace)
from looplab.serve.trace_clear import durable_clear_node_trace


class RunCommandRequest(BaseModel):
    """Documented command body; raw parsing below preserves established HTTP 400 behavior."""

    # Older API clients may attach correlation metadata at this envelope level. It is ignored rather
    # than persisted; event-specific ``data`` remains closed and server-normalized.
    model_config = ConfigDict(extra="allow")

    type: str
    data: dict[str, Any] | None = None
    expected_generation: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class RunCommandError(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    # Older HTTP conflict details omitted one or both advisory fields; durable command errors include
    # them explicitly. Defaults keep the shared documentation schema honest for both envelopes.
    retryable: bool = False
    remediation: str = ""


class RunCommandSubject(BaseModel):
    """Closed public identity currently emitted only for a permanent hypothesis deletion."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["hypothesis"]
    id: str = Field(min_length=1, max_length=256)
    status: Literal["deleted"]


class RunCommandRecord(BaseModel):
    """Public durable command record; additive observation fields remain forward compatible."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(pattern=r"^cmd_[0-9a-f]{32}$")
    status: Literal[
        "accepted", "executing", "succeeded", "noop", "failed", "rejected", "timed_out",
    ]
    event_type: str
    error: RunCommandError | None
    # Pre-generation command records remain readable as terminal history.
    run_generation: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    # Present only when the server can derive a closed, non-secret semantic target from normalized
    # immutable command data. Clients pair it with run_generation before releasing destructive recovery.
    subject: Optional[RunCommandSubject] = None
    created_at: float
    updated_at: float
    event_seq: Optional[int] = Field(default=None, ge=0)


class RunCommandHTTPError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str | RunCommandError


def _command_post_openapi() -> dict[str, Any]:
    """Expose the manual header/body contract without replacing its compatibility parser."""
    return {
        "parameters": [{
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "description": "Opaque command identity; reuse it only for an exact retry.",
            "schema": {"type": "string", "minLength": 1, "maxLength": 512},
        }],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": RunCommandRequest.model_json_schema()},
            },
        },
    }


def _command_responses(description: str) -> dict[int, dict[str, Any]]:
    return {
        200: {"model": RunCommandRecord, "description": description},
        400: {"model": RunCommandHTTPError, "description": "Malformed command request"},
        404: {"model": RunCommandHTTPError, "description": "Run or command not found"},
        409: {"model": RunCommandHTTPError, "description": "Generation or lifecycle conflict"},
        503: {"model": RunCommandHTTPError, "description": "Durability or ownership unavailable"},
    }


def _spawn_engine(*args, **kwargs):
    """Late-bound compatibility seam for patches on either this router or engine_proc."""
    return _engine_proc._spawn_engine(*args, **kwargs)


# `_defaults_backend_llm` used to live here and is now `serve/launch.py::_defaults_backend_llm`
# (doc 25 SR-12). It is launch policy with no HTTP dependency, and keeping it in a ROUTER meant
# `routers/genesis.py` imported a sibling router's private — route modules stopped being independent
# leaves. No re-export: this router does not call it, so a shim here would only re-create the
# coupling in the other direction. /api/start applies the rule through `launch.py::_resolve_settings`.


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, root = srv.run_dir, srv.root

    def _known_engine_liveness(rd: Path, operation: str) -> bool:
        """Return a real lock verdict; unknown ownership cannot authorize a mutation/Popen."""
        liveness = _engine_liveness(rd)
        if liveness is None:
            raise HTTPException(409, {
                "code": "engine_liveness_unknown",
                "message": f"Cannot {operation} because engine ownership is unknown.",
                "remediation": (
                    "Inspect engine.lock and storage locking, then retry only after liveness "
                    "is verifiable."),
                "retryable": True,
            })
        return liveness

    # _Closed 2026-09-23 (review SRV1-07, owner decision): the legacy `POST .../control` and
    # `POST .../resume` compatibility routes are retired. Both first-party clients had left them for
    # `POST .../commands`, no caller outside the suite was known, and `/control` could not be fixed
    # in place: with no durable request identity, a lost-response retry re-appended an ADDITIVE
    # intent. What they guarded lives on the command path — intake normalization
    # (`control_validation.py::normalize_control`), the active-command and pending-finalize
    # refusals, the launch-in-flight handshake — and a durable resume request met while its owner
    # is still in its tail is served by the after-exit waiter the startup scan and the reconcilers
    # install (`engine_proc.py::_claim_and_spawn_resume`)._

    # ------------------------------------------------------------------ authoritative command lifecycle
    def _command_response_headers(response: Response) -> None:
        # These records transition asynchronously. A browser/proxy cache of ``accepted`` would freeze
        # polling forever, and token-scoped deployments must never share one owner's record response.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization"

    @router.post(
        "/api/runs/{run_id}/commands",
        responses=_command_responses("Durable command record"),
        openapi_extra=_command_post_openapi(),
    )
    async def submit_command(run_id: str, request: Request, response: Response):
        _command_response_headers(response)
        rd = await anyio.to_thread.run_sync(_run_dir, run_id)
        body = await json_object(request, "command body")
        idem = request.headers.get("Idempotency-Key", "")
        # submit() takes the run flock and folds the log — offload so it never blocks the event loop.
        return await anyio.to_thread.run_sync(lambda: srv.commands.submit(
            rd, idem, body.get("type"), body.get("data"),
            expected_generation=body.get(EXPECTED_RUN_GENERATION_FIELD)))

    @router.get(
        "/api/runs/{run_id}/commands/{command_id}",
        responses=_command_responses("Current durable command record"),
    )
    def get_command(run_id: str, command_id: str, response: Response):
        _command_response_headers(response)
        return srv.commands.get(_run_dir(run_id), command_id)

    @router.post(
        "/api/runs/{run_id}/commands/{command_id}/retry",
        responses=_command_responses("Retried durable command record"),
    )
    def retry_command(run_id: str, command_id: str, response: Response):
        _command_response_headers(response)
        return srv.commands.retry(_run_dir(run_id), command_id)

    @router.post("/api/runs/{run_id}/resolve-activity-claims")
    async def resolve_activity_claims(run_id: str, request: Request, response: Response):
        """Guarded operator recovery for an ownership claim that cannot be proven dead."""
        _command_response_headers(response)
        body = await json_object(request, "resolve-activity-claims body")
        rd = await anyio.to_thread.run_sync(_run_dir, run_id)
        confirmation = str(body.get("confirmation") or "")
        return await anyio.to_thread.run_sync(
            lambda: srv.commands.resolve_active_claims(rd, confirmation))

    # ------------------------------------------------------------------ spawn
    @router.post("/api/runs/{run_id}/reset")
    async def reset_run(run_id: str, request: Request):
        """round-7 "Replay": reset a run IN PLACE — archive its event log + spans + node workspaces and
        re-spawn a fresh run on the same run-id. The prior artifacts are RENAMED (not deleted) so the
        history is recoverable."""
        return await durable_reset_run(srv, run_id, request, spawn_engine=_spawn_engine)

    @router.post("/api/runs/{run_id}/nodes/{nid}/clear_trace")
    def clear_node_trace(run_id: str, nid: int, body: Optional[dict[str, Any]] = None):
        """Erase ONE node's spans from spans.jsonl — the "clear this node's trace" button. spans.jsonl
        is append-only, so after a node_reset the rebuild would otherwise STACK its fresh bands on top
        of the old attempt's (build_conversation shows every trace tagged with the node). This removes
        the node's spans so only the next build's trace remains. REFUSED while the engine is live — it
        is the sole writer of spans.jsonl and rewriting the file under it would race/corrupt the trace;
        stop the run first. Non-destructive to the event log (events.jsonl, the source of truth, is
        untouched) — only the diagnostics trace is dropped."""
        return durable_clear_node_trace(
            srv, run_id, nid, body, known_engine_liveness=_known_engine_liveness)

    # THE DURABLE START RECORD'S PROTOCOL IS NOT A ROUTER'S BUSINESS (doc 25 SR-01). Seven closures
    # — the observational reconciliation, the keyed-replay inspection, the public projection, the
    # pre-Popen namespace release, the run_started evidence walk — captured `srv` and `root` and
    # nothing else, which made every branch of a crash-window state machine reachable only by
    # building the whole ASGI app and driving HTTP. They now live in `serve/start_record.py`, which
    # also STATES the protocol as a `StartRecordSpec` beside `paid_ledger.py`'s two event-ledger
    # specs (shared vocabulary, deliberately separate storage — see that module's docstring).

    @router.post("/api/start/{run_id}/resolve-claim")
    async def resolve_start_claim(run_id: str, request: Request, response: Response):
        """Operator recovery for a crash-window claim whose child identity cannot be proven."""
        _command_response_headers(response)
        body = await json_object(request, "resolve-claim body")
        confirmation = str(body.get("confirmation") or "")

        def _resolve():
            # `resolve()` walks the filesystem (a symlink per component), so it runs here, on the
            # worker, with the claim resolution — not on the loop every SSE stream shares.
            rd = (root / run_id).resolve()
            if (rd == root or rd.parent != root or rd.name.lower() in _RESERVED_RUN_IDS
                    or rd.name.lower().startswith(_RESET_RECEIPT_PREFIX)):
                raise HTTPException(400, "bad run_id")
            return srv.commands.resolve_spawn_claim(rd, confirmation)

        return await anyio.to_thread.run_sync(_resolve)

    @router.get("/api/start/{run_id}/status")
    def start_status(run_id: str, request: Request, response: Response,
                     idempotency_key: str | None = None):
        """Observe one exact durable startup. GET never launches or resumes an engine."""
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization, Idempotency-Key"
        raw_header_key = request.headers.get("Idempotency-Key")
        header_key = (validate_idempotency_key(raw_header_key)
                      if raw_header_key is not None else None)
        query_key = (validate_idempotency_key(idempotency_key)
                     if idempotency_key is not None else None)
        if (header_key is not None and query_key is not None
                and not secrets.compare_digest(
                    idempotency_key_digest(header_key), idempotency_key_digest(query_key))):
            raise HTTPException(400, {
                "code": "idempotency_key_mismatch",
                "message": "Idempotency-Key header and query parameter disagree",
                "field_errors": {"idempotency_key": "send one exact startup key"},
            })
        key = header_key if header_key is not None else query_key
        if key is None:
            raise HTTPException(400, {
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key header is required",
                "field_errors": {"idempotency_key": "send the startup observation key"},
            })
        rd = safe_run_dir(root, run_id, check_conflict=False)
        digest = idempotency_key_digest(key)
        with srv.commands.sequence(rd):
            record = srv.commands.load_start_record(rd)
            if record is None or not secrets.compare_digest(
                    str(record.get("idempotency_key_digest") or ""), digest):
                raise HTTPException(404, {
                    "code": "start_not_found",
                    "message": "no startup is recorded for this run name and idempotency key",
                })
            _record, public = reconcile_start(srv, rd, record)
        return public

    @router.post("/api/start/preflight")
    async def start_preflight(request: Request):
        """Validate and resolve a launch without writing, reserving a name, or starting an engine."""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, {
                "code": "invalid_launch_request",
                "message": "start body must be valid JSON",
                "field_errors": {},
            }) from exc
        return preflight_response(await anyio.to_thread.run_sync(lambda: preflight_start(srv, body)))

    @router.post("/api/validate")
    async def validate_run_launch(request: Request):
        """Is this launch proposal launchable, and if not, why — the same `preflight_start` funnel
        as `/api/start` and `/api/start/preflight`, answered as a 200 verdict (`launch.py::
        validate_launch`). The TUI asks it on every draft render and before every launch, which is
        what let its own `spec_ready` copy of the rules be deleted (doc 52 row 8). A body that is not
        JSON is not a proposal at all and gets the siblings' 400."""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, {
                "code": "invalid_launch_request",
                "message": "validate body must be valid JSON",
                "field_errors": {},
            }) from exc
        return await anyio.to_thread.run_sync(lambda: validate_launch(srv, body))

    @router.post("/api/start")
    async def start_run(request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, {
                "code": "invalid_launch_request", "message": "start body must be valid JSON",
                "field_errors": {},
            }) from exc
        if not isinstance(body, dict):
            raise HTTPException(400, {
                "code": "invalid_launch_request", "message": "start body must be a JSON object",
                "field_errors": {},
            })

        key = validate_idempotency_key(body.get("idempotency_key"))
        key_digest = idempotency_key_digest(key) if key else ""
        request_digest = launch_request_digest(body) if key else ""
        rd = await anyio.to_thread.run_sync(
            lambda: safe_run_dir(root, body.get("run_id"), check_conflict=False))

        # Lost-response replay is resolved before rereading mutable sources/defaults or rejecting the
        # now-owned run name. The request digest contains effects, never the raw idempotency key.
        # OFF the event loop, like the preflight two lines down already is. `sequence()` acquires a
        # cross-process flock that can wait the full lock_acquire_timeout (60s) under contention, and
        # the start-record read is file I/O. Run inline on this `async def` handler's loop, a
        # contended launch froze every SSE stream and poll on this worker for that whole wait.
        # `None` means "no replay to serve"; a response means the lost-response replay answered.
        def _replay_keyed_start():
            with srv.commands.sequence(rd):
                srv.commands._reject_unresolved_reset(rd, "replay this run start")
                record, public, same_key = inspect_keyed_start(
                    srv, rd, key_digest, request_digest)
                if record is not None:
                    if (same_key and public.get("paid_effect_unknown") is not True
                            and public["status"] in {"accepted", "executing", "succeeded"}):
                        return JSONResponse(public)
                    if same_key or public.get("can_retry") is not True:
                        raise_existing_start(public, same_key=same_key)
            return None

        if key:
            replayed = await anyio.to_thread.run_sync(_replay_keyed_start)
            if replayed is not None:
                return replayed

        plan = await anyio.to_thread.run_sync(lambda: preflight_start(srv, body))
        submitted_token = body.get("validation_token") or ""
        if key and not submitted_token:
            raise HTTPException(409, {
                "code": "launch_validation_required",
                "message": "validate this exact launch proposal before starting it",
                "field_errors": {"validation_token": "run the free preflight first"},
            })
        if submitted_token and submitted_token != plan.validation_token:
            raise HTTPException(409, {
                "code": "launch_validation_stale",
                "message": "the launch draft changed after it was validated",
                "field_errors": {"validation_token": "validate the current draft again"},
            })

        run_id = plan.run_id
        requested_rd = root / run_id
        task_file = rd / "task.input.json"
        # The canonical unified file carries every resolved setting. Keep the process environment to
        # actual deviations from this server's Settings baseline so profile/default provenance and
        # legacy non-generative launches are not turned into explicit overrides accidentally.
        base_settings = Settings().model_dump(mode="json")
        base_settings.pop("llm_api_key", None)
        base_settings.pop("llm_api_key_base_url", None)
        launch_settings = {
            setting: value for setting, value in plan.effective_settings.items()
            if base_settings.get(setting, object()) != value
        }

        # OFF the event loop too. Preparation publishes a durable PID-less spawn claim under the run
        # sequencer, then releases every run/filesystem lock before the settings launch fence is held
        # across Popen. The claim keeps duplicate starts fail-closed during that unlocked boundary.
        # `None` means "launched, fall through"; a response is the lost-response replay.
        launched: dict = {}

        def _launch():
            start_result = None
            record = None
            start_id = ""
            owner = ""
            lease_started = False
            materialization_created = False
            popen_boundary_entered = False
            expected_settings_revision: Optional[str] = None
            try:
                with srv.commands.sequence(rd):
                    srv.commands._reject_unresolved_reset(rd, "start a run with this id")
                    if key:
                        existing, public, same_key = inspect_keyed_start(
                            srv, rd, key_digest, request_digest)
                        if existing is not None:
                            if (same_key and public.get("paid_effect_unknown") is not True
                                    and public["status"] in {"accepted", "executing", "succeeded"}):
                                return JSONResponse(public)
                            if same_key or public.get("can_retry") is not True:
                                raise_existing_start(public, same_key=same_key)

                    # A crashed Replay can temporarily leave the direct run directory without
                    # events.jsonl. The durable marker still owns that namespace.
                    current_rd = requested_rd.resolve()
                    if requested_rd.is_symlink() or current_rd != rd or current_rd.parent != root:
                        raise HTTPException(409, {
                            "code": "run_path_changed",
                            "message": "run path changed while start was being prepared",
                            "field_errors": {"run_id": "choose a stable run name"},
                        })
                    # Bind the validated settings bytes to an opaque UI revision while that resource
                    # is locked. launch_env checks the same revision immediately before Popen.
                    with srv.settings.ui_settings_transaction():
                        current_token = plan.current_token(srv)
                        expected_settings_revision = srv.settings.ui_settings_revision()
                    if current_token != plan.validation_token:
                        raise HTTPException(409, {
                            "code": "launch_validation_changed",
                            "message": (
                                "task, settings, run name, chat, or a referenced path changed "
                                "before launch"),
                            "field_errors": {},
                            "remediation": "Run preflight again and review the updated launch preview.",
                        })
                    if (rd / "events.jsonl").exists():
                        raise HTTPException(409, {
                            "code": "run_id_conflict", "message": f"run {run_id!r} already exists",
                            "field_errors": {"run_id": "choose another run name"},
                        })
                    known_alive = _known_engine_liveness(rd, "start the run")
                    if known_alive or _engine_alive(rd):
                        raise HTTPException(409, {
                            "code": "external_start_in_progress" if key else "start_in_progress",
                            "message": f"run {run_id!r} already has an engine starting",
                        })
                    if srv.commands.spawn_inflight(rd):
                        raise HTTPException(409, {
                            "code": "external_start_uncertain" if key else "start_uncertain",
                            "message": f"run {run_id!r} already has an unresolved startup",
                            "remediation": "Observe or explicitly resolve the spawn claim; do not retry.",
                        })

                    start_id = f"start_{secrets.token_hex(16)}" if key else ""
                    created_at = time.time()
                    if key:
                        record = {
                            "version": 1, "id": start_id, "run_id": run_id,
                            "idempotency_key_digest": key_digest, "request_digest": request_digest,
                            "validation_token": plan.validation_token,
                            "status": "preparing", "phase": "reserved",
                            "paid_effect_unknown": False,
                            "created_at": created_at, "updated_at": created_at,
                        }
                        srv.commands.save_start_record(rd, record)

                    owner = f"start:{start_id}" if key else "start"
                    try:
                        # Close the check-to-create race: only this exact reservation may create the
                        # run directory, and no pre-existing directory may be materialized into.
                        rd.mkdir(parents=False, exist_ok=False)
                        materialization_created = True
                    except FileExistsError as exc:
                        raise HTTPException(409, {
                            "code": "run_id_conflict",
                            "message": f"run {run_id!r} already exists",
                            "field_errors": {"run_id": "choose another run name"},
                        }) from exc
                    atomic_write_text(task_file, json.dumps(plan.canonical_document, indent=2))
                    meta = {"task_file": str(task_file)}
                    if plan.source_task_file:
                        meta["source_task_file"] = plan.source_task_file
                    if key:
                        meta["start_id"] = start_id
                    atomic_write_text(rd / "ui_meta.json", json.dumps(meta, indent=2))

                    chat_path = rd / "chat.jsonl"
                    if plan.seed_chat:
                        chat_bytes = b"".join(orjson.dumps({
                            "role": turn["role"], "content": turn["content"],
                            "ts": created_at + i * 1e-3, "seq": GENESIS_CHAT_SEQ_BASE + i,
                            "genesis": True,
                        }) + b"\n" for i, turn in enumerate(plan.seed_chat))
                        atomic_write_bytes(chat_path, chat_bytes)
                    elif chat_path.exists():
                        atomic_write_bytes(chat_path, b"")
                    if record is not None:
                        record.update(phase="materialized", updated_at=time.time())
                        srv.commands.save_start_record(rd, record)

                    srv.commands.begin_external_spawn(rd, owner)
                    lease_started = True
                    if record is not None:
                        # After this durable phase, crash-before-call and crash-after-Popen are
                        # indistinguishable. The PID-less claim therefore remains fail-closed.
                        record.update(status="executing", phase="popen_pending",
                                      paid_effect_unknown=True, updated_at=time.time())
                        srv.commands.save_start_record(rd, record)

                assert expected_settings_revision is not None
                # No run sequencer or settings-file lock crosses Popen. launch_env retains only the
                # dedicated publication fence, so a completed clear/rotation cannot be overtaken by
                # a child carrying the prior credential. Re-load the exact materialized task so the
                # parent applies the same task-aware consumer plan as the child before accepting a
                # detached process that would only die during its own startup gate.
                from looplab.adapters.tasks import load_task
                launch_task = load_task(task_file)
                with srv.settings.launch_env(
                        launch_settings,
                        expected_settings_revision=expected_settings_revision,
                        task=launch_task) as env:
                    # From this assignment onward, an exception cannot prove whether the helper failed
                    # before or after the OS accepted Popen. Retain the claim and report uncertainty.
                    popen_boundary_entered = True
                    # The operator's explicit launch settings travel as NAMES (their values are
                    # already in the materialized file): `run_started` records them, so an explicitly
                    # launched width is an operator pin (`cli/run_cmds.py::_explicit_setting_names`).
                    pid = _spawn_engine(
                        ["run", str(task_file), "--out", str(rd),
                         *(arg for name in plan.explicit_settings
                           for arg in ("--explicit-setting", name))],
                        env=env, run_dir=rd)

                with srv.commands.sequence(rd):
                    srv.commands.record_external_spawn(rd, owner, pid)
                    if record is not None:
                        record.update(status="accepted", phase="popen_returned",
                                      paid_effect_unknown=False, updated_at=time.time())
                        srv.commands.save_start_record(rd, record)
                        # Fold immediately available positive evidence into the response: a known-live
                        # PID becomes executing and a durable run_started becomes succeeded. PID-less or
                        # uncorrelated evidence becomes uncertain, so clients never navigate on Popen alone.
                        record, start_result = reconcile_start(srv, rd, record)
            except BaseException as exc:
                exposed_exc: BaseException = exc
                if isinstance(exc, SettingsRevisionConflict):
                    exposed_exc = HTTPException(409, {
                        "code": "launch_settings_revision_changed",
                        "message": "Settings changed after launch validation and before process start.",
                        "expected_settings_revision": exc.expected,
                        "current_settings_revision": exc.current,
                        "remediation": "Run preflight again and review the updated launch preview.",
                        })
                elif isinstance(exc, LLMError) and not popen_boundary_entered:
                    exposed_exc = HTTPException(409, {
                        "code": "launch_credentials_invalid",
                        "message": "Current credentials cannot authorize this task's LLM consumers.",
                        "field_errors": {"settings": str(exc)},
                        "remediation": "Fix the bound credential/profile, then run preflight again.",
                    })
                elif isinstance(exc, EventStoreLockError):
                    exposed_exc = HTTPException(503, {
                        "code": ("launch_outcome_unknown" if popen_boundary_entered
                                 else "launch_settings_fence_unavailable"),
                        "message": (
                            "The process-launch outcome could not be recorded safely."
                            if popen_boundary_entered else
                            "Settings launch locking is unavailable; no process was started."),
                        "remediation": (
                            "Observe the existing spawn claim; do not launch a duplicate."
                            if popen_boundary_entered else
                            "Inspect settings storage locking, then retry this launch."),
                    })
                try:
                    with srv.commands.sequence(rd):
                        # Clear ownership only while Popen was definitely never entered. Once entered,
                        # the PID-less claim is intentionally retained as durable uncertainty.
                        if lease_started and not popen_boundary_entered:
                            srv.commands.cancel_external_spawn(rd, owner)
                        if record is not None:
                            detail = getattr(exposed_exc, "detail", None)
                            code = (str(detail.get("code"))
                                    if isinstance(detail, dict) and detail.get("code")
                                    else "spawn_failed" if record.get("phase") == "popen_pending"
                                    else "start_materialization_failed")
                            record.update(
                                status="uncertain" if popen_boundary_entered else "failed",
                                phase=("failed_after_spawn" if popen_boundary_entered
                                       else "failed_before_spawn"),
                                error_code=code, paid_effect_unknown=popen_boundary_entered,
                                # Publish the pre-Popen fact before removing its directory. If this
                                # process dies during cleanup, reconcile_start can finish it safely.
                                namespace_released=False,
                                updated_at=time.time(),
                            )
                            srv.commands.save_start_record(rd, record)
                        if materialization_created and not popen_boundary_entered:
                            namespace_released = release_unspawned_start_namespace(
                                srv, rd, start_id=start_id, task_file=task_file)
                            if record is not None and namespace_released:
                                record.update(namespace_released=True, updated_at=time.time())
                                srv.commands.save_start_record(rd, record)
                except Exception:  # noqa: BLE001 - retain the original failure and fail-closed claim
                    pass
                if exposed_exc is exc:
                    raise
                raise exposed_exc from exc
            launched["start_result"] = start_result
            return None

        replayed = await anyio.to_thread.run_sync(_launch)
        if replayed is not None:
            return replayed
        start_result = launched["start_result"]

        if start_result is not None:
            return start_result
        return {"ok": True, "run_id": run_id, "validation_token": plan.validation_token}

    return router
