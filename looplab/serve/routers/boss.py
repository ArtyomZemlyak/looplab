"""BOSS routes for a live run: the persisted chat transcript, advisory chat, idea suggestion, the
action-router (/command) and the manual report refresh — plus the `_Plan`/`_Action` models and
their control-event mapping. Bodies are verbatim moves from `serve/server.py` (BACKLOG §4);
`looplab.serve.server` re-exports `_Action`/`_Plan`/`_action_to_control`/`_plan_to_actions` so the
historical `looplab.server._Action` import path keeps working for tests and callers."""
from __future__ import annotations

import hashlib
from typing import Optional

import anyio
import orjson
try:
    from fastapi import APIRouter, HTTPException, Request, Response
    from fastapi.responses import JSONResponse
except ModuleNotFoundError as e:  # allow importing pure action models/mappers without the [ui] extra
    if e.name != "fastapi":
        raise
    APIRouter = HTTPException = Request = Response = JSONResponse = None  # type: ignore[assignment,misc]

from looplab.core.atomicio import best_effort_fsync, strict_fsync
from looplab.events.eventstore import EventStore, iter_jsonl
from looplab.events.types import (
    EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_COMMENT_CREATED, EV_DEEP_RESEARCH,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT,
    EV_INJECT_NODE, EV_NODE_RESET, EV_PAUSE, EV_PROMOTE,
    EV_REPORT_GENERATED, EV_REPORT_REFRESH_FAILED, EV_REPORT_REFRESH_STARTED,
    EV_RESUME, EV_RUN_ABORT, EV_SET_STRATEGY,
    EV_SPEC_APPROVED)
from looplab.serve.assistant import safe_provider_failure
from looplab.serve.llm_context import _boss_context, _client_tokens, _node_context
from looplab.serve.paid_work import (
    RunCostAccountingPending as _RunCostAccountingPending,
    flush_durable_run_costs as _flush_durable_run_costs,
    flush_pending_run_costs as _flush_pending_run_costs,
    metered_run_client as _metered_run_client,
    pending_run_cost_key as _pending_run_cost_key,  # noqa: F401 - compatibility seam
    pending_run_cost_state as _pending_run_cost_state,  # noqa: F401 - compatibility seam
    run_directory_identity as _report_run_identity,
)
from looplab.serve.protocol import EXPECTED_RUN_GENERATION_FIELD
from looplab.serve.serve_prompts import CHAT_SYSTEM, COMMAND_SYSTEM, COMPACT_SYSTEM


def _safe_boss_failure(exc: Exception) -> dict:
    """Keep the one server-owned accounting failure distinct from provider outages."""
    if isinstance(exc, _RunCostAccountingPending):
        message = (
            "A prior paid model call is still waiting for durable cost accounting. "
            "Retry after storage recovers."
        )
        return {"error": message, "error_kind": "accounting_pending", "message": message}
    return safe_provider_failure(exc)


# Workstream C → agentic boss: the chat action-router LLM emits a _Plan — a short conversational reply
# plus an ORDERED list of _Action steps. Each _Action maps to a control {type, data} the UI applies
# (boss mode auto-applies them in order, then reopens/resumes the run ONCE if any step needs the
# engine). An empty actions list = pure conversation (only the reply is shown).
from pydantic import BaseModel  # noqa: E402


_DOMAIN_HTTP_FAILURES = {
    "run_generation_changed": {
        "message": "The run was reset or replaced before this work started.",
        "remediation": "Reload the current run generation before trying again.",
    },
    "run_generation_unavailable": {
        "message": "The run has no durable generation identity yet.",
        "remediation": "Wait for run_started, reload the run, and try again.",
    },
}


def _sanitized_domain_http_exception(exc: HTTPException) -> Optional[HTTPException]:
    """Preserve allow-listed lifecycle status without reflecting arbitrary exception detail."""
    detail = exc.detail
    code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
    copy = _DOMAIN_HTTP_FAILURES.get(code)
    if copy is None:
        return None
    return HTTPException(int(exc.status_code), {"code": code, **copy})


def _background_http_failure(exc: HTTPException) -> dict:
    sanitized = _sanitized_domain_http_exception(exc)
    if sanitized is not None:
        detail = sanitized.detail
        return {
            "ok": False,
            "code": detail["code"],
            "error_kind": "run_state_conflict",
            "error": detail["message"],
        }
    return {
        "ok": False,
        "code": "background_request_rejected",
        "error_kind": "request_error",
        "error": "The background request was rejected.",
    }


