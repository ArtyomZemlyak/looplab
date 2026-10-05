"""Stdio MCP adapter over LoopLab's authenticated, durable HTTP API.

One API, one validation boundary: MCP never writes events or reads workspaces directly.
It forwards to the same routes and command service the UI uses. The external agent can
discover every current operation from OpenAPI, even when new endpoints are added later.
"""
from __future__ import annotations

import json
import os
import re
from typing import Literal
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx

from looplab.harness.manifest import harness_manifest
from looplab.harness.phases import phase_catalog, phase_detail


MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
WRITE_RECOVERY_MESSAGE = (
    "API write acknowledgement unavailable; the server may already have accepted or applied it. "
    "Read original receipts and current state/checkpoints before retrying. For commands, use "
    "command_receipt with the original Idempotency-Key and generation; preserve the exact body/key. "
    "For other actions preserve their original action_id and exact body. Do not create a new key "
    "or action_id to recover a missing acknowledgement. No automatic retry was made."
)
MCP_INSTRUCTIONS = (
    "LoopLab evaluates ready-made experiments; the external agent proposes candidates. "
    "Start with capabilities and connection_check, then read /state?observe_only=true, task, config and harness-contract. "
    "Call run_progress with the current generation; inspect source_health and checkpoints. "
    "Search phases and read phase_info before decisions. On reconnect, read command_receipt "
    "before retrying; preserve the exact payload and original key. Explicitly pause or finalize. "
    "Quiet logs do not prove agent or engine liveness. "
    "MCP Connected proves stdio only; use connection_check for live run reads. "
    "Client tool approval may still be required. Inspect isError/is_error, permission_denials and HTTP status; exit 0 is not an applied command receipt. "
    "Transport loss returns status=null. A write acknowledgement can also be unknown after 5xx, oversized or invalid JSON responses, even with HTTP 200. Inspect outcome/code and original receipts before any exact retry, never recover by inventing a new key. "
    "Typed progress/result/command reads verify the returned generation and command receipt ID; response_context_mismatch is unavailable evidence even at HTTP 200. Refresh state/original receipts before acting. "
    "Live operations/operation_schema discovery can also be unavailable. Check code/outcome before choosing routes; a failed catalog read is not an empty capability list. "
    "Follow enabled admission/finish obligations. A trainer exit is not terminal evaluation. "
    "For MCTS value reviews, retain the original expected_evidence_revision with estimates/action_id. Exact replay acknowledges the old batch; changing its revision is a conflict. Read the current complete batch and author a new action after reset. "
    "Exact decision/review retries restore original receipts, not current evidence approval. Refresh progress validity; superseded receipts require a new justified decision/review. HTTP 200 commands can be rejected: inspect receipt status/error. "
    "Incomplete decision/review sources refuse replay, writes and dependent obligations. Inspect progress source_health and ask the operator to recover the journal before retrying; the harness never repairs it or resumes work automatically. "
    "Hypothesis/selection reads and decision/review/hypothesis/verifier/value publications also require a healthy event prefix; incomplete/unavailable refusals name events.jsonl with available health diagnostics. Read progress for diagnostics, then resolve original requests explicitly after operator recovery. "
    "inject_node succeeded means admission, not training success. Inspect terminal status/results; read current-attempt logs before repairing a failure with a new child and command key. A failed parent has no measured score delta. "
    "After each terminal node and finalized run, call result_notices with the current generation and POST "
    "a brief interpretation in the user's language with receipt_id, evidence_token and a stable "
    "action_id; retry a lost response with the exact body. Scores come from LoopLab, not prose. "
    "Typed result pages also validate the version-1 envelope, paging and terminal receipt identities/numeric fields; invalid_result_page with HTTP 200 is unavailable, not empty results. Read again explicitly; no automatic repair/retry. On reconnect, follow result-notices.next_cursor for older receipts, preserving expected_generation; only publish missing current commentary. A changed cursor requires refreshing the latest page. "
    "Commentary executes no actions and never replaces checkpoints or report obligations. "
    "Keep original lesson/skill bodies and action IDs. Exact retries acknowledge stored actions without refreshing signatures, restoring retired support or granting promotion; fresh writes check current evidence. Shared/researcher/developer lesson roles are retained. Damaged event/knowledge sources and fresh completed knowledge reviews refuse with a named source. Inspect refusal health and request operator recovery; no automatic repair/resume. "
    "Typed command_receipt also verifies v1 status/terminal consistency, control event, sequence, error_code and retryable fields. Incomplete HTTP 200 means unavailable without body, not a command verdict. Read again explicitly; a terminal rejected/failed receipt or succeeded inject still does not prove completed training. "
    "Typed run_progress and connection_check validate critical progress fields, source completeness, explicit expansion/finish gates and pending counts. Missing fields are unavailable, not empty obligations. A structurally valid complete=false read retains source diagnostics; read success does not grant admission. Refresh explicitly; no automatic retry or work. "
    "Use only the scoped harness credential; owner-only workflows require the operator."
)


