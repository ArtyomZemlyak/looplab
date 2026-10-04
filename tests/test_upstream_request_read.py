"""Lost-agent recovery reads exact bytes through HTTP/MCP without buying work."""
import base64
import copy
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream
from looplab.engine.upstream_state import digest
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.upstream_tools import REQUEST_PAGE_LIMIT, UpstreamTools
from tests.test_upstream_lane import fixture


def test_recover_large_original_via_paged_http_mcp_and_plan_assistant(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, body = fixture(tmp_path)
    body["files"]["README.md"] += "\n" + "Контекст 🚀\n" * 22000
    def die(*args, **kwargs):
        raise SystemExit("writer exited before Git")
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "maintainer_worktree", die)
        with pytest.raises(SystemExit):
            lane.propose(body)
    claim = store.read_all()[-1]
    proposal, request_hash = claim.data["proposal_id"], claim.data["request_hash"]
    path = lane.rd / claim.data["request_path"]
    raw, before = path.read_bytes(), store.path.read_bytes()
    assert len(raw) > 256 * 1024
    with TestClient(make_app(tmp_path)) as client:
        calls = []
        def bridge(request):
            calls.append(request)
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            data, offset, sha = bytearray(), 0, None
            while True:
                result = api.upstream_request("run", generation, proposal, request_hash, offset, 4096, sha)
                assert result["status"] == 200 and not result.get("code"), result
                page = result["body"]
                assert page["claim_status"] == "unresolved" and page["authority"] == "diagnostic_only"
                assert len(json.dumps(result)) < 12000
                data.extend(base64.b64decode(page["chunk"]))
                sha = page["content_sha256"]
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            assert bytes(data) == raw and hashlib.sha256(data).hexdigest() == sha
            assert digest(json.loads(data)) == request_hash
            assert len(calls) == (len(raw) + 4095) // 4096
            valid = lane.request(generation, proposal, request_hash)
            corruptions = [{}, {"version": True}, {"claim_seq": None}, {"authority": "permit"},
                {"next_offset": None}, {"total_bytes": True}, {"chunk": "!"}, {"chunk_sha256": "f" * 64},
                {"request_path": "../../secret"}, {"source_health": {"events": "complete"}},
                {"request_generation": None}, {"offset": True}, {"settlement_seq": 2},
                {"content_sha256": ""}, {"action_id": "other"}]
            for changes in corruptions:
                broken = {} if not changes else {**copy.deepcopy(valid), **changes}
                seen = []
                def malformed(request):
                    seen.append(request)
                    return httpx.Response(200, json=broken)
                with httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(malformed)) as transport:
                    original = api.client
                    api.client = transport
                    try:
                        refused = api.upstream_request("run", generation, proposal, request_hash)
                        assert refused["outcome"] == "unavailable" and "body" not in refused
                        assert len(seen) == 1
                    finally:
                        api.client = original
        finally:
            api.client.close()
    assistant = UpstreamTools(tmp_path, mode="plan")
    args = {"run_id": "run", "expected_generation": generation,
        "proposal_id": proposal, "expected_request_hash": request_hash}
    oversized = assistant.execute("upstream_request", {**args, "limit": 4096})
    assert oversized.is_error and oversized.structured is None
    reply = assistant.execute("upstream_request", {"run_id": "run", "expected_generation": generation,
        "proposal_id": proposal, "expected_request_hash": request_hash, "limit": REQUEST_PAGE_LIMIT})
    from looplab.agents.tool_loop import _cap_tool_result
    assert not reply.is_error and _cap_tool_result(reply.content) == reply.content
    assert reply.structured["claim_status"] == "unresolved"
    assert next(c for c in assistant.capabilities() if c.name == "upstream_request").approval == "never"
    with pytest.raises(UpstreamRefusal):
        lane.propose(json.loads(data))
    assert store.path.read_bytes() == before and path.read_bytes() == raw
    with pytest.raises(ValueError):
        lane.request(generation, proposal, request_hash, offset=1)
    path.write_bytes(raw + b" ")
    with pytest.raises(UpstreamRefusal, match="between pages"):
        lane.request(generation, proposal, request_hash, offset=1, expected_content_hash=sha)
    path.write_text('{"private-secret":', encoding="utf8")
    with pytest.raises(UpstreamRefusal) as error:
        lane.request(generation, proposal, request_hash)
    assert error.value.code == "upstream_request_unavailable" and "private-secret" not in str(error.value)
    assert store.path.read_bytes() == before


