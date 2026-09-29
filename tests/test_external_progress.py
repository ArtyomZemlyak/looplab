"""A restarted agent can reconstruct its obligations and durable intermediate work."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.harness.checkpoints import ask
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app


def _run(tmp_path):
    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                        concept_pivot=False, concept_run_base=False,
                        cross_run_concepts=False, track_hypotheses=False,
                        report_every=0, novelty_mode="llm",
                        memory_dir=str(tmp_path / "memory"))
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    (rd / "task.snapshot.json").write_bytes((Path(__file__).resolve().parents[1]
                                            / "examples" / "toy_task.json").read_bytes())
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "incarnation-one",
                                 "task_id": "task", "goal": "g", "direction": "min"})
    for nid in (0, 1):
        store.append("node_created", {"node_id": nid, "parent_ids": [],
                                      "operator": "draft", "idea": {"operator": "draft"},
                                      "code": "print(1)"})
        store.append("node_evaluated", {"node_id": nid, "metric": 1.0 + nid})
    return rd, store, TestClient(make_app(tmp_path / "runs"))


def test_policy_preview_tracks_live_strategy_without_creating_candidates(tmp_path):
    rd, store, client = _run(tmp_path)
    config = json.loads((rd / "config.snapshot.json").read_text())
    config["n_seeds"] = 2
    (rd / "config.snapshot.json").write_text(json.dumps(config))
    generation = run_generation_token(store.read_all())
    before = len(store.read_all())
    preview = client.get("/api/runs/demo/harness-progress", params={
        "expected_generation": generation}).json()["policy_preview"]
    assert preview["policy"] == "greedy"
    assert preview["policy_source"] == "config_snapshot"
    assert preview["actions"][0]["kind"] == "improve"
    assert preview["actions"][0]["parent_id"] == 0
    assert len(store.read_all()) == before
    store.append("strategy_decision", {"strategy": {"policy": "mcts"}, "at_node": 2})
    preview = client.get("/api/runs/demo/harness-progress", params={
        "expected_generation": generation}).json()["policy_preview"]
    assert preview["policy"] == "mcts"
    assert preview["policy_source"] == "recorded_strategy"
    assert preview["actions"] and len(store.read_all()) == before + 1


def test_progress_restores_decisions_reviews_and_checkpoint_answers(tmp_path):
    rd, store, client = _run(tmp_path)
    generation = run_generation_token(store.read_all())
    path = "/api/runs/demo/harness-progress"
    args = {"expected_generation": generation}
    initial = client.get(path, params=args)
    assert initial.status_code == 200, initial.text
    assert initial.json()["policy_preview"]["policy"] == "greedy"
    assert initial.json()["policy_preview"]["actions"]
    assert initial.json()["candidate_decisions_per_idea"]["novelty"] == 1
    assert initial.json()["candidate_requirements"] == {
        "effective_concepts": False, "hypothesis_statement": False}
    assert initial.json()["finish_pending_nodes"] == []
    assert initial.json()["source_health"]["decisions"]["file_present"] is False
    assert initial.json()["history"]["decisions"]["total"] == 0
    decision = {"expected_generation": generation, "phase_id": "novelty",
                "action_id": "novelty-1", "idea": {"operator": "draft"},
                "decision": "submit", "reason": "The idea differs from previous trials"}
    for action_id in ("novelty-1", "novelty-2"):
        response = client.post("/api/runs/demo/harness-decisions",
                               json={**decision, "action_id": action_id})
        assert response.status_code == 200, response.text
    review = {"expected_generation": generation, "phase_id": "concept_merge",
              "action_id": "review-1", "decision": "no_applicable_action",
              "reason": "The measured nodes share no concept alias to consolidate"}
    response = client.post("/api/runs/demo/harness-reviews", json=review)
    assert response.status_code == 200, response.text
    current = client.get(path, params={**args, "limit": 1}).json()
    assert current["complete"] and current["event_seq"] == store.read_all()[-1].seq
    assert current["history"]["decisions"]["total"] == 2
    assert current["history"]["decisions"]["has_more"]
    assert current["history"]["decisions"]["items"][0]["action_id"] == "novelty-2"
    older = client.get(path, params={**args, "offset": 1, "limit": 1}).json()
    assert older["history"]["decisions"]["items"][0]["action_id"] == "novelty-1"
    assert older["history"]["reviews"]["items"] == []
    assert current["history"]["reviews"]["items"][0]["validity"] == "current"

    store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(2)"})
    q = ask(rd, 2, 0, "stage_check", stage="train", expectation="loss decreases")
    after = client.get(path, params=args).json()
    assert after["finish_pending_nodes"] == [2]
    assert after["history"]["decisions"]["items"][0]["validity"] == "superseded"
    assert after["history"]["reviews"]["items"][0]["validity"] == "superseded"
    assert after["pending_checkpoint_count"] == 1
    assert after["history"]["checkpoints"]["items"][0]["status"] == "pending"
    answer = {"expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
              "action_id": "check-1", "verdict": "proceed", "reason": "Loss decreased"}
    assert client.post("/api/runs/demo/harness-checkpoints", json=answer).status_code == 200
    restored = TestClient(make_app(tmp_path / "runs")).get(path, params=args).json()
    assert restored["pending_checkpoint_count"] == 0
    assert restored["history"]["checkpoints"]["items"][0]["status"] == "answered"
    assert restored["history"]["checkpoints"]["items"][0]["answer"]["reason"] == "Loss decreased"
    store.append("eval_invocation_claimed", {"node_id": 2, "generation": 0})
    reclaimed = client.get(path, params=args).json()
    assert reclaimed["history"]["checkpoints"]["items"][0]["lifecycle"] == "superseded"
    assert client.get(path, params={"expected_generation": "0" * 64}).status_code == 409

    with (rd / "harness_decisions.jsonl").open("ab") as fh:
        fh.write(b"invalid json\n")
    damaged = client.get(path, params=args).json()
    assert damaged["complete"] is False
    assert damaged["source_health"]["decisions"]["invalid_lines"] == 1
    assert damaged["history"]["decisions"]["total"] == 2
    with (rd / "harness_reviews.jsonl").open("ab") as fh:
        fh.write(b'{"run_uid":"incarnation-one","generation":"unbound"}\n')
    structural = client.get(path, params=args).json()
    assert structural["complete"] is False
    assert structural["source_health"]["reviews"]["invalid_record_rows"] == 1
    assert structural["history"]["reviews"]["total"] == 1
    with (rd / "events.jsonl").open("ab") as fh:
        fh.write(b"not a durable event\n")
    hidden_tail = client.get(path, params=args).json()
    assert hidden_tail["complete"] is False
    assert hidden_tail["source_health"]["events"]["complete"] is False
