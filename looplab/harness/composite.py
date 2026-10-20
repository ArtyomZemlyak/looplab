"""Composite harness operations: one MCP call for the steps an external agent otherwise scripts.

The 2026-10-12..14 audit (doc 81) counted ~11 API calls per submitted experiment and 24 copies of a
`submit.py` an agent wrote to make them, and found the steps the agent skipped were exactly the
ones with no script: filing the experiment under its research question, pre-registering its
contrasts, finishing the run. These functions compose the EXISTING durable routes — nothing here
writes an event or a sidecar itself, every step goes through the same HTTP API, validation and
authorship stamping as a single call would — and stop at the first step that does not succeed,
returning every receipt so a retry can resume with the same keys:

* `submit_experiment` — (optional) pre-register the experiment in the run's protocol journal,
  (optional) add the research question it answers, (optional) publish the research memo, record
  the reviewed decisions the run's settings require, and inject the candidate filed under the
  question with its concepts. `dry_run=True` reads progress and validates without writing.
* `finish_run` — (optional) publish the final report, (optional) the run-end knowledge reviews,
  then finalize (`run_abort` with a reason), after reading the finish gates.
* `retry_node` — a failed node's setup/launch retried as a NEW ATTEMPT of the same node
  (`node_reset` from `eval` or a later stage), never a new node or run.
* `upload_per_query` — a local per-query `.npz`/JSON file sent to the run (the bounded body route).
* `campaign_supertask` — create (or reuse) a super-task and assign runs to it.
* `start_run` — the external agent creates its own run (doc 82 П3): the composable task through the
  same `/api/start/preflight` funnel the UI card uses, then `/api/start` with the validation token and
  a key derived from `key`, then the startup status and the new run's generation. The body is saved on
  the client before the POST (`client_requests.py::ClientStarts`), so a repeat resends it exactly.

Every write step derives its idempotency key / action id from the caller's `key`, so repeating the
same call after a lost response resolves each step to its original receipt instead of repeating it.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from looplab.core.errors import ConfigRefusal

MAX_UPLOAD_FILE_BYTES = 24 * 1024 * 1024
_TERMINAL = {"succeeded", "noop", "failed", "rejected", "timed_out"}


def _run_path(run_id: str, suffix: str = "") -> str:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise ConfigRefusal("run_id must be one literal run identifier")
    return f"/api/runs/{quote(run_id, safe='')}" + suffix


def _ok(result: dict) -> bool:
    return result.get("status") == 200 and not result.get("code")


def _trim(result: dict) -> dict:
    body = result.get("body")
    out = {k: result.get(k) for k in ("status", "code", "outcome", "reason", "message")
           if result.get(k) is not None}
    if isinstance(body, dict):
        for key in ("ok", "replayed", "id", "status", "event_type", "error", "event_seq", "detail",
                    "amendment", "upload", "decision", "narrative"):
            if key in body:
                value = body[key]
                if key == "amendment" and isinstance(value, dict):
                    value = {k: value.get(k) for k in ("amendment_id", "version", "kind", "summary",
                                                       "author")}
                if key == "upload" and isinstance(value, dict):
                    value = {k: value.get(k) for k in ("upload_id", "test_id", "n_queries",
                                                       "consistency", "metrics")}
                out[key] = value
    elif body is not None:
        out["body"] = str(body)[:500]
    return out


class _Steps:
    def __init__(self):
        self.steps: list[dict] = []

    def add(self, name: str, result: dict, ok: Optional[bool] = None) -> bool:
        success = _ok(result) if ok is None else ok
        self.steps.append({"step": name, "ok": success, **_trim(result)})
        return success

    def done(self, *, message: str, **extra) -> dict:
        failed = next((s for s in self.steps if not s["ok"]), None)
        if failed is not None and failed.get("code") == "command_rejected":
            # A REJECTED command is its key's final outcome: the same key replays the rejection.
            message = (f"stopped at {failed['step']}: the command was rejected — read its error, "
                       "fix the cause and call again with a NEW key (completed steps under the old "
                       "key stay recorded)")
        elif failed is not None:
            message = (f"stopped at {failed['step']}: read its receipt; repeat this call with the "
                       "SAME key to resume (completed steps replay their original receipts)")
        return {"ok": failed is None, "stopped_at": failed["step"] if failed else None,
                "steps": self.steps, "message": message, **extra}


def _command(api, run_id: str, generation: str, event_type: str, data: dict, key: str, *,
             settle_seconds: float = 10.0) -> dict:
    """Submit one durable command and wait briefly for a terminal status (folded intents settle
    at once; an engine-acknowledged intent may still be `accepted` when this returns)."""
    result = api.request("POST", _run_path(run_id, "/commands"),
                         {"type": event_type, "expected_generation": generation, "data": data}, key)
    if not _ok(result):
        return result
    deadline = time.monotonic() + settle_seconds
    record = result.get("body") if isinstance(result.get("body"), dict) else {}
    while record.get("status") not in _TERMINAL and time.monotonic() < deadline:
        time.sleep(0.25)
        receipt = api.command_receipt(run_id, generation, idempotency_key=key)
        if _ok(receipt):
            record = (receipt["body"].get("command") or {})
    status = record.get("status")
    out = {"status": 200, "body": {"id": record.get("id"), "status": status,
                                   "event_type": event_type, "event_seq": record.get("event_seq"),
                                   "error": record.get("error") or record.get("error_code")}}
    if status in ("failed", "rejected", "timed_out"):
        out["code"] = f"command_{status}"
    return out


def _settled_inject_refusal(steps: "_Steps", record: dict, status: str) -> dict:
    """A same-key resume whose inject already ended rejected/failed/timed_out: stop with ITS error.
    The key cannot carry a second attempt — the same key and payload resolve to this record — so
    the agent fixes the cause and submits under a NEW key (or, for a retryable failure, retries the
    recorded command itself)."""
    error = record.get("error") or record.get("error_code")
    steps.steps.append({"step": "inject", "ok": False, "status": status, "replayed": True,
                        "code": f"command_{status}", "id": record.get("id"), "error": error})
    retryable = (status in ("failed", "timed_out") and isinstance(error, dict)
                 and error.get("retryable") is True)
    message = (f"this key's inject was already {status}; a command key is bound to its first "
               "outcome, so repeating this call with the SAME key replays it. ")
    message += (f"Its failure is retryable: POST /api/runs/<run>/commands/{record.get('id')}/retry "
                "retries that exact command." if retryable else
                "Read its error, fix the cause (e.g. merge the base and resolve the named paths), "
                "then call again with a NEW key.")
    return {"ok": False, "stopped_at": "inject", "steps": steps.steps, "message": message}


def _preview_summary(result: dict) -> dict:
    """The preview receipt an agent reads: WHAT the submission becomes, never the file bodies
    (`include_files=false`), plus the warnings and the conflict paths a refusal names."""
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    out = {k: result.get(k) for k in ("status", "code", "outcome", "reason", "message")
           if result.get(k) is not None}
    if _ok(result):
        out.update(changed=[r.get("path") for r in (body.get("changed") or [])][:100],
                   deleted=list(body.get("deleted") or [])[:100],
                   **{k: body[k] for k in ("stale_base", "warnings", "inherited_outside_surface",
                                           "binary_files", "foreign", "base_digest")
                      if body.get(k)})
    elif isinstance(body.get("detail"), (dict, str)):
        out["detail"] = body["detail"]
    return out


def submit_experiment(api, run_id: str, expected_generation: str, *, key: str, idea: dict,
                      files: Optional[dict] = None, code: Optional[str] = None,
                      deleted: Optional[list] = None, parent_id: Optional[int] = None,
                      research_question: Optional[dict] = None,
                      experiment: Optional[dict] = None, experiment_reason: str = "",
                      memo: Optional[dict] = None, decisions: Optional[dict] = None,
                      dry_run: bool = False, code_from: Optional[dict] = None,
                      parent_ids: Optional[list] = None,
                      parent_generations: Optional[dict] = None,
                      node_kind: Optional[str] = None, uses: Optional[list[int]] = None) -> dict:
    """`code_from` (doc 79 §4) is the run-worktree / patch / foreign-branch alternative to
    `files`/`code`/`deleted` — exactly one of the two. It is PREVIEWED through the same route the
    intake converts it with (`POST .../code/overlay`, paths and sizes only) BEFORE anything is
    written, so a surface refusal or a merge conflict stops the call with nothing registered, and a
    dry run reports the paths the candidate would change. `parent_ids` names several parents — a
    worktree that ran `git merge nodes/n<id>` lists every node it combined — and is exclusive with
    `parent_id`."""
    # The ONE code_from vocabulary the intake and the preview also read (UI-free; deferred, as
    # every harness->serve edge is: `tests/test_package_layering.py::DEFERRED`).
    from looplab.serve.protocol import code_from_shape_error, normalize_code_from
    steps = _Steps()
    if not isinstance(idea, dict) or not isinstance(idea.get("operator"), str):
        raise ConfigRefusal("idea must be an object with an operator")
    if not key or len(key) > 120:
        raise ConfigRefusal("key must be a stable non-empty string of at most 120 characters")
    if parent_id is not None and parent_ids is not None:
        raise ConfigRefusal("give parent_id or parent_ids, not both")
    if node_kind not in (None, "artifact"):
        raise ConfigRefusal("node_kind must be artifact or omitted")
    idea = dict(idea)
    progress = api.run_progress(run_id, expected_generation)
    if not steps.add("read_progress", progress):
        return steps.done(message="")
    body = progress["body"]
    required = dict(body.get("candidate_decisions_per_idea") or {})
    if node_kind == "artifact":
        required.pop("novelty", None)
    requirements = body.get("candidate_requirements") or {}
    blockers = body.get("candidate_blockers_if_expanding") or []
    missing = []
    if requirements.get("effective_concepts") and not idea.get("concepts"):
        missing.append("idea.concepts (this run requires concept tags on every candidate)")
    if requirements.get("hypothesis_statement") and not idea.get("hypothesis"):
        missing.append("idea.hypothesis (this run requires a hypothesis statement)")
    decisions = dict(decisions or {})
    for phase in required:
        if phase not in decisions:
            missing.append(f"decisions[{phase!r}] = {{reason, alternatives?}} (required review)")
    if code_from is not None:
        if files or code or deleted:
            missing.append("code_from OR files/code/deleted, not both")
        elif code_from_shape_error(code_from):
            missing.append(code_from_shape_error(code_from))
        else:
            code_from = normalize_code_from(code_from)
    elif not (files or code or deleted):
        missing.append("code_from, or files or code (an external run evaluates ready-made "
                       "candidates; code_from={'worktree': name} is the default path)")
    if research_question is not None:
        if not isinstance(research_question, dict) or not research_question.get("id"):
            missing.append("research_question.id")
        else:
            idea.setdefault("parent_card_id", research_question["id"])
    plan = (["register experiment"] if experiment else []) + \
        (["add research question"] if research_question and research_question.get("statement") else []) + \
        (["publish research memo"] if memo else []) + \
        [f"decision {p}" for p in decisions] + ["inject_node"]
    if code_from is not None and not missing:
        # A same-key RESUME whose inject already landed must replay it, not re-judge a worktree
        # the agent has edited since (or a base that has moved): the preview is skipped then.
        prior = (None if dry_run else
                 api.command_receipt(run_id, expected_generation, idempotency_key=f"{key}:inject"))
        record = (prior.get("body") or {}).get("command") if prior and _ok(prior) else None
        status = record.get("status") if isinstance(record, dict) else None
        if status in ("rejected", "failed", "timed_out"):
            # NOT landed (review 2026-10-16): a same-key retry would skip the preview and replay
            # this receipt forever — a durable command key is bound to its first outcome, and the
            # same key with the same payload resolves to that record (`run_commands.py::submit`).
            return _settled_inject_refusal(steps, record, status)
        if isinstance(record, dict):
            steps.steps.append({"step": "preview_code", "ok": True,
                                "status": "skipped: this key's inject was already submitted"})
        else:
            # Before ANY write: the conversion the intake will apply, on the same route and rules.
            # A preview writes nothing the caller owns: `read_only` keeps a 5xx an unavailable
            # read, never an unknown WRITE outcome to recover (`mcp_server.py::HarnessAPI._result`).
            preview = api.request("POST", _run_path(run_id, "/code/overlay"), {
                **code_from, "expected_generation": expected_generation, "include_files": False},
                read_only=True)
            ok = _ok(preview)
            steps.steps.append({"step": "preview_code", "ok": ok, **_preview_summary(preview)})
            if not ok:
                return steps.done(message="")
    if missing or dry_run:
        steps.steps.append({"step": "validate", "ok": not missing, "missing": missing,
                            "blockers": blockers, "plan": plan})
        return steps.done(message=("dry run: nothing was written" if dry_run and not missing
                                   else "nothing was written"), dry_run=dry_run)
    if experiment is not None:
        result = api.request("POST", _run_path(run_id, "/protocol/amendments"), {
            "expected_generation": expected_generation, "action_id": f"{key}:experiment",
            "kind": "experiment", "experiment": experiment,
            "reason": experiment_reason or "pre-registered with the experiment's first candidate"})
        # `experiment_exists` continues ONLY when the server says the registered declaration
        # is this one (`same_declaration`): a second candidate filed under the same experiment.
        # A different hypothesis/arms/correction under a reused id stops here, before anything
        # is injected — it used to count as a replay, inject, and report success while the
        # journal kept the original (review 2026-10-15, weekly-registration-conflict).
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
        registered = (result.get("status") == 409 and detail.get("code") == "experiment_exists"
                      and detail.get("same_declaration") is True)
        if not steps.add("register_experiment", result, ok=_ok(result) or registered):
            return steps.done(message="")
    if research_question and research_question.get("statement"):
        result = _command(api, run_id, expected_generation, "hypothesis_added", {
            "id": research_question["id"], "statement": research_question["statement"],
            "source": "external_agent",
            **({"concepts": research_question["concepts"]} if research_question.get("concepts") else {}),
        }, f"{key}:question")
        if not steps.add("add_research_question", result):
            return steps.done(message="")
    if memo is not None:
        result = _command(api, run_id, expected_generation, "research_completed", {"memo": memo},
                          f"{key}:memo")
        if not steps.add("publish_memo", result):
            return steps.done(message="")
    for phase, spec in decisions.items():
        spec = spec if isinstance(spec, dict) else {"reason": str(spec)}
        result = api.request("POST", _run_path(run_id, "/harness-decisions"), {
            "expected_generation": expected_generation, "action_id": f"{key}:decision:{phase}",
            "phase_id": phase, "idea": idea, "decision": "submit",
            "alternatives": spec.get("alternatives") or [],
            **({"implementations": spec["implementations"],
                "selected_index": spec.get("selected_index", 0)} if spec.get("implementations") else {}),
            "reason": spec.get("reason") or "reviewed before submission"})
        if not steps.add(f"decision:{phase}", result):
            return steps.done(message="")
    data: dict[str, Any] = {"idea": idea}
    for name, value in (("files", files), ("code", code), ("deleted", deleted),
                        ("parent_id", parent_id), ("parent_ids", parent_ids),
                        ("parent_generations", parent_generations), ("code_from", code_from),
                        ("node_kind", node_kind), ("uses", uses)):
        if value is not None:
            data[name] = value
    result = _command(api, run_id, expected_generation, "inject_node", data, f"{key}:inject",
                      settle_seconds=5.0)
    steps.add("inject", result)
    return steps.done(message=("submitted: an inject receipt proves admission, not training; "
                               "read result_notices after the node is terminal"))


def finish_run(api, run_id: str, expected_generation: str, *, key: str,
               report: Optional[dict] = None, reviews: Optional[list] = None,
               reason: str = "campaign complete", dry_run: bool = False) -> dict:
    steps = _Steps()
    progress = api.run_progress(run_id, expected_generation)
    if not steps.add("read_progress", progress):
        return steps.done(message="")
    body = progress["body"]
    gates = {k: body.get(k) for k in ("finish_report_due", "finish_pending_nodes",
                                      "finish_reviews_due", "finish_blockers", "next_step")
             if k in body}
    if dry_run:
        steps.steps.append({"step": "validate", "ok": True, "gates": gates,
                            "plan": (["report"] if report else []) +
                                    [f"review {r.get('phase_id')}" for r in reviews or []] + ["finalize"]})
        return steps.done(message="dry run: nothing was written", dry_run=True)
    if report is not None:
        result = _command(api, run_id, expected_generation, "report_generated", {"content": report},
                          f"{key}:report")
        if not steps.add("final_report", result):
            return steps.done(message="")
    for review in reviews or []:
        result = api.request("POST", _run_path(run_id, "/harness-reviews"), {
            "expected_generation": expected_generation,
            "action_id": f"{key}:review:{review.get('phase_id')}", **review})
        if not steps.add(f"review:{review.get('phase_id')}", result):
            return steps.done(message="")
    result = _command(api, run_id, expected_generation, "run_abort", {"reason": reason},
                      f"{key}:finalize", settle_seconds=5.0)
    steps.add("finalize", result)
    return steps.done(message="finalize submitted; the run is finished when its command settles",
                      gates=gates)


def retry_node(api, run_id: str, expected_generation: str, *, node_id: int, key: str,
               from_stage: str = "eval", generation: Optional[int] = None) -> dict:
    """Retry a node's setup/launch as a new ATTEMPT of the same node. The engine keeps the earlier
    attempt's terminal row and archives its stage logs (`attempt_logs/node_<id>/`, doc 78)."""
    steps = _Steps()
    # The step key must not depend on what THIS call reads: the attempt counter moves the moment
    # the reset lands, so a key derived from it made a repeat after a lost response either refuse
    # (`node_terminal`, the node is now pending) or reset the node a SECOND time under a new key.
    # An explicit `generation=` keys as before; an inferred one keys on (key, node) alone and the
    # original receipt is looked up FIRST, so the repeat replays it instead of reading the node.
    inferred = generation is None
    step_key = (f"{key}:reset:{node_id}" if inferred
                else f"{key}:reset:{node_id}:{generation}")
    if inferred:
        prior = api.command_receipt(run_id, expected_generation, idempotency_key=step_key)
        record = (prior.get("body") or {}).get("command") if _ok(prior) else None
        if isinstance(record, dict) and record.get("id"):
            status = record.get("status")
            out = {"status": 200, "body": {"id": record.get("id"), "status": status,
                                           "event_type": "node_reset",
                                           "event_seq": record.get("event_seq"), "replayed": True,
                                           "error": record.get("error") or record.get("error_code")}}
            if status in ("failed", "rejected", "timed_out"):
                out["code"] = f"command_{status}"
            steps.add("reset", out)
            return steps.done(message=f"node {node_id}: this key's reset was already submitted")
    if generation is None:
        node = api.request("GET", _run_path(
            run_id, f"/nodes/{int(node_id)}?expected_generation={expected_generation}"))
        if node.get("truncated"):
            steps.steps.append({"step": "read_node", "ok": False, **_trim(node),
                                "message": "node detail too large; pass generation= explicitly"})
            return steps.done(message="")
        if not steps.add("read_node", node):
            return steps.done(message="")
        generation = node["body"].get("attempt", 0)
        status = node["body"].get("status")
        if status not in ("failed", "evaluated"):
            steps.steps.append({"step": "node_terminal", "ok": False,
                                "message": f"node {node_id} is {status!r}; retry a terminal node"})
            return steps.done(message="")
    result = _command(api, run_id, expected_generation, "node_reset", {
        "node_id": int(node_id), "generation": int(generation), "from_stage": from_stage},
        step_key, settle_seconds=5.0)
    steps.add("reset", result)
    return steps.done(message=(f"node {node_id} queued for attempt {int(generation) + 1}; "
                               "earlier attempts stay in the event log and the attempt_logs/ archive"))


