"""A reconnect observation never drives, heals or waits on a command worker."""
import json

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.command_identity import command_identity
from looplab.serve.server import make_app


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir()
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=True).model_dump_json())
    (rd / "task.snapshot.json").write_text('{"kind":"quadratic","goal":"g","direction":"min"}')
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
    client = TestClient(make_app(tmp_path))
    return rd, store, client, client.app.state.looplab.commands


def _record(rd, generation, status="accepted", **extra):
    command_id, digest = command_identity("original-key")
    path = rd / ".commands" / (command_id + ".json")
    path.parent.mkdir(exist_ok=True)
    row = {"id": command_id, "idempotency_key_digest": digest, "run_generation": generation,
           "event_type": "inject_node", "status": status, "data": {"code": "owner-secret"},
           "error": None, **extra}
    path.write_text(json.dumps(row))
    return path, row


@pytest.mark.parametrize("status", ["accepted", "executing", "succeeded", "failed", "timed_out", "rejected", "noop"])
def test_receipt_by_id_and_lost_response_key_never_drives_or_changes_files(tmp_path, monkeypatch, status):
    rd, store, client, service = _client(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    _, row = _record(rd, generation, status, event_seq=3,
                     error={"code": "unavailable", "message": "owner-secret", "retryable": True})
    def forbidden(*args, **kwargs):
        pytest.fail("observation reached a mutation/recovery path")
    for name in ("sequence", "_start_worker", "_read_existing", "_save", "_reconcile_observation"):
        monkeypatch.setattr(service, name, forbidden)
    before = {str(p): p.read_bytes() for p in rd.rglob("*") if p.is_file()}
    for token in ("owner-secret", "agent-secret"):
        for query, key in (({"command_id": row["id"]}, ""), ({}, "original-key")):
            response = client.get("/api/runs/demo/command-receipt", params={
                "expected_generation": generation, **query}, headers={"X-LoopLab-Token": token,
                                                                          "Idempotency-Key": key})
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["command"]["status"] == status and value["command"]["id"] == row["id"]
            assert value["terminal"] == (status not in ("accepted", "executing"))
            assert value["command"]["event_seq"] == 3
            assert "owner-secret" not in response.text and "original-key" not in response.text
            assert "idempotency_key_digest" not in response.text and "data" not in value["command"]
            assert response.headers["Cache-Control"] == "no-store"
    assert {str(p): p.read_bytes() for p in rd.rglob("*") if p.is_file()} == before


def test_missing_corrupt_unbound_stale_and_invalid_receipts_fail_visibly(tmp_path, monkeypatch):
    rd, store, client, _ = _client(tmp_path, monkeypatch)
    gen = run_generation_token(store.read_all())
    headers = {"X-LoopLab-Token": "agent-secret", "Idempotency-Key": "original-key"}
    path = "/api/runs/demo/command-receipt"
    assert client.get(path, params={"expected_generation": gen}).status_code == 401
    assert client.get(path, params={"expected_generation": gen}, headers=headers).status_code == 404
    assert not (rd / ".commands").exists()
    record_path, row = _record(rd, gen)
    for payload, status in ((b"", 503), (b"not json", 503), (json.dumps({**row, "status": []}).encode(), 503),
                            (b"[" * 2000 + b"]" * 2000, 503),
                            (json.dumps({**row, "run_generation": "b" * 64}).encode(), 409),
                            (json.dumps({**row, "idempotency_key_digest": "other"}).encode(), 409)):
        record_path.write_bytes(payload)
        response = client.get(path, params={"expected_generation": gen}, headers=headers)
        assert response.status_code == status, response.text
        assert record_path.read_bytes() == payload, "an observation healed/changed the record"
    record_path.write_text(json.dumps(row))
    assert client.get(path, params={"expected_generation": "b" * 64}, headers=headers).status_code == 409
    assert client.get(path, params={"expected_generation": "bad"}, headers=headers).status_code == 400
    assert client.get(path, params={"expected_generation": gen, "command_id": row["id"]}, headers=headers).status_code == 400
    assert client.get(path, params={"expected_generation": gen}, headers={"X-LoopLab-Token": "agent-secret"}).status_code == 400


def test_receipt_uses_the_submission_identity_and_fences_a_reset_during_read(tmp_path, monkeypatch):
    _, store, client, service = _client(tmp_path, monkeypatch)
    gen = run_generation_token(store.read_all())
    params = client.get("/openapi.json").json()["paths"]["/api/runs/{run_id}/command-receipt"]["get"]["parameters"]
    assert any(row["name"] == "Idempotency-Key" and row["in"] == "header" for row in params)
    headers = {"X-LoopLab-Token": "agent-secret", "Idempotency-Key": "original-key"}
    response = client.post("/api/runs/demo/commands", headers=headers, json={
        "type": "hint", "data": {"invalid": True}, "expected_generation": gen})
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    receipt = client.get("/api/runs/demo/command-receipt", headers=headers, params={"expected_generation": gen})
    assert receipt.status_code == 200 and receipt.json()["command"]["id"] == response.json()["id"]
    original = service.generation_fence
    calls = []
    def changed(rd):
        calls.append(rd)
        canonical, generation = original(rd)
        return canonical, generation if len(calls) == 1 else "b" * 64
    monkeypatch.setattr(service, "generation_fence", changed)
    assert client.get("/api/runs/demo/command-receipt", headers=headers, params={"expected_generation": gen}).status_code == 409


def test_oversized_receipt_is_unavailable_without_a_partial_success(tmp_path, monkeypatch):
    rd, store, client, _ = _client(tmp_path, monkeypatch)
    gen = run_generation_token(store.read_all())
    path, row = _record(rd, gen)
    before = path.read_bytes()
    monkeypatch.setattr("looplab.serve.command_receipt._MAX_RECORD_BYTES", 64)
    response = client.get("/api/runs/demo/command-receipt", params={
        "expected_generation": gen, "command_id": row["id"]}, headers={"X-LoopLab-Token": "agent-secret"})
    assert response.status_code == 503 and path.read_bytes() == before


@pytest.mark.parametrize("changes", [{"event_seq": True}, {"event_seq": -1}, {"event_seq": "3"},
    {"error": []}, {"error": "failed"}, {"error": {"code": None}},
    {"error": {"code": ["failed"]}}, {"error": {"retryable": 1}}, {"error": {"retryable": "false"}}])
def test_receipt_does_not_launder_damaged_error_or_sequence(tmp_path, monkeypatch, changes):
    rd, store, client, service = _client(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    path, row = _record(rd, generation, "rejected", **changes)
    def forbidden(*args, **kwargs):
        pytest.fail("receipt repair reached a mutation path")
    for name in ("sequence", "_start_worker", "_save", "_read_existing"):
        monkeypatch.setattr(service, name, forbidden)
    before = path.read_bytes()
    result = client.get("/api/runs/demo/command-receipt", params={
        "expected_generation": generation, "command_id": row["id"]},
        headers={"X-LoopLab-Token": "agent-secret"})
    assert result.status_code == 503, result.text
    assert path.read_bytes() == before
