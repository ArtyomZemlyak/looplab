"""Durability and at-most-once contracts for the paid Concept lens endpoint."""
from __future__ import annotations

import hashlib
import hmac
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


def _idempotency_key(label):
    return f"test-receipt::{label}::0123456789abcdef"


def _post(client, key, prompt="group by usage"):
    return client.post(
        "/api/runs/demo/concepts/lens",
        json=_lens_body(client, prompt),
        headers={"Idempotency-Key": _idempotency_key(key)},
    )


def _abandon(client, key, generation, request_id):
    return client.post(
        "/api/runs/demo/concepts/lens/abandon",
        json={"expected_generation": generation, "request_id": request_id},
        headers={"Idempotency-Key": _idempotency_key(key)},
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


class _ImmediateLensClient:
    def __init__(self):
        self.accountant = CostAccountant()

    def complete_tool(self, _messages, _schema):
        self.accountant.add(0.1, {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        })
        return {"name": "Usage", "label": "By usage", "rels": ["uses"]}

    def complete_text(self, _messages):
        return "unused"


def _write_derived_terminal(run_dir, key, generation, *, root):
    from looplab.serve.routers.runs import (
        _concept_lens_identity,
        _concept_lens_prompt_digest,
    )

    prompt = "group by usage"
    opaque_key = _idempotency_key(key)
    identity = _concept_lens_identity(run_dir, generation, opaque_key)
    digest = _concept_lens_prompt_digest(opaque_key, prompt)
    store = EventStore(run_dir / "events.jsonl")
    store.append(EV_CONCEPT_LENS_STARTED, {
        "lens_request_id": identity,
        "generation": generation,
        "request_digest": digest,
        "input_seq": store.read_all()[-1].seq,
    })
    terminal = store.append(EV_CONCEPT_LENS_COMPLETED, {
        "lens_request_id": identity,
        "generation": generation,
        "request_digest": digest,
        "outcome": "derived",
        "spec": {
            "name": "usage",
            "label": "By usage",
            "rels": ["uses"],
            "kind": "edge",
            "provenance": "agent",
            "root": root,
        },
    })
    return identity, terminal


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
    # Shared job terminals are retained for every observer; durable endpoint replay remains the
    # restart boundary, while one browser poll cannot make another browser see `unknown`.
    second_observer = client.get(f"/api/jobs/{first.json()['job_id']}").json()
    assert second_observer["status"] == "done"
    assert second_observer["seq"] == done["seq"]
    events = EventStore(run_dir / "events.jsonl").read_all()
    assert sum(event.type == EV_CONCEPT_LENS_STARTED for event in events) == 1
    assert sum(event.type == EV_CONCEPT_LENS_COMPLETED for event in events) == 1
    assert sum(event.type == EV_LLM_USAGE for event in events) == 1
    claim = next(event for event in events if event.type == EV_CONCEPT_LENS_STARTED)
    assert claim.data["lens_request_id"] != "stable-key"
    assert "stable-key" not in str(claim.data) and "group by usage" not in str(claim.data)
    assert claim.data["request_digest"] != hashlib.sha256(b"group by usage").hexdigest()
    assert claim.data["request_digest"] == hmac.new(
        _idempotency_key("stable-key").encode("ascii"), b"group by usage", hashlib.sha256
    ).hexdigest()

    def forbidden_factory(*_args, **_kwargs):
        raise AssertionError("durable terminal replay must not rebuild a provider client")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", forbidden_factory)
    replay = _post(client, "stable-key")
    assert replay.status_code == 200 and replay.json()["ok"] is True
    assert replay.json()["request_id"] == first.json()["request_id"]
    assert replay.json()["seq"] == done["seq"]


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


def test_abandon_fences_live_worker_and_wins_late_cross_process_terminal_race(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")
    run_dir = _seed_run(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def factory(*_args, **_kwargs):
        calls.append(True)
        return _BlockingLensClient(started, release)

    monkeypatch.setattr("looplab.serve.server.make_llm_client", factory)
    original = TestClient(make_app(tmp_path))
    first = _post(original, "late-worker-race")
    payload = first.json()
    assert payload["status"] == "running" and started.wait(3)

    live = _abandon(
        original, "late-worker-race", payload["generation"], payload["request_id"])
    assert live.status_code == 409
    assert live.json()["detail"]["code"] == "concept_lens_still_running"

    # A restarted server cannot see the old process receipt. The operator may explicitly resolve its
    # orphan, while the cross-process sequencer still prevents the old worker from committing second.
    restarted = TestClient(make_app(tmp_path))
    orphan = _post(restarted, "late-worker-race")
    assert orphan.json()["code"] == "concept_lens_uncertain"
    abandoned = _abandon(
        restarted, "late-worker-race", payload["generation"], payload["request_id"])
    assert abandoned.status_code == 200
    assert abandoned.json()["reason"] == "operator_abandoned"

    release.set()
    late_result = _wait_job(original, payload["job_id"])
    assert late_result["code"] == "concept_lens_abandoned"
    assert late_result["seq"] == abandoned.json()["seq"]
    assert len(calls) == 1

    events = EventStore(run_dir / "events.jsonl").read_all()
    terminals = [event for event in events
                 if event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                 and event.data.get("lens_request_id") == payload["request_id"]]
    assert len(terminals) == 1
    assert terminals[0].data["outcome"] == "abandoned"
    assert sum(event.type == EV_LLM_USAGE for event in events) == 1


def test_provider_failure_after_dispatch_stays_unresolved_and_is_never_rebilled(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    calls = []
    provider_calls = []

    class FailingClient:
        def __init__(self):
            self.accountant = CostAccountant()

        def complete_tool(self, _messages, _schema):
            provider_calls.append("tool")
            self.accountant.add(0.5, {
                "prompt_tokens": 8,
                "completion_tokens": 0,
                "total_tokens": 8,
            })
            raise RuntimeError("provider failure containing secret-token-123")

        def complete_text(self, _messages):
            provider_calls.append("text")
            raise AssertionError("paid lens parsing must never fall back to a second provider call")

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
    assert provider_calls == ["tool"]
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

    abandoned = _abandon(
        restarted, "ambiguous-key", failed.json()["generation"], failed.json()["request_id"])
    assert abandoned.status_code == 200
    receipt = abandoned.json()
    assert receipt["code"] == "concept_lens_abandoned"
    assert receipt["reason"] == "operator_abandoned"
    assert receipt["abandoned"] is True and receipt["resolved"] is True
    assert receipt["provider_outcome"] == receipt["billing_status"] == "unknown"
    assert "may already have completed and billed" in receipt["warning"]

    # Same-key replay is terminal and byte-identical in identity/sequence; abandonment never retries
    # the provider. A fresh, intentional key is unlocked and may create new paid work.
    same_terminal = _post(restarted, "ambiguous-key")
    assert same_terminal.json()["code"] == "concept_lens_abandoned"
    assert same_terminal.json()["seq"] == receipt["seq"]
    assert len(calls) == 1
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client", lambda *_args, **_kwargs: _ImmediateLensClient())
    fresh = _post(restarted, "fresh-after-abandon")
    assert fresh.status_code == 200 and fresh.json()["ok"] is True

    terminals = [event for event in EventStore(tmp_path / "demo" / "events.jsonl").read_all()
                 if event.type == EV_CONCEPT_LENS_COMPLETED
                 and event.data.get("lens_request_id") == receipt["request_id"]]
    assert len(terminals) == 1
    assert terminals[0].data["outcome"] == "abandoned"
    assert terminals[0].seq == receipt["seq"]


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


def test_legacy_string_root_terminal_replays_after_consolidation_with_original_seq(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    identity, terminal = _write_derived_terminal(
        run_dir, "legacy-root", generation, root="agents/orchestrator")
    EventStore(run_dir / "events.jsonl").append(
        "concept_consolidation", {"rename": {"agents/orchestrator": "agents/controller"}})

    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy terminal replay must not call the provider")),
    )
    replay = _post(TestClient(make_app(tmp_path)), "legacy-root")
    payload = replay.json()
    assert replay.status_code == 200 and payload["ok"] is True
    assert payload["request_id"] == identity and payload["seq"] == terminal.seq
    assert "root" not in payload["spec"]
    assert "agents/controller" in payload["tree"]["nodes"]


@pytest.mark.parametrize("malformed_root", [
    ["agents/orchestrator"],
    {"concept": "agents/orchestrator"},
])
def test_malformed_durable_root_fails_closed_without_500(
        tmp_path, monkeypatch, malformed_root):
    run_dir = _seed_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    identity, terminal = _write_derived_terminal(
        run_dir, "malformed-root", generation, root=malformed_root)
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("malformed terminal replay must not call the provider")),
    )

    replay = _post(TestClient(make_app(tmp_path)), "malformed-root")
    payload = replay.json()
    assert replay.status_code == 200
    assert payload["code"] == "concept_lens_uncertain" and payload["ambiguous"] is True
    assert payload["request_id"] == identity and payload["seq"] == terminal.seq


def test_paid_lens_idempotency_key_must_be_bounded_high_entropy_receipt(tmp_path, monkeypatch):
    _seed_run(tmp_path)
    calls = []
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: calls.append(True) or _ImmediateLensClient())
    client = TestClient(make_app(tmp_path))
    response = client.post(
        "/api/runs/demo/concepts/lens",
        json=_lens_body(client),
        headers={"Idempotency-Key": "guessable"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "concept_lens_idempotency_key_invalid"
    assert calls == []
    assert not any(event.type == EV_CONCEPT_LENS_STARTED
                   for event in EventStore(tmp_path / "demo" / "events.jsonl").read_all())


def test_unrelated_ledger_conflict_is_pre_dispatch_409_without_fake_receipt(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    store = EventStore(run_dir / "events.jsonl")
    foreign = "a" * 64
    for _ in range(2):
        store.append(EV_CONCEPT_LENS_STARTED, {
            "lens_request_id": foreign,
            "generation": generation,
            "request_digest": "b" * 64,
            "input_seq": 0,
        })
    calls = []
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: calls.append(True) or _ImmediateLensClient())

    response = _post(client, "fresh-beside-conflict")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "concept_lens_ledger_conflict"
    assert "request_id" not in detail and "ambiguous" not in detail
    assert calls == []
    assert sum(event.type == EV_CONCEPT_LENS_STARTED for event in store.read_all()) == 2


def test_job_capacity_is_full_pre_dispatch_terminal_without_durable_claim(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    app = make_app(tmp_path)
    client = TestClient(app)
    calls = []
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: calls.append(True) or _ImmediateLensClient())
    monkeypatch.setattr(app.state.looplab.jobs, "reserve", lambda *_args, **_kwargs: {
        "ok": False,
        "code": "job_capacity",
        "error_kind": "capacity",
        "error": "background job capacity is temporarily full; retry later",
    })

    response = _post(client, "capacity-before-claim")
    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is False and payload["code"] == "job_capacity"
    assert payload["reason"] == payload["error_kind"] == "capacity"
    assert payload["schema"] == 1 and payload["generation"] and payload["request_id"]
    assert calls == []
    assert not any(event.type == EV_CONCEPT_LENS_STARTED
                   for event in EventStore(run_dir / "events.jsonl").read_all())
