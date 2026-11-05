from __future__ import annotations

import json

import httpx

from looplab.harness.mcp_server import HarnessAPI


def _transport(request):
    path = request.url.raw_path.decode()
    if path.startswith("/api/agent-events?"):
        events = [
            {"id": 1, "kind": "node_evaluated", "run_id": "demo", "summary": "old result"},
            {"id": 2, "kind": "run_paused", "run_id": "demo", "summary": "old pause"},
            {"id": 3, "kind": "operator_direction", "run_id": "demo", "summary": "new direction"},
        ]
        return httpx.Response(200, content=json.dumps({
            "events": events, "next_after": 3, "timed_out": False}).encode())
    if path == "/api/runs/demo/state?observe_only=true":
        return httpx.Response(200, content=json.dumps({"generation": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}).encode())
    if path == "/api/runs/demo/harness-progress?expected_generation=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&brief=true":
        return httpx.Response(200, content=json.dumps({
            "generation": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "run_uid": "uid-demo",
            "at_node": 0,
            "next_step": {"owner": "external_agent", "code": "continue", "title": "Continue",
                          "detail": "fresh direction", "reads": ["run_progress"],
                          "action": None, "phase_id": None},
            "recorded_lifecycle": {"paused": True, "finished": False, "stop_requested": False},
            "execution": {"engine_running": False, "agent_connection": "not_measured",
                          "recorded_node_counts": {"building": 0, "queued": 0, "evaluating": 0, "pending": 0}},
            "execution_scope": {"mode": "optimization_only", "version": 1},
            "complete": True,
            "source_health": {name: {"read_complete": True}
                              for name in ("events", "decisions", "reviews", "checkpoints")},
            "pending_checkpoint_count": 0,
            "pending_checkpoints": [],
            "pending_checkpoints_truncated": False,
            "finish_pending_node_count": 0,
            "finish_pending_nodes": [],
            "finish_pending_nodes_truncated": False,
            "finish_reviews_due": [],
            "finish_report_due": False,
            "candidate_blockers_if_expanding": [],
            "candidate_decisions_per_idea": {},
            "candidate_requirements": {"effective_concepts": False, "hypothesis_statement": False},
            "history": {},
            "event_seq": 3,
            "evidence_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "policy_preview": {},
            "canary_preflight": {},
        }).encode())
    raise AssertionError(path)


def test_backlog_is_one_fresh_packet_with_history_and_progress():
    api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(_transport))
    result = api.wait_for_next(0, 1, "demo")
    body = result["body"]
    assert [row["kind"] for row in body["events"]] == ["operator_direction"]
    assert body["next_after"] == 3 and body["coalesced_count"] == 2
    assert [row["id"] for row in body["coalesced_history"]] == [1, 2]
    assert body["fresh_progress"]["execution_scope"]["mode"] == "optimization_only"
    assert body["fresh_progress"]["execution"]["engine_running"] is False


def test_coalesce_false_preserves_chronological_events():
    api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(_transport))
    result = api.wait_for_next(0, 1, "demo", coalesce=False)
    assert [row["id"] for row in result["body"]["events"]] == [1, 2, 3]
    assert "coalesced_history" not in result["body"]