def upload_per_query(api, run_id: str, expected_generation: str, *, node_id: int, path: str,
                     metrics: dict, action_id: str, test_label: Optional[str] = None,
                     experiment_id: Optional[str] = None, arm: Optional[str] = None,
                     query_key: str = "keys", cluster_key: Optional[str] = "users",
                     measured_check: Optional[dict] = None) -> dict:
    source = Path(path).expanduser() if path else None
    if source is None or not source.is_file():
        # RESUME (doc 81 §8 leftover): the file is gone or was never passed again, but this client
        # spooled the exact request under (run_id, action_id) — resend that.
        resumed = api.resume_upload(_run_path(run_id, "/per-query"), run_id, action_id)
        if resumed is not None:
            return resumed
        raise ConfigRefusal("path must be a readable local file (and no spooled upload is pending "
                         "for this action_id)")
    size = source.stat().st_size
    if size > MAX_UPLOAD_FILE_BYTES:
        raise ConfigRefusal(f"per-query files are at most {MAX_UPLOAD_FILE_BYTES} bytes")
    fmt = "json" if source.suffix.lower() == ".json" else "npz"
    body = {"expected_generation": expected_generation, "action_id": action_id,
            "node_id": node_id, "format": fmt,
            "content_b64": base64.b64encode(source.read_bytes()).decode("ascii"),
            "metrics": metrics, "query_key": query_key, "cluster_key": cluster_key,
            **{k: v for k, v in (("test_label", test_label), ("experiment_id", experiment_id),
                                 ("arm", arm), ("measured_check", measured_check))
               if v is not None}}
    return api.post_large(_run_path(run_id, "/per-query"), body, spool=(run_id, action_id),
                          source=str(source))


