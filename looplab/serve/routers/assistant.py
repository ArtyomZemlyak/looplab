"""Assistant routes (general chat agent). The evolution of Genesis: a persistent, general-purpose
chat agent with its OWN sessions (not tied to a run). Read-only tools work in every mode;
write/shell/git are gated by the permission MODE. Sessions live under <run_root>/assistant/ (a
RESERVED id), separate from any run's chat.

Bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4). The HITL permission
registry (`_perm_*` state + `_make_approver`/`_acquire_turn`/`_release_turn`/`_deny_session_perms`)
moves as ONE unit. The two turn endpoints (`message` / `message_stream`) shared a long duplicated
prologue/epilogue; that is now `_begin_turn`/`_finish_turn`/`_make_progress_hooks`, parameterized
where the two endpoints deliberately differed (owner-guard cancel check, progress `updated` stamp,
best-effort vs. strict reply persistence) — parameterized, not normalized."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from looplab.serve.assistant import (
    REPO_ROOT as _ASSISTANT_REPO_ROOT, SHARE_DEFAULT_TTL_SECONDS, SHARE_MAX_RECORDS,
    SessionStore, ShareError, SHARE_TITLE_MAX_CHARS, ShareStore,
    run_turn as _assistant_run_turn,
    safe_assistant_failure as _safe_assistant_failure,
    sanitize_assistant_message as _sanitize_assistant_message)
from looplab.serve.engine_proc import _engine_alive
from looplab.serve.llm_context import _client_tokens
from looplab.serve.protocol import (
    ASSISTANT_STREAM_END_SENTINEL, PERM_ALLOW_ALWAYS, PERM_ALLOW_ONCE, PERM_DENY,
    SSE_DONE, SSE_ERROR, SSE_STEP, SSE_TEXT, SSE_TODOS, SSE_TOKEN)
from looplab.tools.perm_modes import (
    GRANT_TTL_SECONDS, RememberedGrantStore, classify_action, normalize_mode)
from looplab.core.redact import redact_secrets

ASSISTANT_SHARE_HEADER = "X-LoopLab-Share"

# Cadence for draining the assistant worker's queue from the event loop (see `gen` below). The poll
# only runs while the worker has produced NOTHING, so it bounds idle latency, not throughput; the
# keepalive comment keeps a buffering proxy's idle read-timer from firing on a long LLM call.
_ASSISTANT_STREAM_POLL_SECONDS = 0.05
_ASSISTANT_STREAM_KEEPALIVE_SECONDS = 10.0


async def _json_object(request: Request) -> dict:
    """Parse a request body as a JSON object or fail with 400 (mirrors routers/boss + control), so a
    non-JSON / non-object body (e.g. a bare ``[]``) yields a clean 400 instead of a 500 from a later
    ``body.get(...)``."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body


async def _json_options(request: Request) -> dict:
    """Like `_json_object`, but an ABSENT body means "no options" rather than a 400 — for routes
    whose every field has a default and whose callers may legitimately send nothing at all."""
    if not await request.body():
        return {}
    return await _json_object(request)


_SHARED_CONTENT_MAX_CHARS = 200_000
_SHARED_ACTIVITY_TEXT_MAX_CHARS = 8_000
_SHARED_TODO_TEXT_MAX_CHARS = 1_000
_SHARED_ITEMS_MAX = 80
_SHARED_TODOS_MAX = 100
_SHARED_MESSAGES_MAX = 400
_SHARED_TOTAL_TEXT_MAX_CHARS = 2_000_000
_SHARED_STRUCTURE_MAX = 20_000
_SHARED_READ_MAX_LINE_BYTES = 8 * 1024 * 1024
_SHARED_READ_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_SHARED_TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})
_SHARED_UI_CONTEXT_SUFFIX = re.compile(r"\n*\[UI context:[^\]]*\]\s*$")
_SHARE_ID_RE = re.compile(r"[0-9a-f]{32}")

# Tool arguments and provider-produced labels routinely contain absolute paths, run ids and usernames.
# Public transcripts therefore never reflect either.  Only an exact known tool name can become one of
# these fixed phrases; unknown/new tools disappear until deliberately reviewed and added here.
_SHARED_TOOL_LABELS = {
    "list_dir": "Listing files", "read_file": "Reading a file",
    "find_files": "Searching files", "grep": "Searching file contents",
    "list_runs": "Listing runs", "read_run": "Reading a run",
    "read_run_experiment": "Reading run experiments", "read_run_logs": "Reading run logs",
    "read_run_trace": "Reading a run trace", "cross_run_concept_map": "Reading shared research",
    "cross_run_atlas": "Reading shared research", "cross_run_prior_attempts": "Reading shared research",
    "cross_run_search": "Searching shared research", "concept_taxonomy": "Reading concept taxonomy",
    "write_file": "Updating files", "edit_file": "Updating files", "apply_patch": "Updating files",
    "delete_file": "Updating files", "revert_file": "Updating files",
    "run_command": "Running a command", "run_tests": "Running checks",
    "read_output": "Reading command output", "list_background": "Listing background work",
    "kill_background": "Stopping background work", "remember": "Updating shared knowledge",
    "concept_merge": "Updating concepts", "concept_purge": "Updating concepts",
    "concept_split": "Updating concepts", "concept_edit_clear": "Updating concepts",
    "finalize_run": "Updating a run", "stop_run": "Updating a run", "resume_run": "Updating a run",
    "reset_node": "Updating a run", "retag_node": "Updating a run",
    "set_run_concepts": "Updating a run", "delete_node": "Updating a run",
    "delete_run": "Updating a run", "extend_budget": "Updating a run",
    "set_directive": "Updating a run", "set_trust_gate": "Updating a run",
    "propose_run": "Preparing a run", "task": "Delegating a subtask",
    "write_todos": "Updating the plan",
}


def _shared_units(value: str) -> int:
    """JavaScript ``String.length`` units, so backend limits cannot exceed the browser validator."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def _shared_text_projection(value, max_chars: int = _SHARED_CONTENT_MAX_CHARS) -> tuple[str, bool]:
    # Redact before truncation so a secret-shaped suffix cannot be cut into a form the redactor misses.
    text = redact_secrets(str(value or ""))
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > max_chars:
            return text[:index], True
    return text, False


def _shared_text(value, max_chars: int = _SHARED_CONTENT_MAX_CHARS) -> str:
    return _shared_text_projection(value, max_chars)[0]


def _shared_visible_content_projection(message: dict) -> tuple[str, bool]:
    raw = str(message.get("content") or "")
    if message.get("role") == "user":
        # Strip the whole invisible suffix before redaction/truncation; truncating first could cut off
        # its closing bracket and turn an otherwise removable private context block into visible text.
        raw = _SHARED_UI_CONTEXT_SUFFIX.sub("", raw).rstrip()
    return _shared_text_projection(raw)


def _shared_visible_content(message: dict) -> str:
    return _shared_visible_content_projection(message)[0]


def _shared_message_projection(message: dict) -> tuple[dict, bool]:
    """Allow-list the fields rendered by the read-only shared transcript. Persisted assistant turns
    also contain mutation internals (absolute paths and full diff previews); merely dropping `raw`
    exposes those through the intentionally untokened share URL."""
    content, truncated = _shared_visible_content_projection(message)
    out = {
        "role": "user" if message.get("role") == "user" else "assistant",
        "content": content,
    }

    def _labels(items):
        labels = []
        omitted = False
        for item in items or []:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            label = _SHARED_TOOL_LABELS.get(tool) if isinstance(tool, str) else None
            if label is not None:
                if len(labels) < _SHARED_ITEMS_MAX:
                    labels.append({"label": label})
                else:
                    omitted = True
        return labels, omitted

    if isinstance(message.get("steps"), list):
        out["steps"], omitted = _labels(message["steps"])
        truncated = truncated or omitted
    if isinstance(message.get("applied"), list):
        out["applied"], omitted = _labels(message["applied"])
        truncated = truncated or omitted
    if isinstance(message.get("todos"), list):
        todos = []
        for item in message["todos"]:
            if not isinstance(item, dict):
                continue
            if len(todos) >= _SHARED_TODOS_MAX:
                truncated = True
                continue
            todo_content, omitted = _shared_text_projection(
                item.get("content"), _SHARED_TODO_TEXT_MAX_CHARS)
            truncated = truncated or omitted
            status = item.get("status")
            todos.append({"content": todo_content,
                          "status": (status if isinstance(status, str)
                                     and status in _SHARED_TODO_STATUSES else "pending")})
        out["todos"] = todos
    if isinstance(message.get("activity"), list):
        activity = []
        for item in message["activity"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                if len(activity) >= _SHARED_ITEMS_MAX:
                    truncated = True
                    continue
                activity_content, omitted = _shared_text_projection(
                    item.get("content"), _SHARED_ACTIVITY_TEXT_MAX_CHARS)
                truncated = truncated or omitted
                activity.append({"type": "text", "content": activity_content})
            # ``activity.tools`` contains labels only, with no trustworthy tool id from which to choose
            # a fixed public phrase.  Omit the segment instead of reflecting model/argument-derived text.
        out["activity"] = activity
    return out, truncated


def _shared_message(message: dict) -> dict:
    return _shared_message_projection(message)[0]


def _complete_shared_messages(messages) -> list[dict]:
    """The longest well-formed user->assistant prefix; never publish a staged, unanswered user turn."""
    if not isinstance(messages, list):
        return []
    complete: list[dict] = []
    pending = None
    for message in messages:
        if not isinstance(message, dict):
            break
        if pending is None:
            if message.get("role") != "user":
                break
            pending = message
        else:
            if message.get("role") != "assistant":
                break
            complete.extend((pending, message))
            pending = None
    return complete


def _shared_message_weight(message: dict) -> int:
    total = _shared_units(message.get("content") or "")
    for item in [*(message.get("steps") or []), *(message.get("applied") or [])]:
        total += _shared_units(item.get("label") or "")
    for item in message.get("todos") or []:
        total += _shared_units(item.get("content") or "") + _shared_units(item.get("status") or "")
    for item in message.get("activity") or []:
        total += _shared_units(item.get("content") or "")
    return total


def _shared_text_structure_weight(value: str) -> int:
    """Mirror the browser's conservative Markdown/DOM amplification estimate."""
    total = 1  # one text/Markdown wrapper
    for char in value:
        if char in "\n\r":
            total += 2
        elif char in "*@[_`":
            total += 1
    return total


