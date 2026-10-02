"""Knowledge publication needs complete sources; retry never refreshes old evidence."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def _session(tmp_path, role="shared"):
    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    memory = tmp_path / "memory"
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True,
        memory_dir=str(memory)).model_dump_json())
    (rd / "task.snapshot.json").write_bytes((Path(__file__).resolve().parents[1] /
                                            "examples" / "toy_task.json").read_bytes())
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task",
                                 "goal": "small search", "direction": "min"})
    store.append("node_created", {"node_id": 0, "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 3}}, "code": "x"})
    store.append("node_evaluated", {"node_id": 0, "metric": .25})
    client = TestClient(make_app(tmp_path / "runs"))
    generation = run_generation_token(store.read_all())
    lesson = {"expected_generation": generation, "action_id": "lesson-1",
        "statement": "Use explicit candidate search to reach the optimum in small parameter spaces",
        "outcome": "supported", "evidence": [0]}
    if role != "shared":
        lesson["role"] = role
    skill = {"expected_generation": generation, "action_id": "skill-1", "lesson_action_id": "lesson-1",
             "body": "Search bounded parameter candidates and validate each measured score."}
    for route, body in (("lessons", lesson), ("skill-candidates", skill)):
        result = client.post("/api/runs/demo/" + route, json=body)
        assert result.status_code == 200, result.text
    return rd, memory, store, client, lesson, skill


def _bytes(rd, memory):
    return {(str(p.relative_to(root)), str(root)): p.read_bytes()
            for root in (rd, memory) for p in root.rglob("*") if p.is_file() and p.suffix != ".lock"}


@pytest.mark.parametrize("change", ["reset", "tombstone", "abort"])
def test_original_lesson_ack_survives_lifecycle_change_without_new_approval(tmp_path, change):
    rd, memory, store, client, lesson, _ = _session(tmp_path)
    event = {"reset": "node_reset", "tombstone": "node_tombstoned", "abort": "node_abort"}[change]
    store.append(event, {"node_ids": [0]} if change == "tombstone" else {"node_id": 0})
    before = _bytes(rd, memory)
    replay = client.post("/api/runs/demo/lessons", json=lesson)
    assert replay.status_code == 200 and replay.json()["replayed"], replay.text
    assert client.post("/api/runs/demo/lessons", json={**lesson, "statement": "changed"}).status_code == 409
    assert client.post("/api/runs/demo/lessons", json={**lesson, "action_id": "fresh"}).status_code == 409
    assert _bytes(rd, memory) == before


@pytest.mark.parametrize("kind", ["lessons", "skill-candidates"])
@pytest.mark.parametrize("damage", [b'broken record\n', b'[]\n', b'\xff\n'])
def test_event_damage_refuses_original_and_fresh_knowledge_publication(tmp_path, kind, damage):
    rd, memory, _, client, lesson, skill = _session(tmp_path)
    body = lesson if kind == "lessons" else skill
    path = rd / "events.jsonl"
    original = path.read_bytes()
    path.write_bytes(original + damage)
    before = _bytes(rd, memory)
    for payload in (body, {**body, "action_id": "fresh"}):
        reply = client.post("/api/runs/demo/" + kind, json=payload)
        assert reply.status_code == 503, reply.text
        assert reply.json()["detail"]["source"] == "events.jsonl"
    assert _bytes(rd, memory) == before
    path.write_bytes(original)
    assert client.post("/api/runs/demo/" + kind, json=body).json()["replayed"]


@pytest.mark.parametrize("source,kind", [("lessons.jsonl", "lessons"),
    ("lessons.jsonl", "skill-candidates"), ("skill_candidate_actions.jsonl", "skill-candidates")])
@pytest.mark.parametrize("damage", [b'broken record\n', b'[]\n', b'\xff\n'])
def test_shared_source_damage_refuses_ack_or_new_work(tmp_path, source, kind, damage):
    rd, memory, _, client, lesson, skill = _session(tmp_path)
    body = lesson if kind == "lessons" else skill
    path = memory / source
    original = path.read_bytes()
    path.write_bytes(original + damage)
    before = _bytes(rd, memory)
    payloads = (body, {**body, "action_id": "fresh"}) if source != "lessons.jsonl" or kind == "lessons" else ({**body, "action_id": "fresh"},)
    for payload in payloads:
        reply = client.post("/api/runs/demo/" + kind, json=payload)
        assert reply.status_code == 503, reply.text
        assert reply.json()["detail"]["source"] == source
    assert _bytes(rd, memory) == before
    path.write_bytes(original)
    assert client.post("/api/runs/demo/" + kind, json=body).json()["replayed"]


def test_original_skill_ack_does_not_revalidate_or_recreate_retired_lesson(tmp_path):
    rd, memory, store, client, _, skill = _session(tmp_path)
    store.append("node_reset", {"node_id": 0})
    (memory / "lessons.jsonl").unlink()
    before = _bytes(rd, memory)
    replay = client.post("/api/runs/demo/skill-candidates", json=skill)
    assert replay.status_code == 200 and replay.json()["replayed"], replay.text
    assert client.post("/api/runs/demo/skill-candidates", json={**skill, "action_id": "fresh"}).status_code == 409
    assert _bytes(rd, memory) == before


@pytest.mark.parametrize("role", ["shared", "researcher", "developer"])
def test_lesson_role_survives_publication_and_exact_retry(tmp_path, role):
    rd, memory, _, client, lesson, _ = _session(tmp_path, role)
    row, = [json.loads(line) for line in (memory / "lessons.jsonl").read_text().splitlines()]
    assert row.get("role", "shared") == role
    before = _bytes(rd, memory)
    replay = client.post("/api/runs/demo/lessons", json=lesson)
    assert replay.status_code == 200 and replay.json()["replayed"], replay.text
    assert replay.json()["lesson"].get("role", "shared") == role
    assert client.post("/api/runs/demo/lessons", json={**lesson,
        "role": "developer" if role != "developer" else "researcher"}).status_code == 409
    assert _bytes(rd, memory) == before


@pytest.mark.parametrize("kind", ["lessons", "skill-candidates"])
def test_generation_change_never_replays_old_knowledge_action(tmp_path, kind):
    rd, memory, _, client, lesson, skill = _session(tmp_path)
    before = _bytes(rd, memory)
    body = lesson if kind == "lessons" else skill
    assert client.post("/api/runs/demo/" + kind, json={**body,
        "expected_generation": "f" * 64}).status_code == 409
    assert _bytes(rd, memory) == before


def test_original_lesson_ack_does_not_restore_a_retired_claim(tmp_path):
    rd, memory, _, client, lesson, _ = _session(tmp_path)
    path = memory / "lessons.jsonl"
    row = json.loads(path.read_text())
    row["outcome"] = "retired"
    path.write_text(json.dumps(row) + "\n")
    before = _bytes(rd, memory)
    replay = client.post("/api/runs/demo/lessons", json=lesson)
    assert replay.status_code == 200 and replay.json()["replayed"], replay.text
    assert replay.json()["lesson"]["outcome"] == "retired"
    assert _bytes(rd, memory) == before


def test_legacy_shared_store_objects_remain_compatible(tmp_path):
    from looplab.harness.journals import read_knowledge_source
    path = tmp_path / "lessons.jsonl"
    path.write_text(json.dumps({"statement": "Legacy source without harness metadata", "outcome": "tested"}) + "\n")
    assert read_knowledge_source(path)[0]["outcome"] == "tested"


@pytest.mark.parametrize("phase,source,action", [("lessons", "lessons.jsonl", "lesson-1"),
    ("skill_candidates", "skill_candidate_actions.jsonl", "skill-1")])
def test_fresh_completed_review_cannot_approve_damaged_knowledge_source(tmp_path, phase, source, action):
    rd, memory, _, client, lesson, _ = _session(tmp_path)
    path = memory / source
    path.write_bytes(path.read_bytes() + b'broken record\n')
    before = _bytes(rd, memory)
    reply = client.post("/api/runs/demo/harness-reviews", json={
        "expected_generation": lesson["expected_generation"], "action_id": "review-1",
        "phase_id": phase, "decision": "completed", "action_ref": action, "evidence": [0],
        "reason": "Reviewed a fixture knowledge publication without claiming new measured evidence."})
    assert reply.status_code == 503, reply.text
    assert reply.json()["detail"]["source"] == source
    assert _bytes(rd, memory) == before
