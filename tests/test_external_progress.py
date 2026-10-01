"""A restarted agent can reconstruct its obligations and durable intermediate work."""
import json
from pathlib import Path

import pytest

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
    assert initial.json()["next_step"]["code"] == "choose_direction"
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
    assert after["next_step"]["code"] == "answer_checkpoint"
    assert after["next_step"]["phase_id"] == "stage_check"
    assert after["history"]["checkpoints"]["items"][0]["status"] == "pending"
    answer = {"expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
              "action_id": "check-1", "verdict": "proceed", "reason": "Loss decreased"}
    assert client.post("/api/runs/demo/harness-checkpoints", json=answer).status_code == 200
    restored = TestClient(make_app(tmp_path / "runs")).get(path, params=args).json()
    assert restored["pending_checkpoint_count"] == 0
    assert restored["next_step"]["code"] == "inspect_pending"
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
    assert damaged["next_step"]["code"] == "inspect_sources"
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


def test_compact_progress_preserves_gates_health_counts_and_history_location(tmp_path):
    rd, store, client = _run(tmp_path)
    config = json.loads((rd / "config.snapshot.json").read_text())
    config.update(deep_research_every=1, report_every=1)
    (rd / "config.snapshot.json").write_text(json.dumps(config))
    generation = run_generation_token(store.read_all())
    path = "/api/runs/demo/harness-progress"
    args = {"expected_generation": generation, "offset": 2, "limit": 1}
    full = client.get(path, params=args).json()
    before = store.read_all()
    compact = client.get(path, params={**args, "brief": True}).json()
    assert store.read_all() == before  # discovery never drives the engine
    assert compact["next_step"] == full["next_step"]
    assert compact["execution"] == full["execution"]
    # Expansion research is a choice-specific gate, not an unconditional instruction.
    assert compact["next_step"]["code"] == "choose_direction"
    assert compact["candidate_blockers_if_expanding"][0]["phase_id"] == "research"
    assert compact["finish_report_due"]
    assert compact["source_health"] == full["source_health"]
    assert compact["history"]["decisions"] == {"total": 0, "offset": 2, "limit": 1, "has_more": False}
    assert "harness-progress" in compact["details"]["history"]
    assert client.get(path, params={"expected_generation": "0" * 64, "brief": True}).status_code == 409
    assert client.get(path, params={"expected_generation": "bad", "brief": True}).status_code == 400

    # Large checkpoint observations stay on the authoritative detail endpoint.
    for nid in range(2, 24):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "code": "print(2)"})
        ask(rd, nid, 0, "train_monitor", observation="x" * 6000)
    compact = client.get(path, params={**args, "brief": True}).json()
    assert compact["pending_checkpoint_count"] == 22
    assert len(compact["pending_checkpoints"]) == 20
    assert compact["pending_checkpoints_truncated"]
    assert compact["finish_pending_node_count"] == 22
    assert compact["finish_pending_nodes_truncated"]
    assert len(json.dumps(compact).encode()) < 16000
    assert "observation" not in compact["pending_checkpoints"][0]
    assert compact["history"]["checkpoints"]["total"] == 22
    assert compact["next_step"]["code"] == "answer_checkpoint"
    with (rd / "harness_reviews.jsonl").open("ab") as fh:
        fh.write(b"invalid json\n")
    damaged = client.get(path, params={**args, "brief": True}).json()
    assert damaged["next_step"]["code"] == "inspect_sources"
    assert damaged["pending_checkpoint_count"] == 22


def test_progress_distinguishes_recorded_pause_and_finish_from_liveness(tmp_path):
    _, store, client = _run(tmp_path)
    args = {"expected_generation": run_generation_token(store.read_all()), "brief": True}
    for event, key in (("pause", "paused"), ("run_finished", "finished")):
        store.append(event, {"reason": "operator"})
        compact = client.get("/api/runs/demo/harness-progress", params=args).json()
        assert compact["recorded_lifecycle"][key]
        assert compact["next_step"]["code"] == "inspect_lifecycle"
        assert "does not certify" in compact["next_step"]["detail"]
        routes = client.get("/openapi.json").json()["paths"]
        for ref in compact["next_step"]["reads"]:
            method, path = ref.split(" ", 1)
            assert method.lower() in routes[path.split("?", 1)[0]]
        assert any("/command-receipt?expected_generation=" in ref for ref in compact["next_step"]["reads"])
        assert not any("/commands/{command_id}" in ref for ref in compact["next_step"]["reads"]), (
            "a discovery read must not silently restart a nonterminal command worker")