def _shared_message_structure_weight(message: dict) -> int:
    total = 6 + _shared_text_structure_weight(message.get("content") or "")
    total += len(message.get("steps") or []) + len(message.get("applied") or [])
    total += 2 * len(message.get("todos") or []) + 2 * len(message.get("activity") or [])
    for item in message.get("activity") or []:
        if item.get("type") == "text":
            total += _shared_text_structure_weight(item.get("content") or "")
        elif item.get("type") == "tools":
            total += len(item.get("labels") or [])
    return total


def _shared_projection(messages, *, initial_text: int = 0) -> tuple[list[dict], bool]:
    """Return the complete public projection plus any count/text/subitem-cap omission signal."""
    complete = _complete_shared_messages(messages)
    bounded = complete[:_SHARED_MESSAGES_MAX]
    truncated = len(bounded) < len(complete)
    projected: list[dict] = []
    total = max(0, initial_text)
    structure = 0
    for index in range(0, len(bounded), 2):
        pair_with_status = [_shared_message_projection(_sanitize_assistant_message(message))
                            for message in bounded[index:index + 2]]
        pair = [message for message, _omitted in pair_with_status]
        if any(omitted for _message, omitted in pair_with_status):
            truncated = True
        weight = sum(_shared_message_weight(message) for message in pair)
        pair_structure = sum(_shared_message_structure_weight(message) for message in pair)
        if (total + weight > _SHARED_TOTAL_TEXT_MAX_CHARS
                or structure + pair_structure > _SHARED_STRUCTURE_MAX):
            truncated = True
            break
        projected.extend(pair)
        total += weight
        structure += pair_structure
    return projected, truncated


def _project_shared_messages(messages, *, initial_text: int = 0) -> list[dict]:
    return _shared_projection(messages, initial_text=initial_text)[0]


def _shared_title(messages: list[dict]) -> str:
    if (messages and messages[0].get("role") == "user"
            and isinstance(messages[0].get("content"), str)):
        visible = " ".join(_shared_visible_content(messages[0]).split())
        if visible:
            return visible[:SHARE_TITLE_MAX_CHARS]
    return "Shared chat"


