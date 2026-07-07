"""BOSS routes for a live run: the persisted chat transcript, advisory chat, idea suggestion, the
action-router (/command) and the manual report refresh — plus the `_Plan`/`_Action` models and
their control-event mapping. Bodies are verbatim moves from `serve/server.py` (BACKLOG §4);
`looplab.serve.server` re-exports `_Action`/`_Plan`/`_action_to_control`/`_plan_to_actions` so the
historical `looplab.server._Action` import path keeps working for tests and callers."""
from __future__ import annotations

from typing import Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from looplab.core.atomicio import best_effort_fsync
from looplab.events.eventstore import EventStore, iter_jsonl
from looplab.events.replay import fold
from looplab.events.types import (
    EV_ANNOTATION, EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_DEEP_RESEARCH,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT,
    EV_INJECT_NODE, EV_NODE_RESET, EV_PAUSE, EV_PROMOTE,
    EV_REPORT_GENERATED, EV_RESUME, EV_RUN_ABORT, EV_SET_STRATEGY,
    EV_SPEC_APPROVED)
from looplab.serve.llm_context import _boss_context, _client_tokens, _node_context
from looplab.serve.serve_prompts import CHAT_SYSTEM, COMMAND_SYSTEM, COMPACT_SYSTEM

# Workstream C → agentic boss: the chat action-router LLM emits a _Plan — a short conversational reply
# plus an ORDERED list of _Action steps. Each _Action maps to a control {type, data} the UI applies
# (boss mode auto-applies them in order, then reopens/resumes the run ONCE if any step needs the
# engine). An empty actions list = pure conversation (only the reply is shown).
from pydantic import BaseModel  # noqa: E402


class _Action(BaseModel):
    action: str = "advise"   # advise|confirm|ablate|fork|promote|reset|hint|note|strategy|budget|deep_research|inject|import|approve|ratify|stop|finalize|resume
    node_id: Optional[int] = None
    text: str = ""           # hint text / note text / free rationale
    stage: str = ""          # reset: propose|implement|eval — which stage to re-run the node FROM
    operator: str = "improve"
    params: dict = {}
    policy: str = ""
    fidelity: str = ""
    source_run: str = ""     # import: the SIBLING run to seed an experiment from
    source_node: Optional[int] = None   # import: which experiment (node id) of that sibling run
    nodes: int = 0           # budget: add this many experiment nodes to the run's node budget
    rationale: str = ""      # one-line why for THIS step (shown on its applied row)


class _Plan(BaseModel):
    reply: str = ""               # conversational narration to the human (always shown)
    actions: list[_Action] = []   # ordered steps to apply; empty list = pure advice