@pytest.mark.parametrize("alive, title", [(True, "evaluations already started"),
    (False, "Engine stopped"), (None, "Engine status unknown")])
def test_execution_combines_same_prefix_activity_with_independent_lock_probe(tmp_path, monkeypatch,
                                                                          alive, title):
    rd, store, client = _run(tmp_path)
    monkeypatch.setattr("looplab.engine.run_lifecycle.engine_liveness", lambda _: alive)
    for nid in range(2, 6):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"},
                                      "eval_start_boundary": nid != 5})
    for nid in (2, 3):
        store.append("node_eval_started", {"node_id": nid, "generation": 0})
    store.append("eval_attempt_withheld", {"node_id": 3, "generation": 0})
    args = {"expected_generation": run_generation_token(store.read_all()), "brief": True}
    before = {path.name: path.read_bytes() for path in rd.iterdir() if path.is_file()}
    response = client.get("/api/runs/demo/harness-progress", params=args)
    body = response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert "Authorization" in response.headers["Vary"]
    assert body["execution"] == {"engine_running": alive, "agent_connection": "not_measured",
        "recorded_node_counts": {"building": 0, "queued": 2, "evaluating": 1, "pending": 1}}
    assert title in body["next_step"]["title"]
    assert body["next_step"]["code"] == "inspect_pending"
    assert {path.name: path.read_bytes() for path in rd.iterdir() if path.is_file()} == before

    # New ownership must not reuse the old admission. A stale generation start
    # cannot put this attempt in training either; a matching re-admission can.
    store.append("resume_served", {"engine_owner_boundary": True})
    store.append("node_eval_started", {"node_id": 2, "generation": 99})
    body = client.get("/api/runs/demo/harness-progress", params=args).json()
    assert body["execution"]["recorded_node_counts"] == {
        "building": 0, "queued": 3, "evaluating": 0, "pending": 1}
    if alive is True:
        assert body["next_step"]["title"] == "Submitted experiments are awaiting evaluation"
    store.append("node_eval_started", {"node_id": 2, "generation": 0})
    store.append("node_evaluated", {"node_id": 2, "generation": 0, "metric": .5})
    body = client.get("/api/runs/demo/harness-progress", params=args).json()
    assert body["execution"]["recorded_node_counts"]["evaluating"] == 0
    assert body["finish_pending_node_count"] == 3


@pytest.mark.parametrize("phase, title", [("stage_check", "Review the completed stage"),
    ("train_monitor", "Answer the training monitor"),
    ("deadline_grace", "Decide whether to extend the deadline")])
def test_checkpoint_names_the_required_decision_even_after_engine_stops(tmp_path, phase, title):
    rd, store, client = _run(tmp_path)
    store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "eval_start_boundary": True})
    store.append("node_eval_started", {"node_id": 2, "generation": 0})
    ask(rd, 2, 0, phase, observation="training log")
    args = {"expected_generation": run_generation_token(store.read_all()), "brief": True}
    body = client.get("/api/runs/demo/harness-progress", params=args).json()
    assert body["execution"]["engine_running"] is False
    assert body["next_step"]["code"] == "answer_checkpoint"
    assert body["next_step"]["title"] == title
    assert body["next_step"]["phase_id"] == phase
    (rd / "harness_reviews.jsonl").write_text("invalid\n")
    damaged = client.get("/api/runs/demo/harness-progress", params=args).json()
    assert damaged["next_step"]["code"] == "inspect_sources"
    assert damaged["pending_checkpoint_count"] == 1