class HarnessAPI:
    def __init__(self, url: str, token: str = "", *, transport=None):
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValueError("LOOPLAB_HARNESS_URL must be a valid HTTP(S) server URL") from None
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("LOOPLAB_HARNESS_URL must be an HTTP(S) server URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("LOOPLAB_HARNESS_URL cannot contain query or fragment")
        try:
            self.client = httpx.Client(base_url=url.rstrip("/") + "/", timeout=30,
                                      follow_redirects=False, trust_env=False, transport=transport,
                                      headers={"X-LoopLab-Token": token} if token else {})
        except httpx.InvalidURL:
            raise ValueError("LOOPLAB_HARNESS_URL must be a valid HTTP(S) server URL") from None

    @staticmethod
    def _path(path: str) -> str:
        parsed = urlsplit(path)
        decoded = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/api/")
                or "\\" in decoded or "//" in decoded
                or any(part in (".", "..") for part in decoded.split("/"))):
            raise ValueError("path must be a relative /api/... route without traversal or fragment")
        return path.lstrip("/")

    @staticmethod
    def _unknown_write(result: dict, reason: str) -> dict:
        return {**result, "code": "request_outcome_unknown", "outcome": "unknown",
                "reason": reason, "message": WRITE_RECOVERY_MESSAGE}

    @staticmethod
    def _checked_read(result: dict) -> dict:
        # Typed discovery reads cannot treat a login page or JSON null/list as evidence.
        # This checks the top-level envelope only; domain schemas and fences still apply.
        if result["status"] == 200 and not result.get("code") and not isinstance(result.get("body"), dict):
            return {"status": 200, "code": "response_incomplete", "outcome": "unavailable",
                    "reason": "invalid_response",
                    "message": "API read did not return a JSON object. Inspect the server and refresh this read; do not act on incomplete evidence."}
        return result

    @staticmethod
    def _read_refusal(reason: str, *, mismatch: bool = False) -> dict:
        return {"status": 200, "code": "response_context_mismatch" if mismatch else "response_incomplete",
                "outcome": "unavailable", "reason": reason,
                "message": "Run/command response did not match the requested identity. Refresh state and original receipts before acting; no retry or worker restart was made."}

    def _checked_generation(self, result: dict, expected: str) -> dict:
        result = self._checked_read(result)
        if result["status"] != 200 or result.get("code"):
            return result
        generation = result["body"].get("generation")
        if not isinstance(generation, str) or re.fullmatch(r"[a-fA-F0-9]{64}", generation) is None:
            return self._read_refusal("invalid_response")
        if generation.lower() != expected.lower():
            return self._read_refusal("generation_mismatch", mismatch=True)
        return result

    def _result(self, response: httpx.Response, *, method: str = "GET") -> dict:
        write = method != "GET"
        if len(response.content) > MAX_RESPONSE_BYTES:
            result = {"status": response.status_code, "truncated": True, "bytes": len(response.content)}
            if write and (200 <= response.status_code < 300 or response.status_code >= 500):
                return self._unknown_write(result, "response_too_large")
            return {**result, "code": "response_incomplete", "outcome": "unavailable",
                    "reason": "response_too_large", "message": "Use a narrower API query; do not act on a partial response."}
        try:
            body = response.json()
        except ValueError:
            if write and 200 <= response.status_code < 300 and not (
                    response.status_code in (204, 205) and not response.content):
                return self._unknown_write({"status": response.status_code}, "invalid_json")
            body = response.text
        result = {"status": response.status_code, "body": body}
        if write and response.status_code >= 500:
            return self._unknown_write(result, "server_error")
        return result

    def _openapi(self) -> tuple[dict | None, dict | None]:
        # Discovery reads the full live catalog, then narrows locally. Do not apply
        # the individual API-reply cap here: the real catalog spans many routes.
        # Refused/incomplete discovery must not masquerade as zero capabilities.
        try:
            response = self.client.get("openapi.json")
        except httpx.TransportError:
            return None, {"status": None, "code": "api_unreachable", "outcome": "unavailable", "at": "openapi",
                          "message": "Live OpenAPI response unavailable. Check the UI/server and explicitly repeat discovery; no retry or work was started."}
        if response.status_code != 200:
            return None, {"status": response.status_code, "code": "api_read_failed", "outcome": "unavailable", "at": "openapi",
                          "message": "Live OpenAPI read refused. Check the server/access before explicitly repeating discovery; no work was started."}
        invalid = {"status": 200, "code": "response_incomplete", "outcome": "unavailable",
                   "at": "openapi", "reason": "invalid_response",
                   "message": "Live OpenAPI catalog is incomplete. Inspect the server and repeat discovery before choosing routes; this is not an empty capability catalog."}
        try:
            spec = response.json()
        except ValueError:
            return None, invalid
        if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
            return None, invalid
        for methods in spec["paths"].values():
            if not isinstance(methods, dict) or any(
                    not isinstance(value, dict) for method, value in methods.items()
                    if method in ("get", "post", "put", "patch", "delete")):
                return None, invalid
        components = spec.get("components", {})
        if not isinstance(components, dict) or not isinstance(components.get("schemas", {}), dict):
            return None, invalid
        return spec, None

    def operations(self, query: str = "", limit: int = 50) -> dict:
        schema, error = self._openapi()
        if error is not None:
            return error
        matches = []
        needle = query.casefold().strip()
        for path, methods in (schema.get("paths") or {}).items():
            for method, spec in methods.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                row = {"method": method.upper(), "path": path,
                       "summary": spec.get("summary", ""), "operation_id": spec.get("operationId", "")}
                if needle and needle not in json.dumps(row, ensure_ascii=False).casefold():
                    continue
                matches.append(row)
        return {"matches": matches[:max(1, min(limit, 100))], "total": len(matches)}

    def schema(self, path: str) -> dict:
        route = "/" + self._path(path).split("?", 1)[0]
        spec, error = self._openapi()
        if error is not None:
            return error
        operations = (spec.get("paths") or {}).get(route)
        definitions = spec.get("components", {}).get("schemas", {})
        needed: dict = {}

        def collect(value):
            if isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                ref = value.get("$ref", "")
                prefix = "#/components/schemas/"
                if isinstance(ref, str) and ref.startswith(prefix):
                    name = ref[len(prefix):]
                    if name in definitions and name not in needed:
                        needed[name] = definitions[name]
                        collect(needed[name])
                for item in value.values():
                    collect(item)

        collect(operations)
        return {"path": route, "operations": operations, "components": needed}

    def request(self, method: str, path: str, body: dict | None = None,
                idempotency_key: str = "") -> dict:
        verb = method.upper()
        if verb not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("unsupported HTTP method")
        if body is not None and (not isinstance(body, dict)
                                 or len(json.dumps(body).encode("utf-8")) > MAX_REQUEST_BYTES):
            raise ValueError("body must be a JSON object of at most 1 MiB")
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        route = self._path(path)
        try:
            response = self.client.request(verb, route, json=body, headers=headers)
        except httpx.TransportError:
            # Never echo transport exception text: it may contain sensitive URLs.
            # Even a timeout after sending a write can hide an accepted command.
            if verb == "GET":
                return {"status": None, "code": "api_unreachable", "outcome": "unavailable",
                        "message": "API read response unavailable. Check the UI/server and read again explicitly; missing evidence does not prove no action occurred."}
            return self._unknown_write({"status": None}, "transport_error")
        return self._result(response, method=verb)

    def run_progress(self, run_id: str, expected_generation: str, language: str = "en") -> dict:
        self._run_identity(run_id, expected_generation)
        if language not in ("en", "ru"):
            raise ValueError("language must be en or ru")
        result = self._checked_generation(self.request("GET", f"/api/runs/{quote(run_id, safe='')}/harness-progress"
                            f"?expected_generation={expected_generation}&brief=true"
                            + ("&language=ru" if language == "ru" else "")), expected_generation)
        if result["status"] == 200 and not result.get("code") and (not self._valid_progress(result["body"])
                or ("language" in result["body"]["next_step"]
                    and result["body"]["next_step"]["language"] != language)):
            return {"status": 200, "code": "response_incomplete", "outcome": "unavailable",
                    "reason": "invalid_progress",
                    "message": "Progress fields are incomplete or inconsistent. Read current state and refresh progress explicitly before deciding; missing gates are not empty obligations. No retry or work was started."}
        return result

    @staticmethod
    def _valid_progress(page: dict) -> bool:
        # Validate the critical compact observation, not the server's decision
        # policy. Incomplete sources remain useful diagnostics when explicitly
        # represented. Missing health/gates cannot silently mean no obligations.
        def count(value):
            return type(value) is int and value >= 0
        def strings(value):
            return isinstance(value, list) and all(isinstance(item, str) and item for item in value)
        health, step = page.get("source_health"), page.get("next_step")
        execution, lifecycle = page.get("execution"), page.get("recorded_lifecycle")
        requirements, blockers = page.get("candidate_requirements"), page.get("candidate_blockers_if_expanding")
        decisions = page.get("candidate_decisions_per_idea")
        if not (isinstance(page.get("run_uid"), str) and count(page.get("event_seq"))
                and count(page.get("at_node")) and isinstance(page.get("evidence_revision"), str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", page["evidence_revision"]) is not None
                and type(page.get("complete")) is bool and isinstance(health, dict)
                and all(isinstance(health.get(name), dict) and type(health[name].get("read_complete")) is bool
                        for name in ("events", "decisions", "reviews", "checkpoints"))
                and all(isinstance(source, dict) and type(source.get("read_complete")) is bool
                        for source in health.values())
                and page["complete"] == all(source["read_complete"] for source in health.values())
                and isinstance(step, dict) and step.get("owner") == "external_agent"
                and ("language" not in step or step["language"] in ("en", "ru"))
                and all(isinstance(step.get(key), str) and step[key] for key in ("code", "title", "detail"))
                and strings(step.get("reads")) and all(key in step and
                    (step[key] is None or isinstance(step[key], str)) for key in ("action", "phase_id"))
                and isinstance(lifecycle, dict) and all(type(lifecycle.get(key)) is bool for key in
                                                      ("paused", "finished", "stop_requested"))
                and isinstance(execution, dict) and "engine_running" in execution
                and (execution["engine_running"] is None or type(execution["engine_running"]) is bool)
                and execution.get("agent_connection") == "not_measured"
                and isinstance(execution.get("recorded_node_counts"), dict)
                and all(count(execution["recorded_node_counts"].get(key)) for key in
                        ("building", "queued", "evaluating", "pending"))
                and isinstance(requirements, dict) and all(type(requirements.get(key)) is bool for key in
                                                          ("effective_concepts", "hypothesis_statement"))
                and isinstance(blockers, list) and all(isinstance(row, dict) and all(
                    isinstance(row.get(key), str) and row[key] for key in ("phase_id", "action")) for row in blockers)
                and isinstance(decisions, dict) and all(isinstance(key, str) and key and count(value)
                    and value > 0 for key, value in decisions.items()) and strings(page.get("finish_reviews_due"))
                and type(page.get("finish_report_due")) is bool):
            return False
        for field, total_key in (("finish_pending_nodes", "finish_pending_node_count"),
                                 ("pending_checkpoints", "pending_checkpoint_count")):
            rows, total, truncated = page.get(field), page.get(total_key), page.get(field + "_truncated")
            if not (isinstance(rows, list) and count(total) and len(rows) == min(total, 20)
                    and type(truncated) is bool and truncated == (total > len(rows))):
                return False
        return (all(count(nid) for nid in page["finish_pending_nodes"])
                and all(isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key]
                    for key in ("checkpoint_id", "phase_id")) and all(count(row.get(key)) for key in
                    ("node_id", "node_generation"))
                    # The server retains -1 for a recorded question with no
                    # invocation claim. This is an observation, not abort authority.
                    and type(row.get("claim_seq")) is int and row["claim_seq"] >= -1
                    for row in page["pending_checkpoints"]))

    def result_notices(self, run_id: str, expected_generation: str,
                       limit: int = 50, cursor: str | None = None) -> dict:
        self._run_identity(run_id, expected_generation)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 256):
            raise ValueError("cursor must be the unchanged next_cursor, at most 256 characters")
        # Cursor is opaque client-side. The server binds its scope and anchor;
        # encoding a single query avoids hand-built URLs and follows no page itself.
        query = {"expected_generation": expected_generation, "limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        path = f"/api/runs/{quote(run_id, safe='')}/result-notices?{urlencode(query)}"
        result = self._checked_generation(self.request("GET", path), expected_generation)
        if result["status"] != 200 or result.get("code"):
            return result
        # Matching generation alone cannot turn missing results/paging or malformed
        # terminal identities into completion evidence. Keep unknown extra fields
        # for compatible server additions; never coerce, repair, page or retry here.
        page = result["body"]
        items = page.get("items")
        total = page.get("total")
        more = page.get("has_more")
        next_cursor = page.get("next_cursor")
        valid = (type(page.get("version")) is int and page["version"] == 1
                 and type(total) is int and total >= 0 and isinstance(items, list)
                 and len(items) <= min(limit, total) and type(more) is bool
                 and "next_cursor" in page
                 and ((more and isinstance(next_cursor, str) and 1 <= len(next_cursor) <= 256)
                      or (more is False and next_cursor is None))
                 and (not more or (bool(items) and total > len(items))))
        if valid and "cursor" not in query:
            valid = more == (total > len(items))
        identities = set()
        for row in items if valid else ():
            valid = isinstance(row, dict)
            if valid:
                rid, token = row.get("id"), row.get("evidence_token")
                valid = (isinstance(rid, str) and rid not in identities
                         and isinstance(token, str) and re.fullmatch(r"[0-9a-f]{64}", token) is not None
                         and ("commentary_source" not in row or row["commentary_source"] in
                              (None, "assistant", "external"))
                         and ("commentary_status" not in row or row["commentary_status"] in
                              ("none", "generating", "ready", "published", "failed", "interrupted",
                               "superseded", "unavailable"))
                         and all(key in row and (row[key] is None or
                             (type(row[key]) in (int, float)
                              and -float("inf") < row[key] < float("inf")))
                             for key in ("score", "confirmed_mean", "confirmed_std"))
                         and (row["confirmed_std"] is None or
                              row["confirmed_std"] >= 0 and row["confirmed_mean"] is not None))
            if valid and row.get("kind") == "node":
                from looplab.harness.result_receipts import valid_score_comparison, valid_trust_evidence
                nid, attempt = row.get("node_id"), row.get("attempt")
                valid = (type(nid) is int and nid >= 0 and type(attempt) is int and attempt >= 0
                         and rid == f"node:{nid}:{attempt}"
                         and row.get("status") in ("evaluated", "failed", "aborted")
                         and (row["status"] == "evaluated" or
                              (row["score"] is None and row["confirmed_mean"] is None and row["confirmed_std"] is None))
                         and valid_score_comparison(row) and valid_trust_evidence(row))
            elif valid:
                from looplab.harness.result_receipts import valid_run_result
                valid = (row.get("kind") == "run" and rid == "run" and row.get("status") == "finished"
                         and valid_run_result(row))
            if not valid:
                break
            identities.add(rid)
        if not valid:
            return {"status": 200, "code": "response_incomplete", "outcome": "unavailable",
                    "reason": "invalid_result_page",
                    "message": "Completion page is incomplete or inconsistent. Read it again explicitly before interpreting results; no paging, retry or engine work was made."}
        return result

    def upstream_request(self, run_id: str, expected_generation: str, proposal_id: str,
                         expected_request_hash: str, offset: int = 0, limit: int = 2048,
                         expected_content_hash: str | None = None) -> dict:
        self._run_identity(run_id, expected_generation)
        from looplab.engine.upstream_requests import validate_selector
        from looplab.harness.upstream_requests import valid_page
        validate_selector(proposal_id, expected_request_hash, offset, limit, expected_content_hash)
        query = {"expected_generation": expected_generation, "expected_request_hash": expected_request_hash,
                 "offset": offset, "limit": limit}
        if expected_content_hash is not None:
            query["expected_content_hash"] = expected_content_hash
        result = self._checked_generation(self.request("GET",
            f"/api/runs/{quote(run_id, safe='')}/upstream/requests/{proposal_id}?{urlencode(query)}"), expected_generation)
        if result.get("status") == 200 and not result.get("code") and not valid_page(
                result["body"], expected_generation, proposal_id, expected_request_hash, offset, limit, expected_content_hash):
            return self._read_refusal("invalid_upstream_request_page")
        return result

    def upstream_status(self, run_id: str, expected_generation: str, offset: int = 0, limit: int = 40,
                        source_node_id: int | None = None, candidate_offset: int = 0, candidate_limit: int = 200) -> dict:
        self._run_identity(run_id, expected_generation)
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
                or type(candidate_offset) is not int or candidate_offset < 0
                or type(candidate_limit) is not int or not 1 <= candidate_limit <= 200
                or source_node_id is not None and (type(source_node_id) is not int or source_node_id < 0)):
            raise ValueError("Use nonnegative source/offsets, history limit 1..100 and candidate limit 1..200")
        query = f"expected_generation={expected_generation}&offset={offset}&limit={limit}&candidate_offset={candidate_offset}&candidate_limit={candidate_limit}"
        if source_node_id is not None:
            query += f"&source_node_id={source_node_id}"
        result = self._checked_generation(self.request("GET", f"/api/runs/{quote(run_id, safe='')}/upstream?{query}"), expected_generation)
        if result.get("status") == 200 and not result.get("code"):
            page = result["body"]
            active, candidates = page.get("active_base"), page.get("candidates")
            valid = (type(page.get("version")) is int and page["version"] == 1 and type(page.get("enabled")) is bool and type(page.get("engine_running")) is bool
                and page.get("source_health") == {"events": "complete"} and isinstance(page.get("history"), list)
                and len(page["history"]) <= limit and "next_offset" in page
                and (page["next_offset"] is None or type(page["next_offset"]) is int
                    and page["next_offset"] == offset + limit and len(page["history"]) == limit)
                and isinstance(active, dict) and isinstance(active.get("revision"), str)
                and re.fullmatch(r"[0-9a-f]{64}", active["revision"]) is not None
                and "selector" in active and "advance_seq" in active
                and isinstance(candidates, dict) and isinstance(candidates.get("rows"), list)
                and type(candidates.get("bounded")) is bool and type(candidates.get("limit")) is int
                and candidates["limit"] == candidate_limit
                and candidates.get("offset", 0) == candidate_offset
                and candidates.get("source_node_id") == source_node_id
                and (candidate_offset == 0 and candidate_limit == 200 and source_node_id is None
                     or {"offset", "next_offset", "source_node_id"} <= set(candidates)))
            from looplab.harness.upstream_receipts import page_detail
            if not valid or not page_detail(page):
                return self._read_refusal("invalid_upstream_page")
        return result

    def upstream_write(self, run_id: str, operation: str, body: dict) -> dict:
        if operation not in ("proposals", "check", "advance") or not isinstance(body, dict):
            raise ValueError("Use propose/check/advance and the retained exact request body")
        self._run_identity(run_id, body.get("expected_generation", ""))
        result = self.request("POST", f"/api/runs/{quote(run_id, safe='')}/upstream/{operation}", body)
        if result.get("status") == 200 and not result.get("code"):
            from looplab.engine.upstream_state import digest
            from looplab.engine.upstream_spec import normalize_request
            receipt = result.get("body")
            expected = {"proposals": {"upstream_proposed", "upstream_proposal_failed"},
                        "check": {"upstream_gate_finished", "upstream_gate_abandoned"}, "advance": {"base_advanced"}}[operation]
            valid = (isinstance(receipt, dict) and type(receipt.get("version")) is int and receipt["version"] == 1
                and receipt.get("action_id") == body.get("action_id") and receipt.get("request_hash") == digest(normalize_request("propose" if operation == "proposals" else operation, body))
                and receipt.get("event_type") in expected and receipt.get("status") in ("succeeded", "failed")
                and type(receipt.get("seq")) is int and receipt["seq"] >= 0
                and isinstance(receipt.get("proposal_id"), str) and re.fullmatch(r"up_[0-9a-f]{24}", receipt["proposal_id"]) is not None)
            if valid and operation == "check" and receipt["event_type"] == "upstream_gate_finished":
                measured = receipt.get("result")
                valid = (isinstance(measured, dict) and type(measured.get("passed")) is bool
                    and receipt["status"] == ("succeeded" if measured["passed"] else "failed")
                    and isinstance(measured.get("checks"), list) and isinstance(measured.get("executions"), list))
            # event validates numeric evidence and safely checks its canonical
            # hash. Never hash an untrusted result first: JSON 1e309 decodes to inf.
            from looplab.harness.upstream_receipts import event
            if valid:
                valid = event(receipt, receipt["event_type"]) and receipt["status"] == (
                    "failed" if receipt["event_type"] == "upstream_proposal_failed" or receipt["event_type"] == "upstream_gate_finished" and receipt["result"]["passed"] is False else "succeeded")
            if valid and operation != "proposals":
                valid = receipt["proposal_id"] == body.get("proposal_id")
            if valid and operation == "proposals":
                valid = receipt["proposal_id"] == "up_" + digest(body["action_id"])[:24]
                if valid and receipt["event_type"] == "upstream_proposed":
                    valid = (receipt["source_node_id"] == body.get("source_node_id")
                        and receipt["expected_base_revision"] == body.get("expected_base_revision")
                        and receipt["flag"] == body.get("flag") and receipt["critic"] == body.get("critic"))
            if valid and operation == "advance":
                valid = (receipt["from_revision"] == body.get("expected_base_revision")
                    and receipt["evidence_token"] == body.get("evidence_token"))
            if not valid:
                return self._unknown_write({"status": result["status"]}, "invalid_upstream_receipt")
        return result

    def connection_check(self, run_id: str, expected_generation: str = "") -> dict:
        """Explicit, read-only bootstrap check; never resumes a worker or probes a model.

        Successful MCP initialization alone does not test this API or credential. The
        state read discovers the generation; handoff/progress fence subsequent reads.
        An operator-supplied generation must match before reading the new incarnation.
        Errors use fixed messages rather than HTTP exception text, URLs or raw bodies.
        Success proves these reads, not agent liveness, credential scope or admission.
        """
        self._run_identity(run_id, expected_generation or "0" * 64)
        base = f"/api/runs/{quote(run_id, safe='')}"

        def read(suffix, phase):
            try:
                result = self.request("GET", base + suffix)
            except (httpx.HTTPError, httpx.InvalidURL):
                return {"ok": False, "status": None, "code": "api_unreachable", "at": phase,
                        "message": "UI/API request failed. Check the server URL, network and running UI."}
            status = result["status"]
            if status != 200:
                code, message = {
                    None: ("api_unreachable", "UI/API request failed. Check the server URL, network and running UI."),
                    401: ("credential_refused", "API rejected the credential. Ask the operator for the scoped token."),
                    403: ("access_refused", "API refused this read. Check the credential and launched run mode."),
                    404: ("run_not_found", "Run not found on this server. Check the URL, run root and literal run ID."),
                    409: ("run_context_changed", "Run context changed or is unavailable. Read state and request fresh handoff."),
                    503: ("source_unavailable", "Run sources are unavailable. Inspect source health before acting."),
                }.get(status, ("api_read_failed", "API read failed. Inspect the server before acting."))
                return {"ok": False, "status": status, "code": code, "at": phase, "message": message}
            if result.get("truncated") or not isinstance(result.get("body"), dict):
                return {"ok": False, "status": status, "code": "invalid_response", "at": phase,
                        "message": "API returned incomplete connection evidence. Do not act on this check."}
            return {"ok": True, "body": result["body"]}

        state = read("/state?observe_only=true", "state")
        if state.get("ok") is False:
            return state
        state = state["body"]
        generation = state.get("generation")
        if not isinstance(generation, str) or re.fullmatch(r"[a-fA-F0-9]{64}", generation) is None:
            return {"ok": False, "status": 200, "code": "invalid_response", "at": "state",
                    "message": "State did not supply a valid generation. Request fresh connection context."}
        # Generation is a hex digest, not a case-sensitive human identifier.
        generation = generation.lower()
        if expected_generation and expected_generation.lower() != generation:
            return {"ok": False, "status": 409, "code": "generation_mismatch", "at": "state",
                    "message": "This handoff belongs to another run generation. Request fresh context."}
        handoff = read(f"/harness-handoff?expected_generation={generation}", "handoff")
        if handoff.get("ok") is False:
            return handoff
        handoff = handoff["body"]
        if handoff.get("credential_configured") is False:
            return {"ok": False, "status": 200, "code": "harness_credential_missing", "at": "handoff",
                    "message": "This server has no harness credential configured. Ask the operator to configure it and restart the UI."}
        paths = handoff.get("server_paths")
        if (handoff.get("run_id") != run_id or handoff.get("generation") != generation
                or handoff.get("mode") != "external_harness" or handoff.get("credential_configured") is not True
                or not isinstance(paths, dict)
                or any(not isinstance(paths.get(key), str) or not paths[key] for key in ("run_root", "run_dir"))
                or (handoff.get("engine_running") is not None and type(handoff["engine_running"]) is not bool)):
            return {"ok": False, "status": 200, "code": "invalid_response", "at": "handoff",
                    "message": "Handoff does not match this external run. Request fresh context before reading progress."}
        progress = read(f"/harness-progress?expected_generation={generation}&brief=true", "progress")
        if progress.get("ok") is False:
            return progress
        progress = progress["body"]
        if progress.get("generation") != generation or not self._valid_progress(progress):
            return {"ok": False, "status": 200, "code": "invalid_response", "at": "context",
                    "message": "Connection evidence does not match this external run. Request fresh context."}
        return {"ok": True, "status": 200, "code": "run_reads_succeeded", "run_id": run_id,
                "generation": generation, "server_paths": {key: paths[key] for key in ("run_root", "run_dir")},
                "engine_running": handoff.get("engine_running"), "agent_connection": "not_measured",
                "evidence_complete": progress["complete"],
                "source_health": progress.get("source_health"), "next_step": progress.get("next_step"),
                "message": "Live run reads succeeded. Check source health, obligations and engine status before decisions; this check starts no work."}

    @staticmethod
    def _run_identity(run_id: str, expected_generation: str):
        if (not run_id or run_id in (".", "..")
                or any(char in run_id for char in "/\\")):
            raise ValueError("run_id must be one literal run identifier")
        if re.fullmatch(r"[a-fA-F0-9]{64}", expected_generation) is None:
            raise ValueError("expected_generation must be the SHA-256 token from /state")

    def command_receipt(self, run_id: str, expected_generation: str,
                        command_id: str = "", idempotency_key: str = "") -> dict:
        self._run_identity(run_id, expected_generation)
        if bool(command_id) == bool(idempotency_key):
            raise ValueError("Supply exactly one command_id or idempotency_key")
        if command_id and re.fullmatch(r"cmd_[0-9a-f]{32}", command_id) is None:
            raise ValueError("Invalid durable command ID")
        if idempotency_key and (len(idempotency_key) > 512
                                or any(ord(ch) < 32 or ord(ch) == 127 for ch in idempotency_key)):
            raise ValueError("Invalid idempotency key")
        path = f"/api/runs/{quote(run_id, safe='')}/command-receipt?expected_generation={expected_generation}"
        if command_id:
            path += f"&command_id={command_id}"
        result = self._checked_generation(self.request("GET", path, idempotency_key=idempotency_key), expected_generation)
        if result["status"] != 200 or result.get("code"):
            return result
        command = result["body"].get("command")
        if not isinstance(command, dict) or not isinstance(command.get("id"), str) or re.fullmatch(r"cmd_[0-9a-f]{32}", command["id"]) is None:
            return self._read_refusal("invalid_response")
        if idempotency_key:
            # Same pure derivation as server submission/observation, not a second hash rule.
            # Valid keys require no FastAPI import, so a remote MCP client needs no UI extra.
            from looplab.serve.command_identity import command_identity

            command_id = command_identity(idempotency_key)[0]
        if command["id"] != command_id:
            return self._read_refusal("command_mismatch", mismatch=True)
        # Share the UI-free wire constants with the server, not a client status
        # table or an import of its optional FastAPI command implementation.
        from looplab.serve.protocol import (COMMAND_STATUSES, COMMAND_TERMINAL_STATUSES,
                                           COMMAND_RECEIPT_ERROR_CAP, CONTROL_EVENTS)

        page = result["body"]
        status, event = command.get("status"), command.get("event_type")
        seq, error = command.get("event_seq"), command.get("error_code")
        if not (type(page.get("version")) is int and page["version"] == 1
                and type(page.get("terminal")) is bool
                and isinstance(status, str) and status in COMMAND_STATUSES
                and page["terminal"] == (status in COMMAND_TERMINAL_STATUSES)
                and isinstance(event, str) and event in CONTROL_EVENTS
                and "event_seq" in command and (seq is None or (type(seq) is int and seq >= 0))
                and isinstance(error, str) and len(error) <= COMMAND_RECEIPT_ERROR_CAP
                and type(command.get("retryable")) is bool):
            return {"status": 200, "code": "response_incomplete", "outcome": "unavailable",
                    "reason": "invalid_command_receipt",
                    "message": "Command receipt is incomplete or inconsistent. Read the original receipt and current state explicitly before choosing recovery; no worker restart or retry was made."}
        return result


