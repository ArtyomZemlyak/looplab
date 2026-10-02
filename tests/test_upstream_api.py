"""Production HTTP and typed MCP access share the measured upstream transaction."""
import json

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from tests.test_upstream_lane import fixture, GENERAL


def test_http_mcp_full_sgd_transaction_and_receipt_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    lane, store, generation, proposal = fixture(tmp_path)
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            # Starlette uses httpx2 here; bridge wire bytes into the harness's httpx.
            return httpx.Response(response.status_code, json=response.json())
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        status = api.upstream_status("run", generation)
        assert status["status"] == 200 and not status.get("code"), status
        made = api.upstream_write("run", "proposals", proposal)
        assert made["body"]["status"] == "succeeded" and not made.get("code"), made
        assert api.upstream_write("run", "proposals", proposal)["body"] == made["body"]
        check = {"expected_generation": generation, "action_id": "api-check", "proposal_id": made["body"]["proposal_id"]}
        checked = api.upstream_write("run", "check", check)
        assert checked["body"]["status"] == "succeeded" and not checked.get("code"), checked
        advance = {"expected_generation": generation, "action_id": "api-advance", "proposal_id": check["proposal_id"],
            "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["body"]["evidence_token"]}
        advanced = api.upstream_write("run", "advance", advance)
        assert advanced["body"]["status"] == "succeeded", advanced
        assert api.upstream_write("run", "advance", advance)["body"] == advanced["body"]
        page = api.upstream_status("run", generation, limit=2)
        assert page["body"]["next_offset"] == 2
        assert api.upstream_status("run", generation, offset=2, limit=2)["status"] == 200
        state = client.get("/api/runs/run/state").json()["state"]
        assert state["upstream_base"]["selector"]["digest"] == made["body"]["selector"]["digest"]
        assert state["nodes"]["0"]["metric_provenance"]["base_revision"]["digest"] != state["upstream_base"]["selector"]["digest"]
        # Recovery route reaches an actual refusal; it does not approve/resume anything.
        response = client.post("/api/runs/run/upstream/recover", json={"expected_generation": generation,
            "action_id": "recovery", "claim_action_id": "unknown", "reason": "Inspect a missing interrupted claim"})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "upstream_claim_missing"
        api.client.close()


@pytest.mark.parametrize("body", [{}, {"version": 1, "enabled": True, "generation": "a" * 64}])
def test_incomplete_mcp_read_is_unavailable_not_empty_obligations(body):
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    result = api.upstream_status("run", "a" * 64)
    assert result["outcome"] == "unavailable" and "body" not in result
    api.client.close()


def test_malformed_successful_write_never_certifies_or_reexecutes():
    calls = []
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json={"status": "succeeded"}))[1]))
    result = api.upstream_write("run", "check", {"expected_generation": "a" * 64, "action_id": "claim", "proposal_id": "up_" + "b" * 24})
    assert result["outcome"] == "unknown" and len(calls) == 1
    api.client.close()
