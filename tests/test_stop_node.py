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


class ReconnectingAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.attempt = 1
        self.saved = {}
        self.lookups = []
        self.lookup_failure = None
        self.node_body = None
        self.node_reads = 0

    def command_receipt(self, run_id, expected_generation, *, idempotency_key):
        self.lookups.append(idempotency_key)
        if self.lookup_failure:
            return self.lookup_failure
        if idempotency_key not in self.saved:
            return {"status": 404, "code": "http_error"}
        return {"status": 200, "body": {"command": self.saved[idempotency_key]}}

    def request(self, method, path, body=None, idempotency_key=None, **kwargs):
        if method == "GET" and "/nodes/" in path:
            self.node_reads += 1
            return {"status": 200, "body": self.node_body or {
                "attempt": self.attempt, "status": "evaluating"}}
        if method == "POST" and path.endswith("/commands"):
            self.commands.append(body)
            record = {"id": "cmd_" + "a" * 32, "status": "succeeded",
                      "event_type": "node_abort", "event_seq": 7, "error": None}
            self.saved[idempotency_key] = record
            return {"status": 200, "body": record}
        return super().request(method, path, body, **kwargs)


def test_lost_stop_response_does_not_abort_a_new_attempt():
    api = ReconnectingAPI()
    first = composite.stop_node(api, "run", "generation", node_id=61,
                                key="original-stop", reason="stop active evaluator")
    assert first["ok"]
    reads = api.node_reads
    api.attempt = 2  # another operator has since retried the stopped node
    repeated = composite.stop_node(api, "run", "generation", node_id=61,
                                   key="original-stop", reason="stop active evaluator")
    assert repeated["ok"]
    assert len(api.commands) == 1, "a reconnect must replay the first abort, not stop attempt 2"
    assert api.node_reads == reads, "resolve the saved command before reading a newer attempt"


def test_unavailable_stop_receipt_does_not_authorize_a_fresh_abort():
    api = ReconnectingAPI()
    api.lookup_failure = {"status": 503, "code": "receipt_unavailable"}
    result = composite.stop_node(api, "run", "generation", node_id=61,
                                 key="original-stop", reason="stop active evaluator")
    assert not result["ok"]
    assert not api.commands and api.node_reads == 0


def test_missing_attempt_is_not_silently_interpreted_as_zero():
    api = ReconnectingAPI()
    api.node_body = {"status": "evaluating"}
    result = composite.stop_node(api, "run", "generation", node_id=61,
                                 key="original-stop", reason="stop active evaluator")
    assert not result["ok"]
    assert not api.commands