def _report_refresh_ledger(events, generation: str) -> tuple[dict[str, object], set[str]]:
    """Return terminal receipts and unresolved paid-work claims for one run generation."""
    starts: set[str] = set()
    terminals: dict[str, object] = {}
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        identity = data.get("refresh_id")
        if (not isinstance(identity, str) or len(identity) != 64
                or any(char not in "0123456789abcdef" for char in identity)
                or data.get("generation") != generation):
            continue
        if event.type == EV_REPORT_REFRESH_STARTED:
            starts.add(identity)
        elif event.type in {EV_REPORT_GENERATED, EV_REPORT_REFRESH_FAILED}:
            terminals.setdefault(identity, event)  # first visible terminal; replay confirms sync
    return terminals, starts - set(terminals)


def _normalize_report_generation(value: object) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in value)):
        raise HTTPException(400, {
            "code": "invalid_run_generation",
            "message": "expected_generation must be an exact 64-character hexadecimal string.",
            "remediation": "Refresh GET /state and submit its generation with this report request.",
        })
    return value.lower()


_REPORT_REFRESH_ERROR_KINDS = frozenset({
    "credentials", "rate_limit", "unavailable", "provider_error", "capacity", "internal",
    "accounting_pending",
})


def _report_refresh_terminal(event, generation: str) -> dict:
    if event.type == EV_REPORT_GENERATED and isinstance(event.data.get("content"), dict):
        return {
            "ok": True,
            "seq": event.seq,
            "generation": generation,
            "content": event.data["content"],
        }
    raw_kind = str(event.data.get("error_kind") or "")
    kind = raw_kind if raw_kind in _REPORT_REFRESH_ERROR_KINDS else "provider_error"
    return {
        "ok": False,
        "code": "report_refresh_failed",
        "error_kind": kind,
        "error": "Report generation failed. Retry with a new request identity.",
        "generation": generation,
    }


def _confirm_report_refresh_terminal(path) -> bool:
    """Upgrade a visible terminal to a durable replay receipt before returning it.

    A strict append can write a complete line and then fail to confirm its fsync. Such a line is
    intentionally visible for same-key reconciliation, but it is not authoritative until a later
    sync succeeds. Windows requires a writable descriptor for ``fsync``/``_commit``.
    """
    try:
        with open(path, "r+b") as handle:
            strict_fsync(handle.fileno())
        return True
    except Exception:  # noqa: BLE001 - replay remains fail-closed on every storage capability gap
        return False


def _record_report_refresh_failure(srv, run_dir, generation: str, identity: str,
                                   error_kind: str) -> bool:
    """Persist a sanitized terminal before telling the caller that a fresh key is safe."""
    safe_kind = error_kind if error_kind in _REPORT_REFRESH_ERROR_KINDS else "provider_error"
    try:
        with srv.commands.sequence(run_dir):
            canonical = srv.commands.validate_paths(run_dir)
            if srv.commands.run_generation(canonical) != generation:
                return False
            store = EventStore(canonical / "events.jsonl")
            terminals, unresolved = _report_refresh_ledger(store.read_all(), generation)
            if identity in terminals:
                return True
            if identity not in unresolved:
                return False
            store.append(
                EV_REPORT_REFRESH_FAILED,
                {"refresh_id": identity, "generation": generation, "error_kind": safe_kind},
                require_lock=True, require_durable=True,
            )
        return True
    except Exception:  # noqa: BLE001 - an unresolved claim remains fail-closed after storage failure
        return False