def _action_to_control(c: "_Action", st) -> Optional[dict]:
    """Map ONE classified boss action to a control {type, data, label}. None => no actionable verb
    (a pure-advice step, skipped). `label` is the human-readable line the UI's applied-row shows."""
    a, nid = c.action, c.node_id
    if a == "confirm" and nid is not None:
        return {"type": EV_FORCE_CONFIRM, "data": {"node_id": nid}, "label": f"Confirm #{nid} (multi-seed robustness)"}
    if a == "ablate" and nid is not None:
        return {"type": EV_FORCE_ABLATE, "data": {"node_id": nid}, "label": f"Ablate #{nid} (sensitivity probe)"}
    if a == "fork" and nid is not None:
        return {"type": EV_FORK, "data": {"from_node_id": nid}, "label": f"Fork an improve-branch from #{nid}"}
    if a == "promote" and nid is not None:
        return {"type": EV_PROMOTE, "data": {"node_id": nid, "alias": "champion"}, "label": f"Promote #{nid} to champion"}
    if a == "reset" and nid is not None:
        # Re-run an EXISTING node in place (never a new node). Stage picks how far back to rewind:
        # propose (full re-do) / implement (keep idea, re-develop) / eval (re-score, keep code) — OR the
        # name of an eval-PIPELINE stage (train / data_prep / …) to restart the pipeline from, reusing
        # earlier stages' artifacts.
        stage = (c.stage or "eval").strip()
        how = {"propose": "re-propose from scratch", "implement": "re-run the Developer (keep the idea)",
               "eval": "re-score (keep the code)"}.get(stage.lower(),
               f"re-run the eval pipeline from '{stage}' (reuse earlier stages)")
        return {"type": EV_NODE_RESET, "data": {"node_id": nid, "from_stage": stage},
                "label": f"Reset #{nid} in place — {how}"}
    if a == "hint" and c.text:
        # The boss authors the COMPLETE current directive each turn (it has the full chat + run
        # context), so a boss hint REPLACES the standing one rather than piling up — the researcher/
        # agent/strategist then read a single, current directive instead of a contradictory stack.
        return {"type": EV_HINT, "data": {"text": c.text, "replace": True},
                "label": f"Set directive: {c.text[:60]}"}
    if a in ("note", "annotate") and nid is not None and c.text:
        return {"type": EV_ANNOTATION, "data": {"node_id": nid, "text": c.text}, "label": f"Note on #{nid}: {c.text[:50]}"}
    if a in ("budget", "extend_budget"):
        n = int(c.nodes)
        if n <= 0:                       # a budget verb only ADDS room — ignore zero/negative (no-op)
            return None
        n = min(n, 1000)                 # cap a hallucinated huge delta so the LLM can't trigger a runaway
        return {"type": EV_BUDGET_EXTEND, "data": {"add_nodes": n},
                "label": f"Extend the run budget by {n} node(s)"}
    if a == "strategy" and (c.policy or c.fidelity):
        strat = {k: v for k, v in (("policy", c.policy), ("fidelity", c.fidelity)) if v}
        pretty = " ".join(f"{k}={v}" for k, v in strat.items())   # "policy=ucb fidelity=low", not a dict repr
        return {"type": EV_SET_STRATEGY, "data": {"strategy": strat}, "label": f"Switch strategy → {pretty}"}
    if a == "deep_research":
        return {"type": EV_DEEP_RESEARCH, "data": {}, "label": "Run a deep-research step now"}
    if a == "inject":
        idea = {"operator": c.operator or "improve", "params": c.params or {}, "rationale": c.rationale or c.text or ""}
        pp = " ".join(f"{k}={v}" for k, v in (idea["params"] or {}).items())   # "lr=0.1 depth=3", not a dict repr
        return {"type": EV_INJECT_NODE, "data": {"idea": idea, "parent_id": nid, "code": None},
                "label": f"Add experiment: {idea['operator']}" + (f" ({pp})" if pp else "")}
    if a == "import" and c.source_run and c.source_node is not None:
        # Seed an experiment FROM a sibling run. The source idea/code/metric are resolved from disk at
        # apply time (in /control), which then bakes `origin` provenance into the inject_node event —
        # so this rides the existing manual-injection pipeline, no new event type.
        return {"type": EV_INJECT_NODE,
                "data": {"idea": {"operator": c.operator or "improve", "params": c.params or {},
                                  "rationale": c.rationale or c.text or ""},
                         "parent_id": nid, "code": None,
                         "source_run": c.source_run, "source_node": int(c.source_node)},
                "label": f"Import #{c.source_node} from run {c.source_run}"}
    if a == "approve":
        node = nid if nid is not None else st.best_node_id
        if node is None:                      # no champion yet -> not an actionable approve
            return None
        return {"type": EV_APPROVAL_GRANTED, "data": {"node_id": node}, "label": f"Approve #{node}"}
    if a == "ratify":
        return {"type": EV_SPEC_APPROVED, "data": {}, "label": "Ratify the eval spec"}
    if a in ("stop", "pause", "finalize", "abort", "resume"):
        # 3 verbs: stop = freeze (EV_PAUSE, no wrap-up); finalize = stop + wrap-up (EV_RUN_ABORT →
        # run_finished → report/lessons/cost); resume = continue (EV_RESUME). pause≡stop, abort≡finalize
        # (back-compat aliases the LLM might still emit).
        t = {"stop": EV_PAUSE, "pause": EV_PAUSE, "finalize": EV_RUN_ABORT,
             "abort": EV_RUN_ABORT, "resume": EV_RESUME}[a]
        return {"type": t, "data": ({"reason": "finalized"} if t == EV_RUN_ABORT else {}),
                "label": ("Resume" if t == EV_RESUME else
                          "Finalize (wrap up)" if t == EV_RUN_ABORT else "Stop (freeze)") + " the run"}
    return None


