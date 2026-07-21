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


def _recover(client, generation, *, headers=None):
    return client.get(
        "/api/runs/demo/concepts/lens/recovery",
        params={"expected_generation": generation},
        headers=headers,
    )


def _recovery_abandon(client, generation, request_id, started_seq,
                      resolution="recover-resolution-a", *, headers=None):
    combined_headers = {
        "Resolution-Idempotency-Key": _idempotency_key(resolution),
        **(headers or {}),
    }
    return client.post(
        "/api/runs/demo/concepts/lens/recovery/abandon",
        json={
            "expected_generation": generation,
            "request_id": request_id,
            "expected_started_seq": started_seq,
        },
        headers=combined_headers,
    )


def _write_orphan_claim(run_dir, key, generation, *, prompt="private recovery prompt"):
    from looplab.serve.routers.runs import (
        _concept_lens_identity,
        _concept_lens_prompt_digest,
    )

    opaque_key = _idempotency_key(key)
    request_id = _concept_lens_identity(run_dir, generation, opaque_key)
    request_digest = _concept_lens_prompt_digest(opaque_key, prompt)
    store = EventStore(run_dir / "events.jsonl")
    started = store.append(EV_CONCEPT_LENS_STARTED, {
        "lens_request_id": request_id,
        "generation": generation,
        "request_digest": request_digest,
        "input_seq": store.read_all()[-1].seq,
    })
    return request_id, request_digest, started


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
    # CODEX AGENT: the endpoint is allowed to outlive its bounded inline wait. Force that public async
    # contract here and observe the job receipt; assuming a fast provider failure always completes
    # inline made the at-most-once test depend on CI scheduler latency.
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")
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
    initial = failed.json()
    assert initial["status"] == "running"
    failure = _wait_job(client, initial["job_id"])
    assert failure["code"] == "concept_lens_uncertain"
    assert failure["ambiguous"] is True
    assert "secret-token-123" not in failed.text and "secret-token-123" not in str(failure)
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
        restarted, "ambiguous-key", failure["generation"], failure["request_id"])
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
    assert fresh.status_code == 200 and fresh.json()["status"] == "running"
    assert _wait_job(restarted, fresh.json()["job_id"])["ok"] is True

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


def test_lost_receipt_discovers_orphan_and_resolution_replays_without_secret_or_provider(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must never construct a provider client")),
    )
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]

    empty = _recover(client, generation)
    assert empty.status_code == 200
    assert empty.json() == {"schema": 1, "generation": generation, "state": "none"}

    prompt = "private recovery prompt containing token-secret-123"
    request_id, digest, started = _write_orphan_claim(
        run_dir, "lost-browser-receipt", generation, prompt=prompt)
    discovered = _recover(client, generation)
    payload = discovered.json()
    assert discovered.status_code == 200
    assert payload == {
        "schema": 1,
        "generation": generation,
        "state": "orphaned",
        "request_id": request_id,
        "started_seq": started.seq,
        "input_seq": started.data["input_seq"],
    }
    assert discovered.headers["Cache-Control"] == "no-store"
    assert {"Authorization", "X-LoopLab-Token"}.issubset(
        {item.strip() for item in discovered.headers["Vary"].split(",")})
    for secret in (prompt, "token-secret-123", digest, _idempotency_key("lost-browser-receipt")):
        assert secret not in discovered.text

    resolved = _recovery_abandon(
        client, generation, request_id, started.seq, "first-resolution")
    receipt = resolved.json()
    assert resolved.status_code == 200
    assert receipt["code"] == "concept_lens_abandoned"
    assert receipt["reason"] == "operator_recovered_abandon"
    assert receipt["provider_outcome"] == receipt["billing_status"] == "unknown"
    assert receipt["request_id"] == request_id
    assert "may already have completed and billed" in receipt["warning"]
    assert "Resolution-Idempotency-Key" in resolved.headers["Vary"]

    same_resolution = _recovery_abandon(
        client, generation, request_id, started.seq, "first-resolution")
    other_resolution = _recovery_abandon(
        client, generation, request_id, started.seq, "different-resolution")
    assert same_resolution.json()["seq"] == receipt["seq"]
    assert other_resolution.json()["seq"] == receipt["seq"]
    terminal_discovery = _recover(client, generation).json()
    assert terminal_discovery["state"] == "terminal"
    assert terminal_discovery["request_id"] == request_id
    assert terminal_discovery["started_seq"] == started.seq
    assert terminal_discovery["terminal"]["seq"] == receipt["seq"]
    assert terminal_discovery["terminal"]["reason"] == "operator_recovered_abandon"

    events = EventStore(run_dir / "events.jsonl").read_all()
    terminals = [event for event in events
                 if event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                 and event.data.get("lens_request_id") == request_id]
    assert len(terminals) == 1
    assert terminals[0].data["reason"] == "operator_recovered_abandon"
    assert terminals[0].data["resolution"] == "operator_recovery"
    assert len(terminals[0].data["resolution_id"]) == 64
    assert _idempotency_key("first-resolution") not in str(terminals[0].data)
    assert sum(event.type == EV_LLM_USAGE for event in events) == 0


