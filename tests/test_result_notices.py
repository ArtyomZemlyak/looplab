"""Completion briefs are measured, replayable and cannot forge owner chat/actions."""
import json

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def _run(tmp_path, monkeypatch, external=True, run_fields=None):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-secret")
    rd = tmp_path / "demo"
    rd.mkdir()
    (rd / "config.snapshot.json").write_text(Settings(backend="toy", external_harness=external).model_dump_json())
    (rd / "task.snapshot.json").write_text('{"kind":"quadratic","goal":"g","direction":"min"}')
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "one", "task_id": "task", "goal": "g", "direction": "min", **(run_fields or {})})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": {"operator": "draft"}, "code": "print(1)"})
    client = TestClient(make_app(tmp_path))
    return rd, store, client, run_generation_token(store.read_all())


def _read(client, generation, **params):
    return client.get("/api/runs/demo/result-notices", params={"expected_generation": generation, **params},
                      headers={"X-LoopLab-Token": "agent-secret"})


def _body(receipt, generation):
    return {"expected_generation": generation, "receipt_id": receipt["id"],
            "evidence_token": receipt["evidence_token"], "action_id": "brief-1",
            "summary": "Измеренный результат получен; перед продолжением проверьте повторные seeds."}


def test_pending_node_has_no_brief_then_terminal_receipt_is_stable_and_read_only(tmp_path, monkeypatch):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    assert _read(client, gen).json()["items"] == []
    store.append("node_evaluated", {"node_id": 0, "metric": 0.25})
    service = client.app.state.looplab.commands
    def forbidden(*args, **kwargs):
        pytest.fail("result read tried to drive an engine or command")
    monkeypatch.setattr(service, "sequence", forbidden)
    monkeypatch.setattr(service, "_start_worker", forbidden)
    before = (rd / "events.jsonl").read_bytes()
    first = _read(client, gen)
    assert first.status_code == 200, first.text
    row = first.json()["items"][0]
    assert row["status"] == "evaluated" and row["score"] == 0.25
    assert row["confirmed_mean"] is None
    assert first.headers["cache-control"] == "no-store"
    assert _read(client, gen).json() == first.json()
    assert (rd / "events.jsonl").read_bytes() == before
    assert not (rd / "result_commentary.jsonl").exists()
    assert _read(client, "a" * 64).status_code == 409


def test_scoped_comment_is_idempotent_and_never_changes_scores_or_owner_chat(tmp_path, monkeypatch):
    rd, store, client, gen = _run(tmp_path, monkeypatch)
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    receipt = _read(client, gen).json()["items"][0]
    body = _body(receipt, gen)
    headers = {"X-LoopLab-Token": "agent-secret"}
    before = (rd / "events.jsonl").read_bytes()
    for replayed in (False, True):
        result = client.post("/api/runs/demo/result-notices", json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["replayed"] == replayed
    assert len((rd / "result_commentary.jsonl").read_text().splitlines()) == 1
    assert _read(client, gen).json()["items"][0]["commentary"] == body["summary"]
    assert (rd / "events.jsonl").read_bytes() == before and not (rd / "chat.jsonl").exists()
    assert client.post("/api/runs/demo/chat-log", json={"role": "action", "action": "resume"}, headers=headers).status_code == 403
    for mutation in ({"score": 99}, {"role": "assistant"}, {"action": "resume"}):
        assert client.post("/api/runs/demo/result-notices", json={**body, **mutation}, headers=headers).status_code == 422
    assert client.post("/api/runs/demo/result-notices", json={**body, "summary": "different"}, headers=headers).status_code == 409
    assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "duplicate"}, headers=headers).status_code == 409
    # Reconnect restores one brief, not a persisted copy on every poll.
    restarted = TestClient(make_app(tmp_path))
    assert _read(restarted, gen).json() == _read(client, gen).json()
    store.append("node_reset", {"node_id": 0})
    assert _read(client, gen).json()["items"] == []
    assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "late"}, headers=headers).status_code == 409
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.5})
    fresh = _read(client, gen).json()["items"][0]
    assert fresh["attempt"] == 1 and fresh["commentary"] is None