def build_server(api: HarnessAPI):
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    mcp = FastMCP("looplab", instructions=MCP_INSTRUCTIONS)

    @mcp.tool()
    def capabilities() -> dict:
        """Discover LoopLab phases, backend support, limits and control interfaces."""
        return harness_manifest()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                                         destructiveHint=False, openWorldHint=True))
    def connection_check(run_id: str, expected_generation: str = "") -> dict:
        """Check UI/API access to one launched external run, beyond MCP Connected status.

        Performs GET state, generation-fenced handoff and compact progress only. Pass
        the copied handoff generation to refuse a replaced run; otherwise discover it.
        Reports fixed diagnostics for refused access, missing run, stale context or
        unavailable API. Does not resume, submit, probe a model or certify agent liveness.
        Progress must include consistent health, obligations and pending counts;
        malformed evidence cannot produce ok=true. Valid incomplete-source diagnostics
        remain readable with evidence_complete=false, not permission to act.
        """
        return api.connection_check(run_id, expected_generation)

    @mcp.tool()
    def phases(query: str = "") -> list[dict]:
        """Find standard/external decision phases by name, entity or purpose.
        Each phase names the built-in owner and durable read/write surfaces.
        write_access marks owner-only setup actions. A command:TYPE write goes through the
        generation-fenced /api/runs/{run_id}/commands endpoint."""
        return phase_catalog(query)

    @mcp.tool()
    def phase_info(phase_id: str) -> dict:
        """Read a phase's entity, evidence, output actions and command field contracts.
        Local metadata uses the UI-free protocol shared with server validation;
        the remote client needs only [harness], not a local FastAPI server. Use
        matching client/server LoopLab versions and live operation_schema too."""
        phase = phase_detail(phase_id)
        if phase is None:
            return {"error": "unknown phase", "phase_id": phase_id}
        from looplab.serve.protocol import (CONTROL_DATA_FIELDS,
                                           CONTROL_SERVER_DERIVED_FIELDS)
        phase["commands"] = {
            name: {"request_fields": sorted(CONTROL_DATA_FIELDS[name]),
                   "server_derived": sorted(CONTROL_SERVER_DERIVED_FIELDS.get(name, ())) }
            for ref in phase["writes"] if ref.startswith("command:")
            for name in (ref.removeprefix("command:"),)
        }
        if phase_id in ("research", "proposal", "novelty", "implementation"):
            from looplab.core.models import Idea, ResearchMemo
            phase["entity_schema"] = (ResearchMemo if phase_id == "research"
                                      else Idea).model_json_schema()
        return phase

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def upstream_request(run_id: str, expected_generation: str, proposal_id: str,
                         expected_request_hash: str, offset: int = 0, limit: int = 2048,
                         expected_content_hash: str | None = None) -> dict:
        """Read original proposal bytes, one diagnostic base64 page. Follow next_offset explicitly with content_sha256; assemble and verify all bytes before exact recovery. No work or retry starts."""
        return api.upstream_request(run_id, expected_generation, proposal_id, expected_request_hash,
                                    offset, limit, expected_content_hash)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def upstream_status(run_id: str, expected_generation: str, offset: int = 0, limit: int = 40,
                        source_node_id: int | None = None, candidate_offset: int = 0, candidate_limit: int = 200) -> dict:
        """Read verified base and independently paged nominations/history. Filter source_node_id or follow candidates.next_offset; use small candidate_limit for narrow replies. No work starts."""
        return api.upstream_status(run_id, expected_generation, offset, limit,
                                   source_node_id, candidate_offset, candidate_limit)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
    def upstream_propose(run_id: str, body: dict) -> dict:
        """Submit Maintainer's generalized patch plus separate recipe, documented old-default flag and critic. Preserve exact body/action_id. Requires stopped engine."""
        return api.upstream_write(run_id, "proposals", body)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    def upstream_check(run_id: str, body: dict) -> dict:
        """Buy explicit full-source equivalence and operator regression/repair-trigger checks. A lost response never implies a failed check; read history before exact retry."""
        return api.upstream_write(run_id, "check", body)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
    def upstream_advance(run_id: str, body: dict) -> dict:
        """Explicit stopped-engine CAS with current passing evidence_token and expected_base_revision. Only future lifecycles change. Resume separately."""
        return api.upstream_write(run_id, "advance", body)

    @mcp.tool()
    def settings_keys(query: str = "") -> dict:
        """Search all LoopLab Settings names, including advanced settings absent from the UI form."""
        from looplab.core.config import Settings
        names = sorted(name for name in Settings.model_fields
                       if query.casefold() in name.casefold())
        return {"matches": names[:100], "total": len(names)}

    @mcp.tool()
    def setting_info(name: str) -> dict:
        """Read one setting's actual JSON Schema, default and curated operator help."""
        manifest = harness_manifest(include_settings=True)
        schema = manifest["settings_schema"]
        field = schema.get("properties", {}).get(name)
        if field is None:
            return {"error": "unknown setting", "name": name}
        definitions = schema.get("$defs", {})
        needed = {}

        def collect(value):
            if isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                ref = value.get("$ref", "")
                prefix = "#/$defs/"
                if isinstance(ref, str) and ref.startswith(prefix):
                    key = ref[len(prefix):]
                    if key in definitions and key not in needed:
                        needed[key] = definitions[key]
                        collect(needed[key])
                for item in value.values():
                    collect(item)

        collect(field)
        curated = next((row for group in manifest["settings_help"].get("groups", [])
                        for row in group.get("fields", []) if row.get("key") == name), None)
        return {"name": name, "schema": field, "definitions": needed,
                "operator_help": curated}

    @mcp.tool()
    def operations(query: str = "", limit: int = 50) -> dict:
        """Search the live LoopLab OpenAPI catalog for reads, settings and controls.
        One authenticated GET, no automatic retry/cache fallback. Check code/outcome:
        transport, HTTP or malformed catalog failures return unavailable evidence,
        not an empty matches list. Repeat discovery explicitly after recovery."""
        return api.operations(query, limit)

    @mcp.tool()
    def operation_schema(path: str) -> dict:
        """Get a route's input/output schema before calling it.
        One authenticated live catalog GET, no automatic retry/cache fallback.
        Check code/outcome before using operations/components: unavailable discovery
        is not evidence that a route is absent. Repeat explicitly after recovery."""
        return api.schema(path)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                        idempotentHint=True, openWorldHint=False))
    def run_progress(run_id: str, expected_generation: str, language: Literal["en", "ru"] = "en") -> dict:
        """Read the compact next step, continue/finish gates and source health.
        execution separates recorded node activity from the last-read engine lock
        probe; agent connection is unmeasured. Refresh even without new events.
        language=en|ru selects next-step wording only, with the same obligations
        and read/action references. Older servers may omit the language stamp.
        Use the current generation from /state. Read detail references and phase_info
        before deciding; refresh after events or answers. This performs one GET only,
        returns HTTP failures unchanged, and never retries or submits a candidate.
        Check code/outcome before body: even HTTP 200 can carry unavailable evidence
        when the reply is over cap, not a JSON object or lacks matching generation.
        A different generation returns response_context_mismatch without its body.
        Missing/inconsistent critical health, gates, lifecycle or pending counts return
        invalid_progress/unavailable without body. Valid complete=false diagnostics
        are retained; empty gates must be explicit, and advice grants no permission."""
        return api.run_progress(run_id, expected_generation, language)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                        idempotentHint=True, openWorldHint=False))
    def result_notices(run_id: str, expected_generation: str,
                       limit: int = 50, cursor: str | None = None) -> dict:
        """Read current terminal node/run evidence and existing chat interpretations.

        One generation-fenced GET; no automatic paging, retry, submit or resume.
        Pass next_cursor unchanged for older receipts; refresh the latest page after
        draining or changed cursor evidence. Limit 1..200, default 50; reduce it if
        the response exceeds the byte cap. Check status/code/outcome before body:
        malformed, incomplete-page or mismatched-generation HTTP 200 is unavailable,
        not zero results. The version-1 envelope, paging and terminal receipt
        identities/numeric fields are checked; extra fields are retained.
        POST missing interpretations via api_request using current receipt_id and
        evidence_token, a stable action_id and exact body for lost-response retries.
        Commentary never replaces checkpoints/reports or adds an engine wait.
        """
        return api.result_notices(run_id, expected_generation, limit, cursor)

    @mcp.tool()
    def api_request(method: str, path: str, body: dict | None = None,
                    idempotency_key: str = "") -> dict:
        """Call a live /api route using UI authorization and validation. Commands require
        a unique Idempotency-Key and the run's expected_generation in the JSON body;
        reuse the same key only for an exact lost-response retry. HTTP errors are returned
        with their status and body so the agent can handle stale state explicitly.
        Transport loss returns status=null: a write has outcome=unknown, not failed.
        A 5xx write response or incomplete 2xx acknowledgement also has outcome=unknown;
        the received HTTP status is preserved. Do not treat HTTP 200 alone as a receipt.
        Inspect saved receipts before an exact retry; this tool never retries itself."""
        return api.request(method, path, body, idempotency_key)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                        idempotentHint=True, openWorldHint=False))
    def command_receipt(run_id: str, expected_generation: str,
                        command_id: str = "", idempotency_key: str = "") -> dict:
        """Observe a saved command after disconnect or a lost POST response.
        Supply exactly one known command_id or the ORIGINAL Idempotency-Key.
        This performs one generation-fenced GET without reconciliation, worker
        restart, retry or resume. A missing/stale receipt does not prove no action
        occurred; read state, events and checkpoints before choosing recovery.
        Check code/outcome before body: a malformed/over-cap HTTP 200 reply is
        unavailable evidence, never proof that a receipt is absent.
        The returned generation and command ID must match the request, including
        the original key's durable ID; response_context_mismatch omits that body.
        Version-1 status/terminal, control event, sequence and error fields must
        be complete and consistent; invalid_command_receipt omits the body.
        Terminal includes rejected/failed, and succeeded inject proves admission,
        not completed training. Extra fields remain compatible.
        """
        return api.command_receipt(run_id, expected_generation, command_id, idempotency_key)

    return mcp


def run_stdio(url: str | None = None, token: str | None = None) -> None:
    credential = token if token is not None else os.environ.get("LOOPLAB_HARNESS_TOKEN", "")
    if not credential or not credential.strip() or credential == "${LOOPLAB_HARNESS_TOKEN}":
        raise ValueError("Set LOOPLAB_HARNESS_TOKEN in the MCP process environment. "
                         "LOOPLAB_UI_TOKEN is never used by harness-mcp; ask the operator "
                         "for a distinct scoped credential.")
    if any(ord(char) < 32 or ord(char) > 126 for char in credential):
        raise ValueError("LOOPLAB_HARNESS_TOKEN must be printable ASCII without control characters.")
    if credential == os.environ.get("LOOPLAB_UI_TOKEN", ""):
        raise ValueError("The harness credential must differ from LOOPLAB_UI_TOKEN.")
    api = HarnessAPI(url or os.environ.get("LOOPLAB_HARNESS_URL", "http://127.0.0.1:8765"),
                     credential)
    try:
        build_server(api).run(transport="stdio")
    finally:
        api.client.close()
