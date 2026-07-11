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
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from looplab.serve.assistant import (
    REPO_ROOT as _ASSISTANT_REPO_ROOT, SessionStore, run_turn as _assistant_run_turn)
from looplab.serve.engine_proc import _engine_alive
from looplab.serve.llm_context import _client_tokens
from looplab.serve.protocol import (
    ASSISTANT_STREAM_END_SENTINEL, PERM_ALLOW_ALWAYS, PERM_ALLOW_ONCE, PERM_DENY,
    SSE_DONE, SSE_ERROR, SSE_STEP, SSE_TEXT, SSE_TODOS, SSE_TOKEN)


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
    # REAL result). "allow_always" remembers the tool kind for the session so it stops asking.
    _perm_lock = threading.Lock()
    _perm_reqs: dict = {}
    _perm_always: dict = {}
    _asst_progress: dict = {}      # sid -> {steps:[label,…], updated} — live tool steps during a turn
    _asst_cancel: dict = {}        # sid -> threading.Event — set by /cancel to stop an in-flight turn
    _PERM_TIMEOUT = float(os.environ.get("LOOPLAB_ASSISTANT_PERM_TIMEOUT", "900"))

    def _make_approver(sid: str, cancel_ev: "Optional[threading.Event]" = None):
        def approver(action: dict) -> str:
            # A confirm raised AFTER the user hit Stop (the in-flight model response's tools still
            # execute once) must not park the worker for _PERM_TIMEOUT on a card nobody will see —
            # deny immediately, so the loop's next cancel_check ends the turn.
            if cancel_ev is not None and cancel_ev.is_set():
                return PERM_DENY
            kind = (action or {}).get("tool_kind", "")
            with _perm_lock:
                if kind and kind in _perm_always.get(sid, set()):
                    return PERM_ALLOW_ALWAYS
            req_id = secrets.token_hex(8)
            ev = threading.Event()
            safe_action = {k: v for k, v in (action or {}).items()
                           if k in ("tool", "tool_kind", "label", "verb", "path", "preview", "cwd")}
            with _perm_lock:
                _perm_reqs[req_id] = {"id": req_id, "session": sid, "action": safe_action,
                                      "status": "pending", "decision": None, "event": ev,
                                      "created": time.time()}
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
                if dec == PERM_ALLOW_ALWAYS and kind:
                    _perm_always.setdefault(sid, set()).add(kind)
            return dec
        return approver

    def _acquire_turn(sid: str, cancel_ev: "threading.Event"):
        """Claim the SINGLE active-turn slot for a session. Two concurrent turns on one session (two
        tabs, or the stream + non-stream endpoints) otherwise interleave: the second reads a `history`
        missing the first's reply, `_asst_cancel[sid]` gets clobbered so Stop hits the wrong turn, and
        replies append in completion order (u1,u2,a2,a1). Reject the second with 409 instead. Registers
        the cancel event + a fresh progress entry, both owned by `cancel_ev`, atomically under the
        lock."""
        with _perm_lock:
            existing = _asst_cancel.get(sid)
            if existing is not None and not existing.is_set():
                raise HTTPException(409, "a turn is already running for this session")
            _asst_cancel[sid] = cancel_ev
            _asst_progress[sid] = {"steps": [], "todos": [], "text": "", "updated": time.time(),
                                   "owner": cancel_ev}

    def _release_turn(sid: str, cancel_ev: "threading.Event") -> None:
        """Tear down ONLY this turn's registry entries (a newer turn may have replaced them): pop the
        cancel event and progress entry only when `cancel_ev` still owns them."""
        with _perm_lock:
            if _asst_cancel.get(sid) is cancel_ev:
                _asst_cancel.pop(sid, None)
            p = _asst_progress.get(sid)
            if p is not None and p.get("owner") is cancel_ev:
                _asst_progress.pop(sid, None)

    def _deny_session_perms(sid: str) -> None:
        """Auto-deny + UNBLOCK every pending permission request for a session. Called on cancel/delete
        so a worker parked in `approver.ev.wait` un-parks immediately (returns "deny" → the mutating
        tool is NOT executed → the loop's next cancel_check ends the turn), instead of blocking for
        _PERM_TIMEOUT. Also flips the request to resolved so a stopped turn's confirm can't be
        re-surfaced and approved later to fire a mutation the user already cancelled."""
        with _perm_lock:
            for r in _perm_reqs.values():
                if r.get("session") == sid and r.get("status") == "pending":
                    r["decision"] = PERM_DENY
                    r["status"] = "resolved"
                    r["event"].set()

    @router.get("/api/assistant/permissions")
    def assistant_permissions(session: Optional[str] = None):
        with _perm_lock:
            out = [{"id": r["id"], "session": r["session"], "action": r["action"],
                    "created": r["created"]}
                   for r in _perm_reqs.values()
                   if r["status"] == "pending" and (not session or r["session"] == session)]
        return {"pending": out}

    @router.post("/api/assistant/permissions/{req_id}")
    async def assistant_resolve(req_id: str, request: Request):
        body = await request.json()
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
        body = await request.json()
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
        _deny_session_perms(sid)   # un-park a worker waiting on a confirm; kill the stale request
        if ev is not None:
            ev.set()
            return {"ok": True, "cancelling": True}
        return {"ok": True, "cancelling": False}   # nothing running for this session

    @router.get("/api/assistant/sessions")
    def assistant_sessions():
        return {"sessions": _asst.list()}

    @router.post("/api/assistant/sessions")
    async def assistant_create(request: Request):
        body = await request.json()
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
        _deny_session_perms(sid)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        with _perm_lock:      # drop the deleted session's in-memory grants/progress (no slow leak)
            _perm_always.pop(sid, None)
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
        # Strip `raw` (the full model-facing instruction: attached-file contents, UI-context preamble)
        # from the read-only share — a shared link shows the clean bubbles, not the injected context.
        return {"meta": sess["meta"],
                "messages": [{k: v for k, v in m.items() if k != "raw"} for m in sess["messages"]]}

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
        slot, persisting the user turn (with `raw`) and resolving settings. Returns
        (instruction, eff_mode, history, cancel_ev, settings)."""
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
        history = list(sess["messages"])          # BEFORE appending the new user turn
        from looplab.serve.assistant import normalize_mode as _norm_mode
        eff_mode = _norm_mode(mode or sess["meta"].get("mode") or "plan")   # never persist a junk mode
        # Claim the single active-turn slot (409 if a turn is already running) BEFORE mutating the
        # transcript, so a rejected concurrent turn leaves no dangling user message — one running turn
        # per session across BOTH endpoints. The cancel event makes the turn interruptible (Stop).
        cancel_ev = threading.Event()
        _acquire_turn(sid, cancel_ev)
        # Own the slot from here: any failure BEFORE the background work takes over must release it, or
        # the session wedges at 409 forever. `_asst.append` raises ValueError if a concurrent DELETE
        # rmtree'd the session dir between `_asst.get` above and here; `_llm_settings` can also raise.
        try:
            turn = {"role": "user", "content": display or instruction, "mode": eff_mode}
            if display and display != instruction:
                # Keep the FULL model-facing instruction (attached-file contents, UI-context preamble)
                # alongside the clean bubble: later turns rebuild the model's history from this
                # transcript, and an attachment exists nowhere else (it was read in the browser). The UI
                # renders `content` only, so the bubble stays clean.
                turn["raw"] = instruction
            _asst.append(sid, turn)
            _asst.update_meta(sid, mode=eff_mode)   # remember the chosen mode so a reload/switch keeps it
            s = _llm_settings()
        except Exception:
            _release_turn(sid, cancel_ev)
            raise
        return instruction, eff_mode, history, cancel_ev, s

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
        body = await request.json()
        instruction, eff_mode, history, cancel_ev, s = _begin_turn(sid, body)
        try:
            client = srv.make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail with a usable message
            _release_turn(sid, cancel_ev)
            reply = f"Couldn't reach the model ({e})."
            _asst.append(sid, {"role": "assistant", "content": reply})
            return {"ok": False, "error": str(e), "reply": reply, "mode": eff_mode}

        approver = _make_approver(sid, cancel_ev)   # a confirm raised after Stop denies instantly
        _on_step, _on_todos = _make_progress_hooks(sid, cancel_ev)

        def _compute() -> dict:
            try:
                res = _assistant_run_turn(client, root, history, instruction, eff_mode,
                                          alive_fn=_engine_alive, settings=s, approver=approver,
                                          on_step=_on_step, on_todos=_on_todos,
                                          cancel_check=cancel_ev.is_set)
                return _finish_turn(sid, history, instruction, client, res,
                                    best_effort_persist=True)
            finally:
                _release_turn(sid, cancel_ev)

        return await srv.jobs.run_as_job(_compute)

    @router.post("/api/assistant/sessions/{sid}/message_stream")
    async def assistant_message_stream(sid: str, request: Request):
        """Streaming variant: SSE of `token` (final-answer tokens), `step`, `todos`, then `done` (the
        full result) — real token streaming for the Claude-Desktop feel. HITL still works: a mutating
        action pauses the worker on the permission registry while the client polls /permissions."""
        import queue as _queue
        body = await request.json()
        instruction, eff_mode, history, cancel_ev, s = _begin_turn(sid, body)
        q: "_queue.Queue" = _queue.Queue()
        try:
            client = srv.make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline -> stream a single error event
            _release_turn(sid, cancel_ev)
            # Persist an assistant error bubble too (mirror the non-stream endpoint) so a page reload
            # shows the failure instead of a DANGLING user turn — the user's message with no reply,
            # since _begin_turn already appended the user turn before make_llm_client.
            try:
                _asst.append(sid, {"role": "assistant", "content": f"Couldn't reach the model ({e})."})
            except Exception:  # noqa: BLE001 - a concurrent session DELETE must not turn this into a 500
                pass
            err_msg = str(e)

            async def _err_gen():
                yield f"event: {SSE_ERROR}\ndata: {json.dumps(err_msg)}\n\n"
            return StreamingResponse(_err_gen(), media_type="text/event-stream")
        approver = _make_approver(sid, cancel_ev)   # a confirm raised AFTER Stop denies instantly
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
                                          cancel_check=cancel_ev.is_set)
                res = _finish_turn(sid, history, instruction, client, res,
                                   best_effort_persist=False)
                q.put((SSE_DONE, {k: res.get(k) for k in
                                  ("reply", "steps", "applied", "proposals", "todos", "refs", "tokens", "mode")}))
            except Exception as e:  # noqa: BLE001
                q.put((SSE_ERROR, str(e)))
            finally:
                # Tear down ONLY OUR turn's registry entries (owner-guarded): a newer turn may have
                # replaced them, and popping unconditionally would orphan it.
                _release_turn(sid, cancel_ev)
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