def build_router(srv) -> APIRouter:
    router = APIRouter()
    root = srv.root
    _llm_settings = srv.llm_settings

    # ------------------------------------------------------------------ assistant (general chat agent)
    _asst = SessionStore(root)
    _shares = ShareStore(root)

    # --- pause-resume human-in-the-loop permission registry -------------------------------------
    # A mutating tool in a confirm mode calls the injected approver, which registers a request here
    # and BLOCKS the (background) turn thread on an Event; the UI polls GET .../permissions, shows a
    # confirm-card, and POST .../permissions/{id} sets the decision + unblocks the thread. This keeps
    # the synchronous drive_tool_loop unchanged while giving true mid-loop approval (the tool sees the
    # REAL result). "allow_always" is exact-action/scope, mode, and turn-epoch bound — never a broad
    # session→tool-kind bypass.
    _perm_lock = threading.Lock()
    # Share mint/revoke and deletion change one authorization lifecycle.  A single order here prevents
    # a mint racing past DELETE's active-link check or an Unshare being lost behind a concurrent mint.
    _session_lifecycle_lock = threading.RLock()
    _perm_reqs: dict = {}
    _perm_always = RememberedGrantStore()
    _asst_progress: dict = {}      # sid -> {steps:[label,…], updated} — live tool steps during a turn
    _asst_cancel: dict = {}        # sid -> threading.Event — set by /cancel to stop an in-flight turn
    _asst_turn_done: dict = {}     # sid -> threading.Event — deletion waits for the worker to quiesce
    _asst_epoch: dict = {}         # sid -> opaque random epoch owned by the active cancel event
    _deleting_sessions: set[str] = set()  # closes get→turn races while DELETE owns the session
    # A share fence is intentionally only a tiny registry flag. Snapshot capture/projection and the
    # capability-store fsync can be slow, so they must not hold ``_perm_lock`` and stall permission,
    # progress, Stop, or unrelated chats. A fenced session cannot claim a new turn until mint either
    # commits or rolls back; lifecycle operations serialize outside this registry via
    # ``_session_lifecycle_lock`` (lock order is always lifecycle -> registry, never the reverse).
    _share_fenced_sessions: set[str] = set()
    try:
        _configured_perm_timeout = float(os.environ.get("LOOPLAB_ASSISTANT_PERM_TIMEOUT", "900"))
    except (TypeError, ValueError):
        _configured_perm_timeout = 900.0
    _PERM_TIMEOUT = (_configured_perm_timeout
                     if math.isfinite(_configured_perm_timeout) and _configured_perm_timeout > 0
                     else 900.0)
    _PERM_TIMEOUT = min(3600.0, max(1.0, _PERM_TIMEOUT))
    _SENSITIVE_SCOPE_KEY = re.compile(
        r"token|secret|credential|password|passwd|api[_-]?key|content|preview", re.I)

    def _share_http_error(status_code: int, code: str, message: str, *, remediation: str = "",
                          **extra) -> HTTPException:
        detail = {"code": code, "message": message}
        if remediation:
            detail["remediation"] = remediation
        detail.update(extra)
        return HTTPException(status_code=status_code, detail=detail)

    def _acknowledged_live_share_ids(body: dict) -> frozenset[str]:
        raw = body.get("acknowledged_live_share_ids", [])
        if not isinstance(raw, list) or len(raw) > SHARE_MAX_RECORDS:
            raise _share_http_error(
                400, "assistant_live_share_ack_invalid",
                f"acknowledged_live_share_ids must be a list of at most {SHARE_MAX_RECORDS} ids.")
        acknowledged: set[str] = set()
        for value in raw:
            if (not isinstance(value, str) or len(value) != 32
                    or _SHARE_ID_RE.fullmatch(value) is None
                    or value in acknowledged):
                raise _share_http_error(
                    400, "assistant_live_share_ack_invalid",
                    "acknowledged_live_share_ids must contain unique lowercase capability ids.")
            acknowledged.add(value)
        return frozenset(acknowledged)

    def _live_share_ack_required(active_count: int) -> HTTPException:
        return _share_http_error(
            409, "assistant_live_share_ack_required",
            "The live public-link state changed before this message could be saved.",
            remediation="Refresh the chat, review its live-link warning, and send again.",
            active_live_share_count=max(0, int(active_count)))

    def _rollback_share_token(token: str) -> None:
        # The token has not crossed the response boundary yet.  Best-effort revocation makes a meta
        # write failure atomic from the caller's perspective without letting rollback mask that error.
        try:
            _shares.revoke_token(token)
        except Exception:  # noqa: BLE001 - do not expose an unpublished capability on rollback failure
            pass

    def _public_scope(scope: object) -> dict:
        """Keep the exact digest private while exposing only bounded, redacted review metadata."""
        if not isinstance(scope, dict):
            return {}
        public = {}
        for key, value in list(scope.items())[:32]:
            if not isinstance(key, str):
                continue
            safe_key = key[:80]
            if _SENSITIVE_SCOPE_KEY.search(safe_key) and not safe_key.lower().endswith("_digest"):
                continue
            if isinstance(value, str):
                # Redact BEFORE truncating so a pattern-shaped secret straddling the length bound
                # can't lose its tail and evade the match (the reviews-surface discipline).
                public[safe_key] = redact_secrets(value, entropy=False)[:4000]
            elif value is None or isinstance(value, (bool, int)):
                public[safe_key] = value
            elif isinstance(value, float) and math.isfinite(value):
                public[safe_key] = value
            elif isinstance(value, list):
                public[safe_key] = [
                    redact_secrets(str(item), entropy=False)[:1000]
                    for item in value[:200] if isinstance(item, (str, bool, int, float))]
        return public

    def _make_approver(sid: str, mode: str, epoch: str,
                       cancel_ev: "Optional[threading.Event]" = None):
        def approver(action: dict) -> str:
            # A confirm raised AFTER the user hit Stop (the in-flight model response's tools still
            # execute once) must not park the worker for _PERM_TIMEOUT on a card nobody will see —
            # deny immediately, so the loop's next cancel_check ends the turn.
            if cancel_ev is not None and cancel_ev.is_set():
                return PERM_DENY
            policy = classify_action(action)
            with _perm_lock:
                if cancel_ev is not None and cancel_ev.is_set():
                    return PERM_DENY
                if _perm_always.allows(sid, mode, epoch, policy):
                    return PERM_ALLOW_ALWAYS
            req_id = secrets.token_hex(8)
            ev = threading.Event()
            safe_action = {}
            public_limits = {
                "tool": 160, "tool_kind": 80, "label": 500, "verb": 1000,
                "path": 1000, "preview": 4000, "cwd": 1000,
            }
            for key, limit in public_limits.items():
                value = (action or {}).get(key)
                if isinstance(value, str):
                    safe_action[key] = value[:limit]
            safe_action.update({
                "risk": policy.risk,
                "action_id": policy.action_id,
                "scope": _public_scope(policy.scope),
                "scope_digest": policy.scope_digest,
                "consequence": policy.consequence,
                "rememberable": policy.rememberable,
            })
            created = time.time()
            public_epoch = hashlib.sha256(epoch.encode("utf-8")).hexdigest()[:16]
            with _perm_lock:
                _perm_reqs[req_id] = {"id": req_id, "session": sid, "action": safe_action,
                                      "status": "pending", "decision": None, "event": ev,
                                      "created": created,
                                      "expires_at": created + _PERM_TIMEOUT,
                                      "grant_ttl_seconds": int(GRANT_TTL_SECONDS),
                                      "mode": normalize_mode(mode), "epoch": public_epoch,
                                      "policy": policy}
                if len(_perm_reqs) > 128:      # evict oldest RESOLVED beyond 128 — NEVER a pending
                    # request (its approver thread is blocked on that entry's Event; evicting it would
                    # 404 the resolve and hang the worker for the whole _PERM_TIMEOUT).
                    stale = [k for k in sorted(_perm_reqs, key=lambda j: _perm_reqs[j]["created"])
                             if _perm_reqs[k]["status"] != "pending"]
                    for k in stale[:max(0, len(_perm_reqs) - 128)]:
                        _perm_reqs.pop(k, None)
            # Wait in short slices so a Stop DURING the wait un-parks promptly (cancel_ev wake) even
            # though the resolve Event is what a normal Approve/Reject sets.
            deadline = time.monotonic() + _PERM_TIMEOUT
            got = False
            while time.monotonic() < deadline:
                got = ev.wait(timeout=1.0)
                if got or (cancel_ev is not None and cancel_ev.is_set()):
                    break
            with _perm_lock:
                req = _perm_reqs.get(req_id) or {}
                dec = (req.get("decision") if got else PERM_DENY) or PERM_DENY
                req["status"] = "resolved"
                if cancel_ev is not None and cancel_ev.is_set():
                    dec = PERM_DENY
                    req["decision"] = PERM_DENY
                    _perm_always.invalidate(sid, epoch=epoch)
                elif dec == PERM_ALLOW_ALWAYS:
                    # The resolver rejects this for HIGH/UNKNOWN; retain defense in depth if an
                    # in-process caller tampers with the request.
                    if not _perm_always.remember(sid, mode, epoch, policy):
                        dec = PERM_DENY
                        req["decision"] = PERM_DENY
            return dec
        return approver

    def _acquire_turn(sid: str, cancel_ev: "threading.Event", epoch: str):
        """Claim the SINGLE active-turn slot for a session. Two concurrent turns on one session (two
        tabs, or the stream + non-stream endpoints) otherwise interleave: the second reads a `history`
        missing the first's reply, `_asst_cancel[sid]` gets clobbered so Stop hits the wrong turn, and
        replies append in completion order (u1,u2,a2,a1). Reject the second with 409 instead. Registers
        the cancel event + a fresh progress entry, both owned by `cancel_ev`, atomically under the
        lock."""
        with _perm_lock:
            if sid in _deleting_sessions:
                raise HTTPException(409, "this session is being deleted")
            if sid in _share_fenced_sessions:
                raise HTTPException(409, {
                    "code": "assistant_turn_share_in_progress",
                    "message": "A public snapshot is being created for this chat. Try sending again in a moment.",
                })
            existing = _asst_cancel.get(sid)
            # A set cancel flag means "stopping", not "finished": the old worker may still be inside
            # an uninterruptible model/tool call and may already own a durable run command.  Keep the
            # slot until its finally calls `_release_turn` so U2 cannot overtake the missing A1.
            if existing is not None:
                raise HTTPException(409, "a turn is already running for this session")
            _asst_cancel[sid] = cancel_ev
            _asst_turn_done[sid] = threading.Event()
            _asst_epoch[sid] = epoch
            _asst_progress[sid] = {"steps": [], "todos": [], "text": "", "updated": time.time(),
                                   "owner": cancel_ev, "epoch": epoch}

    def _release_turn(sid: str, cancel_ev: "threading.Event", epoch: str) -> None:
        """Tear down ONLY this turn's registry entries (a newer turn may have replaced them): pop the
        cancel event and progress entry only when `cancel_ev` still owns them."""
        done = None
        with _perm_lock:
            if _asst_cancel.get(sid) is cancel_ev:
                _asst_cancel.pop(sid, None)
                done = _asst_turn_done.pop(sid, None)
                _asst_epoch.pop(sid, None)
                _perm_always.invalidate(sid, epoch=epoch)
            p = _asst_progress.get(sid)
            if p is not None and p.get("owner") is cancel_ev:
                _asst_progress.pop(sid, None)
        if done is not None:
            done.set()

    def _deny_session_perms_locked(sid: str) -> None:
        """Deny a session's pending cards while `_perm_lock` is held by the caller."""
        _perm_always.invalidate(sid)
        for r in _perm_reqs.values():
            if r.get("session") == sid and r.get("status") == "pending":
                r["decision"] = PERM_DENY
                r["status"] = "resolved"
                r["event"].set()

    def _deny_session_perms(sid: str) -> None:
        """Auto-deny + UNBLOCK every pending permission request for a session. Called on cancel/delete
        so a worker parked in `approver.ev.wait` un-parks immediately (returns "deny" → the mutating
        tool is NOT executed → the loop's next cancel_check ends the turn), instead of blocking for
        _PERM_TIMEOUT. Also flips the request to resolved so a stopped turn's confirm can't be
        re-surfaced and approved later to fire a mutation the user already cancelled."""
        with _perm_lock:
            _deny_session_perms_locked(sid)

    @router.get("/api/assistant/permissions")
    def assistant_permissions(session: Optional[str] = None):
        with _perm_lock:
            out = [{"id": r["id"], "session": r["session"], "action": r["action"],
                    "created": r["created"], "expires_at": r["expires_at"],
                    "grant_ttl_seconds": r["grant_ttl_seconds"],
                    "mode": r["mode"], "epoch": r["epoch"]}
                   for r in _perm_reqs.values()
                   if r["status"] == "pending" and (not session or r["session"] == session)]
        return {"pending": out}

    @router.post("/api/assistant/permissions/{req_id}")
    async def assistant_resolve(req_id: str, request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "permission body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "permission body must be a JSON object")
        dec = body.get("decision", PERM_DENY)
        with _perm_lock:
            r = _perm_reqs.get(req_id)
            if not r:
                raise HTTPException(404, "no such permission request")
            # CAS pending -> resolved (arch-review §3 P0-6): a request that is ALREADY resolved — the
            # user approved it once, or cancel/delete auto-denied it — must not be overwritten. Without
            # this, a stale resolve racing a cancel could flip a denied request back to `allow` after
            # the worker un-parked, firing the mutation the user just cancelled (a reproduced
            # write-after-cancel). A stale/duplicate resolve now returns 409 instead of clobbering.
            if r.get("status") != "pending":
                raise HTTPException(409, "permission request already resolved")
            if time.time() >= float(r.get("expires_at") or 0):
                r["decision"] = PERM_DENY
                r["status"] = "resolved"
                r["event"].set()
                raise HTTPException(409, {
                    "code": "permission_request_expired",
                    "message": "This approval request expired; run the action again to review it.",
                })
            policy = r.get("policy")
            if dec == PERM_ALLOW_ALWAYS and not bool(getattr(policy, "rememberable", False)):
                raise HTTPException(400, {
                    "code": "permission_not_rememberable",
                    "risk": getattr(policy, "risk", "UNKNOWN"),
                    "message": "This action requires an explicit approval every time.",
                })
            r["decision"] = dec if dec in (PERM_ALLOW_ONCE, PERM_ALLOW_ALWAYS, PERM_DENY) else PERM_DENY
            r["status"] = "resolved"
            r["event"].set()
        return {"ok": True}

    @router.get("/api/assistant/commands")
    def assistant_commands():
        from looplab.serve.assistant_commands import list_commands
        return {"commands": list_commands()}

    @router.post("/api/assistant/revert")
    async def assistant_revert(request: Request):
        """Undo one exact assistant change while its applied post-image is still current."""
        body = await _json_object(request)
        path = body.get("path")
        if not isinstance(path, str) or not path:
            raise HTTPException(400, "path is required")
        recovery_id = body.get("recovery_id")
        if not isinstance(recovery_id, str) or not recovery_id or len(recovery_id) > 128:
            raise HTTPException(400, "recovery_id is required")
        expected_postimage = body.get("expected_postimage")
        if (not isinstance(expected_postimage, dict)
                or set(expected_postimage) != {"exists", "digest", "mode"}
                or not isinstance(expected_postimage.get("exists"), bool)):
            raise HTTPException(
                400, "expected_postimage must contain exactly exists, digest, and mode")
        expected_digest = expected_postimage.get("digest")
        expected_mode = expected_postimage.get("mode")
        if expected_postimage["exists"]:
            if (not isinstance(expected_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None):
                raise HTTPException(400, "expected_postimage.digest must be a SHA-256 digest")
            if type(expected_mode) is not int or not 0 <= expected_mode <= 0o7777:
                raise HTTPException(
                    400, "expected_postimage.mode must be valid permission bits")
        elif expected_digest is not None or expected_mode is not None:
            raise HTTPException(
                400, "expected_postimage.digest and mode must be null when the file is absent")
        expected_state = {
            "exists": expected_postimage["exists"], "digest": expected_digest,
        }
        from looplab.tools.perm_modes import DEFAULT_MODE
        from looplab.tools.write_tools import WriteTools
        # This is the OPERATOR-CLICKED undo, and the click IS the approval — so it calls the ungated,
        # receipt-bound `WriteTools.revert_exact`. The MODEL-invocable `_revert_tool` keeps its
        # historical path-only behavior and mode/approver gate. The path is still bounded by `_check`:
        # allowed roots, `looks_secret`, and the protected-file list, none of which depend on the mode.
        # `mode` is therefore inert here — `revert_exact` never reaches `_authorize`. It is pinned to the
        # fail-closed DEFAULT rather than "auto" so that if this route is ever pointed at a GATED
        # verb, the permission machinery denies by default instead of silently self-approving.
        wt = WriteTools([Path.home(), _ASSISTANT_REPO_ROOT, root], mode=DEFAULT_MODE,
                        repo_root=_ASSISTANT_REPO_ROOT, backup_dir=root / "assistant" / "backups")
        result = await anyio.to_thread.run_sync(
            lambda: wt.revert_exact(path, recovery_id, expected_state, expected_mode))
        if result is None:
            raise HTTPException(409, {
                "code": "assistant_revert_conflict",
                "message": "This change is no longer the current undo target or the file changed after it was applied.",
            })
        return {"ok": True, "result": result}

    @router.get("/api/assistant/progress")
    def assistant_progress(session: str):
        with _perm_lock:
            p = _asst_progress.get(session)
            return {"steps": list(p["steps"]) if p else [],
                    "todos": list(p.get("todos", [])) if p else [],
                    "text": p.get("text", "") if p else "",   # live answer-so-far (proxy-buffered SSE fallback)
                    "active": bool(p)}

    @router.post("/api/assistant/sessions/{sid}/cancel")
    def assistant_cancel(sid: str):
        """Stop an in-flight turn. Sets the session's cancel flag; the tool loop checks it at the next
        turn boundary and finalizes from what it has. Works from any client (survives a page reload,
        since the turn + flag live server-side, keyed by session id)."""
        with _perm_lock:
            ev = _asst_cancel.get(sid)
            if ev is not None:
                # Publish cancellation and deny/invalidate THIS currently-owned turn atomically. If
                # the worker released and a new turn acquired between two lock sections, a stale
                # Cancel could otherwise deny the new turn's card.
                ev.set()
            _deny_session_perms_locked(sid)
        if ev is not None:
            # Publish cancellation BEFORE grant invalidation/pending denial. A repeated action racing
            # this endpoint must see the set flag and cannot consume an exact remembered grant in the
            # same registry transaction.
            return {"ok": True, "cancelling": True}
        return {"ok": True, "cancelling": False}   # nothing running for this session

    @router.get("/api/assistant/sessions")
    def assistant_sessions():
        # ``meta.shared`` is only a legacy hint and can remain true after expiry or a failed cleanup.
        # Derive all badges from active validated capability records, without returning a token/hash.
        with _session_lifecycle_lock:
            try:
                summary = _shares.active_summary_by_session()
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            sessions = []
            for meta in _asst.list():
                active = summary.get(meta.get("id"), {})
                count = int(active.get("share_count") or 0)
                sessions.append({**meta, "shared": count > 0, "share_count": count,
                                 "share_ids": list(active.get("share_ids") or []),
                                 "live_share_ids": list(active.get("live_share_ids") or []),
                                 "share_live": bool(active.get("share_live")) if count else False,
                                 "share_expires_at": (active.get("share_expires_at")
                                                      if count else None)})
        return {"sessions": sessions}

    @router.post("/api/assistant/sessions")
    async def assistant_create(request: Request):
        body = await _json_object(request)
        meta = _asst.create(title=(body.get("title") or ""),
                            mode=body.get("mode") or "plan")
        return meta

    @router.get("/api/assistant/sessions/{sid}")
    def assistant_get(sid: str):
        with _session_lifecycle_lock:
            try:
                sess = _asst.get(sid)
            except ValueError:
                raise HTTPException(404, "no such session")
            if sess is None:
                raise HTTPException(404, "no such session")
            try:
                active = _shares.active_for_session(sid)
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            share_count = len(active)
            share_expires_at = max(
                (record["expires_at"] for record in active), default=None)
            meta = {**sess["meta"], "shared": share_count > 0, "share_count": share_count,
                    "share_ids": [record["id"] for record in active],
                    "live_share_ids": [record["id"] for record in active if record["live"]],
                    "share_expires_at": share_expires_at,
                    "share_live": any(record["live"] for record in active)}
            sess = {**sess, "meta": meta,
                    "messages": [_sanitize_assistant_message(m) for m in sess["messages"]]}
        return sess

    @router.delete("/api/assistant/sessions/{sid}")
    def assistant_delete(sid: str):
        import shutil
        try:
            d = _asst._sdir(sid)
        except ValueError:
            raise HTTPException(404, "no such session")
        # Establish deletion ownership and cancel the worker while lifecycle excludes share mint and
        # turn claim.  The potentially long worker wait happens *outside* lifecycle: reply persistence
        # now needs that lock for its final capability check, so waiting while holding it would make
        # DELETE and the worker deadlock until timeout.  `_deleting_sessions` closes both races while
        # the lock is released.
        with _session_lifecycle_lock:
            try:
                active_shares = _shares.active_for_session(sid)
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            if active_shares:
                raise _share_http_error(
                    409, "assistant_delete_shared",
                    "This chat still has active public share links.",
                    remediation="Unshare the chat, then delete it.",
                    active_share_count=len(active_shares))
            # Stop an in-flight turn before removing its files: cancel the loop, unblock a worker parked
            # on a confirm, and prevent a request that already read the session from claiming a new turn.
            # Waiting for the worker closes the mutation-journal/reply race that could recreate material
            # after rmtree and turn a reported success into a partial deletion.
            with _perm_lock:
                if sid in _deleting_sessions:
                    raise HTTPException(409, {
                        "code": "assistant_delete_busy",
                        "message": "This chat is already being deleted. Try again shortly.",
                    })
                _deleting_sessions.add(sid)
                cev = _asst_cancel.get(sid)
                done = _asst_turn_done.get(sid)
                if cev is not None:
                    cev.set()
                _deny_session_perms_locked(sid)
        try:
            if cev is not None and (done is None or not done.wait(timeout=5.0)):
                raise HTTPException(409, {
                    "code": "assistant_delete_busy",
                    "message": "The live Assistant turn is still stopping. Try deleting this chat again shortly.",
                })
            with _session_lifecycle_lock:
                # Defense in depth for another process/store writer: in-process share creation has
                # already been fenced by `_deleting_sessions`, but never delete after a capability
                # appeared while lifecycle was released for worker quiescence.
                try:
                    active_shares = _shares.active_for_session(sid)
                except ShareError as exc:
                    raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
                if active_shares:
                    raise _share_http_error(
                        409, "assistant_delete_shared",
                        "This chat gained an active public share link while deletion was waiting.",
                        remediation="Unshare the chat, then delete it.",
                        active_share_count=len(active_shares))
                if d.exists():
                    try:
                        shutil.rmtree(d)
                    except OSError:
                        # Windows commonly refuses a recursive delete while an editor/indexer still has
                        # a transcript file open. Never report success while session material remains.
                        if d.exists():
                            raise HTTPException(409, {
                                "code": "assistant_delete_incomplete",
                                "message": "The chat could not be completely removed. Close anything using its files and try again.",
                            })
                if d.exists():
                    raise HTTPException(409, {
                        "code": "assistant_delete_incomplete",
                        "message": "The chat could not be completely removed. Close anything using its files and try again.",
                    })
                with _perm_lock:  # drop the deleted session's in-memory grants/progress (no slow leak)
                    _perm_always.invalidate(sid)
                    _asst_epoch.pop(sid, None)
                    _asst_progress.pop(sid, None)
                    _asst_turn_done.pop(sid, None)
                    for k in [k for k, r in _perm_reqs.items()
                              if r.get("session") == sid and r.get("status") != "pending"]:
                        _perm_reqs.pop(k, None)
        finally:
            with _perm_lock:
                _deleting_sessions.discard(sid)
        return {"ok": True}

    @router.post("/api/assistant/sessions/{sid}/share")
    async def assistant_share(sid: str, request: Request):
        """Mint a read-only share link.

        The link is its own capability — a high-entropy secret kept only as a digest, with an expiry
        and a revoke — never the session id. Sharing used to flip `shared: true` and publish the id
        itself, which made the chat readable forever by anyone who saw the URL, kept publishing every
        turn the owner wrote afterwards, and could only be taken back by deleting the chat.

        Body: `ttl_seconds` (default one week) and `live`. `live` is the explicit answer to "does the
        link follow the conversation?": the default false FREEZES it at the turns that exist now, so
        continuing to talk cannot retroactively publish what comes next."""
        body = await _json_options(request)
        live = body.get("live", False)
        if not isinstance(live, bool):
            raise _share_http_error(400, "assistant_share_invalid", "live must be a boolean")
        with _session_lifecycle_lock:
            # Install a per-session fence in one short registry transaction. It prevents a turn from
            # staging its user message between our active-turn check and frozen ``upto`` while leaving
            # the global permission/progress/cancel registry available during transcript reads,
            # projection, capability-store fsync, and the legacy meta write.
            with _perm_lock:
                if sid in _deleting_sessions:
                    raise _share_http_error(
                        409, "assistant_share_session_deleting", "This chat is being deleted.",
                        remediation="Wait for deletion to finish.")
                if _asst_cancel.get(sid) is not None:
                    raise _share_http_error(
                        409, "assistant_share_turn_active",
                        "A reply is still being generated for this chat.",
                        remediation="Wait for the reply to finish or stop it before sharing.")
                _share_fenced_sessions.add(sid)
            try:
                try:
                    meta = _asst.validated_meta(sid)
                except ValueError:
                    raise _share_http_error(404, "assistant_session_not_found", "No such chat.")
                if meta is None:
                    raise _share_http_error(404, "assistant_session_not_found", "No such chat.")
                # Apply physical row/aggregate limits before JSON materialization.  The old owner
                # `_asst.get` path parsed every hidden/raw field first, so a hand-edited oversized
                # transcript could exhaust the server merely by clicking Share.
                bounded = _asst.bounded_complete_messages(
                    sid, upto=None, max_messages=_SHARED_MESSAGES_MAX,
                    max_line_bytes=_SHARED_READ_MAX_LINE_BYTES,
                    max_total_bytes=_SHARED_READ_MAX_TOTAL_BYTES,
                    report_incomplete=True)
                if bounded is None:
                    raise _share_http_error(
                        503, "assistant_share_transcript_unavailable",
                        "The chat transcript could not be read safely.",
                        remediation="Try sharing again.")
                complete, read_truncated, incomplete = bounded
                if incomplete:
                    raise _share_http_error(
                        409, "assistant_share_turn_incomplete",
                        "This chat ends with an incomplete Assistant turn.",
                        remediation="Finish or recover the pending reply before sharing.")
                share_title = _shared_title(complete)
                public_title = _shared_text(share_title, SHARE_TITLE_MAX_CHARS)
                _, projection_truncated = _shared_projection(
                    complete, initial_text=_shared_units(public_title))
                if not live:
                    if read_truncated or projection_truncated:
                        raise _share_http_error(
                            413, "assistant_share_snapshot_too_large",
                            "This chat is too large for a complete frozen public snapshot.",
                            remediation="Create an explicitly live link or share a shorter chat.")
                try:
                    token, record = _shares.create(
                        sid, message_count=len(complete), title=share_title, live=live,
                        ttl_seconds=body.get("ttl_seconds", SHARE_DEFAULT_TTL_SECONDS))
                except ShareError as exc:
                    raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
                except OSError as exc:
                    raise _share_http_error(
                        503, "assistant_share_unavailable",
                        "The public link store is unavailable. No link was published.",
                        remediation="Try sharing again.") from exc
                # This field remains a backward-compatible hint only.  If its write fails, invalidate
                # the unpublished capability; the authoritative owner list is derived from ShareStore.
                try:
                    updated = _asst.update_meta(sid, shared=True)
                except Exception as exc:  # noqa: BLE001 - disk/meta failures become a typed public error
                    _rollback_share_token(token)
                    raise _share_http_error(
                        503, "assistant_share_metadata_unavailable",
                        "The share could not be recorded safely. No link was published.",
                        remediation="Try sharing again.") from exc
                if updated is None:
                    _rollback_share_token(token)
                    raise _share_http_error(404, "assistant_session_not_found", "No such chat.")
            finally:
                with _perm_lock:
                    _share_fenced_sessions.discard(sid)
        return {"ok": True, "url": f"#/assistant/shared/{token}", "session": sid,
                "share_id": record["id"],
                "expires_at": record["expires_at"], "live": record["live"]}

    @router.delete("/api/assistant/sessions/{sid}/share")
    def assistant_unshare(sid: str):
        """Revoke every link for this chat. Revocation exists so taking a share back does not mean
        deleting the conversation; a bounded tombstone window keeps stale tokens resolving to nothing,
        and any future id reuse still carries an unrelated fresh secret."""
        with _session_lifecycle_lock:
            try:
                _asst._sdir(sid)
            except ValueError:
                raise _share_http_error(404, "assistant_session_not_found", "No such chat.")
            try:
                revoked = _shares.revoke_session(sid)
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            except OSError as exc:
                raise _share_http_error(
                    503, "assistant_unshare_unavailable",
                    "The public links could not all be revoked safely.",
                    remediation="Try unsharing again.") from exc
            # Revocation is authoritative.  Never turn a successful revoke into an ambiguous failure
            # merely because the legacy display hint could not be updated.
            try:
                _asst.update_meta(sid, shared=False)
            except OSError:
                pass
        return {"ok": True, "revoked": revoked}

    @router.get("/api/assistant/sessions/{sid}/shares")
    def assistant_shares(sid: str):
        """The owner's view of this chat's live links — never the tokens, only their terms."""
        with _session_lifecycle_lock:
            try:
                _asst._sdir(sid)
            except ValueError:
                raise _share_http_error(404, "assistant_session_not_found", "No such chat.")
            try:
                shares = _shares.active_for_session(sid)
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            return {"shares": shares}

    def _assistant_shared(token: str):
        record = _shares.resolve(token)
        if record is None:
            # One indistinguishable answer for unknown, expired, revoked and wrong-secret. Anything
            # more specific turns this public route into an oracle for which chats exist.
            raise HTTPException(404, "not shared")
        bounded = _asst.bounded_complete_messages(
            record["session"], upto=record.get("upto"), max_messages=_SHARED_MESSAGES_MAX,
            max_line_bytes=_SHARED_READ_MAX_LINE_BYTES,
            max_total_bytes=_SHARED_READ_MAX_TOTAL_BYTES)
        if bounded is None:
            raise HTTPException(404, "not shared")
        messages, read_truncated = bounded
        # The shared route is intentionally untokened beyond the capability itself. Return only what
        # the read-only transcript renders; never expose raw prompts, diff previews, absolute paths,
        # refs, or launch proposals. Sanitize FIRST (like the owner GET at assistant_get): a legacy
        # raw failure bubble embeds the provider request URL / routed model / account id, which
        # redact_secrets does NOT strip — so without this the PUBLIC share leaks provider metadata
        # the authenticated route already hides.
        public_title = _shared_text(record["title"], SHARE_TITLE_MAX_CHARS)
        projected, projection_truncated = _shared_projection(
            messages, initial_text=_shared_units(public_title))
        truncated = read_truncated or projection_truncated
        payload = {"meta": {"shared": True, "live": record["live"],
                            "expires_at": record["expires_at"],
                            "truncated": truncated,
                            # Titles are frozen from visible shared content at mint; mutable owner meta
                            # can be derived from raw attachment instructions and is never public.
                            "title": public_title},
                   "messages": projected}
        # Projection can be non-trivial.  Re-authenticate at the last possible point so an Unshare or
        # expiry that landed while we read/redacted the transcript cannot return a stale authorized DTO.
        confirmed = _shares.resolve(token)
        identity = ("id", "session", "created_at", "expires_at", "live", "upto", "title")
        if confirmed is None or any(confirmed.get(key) != record.get(key) for key in identity):
            raise HTTPException(404, "not shared")
        return payload

    @router.get("/api/assistant/shared")
    def assistant_shared_header(request: Request):
        """Header-carried public capability.

        The browser-facing share URL keeps the secret in its fragment. Carrying it in a request
        header prevents ordinary origin/proxy access logs from recording the bearer as a path.
        """
        token = request.headers.get(ASSISTANT_SHARE_HEADER, "")
        if not token:
            raise HTTPException(404, "not shared")
        return _assistant_shared(token)

    @router.post("/api/assistant/sessions/{sid}/fork")
    def assistant_fork(sid: str):
        try:
            child = _asst.fork(sid)
        except ValueError:
            raise HTTPException(404, "no such session")
        if child is None:
            raise HTTPException(404, "no such session")
        return child

    # --- shared turn prologue/epilogue (the two turn endpoints used to duplicate all of this) -----
    def _begin_turn(sid: str, body: dict):
        """Everything a turn does BEFORE the model runs, for both endpoints: session fetch/404,
        empty-message check, history snapshot, mode normalization, claiming the single active-turn
        slot, persisting the user turn (with `raw`) and resolving settings. Returns the pinned
        instruction/mode, prior history, turn owner, settings, turn id, recovery flag, and the live
        capabilities explicitly acknowledged for reply-persistence fencing."""
        instruction = (body.get("instruction") or body.get("content") or "").strip()
        # `display` (optional) is the CLEAN text the user typed; `instruction` may carry an invisible
        # UI-context preamble (open run, #experiments, files) for the model. Persist the clean bubble so
        # a page reload doesn't reveal the preamble; the model still receives the full instruction.
        display = (body.get("display") or "").strip()
        mode = body.get("mode")
        acknowledged_live_ids = _acknowledged_live_share_ids(body)
        try:
            sess = _asst.get(sid)
        except ValueError:
            raise HTTPException(404, "no such session")
        if sess is None:
            raise HTTPException(404, "no such session")
        if not instruction:
            raise HTTPException(400, "empty message")
        history = list(sess["messages"])          # BEFORE appending/recovering the new user turn
        from looplab.serve.assistant import normalize_mode as _norm_mode
        eff_mode = _norm_mode(mode or sess["meta"].get("mode") or "plan")   # fresh-turn default
        # Claim the single active-turn slot (409 if a turn is already running) BEFORE mutating the
        # transcript, so a rejected concurrent turn leaves no dangling user message — one running turn
        # per session across BOTH endpoints. The cancel event makes the turn interruptible (Stop).
        cancel_ev = threading.Event()
        turn_epoch = secrets.token_hex(16)
        # A temporarily unreadable capability store must not look private while accepting a durable
        # reply: an older live link could become readable again and publish that reply after recovery.
        # Serialize the strict store read with share/unshare/delete, then claim the turn while still
        # holding lifecycle (the established order is lifecycle -> permission registry). Once claimed,
        # a concurrent share sees the active turn and cannot slip between this check and the append.
        with _session_lifecycle_lock:
            try:
                active_shares = _shares.active_for_session(sid)
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            current_live_ids = frozenset(
                record["id"] for record in active_shares if record["live"])
            if acknowledged_live_ids != current_live_ids:
                raise _live_share_ack_required(len(current_live_ids))
            _acquire_turn(sid, cancel_ev, turn_epoch)
        # Own the slot from here: any failure BEFORE the background work takes over must release it, or
        # the session wedges at 409 forever. `_asst.append` raises ValueError if a concurrent DELETE
        # rmtree'd the session dir between `_asst.get` above and here; `_llm_settings` can also raise.
        try:
            # DELETE may have completed while this request was paused between its first read and slot
            # acquisition. Revalidate only after owning the turn; a later DELETE must wait for our done
            # event, so the append below cannot race rmtree once this check succeeds.
            if _asst.get(sid) is None:
                raise HTTPException(404, "no such session")
            shown = display or instruction
            trailing = history[-1] if history else None
            trailing_user = isinstance(trailing, dict) and trailing.get("role") == "user"
            recover_turn = bool(
                trailing_user
                and trailing.get("turn_id") and trailing.get("content") == shown)
            if recover_turn:
                # A server died after this durably staged user turn but before its assistant reply.
                # Re-run only the EXACT persisted intent with the same namespace. Matching merely
                # the clean bubble is insufficient: `display` can hide a changed model-facing raw
                # instruction, while a changed mode can promote an originally gated turn to auto.
                persisted_instruction = str(trailing.get("raw") or trailing.get("content") or "")
                persisted_mode_raw = trailing.get("mode")
                persisted_mode = _norm_mode(persisted_mode_raw)
                raw_mismatch = instruction != persisted_instruction
                mode_mismatch = mode is not None and mode != persisted_mode
                # New turn ids have always persisted a normalized mode. Treat a hand-edited/corrupt
                # value as unavailable instead of silently interpreting it as read-only plan.
                persisted_mode_invalid = persisted_mode_raw != persisted_mode
                if raw_mismatch or mode_mismatch or persisted_mode_invalid:
                    field = ("instruction" if raw_mismatch else
                             "mode" if mode_mismatch else "persisted_mode")
                    raise HTTPException(409, {
                        "code": "assistant_turn_recovery_mismatch",
                        "field": field,
                        "message": "Recovery must use the exact persisted instruction and permission mode.",
                    })
                turn_id = str(trailing["turn_id"])
                instruction = persisted_instruction
                eff_mode = persisted_mode
                history = history[:-1]
            elif trailing_user:
                # An unanswered U1 may already have completed an additive/paid run command.  Never
                # feed it to a new T2 model turn under a fresh idempotency namespace; only exact
                # recovery of U1 is safe.  Starting another session/deleting this one is the explicit
                # abandon path.
                raise HTTPException(409, {
                    "code": "assistant_turn_recovery_required",
                    "message": "The previous assistant turn has no durable reply; recover that exact turn before starting another.",
                })
            else:
                turn_id = secrets.token_hex(16)
                turn = {"role": "user", "content": shown, "mode": eff_mode, "turn_id": turn_id}
                if display and display != instruction:
                    # Keep the FULL model-facing instruction (attachments/context) beside clean copy.
                    turn["raw"] = instruction
                _asst.append(sid, turn)
            _asst.update_meta(sid, mode=eff_mode)   # remember the chosen mode so a reload/switch keeps it
            s = _llm_settings()
        except Exception:
            _release_turn(sid, cancel_ev, turn_epoch)
            raise
        return (instruction, eff_mode, history, cancel_ev, s, turn_id, recover_turn, turn_epoch,
                current_live_ids)

    def _make_progress_hooks(sid: str, cancel_ev: "threading.Event", q=None):
        """The per-turn `on_step`/`on_todos` callbacks. `q` (stream endpoint only) additionally mirrors
        each event onto the SSE queue — and its hooks carry the STRICTER owner guard (`not
        cancel_ev.is_set()`, no `updated` stamp): a cancelled worker's late tool steps must not
        re-create/refresh the progress entry after a newer turn (or the finally) popped it, else
        `progress.active` stays true forever and every future open of the session reattaches to a
        phantom "thinking" turn. The non-stream hooks keep their original owner-only guard and stamp
        `updated` — the deliberate difference is parameterized, not normalized."""
        def _on_step(ev):
            with _perm_lock:
                p = _asst_progress.get(sid)   # owner-guarded: only OUR turn touches its progress entry
                if p is not None and p.get("owner") is cancel_ev and (q is None or not cancel_ev.is_set()):
                    p["steps"] = (p["steps"] + [ev.get("label") or ev.get("tool") or "…"])[-40:]
                    if q is None:
                        p["updated"] = time.time()
            if q is not None:
                q.put((SSE_STEP, ev.get("label") or ev.get("tool") or "…"))

        def _on_todos(items):
            with _perm_lock:   # mirror todos into the progress channel so a reattach restores them
                p = _asst_progress.get(sid)
                if p is not None and p.get("owner") is cancel_ev and (q is None or not cancel_ev.is_set()):
                    p["todos"] = items
                    if q is None:
                        p["updated"] = time.time()
            if q is not None:
                q.put((SSE_TODOS, items))

        return _on_step, _on_todos

    def _append_reply_if_share_safe(sid: str, turn: dict, *, expected_len: int,
                                    begin_live_ids: frozenset[str]) -> bool:
        """Append only while the capability store remains authoritative for this turn.

        Share creation is fenced while a turn is active, but a revoke/expiry may legitimately remove
        acknowledged ids.  Therefore the current live set may shrink; it must never gain an
        unacknowledged id.  If a live capability existed at begin, disappearance of the whole store is
        ambiguous rather than proof of revocation and must leave the staged user turn recoverable.
        """
        with _session_lifecycle_lock:
            try:
                active_shares = _shares.active_for_session(
                    sid, require_store=bool(begin_live_ids))
            except ShareError as exc:
                raise _share_http_error(exc.status_code, exc.code, str(exc)) from exc
            current_live_ids = frozenset(
                record["id"] for record in active_shares if record["live"])
            if not current_live_ids.issubset(begin_live_ids):
                raise _live_share_ack_required(len(current_live_ids))
            return _asst.append_if_len(sid, turn, expected_len=expected_len)

    def _finish_turn(sid: str, history: list, instruction: str, client, res: dict,
                     best_effort_persist: bool, begin_live_ids: frozenset[str],
                     cancelled=None) -> dict:
        """Everything a turn does AFTER the model ran, for both endpoints: token accounting, the
        stale-reply-safe conditional append, and first-turn titling. `best_effort_persist` keeps the
        non-stream endpoint's swallow-and-return behavior; the stream worker lets a persistence error
        propagate to its SSE error path, exactly as before."""
        res["tokens"] = _client_tokens(client)

        def _persist() -> None:
            # Conditional append: persist the reply ONLY while the transcript still ends at OUR user
            # turn (len(history)+1) — atomically. If the user cancelled and sent a newer message,
            # appending unconditionally would interleave the transcripts (u1,u2,a1,a2) and recoverReply
            # could finalize turn 2's placeholder with turn 1's stale reply — drop the stale reply.
            ok = _append_reply_if_share_safe(
                sid, {"role": "assistant", "content": res.get("reply", ""),
                      "error_kind": res.get("error_kind"),
                      "steps": res.get("steps") or [], "applied": res.get("applied") or [],
                      "proposals": res.get("proposals") or [], "todos": res.get("todos") or [],
                      "tokens": res.get("tokens")},
                expected_len=len(history) + 1, begin_live_ids=begin_live_ids)
            if not ok:
                res["reply"] = ""   # dropped as stale; the job result / done event still returns

        if best_effort_persist:
            try:
                _persist()
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001 - persistence is best-effort; still return the reply
                pass
        else:
            _persist()
        # Titling is a SECOND paid provider call, issued after the turn's own model work. Stop had no
        # effect here, so cancelling a first turn still launched it — spending money the operator just
        # asked us not to spend, and holding the session's active slot for as long as that request hung.
        # The persistence above deliberately still runs: the reply was already produced and paid for.
        if cancelled is not None and cancelled():
            return res
        if not history:             # first turn -> title the session from the exchange (cheap)
            try:
                title = client.complete_text([
                    {"role": "system", "content": "Reply with a SHORT title (<= 6 words, no "
                     "quotes) for this chat based on the user's first message."},
                    {"role": "user", "content": instruction[:400]}]).strip().strip('"')[:60]
                if title:
                    _asst.update_meta(sid, title=title)
            except Exception:  # noqa: BLE001 - titling is best-effort
                pass
        return res

    @router.post("/api/assistant/sessions/{sid}/message")
    async def assistant_message(sid: str, request: Request):
        """One assistant turn. Persists the user turn, drives the read-only tool loop as a BACKGROUND
        JOB (so a long turn returns {status:'running', job_id} the UI awaits via jobAwait instead of
        504ing), then persists the assistant reply. Soft-fails offline."""
        body = await _json_object(request)
        (instruction, eff_mode, history, cancel_ev, s, turn_id, recover_turn,
         turn_epoch, begin_live_ids) = _begin_turn(sid, body)
        try:
            from looplab.core.llm import make_llm_client_for
            client = make_llm_client_for(s, factory=srv.make_llm_client)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail with a usable message
            failure = _safe_assistant_failure(e)
            try:
                _append_reply_if_share_safe(
                    sid, {"role": "assistant", "content": failure["reply"],
                          "error_kind": failure["error_kind"]},
                    expected_len=len(history) + 1, begin_live_ids=begin_live_ids)
            finally:
                _release_turn(sid, cancel_ev, turn_epoch)
            return {"ok": False, **failure, "mode": eff_mode}

        approver = _make_approver(
            sid, eff_mode, turn_epoch, cancel_ev)   # a confirm after Stop denies instantly
        _on_step, _on_todos = _make_progress_hooks(sid, cancel_ev)

        def _compute() -> dict:
            try:
                res = _assistant_run_turn(client, root, history, instruction, eff_mode,
                                          alive_fn=_engine_alive, settings=s, approver=approver,
                                          on_step=_on_step, on_todos=_on_todos,
                                           cancel_check=cancel_ev.is_set,
                                           command_service=srv.commands,
                                           command_key_namespace=f"{sid}:{turn_id}",
                                           mutation_journal_path=_asst.mutation_journal_path(sid, turn_id),
                                           mutation_recovery=recover_turn)
                return _finish_turn(sid, history, instruction, client, res,
                                    best_effort_persist=True,
                                    begin_live_ids=begin_live_ids,
                                    cancelled=cancel_ev.is_set)
            finally:
                _release_turn(sid, cancel_ev, turn_epoch)

        return await srv.jobs.run_as_job(_compute)

    @router.post("/api/assistant/sessions/{sid}/message_stream")
    async def assistant_message_stream(sid: str, request: Request):
        """Streaming variant: SSE of `token` (final-answer tokens), `step`, `todos`, then `done` (the
        full result) — real token streaming for the Claude-Desktop feel. HITL still works: a mutating
        action pauses the worker on the permission registry while the client polls /permissions."""
        import queue as _queue
        body = await _json_object(request)
        (instruction, eff_mode, history, cancel_ev, s, turn_id, recover_turn,
         turn_epoch, begin_live_ids) = _begin_turn(sid, body)
        q: "_queue.Queue" = _queue.Queue()
        try:
            from looplab.core.llm import make_llm_client_for
            client = make_llm_client_for(s, factory=srv.make_llm_client)
        except Exception as e:  # noqa: BLE001 - offline -> stream a single error event
            # Persist an assistant error bubble too (mirror the non-stream endpoint) so a page reload
            # shows the failure instead of a DANGLING user turn — the user's message with no reply,
            # since _begin_turn already appended the user turn before make_llm_client.
            failure = _safe_assistant_failure(e)
            try:
                _append_reply_if_share_safe(
                    sid, {"role": "assistant", "content": failure["reply"],
                          "error_kind": failure["error_kind"]},
                    expected_len=len(history) + 1, begin_live_ids=begin_live_ids)
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001 - a concurrent session DELETE must not turn this into a 500
                pass
            finally:
                _release_turn(sid, cancel_ev, turn_epoch)
            err_msg = failure["reply"]

            async def _err_gen():
                yield f"event: {SSE_ERROR}\ndata: {json.dumps(err_msg)}\n\n"
            return StreamingResponse(_err_gen(), media_type="text/event-stream")
        approver = _make_approver(
            sid, eff_mode, turn_epoch, cancel_ev)   # a confirm after Stop denies instantly
        _on_step, _on_todos = _make_progress_hooks(sid, cancel_ev, q)

        def _progress_text(piece):
            # Mirror the live assistant output into the POLLED progress channel too, not only the SSE
            # queue: behind a buffering proxy (jupyter-server-proxy / nginx) the token/text SSE events
            # arrive batched at the very END, so a client that also polls /progress still watches the
            # answer form live. Owner-guarded + last-8KB capped like `steps`.
            if not piece:
                return
            with _perm_lock:
                p = _asst_progress.get(sid)
                if p is not None and p.get("owner") is cancel_ev and not cancel_ev.is_set():
                    p["text"] = (p.get("text", "") + piece)[-8000:]

        def _on_text(content):
            q.put((SSE_TEXT, content))        # interstitial assistant prose (between tool rounds)
            _progress_text(content)

        def _reply_sink(piece):
            q.put((SSE_TOKEN, piece))
            _progress_text(piece)

        def _worker():
            try:
                res = _assistant_run_turn(client, root, history, instruction, eff_mode,
                                          alive_fn=_engine_alive, settings=s, approver=approver,
                                          on_step=_on_step, on_todos=_on_todos, on_text=_on_text,
                                          reply_sink=_reply_sink,
                                           cancel_check=cancel_ev.is_set,
                                           command_service=srv.commands,
                                           command_key_namespace=f"{sid}:{turn_id}",
                                           mutation_journal_path=_asst.mutation_journal_path(sid, turn_id),
                                           mutation_recovery=recover_turn)
                res = _finish_turn(sid, history, instruction, client, res,
                                   best_effort_persist=False,
                                   begin_live_ids=begin_live_ids,
                                   cancelled=cancel_ev.is_set)
                q.put((SSE_DONE, {k: res.get(k) for k in
                                  ("reply", "steps", "applied", "proposals", "todos", "refs", "tokens", "mode")}))
            except Exception as e:  # noqa: BLE001
                # Worker failures may carry provider URLs, routed model/account metadata or
                # credential fragments.  SSE is part of the public API contract too: keep its
                # historical string payload, but emit only the allow-listed assistant message.
                q.put((SSE_ERROR, _safe_assistant_failure(e)["reply"]))
            finally:
                # Tear down ONLY OUR turn's registry entries (owner-guarded): a newer turn may have
                # replaced them, and popping unconditionally would orphan it.
                _release_turn(sid, cancel_ev, turn_epoch)
                q.put((ASSISTANT_STREAM_END_SENTINEL, None))
        threading.Thread(target=_worker, daemon=True).start()

        async def gen():
            # Defeat proxy buffering: jupyter-server-proxy/tornado (and any nginx hop) can hold a small
            # initial buffer, so step/text/token events arrive BATCHED at the end instead of live. A
            # >2KB first write forces the buffer to flush immediately; then poll the queue with a timeout
            # so a long or stalled LLM call still emits a keepalive comment (the proxy's idle read-timer
            # never fires, and a dead client surfaces promptly on the failed write).
            yield ": " + " " * 2048 + "\n\n"
            # Drain the worker's queue ON THE EVENT LOOP. This used to be
            # `await anyio.to_thread.run_sync(lambda: q.get(timeout=10))`, which parked one anyio
            # threadpool worker per open stream for essentially the whole turn — and FastAPI's sync
            # `def` routes and every other `anyio.to_thread` caller (the runs SSE `_state_payload`,
            # the boss offloads, the settings/project writes) share that same default 40-token pool,
            # so a few dozen concurrent assistant streams starved the entire server. A non-blocking
            # `get_nowait` costs no pool token; the sleep runs ONLY when the worker has produced
            # nothing, so it bounds idle latency and never throttles a stream that is emitting.
            idle = 0.0
            while True:
                try:
                    kind, data = q.get_nowait()
                except _queue.Empty:
                    await anyio.sleep(_ASSISTANT_STREAM_POLL_SECONDS)
                    idle += _ASSISTANT_STREAM_POLL_SECONDS
                    if idle >= _ASSISTANT_STREAM_KEEPALIVE_SECONDS:
                        idle = 0.0
                        yield ": keepalive\n\n"
                    continue
                idle = 0.0
                if kind == ASSISTANT_STREAM_END_SENTINEL:
                    break
                yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