def test_orphan_and_abandoned_requests_grant_no_authority_and_reset_refuses(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    from looplab.engine.upstream_spec import normalize_request
    body = normalize_request("propose", body)
    proposal, request_hash = "up_" + digest(body["action_id"])[:24], digest(body)
    with pytest.raises(UpstreamRefusal) as missing:
        lane.request(generation, proposal, request_hash)
    assert missing.value.code == "upstream_request_unavailable"
    assert f"upstream/requests/{proposal}/request.json" in str(missing.value)
    lane._retain_proposal_request(body, proposal)
    assert lane.request(generation, proposal, request_hash)["claim_status"] == "unclaimed"
    def die(*args, **kwargs):
        raise SystemExit()
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "maintainer_worktree", die)
        with pytest.raises(SystemExit):
            lane.propose(body)
    lane.abandon({"expected_generation": generation, "action_id": "abandon-reader",
        "claim_action_id": body["action_id"], "reason": "Writer exited"})
    assert lane.request(generation, proposal, request_hash)["claim_status"] == "abandoned"
    with pytest.raises(UpstreamRefusal) as error:
        lane.request("f" * 64, proposal, request_hash)
    assert error.value.code == "run_generation_conflict"
    with pytest.raises(UpstreamRefusal) as error:
        lane.request(generation, proposal, "f" * 64)
    assert error.value.code == "upstream_request_changed"
    store.path.write_bytes(store.path.read_bytes() + b'{"broken":')
    with pytest.raises(UpstreamRefusal) as error:
        lane.request(generation, proposal, request_hash)
    assert error.value.code == "upstream_source_unavailable"


def test_completed_request_scoped_read_and_generation_reset_during_read(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    made = lane.propose(body)
    proposal, request_hash = made["proposal_id"], made["request_hash"]
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-request-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-request-secret")
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        route = f"/api/runs/run/upstream/requests/{proposal}"
        args = {"expected_generation": generation, "expected_request_hash": request_hash}
        assert client.get(route, params=args).status_code == 401
        result = client.get(route, params=args, headers={"X-LoopLab-Token": "agent-request-secret"})
        assert result.status_code == 200 and result.json()["claim_status"] == "completed"
        assert result.headers["cache-control"] == "no-store"
        invalid = client.get(route, params={**args, "offset": 1}, headers={"X-LoopLab-Token": "agent-request-secret"})
        assert invalid.status_code == 409 and invalid.json()["detail"]["code"] == "upstream_request_invalid"
        denied = client.post("/api/runs/run/upstream/recover", headers={"X-LoopLab-Token": "agent-request-secret"},
            json={"expected_generation": generation, "action_id": "forbidden-recovery",
                  "claim_action_id": body["action_id"], "reason": "Agent cannot abandon"})
        assert denied.status_code == 403
    assert store.path.read_bytes() == before
    from looplab.harness.upstream_requests import valid_page
    page = lane.request(generation, proposal, request_hash, limit=4096)
    assert page["next_offset"] is None
    assert valid_page(page, generation, proposal, request_hash, 0, 4096, None)
    for field in ("content_sha256", "request_generation"):
        assert not valid_page({**page, field: "f" * 64}, generation, proposal, request_hash, 0, 4096, None)
    from looplab.engine import upstream_requests
    real_read = upstream_requests.read_bounded_regular_file
    def reset_after_file(*args, **kwargs):
        raw = real_read(*args, **kwargs)
        records = before.splitlines()
        first = json.loads(records[0])
        first["ts"] += 1
        records[0] = json.dumps(first).encode()
        store.path.write_bytes(b"\n".join(records) + b"\n")
        return raw
    monkeypatch.setattr(upstream_requests, "read_bounded_regular_file", reset_after_file)
    with pytest.raises(UpstreamRefusal) as error:
        lane.request(generation, proposal, request_hash)
    assert error.value.code == "run_generation_conflict"
