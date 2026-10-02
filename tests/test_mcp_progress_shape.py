"""Missing gates/health cannot be presented as a usable external progress read."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from tests.test_harness_connection import GEN, progress, response


def _read(page, tool):
    seen = []
    def handler(request):
        seen.append(request)
        value = page if request.url.path.endswith("/harness-progress") else response(request.url.path)
        return httpx.Response(200, json=value)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = getattr(api, tool)("demo", GEN)
    assert len(seen) == (1 if tool == "run_progress" else 3)
    assert all(row.method == "GET" for row in seen)
    return result


@pytest.mark.parametrize("tool", ["run_progress", "connection_check"])
@pytest.mark.parametrize("field", ["run_uid", "at_node", "event_seq", "evidence_revision", "complete", "source_health",
    "next_step", "execution", "recorded_lifecycle", "candidate_blockers_if_expanding",
    "candidate_decisions_per_idea", "candidate_requirements", "finish_reviews_due",
    "finish_report_due", "finish_pending_nodes", "finish_pending_node_count",
    "finish_pending_nodes_truncated", "pending_checkpoint_count", "pending_checkpoints",
    "pending_checkpoints_truncated"])
def test_missing_critical_progress_field_is_unavailable(tool, field):
    page = progress()
    del page[field]
    result = _read(page, tool)
    assert result["status"] == 200 and "body" not in result
    if tool == "run_progress":
        assert result["code"] == "response_incomplete" and result["outcome"] == "unavailable"
        assert result["reason"] == "invalid_progress"
    else:
        assert result["ok"] is False and result["code"] == "invalid_response"


@pytest.mark.parametrize("changes", [{"source_health": {}}, {"next_step": {}},
    {"complete": 1}, {"complete": False}, {"event_seq": True}, {"event_seq": -1},
    {"evidence_revision": "unknown"}, {"candidate_blockers_if_expanding": [{}]},
    {"candidate_decisions_per_idea": [False]}, {"finish_reviews_due": "none"},
    {"finish_report_due": 0}, {"finish_pending_node_count": 1},
    {"pending_checkpoint_count": 1}, {"pending_checkpoints_truncated": True},
    {"candidate_requirements": {}}, {"recorded_lifecycle": {"paused": False}},
    {"execution": {"engine_running": 1}}, {"source_health": {"events": {"read_complete": True}}}])
def test_inconsistent_progress_is_not_an_empty_obligation_board(changes):
    result = _read({**progress(), **changes}, "run_progress")
    assert result["outcome"] == "unavailable" and result["reason"] == "invalid_progress"
    assert "body" not in result


@pytest.mark.parametrize("tool", ["run_progress", "connection_check"])
@pytest.mark.parametrize("complete", [True, False])
def test_actual_incomplete_sources_are_retained_with_diagnostics(tool, complete):
    page = progress()
    page["complete"] = complete
    page["source_health"]["reviews"].update(read_complete=complete, invalid_lines=0 if complete else 1)
    page["future_metadata"] = {"retained": True}
    if not complete:
        page["next_step"]["code"] = "inspect_sources"
    result = _read(page, tool)
    if tool == "run_progress":
        assert result == {"status": 200, "body": page}
    else:
        assert result["ok"] and result["evidence_complete"] == complete
        assert result["source_health"] == page["source_health"]


@pytest.mark.parametrize("damaged", [False, True])
@pytest.mark.parametrize("pending", [0, 22])
def test_real_compact_server_projection_satisfies_client_contract(tmp_path, damaged, pending):
    from tests.test_external_progress import _run
    from looplab.events.run_generation import run_generation_token
    from looplab.harness.checkpoints import ask
    rd, store, server = _run(tmp_path)
    for nid in range(2, 2 + pending):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "code": "print(2)"})
        if nid % 2:
            store.append("eval_invocation_claimed", {"node_id": nid, "generation": 0})
        ask(rd, nid, 0, "train_monitor", observation="real journal question")
    if damaged:
        (rd / "harness_decisions.jsonl").write_text('broken\n', encoding="utf8")
    generation = run_generation_token(store.read_all())
    before = (rd / "events.jsonl").read_bytes()
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=server.get(request.url.path,
                                                         params=dict(request.url.params)).json())))
    result = api.run_progress("demo", generation)
    assert result["body"]["generation"] == generation
    assert result["body"]["execution"]["engine_running"] is False
    assert result["body"]["complete"] is not damaged
    assert result["body"]["pending_checkpoint_count"] == pending
    assert result["body"]["finish_pending_node_count"] == pending
    assert (rd / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("total", [1, 20, 23])
def test_nonempty_gates_and_truncated_pending_work_remain_visible(total):
    page = progress()
    page["candidate_decisions_per_idea"] = {"novelty": 1, "candidate_ranking": 3}
    page["candidate_blockers_if_expanding"] = [{"phase_id": "report", "action": "command:report_generated"}]
    page["finish_reviews_due"] = ["skills"]
    page["finish_report_due"] = True
    page["finish_pending_nodes"] = list(range(min(total, 20)))
    page["finish_pending_node_count"] = total
    page["finish_pending_nodes_truncated"] = total > 20
    page["pending_checkpoints"] = [{"checkpoint_id": f"q{i}", "phase_id": "train_monitor",
        "node_id": i, "node_generation": 0, "claim_seq": i + 1} for i in range(min(total, 20))]
    page["pending_checkpoint_count"] = total
    page["pending_checkpoints_truncated"] = total > 20
    assert _read(page, "run_progress")["body"] == page


@pytest.mark.parametrize("complete", [True, False])
def test_additional_source_health_and_unknown_advice_code_are_compatible(complete):
    page = progress()
    page["source_health"]["future_journal"] = {"read_complete": complete}
    page["complete"] = complete
    page["next_step"]["code"] = "future_advice" if complete else "inspect_sources"
    assert _read(page, "run_progress")["body"] == page


@pytest.mark.parametrize("field,changes", [("next_step", {"reads": "GET /state"}),
    ("source_health", {"reviews": {"read_complete": "false"}}),
    ("execution", {"engine_running": 1}),
    ("execution", {"recorded_node_counts": {"building": False, "queued": 0, "evaluating": 0, "pending": 0}}),
    ("candidate_decisions_per_idea", {"novelty": True}),
    ("candidate_requirements", {"effective_concepts": "false"}),
    ("recorded_lifecycle", {"paused": 0})])
def test_boolean_and_collection_types_are_not_coerced(field, changes):
    page = progress()
    page[field].update(changes)
    assert _read(page, "run_progress")["reason"] == "invalid_progress"


@pytest.mark.parametrize("claim_seq", [True, -2, "3"])
def test_bad_checkpoint_claim_cannot_become_authority(claim_seq):
    page = progress()
    page["pending_checkpoint_count"] = 1
    page["pending_checkpoints"] = [{"checkpoint_id": "question", "phase_id": "train_monitor",
        "node_id": 0, "node_generation": 0, "claim_seq": claim_seq}]
    assert _read(page, "run_progress")["reason"] == "invalid_progress"