def test_failure_abort_parent_unknown_and_finalize_release(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve", "idea": {"operator": "improve"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.5, "violations": [{"kind": "constraint"}]})
    row = _read(client, gen).json()["items"][1]
    assert row["parents"][0]["comparability"] == "unknown" and not row["feasible"]
    store.append("node_created", {"node_id": 2, "operator": "draft", "idea": {"operator": "draft"}, "code": "x"})
    store.append("node_failed", {"node_id": 2, "error": "failed owner-secret", "reason": "crash"})
    row = _read(client, gen).json()["items"][-1]
    assert row["status"] == "failed" and row["score"] is None and "owner-secret" not in json.dumps(row)
    store.append("node_abort", {"node_id": 0})
    assert _read(client, gen).json()["items"][0]["score"] is None
    store.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: True)
    assert all(r["kind"] == "node" for r in _read(client, gen).json()["items"])
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: None)
    assert all(r["kind"] == "node" for r in _read(client, gen).json()["items"])
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    rows = _read(client, gen, limit=1).json()
    assert rows["has_more"] and rows["items"][0]["kind"] == "run"
    assert rows["items"][0]["selected_node"] is None
    monkeypatch.setattr("looplab.serve.result_notices.incomplete_finalize_scope", lambda events: {})
    assert all(r["kind"] == "node" for r in _read(client, gen).json()["items"])


def test_incomplete_sources_are_explicit_and_internal_runs_cannot_be_external_authored(tmp_path, monkeypatch):
    rd, store, client, gen = _run(tmp_path, monkeypatch, external=False)
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    body = _body(_read(client, gen).json()["items"][0], gen)
    assert client.post("/api/runs/demo/result-notices", json=body, headers={"X-LoopLab-Token": "agent-secret"}).status_code == 409
    (rd / "result_commentary.jsonl").write_text('{"broken":')
    assert _read(client, gen).status_code == 503
    (rd / "result_commentary.jsonl").unlink()
    with (rd / "events.jsonl").open("ab") as f:
        f.write(b'{"broken":')
    assert _read(client, gen).status_code == 503


def test_provenance_changes_with_same_score_withdraw_old_interpretation(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0, "metric_provenance": {}})
    before = _read(client, gen).json()["items"][0]
    body = _body(before, gen)
    headers = {"X-LoopLab-Token": "agent-secret"}
    assert client.post("/api/runs/demo/result-notices", json=body, headers=headers).status_code == 200
    store.append("applied_params_backfilled", {"node_id": 0, "generation": 0, "unrecoverable": "old workdir is gone"})
    after = _read(client, gen).json()["items"][0]
    assert after["score"] == before["score"]
    assert after["evidence_token"] != before["evidence_token"] and after["commentary"] is None
    assert client.post("/api/runs/demo/result-notices", json={**body, "action_id": "late"}, headers=headers).status_code == 409


def test_parallel_completion_order_and_parent_reset_cannot_rewrite_comparison(tmp_path, monkeypatch):
    _, store, client, gen = _run(tmp_path, monkeypatch)
    store.append("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}})
    store.append("node_evaluated", {"node_id": 1, "metric": 2.0})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    assert [r["node_id"] for r in _read(client, gen).json()["items"]] == [1, 0]
    store.append("node_created", {"node_id": 2, "parent_ids": [0], "operator": "improve", "idea": {"operator": "improve"}})
    store.append("node_evaluated", {"node_id": 2, "metric": 0.5})
    child = _read(client, gen).json()["items"][-1]
    assert child["parents"][0]["attempt"] == 0
    store.append("node_reset", {"node_id": 0})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.1})
    child = next(r for r in _read(client, gen).json()["items"] if r["node_id"] == 2)
    assert child["parents"] == []
