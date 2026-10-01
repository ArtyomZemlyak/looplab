"""Recovery must not read or answer a checkpoint from an incomplete source prefix."""
import json

import pytest
from fastapi.testclient import TestClient

from looplab.harness.checkpoints import answer_for, ask
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app
from tests.test_external_checkpoints import seeded


@pytest.mark.parametrize("damage", ["json", "duplicate", "duplicate_answer", "orphan", "shape", "events"])
def test_damaged_sources_refuse_checkpoint_reads_and_answers_until_repaired(tmp_path, damage):
    rd, store, _ = seeded(tmp_path)
    client = TestClient(make_app(rd.parent), raise_server_exceptions=False)
    question = ask(rd, 0, 0, "stage_check", stage="train")
    generation = run_generation_token(store.read_all())
    journal = rd / "harness_checkpoints.jsonl"
    source = rd / "events.jsonl" if damage == "events" else journal
    valid = source.read_bytes()
    answer_row = json.dumps({"type": "answer", "checkpoint_id": question["checkpoint_id"],
        "verdict": "proceed", "action_id": "duplicate:answer", "reason": "Reviewed", "failure_kind": ""}).encode() + b'\n'
    poison = {
        "json": b'{broken\n', "events": b'{broken\n',
        "duplicate": json.dumps(question).encode() + b'\n',
        "duplicate_answer": answer_row * 2,
        "orphan": json.dumps({"type": "answer", "checkpoint_id": "absent",
            "verdict": "proceed", "action_id": "orphan", "reason": "unknown subject"}).encode() + b'\n',
        "shape": b'{"type":"question","checkpoint_id":8}\n',
    }[damage]
    source.write_bytes(valid + poison)
    before = {p: p.read_bytes() for p in (source, journal)}
    path = "/api/runs/demo/harness-checkpoints"
    answer = {"expected_generation": generation, "checkpoint_id": question["checkpoint_id"],
              "action_id": "source-health:answer", "verdict": "proceed", "reason": "Reviewed stage"}
    assert client.get(path, params={"expected_generation": generation}).status_code == 503
    assert client.post(path, json=answer).status_code == 503
    assert all(p.read_bytes() == content for p, content in before.items())
    source.write_bytes(valid)
    assert len(client.get(path, params={"expected_generation": generation}).json()["pending"]) == 1
    assert client.post(path, json=answer).status_code == 200
    assert client.post(path, json=answer).json()["replayed"]


@pytest.mark.parametrize("source_name", ["harness_checkpoints.jsonl", "events.jsonl"])
def test_engine_cannot_publish_or_consume_an_answer_from_a_damaged_source(tmp_path, source_name):
    rd, store, client = seeded(tmp_path)
    question = ask(rd, 0, 0, "stage_check")
    generation = run_generation_token(store.read_all())
    assert client.post("/api/runs/demo/harness-checkpoints", json={"expected_generation": generation,
        "checkpoint_id": question["checkpoint_id"], "action_id": "healthy:answer",
        "verdict": "proceed", "reason": "Reviewed"}).status_code == 200
    source = rd / source_name
    source.write_bytes(source.read_bytes() + b'{broken\n')
    damaged = source.read_bytes()
    with pytest.raises(OSError):
        answer_for(rd, question["checkpoint_id"])
    with pytest.raises(OSError):
        ask(rd, 0, 0, "stage_check")
    assert source.read_bytes() == damaged


def test_engine_cannot_consume_a_saved_answer_when_event_source_is_missing(tmp_path):
    rd, store, client = seeded(tmp_path)
    question = ask(rd, 0, 0, "stage_check")
    generation = run_generation_token(store.read_all())
    assert client.post("/api/runs/demo/harness-checkpoints", json={"expected_generation": generation,
        "checkpoint_id": question["checkpoint_id"], "action_id": "healthy:answer",
        "verdict": "proceed", "reason": "Reviewed"}).status_code == 200
    events = rd / "events.jsonl"
    valid = events.read_bytes()
    events.unlink()
    with pytest.raises(OSError):
        answer_for(rd, question["checkpoint_id"])
    events.write_bytes(valid)
    assert answer_for(rd, question["checkpoint_id"])["verdict"] == "proceed"


def test_damage_during_event_read_cannot_publish_a_question(tmp_path, monkeypatch):
    from looplab.events.eventstore import EventStore

    rd, _, _ = seeded(tmp_path)
    original = EventStore.read_all

    def damaged_read(store):
        events = original(store)
        store.path.write_bytes(store.path.read_bytes() + b'{broken\n')
        return events

    monkeypatch.setattr(EventStore, "read_all", damaged_read)
    with pytest.raises(OSError):
        ask(rd, 0, 0, "stage_check")
    assert not (rd / "harness_checkpoints.jsonl").exists()