def resume_per_query_upload(api, run_id: str, action_id: str) -> dict:
    resumed = api.resume_upload(_run_path(run_id, "/per-query"), run_id, action_id)
    if resumed is None:
        entry = api.uploads.entry(run_id, action_id) if api.uploads is not None else None
        return {"ok": False, "code": "no_pending_upload",
                "message": ("this upload was already answered (status "
                            f"{entry.get('http_status')}); read the run's per-query view"
                            if entry and entry.get("status") == "settled" else
                            "no spooled body for this run_id/action_id on this client")}
    return resumed


def campaign_supertask(api, name: str, run_ids: list, supertask_id: Optional[str] = None) -> dict:
    """Create (or reuse) a super-task and put each run in it, CAS-fenced on the Runs list."""
    steps = _Steps()
    if supertask_id is None:
        listing = api.request("GET", "/api/supertasks")
        if not steps.add("list_supertasks", listing):
            return steps.done(message="")
        existing = next((s for s in listing["body"].get("supertasks", [])
                         if s.get("name") == name), None)
        if existing is None:
            created = api.request("POST", "/api/supertasks", {"name": name})
            if not steps.add("create_supertask", created):
                return steps.done(message="")
            supertask_id = created["body"]["id"]
        else:
            supertask_id = existing["id"]
    runs = api.request("GET", "/api/runs")
    if runs.get("truncated"):
        return {"ok": False, "steps": steps.steps, "message": "the Runs list is too large for one read"}
    if not steps.add("list_runs", runs):
        return steps.done(message="")
    rows = runs["body"].get("runs", []) if isinstance(runs["body"], dict) else runs["body"]
    by_id = {row.get("run_id"): row for row in rows if isinstance(row, dict)}
    for rid in run_ids:
        row = by_id.get(rid)
        if row is None:
            steps.steps.append({"step": f"assign:{rid}", "ok": False, "message": "no such run"})
            return steps.done(message="")
        if row.get("supertask_id") == supertask_id:
            steps.steps.append({"step": f"assign:{rid}", "ok": True, "status": "already assigned"})
            continue
        result = api.request("POST", _run_path(rid, "/supertask"), {
            "supertask_id": supertask_id, "expected_supertask_id": row.get("supertask_id"),
            "expected_generation": row.get("generation")})
        if not steps.add(f"assign:{rid}", result):
            return steps.done(message="")
    return steps.done(message=f"runs grouped under super-task {supertask_id}",
                      supertask_id=supertask_id)