def _run_report_refresh_worker(srv, settings, run_dir, generation: str,
                               identity: str) -> dict:
    """Execute one already-claimed report workflow and publish exactly one terminal outcome."""
    try:
        from looplab.serve.report import generate_report
        with _metered_run_client(srv, settings, run_dir, generation) as client:
            state = srv.state(run_dir)
            content = generate_report(
                state, client, parser=settings.llm_parser,
                trigger="manual", raise_on_failure=True)
            try:
                event = EventStore(run_dir / "events.jsonl").append(
                    EV_REPORT_GENERATED, {
                        "content": content, "at_node": content.get("at_node"), "trigger": "manual",
                        "refresh_id": identity, "generation": generation,
                    }, require_lock=True, require_durable=True)
            except Exception:  # noqa: BLE001 - paid work completed; only same-key reconcile is safe
                return {
                    "ok": False,
                    "code": "report_refresh_uncertain",
                    "error_kind": "uncertain",
                    "error": (
                        "The paid report completed, but its durable success receipt is unconfirmed. "
                        "Resume with the same request identity; do not start a new request."
                    ),
                    "generation": generation,
                    "ambiguous": True,
                }
    except HTTPException as exc:
        failure = _background_http_failure(exc)
        if failure.get("code") in _DOMAIN_HTTP_FAILURES:
            return {**failure, "generation": generation}
        if _record_report_refresh_failure(
                srv, run_dir, generation, identity,
                str(failure.get("error_kind") or "")):
            return {**failure, "generation": generation}
        return {
            "ok": False, "code": "report_refresh_uncertain", "error_kind": "uncertain",
            "error": "The rejected report request has no durable terminal receipt.",
            "generation": generation, "ambiguous": True,
        }
    except Exception as exc:  # noqa: BLE001 - public failure is allow-listed below
        failure = _safe_boss_failure(exc)
        if _record_report_refresh_failure(
                srv, run_dir, generation, identity,
                str(failure.get("error_kind") or "")):
            return {"ok": False, **failure, "generation": generation}
        return {
            "ok": False, "code": "report_refresh_uncertain", "error_kind": "uncertain",
            "error": "The paid report attempt has no durable terminal receipt.",
            "generation": generation, "ambiguous": True,
        }
    return {"ok": True, "seq": event.seq, "generation": generation, "content": content}


def _sanitize_chat_turn(turn: object) -> dict:
    """Canonicalize legacy paid-report action errors before storage or owner projection."""
    out = dict(turn) if isinstance(turn, dict) else {}
    action = out.get("action")
    stable_codes = {
        "report_refresh_uncertain", "report_refresh_in_progress", "report_refresh_failed",
        "job_unknown", "job_contact_lost", "job_timeout", "job_protocol_error", "job_capacity",
        "run_generation_changed", "run_generation_unavailable",
    }
    if (out.get("role") == "action" and isinstance(action, dict)
            and action.get("type") == "__refresh_report__" and out.get("error")
            and not isinstance(out.get("report_refresh"), dict)
            and out.get("report_code") not in stable_codes
            and out.get("error_kind") not in _REPORT_REFRESH_ERROR_KINDS):
        failure = safe_provider_failure(RuntimeError(str(out["error"])))
        out["error"] = failure["message"]
        out["error_kind"] = failure["error_kind"]
    return out


class _Action(BaseModel):
    action: str = "advise"   # advise|confirm|ablate|fork|promote|reset|hint|note|strategy|budget|deep_research|inject|import|approve|ratify|stop|finalize|resume
    node_id: Optional[int] = None
    text: str = ""           # hint text / note text / free rationale
    stage: str = ""          # reset: propose|implement|eval OR an eval-PIPELINE stage name (train/…) to re-run FROM
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


def _bound_node_data(st, bound_node_id: int, **data) -> dict:
    """Stamp a planned action with the lifecycle visible while the plan was produced.

    The UI may leave a confirmation card open across a reset; /control compares this generation with
    current state and returns 409 instead of applying the old plan to different code under the same id.
    Lightweight test/dummy states without a `nodes` projection keep the historical mapping shape.
    """
    node = ((getattr(st, "nodes", {}) or {}).get(bound_node_id)
            if st is not None else None)
    if node is not None:
        data["generation"] = node.attempt
    return data


def _bound_parent_data(st, bound_parent_id: Optional[int], **data) -> dict:
    if bound_parent_id is None:
        return data
    node = ((getattr(st, "nodes", {}) or {}).get(bound_parent_id)
            if st is not None else None)
    if node is not None:
        data["parent_generations"] = {str(bound_parent_id): node.attempt}
    return data


