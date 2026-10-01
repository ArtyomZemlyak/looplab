"""A saved receipt remains replayable; only a current evaluator may consume its answer."""
import json

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.harness.checkpoints import answer_for, ask
from tests.test_external_checkpoints import seeded


@pytest.mark.parametrize("move", ["reset", "reclaim", "terminal", "new_run", "new_uid"])
def test_saved_answer_cannot_authorize_a_superseded_evaluator(tmp_path, move):
    rd, store, client = seeded(tmp_path)
    store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0,
        "attempt": 0, "invocation_id": "first"})
    question = ask(rd, 0, 0, "stage_check", stage="train")
    generation = run_generation_token(store.read_all())
    body = {"expected_generation": generation, "checkpoint_id": question["checkpoint_id"],
        "action_id": "accepted:answer", "verdict": "proceed", "reason": "Reviewed first evaluator"}
    path = "/api/runs/demo/harness-checkpoints"
    assert client.post(path, json=body).status_code == 200
    assert answer_for(rd, question["checkpoint_id"])["verdict"] == "proceed"
    journal = rd / "harness_checkpoints.jsonl"
    before = journal.read_bytes()
    if move == "reset":
        store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    elif move == "reclaim":
        store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0,
            "attempt": 0, "invocation_id": "second", "after_interrupted_attempt": True})
    elif move == "terminal":
        store.append("node_failed", {"node_id": 0, "generation": 0, "error": "stopped"})
    elif move == "new_run":
        (rd / "events.jsonl").unlink()
        replacement = EventStore(rd / "events.jsonl")
        replacement.append("run_started", {"run_id": "demo", "direction": "min"})
        replacement.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft"})
    else:
        events = [event.model_dump(mode="json") for event in store.read_all()]
        events[0]["data"]["run_uid"] = "another-incarnation"
        (rd / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf8")
        assert run_generation_token(EventStore(rd / "events.jsonl").read_all()) == generation
    assert answer_for(rd, question["checkpoint_id"]) is None
    # An old ACK is still evidence of acceptance, not permission for a new evaluator.
    if move in {"reset", "reclaim", "terminal"}:
        assert client.post(path, json=body).json()["replayed"]
    assert journal.read_bytes() == before


def test_pause_does_not_supersede_an_inflight_checkpoint(tmp_path):
    rd, store, client = seeded(tmp_path)
    question = ask(rd, 0, 0, "stage_check", stage="train")
    generation = run_generation_token(store.read_all())
    store.append("pause", {})
    assert client.post("/api/runs/demo/harness-checkpoints", json={
        "expected_generation": generation, "checkpoint_id": question["checkpoint_id"],
        "action_id": "paused:answer", "verdict": "proceed", "reason": "Finish only this inflight check"}).status_code == 200
    assert answer_for(rd, question["checkpoint_id"])["verdict"] == "proceed"


@pytest.mark.parametrize("visible_events", [0, 1, 2])
def test_behind_event_read_cannot_authorize_a_saved_answer_but_recovers(tmp_path, monkeypatch, visible_events):
    import looplab.harness.checkpoints as checkpoints

    rd, store, client = seeded(tmp_path)
    store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0,
        "attempt": 0, "invocation_id": "current"})
    question = ask(rd, 0, 0, "stage_check")
    generation = run_generation_token(store.read_all())
    assert client.post("/api/runs/demo/harness-checkpoints", json={"expected_generation": generation,
        "checkpoint_id": question["checkpoint_id"], "action_id": "behind:answer",
        "verdict": "proceed", "reason": "Reviewed"}).status_code == 200
    complete = store.read_all()
    real_read = checkpoints.EventStore.read_all
    monkeypatch.setattr(checkpoints.EventStore, "read_all", lambda self: complete[:visible_events])
    assert answer_for(rd, question["checkpoint_id"]) is None
    monkeypatch.setattr(checkpoints.EventStore, "read_all", real_read)
    assert answer_for(rd, question["checkpoint_id"])["verdict"] == "proceed"