def test_recovery_rejects_generation_request_seq_and_overlapping_claims(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    monkeypatch.setattr(
        "looplab.serve.server.make_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid recovery must remain provider-free")),
    )
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    request_id, _digest, started = _write_orphan_claim(
        run_dir, "strict-recovery", generation)
    other_generation = ("f" if generation != "f" * 64 else "e") * 64

    invalid_generation = client.get(
        "/api/runs/demo/concepts/lens/recovery",
        params={"expected_generation": "not-a-generation"},
    )
    assert invalid_generation.status_code == 400
    assert invalid_generation.json()["detail"]["code"] == "invalid_run_generation"
    stale = _recover(client, other_generation)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "run_generation_changed"
    wrong_request = _recovery_abandon(
        client, generation, "a" * 64, started.seq, "wrong-request-resolution")
    assert wrong_request.status_code == 409
    assert wrong_request.json()["detail"]["code"] == "concept_lens_recovery_claim_missing"
    wrong_seq = _recovery_abandon(
        client, generation, request_id, started.seq + 1, "wrong-seq-resolution")
    assert wrong_seq.status_code == 409
    assert wrong_seq.json()["detail"]["code"] == "concept_lens_started_seq_mismatch"
    paid_key_is_not_a_resolution_key = client.post(
        "/api/runs/demo/concepts/lens/recovery/abandon",
        json={
            "expected_generation": generation,
            "request_id": request_id,
            "expected_started_seq": started.seq,
        },
        headers={"Idempotency-Key": _idempotency_key("strict-recovery")},
    )
    assert paid_key_is_not_a_resolution_key.status_code == 400
    assert (paid_key_is_not_a_resolution_key.json()["detail"]["code"]
            == "concept_lens_resolution_key_invalid")
    bool_seq = client.post(
        "/api/runs/demo/concepts/lens/recovery/abandon",
        json={
            "expected_generation": generation,
            "request_id": request_id,
            "expected_started_seq": True,
        },
        headers={"Resolution-Idempotency-Key": _idempotency_key("bool-seq")},
    )
    assert bool_seq.status_code == 400
    assert bool_seq.json()["detail"]["code"] == "concept_lens_started_seq_invalid"

    _write_orphan_claim(run_dir, "overlapping-recovery", generation)
    conflicted = _recover(client, generation)
    assert conflicted.status_code == 200
    assert conflicted.json()["state"] == "conflict"
    assert "request_id" not in conflicted.json()
    rejected = _recovery_abandon(
        client, generation, request_id, started.seq, "conflict-resolution")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "concept_lens_recovery_conflict"
    events = EventStore(run_dir / "events.jsonl").read_all()
    assert not any(event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                   for event in events)
    assert sum(event.type == EV_LLM_USAGE for event in events) == 0


