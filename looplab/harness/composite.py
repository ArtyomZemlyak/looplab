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

Every write step derives its idempotency key / action id from the caller's `key`, so repeating the
same call after a lost response resolves each step to its original receipt instead of repeating it.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

MAX_UPLOAD_FILE_BYTES = 24 * 1024 * 1024
_TERMINAL = {"succeeded", "noop", "failed", "rejected", "timed_out"}


def _run_path(run_id: str, suffix: str = "") -> str:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise ValueError("run_id must be one literal run identifier")
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
        return {"ok": failed is None, "stopped_at": failed["step"] if failed else None,
                "steps": self.steps, "message": message if failed is None else (
                    f"stopped at {failed['step']}: read its receipt; repeat this call with the SAME "
                    "key to resume (completed steps replay their original receipts)"), **extra}


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


def submit_experiment(api, run_id: str, expected_generation: str, *, key: str, idea: dict,
                      files: Optional[dict] = None, code: Optional[str] = None,
                      deleted: Optional[list] = None, parent_id: Optional[int] = None,
                      research_question: Optional[dict] = None,
                      experiment: Optional[dict] = None, experiment_reason: str = "",
                      memo: Optional[dict] = None, decisions: Optional[dict] = None,
                      dry_run: bool = False) -> dict:
    steps = _Steps()
    if not isinstance(idea, dict) or not isinstance(idea.get("operator"), str):
        raise ValueError("idea must be an object with an operator")
    if not key or len(key) > 120:
        raise ValueError("key must be a stable non-empty string of at most 120 characters")
    idea = dict(idea)
    progress = api.run_progress(run_id, expected_generation)
    if not steps.add("read_progress", progress):
        return steps.done(message="")
    body = progress["body"]
    required = body.get("candidate_decisions_per_idea") or {}
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
    if not (files or code or deleted):
        missing.append("files or code (an external run evaluates ready-made candidates)")
    if research_question is not None:
        if not isinstance(research_question, dict) or not research_question.get("id"):
            missing.append("research_question.id")
        else:
            idea.setdefault("parent_card_id", research_question["id"])
    plan = (["register experiment"] if experiment else []) + \
        (["add research question"] if research_question and research_question.get("statement") else []) + \
        (["publish research memo"] if memo else []) + \
        [f"decision {p}" for p in decisions] + ["inject_node"]
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
        replay = (result.get("status") == 409 and isinstance(result.get("body"), dict)
                  and ((result["body"].get("detail") or {}) if isinstance(
                      result["body"].get("detail"), dict) else {}).get("code") == "experiment_exists")
        if not steps.add("register_experiment", result, ok=_ok(result) or replay):
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
                        ("parent_id", parent_id)):
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
        f"{key}:reset:{node_id}:{generation}", settle_seconds=5.0)
    steps.add("reset", result)
    return steps.done(message=(f"node {node_id} queued for attempt {int(generation) + 1}; "
                               "earlier attempts stay in the event log and the attempt_logs/ archive"))


def upload_per_query(api, run_id: str, expected_generation: str, *, node_id: int, path: str,
                     metrics: dict, action_id: str, test_label: Optional[str] = None,
                     experiment_id: Optional[str] = None, arm: Optional[str] = None,
                     query_key: str = "keys", cluster_key: Optional[str] = "users",
                     measured_check: Optional[dict] = None) -> dict:
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError("path must be a readable local file")
    size = source.stat().st_size
    if size > MAX_UPLOAD_FILE_BYTES:
        raise ValueError(f"per-query files are at most {MAX_UPLOAD_FILE_BYTES} bytes")
    fmt = "json" if source.suffix.lower() == ".json" else "npz"
    body = {"expected_generation": expected_generation, "action_id": action_id,
            "node_id": node_id, "format": fmt,
            "content_b64": base64.b64encode(source.read_bytes()).decode("ascii"),
            "metrics": metrics, "query_key": query_key, "cluster_key": cluster_key,
            **{k: v for k, v in (("test_label", test_label), ("experiment_id", experiment_id),
                                 ("arm", arm), ("measured_check", measured_check))
               if v is not None}}
    return api.post_large(_run_path(run_id, "/per-query"), body)


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


__all__ = ["campaign_supertask", "finish_run", "retry_node", "submit_experiment",
           "upload_per_query"]
