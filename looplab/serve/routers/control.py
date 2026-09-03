"""Control-plane routes: append control intents (/control) and spawn/resume/reset/start engine
processes. Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import threading
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
from looplab.events.eventstore import (
    MAX_EVENT_BATCH_BYTES, EventStore, EventStoreConcurrencyError, EventStoreLockError,
    decode_event_record)
from looplab.events.replay import fold
from looplab.events.types import EV_APPROVAL_GRANTED, EV_RESUME_REQUESTED, EV_SPEC_APPROVED
from looplab.serve.appstate import _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX
from looplab.serve.http import json_object
from looplab.serve.engine_proc import (
    EngineSpawnOutcomeUnknown, _claim_and_spawn_resume, _engine_alive, _engine_liveness,
    _resolve_task_file, run_lifecycle_lock_http)
from looplab.serve.launch import (
    idempotency_key_digest,
    launch_request_digest,
    preflight_response,
    preflight_start,
    safe_run_dir,
    validate_idempotency_key,
)
from looplab.serve.protocol import (
    COLLABORATION_EVENTS, CONTROL_EVENTS, EXPECTED_RUN_GENERATION_FIELD, GENESIS_CHAT_SEQ_BASE)
from looplab.serve.control_validation import normalize_control
from looplab.serve.reset_route import durable_reset_run
from looplab.serve.settings_store import SettingsRevisionConflict
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


# WHO STILL CALLS THE LEGACY `/control` ROUTE — a process-local tally, so the port to `/commands`
# is a number rather than an intention. The route's own comment has said since it was written that
# it needs "a deprecation window with a warning header and a migration note"; nothing counted, so
# nobody could say how far along that was or whether anything outside the test suite still spoke it.
#
# `{event_type: {user_agent: count}}`. The User-Agent is what separates the suite's own httpx client
# from a real deployment's browser or script — the whole question the port turns on — and it is a
# header the caller volunteers about itself, not an identity, so it discloses nothing about who is
# operating. Truncated and bounded because it is untrusted input on a hot path.
#
# NOT AN EVENT, deliberately. This measures the SERVER's clients over its lifetime, not a run's
# history, and a durable row per legacy call would put that history into the very log this route is
# criticised for appending to unfenced.
_LEGACY_CONTROL_MAX_AGENTS = 32
_LEGACY_CONTROL_AGENT_CHARS = 120
_legacy_control_callers: dict[str, dict[str, int]] = {}
_legacy_control_lock = threading.Lock()


def _note_legacy_control_caller(event_type: str, user_agent: str) -> None:
    """Record one SUCCESSFUL legacy control append. Never raises."""
    agent = (user_agent or "unknown").strip()[:_LEGACY_CONTROL_AGENT_CHARS] or "unknown"
    with _legacy_control_lock:
        agents = _legacy_control_callers.setdefault(event_type or "unknown", {})
        if agent not in agents and len(agents) >= _LEGACY_CONTROL_MAX_AGENTS:
            # A caller that varies its User-Agent per request must not grow this map without bound.
            # The overflow bucket keeps the COUNT honest while dropping the distinction.
            agent = "(other)"
        agents[agent] = agents.get(agent, 0) + 1


def legacy_control_callers() -> dict[str, dict[str, int]]:
    """A copy of the tally, for an operator or a test asking who has not migrated yet."""
    with _legacy_control_lock:
        return {etype: dict(agents) for etype, agents in _legacy_control_callers.items()}


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

    # ------------------------------------------------------------------ control
    # KNOWN GAP (needs a deprecation, not a patch): this compatibility route has no durable request
    # identity and no mandatory generation fence, so a lost-response retry re-appends an ADDITIVE
    # intent — `budget_extend`'s `add_nodes` is a documented delta, and inject/fork/deep_research each
    # queue another PAID unit of work. `/commands` is the fenced path and is what both first-party
    # clients use (ui/src/api.js, tui_api.py). Requiring `expected_seq` for those types was tried and
    # reverted: it is the correct end state but breaks the contract this route exists to preserve
    # (41 call sites in the suite alone append here unfenced), so it needs a deprecation window with a
    # warning header and a migration note — not a silent 409.
    # OPEN[legacy-control-route-is-not-retired] the route still exists and the suite still speaks
    # it unfenced, which is the reason a silent 409 is not the fix. It now ANNOUNCES its
    # deprecation (headers below) and COUNTS its callers (`legacy_control_callers`), so the port to
    # `/commands` is schedulable and its progress readable; what is open is doing it and deleting
    # the route.
    # proof:`present:async def control(@looplab/serve/routers/control.py`
    @router.post("/api/runs/{run_id}/control")
    async def control(run_id: str, request: Request, response: Response):
        rd = _run_dir(run_id)
        body = await json_object(request, "control body")
        # ANNOUNCED, not silently tolerated. A caller cannot discover a deprecation it is never told
        # about, and the paragraph above had been the entire notice — in a comment, where no client
        # can read it. `Deprecation: true` is the boolean form of the deprecation header field, and
        # `Link; rel="successor-version"` is what names the replacement (RFC 8288).
        #
        # THERE IS DELIBERATELY NO `Sunset`. RFC 8594's field carries a DATE, nobody has committed to
        # one, and emitting an invented date would be a schedule this project has not agreed to —
        # the same reason `DECLINED[…]` markers here must carry a number rather than a plausible
        # sentence. The header pair is `Deprecation` + `Link` until a removal date is actually
        # decided; adding `Sunset` then is one line.
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = (
            f'</api/runs/{run_id}/commands>; rel="successor-version"')
        response.headers["Warning"] = (
            '299 - "This endpoint has no durable request identity: a lost-response retry '
            're-appends an ADDITIVE intent. Use POST /commands with an Idempotency-Key."')

        def _append_control() -> dict:
            # Offloaded to a worker thread: ``sequence`` takes the cross-process flock (blocking up to
            # ``lock_acquire_timeout``) and the append does disk I/O — holding that on the ASGI event
            # loop freezes every concurrent SSE/poll in the worker. Same offload the start/preflight
            # handlers already use.
            with srv.commands.sequence(rd):
                local_rd = srv.commands.validate_paths(rd)
                srv.commands.reject_if_active(local_rd, "append a legacy control event")
                etype = body.get("type")
                if etype not in CONTROL_EVENTS:
                    raise HTTPException(400, f"unknown control event: {etype!r}")
                if etype in COLLABORATION_EVENTS:
                    raise HTTPException(409, {
                        "code": "command_protocol_required",
                        "message": "versioned comments require the durable command endpoint",
                        "remediation": (
                            "submit with Idempotency-Key and the exact expected run generation"),
                    })
                # Approval decisions are valid only for the exact gate the normalizer folded. If the
                # caller omitted an explicit CAS, bind the append to that pre-normalization tail so a
                # replacement approval request cannot be accepted by this legacy endpoint.
                gated_baseline = None
                if etype in {EV_APPROVAL_GRANTED, EV_SPEC_APPROVED}:
                    events = srv.events(local_rd)
                    gated_baseline = events[-1].seq if events else -1
                # One shared normalizer owns strict payload validation plus node-attempt and parent
                # generation CAS. Pass the raw data intact so attempt>0 tokens are never erased here.
                data = normalize_control(srv, local_rd, etype, body.get("data"))
                _known_engine_liveness(local_rd, "append a control event")
                expected = body.get("expected_seq")
                if expected is None and gated_baseline is not None:
                    expected = gated_baseline
                # Require an actual JSON integer. This is a compare-and-swap token naming the EXACT tail
                # the caller observed, so coercion defeats its purpose: `int(7.9)` silently truncates to
                # 7 and `int("7")` accepts a string, either of which would fence the append against a
                # tail the caller never named — authorizing a mutation on a state it did not see.
                # (`bool` is an `int` subclass in Python, hence the explicit reject.)
                if expected is not None:
                    if isinstance(expected, bool) or not isinstance(expected, int):
                        raise HTTPException(400, "expected_seq must be an integer")
                try:
                    ev = EventStore(local_rd / "events.jsonl").append(
                        etype, data, expected_last_seq=expected)
                except EventStoreConcurrencyError as exc:
                    raise HTTPException(409, str(exc)) from exc
            return {"ok": True, "seq": ev.seq, "type": etype}

        result = await anyio.to_thread.run_sync(_append_control)
        # WHO STILL CALLS THIS. The comment above says the port needs "a deprecation window with a
        # warning header and a migration note" and could not say how far along it is, because
        # nothing counted. Recorded only for an append that SUCCEEDED: a 400/409 is a caller the
        # route refused, and counting it as a migration blocker would inflate the number the port is
        # tracked against. Process-local and deliberately not an event — this measures the SERVER's
        # clients, not a run's history, and a durable row per legacy call would put that history in
        # the log the route is criticised for appending to.
        _note_legacy_control_caller(str(result.get("type") or ""),
                                    request.headers.get("User-Agent", ""))
        return result

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
        rd = _run_dir(run_id)
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
        rd = _run_dir(run_id)
        confirmation = str(body.get("confirmation") or "")
        return await anyio.to_thread.run_sync(
            lambda: srv.commands.resolve_active_claims(rd, confirmation))

    # ------------------------------------------------------------------ spawn / resume
    def _task_file_for(rd: Path) -> Optional[str]:
        # The resolved immutable snapshot is authoritative. The shared helper tolerates malformed
        # legacy ui_meta and only accepts its task_file when no snapshot exists and the target exists.
        return _resolve_task_file(rd)

    def _append_resume_request(rd: Path) -> str:
        """Classify and durably append one handoff against the exact folded tail."""
        store = EventStore(rd / "events.jsonl")
        for _attempt in range(8):
            events = store.read_all()
            state = fold(events)
            last_seq = events[-1].seq if events else -1
            last_stop = state.last_stop_request_seq
            last_finish = state.last_finish_seq
            mode = ("finalize" if state.stop_requested and last_stop > last_finish else "resume")
            try:
                store.append(EV_RESUME_REQUESTED, {"mode": mode}, expected_last_seq=last_seq)
                return mode
            except EventStoreConcurrencyError:
                continue
        raise HTTPException(409, "run state changed repeatedly; retry resume")

    @router.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        rd = _run_dir(run_id)
        # The command sequencer excludes authoritative command workers while the lifecycle lock
        # serializes this durable handoff with reset/delete and the resume reconciler.
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            srv.commands.reject_if_active(
                rd, "resume through the legacy endpoint", allow_incomplete_finalize=True)
            task_file = _task_file_for(rd)
            if not task_file:
                raise HTTPException(
                    400, "run is not resumable — no task.snapshot.json or ui_meta.json "
                         "(it predates self-describing runs; start it via the UI to enable resume)")
            with run_lifecycle_lock_http(rd):
                known_alive = _known_engine_liveness(rd, "resume the run")
                # Durable before every liveness branch: a current owner in its final tail, or a
                # detached child that dies before engine.lock, leaves a recoverable intent.
                mode = _append_resume_request(rd)
            cli_args = (
                ["finalize", str(rd), "--task-file", str(task_file)]
                if mode == "finalize"
                else ["resume", str(rd), "--task-file", str(task_file)])
            # Preserve the historical monkeypatch seam while the production verdict remains the
            # exact tri-state result captured under the lifecycle fence.
            was_alive = known_alive or _engine_alive(rd)
            # Mirror the launch into the command service's pre-lock lease so command-aware callers
            # also fail closed during Popen→engine.lock. The event-log claim stays authoritative.
            srv.commands.begin_external_spawn(rd, "legacy-resume")
            popen_returned = False

            def _record_spawn(pid: Optional[int]) -> None:
                nonlocal popen_returned
                # Mark the Popen boundary before persisting the PID. If persistence fails, the child
                # may be live and the PID-less preclaim must remain as duplicate-spawn quarantine.
                popen_returned = True
                srv.commands.record_external_spawn(rd, "legacy-resume", pid)

            try:
                spawned = _claim_and_spawn_resume(
                    rd, cli_args, cancel_event=srv.resume_cancel, wait_on_alive=True,
                    spawn_engine=_spawn_engine, on_spawn=_record_spawn,
                    launch_env=lambda: srv.settings.launch_env_for_run(rd))
            except BaseException as exc:
                if not popen_returned and not isinstance(exc, EngineSpawnOutcomeUnknown):
                    srv.commands.cancel_external_spawn(rd, "legacy-resume")
                raise
            if not spawned:
                # A live owner/post-exit waiter is fenced by the durable resume claim instead.
                srv.commands.cancel_external_spawn(rd, "legacy-resume")
            if was_alive and not spawned:
                return {"ok": True, "already_running": True, "resume_after_exit": True}
            return {"ok": True, "launch_pending": not spawned}

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

    def _start_public(record: dict) -> dict:
        status = str(record.get("status") or "uncertain")
        # ``accepted`` proves only that Popen returned and its ownership evidence was persisted.  The
        # child is positively started only once its exact PID generation, engine lock, or run_started
        # event is observed.  Likewise, never advertise retry while a paid effect may have escaped.
        started = status in {"executing", "succeeded"}
        paid_effect_unknown = bool(record.get("paid_effect_unknown"))
        can_retry = (status in {"not_started", "failed"} and not paid_effect_unknown
                     and record.get("namespace_released") is not False)
        result = {
            "ok": status in {"accepted", "executing", "succeeded"},
            "run_id": str(record.get("run_id") or ""),
            "start_id": str(record.get("id") or ""),
            "status": status,
            "started": started,
            "can_retry": can_retry,
            "paid_effect_unknown": paid_effect_unknown,
        }
        if record.get("validation_token"):
            result["validation_token"] = str(record["validation_token"])
        if record.get("error_code"):
            result["error"] = {"code": str(record["error_code"])}
        return result

    def _start_meta_id(rd: Path) -> str:
        path = rd / "ui_meta.json"
        if path.is_symlink():
            return ""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return ""
        return str(value.get("start_id") or "") if isinstance(value, dict) else ""

    def _release_unspawned_start_namespace(
            rd: Path, *, start_id: str, task_file: Path) -> bool:
        """Remove only this request's pristine materialization before the Popen boundary.

        The caller holds ``commands.sequence(rd)`` and has already retired its exact PID-less claim.
        Any unexpected/reparse entry leaves the namespace intact and therefore fail-closed; the
        root-side start record remains as the durable audit receipt either way.
        """
        try:
            run_info = rd.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        attributes = int(getattr(run_info, "st_file_attributes", 0) or 0)
        try:
            invalid_run = (
                stat.S_ISLNK(run_info.st_mode) or not stat.S_ISDIR(run_info.st_mode)
                or bool(attributes & reparse_flag) or rd.resolve() != rd
                or rd.parent != root)
        except OSError:
            return False
        if invalid_run:
            return False
        try:
            entries = list(rd.iterdir())
        except OSError:
            return False
        allowed = {"task.input.json", "ui_meta.json", "chat.jsonl"}
        if any(entry.name not in allowed for entry in entries):
            return False

        meta = rd / "ui_meta.json"
        if meta in entries:
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                return False
            if (not isinstance(payload, dict)
                    or str(payload.get("task_file") or "") != str(task_file)
                    or (start_id and str(payload.get("start_id") or "") != start_id)
                    or (not start_id and payload.get("start_id"))):
                return False

        for entry in entries:
            try:
                info = entry.lstat()
                entry_attributes = int(getattr(info, "st_file_attributes", 0) or 0)
                if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                        or bool(entry_attributes & reparse_flag)
                        or entry.resolve().parent != rd):
                    return False
            except OSError:
                return False
        try:
            for entry in entries:
                entry.unlink()
            rd.rmdir()
        except OSError:
            return False
        return True

    def _has_first_run_started(rd: Path) -> bool:
        """Whether the first identity event is a durable, correlated ``run_started``.

        Current engines durably emit ``setup_started``/``setup_step`` immediately before their
        identity anchor; older valid engines emitted ``run_started`` at sequence zero.  Accept both
        layouts, but fail closed on a torn line, a malformed/unsupported envelope, a sequence gap,
        an unrelated pre-identity event, or a run id that does not name this exact directory.  A
        merely parseable ``{"type": "run_started"}`` is not process evidence.
        """
        path = rd / "events.jsonl"
        if path.is_symlink():
            return False
        try:
            with path.open("rb") as stream:
                expected_seq = 0
                total_bytes = 0
                for _ in range(4096):
                    raw = stream.readline(MAX_EVENT_BATCH_BYTES + 1)
                    if not raw:
                        return False
                    total_bytes += len(raw)
                    if (len(raw) > MAX_EVENT_BATCH_BYTES
                            or total_bytes > 2 * MAX_EVENT_BATCH_BYTES
                            or not raw.endswith(b"\n") or not raw.strip()):
                        return False
                    physical = orjson.loads(raw)
                    if (not isinstance(physical, dict)
                            or not {"v", "seq", "ts", "type", "data"} <= set(physical)):
                        return False
                    for event in decode_event_record(physical, strict=True):
                        version = event.v
                        seq = event.seq
                        ts = event.ts
                        event_type = event.type
                        data = event.data
                        if (type(version) is not int or version != 1
                                or type(seq) is not int or seq != expected_seq
                                or isinstance(ts, bool) or not isinstance(ts, (int, float))
                                or not math.isfinite(ts) or ts <= 0
                                or not isinstance(event_type, str)
                                or not isinstance(data, dict)):
                            return False
                        expected_seq += 1
                        if event_type == "run_started":
                            run_id = data.get("run_id")
                            return isinstance(run_id, str) and run_id == rd.name
                        if event_type not in {"setup_started", "setup_step"}:
                            return False
                return False
        except (OSError, ValueError, TypeError, orjson.JSONDecodeError):
            return False

    def _reconcile_start(rd: Path, record: dict) -> tuple[dict, dict]:
        """Fold durable run/claim evidence into one observational startup state.

        Callers hold ``commands.sequence(rd)``. This function may retire an observed/dead spawn
        claim and finish an explicitly recorded pre-Popen namespace cleanup, but never creates a
        directory, lease, event, or process.
        """
        updated = dict(record)
        start_id = str(updated.get("id") or "")
        meta_matches = _start_meta_id(rd) == start_id
        liveness = _engine_liveness(rd)

        def transition(**changes) -> None:
            # Stable polling must be observational: publish a new timestamp only for an actual state
            # transition, not on every GET of the same evidence.
            if any(updated.get(key) != value for key, value in changes.items()):
                updated.update(changes)
                updated["updated_at"] = time.time()

        if (updated.get("status") == "failed"
                and updated.get("phase") == "failed_before_spawn"
                and updated.get("paid_effect_unknown") is False
                and updated.get("namespace_released") is False):
            evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
            if evidence in {"absent", "dead_or_cleared"} and liveness is False:
                released = _release_unspawned_start_namespace(
                    rd, start_id=start_id, task_file=rd / "task.input.json")
                if released:
                    transition(namespace_released=True)

        if meta_matches and _has_first_run_started(rd):
            transition(status="succeeded", phase="event_observed", paid_effect_unknown=False,
                       error_code=None)
        elif meta_matches and (liveness is True
                               or (liveness is False and _engine_alive(rd))):
            transition(status="executing", phase="engine_observed", paid_effect_unknown=False,
                       error_code=None)
        elif str(updated.get("phase") or "") in {
                "popen_pending", "popen_returned", "engine_observed"}:
            evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
            # A start_id in ui_meta is the durable correlation between this sidecar and this run
            # directory.  An engine lock without it may belong to a manually replaced incarnation.
            if meta_matches and evidence in {"live", "pending_known"}:
                transition(status="executing", paid_effect_unknown=False, error_code=None)
            elif not meta_matches or evidence in {"uncertain", "mismatched"}:
                transition(status="uncertain", paid_effect_unknown=True,
                           error_code="start_uncertain")
            else:
                # Popen may already have crossed the provider boundary before dying. A new explicit
                # launch is possible only after review/revalidation; never call it automatically.
                transition(status="failed", phase="failed_after_spawn",
                           paid_effect_unknown=True, error_code="start_failed_after_spawn")
        elif str(updated.get("phase") or "") in {"reserved", "materialized"}:
            evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
            if evidence in {"absent", "dead_or_cleared"}:
                transition(status="not_started", paid_effect_unknown=False, error_code=None)
            else:
                transition(status="uncertain", paid_effect_unknown=True,
                           error_code="start_uncertain")
        if updated != record:
            srv.commands.save_start_record(rd, updated)
        return updated, _start_public(updated)

    def _inspect_keyed_start(rd: Path, key_digest: str, request_digest: str):
        record = srv.commands.load_start_record(rd)
        if record is None:
            return None, None, False
        same_key = secrets.compare_digest(
            str(record.get("idempotency_key_digest") or ""), key_digest)
        if same_key and not secrets.compare_digest(
                str(record.get("request_digest") or ""), request_digest):
            raise HTTPException(409, {
                "code": "idempotency_key_reused",
                "message": "this idempotency key belongs to a different launch request",
                "field_errors": {"idempotency_key": "generate a new key for the edited proposal"},
            })
        reconciled, public = _reconcile_start(rd, record)
        return reconciled, public, same_key

    def _raise_existing_start(public: dict, *, same_key: bool) -> None:
        status = str(public.get("status") or "uncertain")
        if not same_key:
            raise HTTPException(409, {
                "code": "run_id_conflict",
                "message": "this run name is already owned by another startup",
                "start_id": public.get("start_id"),
                "field_errors": {"run_id": "choose another run name"},
                "remediation": "Use the card that owns the existing startup, or choose another name.",
            })
        if status == "uncertain" or public.get("paid_effect_unknown") is True:
            raise HTTPException(409, {
                "code": "start_uncertain",
                "message": "the earlier startup may have crossed Popen; observe it before retrying",
                "start_id": public.get("start_id"),
                "status": status,
                "paid_effect_unknown": bool(public.get("paid_effect_unknown")),
                "remediation": "Use the startup status endpoint; do not submit another launch.",
            })
        if status in {"accepted", "executing", "succeeded"}:
            return
        if same_key:
            raise HTTPException(409, {
                "code": "start_not_completed",
                "message": "this startup did not establish a run",
                "start_id": public.get("start_id"),
                "remediation": "Review provider/error evidence, then validate again before a new launch.",
            })

    @router.post("/api/start/{run_id}/resolve-claim")
    async def resolve_start_claim(run_id: str, request: Request, response: Response):
        """Operator recovery for a crash-window claim whose child identity cannot be proven."""
        _command_response_headers(response)
        body = await json_object(request, "resolve-claim body")
        rd = (root / run_id).resolve()
        if (rd == root or rd.parent != root or rd.name.lower() in _RESERVED_RUN_IDS
                or rd.name.lower().startswith(_RESET_RECEIPT_PREFIX)):
            raise HTTPException(400, "bad run_id")
        confirmation = str(body.get("confirmation") or "")
        return await anyio.to_thread.run_sync(
            lambda: srv.commands.resolve_spawn_claim(rd, confirmation))

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
            _record, public = _reconcile_start(rd, record)
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
        rd = safe_run_dir(root, body.get("run_id"), check_conflict=False)

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
                record, public, same_key = _inspect_keyed_start(rd, key_digest, request_digest)
                if record is not None:
                    if (same_key and public.get("paid_effect_unknown") is not True
                            and public["status"] in {"accepted", "executing", "succeeded"}):
                        return JSONResponse(public)
                    if same_key or public.get("can_retry") is not True:
                        _raise_existing_start(public, same_key=same_key)
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
                        existing, public, same_key = _inspect_keyed_start(
                            rd, key_digest, request_digest)
                        if existing is not None:
                            if (same_key and public.get("paid_effect_unknown") is not True
                                    and public["status"] in {"accepted", "executing", "succeeded"}):
                                return JSONResponse(public)
                            if same_key or public.get("can_retry") is not True:
                                _raise_existing_start(public, same_key=same_key)

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
                    pid = _spawn_engine(
                        ["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)

                with srv.commands.sequence(rd):
                    srv.commands.record_external_spawn(rd, owner, pid)
                    if record is not None:
                        record.update(status="accepted", phase="popen_returned",
                                      paid_effect_unknown=False, updated_at=time.time())
                        srv.commands.save_start_record(rd, record)
                        # Fold immediately available positive evidence into the response: a known-live
                        # PID becomes executing and a durable run_started becomes succeeded. PID-less or
                        # uncorrelated evidence becomes uncertain, so clients never navigate on Popen alone.
                        record, start_result = _reconcile_start(rd, record)
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
                                # process dies during cleanup, _reconcile_start can finish it safely.
                                namespace_released=False,
                                updated_at=time.time(),
                            )
                            srv.commands.save_start_record(rd, record)
                        if materialization_created and not popen_boundary_entered:
                            namespace_released = _release_unspawned_start_namespace(
                                rd, start_id=start_id, task_file=task_file)
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
