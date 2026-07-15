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
    REPO_ROOT as _ASSISTANT_REPO_ROOT, SessionStore, run_turn as _assistant_run_turn,
    safe_assistant_failure as _safe_assistant_failure,
    sanitize_assistant_message as _sanitize_assistant_message)
from looplab.serve.engine_proc import _engine_alive
from looplab.serve.llm_context import _client_tokens
from looplab.serve.protocol import (
    ASSISTANT_STREAM_END_SENTINEL, PERM_ALLOW_ALWAYS, PERM_ALLOW_ONCE, PERM_DENY,
    SSE_DONE, SSE_ERROR, SSE_STEP, SSE_TEXT, SSE_TODOS, SSE_TOKEN)
from looplab.tools.perm_modes import (
    GRANT_TTL_SECONDS, RememberedGrantStore, classify_action, normalize_mode)
from looplab.trust.redact import redact_secrets


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


def _shared_text(value) -> str:
    from looplab.trust.redact import redact_secrets
    return redact_secrets(str(value or ""))


def _shared_message(message: dict) -> dict:
    """Allow-list the fields rendered by the read-only shared transcript. Persisted assistant turns
    also contain mutation internals (absolute paths and full diff previews); merely dropping `raw`
    exposes those through the intentionally untokened share URL."""
    out = {
        "role": str(message.get("role") or "assistant"),
        "content": _shared_text(message.get("content")),
    }
    if isinstance(message.get("ts"), (int, float)):
        out["ts"] = message["ts"]
    if isinstance(message.get("mode"), str):
        out["mode"] = message["mode"]

    def _labels(items):
        return [{k: _shared_text(item.get(k)) for k in ("label", "tool") if item.get(k)}
                for item in (items or []) if isinstance(item, dict)]

    if isinstance(message.get("steps"), list):
        out["steps"] = _labels(message["steps"])
    if isinstance(message.get("applied"), list):
        out["applied"] = _labels(message["applied"])
    if isinstance(message.get("todos"), list):
        out["todos"] = [
            {"content": _shared_text(t.get("content")),
             "status": str(t.get("status") or "pending")}
            for t in message["todos"] if isinstance(t, dict)
        ]
    if isinstance(message.get("tokens"), dict):
        out["tokens"] = {str(k): v for k, v in message["tokens"].items()
                         if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if isinstance(message.get("activity"), list):
        activity = []
        for item in message["activity"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                activity.append({"type": "text", "content": _shared_text(item.get("content"))})
            elif item.get("type") == "tools":
                activity.append({"type": "tools",
                                 "labels": [_shared_text(v) for v in (item.get("labels") or [])]})
        out["activity"] = activity
    return out


def build_router(srv) -> APIRouter:
    router = APIRouter()
    root = srv.root
    _llm_settings = srv.llm_settings

    # ------------------------------------------------------------------ assistant (general chat agent)
    _asst = SessionStore(root)

    # --- pause-resume human-in-the-loop permission registry -------------------------------------
    # A mutating tool in a confirm mode calls the injected approver, which registers a request here
    # and BLOCKS the (background) turn thread on an Event; the UI polls GET .../permissions, shows a
    # confirm-card, and POST .../permissions/{id} sets the decision + unblocks the thread. This keeps
    # the synchronous drive_tool_loop unchanged while giving true mid-loop approval (the tool sees the
    # REAL result). "allow_always" is exact-action/scope, mode, and turn-epoch bound — never a broad
    # session→tool-kind bypass.
    _perm_lock = threading.Lock()
    _perm_reqs: dict = {}
    _perm_always = RememberedGrantStore()
    _asst_progress: dict = {}      # sid -> {steps:[label,…], updated} — live tool steps during a turn
    _asst_cancel: dict = {}        # sid -> threading.Event — set by /cancel to stop an in-flight turn
    _asst_epoch: dict = {}         # sid -> opaque random epoch owned by the active cancel event
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
            existing = _asst_cancel.get(sid)
            # A set cancel flag means "stopping", not "finished": the old worker may still be inside
            # an uninterruptible model/tool call and may already own a durable run command.  Keep the
            # slot until its finally calls `_release_turn` so U2 cannot overtake the missing A1.
            if existing is not None:
                raise HTTPException(409, "a turn is already running for this session")
            _asst_cancel[sid] = cancel_ev
            _asst_epoch[sid] = epoch
            _asst_progress[sid] = {"steps": [], "todos": [], "text": "", "updated": time.time(),
                                   "owner": cancel_ev, "epoch": epoch}

    def _release_turn(sid: str, cancel_ev: "threading.Event", epoch: str) -> None:
        """Tear down ONLY this turn's registry entries (a newer turn may have replaced them): pop the
        cancel event and progress entry only when `cancel_ev` still owns them."""
        with _perm_lock:
            if _asst_cancel.get(sid) is cancel_ev:
                _asst_cancel.pop(sid, None)
                _asst_epoch.pop(sid, None)
                _perm_always.invalidate(sid, epoch=epoch)
            p = _asst_progress.get(sid)
            if p is not None and p.get("owner") is cancel_ev:
                _asst_progress.pop(sid, None)

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
        """Undo the assistant's most recent change to a file (restore the pre-edit snapshot)."""
        body = await _json_object(request)
        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "path is required")
        from looplab.tools.write_tools import WriteTools
        wt = WriteTools([Path.home(), _ASSISTANT_REPO_ROOT, root], mode="auto",
                        repo_root=_ASSISTANT_REPO_ROOT, backup_dir=root / "assistant" / "backups")
        return {"ok": True, "result": wt.revert(path)}

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
        return {"sessions": _asst.list()}

    @router.post("/api/assistant/sessions")
    async def assistant_create(request: Request):
        body = await _json_object(request)
        meta = _asst.create(title=(body.get("title") or ""),
                            mode=body.get("mode") or "plan")
        return meta

    @router.get("/api/assistant/sessions/{sid}")
    def assistant_get(sid: str):
        try:
            sess = _asst.get(sid)
        except ValueError:
            raise HTTPException(404, "no such session")
        if sess is None:
            raise HTTPException(404, "no such session")
        sess = {**sess, "messages": [_sanitize_assistant_message(m) for m in sess["messages"]]}
        return sess

    @router.delete("/api/assistant/sessions/{sid}")
    def assistant_delete(sid: str):
        import shutil
        try:
            d = _asst._sdir(sid)
        except ValueError:
            raise HTTPException(404, "no such session")
        # Stop an in-flight turn on this session before removing its files: cancel the loop AND unblock
        # a worker parked on a confirm — else it runs against a now-deleted session for up to 900s.
        with _perm_lock:
            cev = _asst_cancel.get(sid)
            if cev is not None:
                cev.set()
            _deny_session_perms_locked(sid)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        with _perm_lock:      # drop the deleted session's in-memory grants/progress (no slow leak)
            _perm_always.invalidate(sid)
            _asst_epoch.pop(sid, None)
            _asst_progress.pop(sid, None)
            for k in [k for k, r in _perm_reqs.items()
                      if r.get("session") == sid and r.get("status") != "pending"]:
                _perm_reqs.pop(k, None)
        return {"ok": True}

    @router.post("/api/assistant/sessions/{sid}/share")
    def assistant_share(sid: str):
        meta = _asst.update_meta(sid, shared=True)
        if meta is None:
            raise HTTPException(404, "no such session")
        return {"ok": True, "url": f"#/assistant/shared/{sid}", "session": sid}

    @router.get("/api/assistant/shared/{sid}")
    def assistant_shared(sid: str):
        try:
            sess = _asst.get(sid)
        except ValueError:
            raise HTTPException(404, "no such session")
        if sess is None or not sess["meta"].get("shared"):
            raise HTTPException(404, "not shared")
        # The shared route is intentionally untokened. Return only what the read-only transcript
        # renders; never expose raw prompts, diff previews, absolute paths, refs, or launch proposals.
        # Sanitize FIRST (like the owner GET at assistant_get): a legacy raw failure bubble embeds the
        # provider request URL / routed model / account id, which redact_secrets does NOT strip — so
        # without this the PUBLIC share leaks provider metadata the authenticated route already hides.
        meta = {"shared": True, "title": _shared_text(sess["meta"].get("title") or "Shared chat")}
        return {"meta": meta,
                "messages": [_shared_message(_sanitize_assistant_message(m)) for m in sess["messages"]]}

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
        instruction/mode, prior history, turn owner, settings, turn id, and recovery flag."""
        instruction = (body.get("instruction") or body.get("content") or "").strip()
        # `display` (optional) is the CLEAN text the user typed; `instruction` may carry an invisible
        # UI-context preamble (open run, #experiments, files) for the model. Persist the clean bubble so
        # a page reload doesn't reveal the preamble; the model still receives the full instruction.
        display = (body.get("display") or "").strip()
        mode = body.get("mode")
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
        _acquire_turn(sid, cancel_ev, turn_epoch)
        # Own the slot from here: any failure BEFORE the background work takes over must release it, or
        # the session wedges at 409 forever. `_asst.append` raises ValueError if a concurrent DELETE
        # rmtree'd the session dir between `_asst.get` above and here; `_llm_settings` can also raise.
        try:
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
        return instruction, eff_mode, history, cancel_ev, s, turn_id, recover_turn, turn_epoch

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

    def _finish_turn(sid: str, history: list, instruction: str, client, res: dict,
                     best_effort_persist: bool) -> dict:
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
            ok = _asst.append_if_len(
                sid, {"role": "assistant", "content": res.get("reply", ""),
                      "error_kind": res.get("error_kind"),
                      "steps": res.get("steps") or [], "applied": res.get("applied") or [],
                      "proposals": res.get("proposals") or [], "todos": res.get("todos") or [],
                      "tokens": res.get("tokens")},
                expected_len=len(history) + 1)
            if not ok:
                res["reply"] = ""   # dropped as stale; the job result / done event still returns

        if best_effort_persist:
            try:
                _persist()
            except Exception:  # noqa: BLE001 - persistence is best-effort; still return the reply
                pass
        else:
            _persist()
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
         turn_epoch) = _begin_turn(sid, body)
        try:
            client = srv.make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail with a usable message
            _release_turn(sid, cancel_ev, turn_epoch)
            failure = _safe_assistant_failure(e)
            _asst.append(sid, {"role": "assistant", "content": failure["reply"],
                               "error_kind": failure["error_kind"]})
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
                                    best_effort_persist=True)
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
         turn_epoch) = _begin_turn(sid, body)
        q: "_queue.Queue" = _queue.Queue()
        try:
            client = srv.make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline -> stream a single error event
            _release_turn(sid, cancel_ev, turn_epoch)
            # Persist an assistant error bubble too (mirror the non-stream endpoint) so a page reload
            # shows the failure instead of a DANGLING user turn — the user's message with no reply,
            # since _begin_turn already appended the user turn before make_llm_client.
            failure = _safe_assistant_failure(e)
            try:
                _asst.append(sid, {"role": "assistant", "content": failure["reply"],
                                   "error_kind": failure["error_kind"]})
            except Exception:  # noqa: BLE001 - a concurrent session DELETE must not turn this into a 500
                pass
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
                                   best_effort_persist=False)
                q.put((SSE_DONE, {k: res.get(k) for k in
                                  ("reply", "steps", "applied", "proposals", "todos", "refs", "tokens", "mode")}))
            except Exception as e:  # noqa: BLE001
                q.put((SSE_ERROR, str(e)))
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
            while True:
                try:
                    kind, data = await anyio.to_thread.run_sync(lambda: q.get(timeout=10))
                except _queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if kind == ASSISTANT_STREAM_END_SENTINEL:
                    break
                yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