def test_recovery_fences_live_worker_then_wins_late_cross_process_terminal(
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
    first = _post(original, "recovery-late-worker")
    paid = first.json()
    assert paid["status"] == "running" and started.wait(3)

    live = _recover(original, paid["generation"])
    live_payload = live.json()
    assert live_payload["state"] == "running"
    assert live_payload["status"] == "running"
    assert live_payload["job_id"] == paid["job_id"]
    fenced = _recovery_abandon(
        original, paid["generation"], paid["request_id"], live_payload["started_seq"],
        "live-worker-resolution")
    assert fenced.status_code == 409
    assert fenced.json()["detail"]["code"] == "concept_lens_still_running"

    restarted = TestClient(make_app(tmp_path))
    orphan = _recover(restarted, paid["generation"]).json()
    assert orphan["state"] == "orphaned"
    resolved = _recovery_abandon(
        restarted, paid["generation"], paid["request_id"], orphan["started_seq"],
        "cross-process-resolution")
    assert resolved.status_code == 200
    assert resolved.json()["reason"] == "operator_recovered_abandon"

    release.set()
    late = _wait_job(original, paid["job_id"])
    assert late["code"] == "concept_lens_abandoned"
    assert late["reason"] == "operator_recovered_abandon"
    assert late["seq"] == resolved.json()["seq"]
    assert calls == [True]
    events = EventStore(run_dir / "events.jsonl").read_all()
    terminals = [event for event in events
                 if event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                 and event.data.get("lens_request_id") == paid["request_id"]]
    assert len(terminals) == 1
    assert sum(event.type == EV_LLM_USAGE for event in events) == 1


def test_recovery_projects_retained_done_job_for_fresh_polling(tmp_path):
    run_dir = _seed_run(tmp_path)
    app = make_app(tmp_path)
    client = TestClient(app)
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    request_id, _digest, started = _write_orphan_claim(
        run_dir, "retained-done-recovery", generation)
    reservation = app.state.looplab.jobs.reserve(request_id, consume_on_poll=False)
    app.state.looplab.jobs.put(
        reservation["job_id"],
        status="done",
        result={
            "ok": False,
            "code": "concept_lens_uncertain",
            "request_id": request_id,
        },
    )

    projection = _recover(client, generation)
    assert projection.status_code == 200
    assert projection.json() == {
        "schema": 1,
        "generation": generation,
        "state": "running",
        "request_id": request_id,
        "started_seq": started.seq,
        "input_seq": started.data["input_seq"],
        "job_id": reservation["job_id"],
        "status": "done",
    }
    polled = client.get(f"/api/jobs/{reservation['job_id']}").json()
    assert polled["status"] == "done"
    assert polled["code"] == "concept_lens_uncertain"


def test_recovery_partial_append_replay_returns_first_terminal_without_duplicate(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    request_id, _digest, started = _write_orphan_claim(
        run_dir, "partial-resolution", generation)
    real_append = EventStore.append
    injected = []

    def append_then_raise(self, event_type, data, *args, **kwargs):
        event = real_append(self, event_type, data, *args, **kwargs)
        if (event_type == EV_CONCEPT_LENS_COMPLETED
                and data.get("reason") == "operator_recovered_abandon"
                and not injected):
            injected.append(event.seq)
            raise OSError("simulated response loss after durable append")
        return event

    monkeypatch.setattr(EventStore, "append", append_then_raise)
    uncertain = _recovery_abandon(
        client, generation, request_id, started.seq, "partial-append-resolution")
    assert uncertain.status_code == 200
    assert uncertain.json()["code"] == "concept_lens_uncertain"
    assert uncertain.json()["request_id"] == request_id

    monkeypatch.setattr(EventStore, "append", real_append)
    replay = _recovery_abandon(
        client, generation, request_id, started.seq, "partial-append-resolution")
    competing_replay = _recovery_abandon(
        client, generation, request_id, started.seq, "different-after-partial")
    assert replay.json()["code"] == "concept_lens_abandoned"
    assert replay.json()["reason"] == "operator_recovered_abandon"
    assert replay.json()["seq"] == injected[0]
    assert competing_replay.json()["seq"] == injected[0]
    terminals = [event for event in EventStore(run_dir / "events.jsonl").read_all()
                 if event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                 and event.data.get("lens_request_id") == request_id]
    assert len(terminals) == 1
    assert terminals[0].seq == injected[0]


def test_recovery_strict_fold_fails_closed_on_duplicate_current_claim(tmp_path):
    run_dir = _seed_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts").json()["generation"]
    request_id, digest, started = _write_orphan_claim(
        run_dir, "duplicate-recovery", generation)
    EventStore(run_dir / "events.jsonl").append(EV_CONCEPT_LENS_STARTED, {
        "lens_request_id": request_id,
        "generation": generation,
        "request_digest": digest,
        "input_seq": started.data["input_seq"],
    })

    projection = _recover(client, generation)
    assert projection.status_code == 200
    assert projection.json()["state"] == "conflict"
    assert "request_id" not in projection.json()
    resolution = _recovery_abandon(
        client, generation, request_id, started.seq, "duplicate-claim-resolution")
    assert resolution.status_code == 409
    assert resolution.json()["detail"]["code"] == "concept_lens_recovery_conflict"
    assert not any(event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                   for event in EventStore(run_dir / "events.jsonl").read_all())


def test_recovery_is_owner_only_and_review_capability_cannot_translate(
        tmp_path, monkeypatch):
    run_dir = _seed_run(tmp_path)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    owner = {"X-LoopLab-Token": "owner-secret"}
    client = TestClient(make_app(tmp_path))
    generation = client.get("/api/runs/demo/concepts", headers=owner).json()["generation"]
    request_id, _digest, started = _write_orphan_claim(
        run_dir, "owner-only-recovery", generation)
    created = client.post(
        "/api/runs/demo/reviews",
        headers=owner,
        json={"ttl_seconds": 3600, "include_evidence": False},
    )
    assert created.status_code == 200
    review = {"X-LoopLab-Review": created.json()["token"]}

    assert _recover(client, generation).status_code == 401
    owner_read = _recover(client, generation, headers=owner)
    assert owner_read.status_code == 200 and owner_read.json()["state"] == "orphaned"
    assert owner_read.headers["Cache-Control"] == "no-store"
    vary = {item.strip() for item in owner_read.headers["Vary"].split(",")}
    assert {"Authorization", "X-LoopLab-Token"}.issubset(vary)
    review_read = _recover(client, generation, headers=review)
    assert review_read.status_code == 403
    assert review_read.json()["kind"] == "review_read_only"
    review_write = _recovery_abandon(
        client, generation, request_id, started.seq, "review-resolution", headers=review)
    assert review_write.status_code == 403
    assert review_write.json()["kind"] == "review_read_only"
    assert not any(event.type in {EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}
                   for event in EventStore(run_dir / "events.jsonl").read_all())
