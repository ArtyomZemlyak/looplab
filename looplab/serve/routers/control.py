"""Control-plane routes: append control intents (/control) and spawn/resume/reset/start engine
processes. Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from looplab.serve import engine_proc as _engine_proc
from looplab.core.atomicio import (
    atomic_write_bytes, atomic_write_text, strict_atomic_write_bytes,
    strict_atomic_write_text)
from looplab.core.config import Settings
from looplab.events.eventstore import (
    MAX_EVENT_BATCH_BYTES, EventStore, EventStoreConcurrencyError, decode_event_record)
from looplab.events.replay import fold
from looplab.events.traceview import trace_file_revision
from looplab.events.types import EV_APPROVAL_GRANTED, EV_RESUME_REQUESTED, EV_SPEC_APPROVED
from looplab.serve.appstate import (
    _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX, _TRACE_CLEAR_RECEIPT_PREFIX)
from looplab.serve.engine_proc import (
    _claim_and_spawn_resume, _engine_alive, _engine_liveness,
    _fresh_resume_launch_pending,
    _resolve_task_file, engine_write_lock_http, run_lifecycle_lock_http)
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
from looplab.serve.run_commands import normalize_control
from looplab.serve.reset_route import durable_reset_run


_TRACE_CLEAR_OPERATION_RE = re.compile(r"^tc_[0-9a-f]{32}$")


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


def _defaults_backend_llm(task_spec: Optional[dict], task_file: Optional[str],
                          settings: dict, ui_settings: dict) -> bool:
    """True when a launch should default `backend="llm"`: the task normalizes to a GENERATIVE kind
    (the agent writes/edits code) and nobody chose a backend. CLI parity (mega-review P10):
    `looplab run --goal` already defaults backend=llm for these kinds (cli.py's `backend_chosen`
    rule), but Settings.backend defaults to "toy" — a repo/dataset run launched over HTTP without
    this got NoOpRepoDeveloper and every node silently re-evaluated the unchanged baseline (no
    error, just a flat run). Shared by /api/start (authoritative — the one funnel every launch goes
    through) and the genesis card (display-only, so the operator can see/override it pre-launch).
    "Chosen" = a `backend` key already in the merged launch/card `settings`, or one the deployment
    set — a UI-saved value, LOOPLAB_BACKEND env, or a `.env` line all land in
    `Settings(**ui).model_fields_set`, the same test cli.py's `backend_chosen` uses (and
    `_spawn_engine` overlays our env ON TOP of os.environ, so injecting would clobber it). Only that
    surface-specific "chosen" detection lives here; the kind→backend rule itself is
    `engine/genesis.py::default_backend`, shared with cli.py's genesis defaulting."""
    if "backend" in settings:
        return False
    file_settings: dict = {}
    if not (isinstance(task_spec, dict) and task_spec):
        if not task_file:
            return False
        # A catalogue/snapshot launch: the task lives only in the file — read it with the SAME
        # loader the spawned engine uses (cli.py `run` → appconfig.load_document): it handles a
        # YAML catalogue entry, a unified config's `task:` block, and a BOM'd JSON, all of which a
        # raw json.loads mis-reads — so this default can never disagree with the task the engine
        # actually parses out of the very same file (read parity).
        try:
            from looplab.core.appconfig import load_document
            task_spec, file_settings, _out = load_document(Path(task_file))
        except (OSError, ValueError):
            return False                # unreadable/foreign task file → no default; fails downstream
        if not (isinstance(task_spec, dict) and task_spec):
            return False
    from looplab.adapters.tasks import normalize_task
    from looplab.engine.genesis import default_backend
    # Best-effort, NARROW: only the task normalization may soft-fail here — an unnormalizable spec
    # is validate_task's 400 (or the engine's own startup error), never this default's concern.
    try:
        kind = normalize_task(dict(task_spec)).get("kind")
    except (KeyError, TypeError, ValueError):
        return False
    # `chosen=False` probe first: a non-generative kind can never default, so skip the Settings
    # construction (env + saved-UI validation) entirely for it.
    if default_backend(kind, chosen=False) != "llm":
        return False
    try:
        # A unified task file's settings outrank UI/env defaults in the CLI. Treat its backend as an
        # explicit choice too, so the display-only Genesis hint cannot promise llm while the child
        # would actually consume backend=toy from that file.
        selected = {**(ui_settings or {}), **(file_settings or {})}
        return "backend" not in getattr(Settings(**selected), "model_fields_set", set())
    except ValueError:  # pydantic ValidationError ⊂ ValueError — bad saved/env settings fail later,
        return False    # in the spawned engine's own Settings(); don't inject on top of them


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
    @router.post("/api/runs/{run_id}/control")
    async def control(run_id: str, request: Request):
        rd = _run_dir(run_id)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "control body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "control body must be a JSON object")

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

        return await anyio.to_thread.run_sync(_append_control)

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
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "command body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "command body must be a JSON object")
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
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "resolve-activity-claims body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "resolve-activity-claims body must be a JSON object")
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
                    before_spawn=srv.settings.refresh_env_secrets)
            except BaseException:
                if not popen_returned:
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

    def _trace_clear_receipt_lstat(path: Path) -> Optional[os.stat_result]:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HTTPException(503, {
                "code": "trace_clear_receipt_unavailable",
                "message": "The durable trace clear receipt path could not be inspected.",
                "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
            }) from exc

    def _trace_clear_receipt_reparse(info: os.stat_result) -> bool:
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)

    def _trace_clear_regular_receipt(path: Path) -> Optional[os.stat_result]:
        info = _trace_clear_receipt_lstat(path)
        if info is None:
            return None
        if _trace_clear_receipt_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise HTTPException(409, {
                "code": "trace_clear_receipt_path_invalid",
                "message": "A trace clear receipt must be a regular run-root file.",
                "remediation": "Remove the conflicting non-receipt entry before retrying.",
            })
        return info

    def _trace_clear_receipt_path(rd: Path, operation_id: str) -> Path:
        if _TRACE_CLEAR_OPERATION_RE.fullmatch(operation_id) is None:
            raise HTTPException(400, "operation_id must be tc_ followed by 32 lowercase hex digits")
        sequence_path = srv.commands._sequence_path(rd)
        # Keep durable operation receipts directly in the already-established run root. The command
        # lock directory is intentionally ephemeral and may have been created with ordinary mkdir;
        # putting a write-ahead record inside its first incarnation could therefore lose the whole
        # directory entry after power failure even though the receipt file itself was synced.
        path = sequence_path.parent.parent / (
            f"{_TRACE_CLEAR_RECEIPT_PREFIX}{sequence_path.stem}.{operation_id}.json")
        _trace_clear_regular_receipt(path)
        return path

    def _valid_trace_clear_result(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and type(value.get("removed")) is int and value["removed"] >= 0
            and type(value.get("kept")) is int and value["kept"] >= 0
        )

    def _load_trace_clear_receipt(path: Path) -> Optional[dict[str, Any]]:
        before = _trace_clear_regular_receipt(path)
        if before is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(503, {
                "code": "trace_clear_receipt_unavailable",
                "message": "The durable trace clear receipt is unreadable.",
                "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
            }) from exc
        after = _trace_clear_regular_receipt(path)
        identity = lambda info: (
            info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns,
            int(getattr(info, "st_file_attributes", 0) or 0),
        )
        if after is None or identity(after) != identity(before):
            raise HTTPException(503, {
                "code": "trace_clear_receipt_unavailable",
                "message": "The durable trace clear receipt changed while it was being read.",
                "remediation": "Retry only with the same operation id after storage is stable.",
            })
        if (not isinstance(value, dict)
                or value.get("version") != 2
                or value.get("status") not in {"pending", "succeeded", "superseded"}
                or not isinstance(value.get("id"), str)
                or _TRACE_CLEAR_OPERATION_RE.fullmatch(value["id"]) is None
                or not isinstance(value.get("expected_generation"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["expected_generation"]) is None
                or not isinstance(value.get("expected_trace_revision"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["expected_trace_revision"]) is None
                or type(value.get("node_id")) is not int
                or type(value.get("node_generation")) is not int
                or value["node_generation"] < 0
                or type(value.get("source_exists")) is not bool
                or type(value.get("result_exists")) is not bool
                or not isinstance(value.get("source_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["source_digest"]) is None
                or not isinstance(value.get("result_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["result_digest"]) is None
                or not _valid_trace_clear_result(value.get("result"))):
            raise HTTPException(503, {
                "code": "trace_clear_receipt_unavailable",
                "message": "The durable trace clear receipt is malformed.",
                "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
            })
        return value

    def _pending_trace_clear_for_lifecycle(
            rd: Path, *, receipt_path: Path, expected_generation: str,
            expected_trace_revision: str, nid: int,
            node_generation: int) -> Optional[dict[str, Any]]:
        sequence_path = srv.commands._sequence_path(rd)
        pattern = f"{_TRACE_CLEAR_RECEIPT_PREFIX}{sequence_path.stem}.tc_*.json"
        for path in sequence_path.parent.parent.glob(pattern):
            if path == receipt_path:
                continue
            # A pre-upgrade run directory may already occupy the now-reserved namespace; it is not a
            # receipt. Symlinks and other suspicious matching entries still fail closed in the loader.
            info = _trace_clear_receipt_lstat(path)
            if (info is not None and stat.S_ISDIR(info.st_mode)
                    and not _trace_clear_receipt_reparse(info)):
                continue
            receipt = _load_trace_clear_receipt(path)
            if (receipt is not None
                    and receipt.get("status") == "pending"
                    and receipt.get("expected_generation") == expected_generation
                    and receipt.get("expected_trace_revision") == expected_trace_revision
                    and receipt.get("node_id") == nid
                    and receipt.get("node_generation") == node_generation):
                return receipt
        return None

    def _save_trace_clear_receipt(path: Path, value: dict[str, Any]) -> None:
        try:
            strict_atomic_write_text(
                path, json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(503, {
                "code": "trace_clear_receipt_unavailable",
                "message": "The trace clear receipt could not be published durably.",
                "remediation": "Inspect trace-clear receipt storage in the run root before retrying.",
            }) from exc

    def _trace_clear_pending(operation_id: str, message: str) -> HTTPException:
        return HTTPException(425, {
            "code": "trace_clear_pending",
            "operation_id": operation_id,
            "message": message,
            "remediation": "Verify this same operation again; do not submit a new clear.",
        })

    @contextmanager
    def _trace_clear_guard(rd: Path, operation_id: str, pending: bool):
        """Preserve an already-pending identity when outer ownership cannot yet be acquired."""
        entered = False
        try:
            with (srv.commands.destructive_guard(rd, "clear node trace") as canonical,
                  run_lifecycle_lock_http(canonical)):
                entered = True
                yield canonical
        except HTTPException:
            if pending and not entered:
                raise _trace_clear_pending(
                    operation_id,
                    "The original clear is waiting for exclusive run lifecycle ownership.")
            raise

    def _trace_clear_receipt_result(
            receipt: Optional[dict[str, Any]], *, receipt_path: Path, operation_id: str,
            expected_generation: str, expected_trace_revision: str,
            nid: int, node_generation: int) -> Optional[dict[str, Any]]:
        if receipt is None:
            return None
        identity = (
            receipt.get("id") == operation_id
            and receipt.get("expected_generation") == expected_generation
            and receipt.get("expected_trace_revision") == expected_trace_revision
            and receipt.get("node_id") == nid
            and receipt.get("node_generation") == node_generation
        )
        if not identity:
            raise HTTPException(409, {
                "code": "trace_clear_operation_conflict",
                "message": "This trace clear operation id belongs to a different lifecycle.",
                "remediation": "Keep the original operation identity; do not reuse its id.",
            })
        if receipt.get("status") == "pending":
            # A same-id retry must enter the run sequencer. If the original owner is still alive it
            # waits (or times out ambiguously); after a process crash it becomes the recovery owner.
            return None
        if receipt.get("status") == "superseded":
            # A strict write may fail after its atomic replace became visible but before parent
            # durability was confirmed. Never expose a terminal receipt until this observer has
            # re-published the exact terminal bytes successfully.
            _save_trace_clear_receipt(receipt_path, receipt)
            raise HTTPException(409, {
                "code": "trace_clear_operation_superseded",
                "operation_id": operation_id,
                "message": (
                    "The trace changed after an interrupted clear, so its original outcome can no "
                    "longer be reconstructed. The operation is closed and will not mutate again."),
                "remediation": "Reload the current trace and form a new confirmation if needed.",
            })
        result = receipt.get("result")
        if not _valid_trace_clear_result(result):
            raise HTTPException(503, {
                "code": "trace_clear_receipt_unavailable",
                "message": "The completed trace clear receipt has no valid result.",
                "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
            })
        _save_trace_clear_receipt(receipt_path, receipt)
        return {
            "ok": True,
            "status": "succeeded",
            "operation_id": operation_id,
            "removed": result["removed"],
            "kept": result["kept"],
        }

    def _trace_content_snapshot(path: Path) -> tuple[bool, str, bytes]:
        """Read one exact trace snapshot without following a service-owned symlink."""
        try:
            if path.is_symlink():
                raise HTTPException(409, {
                    "code": "trace_path_invalid",
                    "message": "spans.jsonl must not be a symlink.",
                    "remediation": "Restore the run-owned trace file before clearing diagnostics.",
                })
            data = path.read_bytes()
            if path.is_symlink():
                raise HTTPException(409, {
                    "code": "trace_path_invalid",
                    "message": "spans.jsonl changed to a symlink while it was being inspected.",
                    "remediation": "Restore the run-owned trace file before clearing diagnostics.",
                })
            exists = True
        except HTTPException:
            raise
        except FileNotFoundError:
            data = b""
            exists = False
        except OSError as exc:
            raise HTTPException(503, {
                "code": "trace_revision_unavailable",
                "message": "The current trace contents could not be verified.",
                "remediation": "Inspect spans.jsonl storage before clearing diagnostics.",
            }) from exc
        return exists, hashlib.sha256(data).hexdigest(), data

    def _filtered_trace_snapshot(source: bytes, nid: int) -> tuple[bytes, dict[str, int]]:
        """Remove matching rows while preserving every unrelated or quarantined byte.

        Inspect every newline-terminated row independently so a malformed exporter row cannot hide
        later valid spans for this node. Invalid rows and a torn final row stay byte-for-byte.
        """
        kept_chunks: list[bytes] = []
        kept = 0
        removed = 0
        parts = source.split(b"\n")
        for index, part in enumerate(parts):
            terminated = index < len(parts) - 1
            chunk = part + (b"\n" if terminated else b"")
            if not chunk and not terminated:
                continue
            if not terminated:
                kept_chunks.append(chunk)
                continue
            line = part.strip()
            if not line:
                kept_chunks.append(chunk)
                continue
            try:
                row = orjson.loads(line)
            except orjson.JSONDecodeError:
                kept_chunks.append(chunk)
                continue
            if not isinstance(row, dict):
                kept_chunks.append(chunk)
                continue
            attributes = row.get("attributes")
            row_node_id = attributes.get("node_id") if isinstance(attributes, dict) else None
            if str(row_node_id) == str(nid):
                removed += 1
            else:
                kept += 1
                kept_chunks.append(chunk)
        return b"".join(kept_chunks), {"removed": removed, "kept": kept}

    def _invalidate_trace_clear(rd: Path, sp: Path) -> None:
        # The persisted index stores byte offsets into spans.jsonl. Invalidate it on every recovered
        # success as well: a crash may have committed the trace replacement before this cleanup.
        from looplab.events.span_index import invalidate
        invalidate(sp)
        try:
            (rd / "spans.index.jsonl").unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(503, {
                "code": "trace_clear_projection_unavailable",
                "message": "The trace changed, but its stale index could not be retired.",
                "remediation": "Repair the run directory, then verify this same operation again.",
            }) from exc
        srv.invalidate_trace_view(rd)

    def _complete_trace_clear(
            rd: Path, sp: Path, receipt_path: Path,
            receipt: dict[str, Any]) -> dict[str, Any]:
        if receipt["result"]["removed"] > 0 or not receipt["source_exists"]:
            _invalidate_trace_clear(rd, sp)
        completed = {
            **receipt,
            "status": "succeeded",
            "updated_at": time.time(),
        }
        # Publish success only after the trace postcondition was durably confirmed and every
        # process-local/disk projection was invalidated.
        _save_trace_clear_receipt(receipt_path, completed)
        result = completed["result"]
        return {
            "ok": True,
            "status": "succeeded",
            "operation_id": completed["id"],
            "removed": result["removed"],
            "kept": result["kept"],
        }

    def _supersede_trace_clear(
            receipt_path: Path, receipt: dict[str, Any], reason: str) -> None:
        _save_trace_clear_receipt(receipt_path, {
            **receipt,
            "status": "superseded",
            "superseded_reason": reason,
            "updated_at": time.time(),
        })
        raise HTTPException(409, {
            "code": "trace_clear_operation_superseded",
            "operation_id": receipt["id"],
            "message": (
                "The trace lifecycle changed after an interrupted clear. Its old operation has "
                "been closed without another deletion."),
            "remediation": "Reload the current trace and form a new confirmation if needed.",
        })

    def _apply_prepared_trace_clear(
            rd: Path, sp: Path, receipt_path: Path, receipt: dict[str, Any],
            *, current: Optional[tuple[bool, str, bytes]] = None,
            prepared: Optional[tuple[bytes, dict[str, int]]] = None) -> dict[str, Any]:
        """Recover or apply one durable write-ahead trace-clear receipt."""
        current_exists, current_digest, current_bytes = current or _trace_content_snapshot(sp)
        source_matches = (
            current_exists == receipt["source_exists"]
            and current_digest == receipt["source_digest"]
        )
        result_matches = (
            current_exists == receipt["result_exists"]
            and current_digest == receipt["result_digest"]
        )
        if result_matches and not source_matches:
            return _complete_trace_clear(rd, sp, receipt_path, receipt)
        if not source_matches:
            _supersede_trace_clear(receipt_path, receipt, "trace_changed_after_pending")

        result_bytes, result = prepared or _filtered_trace_snapshot(current_bytes, receipt["node_id"])
        prepared_matches = (
            current_exists == receipt["source_exists"]
            and receipt["result_exists"] == current_exists
            and hashlib.sha256(result_bytes).hexdigest() == receipt["result_digest"]
            and result == receipt["result"]
        )
        if not prepared_matches:
            _supersede_trace_clear(receipt_path, receipt, "prepared_postcondition_changed")
        if result_matches:
            return _complete_trace_clear(rd, sp, receipt_path, receipt)

        if result_bytes != current_bytes:
            try:
                # A durable success receipt must never outrun the destructive replacement. Strict
                # write failure is indeterminate: keep `pending`; a same-id retry compares hashes.
                strict_atomic_write_bytes(sp, result_bytes)
            except Exception as exc:  # noqa: BLE001 - normalize every durability/storage failure
                raise HTTPException(503, {
                    "code": "trace_clear_outcome_unknown",
                    "operation_id": receipt["id"],
                    "message": "The trace replacement did not return a durable completion receipt.",
                    "remediation": "Verify this same operation again; do not submit a new clear.",
                }) from exc

        try:
            post_exists, post_digest, _post_bytes = _trace_content_snapshot(sp)
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
            if code == "trace_revision_unavailable":
                raise HTTPException(503, {
                    "code": "trace_clear_outcome_unknown",
                    "operation_id": receipt["id"],
                    "message": "The trace replacement could not be read back for confirmation.",
                    "remediation": "Verify this same operation again; do not submit a new clear.",
                }) from exc
            raise
        if (post_exists != receipt["result_exists"]
                or post_digest != receipt["result_digest"]):
            raise HTTPException(503, {
                "code": "trace_clear_outcome_unknown",
                "operation_id": receipt["id"],
                "message": "The trace replacement postcondition could not be confirmed.",
                "remediation": "Verify this same operation again; do not submit a new clear.",
            })
        return _complete_trace_clear(rd, sp, receipt_path, receipt)

    @router.post("/api/runs/{run_id}/nodes/{nid}/clear_trace")
    def clear_node_trace(run_id: str, nid: int, body: Optional[dict[str, Any]] = None):
        """Erase ONE node's spans from spans.jsonl — the "clear this node's trace" button. spans.jsonl
        is append-only, so after a node_reset the rebuild would otherwise STACK its fresh bands on top
        of the old attempt's (build_conversation shows every trace tagged with the node). This removes
        the node's spans so only the next build's trace remains. REFUSED while the engine is live — it
        is the sole writer of spans.jsonl and rewriting the file under it would race/corrupt the trace;
        stop the run first. Non-destructive to the event log (events.jsonl, the source of truth, is
        untouched) — only the diagnostics trace is dropped."""
        payload = body or {}
        expected_generation = payload.get(EXPECTED_RUN_GENERATION_FIELD)
        expected_trace_revision = payload.get("expected_trace_revision")
        node_generation = payload.get("node_generation")
        operation_id = payload.get("operation_id")
        if expected_generation is not None and (
                not isinstance(expected_generation, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_generation) is None):
            raise HTTPException(400, "expected_generation must be a lowercase SHA-256 token")
        if node_generation is not None and (
                type(node_generation) is not int or node_generation < 0):
            raise HTTPException(400, "node_generation must be a non-negative integer")
        if expected_trace_revision is not None and (
                not isinstance(expected_trace_revision, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_trace_revision) is None):
            raise HTTPException(400, "expected_trace_revision must be a lowercase SHA-256 token")
        if operation_id is not None and (
                not isinstance(operation_id, str)
                or _TRACE_CLEAR_OPERATION_RE.fullmatch(operation_id) is None):
            raise HTTPException(
                400, "operation_id must be tc_ followed by 32 lowercase hex digits")
        if (expected_generation is None or expected_trace_revision is None
                or node_generation is None or operation_id is None):
            raise HTTPException(428, {
                "code": "trace_clear_identity_required",
                "message": "Trace clear requires an operation id and the exact run and node lifecycle.",
                "remediation": "Reload the node and submit all identities from the rendered trace.",
            })

        rd = _run_dir(run_id)
        receipt_path = _trace_clear_receipt_path(rd, operation_id)
        receipt = _load_trace_clear_receipt(receipt_path)
        completed = _trace_clear_receipt_result(
            receipt,
            receipt_path=receipt_path,
            operation_id=operation_id,
            expected_generation=expected_generation,
            expected_trace_revision=expected_trace_revision,
            nid=nid,
            node_generation=node_generation,
        )
        if completed is not None:
            return completed
        # Take command -> lifecycle -> engine-writer locks in that order because this is a destructive
        # whole-file rewrite. The command sequencer alone is not enough: the resume reconciler spawns
        # engines under the lifecycle lock, while direct CLI writers own only engine.lock.
        with _trace_clear_guard(rd, operation_id, receipt is not None) as rd:
            # A retry may have waited behind the original handler in the command sequencer. Re-read
            # inside ownership: a completed operation returns idempotently; a pending same-id
            # operation becomes recoverable after an earlier process crash.
            receipt = _load_trace_clear_receipt(receipt_path)
            completed = _trace_clear_receipt_result(
                receipt,
                receipt_path=receipt_path,
                operation_id=operation_id,
                expected_generation=expected_generation,
                expected_trace_revision=expected_trace_revision,
                nid=nid,
                node_generation=node_generation,
            )
            if completed is not None:
                return completed
            recovering = receipt is not None  # succeeded/superseded already returned or raised
            if recovering:
                # Re-confirm the write-ahead record before any recovery decision. This closes the
                # strict-write ambiguity where `pending` became visible but its parent fsync failed:
                # no trace mutation or terminal response may rely on that unconfirmed directory entry.
                _save_trace_clear_receipt(receipt_path, receipt)
            if not recovering:
                pending = _pending_trace_clear_for_lifecycle(
                    rd,
                    receipt_path=receipt_path,
                    expected_generation=expected_generation,
                    expected_trace_revision=expected_trace_revision,
                    nid=nid,
                    node_generation=node_generation,
                )
                if pending is not None:
                    raise _trace_clear_pending(
                        pending["id"],
                        "Another request is already resolving this exact trace clear.")

            current_generation = srv.commands.run_generation(rd)
            if expected_generation != current_generation:
                if recovering:
                    _supersede_trace_clear(
                        receipt_path, receipt, "run_generation_changed_after_pending")
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected_generation,
                    "current_generation": current_generation or None,
                    "message": "The run was reset or replaced before the trace clear was submitted.",
                    "remediation": "Reload the run before clearing trace diagnostics.",
                })

            # A new operation can fail definitively before mutation. A pending operation cannot:
            # while any writer may be alive its historical outcome remains unknown, so retain its
            # operation identity and ask the client to verify again after ownership is stopped.
            if recovering:
                liveness = _engine_liveness(rd)
                if (liveness is not False or _engine_alive(rd)
                        or _fresh_resume_launch_pending(rd)):
                    raise _trace_clear_pending(
                        operation_id,
                        "The original clear cannot be reconciled while trace write ownership is busy.")
            else:
                known_alive = _known_engine_liveness(rd, "clear the node trace")
                if known_alive or _engine_alive(rd) or _fresh_resume_launch_pending(rd):
                    raise HTTPException(
                        409, "run is live — stop it first (the engine is writing spans.jsonl)")

            # Own the same writer lock as every direct CLI for the entire rewrite. Track whether the
            # context entered so a recovery-time acquisition failure remains 425/ambiguous, while an
            # application error raised inside ownership keeps its precise status.
            writer_lock_entered = False
            try:
                with engine_write_lock_http(rd):
                    writer_lock_entered = True
                    current_generation = srv.commands.run_generation(rd)
                    if expected_generation != current_generation:
                        if recovering:
                            _supersede_trace_clear(
                                receipt_path, receipt,
                                "run_generation_changed_after_writer_lock")
                        raise HTTPException(409, {
                            "code": "run_generation_changed",
                            "expected_generation": expected_generation,
                            "current_generation": current_generation or None,
                            "message": "The run changed before exclusive trace access was acquired.",
                            "remediation": "Reload the run before clearing trace diagnostics.",
                        })
                    current_state = srv.state(rd)
                    if current_state.resume_pending():
                        if recovering:
                            raise _trace_clear_pending(
                                operation_id,
                                "The original clear cannot be reconciled while a resume is pending.")
                        raise HTTPException(409, "run has an unserved resume — stop it first "
                                                 "(an engine is about to write spans.jsonl)")
                    current_node = current_state.nodes.get(nid)
                    current_node_generation = getattr(current_node, "attempt", None)
                    node_changed = (
                        current_node is None or current_node.tombstoned
                        or nid in current_state.aborted_nodes
                        or type(current_node_generation) is not int
                        or current_node_generation < 0
                        or node_generation != current_node_generation
                    )
                    if node_changed:
                        if recovering:
                            _supersede_trace_clear(
                                receipt_path, receipt, "node_generation_changed_after_pending")
                        raise HTTPException(409, {
                            "code": "node_generation_changed",
                            "node_id": nid,
                            "expected_node_generation": node_generation,
                            "current_node_generation": (
                                current_node_generation
                                if type(current_node_generation) is int
                                and current_node_generation >= 0 else None),
                            "message": (
                                "The node no longer has the confirmed trace lifecycle."
                                if current_node is None
                                else "The node was rebuilt before the trace clear was submitted."),
                            "remediation": "Reload the node before clearing trace diagnostics.",
                        })

                    sp = rd / "spans.jsonl"
                    if recovering:
                        try:
                            return _apply_prepared_trace_clear(
                                rd, sp, receipt_path, receipt)
                        except HTTPException as exc:
                            code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
                            if code == "trace_path_invalid":
                                _supersede_trace_clear(
                                    receipt_path, receipt, "trace_path_changed_after_pending")
                            raise

                    current_trace_revision = trace_file_revision(sp)
                    if current_trace_revision is None:
                        raise HTTPException(503, {
                            "code": "trace_revision_unavailable",
                            "message": "The current trace file identity could not be verified.",
                            "remediation": "Inspect spans.jsonl storage before clearing diagnostics.",
                        })
                    if expected_trace_revision != current_trace_revision:
                        raise HTTPException(409, {
                            "code": "trace_revision_changed",
                            "expected_trace_revision": expected_trace_revision,
                            "current_trace_revision": current_trace_revision,
                            "message": "Trace diagnostics changed after the confirmation was rendered.",
                            "remediation": "Refresh the node and confirm against the current trace.",
                        })
                    source = _trace_content_snapshot(sp)
                    verified_trace_revision = trace_file_revision(sp)
                    if (verified_trace_revision is None
                            or verified_trace_revision != current_trace_revision):
                        raise HTTPException(409, {
                            "code": "trace_revision_changed",
                            "expected_trace_revision": expected_trace_revision,
                            "current_trace_revision": verified_trace_revision,
                            "message": "Trace diagnostics changed while the clear snapshot was read.",
                            "remediation": "Refresh the node and confirm against the current trace.",
                        })
                    result_bytes, result = _filtered_trace_snapshot(source[2], nid)
                    receipt = {
                        "version": 2,
                        "id": operation_id,
                        "status": "pending",
                        "expected_generation": expected_generation,
                        "expected_trace_revision": expected_trace_revision,
                        "node_id": nid,
                        "node_generation": node_generation,
                        "source_exists": source[0],
                        "source_digest": source[1],
                        "result_exists": source[0],
                        "result_digest": hashlib.sha256(result_bytes).hexdigest(),
                        "result": result,
                        "created_at": time.time(),
                    }
                    # Write-ahead hashes distinguish "not applied" from "already applied" after a
                    # crash. The same operation may recover; a changed third state is closed without
                    # another deletion.
                    _save_trace_clear_receipt(receipt_path, receipt)
                    return _apply_prepared_trace_clear(
                        rd, sp, receipt_path, receipt,
                        current=source, prepared=(result_bytes, result))
            except HTTPException:
                if recovering and not writer_lock_entered:
                    raise _trace_clear_pending(
                        operation_id,
                        "The original clear is waiting for exclusive trace write ownership.")
                raise

    def _start_public(record: dict) -> dict:
        status = str(record.get("status") or "uncertain")
        # ``accepted`` proves only that Popen returned and its ownership evidence was persisted.  The
        # child is positively started only once its exact PID generation, engine lock, or run_started
        # event is observed.  Likewise, never advertise retry while a paid effect may have escaped.
        started = status in {"executing", "succeeded"}
        paid_effect_unknown = bool(record.get("paid_effect_unknown"))
        can_retry = status in {"not_started", "failed"} and not paid_effect_unknown
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
        claim through the command service, but never creates a directory, lease, event, or process.
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
        if same_key and status in {"accepted", "executing", "succeeded"}:
            return
        if status == "uncertain":
            raise HTTPException(409, {
                "code": "start_uncertain",
                "message": "the earlier startup may have crossed Popen; observe it before retrying",
                "start_id": public.get("start_id"),
                "remediation": "Use the startup status endpoint; do not submit another launch.",
            })
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
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "resolve-claim body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "resolve-claim body must be a JSON object")
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
        # CLAUDE REVIEW: [PERF] Async handler, blocking body: both this `with srv.commands.sequence`
        # block and the main launch block below run directly on the event loop — cross-process flock
        # acquisition (up to lock_acquire_timeout=60s under contention), start-record I/O,
        # current_token's re-stat/Settings work, file materialization and the engine Popen. The
        # preflight is carefully offloaded via anyio.to_thread.run_sync two lines down, but these
        # heavier sections are not, so a contended or slow launch freezes every SSE/poll on this
        # worker. Wrap the sequence-holding sections in to_thread like /control and submit_command.
        if key:
            with srv.commands.sequence(rd):
                record, public, same_key = _inspect_keyed_start(rd, key_digest, request_digest)
                if record is not None:
                    if same_key and public["status"] in {"accepted", "executing", "succeeded"}:
                        return JSONResponse(public)
                    if same_key or public["status"] not in {"not_started", "failed"}:
                        _raise_existing_start(public, same_key=same_key)

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
        env = srv.settings.settings_env({
            setting: value for setting, value in plan.effective_settings.items()
            if base_settings.get(setting, object()) != value
        })

        start_result = None
        with srv.commands.sequence(rd):
            if key:
                existing, public, same_key = _inspect_keyed_start(
                    rd, key_digest, request_digest)
                if existing is not None:
                    if same_key and public["status"] in {"accepted", "executing", "succeeded"}:
                        return JSONResponse(public)
                    if same_key or public["status"] not in {"not_started", "failed"}:
                        _raise_existing_start(public, same_key=same_key)

            # A crashed Replay can temporarily leave the direct run directory without events.jsonl.
            # That absence is not an available run name: the durable marker still owns the namespace.
            # Keep lost-response observation of an already-owned keyed start above, but fence every
            # genuinely new reservation before it writes task/chat metadata or publishes a spawn claim.
            srv.commands._reject_unresolved_reset(rd, "start a run with this id")

            current_rd = requested_rd.resolve()
            if requested_rd.is_symlink() or current_rd != rd or current_rd.parent != root:
                raise HTTPException(409, {
                    "code": "run_path_changed",
                    "message": "run path changed while start was being prepared",
                    "field_errors": {"run_id": "choose a stable run name"},
                })
            current_token = plan.current_token(srv)
            if current_token != plan.validation_token:
                raise HTTPException(409, {
                    "code": "launch_validation_changed",
                    "message": "task, settings, run name, chat, or a referenced path changed before launch",
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
            record = None
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
            lease_started = False
            popen_boundary_entered = False
            try:
                rd.mkdir(parents=True, exist_ok=True)
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
                # From this assignment onward, an exception cannot prove whether the helper failed
                # before or after the OS accepted Popen. Retain the claim and report uncertainty.
                popen_boundary_entered = True
                pid = _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
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
                # Clear ownership only while we still know the Popen boundary was never entered.
                if lease_started and not popen_boundary_entered:
                    srv.commands.cancel_external_spawn(rd, owner)
                if record is not None:
                    detail = getattr(exc, "detail", None)
                    code = (str(detail.get("code"))
                            if isinstance(detail, dict) and detail.get("code")
                            else "spawn_failed" if record.get("phase") == "popen_pending"
                            else "start_materialization_failed")
                    record.update(
                        status="uncertain" if popen_boundary_entered else "failed",
                        phase=("failed_after_spawn" if popen_boundary_entered
                               else "failed_before_spawn"),
                        error_code=code, paid_effect_unknown=popen_boundary_entered,
                        updated_at=time.time(),
                    )
                    try:
                        srv.commands.save_start_record(rd, record)
                    except Exception:  # noqa: BLE001 - preserve original error + the spawn claim
                        pass
                raise

        if start_result is not None:
            return start_result
        return {"ok": True, "run_id": run_id, "validation_token": plan.validation_token}

    return router
