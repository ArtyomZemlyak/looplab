from __future__ import annotations

import anyio
import httpx

from looplab.harness import composite
from looplab.harness.mcp_server import HarnessAPI, build_server


class FakeAPI:
    def __init__(self):
        self.commands = []

    def request(self, method, path, body=None, idempotency_key=None, **_kwargs):
        if method == "POST" and path.endswith("/commands"):
            self.commands.append(body)
            return {"status": 200, "body": {
                "status": "succeeded", "event_type": body["type"], "event_seq": 7}}
        if method == "GET" and "/nodes/" in path:
            return {"status": 200, "body": {"attempt": 1, "status": "aborted"}}
        if method == "GET" and path == "/api/gpu-queue":
            return {"status": 200, "body": {"lease": False, "reservations": []}}
        raise AssertionError((method, path, body))

    def run_progress(self, run_id, generation):
        return {"status": 200, "body": {"execution": {"engine_running": False}}}


def test_stop_node_returns_abort_and_separate_observations():
    api = FakeAPI()
    result = composite.stop_node(
        api, "run", "generation", node_id=61, generation=1,
        key="stop61", reason="stop active evaluator")
    assert result["ok"], result
    assert result["stop_kind"] == "active_evaluator_abort"
    assert result["node_status"] == "aborted"
    assert result["engine_running"] is False
    assert result["gpu_queue"]["lease"] is False
    assert result["checkpoint"]["status"] == "not_collected"
    assert "unsaved steps" in result["checkpoint"]["reason"]
    command = api.commands[0]
    assert command["type"] == "node_abort"
    assert command["data"] == {
        "node_id": 61, "generation": 1, "reason": "stop active evaluator"}
    assert command["expected_generation"] == "generation"


def test_stop_node_is_advertised_as_a_distinct_lifecycle_operation():
    api = HarnessAPI("http://testserver/", transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("no network expected"))))
    schemas = {tool.name: tool.model_dump(by_alias=True)
               for tool in anyio.run(build_server(api).list_tools)}
    assert "stop_node" in schemas
    assert "pause" not in schemas["stop_node"]["description"].lower().split("unlike")[0]
