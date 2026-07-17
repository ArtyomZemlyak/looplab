"""Durability and at-most-once contracts for the paid Concept lens endpoint."""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.llm import CostAccountant  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_CONCEPT_LENS_COMPLETED,
    EV_CONCEPT_LENS_FAILED,
    EV_CONCEPT_LENS_STARTED,
    EV_LLM_USAGE,
)
from looplab.serve.server import make_app  # noqa: E402


def _seed_run(root):
    run_dir = root / "demo"
    run_dir.mkdir(parents=True)
    store = EventStore(run_dir / "events.jsonl")
    store.append(
        "run_started", {"run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
    store.append("node_created", {
        "node_id": 0,
        "parent_ids": [],
        "operator": "draft",
        "idea": {
            "operator": "draft",
            "params": {},
            "rationale": "r",
            "concepts": ["agents/orchestrator", "llm/gpt"],
        },
    })
    store.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    store.append("concept_edge", {"edges": [{
        "src": "agents/orchestrator",
        "rel": "uses",
        "dst": "llm/gpt",
        "confidence": 1.0,
        "provenance": "asserted",
    }]})
    return run_dir


def _lens_body(client, prompt="group by usage"):
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    return {"prompt": prompt, "expected_generation": generation}


def _post(client, key, prompt="group by usage"):
    return client.post(
        "/api/runs/demo/concepts/lens",
        json=_lens_body(client, prompt),
        headers={"Idempotency-Key": key},
    )


def _wait_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload.get("status") == "done":
            return payload
        time.sleep(0.02)
    raise AssertionError("background lens job did not complete")


class _BlockingLensClient:
    def __init__(self, started, release, *, failure=None):
        self.started = started
        self.release = release
        self.failure = failure
        self.accountant = CostAccountant()

    def complete_tool(self, _messages, _schema):
        self.accountant.add(0.25, {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 7,
        })
        self.started.set()
        assert self.release.wait(5), "test did not release the provider"
        if self.failure is not None:
            raise self.failure
        return {"name": "Usage", "label": "By usage", "rels": ["uses"]}

    def complete_text(self, _messages):
        return "unused"


def test_same_key_rejoins_one_paid_call_and_replays_durable_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")
    run_dir = _seed_run(tmp_path)
    started = threading.Event()
    release = threading.Event()
    factory_calls = []

    def factory(*_args, **_kwargs):
        factory_calls.append(True)
        return _BlockingLensClient(started, release)

    monkeypatch.setattr("looplab.serve.server.make_llm_client", factory)
    client = TestClient(make_app(tmp_path))
    first = _post(client, "stable-key")
    assert first.status_code == 200 and first.json()["status"] == "running"
    assert started.wait(3)

    same = _post(client, "stable-key")
    assert same.json()["job_id"] == first.json()["job_id"]
    reused = _post(client, "stable-key", "a different prompt")
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "idempotency_key_reused"
    competing = _post(client, "other-key")
    assert competing.status_code == 409
    assert competing.json()["detail"]["code"] == "concept_lens_in_progress"
    assert len(factory_calls) == 1

    release.set()
    done = _wait_job(client, first.json()["job_id"])
    assert done["ok"] is True and done["spec"]["rels"] == ["uses"]
    events = EventStore(run_dir / "events.jsonl").read_all()
    assert sum(event.type == EV_CONCEPT_LENS_STARTED for event in events) == 1
    assert sum(event.type == EV_CONCEPT_LENS_COMPLETED for event in events) == 1
    assert sum(event.type == EV_LLM_USAGE for event in events) == 1
    claim = next(event for event in events if event.type == EV_CONCEPT_LENS_STARTED)
    assert claim.data["lens_request_id"] != "stable-key"
    assert "stable-key" not in str(claim.data) and "group by usage" not in str(claim.data)

    def forbidden_factory(*_args, **_kwargs):
        raise AssertionError("durable terminal replay must not rebuild a provider client")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", forbidden_factory)
    replay = _post(client, "stable-key")
    assert replay.status_code == 200 and replay.json()["ok"] is True
    assert replay.json()["request_id"] == first.json()["request_id"]


def test_restart_orphan_is_uncertain_until_original_worker_publishes_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")
    _seed_run(tmp_path)
    started = threading.Event()
    release = threading.Event()
    factory_calls = []

    def factory(*_args, **_kwargs):
        factory_calls.append(True)
        return _BlockingLensClient(started, release)

    monkeypatch.setattr("looplab.serve.server.make_llm_client", factory)
    first_client = TestClient(make_app(tmp_path))
    first = _post(first_client, "restart-key")
    assert first.json()["status"] == "running" and started.wait(3)

    def forbidden_factory(*_args, **_kwargs):
        raise AssertionError("a restarted process must not repeat an unresolved paid call")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", forbidden_factory)
    restarted_client = TestClient(make_app(tmp_path))
    orphan = _post(restarted_client, "restart-key")
    assert orphan.status_code == 200
    assert orphan.json()["code"] == "concept_lens_uncertain"
    assert orphan.json()["ambiguous"] is True
    competing = _post(restarted_client, "new-key")
    assert competing.status_code == 409
    assert competing.json()["detail"]["code"] == "concept_lens_in_progress"
    assert len(factory_calls) == 1

    release.set()
    assert _wait_job(first_client, first.json()["job_id"])["ok"] is True
    replay = _post(restarted_client, "restart-key")
    assert replay.status_code == 200 and replay.json()["ok"] is True
    assert len(factory_calls) == 1


def test_provider_failure_after_dispatch_stays_unresolved_and_is_never_rebilled(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    calls = []

    class FailingClient:
        def __init__(self):
            self.accountant = CostAccountant()

        def complete_tool(self, _messages, _schema):
            self.accountant.add(0.5, {
                "prompt_tokens": 8,
                "completion_tokens": 0,
                "total_tokens": 8,
            })
            raise RuntimeError("provider failure containing secret-token-123")

    def factory(*_args, **_kwargs):
        calls.append(True)
        return FailingClient()

    monkeypatch.setattr("looplab.serve.server.make_llm_client", factory)
    client = TestClient(make_app(tmp_path))
    failed = _post(client, "ambiguous-key")
    assert failed.status_code == 200
    assert failed.json()["code"] == "concept_lens_uncertain"
    assert failed.json()["ambiguous"] is True
    assert "secret-token-123" not in failed.text
    events = EventStore(tmp_path / "demo" / "events.jsonl").read_all()
    assert sum(event.type == EV_CONCEPT_LENS_STARTED for event in events) == 1
    assert sum(event.type == EV_LLM_USAGE for event in events) == 1
    assert not any(event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                   for event in events)

    def forbidden_factory(*_args, **_kwargs):
        raise AssertionError("restart reconciliation must not dispatch again")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", forbidden_factory)
    restarted = TestClient(make_app(tmp_path))
    replay = _post(restarted, "ambiguous-key")
    assert replay.json()["code"] == "concept_lens_uncertain"
    assert _post(restarted, "different-key").status_code == 409
    assert len(calls) == 1


def test_client_construction_failure_gets_retry_safe_terminal_receipt(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    calls = []

    def failing_factory(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("api key is invalid: secret-token-123")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", failing_factory)
    client = TestClient(make_app(tmp_path))
    failed = _post(client, "pre-dispatch-key")
    assert failed.status_code == 200
    assert failed.json()["code"] == "concept_lens_failed"
    assert failed.json()["reason"] == "no_model"
    assert failed.json()["error_kind"] == "credentials"
    assert "secret-token-123" not in failed.text
    events = EventStore(tmp_path / "demo" / "events.jsonl").read_all()
    assert [event.type for event in events[-2:]] == [
        EV_CONCEPT_LENS_STARTED, EV_CONCEPT_LENS_FAILED]

    def forbidden_factory(*_args, **_kwargs):
        raise AssertionError("a failure terminal must replay without provider construction")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", forbidden_factory)
    replay = _post(TestClient(make_app(tmp_path)), "pre-dispatch-key")
    assert replay.status_code == 200 and replay.json()["code"] == "concept_lens_failed"
    assert len(calls) == 1


def test_unconfirmed_claim_never_dispatches_provider(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    import looplab.events.eventstore as eventstore_module

    real_fsync = eventstore_module.strict_fsync
    provider_calls = []

    def fail_claim_sync(_fd):
        raise OSError("simulated durable storage failure")

    def factory(*_args, **_kwargs):
        provider_calls.append(True)
        raise AssertionError("provider must not run after an unconfirmed claim")

    monkeypatch.setattr(eventstore_module, "strict_fsync", fail_claim_sync)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", factory)
    client = TestClient(make_app(tmp_path), raise_server_exceptions=False)
    failed = _post(client, "sync-failure-key")
    assert failed.status_code == 500
    assert provider_calls == []

    monkeypatch.setattr(eventstore_module, "strict_fsync", real_fsync)
    reconciled = _post(TestClient(make_app(tmp_path)), "sync-failure-key")
    assert reconciled.status_code == 200
    assert reconciled.json()["code"] == "concept_lens_uncertain"
    assert provider_calls == []
