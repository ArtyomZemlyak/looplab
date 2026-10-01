"""External checkpoint discovery follows sidecar writes, evaluator claims and source health."""
import json

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.harness.checkpoints import ask
from looplab.serve.external_attention import external_checkpoint_attention
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app


@pytest.fixture
def run(tmp_path, monkeypatch):
    rd = tmp_path / "demo"
    rd.mkdir()
    config = rd / "config.snapshot.json"
    config.write_text(json.dumps(Settings(backend="toy", external_harness=True).model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "external-one",
                                 "task_id": "toy", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    monkeypatch.setattr("looplab.serve.routers.attention._engine_liveness", lambda _: True)
    return rd, store, TestClient(make_app(tmp_path))


def refreshed(client, monkeypatch, previous, predicate=lambda _: True):
    monkeypatch.setattr("looplab.serve.routers.attention._SNAPSHOT_SOFT_REFRESH_SECONDS", 0.0)
    for _ in range(400):
        payload = client.get("/api/attention").json()
        if payload["generated_at"] != previous["generated_at"] and predicate(payload):
            return payload
    raise AssertionError("attention did not refresh")


@pytest.mark.parametrize("phase", ["stage_check", "train_monitor", "asha_live", "deadline_grace"])
def test_question_and_answer_refresh_without_a_new_event(run, monkeypatch, phase):
    rd, store, client = run
    first = client.get("/api/attention").json()
    assert first["items"] == [] and not first["partial"]
    before = (rd / "events.jsonl").read_bytes()
    q = ask(rd, 0, 0, phase, observation="PRIVATE training log", expectation="PRIVATE hypothesis")
    opened = refreshed(client, monkeypatch, first, lambda value: value["active_action_count"] == 1)
    assert opened["active_action_count"] == 1 and not opened["partial"]
    item, = opened["items"]
    assert item["kind"] == "external_checkpoint"
    assert item["node_id"] == 0 and item["node_generation"] == 0
    assert item["generation"] == run_generation_token(store.read_all())
    assert item["active"] and not item["browser"] and not item["derived"]
    assert "PRIVATE" not in json.dumps(opened)
    # Polling the inbox itself performs no recovery, answer or command write.
    assert (rd / "events.jsonl").read_bytes() == before
    same = refreshed(client, monkeypatch, opened)
    assert same["items"][0]["id"] == item["id"]
    response = client.post("/api/runs/demo/harness-checkpoints", json={
        "expected_generation": item["generation"], "checkpoint_id": q["checkpoint_id"],
        "action_id": "answer-one", "verdict": {"stage_check": "proceed", "deadline_grace": "stop"}.get(phase, "continue"),
        "reason": "Reviewed current evidence"})
    assert response.status_code == 200, response.text
    answered = refreshed(client, monkeypatch, same, lambda value: value["active_action_count"] == 0)
    assert answered["items"] == [] and answered["active_action_count"] == 0
    assert (rd / "events.jsonl").read_bytes() == before


def test_damaged_checkpoint_source_retains_only_explicitly_stale_attention(run, monkeypatch):
    rd, store, client = run
    ask(rd, 0, 0, "stage_check", observation="must never reach the inbox")
    first = client.get("/api/attention").json()
    with (rd / "harness_checkpoints.jsonl").open("ab") as stream:
        stream.write(b"{broken json\n")
    damaged = refreshed(client, monkeypatch, first, lambda value: value["partial"])
    assert damaged["partial"]
    assert damaged["items"][0]["stale"]
    assert damaged["items"][0]["id"] == first["items"][0]["id"]
    assert not damaged["items"][0]["browser"]
    fresh_server = TestClient(make_app(rd.parent)).get("/api/attention").json()
    assert fresh_server["partial"] and not fresh_server["items"]


@pytest.mark.parametrize("change", ["reclaim", "terminal", "reset", "incarnation", "internal"])
def test_old_questions_cannot_survive_a_changed_owner_or_subject(run, change):
    rd, store, _ = run
    ask(rd, 0, 0, "stage_check")
    initial = store.read_all()
    assert external_checkpoint_attention("demo", rd, initial)
    if change == "reclaim":
        store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0})
    elif change == "terminal":
        store.append("node_evaluated", {"node_id": 0, "metric": 1})
    elif change == "reset":
        store.append("node_reset", {"node_id": 0})
    elif change == "incarnation":
        replacement = EventStore(rd / "replacement.jsonl")
        replacement.append("run_started", {"run_id": "demo", "run_uid": "external-two", "task_id": "toy"})
        replacement.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": {"operator": "draft"}})
        initial = replacement.read_all()
    else:
        (rd / "config.snapshot.json").write_text(json.dumps(Settings(backend="toy").model_dump(mode="json")))
    assert not external_checkpoint_attention("demo", rd, initial if change == "incarnation" else store.read_all())


def test_list_mode_follows_snapshot_repairs_without_losing_the_run(run):
    rd, store, client = run
    first, = client.get("/api/runs").json()
    assert first["external_harness"] is True
    before = (rd / "events.jsonl").read_bytes()
    (rd / "config.snapshot.json").write_text("{broken")
    unknown, = client.get("/api/runs").json()
    assert unknown["external_harness"] is None
    (rd / "config.snapshot.json").write_text(json.dumps(Settings(backend="toy").model_dump(mode="json")))
    repaired, = client.get("/api/runs").json()
    assert repaired["external_harness"] is False
    assert repaired["generation"] == first["generation"]
    assert (rd / "events.jsonl").read_bytes() == before


def test_unreadable_mode_marks_attention_partial_until_repaired(run, monkeypatch):
    rd, store, client = run
    ask(rd, 0, 0, "stage_check")
    first = client.get("/api/attention").json()
    saved = (rd / "config.snapshot.json").read_bytes()
    (rd / "config.snapshot.json").write_text("{broken")
    damaged = refreshed(client, monkeypatch, first, lambda value: value["partial"])
    assert damaged["items"][0]["stale"]
    (rd / "config.snapshot.json").write_bytes(saved)
    repaired = refreshed(client, monkeypatch, damaged, lambda value: not value["partial"])
    assert not repaired["items"][0].get("stale", False)


def test_external_questions_are_ordered_and_counted_before_paging(run):
    rd, store, client = run
    for phase in ("stage_check", "train_monitor", "deadline_grace"):
        ask(rd, 0, 0, phase)
    first = client.get("/api/attention?limit=1").json()
    assert first["active_action_count"] == 3 and len(first["items"]) == 1
    ids = {first["items"][0]["id"]}
    cursor = first["next_cursor"]
    while cursor:
        page = client.get("/api/attention", params={"limit": 1, "cursor": cursor}).json()
        assert page["active_action_count"] == 3
        ids.add(page["items"][0]["id"])
        cursor = page["next_cursor"]
    assert len(ids) == 3


def test_checkpoint_inbox_requires_authentication_and_preserves_existing_harness_read_access(run, monkeypatch):
    rd, store, _ = run
    ask(rd, 0, 0, "stage_check")
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-test")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-test")
    client = TestClient(make_app(rd.parent))
    assert client.get("/api/attention").status_code == 401
    agent = client.get("/api/attention", headers={"X-LoopLab-Token": "agent-test"})
    assert agent.status_code == 200 and agent.json()["active_action_count"] == 1
    response = client.get("/api/attention", headers={"X-LoopLab-Token": "owner-test"})
    assert response.status_code == 200 and response.json()["active_action_count"] == 1