def _action_to_control(c: "_Action", st) -> Optional[dict]:
    """Map ONE classified boss action to a control {type, data, label}. None => no actionable verb
    (a pure-advice step, skipped). `label` is the human-readable line the UI's applied-row shows."""
    a, nid = c.action, c.node_id
    if a == "confirm" and nid is not None:
        return {"type": EV_FORCE_CONFIRM, "data": _bound_node_data(st, nid, node_id=nid),
                "label": f"Confirm #{nid} (multi-seed robustness)"}
    if a == "ablate" and nid is not None:
        return {"type": EV_FORCE_ABLATE, "data": _bound_node_data(st, nid, node_id=nid),
                "label": f"Ablate #{nid} (sensitivity probe)"}
    if a == "fork" and nid is not None:
        return {"type": EV_FORK, "data": _bound_node_data(st, nid, from_node_id=nid),
                "label": f"Fork an improve-branch from #{nid}"}
    if a == "promote" and nid is not None:
        return {"type": EV_PROMOTE,
                "data": _bound_node_data(st, nid, node_id=nid, alias="champion"),
                "label": f"Promote #{nid} to champion"}
    if a == "reset" and nid is not None:
        # Re-run an EXISTING node in place (never a new node). Stage picks how far back to rewind:
        # propose (full re-do) / implement (keep idea, re-develop) / eval (re-score, keep code) — OR the
        # name of an eval-PIPELINE stage (train / data_prep / …) to restart the pipeline from, reusing
        # earlier stages' artifacts.
        stage = (c.stage or "eval").strip()
        how = {"propose": "re-propose from scratch", "implement": "re-run the Developer (keep the idea)",
               "eval": "re-score (keep the code)"}.get(stage.lower(),
               f"re-run the eval pipeline from '{stage}' (reuse earlier stages)")
        return {"type": EV_NODE_RESET,
                "data": _bound_node_data(st, nid, node_id=nid, from_stage=stage),
                "label": f"Reset #{nid} in place — {how}"}
    if a == "hint" and c.text:
        # The boss authors the COMPLETE current directive each turn (it has the full chat + run
        # context), so a boss hint REPLACES the standing one rather than piling up — the researcher/
        # agent/strategist then read a single, current directive instead of a contradictory stack.
        return {"type": EV_HINT, "data": {"text": c.text, "replace": True},
                "label": f"Set directive: {c.text[:60]}"}
    if a in ("note", "annotate") and nid is not None and c.text:
        node = (getattr(st, "nodes", {}) or {}).get(nid)
        generation = getattr(node, "attempt", 0)
        return {"type": EV_COMMENT_CREATED,
                "data": {"node_id": nid, "node_generation": generation, "text": c.text},
                "label": f"Comment on #{nid}: {c.text[:50]}"}
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
        return {"type": EV_INJECT_NODE,
                "data": _bound_parent_data(st, nid, idea=idea, parent_id=nid, code=None),
                "label": f"Add experiment: {idea['operator']}" + (f" ({pp})" if pp else "")}
    if a == "import" and c.source_run and c.source_node is not None:
        # Seed an experiment FROM a sibling run. The source idea/code/metric are resolved from disk at
        # apply time (in /control), which then bakes `origin` provenance into the inject_node event —
        # so this rides the existing manual-injection pipeline, no new event type.
        return {"type": EV_INJECT_NODE,
                "data": _bound_parent_data(
                    st, nid,
                    idea={"operator": c.operator or "improve", "params": c.params or {},
                          "rationale": c.rationale or c.text or ""},
                    parent_id=nid, code=None,
                    source_run=c.source_run, source_node=int(c.source_node)),
                "label": f"Import #{c.source_node} from run {c.source_run}"}
    if a == "approve":
        # An omitted node means the exact pending subject, not the latest best. Explicit non-best
        # approval remains supported, but default/NL approval must not drift when ranking changes.
        if not getattr(st, "awaiting_approval", False):
            return None
        node = nid if nid is not None else getattr(st, "approval_subject", None)
        nodes = getattr(st, "nodes", {}) or {}
        target = nodes.get(node) if node is not None else None
        if (node is None or target is None or getattr(target, "tombstoned", False)
                or node in (getattr(st, "aborted_nodes", []) or [])):
            return None
        if nid is None:
            generation = getattr(st, "approval_generation", None)
            if (not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
                    or target.attempt != generation):
                return None
            data = {"node_id": node, "generation": generation}
        else:
            data = _bound_node_data(st, node, node_id=node)
        return {"type": EV_APPROVAL_GRANTED, "data": data, "label": f"Approve #{node}"}
    if a == "ratify":
        if (getattr(st, "proposed_spec", None) is None
                or not getattr(st, "spec_approval_requested", False)
                or getattr(st, "spec_confirmed", False)):
            return None
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