def _plan_to_actions(plan: "_Plan", st) -> list[dict]:
    """Map a boss plan to the ORDERED list of control actions, dropping pure-advice steps. Each
    carries its own `rationale` so the UI's applied-row can explain that step."""
    out: list[dict] = []
    for step in plan.actions:
        ctrl = _action_to_control(step, st)
        if ctrl is not None:
            ctrl["rationale"] = step.rationale or ""
            out.append(ctrl)
    return out


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, _events = srv.run_dir, srv.events
    _llm_settings = srv.llm_settings

    # ---- persisted chat transcript (the human↔boss conversation, saved WITH the run) --------------
    # The /chat and /command endpoints below are stateless thinking aids — the UI holds the history.
    # That history used to live ONLY in React state, so it vanished whenever the Dock remounted (a
    # Search↔Report toggle, the finish-auto-land, reopening the run, or a reload). This sidecar makes
    # the transcript durable: one JSON turn per line in `chat.jsonl`, loaded on mount + appended per
    # turn. It is the UI server's OWN file (the engine never touches it), kept separate from
    # events.jsonl so it never folds into engine state and a `reset` can archive it independently.
    @router.get("/api/runs/{run_id}/chat-log")
    def chat_log(run_id: str):
        """The saved chat turns for this run, in order ({role:'user'|'assistant'|'action', …})."""
        rd = _run_dir(run_id)
        return list(iter_jsonl(rd / "chat.jsonl"))

    @router.post("/api/runs/{run_id}/chat-log")
    async def chat_log_append(run_id: str, request: Request):
        """Append ONE chat turn (the verbatim feed entry: role/content/trace or role/action/status)
        so it survives a remount/reload. Single writer (this server) + a synchronous fsync'd append
        per request serialize within the process, so no cross-process lock is needed here."""
        rd = _run_dir(run_id)
        turn = await request.json()
        if not isinstance(turn, dict):
            raise HTTPException(400, "chat turn must be a JSON object")
        path = rd / "chat.jsonl"
        with open(path, "ab") as f:
            f.write(orjson.dumps(turn) + b"\n")
            f.flush()
            best_effort_fsync(f.fileno())   # FUSE/S3 fsync may raise — don't fail the chat append
        return {"ok": True}

    @router.post("/api/runs/{run_id}/chat-compact")
    async def chat_compact(run_id: str, request: Request):
        """Summarize a stretch of older chat turns into ONE tight recap, so the boss's working memory
        stops growing turn-over-turn (the human↔boss history is re-sent in full each message). The UI
        sends the turns to fold; we return a recap string (+ its token cost) which the UI appends as a
        durable `summary` turn and then sends to the boss IN PLACE OF those turns. Read-only + soft-fail
        offline — compaction is opt-in, so a missing model just leaves the chat uncompacted."""
        rd = _run_dir(run_id)
        body = await request.json()
        msgs = body.get("messages") or []
        convo = "\n".join(f"{m.get('role')}: {m.get('content', '')}"
                          for m in msgs if str(m.get("content", "")).strip())
        if not convo.strip():
            return {"ok": True, "summary": "", "tokens": None}
        try:
            client = srv.make_llm_client(_llm_settings(rd))
            sys_prompt = COMPACT_SYSTEM
            summary = await anyio.to_thread.run_sync(lambda: client.complete_text(
                [{"role": "system", "content": sys_prompt}, {"role": "user", "content": convo}]))
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail (chat stays uncompacted)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        return {"ok": True, "summary": (summary or "").strip(), "tokens": _client_tokens(client)}

    @router.post("/api/runs/{run_id}/chat")
    async def chat(run_id: str, request: Request):
        """Advisory chat grounded on a run (and optionally one experiment node). Read-only — it
        never appends events; it's a thinking aid. The UI keeps the history and posts the full
        message list each turn. Soft-fails offline so the panel degrades cleanly."""
        rd = _run_dir(run_id)
        body = await request.json()
        msgs = body.get("messages") or []
        nid = body.get("node_id")
        st = fold(_events(rd))
        sys_prompt = CHAT_SYSTEM + _boss_context(st, nid, rd)
        try:
            client = srv.make_llm_client(_llm_settings(rd))
            # Offload to a thread — an `async def` handler must not run the blocking completion on the
            # event loop (it would freeze all other clients + the SSE streams for up to the 180s timeout).
            text = await anyio.to_thread.run_sync(
                lambda: client.complete_text([{"role": "system", "content": sys_prompt}, *msgs]))
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        # Return the LLM I/O so the chat row can expand into a langfuse-style trace. We include the
        # user's latest message + the system prompt (which carries the run/node context) so the trace
        # honestly shows the actual input, but omit the REST of the echoed conversation — the client
        # already holds it, and re-sending the whole history would grow the payload O(n²) over a long
        # chat (the single latest user turn is O(1)). `text` unchanged.
        user_msg = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
        return {"ok": True, "text": text,
                "trace": {"model": getattr(client, "model", None),
                          "system": sys_prompt, "user": user_msg, "completion": text,
                          "tokens": _client_tokens(client)}}

    @router.post("/api/runs/{run_id}/suggest")
    async def suggest(run_id: str, request: Request):
        """Turn the chat discussion (or a free-form instruction) into a CONCRETE experiment idea
        (operator + params + rationale) the UI can drop straight into the inject-node dialog.
        Uses structured output so the result is a ready-to-run Idea. Soft-fails offline."""
        rd = _run_dir(run_id)
        body = await request.json()
        nid = body.get("node_id")
        instruction = (body.get("instruction") or "").strip()
        history = body.get("messages") or []
        st = fold(_events(rd))
        from looplab.core.models import Idea
        from looplab.core.parse import parse_structured
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in history)
        prompt = ("Propose ONE next experiment as a structured Idea (operator one of "
                  "draft/improve/debug/merge; numeric params; a short rationale). Base it on the "
                  "run context and this discussion.\n\n" + _node_context(st, nid, rd)
                  + (f"\n\nDiscussion so far:\n{convo}" if convo else "")
                  + (f"\n\nInstruction: {instruction}" if instruction else ""))
        s = _llm_settings(rd)
        try:
            client = srv.make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        try:
            # Offload — `async def` handler; the blocking parse_structured / complete_text below would
            # otherwise stall the event loop (and the SSE streams) for the whole LLM round-trip.
            idea = await anyio.to_thread.run_sync(
                lambda: parse_structured(client, [{"role": "user", "content": prompt}], Idea, s.llm_parser))
            return {"ok": True, "idea": idea.model_dump(mode="json"), "parsed": True}
        except Exception:  # noqa: BLE001 - small models fumble strict tool-call output; fall back to
            # a free-text suggestion the operator can finish editing in the inject dialog. Never the
            # difference between "got a starting point" and "got nothing".
            try:
                text = await anyio.to_thread.run_sync(lambda: client.complete_text([
                    {"role": "system", "content": "Reply with a one-line experiment suggestion: the "
                     "operator (improve/draft/debug), suggested params, and why — plain text."},
                    {"role": "user", "content": prompt}]))
                return {"ok": True, "parsed": False,
                        "idea": {"operator": "improve", "params": {}, "rationale": text.strip()[:600]}}
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @router.post("/api/runs/{run_id}/command")
    async def command(run_id: str, request: Request):
        """Action-router (Workstream C): turn a free-text instruction into EITHER a concrete control
        action the UI confirms-then-executes, or a grounded advisory reply. Read-only itself — it
        never appends events; the UI calls /control after the human confirms. Soft-fails offline.
        Runs as a BACKGROUND JOB (like scope-report generate): the boss tool-loop can outlast a UI
        proxy's gateway timeout, so a slow model hands back {status:'running', job_id} the UI awaits
        via jobAwait instead of 504ing — a fast model still returns the plan inline within the wait,
        so the confirm-card flow downstream is unchanged."""
        rd = _run_dir(run_id)
        body = await request.json()
        msgs = body.get("messages") or []
        nid = body.get("node_id")
        instruction = (body.get("instruction") or "").strip()
        st = fold(_events(rd))
        s = _llm_settings(rd)
        try:
            client = srv.make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        sys_prompt = COMMAND_SYSTEM + _boss_context(st, nid, rd)
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in msgs)
        user = (f"Instruction: {instruction}" if instruction else "") + (f"\n\nDiscussion:\n{convo}" if convo else "")
        from looplab.core.parse import parse_structured

        def _route_with_tools() -> Optional["_Plan"]:
            """Boss decision grounded by the run-introspection tools: it MAY read experiments / data
            before choosing, then emits ONE _Plan (reply + ordered actions). None when the model can't
            drive the tool loop (then the caller falls back to a plain single-call route)."""
            from looplab.agents.agent import CompositeTools, drive_tool_loop, loop_opts_from_settings
            from looplab.tools.run_tools import RunTools, SiblingRunTools
            providers = [RunTools(), SiblingRunTools(rd.parent, rd.name)]
            try:                                  # DataTools needs the task; add it when we can load it
                from looplab.tools.run_tools import DataTools
                from looplab.adapters.tasks import load_task
                snap = rd / "task.snapshot.json"
                if snap.exists():
                    providers.append(DataTools(load_task(snap)))
            except Exception:  # noqa: BLE001 - tools are best-effort; RunTools alone is fine
                pass
            tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
            tools.bind_state(st, st.nodes.get(nid) if nid is not None else None)
            emit_spec = {"type": "function", "function": {
                "name": "emit", "description": "Emit the plan: a reply plus the ordered actions to "
                "apply now (empty actions = advice only).",
                "parameters": _Plan.model_json_schema()}}
            tool_sys = sys_prompt + ("\n\nThe context above (digest + report) usually has what you "
                "need — prefer to `emit` your plan directly. ONLY call a read-only tool first "
                "(read_experiment for a node's code/trials, find_analogous, data_schema/data_profile) "
                "when you SPECIFICALLY need a detail it doesn't show. Call `emit` exactly once.")
            box: dict = {}
            def _fin(args):
                try:
                    box["c"] = _Plan(**{k: v for k, v in (args or {}).items()
                                        if k in _Plan.model_fields})
                except Exception:  # noqa: BLE001 - junk emit -> treat as advise (empty plan)
                    box["c"] = _Plan()
                return box["c"]
            # Loop limits are CONFIG-DRIVEN (Settings.agent_max_turns / agent_time_budget_s), default
            # UNLIMITED — never hardcoded. The old hardcoded 3-turn / 45s cap cut a slow reasoning
            # model (e.g. minimax-m3 with reasoning=high) off BEFORE it emitted, silently dropping the
            # plan to a no-op advisory reply. With the limits open, drive_tool_loop forces the emit as
            # soon as the model stops calling tools, so the boss reliably returns a real plan. Latency
            # is bounded instead by running the WHOLE route as a BACKGROUND JOB below: a slow turn
            # returns {status:running} to the UI rather than 504ing behind a proxy. Set a positive cap
            # in settings only if you also want to hard-stop the loop itself.
            drive_tool_loop(client, tools, [{"role": "system", "content": tool_sys},
                                            {"role": "user", "content": user}],
                            emit_spec, max_turns=getattr(s, "agent_max_turns", 0),
                            time_budget_s=getattr(s, "agent_time_budget_s", 0.0),
                            finalize=_fin, fallback=lambda _m: box.get("c"),
                            **loop_opts_from_settings(s))     # B1 stuck (+ C1 self-plan / C2 if set)
            # Return None (not an empty _Plan) when the loop produced no emit, so the caller falls
            # through to the forced-emit single-call route instead of short-circuiting to advisory.
            return box.get("c")

        # All the LLM/agent work below is blocking + network-bound AND can outlast a UI proxy's
        # gateway timeout (the boss tool-loop's in-flight turn runs to its own client timeout — see
        # _route_with_tools). So it runs as a BACKGROUND JOB in a worker thread: a fast model still
        # returns inline within the wait (no polling; the confirm-card flow is unchanged), a slow one
        # hands back {status:'running', job_id} the UI awaits via jobAwait. The thread keeps the
        # blocking call off the event loop too, so it never stalls SSE tails / other clients. Each
        # response dict below is the EXACT contract the UI's runCommand already handles.
        def _compute() -> dict:
            plan = None
            try:
                plan = _route_with_tools()
            except Exception:  # noqa: BLE001 - model can't tool-call / loop error -> single-call route
                plan = None
            if plan is None:
                try:
                    plan = parse_structured(client, [{"role": "system", "content": sys_prompt},
                                                     {"role": "user", "content": user}], _Plan, s.llm_parser)
                except Exception:  # noqa: BLE001 - parse fumble -> fall through to advisory reply
                    plan = None
            if plan is not None:
                actions = _plan_to_actions(plan, st)
                tok = _client_tokens(client)     # whole-turn token cost (incl. any tool-loop sub-calls)
                if actions:
                    # An agentic plan: the ordered actions the UI applies in sequence, plus the boss's
                    # narration. `reply` (if any) is shown as the chat message above the applied rows.
                    return {"ok": True, "actions": actions, "reply": plan.reply or "", "tokens": tok}
                if plan.reply:                   # the boss chose to only talk back — show its reply
                    return {"ok": True, "reply": plan.reply, "tokens": tok}
            try:
                advise_sys = ("You are an ML research collaborator embedded in an experiment loop. Answer "
                              "concisely, grounded on the run.\n" + _boss_context(st, nid, rd))
                advise_msgs = msgs + ([{"role": "user", "content": instruction}] if instruction else [])
                text = client.complete_text([{"role": "system", "content": advise_sys}, *advise_msgs])
                # Carry the LLM I/O back so the chat row can expand into a langfuse-style trace card. We
                # include the user's instruction + system prompt so the trace shows the real input, but
                # omit the rest of the echoed conversation (the client holds it) to avoid O(n²) growth.
                user_msg = instruction or next(
                    (m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
                return {"ok": True, "reply": text,
                        "trace": {"model": getattr(client, "model", None),
                                  "system": advise_sys, "user": user_msg, "completion": text,
                                  "tokens": _client_tokens(client)}}
            except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail (advisory chat)
                return {"ok": False, "error": str(e)}

        return await srv.jobs.run_as_job(_compute)

    @router.post("/api/runs/{run_id}/report_refresh")
    async def report_refresh(run_id: str):
        """Force a high-quality regeneration of the agent-authored run report NOW. Appends a
        `report_generated` event directly, so it works whether or not the engine loop is alive —
        appends are lock-guarded, same as control events. Soft-fails offline: the deterministic
        report keeps rendering and no event is written. Runs as a BACKGROUND JOB (like scope-report
        generate): a slow/large model's full regeneration can outlast a UI proxy's gateway timeout,
        so it hands back {status:'running', job_id} the UI awaits via jobAwait instead of 504ing — a
        fast model still returns {ok, seq, content} inline within the wait."""
        rd = _run_dir(run_id)
        st = fold(_events(rd))
        s = _llm_settings(rd)

        def _compute() -> dict:
            # Runs in a worker thread (see _run_as_job): the synthesis is blocking + network-bound, so
            # inline it would freeze the event loop / SSE tails AND risk a proxy 504 on a slow model.
            # Append the event from the thread too — appends are lock-guarded, same as a control event.
            try:
                from looplab.serve.report import generate_report
                client = srv.make_llm_client(s)
                content = generate_report(st, client, parser=s.llm_parser, trigger="manual")
            except Exception as e:  # noqa: BLE001 — offline / no model -> soft fail, no event
                return {"ok": False, "error": str(e)}
            ev = EventStore(rd / "events.jsonl").append(
                EV_REPORT_GENERATED, {"content": content, "at_node": content.get("at_node"),
                                     "trigger": "manual"})
            return {"ok": True, "seq": ev.seq, "content": content}

        return await srv.jobs.run_as_job(_compute)

    return router
