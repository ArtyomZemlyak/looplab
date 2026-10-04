"""Assistant access to the same explicit upstream transaction external MCP uses."""
import json
from pathlib import Path

from looplab.core.errors import ConfigRefusal
from looplab.tools._base import RESULT_CAP, ToolCapability, ToolResult, fn_spec

# Base64 expands bytes by 4/3; reserve identity/hash/pagination metadata inside
# the shared model result cap, rather than returning an amputated exact request.
REQUEST_PAGE_LIMIT = min(4096, max(1, (RESULT_CAP - 1800) * 3 // 4))


class UpstreamTools:
    def __init__(self, run_root, *, mode="plan", approver=None):
        self.run_root, self.mode, self.approver = Path(run_root).resolve(), mode, approver

    def specs(self):
        rows = [fn_spec("upstream_status", "Read active base, independently paged nominations/history and Maintainer instructions. Filter source_node_id or follow candidates.next_offset using candidate_offset; narrow candidate_limit and history limit for small replies. Starts no work.",
            {"run_id": {"type": "string"}, "expected_generation": {"type": "string"},
             "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
             "source_node_id": {"type": "integer", "minimum": 0},
             "candidate_offset": {"type": "integer", "minimum": 0},
             "candidate_limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["run_id", "expected_generation"])]
        rows.append(fn_spec("upstream_request", "Read original proposal bytes as one bounded diagnostic base64 page. Follow next_offset explicitly with expected_content_hash=content_sha256; verify all bytes and request hash before exact recovery. Starts no work.",
            {"run_id": {"type": "string"}, "expected_generation": {"type": "string"},
             "proposal_id": {"type": "string"}, "expected_request_hash": {"type": "string"},
             "offset": {"type": "integer", "minimum": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": REQUEST_PAGE_LIMIT},
             "expected_content_hash": {"type": "string"}},
            ["run_id", "expected_generation", "proposal_id", "expected_request_hash"]))
        if self.mode != "plan":
            for operation, purpose in (
                ("propose", "Generalize measured source_node_id and hunk_hashes in a run-owned worktree using Maintainer. Supply files/deleted, separate recipe_files/recipe_deleted, summary, documented flag {name,default,enabled}, documentation_path, named critic {verdict:pass,reason,reviewer}, expected_base_revision."),
                ("check", "Buy real full-source repetitions and operator regression/trigger probes for proposal_id. Stopped engine required. Read measured history before the next decision."),
                ("advance", "Explicit CAS for future experiments using proposal_id, expected_base_revision and measured evidence_token. Pause, wait for engine exit, then advance; resume separately.")):
                rows.append(fn_spec("upstream_" + operation, purpose + " Exact body must include expected_generation and stable action_id; retain it for lost replies. No automatic retry/execution.",
                    {"run_id": {"type": "string"}, "body": {"type": "object"}}, ["run_id", "body"]))
        return rows

    def capabilities(self):
        return [ToolCapability(name=(name := row["function"]["name"]), input_schema=row["function"]["parameters"],
            effect="read" if name in ("upstream_status", "upstream_request") else "execute" if name == "upstream_check" else "control",
            risk="low" if name in ("upstream_status", "upstream_request") else "high", idempotency="conditional",
            approval="never" if name in ("upstream_status", "upstream_request") else "policy", concurrency_safe=True) for row in self.specs()]

    def execute(self, name, args):
        from looplab.adapters.tasks import load_task
        from looplab.core.config import read_config_snapshot
        from looplab.core.pathsafe import is_reparse
        from looplab.engine.upstream import UpstreamLane
        from looplab.events.eventstore import EventStoreConcurrencyError, EventStoreLockError
        from looplab.tools.perm_modes import authorize
        rid = str(args.get("run_id", ""))
        if not rid or rid.startswith(".") or "/" in rid or "\\" in rid:
            return ToolResult("Invalid direct-child run_id", is_error=True)
        rd = self.run_root / rid
        try:
            if is_reparse(rd.lstat()) or rd.resolve().parent != self.run_root:
                return ToolResult("Run aliases are unavailable", is_error=True)
            task = load_task(rd / "task.snapshot.json", existing_run=True)
            lane = UpstreamLane(rd, task, read_config_snapshot(rd / "config.snapshot.json", refuse_unknown=True))
            if name == "upstream_status":
                result = lane.read(args["expected_generation"], offset=args.get("offset", 0), limit=args.get("limit", 10),
                    source_node_id=args.get("source_node_id"), candidate_offset=args.get("candidate_offset", 0),
                    candidate_limit=args.get("candidate_limit", 200))
                from looplab.agents.maintainer import Maintainer
                result["maintainer_instruction"] = Maintainer.instruction
            elif name == "upstream_request":
                limit = args.get("limit", min(1024, REQUEST_PAGE_LIMIT))
                if type(limit) is not int or not 1 <= limit <= REQUEST_PAGE_LIMIT:
                    return ToolResult(f"Use request byte limit 1..{REQUEST_PAGE_LIMIT}; larger pages would exceed the model result cap. No read or work started", is_error=True)
                result = lane.request(args["expected_generation"], args["proposal_id"], args["expected_request_hash"],
                    offset=args.get("offset", 0), limit=limit,
                    expected_content_hash=args.get("expected_content_hash"))
            elif name in {row["function"]["name"] for row in self.specs()}:
                body = args.get("body")
                if not isinstance(body, dict):
                    return ToolResult("Supply the exact request body", is_error=True)
                from looplab.engine.upstream_spec import normalize_request
                from looplab.engine.upstream_state import digest
                body = normalize_request(name.removeprefix("upstream_"), body)
                blocked = authorize(self.mode, self.approver, {"tool": name, "tool_kind": "run_control",
                    "verb": name, "label": f"{name} {rid}", "run_id": rid,
                    "preview": json.dumps(body, ensure_ascii=False)[:2000], "scope": {"run_id": rid, "request_digest": digest(body), "body": body}},
                    denied="Upstream writes disabled in plan mode", declined="Upstream action declined")
                if blocked:
                    return ToolResult(blocked, is_error=True)
                result = getattr(lane, name.removeprefix("upstream_"))(body)
            else:
                return ToolResult("Unknown upstream tool", is_error=True)
            text = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if name == "upstream_request" and len(text) > RESULT_CAP:
                return ToolResult("Request page metadata exceeds the model result cap. Explicitly request a smaller byte limit; no execution or retry occurred", is_error=True)
            if len(text) > 12000:
                text = text[:11500] + "\n[Bounded view; read next upstream history page through API.]"
            return ToolResult(text, structured=result, receipt={k: result[k] for k in ("action_id", "seq", "status", "proposal_id", "evidence_token") if k in result}, is_error=result.get("status") == "failed")
        except (ConfigRefusal, OSError, ValueError, KeyError, TypeError, AttributeError, EventStoreConcurrencyError, EventStoreLockError) as exc:
            return ToolResult(str(exc) if isinstance(exc, ConfigRefusal) else "Upstream source/request unavailable; inspect task/config/events before recovery", is_error=True)
