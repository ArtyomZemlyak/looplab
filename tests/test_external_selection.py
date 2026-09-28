"""Enabled selection decisions are agent-owned and block admission until recorded."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_INJECT_NODE
from looplab.harness.selection import verification_due
from looplab.serve.control_validation import normalize_control
from looplab.serve.server import make_app
from tests.factories import http_run_generation


def _session(tmp_path, **overrides):
    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                        track_hypotheses=False, concept_pivot=False,
                        concept_run_base=False, cross_run_concepts=False, **overrides)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    (rd / "task.snapshot.json").write_bytes((Path(__file__).resolve().parents[1]
                                            / "examples" / "toy_task.json").read_bytes())
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "incarnation-one",
                                 "task_id": "task", "goal": "g",
                                 "direction": "max", "select_verifier": settings.select_verifier,
                                 "select_verifier_samples": settings.select_verifier_samples})
    for nid in (0, 1):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "rationale": f"trial {nid}"},
                                      "code": "print(1)"})
        store.append("node_evaluated", {"node_id": nid, "metric": 0.8})
    client = TestClient(make_app(tmp_path / "runs"))
    generation = http_run_generation(client)
    return rd, store, client, generation


def _candidate_error(rd, store):
    with pytest.raises(HTTPException) as exc:
        normalize_control(SimpleNamespace(state=lambda _: fold(store.read_all())), rd,
                          EV_INJECT_NODE, {"idea": {"operator": "draft"}, "code": "print(1)"})
    return exc.value.detail["code"]


def test_selection_verifier_is_complete_evidence_bound_and_idempotent(tmp_path):
    rd, store, client, generation = _session(tmp_path, select_verifier=True)
    endpoint = "/api/runs/demo/harness-selection"
    assert _candidate_error(rd, store) == "external_selection_verifier_required"
    observation = client.get(endpoint, params={"expected_generation": generation}).json()
    group = observation["tie_groups"][0]
    body = {"expected_generation": generation, "action_id": "tie-1",
            "members": [{"node_id": member["node_id"], "generation": member["generation"],
                         "evidence_digest": member["evidence_digest"],
                         "samples": [nid == 0] * 3}
                        for nid, member in enumerate(group)]}
    assert client.post(endpoint + "/verify", json={**body, "members": body["members"][:1]}).status_code == 422
    stale = {**body, "members": [{**body["members"][0], "evidence_digest": "0" * 64},
                                   body["members"][1]]}
    assert client.post(endpoint + "/verify", json=stale).status_code == 409
    result = client.post(endpoint + "/verify", json=body)
    assert result.status_code == 200, result.text
    assert client.post(endpoint + "/verify", json=body).json()["replayed"]
    assert [fold(store.read_all()).nodes[n].verifier_score for n in (0, 1)] == [1.0, 0.0]
    assert fold(store.read_all()).best_node_id == 0
    assert not client.get(endpoint, params={"expected_generation": generation}).json()["tie_groups"]
    assert not verification_due(Settings(backend="toy", external_harness=True,
                                         select_verifier=True), fold(store.read_all()))


def test_mcts_values_require_complete_current_batch_and_reopen_after_reset(tmp_path):
    rd, store, client, generation = _session(tmp_path, policy="mcts", mcts_value_weight=0.4)
    endpoint = "/api/runs/demo/harness-selection"
    assert _candidate_error(rd, store) == "external_mcts_value_required"
    observation = client.get(endpoint, params={"expected_generation": generation}).json()
    assert len(observation["value_candidates"]) == 2
    estimates = [{"node_id": n["node_id"], "generation": n["generation"],
                  "value": 0.2 if n["node_id"] else 0.8,
                  "rationale": "Several promising next changes remain"}
                 for n in observation["value_candidates"]]
    body = {"expected_generation": generation, "expected_evidence_revision":
            observation["evidence_revision"], "action_id": "values-1", "estimates": estimates}
    assert client.post(endpoint + "/values", json={**body, "estimates": estimates[:1]}).status_code == 409
    assert client.post(endpoint + "/values", json={**body,
                       "expected_evidence_revision": "0" * 64}).status_code == 409
    result = client.post(endpoint + "/values", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["count"] == 2
    assert client.post(endpoint + "/values", json=body).json()["replayed"]
    assert [fold(store.read_all()).nodes[n].value_prior for n in (0, 1)] == [0.8, 0.2]
    assert not client.get(endpoint, params={"expected_generation": generation}).json()["value_candidates"]
    store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.8})
    assert client.get(endpoint, params={"expected_generation": generation}).json()["value_candidates"][0]["generation"] == 1
    assert _candidate_error(rd, store) == "external_mcts_value_required"


def test_lesson_and_skill_cadence_requires_recorded_reviews(tmp_path):
    from looplab.harness.reviews import cadence_reviews_due

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    rd, store, client, generation = _session(tmp_path, memory_dir=str(memory_dir),
                                              lessons_every=2)
    assert _candidate_error(rd, store) == "external_memory_cadence_review_required"
    settings = Settings(backend="toy", external_harness=True, memory_dir=str(memory_dir),
                        lessons_every=2)
    assert cadence_reviews_due(rd, settings, fold(store.read_all()), generation) == [
        "lessons", "skill_candidates"]
    for phase in ("lessons", "skill_candidates"):
        body = {"expected_generation": generation, "action_id": "window-" + phase,
                "phase_id": phase, "decision": "no_applicable_action",
                "reason": "Only independent baseline trials; no comparison or reusable skill yet"}
        response = client.post("/api/runs/demo/harness-reviews", json=body)
        assert response.status_code == 200, response.text
    assert cadence_reviews_due(rd, settings, fold(store.read_all()), generation) == []
    store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.9})
    assert cadence_reviews_due(rd, settings, fold(store.read_all()), generation) == [
        "lessons", "skill_candidates"]


def test_report_cadence_requires_current_narrative(tmp_path):
    from looplab.harness.obligations import report_cadence_due

    rd, store, _, _ = _session(tmp_path, report_every=2, reflection_priors=False)
    settings = Settings(backend="toy", external_harness=True, report_every=2)
    assert _candidate_error(rd, store) == "external_report_cadence_required"
    store.append("report_generated", {"at_node": 2,
                                      "content": {"at_node": 2, "headline": "Two trials complete"}})
    assert not report_cadence_due(settings, fold(store.read_all()), store.read_all())
    store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.9})
    assert report_cadence_due(settings, fold(store.read_all()), store.read_all())
