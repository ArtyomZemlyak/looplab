"""Abandoned-claim recovery stays explicit through HTTP, MCP and Assistant."""
import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.config import Settings

from looplab.engine import upstream
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.upstream_tools import UpstreamTools
from looplab.tools.perm_modes import APPROVAL_ALLOW_ONCE
from tests.test_upstream_lane import fixture


def test_abandoned_check_recovery_reaches_each_client_without_new_work(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "lost-check",
        "proposal_id": made["proposal_id"]}
    def die(*args, **kwargs):
        raise SystemExit("Private process loss after durable claim")
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "execute_gate", die)
        with pytest.raises(SystemExit):
            lane.check(request)
    recovery = {"expected_generation": generation, "action_id": "owner-recovery",
        "claim_action_id": request["action_id"], "reason": "The private process exited"}
    with TestClient(make_app(tmp_path)) as client:
        response = client.post("/api/runs/run/upstream/recover", json=recovery)
        assert response.status_code == 200
        recovered = response.json()
        before = store.path.read_bytes()
        def bridge(r):
            reply = client.request(r.method, str(r.url), content=r.content, headers=r.headers)
            return httpx.Response(reply.status_code, content=reply.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            with monkeypatch.context() as patch:
                patch.setattr(upstream, "engine_alive", lambda rd: True)
                ack = client.post("/api/runs/run/upstream/recover", json=recovery)
                assert ack.status_code == 200 and ack.json() == recovered
                refused = api.upstream_write("run", "check", request)
                assert refused["status"] == 409
                assert refused["body"]["detail"]["code"] == "upstream_claim_abandoned"
                assert "new action_id" in refused["body"]["detail"]["message"]
                assistant = UpstreamTools(tmp_path, mode="auto", approver=lambda action: APPROVAL_ALLOW_ONCE)
                reply = assistant.execute("upstream_check", {"run_id": "run", "body": request})
                assert reply.is_error and "already abandoned" in reply.content
                # The stopped lane's refusal is `upstream_mode: off` (doc 72); the live modes queue
                # the action for the running engine instead (`tests/test_upstream_live_lane.py`).
                snapshot = tmp_path / "run" / "config.snapshot.json"
                live = snapshot.read_bytes()
                snapshot.write_text(Settings.model_validate_json(live).model_copy(
                    update={"upstream_mode": "off"}).model_dump_json(), encoding="utf8")
                fresh = api.upstream_write("run", "check", {**request, "action_id": "fresh-check"})
                assert fresh["status"] == 409
                assert fresh["body"]["detail"]["code"] == "upstream_engine_running"
                snapshot.write_bytes(live)
            assert store.path.read_bytes() == before
            assert not any(e.type in ("upstream_execution", "base_advanced", "resume") for e in store.read_all())
            checked = api.upstream_write("run", "check", {**request, "action_id": "fresh-check"})
            assert checked["status"] == 200 and checked["body"]["status"] == "succeeded", checked
            assert len(checked["body"]["result"]["executions"]) == 7
            assert not any(e.type in ("base_advanced", "resume") for e in store.read_all())
        finally:
            api.client.close()