async def _json_object(request) -> dict:
    """Parse a request body as a JSON object or fail with 400. Mirrors the guards in
    ``routers/control.py`` so a non-JSON or non-object body (e.g. a bare ``[]``) yields a clean 400
    instead of an ``AttributeError``/``JSONDecodeError`` surfacing as a 500."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body


def build_router(srv) -> APIRouter:
    if APIRouter is None:
        raise ModuleNotFoundError(
            "The LoopLab boss routes need the [ui] extra: pip install 'looplab[ui]' "
            "(fastapi + uvicorn).",
            name="fastapi",
        )
    router = APIRouter()
    _run_dir = srv.run_dir
    _llm_settings = srv.llm_settings
    # RunCommandService invokes this optional callback before acquiring its destructive sequencer.
    # That is the non-paid recovery seam for a pending boss delta: flushing may close an activity
    # context (which acquires the same sequencer), so registering a callback is safer than importing
    # this router from the command layer or calling it after destructive_guard already owns the lock.
    srv.flush_pending_run_costs = lambda run_dir: _flush_pending_run_costs(srv, run_dir)
    # Reset/delete invoke this durable-only twin a second time while already holding the command
    # sequencer. It cannot close activity contexts or recursively acquire that sequencer.
    srv.flush_durable_run_costs = lambda run_dir: _flush_durable_run_costs(srv, run_dir)

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
        return [_sanitize_chat_turn(turn) for turn in iter_jsonl(rd / "chat.jsonl")]

    @router.post("/api/runs/{run_id}/chat-log")
    async def chat_log_append(run_id: str, request: Request):
        """Append ONE chat turn (the verbatim feed entry: role/content/trace or role/action/status)
        so it survives a remount/reload. Single writer (this server) + a synchronous fsync'd append
        per request serialize within the process, so no cross-process lock is needed here."""
        rd = _run_dir(run_id)
        generation = srv.commands.run_generation(rd)
        try:
            turn = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "chat turn must be valid JSON") from exc
        if not isinstance(turn, dict):
            raise HTTPException(400, "chat turn must be a JSON object")
        turn = _sanitize_chat_turn(turn)
        path = rd / "chat.jsonl"
        with srv.commands.run_activity(rd, "chat_append", generation=generation):
            with open(path, "ab") as f:
                f.write(orjson.dumps(turn) + b"\n")
                f.flush()
                best_effort_fsync(f.fileno())  # FUSE/S3 fsync may raise — don't fail the chat append
        return {"ok": True}

    @router.post("/api/runs/{run_id}/chat-compact")
    async def chat_compact(run_id: str, request: Request):
        """Summarize a stretch of older chat turns into ONE tight recap, so the boss's working memory
        stops growing turn-over-turn (the human↔boss history is re-sent in full each message). The UI
        sends the turns to fold; we return a recap string (+ its token cost) which the UI appends as a
        durable `summary` turn and then sends to the boss IN PLACE OF those turns. Read-only + soft-fail
        offline — compaction is opt-in, so a missing model just leaves the chat uncompacted."""
        rd = _run_dir(run_id)
        generation = srv.commands.run_generation(rd)
        body = await _json_object(request)
        msgs = body.get("messages") or []
        convo = "\n".join(f"{m.get('role')}: {m.get('content', '')}"
                          for m in msgs if str(m.get("content", "")).strip())
        if not convo.strip():
            return {"ok": True, "summary": "", "tokens": None}
        try:
            with _metered_run_client(srv, _llm_settings(rd), rd, generation) as client:
                sys_prompt = COMPACT_SYSTEM
                summary = await anyio.to_thread.run_sync(lambda: client.complete_text(
                    [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": convo}]))
                tokens = _client_tokens(client)
        except HTTPException as exc:
            sanitized = _sanitized_domain_http_exception(exc)
            if sanitized is not None:
                raise sanitized from exc
            return JSONResponse({"ok": False, **_safe_boss_failure(exc)}, status_code=200)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail (chat stays uncompacted)
            return JSONResponse({"ok": False, **_safe_boss_failure(e)}, status_code=200)
        return {"ok": True, "summary": (summary or "").strip(), "tokens": tokens}

    @router.post("/api/runs/{run_id}/chat")
    async def chat(run_id: str, request: Request):
        """Advisory chat grounded on a run (and optionally one experiment node). Read-only — it
        never appends events; it's a thinking aid. The UI keeps the history and posts the full
        message list each turn. Soft-fails offline so the panel degrades cleanly."""
        rd = _run_dir(run_id)
        generation = srv.commands.run_generation(rd)
        body = await _json_object(request)
        msgs = body.get("messages") or []
        nid = body.get("node_id")
        st = srv.state(rd)
        # advisory=True: /chat has no actions channel, so the RUN STATUS block must recommend,
        # not command ("you MUST act: resume") — else the model claims actions it can't take.
        sys_prompt = CHAT_SYSTEM + _boss_context(st, nid, rd, advisory=True)
        try:
            with _metered_run_client(srv, _llm_settings(rd), rd, generation) as client:
                # Offload to a thread — an `async def` handler must not run the blocking completion on
                # the event loop (it would freeze other clients/SSE for up to the client timeout).
                text = await anyio.to_thread.run_sync(
                    lambda: client.complete_text([{"role": "system", "content": sys_prompt}, *msgs]))
                model = getattr(client, "model", None)
                tokens = _client_tokens(client)
        except HTTPException as exc:
            sanitized = _sanitized_domain_http_exception(exc)
            if sanitized is not None:
                raise sanitized from exc
            return JSONResponse({"ok": False, **_safe_boss_failure(exc)}, status_code=200)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail
            return JSONResponse({"ok": False, **_safe_boss_failure(e)}, status_code=200)
        # Return the LLM I/O so the chat row can expand into a langfuse-style trace. We include the
        # user's latest message + the system prompt (which carries the run/node context) so the trace
        # honestly shows the actual input, but omit the REST of the echoed conversation — the client
        # already holds it, and re-sending the whole history would grow the payload O(n²) over a long
        # chat (the single latest user turn is O(1)). `text` unchanged.
        user_msg = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
        return {"ok": True, "text": text,
                "trace": {"model": model,
                          "system": sys_prompt, "user": user_msg, "completion": text,
                          "tokens": tokens}}

    @router.post("/api/runs/{run_id}/suggest")
    async def suggest(run_id: str, request: Request):
        """Turn the chat discussion (or a free-form instruction) into a CONCRETE experiment idea
        (operator + params + rationale) the UI can drop straight into the inject-node dialog.
        Uses structured output so the result is a ready-to-run Idea. Soft-fails offline."""
        rd = _run_dir(run_id)
        generation = srv.commands.run_generation(rd)
        body = await _json_object(request)
        nid = body.get("node_id")
        instruction = (body.get("instruction") or "").strip()
        history = body.get("messages") or []
        st = srv.state(rd)
        from looplab.core.models import Idea
        from looplab.core.parse import parse_structured
        from looplab.agents.roles import _CONCEPT_AUTHORING_GUIDANCE
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in history)
        prompt = ("Propose ONE next experiment as a structured Idea (operator one of "
                  "draft/improve/debug/merge; numeric params; a short rationale). "
                  + _CONCEPT_AUTHORING_GUIDANCE + "Base it on the "
                  "run context and this discussion.\n\n" + _node_context(st, nid, rd)
                  + (f"\n\nDiscussion so far:\n{convo}" if convo else "")
                  + (f"\n\nInstruction: {instruction}" if instruction else ""))
        s = _llm_settings(rd)
        try:
            with _metered_run_client(srv, s, rd, generation) as client:
                try:
                    # Offload — the blocking parser/completion must not stall the event loop.
                    idea = await anyio.to_thread.run_sync(lambda: parse_structured(
                        client, [{"role": "user", "content": prompt}], Idea, s.llm_parser))
                    return {"ok": True, "idea": idea.model_dump(mode="json"), "parsed": True}
                except Exception:  # small models can fumble strict output; fall back to editable text
                    text = await anyio.to_thread.run_sync(lambda: client.complete_text([
                        {"role": "system", "content": "Reply with a one-line experiment suggestion: "
                         "the operator (improve/draft/debug), suggested params, and why — plain text."},
                        {"role": "user", "content": prompt}]))
                    fallback = Idea(operator="improve", params={}, rationale=text.strip()[:600])
                    return {"ok": True, "parsed": False,
                            "idea": fallback.model_dump(mode="json")}
        except HTTPException as exc:
            sanitized = _sanitized_domain_http_exception(exc)
            if sanitized is not None:
                raise sanitized from exc
            return JSONResponse({"ok": False, **_safe_boss_failure(exc)}, status_code=200)
        except Exception as e:  # noqa: BLE001 - offline / no model
            return JSONResponse({"ok": False, **_safe_boss_failure(e)}, status_code=200)

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
        generation = srv.commands.run_generation(rd)
        body = await _json_object(request)
        msgs = body.get("messages") or []
        nid = body.get("node_id")
        instruction = (body.get("instruction") or "").strip()
        st = srv.state(rd)
        s = _llm_settings(rd)
        sys_prompt = COMMAND_SYSTEM + _boss_context(st, nid, rd)
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in msgs)
        user = (f"Instruction: {instruction}" if instruction else "") + (f"\n\nDiscussion:\n{convo}" if convo else "")
        from looplab.core.parse import parse_structured

        def _route_with_tools(client) -> Optional["_Plan"]:
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
            try:
                context = _metered_run_client(srv, s, rd, generation)
                client = context.__enter__()
            except HTTPException as exc:
                return _background_http_failure(exc)
            except Exception as e:  # noqa: BLE001 - offline / no model
                return {"ok": False, **_safe_boss_failure(e)}
            try:
                plan = None
                try:
                    plan = _route_with_tools(client)
                except Exception:  # model can't tool-call / loop error -> single-call route
                    plan = None
                if plan is None:
                    try:
                        plan = parse_structured(
                            client, [{"role": "system", "content": sys_prompt},
                                     {"role": "user", "content": user}], _Plan, s.llm_parser)
                    except Exception:  # parse fumble -> fall through to advisory reply
                        plan = None
                if plan is not None:
                    actions = _plan_to_actions(plan, st)
                    tok = _client_tokens(client)
                    if actions:
                        return {"ok": True, "actions": actions, "reply": plan.reply or "", "tokens": tok}
                    if plan.reply:
                        return {"ok": True, "reply": plan.reply, "tokens": tok}
                try:
                    advise_sys = ("You are an ML research collaborator embedded in an experiment "
                                  "loop. Answer concisely, grounded on the run.\n"
                                  + _boss_context(st, nid, rd, advisory=True))
                    advise_msgs = msgs + ([{"role": "user", "content": instruction}]
                                          if instruction else [])
                    text = client.complete_text(
                        [{"role": "system", "content": advise_sys}, *advise_msgs])
                    user_msg = instruction or next(
                        (m.get("content", "") for m in reversed(msgs)
                         if m.get("role") == "user"), "")
                    return {"ok": True, "reply": text, "trace": {
                        "model": getattr(client, "model", None), "system": advise_sys,
                        "user": user_msg, "completion": text, "tokens": _client_tokens(client)}}
                except HTTPException as exc:
                    return _background_http_failure(exc)
                except Exception as e:  # noqa: BLE001 - offline/model soft fail
                    return {"ok": False, **_safe_boss_failure(e)}
            finally:
                context.__exit__(None, None, None)

        return await srv.jobs.run_as_job(_compute)

    @router.post("/api/runs/{run_id}/report_refresh")
    async def report_refresh(run_id: str, request: Request, response: Response):
        """Force a high-quality regeneration of the agent-authored run report NOW. Appends a
        `report_generated` event directly, so it works whether or not the engine loop is alive —
        appends are lock-guarded, same as control events. Provider failure preserves the last report
        and writes a sanitized `report_refresh_failed` terminal receipt. Runs as a BACKGROUND JOB
        (like scope-report generation): a slow model can outlast a UI proxy's gateway timeout,
        so it hands back {status:'running', job_id} the UI awaits via jobAwait instead of 504ing — a
        fast model still returns {ok, seq, content} inline within the wait."""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "report refresh body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "report refresh body must be a JSON object")
        expected = _normalize_report_generation(body.get(EXPECTED_RUN_GENERATION_FIELD))
        raw_idempotency_key = request.headers.get("Idempotency-Key", "")
        if not raw_idempotency_key or len(raw_idempotency_key) > 512:
            raise HTTPException(
                400, "Idempotency-Key is required and must be at most 512 characters")

        rd = _run_dir(run_id)
        settings = None
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization, Idempotency-Key"
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            generation = srv.commands.run_generation(rd)
            if not generation:
                raise HTTPException(409, {
                    "code": "run_generation_unavailable",
                    "message": "The run has no durable generation identity.",
                    "remediation": "Wait for run_started, refresh the run, and try again.",
                })
            if generation != expected:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected,
                    "current_generation": generation,
                    "message": "The run was reset or replaced before report refresh arrived.",
                    "remediation": "Reload the replacement run before generating its report.",
                })
            # The event log is the restart-safe idempotency ledger. `started` lands
            # before provider construction; success/failure is a terminal receipt. An orphaned start
            # is intentionally uncertain and can never be replayed into a second paid call.
            canonical_identity = _report_run_identity(rd)
            job_identity = hashlib.sha256(
                ("report_refresh\0" + canonical_identity + "\0" + generation + "\0"
                 + raw_idempotency_key).encode("utf-8")).hexdigest()
            store = EventStore(rd / "events.jsonl")
            terminals, unresolved = _report_refresh_ledger(store.read_all(), generation)
            terminal = terminals.get(job_identity)
            if terminal is not None:
                if not _confirm_report_refresh_terminal(store.path):
                    return {
                        "ok": False,
                        "code": "report_refresh_uncertain",
                        "error_kind": "uncertain",
                        "error": (
                            "The saved report terminal is visible but its durable receipt is still "
                            "unconfirmed. Resume with this same request identity later."
                        ),
                        "generation": generation,
                        "ambiguous": True,
                    }
                return _report_refresh_terminal(terminal, generation)
            if unresolved:
                if job_identity not in unresolved:
                    raise HTTPException(409, {
                        "code": "report_refresh_in_progress",
                        "message": "Another report refresh already owns this run generation.",
                        "remediation": "Wait for its report event or reload before trying again.",
                    })
                reservation = srv.jobs.rejoin(job_identity)
                if reservation is None:
                    return {
                        "ok": False,
                        "code": "report_refresh_uncertain",
                        "error_kind": "uncertain",
                        "error": "The earlier paid report attempt has no live process receipt.",
                        "generation": generation,
                        "ambiguous": True,
                    }
                # The current implementation starts before releasing this sequencer. The lazy
                # fallback also repairs a workerless reservation created by an older process without
                # reading mutable settings when the existing worker is already live.
                compute = lambda: _run_report_refresh_worker(  # noqa: E731
                    srv, _llm_settings(rd), rd, generation, job_identity)
            else:
                # Configuration is needed only for brand-new paid work. Durable terminal replay,
                # restart uncertainty, and live same-key rejoin must survive later config damage.
                settings = _llm_settings(rd)
                # The event ledger, not this bounded process receipt, owns replay.
                reservation = srv.jobs.reserve(job_identity, consume_on_poll=True)
                if reservation.get("status") != "running":
                    return {**reservation, "generation": generation}
                compute = lambda: _run_report_refresh_worker(  # noqa: E731 - bound claim closure
                    srv, settings, rd, generation, job_identity)
                try:
                    store.append(
                        EV_REPORT_REFRESH_STARTED,
                        {"refresh_id": job_identity, "generation": generation},
                        require_lock=True, require_durable=True,
                    )
                    # Start before releasing the sequencer: no accepted durable claim can remain an
                    # in-memory workerless reservation if the HTTP task is cancelled at its first await.
                    srv.jobs.start_reserved(reservation["job_id"], compute)
                except Exception:
                    srv.jobs.discard_reservation(str(reservation.get("job_id") or ""))
                    raise
        # Return a durable job receipt well inside the browser's request deadline; paid work continues
        # in the worker and the same identity rejoins it after any ambiguous transport response.
        result = await srv.jobs.run_as_job(
            compute, inline_wait=min(0.5, srv.jobs.inline_wait),
            consume_inline_result=True, reserved_job_id=reservation["job_id"])
        if result.get("code") == "job_failed":
            if not _record_report_refresh_failure(
                    srv, rd, generation, job_identity, "internal"):
                return {
                    "ok": False,
                    "code": "report_refresh_uncertain",
                    "error_kind": "uncertain",
                    "error": "The report worker ended, but its terminal receipt could not be stored.",
                    "generation": generation,
                    "ambiguous": True,
                }
            result = {**result, "generation": generation}
        return result

    return router
