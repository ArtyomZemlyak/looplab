"""Real state projection: parent references survive resets without exposing the internal map."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.node_comparison import public_parent_comparison
from looplab.serve.server import make_app

REFERENCE = json.loads((Path(__file__).parent / "fixtures/parent_comparison_v1.json").read_text())


def _run(root):
    rd = root / "comparison"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "comparison", "task_id": "t", "goal": "g", "direction": "min"})
    for nid, parents, metric in [(0, [], 10), (1, [0], 7), (2, [0, 1], 8)]:
        store.append("node_created", {"node_id": nid, "generation": 0, "parent_ids": parents,
            "operator": "draft" if not parents else "improve", "code": "private code",
            "idea": {"operator": "improve", "params": {}, "rationale": ""}})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
            "violations": [], "wall_time": 1,
            "metric_provenance": {"comparability": {"keys": {"measured": "matching-inputs"}}}})
    return rd, store


def test_state_http_owner_review_and_history_carry_the_recorded_parent_attempt(tmp_path):
    rd, store = _run(tmp_path)
    app = make_app(tmp_path)
    client = TestClient(app)
    before = client.get("/api/runs/comparison/state")
    assert before.status_code == 200
    body = before.json()
    child = body["state"]["nodes"]["1"]
    assert {key: child[key] for key in REFERENCE} == REFERENCE
    assert body["state"]["nodes"]["0"]["parent_comparison"] is None
    assert body["state"]["nodes"]["2"]["parent_comparison"] is None
    for node in body["state"]["nodes"].values():
        assert "parent_generations" not in node
        assert "code" not in node
    seq = body["seq"]
    detail = client.get("/api/runs/comparison/nodes/1").json()
    assert detail["parent_comparison"] == REFERENCE["parent_comparison"]
    assert "parent_generations" not in detail
    for nid in (0, 2):
        assert client.get(f"/api/runs/comparison/nodes/{nid}").json()["parent_comparison"] is None
    store.append("node_reset", {"node_id": 0, "generation": 0})
    after = client.get("/api/runs/comparison/state").json()["state"]
    assert after["nodes"]["0"]["attempt"] == 1
    assert after["nodes"]["1"]["parent_comparison"] == REFERENCE["parent_comparison"]
    assert after["nodes"]["1"]["parent_comparison"]["attempt"] != after["nodes"]["0"]["attempt"]
    historical = client.get(f"/api/runs/comparison/state?seq={seq}").json()["state"]
    assert historical["nodes"]["0"]["attempt"] == 0
    assert historical["nodes"]["1"]["parent_comparison"] == REFERENCE["parent_comparison"]
    after_detail = client.get("/api/runs/comparison/nodes/1").json()
    assert after_detail["parent_comparison"] == REFERENCE["parent_comparison"]
    old_detail = client.get(f"/api/runs/comparison/nodes/1?seq={seq}&expected_generation={body['generation']}")
    assert old_detail.status_code == 200
    assert old_detail.json()["parent_comparison"] == REFERENCE["parent_comparison"]
    review = app.state.looplab.state_payload(rd, audience="review")["state"]
    assert review["nodes"]["1"]["parent_comparison"] == REFERENCE["parent_comparison"]


def test_review_detail_carries_the_same_bounded_reference_without_expanding_summary_access(tmp_path, monkeypatch):
    _run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "comparison-owner")
    client = TestClient(make_app(tmp_path))
    owner = {"X-LoopLab-Token": "comparison-owner"}
    for evidence in (False, True):
        created = client.post("/api/runs/comparison/reviews", headers=owner,
            json={"ttl_seconds": 3600, "include_evidence": evidence})
        assert created.status_code == 200
        header = {"X-LoopLab-Review": created.json()["token"]}
        detail = client.get("/api/review/nodes/1", headers=header)
        if not evidence:
            assert detail.status_code == 403
        else:
            assert detail.status_code == 200
            assert detail.json()["parent_comparison"] == REFERENCE["parent_comparison"]
            assert "parent_generations" not in detail.json()


@pytest.mark.parametrize("attempt", [None, True, -1, "0", 0, 1])
def test_public_parent_reference_is_bounded_and_refuses_missing_or_invalid_attempts(attempt):
    node = SimpleNamespace(parent_ids=[0], parent_generations={"0": attempt, "foreign": "not public"})
    result = public_parent_comparison(node)
    if type(attempt) is int and attempt >= 0:
        assert result == {"version": 1, "node_id": 0, "attempt": attempt}
    else:
        assert result is None
    assert public_parent_comparison(SimpleNamespace(parent_ids=[], parent_generations={})) is None
    assert public_parent_comparison(SimpleNamespace(parent_ids=[0, 1], parent_generations={"0": 0, "1": 0})) is None