__all__ = ["campaign_supertask", "finish_run", "resume_per_query_upload", "retry_node",
           "submit_experiment", "upload_per_query"]


# The startup statuses `serve/start_record.py::start_public` reports while an engine is coming up.
_START_PENDING = {"preparing", "accepted", "executing"}


def _start_run_id(task: dict, key: str) -> str:
    """A run name that is a pure function of the task's goal and the caller's key, so the same
    `start_run` repeated after a lost response names the same run (and the server's keyed replay
    answers it) while a new key never collides with an old run."""
    import hashlib
    from looplab.core.run_proposal import slug_run_id
    stem = slug_run_id(task.get("goal") or task.get("competition") or task.get("kaggle") or "run")
    return f"{(stem or 'run')[:30].rstrip('-')}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _refusal_fields(result: dict) -> dict:
    """A 4xx launch refusal's `code`/`message`/`field_errors`, so the agent can fix and repeat."""
    body = result.get("body")
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        out = {k: detail[k] for k in ("code", "message", "field_errors", "remediation")
               if k in detail}
    else:
        out = {"message": str(detail)[:500]} if detail else {}
    out.setdefault("message", "")
    return out


def start_run(api, *, task: dict, key: str, run_id: Optional[str] = None,
              settings: Optional[dict] = None, dry_run: bool = False,
              wait_seconds: float = 60.0) -> dict:
    """Create an EXTERNAL run from a composable task and wait until its engine has started.

    `external_harness=true` is always set; the launch point then fills `backend=toy` and the
    `agent-minimal` profile when `settings` names neither (`core/config.py::external_launch_defaults`).
    A refused task comes back with the funnel's `field_errors`: fix them and call again with the same
    key — nothing was started. After a lost response, the same key resends the SAVED body; a new
    task needs a new key."""
    from looplab.core.run_proposal import RunProposal

    steps = _Steps()
    if not isinstance(task, dict) or not task:
        raise ConfigRefusal("task must be a composable task object: goal + direction + the fields "
                            "you have (repo / dataset / cmd / kaggle); no `kind`")
    if (not isinstance(key, str) or not key or len(key) > 190
            or any(ord(c) < 32 or ord(c) > 126 for c in key)):
        raise ConfigRefusal("key must be printable ASCII of 1-190 characters")
    if settings is not None and not isinstance(settings, dict):
        raise ConfigRefusal("settings must be an object of Settings fields")
    start_key = f"{key}:start"
    reach = api.ensure_reachable() if callable(getattr(api, "ensure_reachable", None)) else None
    if reach is not None and not reach.get("ok"):
        steps.steps.append({"step": "server", "ok": False, **reach})
        return steps.done(message="")
    store = getattr(api, "starts", None)
    body = store.saved(start_key) if store is not None else None
    if body is None:
        merged = {**(settings or {}), "external_harness": True}
        name = run_id or _start_run_id(task, key)
        body = RunProposal.from_card({"run_id": name, "task": task, "settings": merged},
                                     planner="mcp").start_body()
        pre = api.request("POST", "/api/start/preflight", body, read_only=True)
        if not steps.add("preflight", pre):
            return steps.done(**_refusal_fields(pre))
        preview = pre["body"].get("preview") if isinstance(pre.get("body"), dict) else None
        if dry_run:
            return steps.done(message="dry run: validated, nothing was started", dry_run=True,
                              run_id=body["run_id"], preview=preview,
                              warnings=pre["body"].get("warnings", []))
        body = {**body, "validation_token": pre["body"].get("validation_token"),
                "idempotency_key": start_key}
        if store is not None:
            try:
                store.save(start_key, body)
            except (OSError, ValueError) as exc:
                steps.steps.append({"step": "save_request", "ok": False, "outcome": "not_sent",
                                    "message": getattr(exc, "message", None) or
                                    "the launch body could not be saved on this machine; "
                                    "nothing was sent"})
                return steps.done(message="")
    elif dry_run:
        return steps.done(message="dry run: this key's launch is already saved; nothing was sent",
                          dry_run=True, run_id=body.get("run_id"))
    run = body["run_id"]
    started = api.request("POST", "/api/start", body)
    if started.get("outcome") != "unknown" and not steps.add("start", started):
        return steps.done(**_refusal_fields(started))
    if started.get("outcome") == "unknown":
        # A lost acknowledgement: observe the durable startup record instead of guessing.
        steps.steps.append({"step": "start", "ok": True, "outcome": "unknown",
                            "message": "acknowledgement lost; observing the startup record"})
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    status_path = f"/api/start/{quote(run, safe='')}/status"
    while True:
        status = api.request("GET", status_path, idempotency_key=start_key)
        record = status.get("body") if isinstance(status.get("body"), dict) else {}
        if (status.get("status") != 200 or record.get("status") not in _START_PENDING
                or time.monotonic() >= deadline):
            break
        time.sleep(0.5)
    # Appended by hand: `_Steps.add` would let the startup record's own `ok` (true while the engine is
    # merely `executing`) overwrite this step's verdict.
    started_ok = status.get("status") == 200 and record.get("status") == "succeeded"
    steps.steps.append({"step": "startup", "ok": started_ok, "http_status": status.get("status"),
                        **{k: status[k] for k in ("code", "outcome", "message") if status.get(k)},
                        **{k: record[k] for k in ("status", "started", "can_retry",
                                                  "paid_effect_unknown", "error") if k in record}})
    if not started_ok:
        if record.get("status") in _START_PENDING:
            steps.steps[-1]["message"] = ("the engine has not reported run_started yet; repeat "
                                          "start_run with the same key to keep observing")
        return steps.done(message="", run_id=run)
    state = api.request("GET", _run_path(run, "/state?observe_only=true"))
    generation = (state.get("body") or {}).get("generation") if isinstance(state.get("body"), dict) else None
    if not steps.add("read_state", state, ok=_ok(state) and isinstance(generation, str)):
        return steps.done(message="", run_id=run)
    progress = api.run_progress(run, generation)
    next_step = (progress.get("body") or {}).get("next_step") if _ok(progress) else None
    steps.add("read_progress", progress)
    execution = (progress.get("body") or {}).get("execution") if _ok(progress) else None
    if isinstance(execution, dict) and execution.get("engine_running") is True:
        # One more observation now that the engine lock is visible: it retires the start's spawn
        # lease (`serve/start_record.py::reconcile_start`), so the first command is not refused.
        api.request("GET", status_path, idempotency_key=start_key)
    base = str(api.client.base_url).rstrip("/")
    return steps.done(message="run started; drive it with run_progress, submit_experiment and "
                              "wait_for_next. Give the user ui_url.",
                      run_id=run, generation=generation, ui_url=f"{base}/#/run/{quote(run, safe='')}",
                      next_step=next_step)

