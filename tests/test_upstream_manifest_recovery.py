"""A damaged retained proposal names its source without buying another check."""
import json

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.perm_modes import APPROVAL_ALLOW_ONCE
from looplab.tools.upstream_tools import UpstreamTools
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("damage", ["truncated", "encoding", "surrogate", "nonfinite", "deep", "missing", "oversized", "changed"])
def test_manifest_refusal_names_source_and_keeps_original_receipts(tmp_path, monkeypatch, damage):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    check = {"expected_generation": generation, "action_id": "measured-check", "proposal_id": made["proposal_id"]}
    checked = lane.check(check)
    assert checked["status"] == "succeeded"
    advance = {"expected_generation": generation, "action_id": "blocked-advance", "proposal_id": made["proposal_id"],
        "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["evidence_token"]}
    relative = "upstream/proposals/" + made["proposal_id"] + "/manifest.json"
    path = lane.rd / relative
    original = path.read_bytes()
    damaged = (b'{"private-secret":' if damage == "truncated" else
        b'"\xffprivate-secret"' if damage == "encoding" else
        b'"\\ud800private-secret"' if damage == "surrogate" else
        b'{"private-secret":NaN}' if damage == "nonfinite" else
        b'[' * 1200 + b'"private-secret"' + b']' * 1200 if damage == "deep" else
        b' ' * (2 * 1024 * 1024 + 1) if damage == "oversized" else
        json.dumps({**proposal, "summary": "private-secret changed proposal"}).encode())
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(damaged)
    before = store.path.read_bytes()
    code = "upstream_manifest_changed" if damage == "changed" else "upstream_manifest_unavailable"
    status = 409 if damage == "changed" else 503
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        assistant = UpstreamTools(tmp_path, mode="auto", approver=lambda action: APPROVAL_ALLOW_ONCE)
        try:
            for operation, body in (("check", {**check, "action_id": "blocked-check"}), ("advance", advance)):
                with pytest.raises(UpstreamRefusal) as refused:
                    getattr(lane, operation)(body)
                assert refused.value.code == code
                assert relative in str(refused.value)
                assert "private-secret" not in str(refused.value)
                reply = api.upstream_write("run", operation, body)
                assert reply["status"] == status
                assert reply["body"]["detail"]["code"] == code
                assert relative in reply["body"]["detail"]["message"]
                result = assistant.execute("upstream_" + operation, {"run_id": "run", "body": body})
                assert result.is_error and relative in result.content
                assert "private-secret" not in str(reply) + result.content
            # Exact ACKs are history reads, independent of damaged live sidecars.
            assert api.upstream_write("run", "proposals", proposal)["body"] == made
            assert api.upstream_write("run", "check", check)["body"] == checked
            assert store.path.read_bytes() == before
            assert len([e for e in store.read_all() if e.type == "upstream_execution"]) == 7
            assert not any(e.type in ("base_advanced", "resume") for e in store.read_all())
            # Restoring the original bytes grants no automatic action. A fresh
            # explicitly submitted CAS may use the original current gate.
            path.write_bytes(original)
            assert store.path.read_bytes() == before
            accepted = api.upstream_write("run", "advance", advance)
            assert accepted["status"] == 200 and accepted["body"]["status"] == "succeeded", accepted
            assert len([e for e in store.read_all() if e.type == "upstream_execution"]) == 7
            assert not any(e.type == "resume" for e in store.read_all())
        finally:
            api.client.close()
