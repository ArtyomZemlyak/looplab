"""Durable, at-most-once Genesis startup and lost-response observation."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _task() -> dict:
    return {"benchmark": "quadratic", "goal": "minimize", "direction": "min"}


def _validated(client: TestClient, run_id: str, **extra) -> dict:
    base = {"run_id": run_id, "task": _task(), **extra}
    token = client.post("/api/start/preflight", json=base).json()["validation_token"]
    return {**base, "validation_token": token}


def _event_line(seq: int, event_type: str, data: dict, *, ts: float = 1.0,
                version: int = 1, newline: bool = True) -> bytes:
    raw = json.dumps({
        "v": version, "seq": seq, "ts": ts, "type": event_type, "data": data,
    }).encode("utf-8")
    return raw + (b"\n" if newline else b"")


def _finish_spawn(calls: list, args, kwargs):
    calls.append((args, kwargs))
    run_dir = Path(kwargs["run_dir"])
    (run_dir / "events.jsonl").write_bytes(
        _event_line(0, "run_started", {"run_id": run_dir.name}))
    return None


def test_same_key_lost_response_replay_is_one_spawn_even_after_inputs_drift(tmp_path, monkeypatch):
    source = tmp_path / "task.json"
    source.write_text(json.dumps(_task()), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn(calls, args, kwargs),
    )
    base = {"run_id": "durable", "task_file": str(source),
            "chat": [{"role": "user", "content": "/new durable"}]}
    token = client.post("/api/start/preflight", json=base).json()["validation_token"]
    payload = {**base, "validation_token": token, "idempotency_key": "launch-key-1"}

    first = client.post("/api/start", json=payload)
    assert first.status_code == 200 and first.json()["status"] == "succeeded"
    assert first.json()["started"] is True  # the fixture wrote durable run_started evidence
    start_id = first.json()["start_id"]
    chat_before = (tmp_path / "durable" / "chat.jsonl").read_bytes()

    # The accepted result wins before preflight rereads either mutable source/default.
    source.unlink()
    (tmp_path / "ui_settings.json").write_text('{"max_nodes": 99}', encoding="utf-8")
    replay = client.post("/api/start", json=payload)
    assert replay.status_code == 200
    assert replay.json()["start_id"] == start_id
    assert replay.json()["status"] == "succeeded"
    assert len(calls) == 1
    assert (tmp_path / "durable" / "chat.jsonl").read_bytes() == chat_before


def test_same_key_different_effect_is_rejected_and_different_key_cannot_steal_run(
        tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn(calls, args, kwargs),
    )
    payload = {**_validated(client, "owned"), "idempotency_key": "same-key"}
    assert client.post("/api/start", json=payload).status_code == 200

    changed = client.post("/api/start", json={
        **payload, "chat": [{"role": "user", "content": "different effect"}],
    })
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "idempotency_key_reused"

    other = client.post("/api/start", json={**payload, "idempotency_key": "other-key"})
    assert other.status_code == 409
    assert other.json()["detail"]["code"] == "run_id_conflict"
    assert len(calls) == 1


def test_status_is_exact_observation_no_store_and_never_retries_uncertain_popen(
        tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    calls = []

    def pidless_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", pidless_spawn)
    payload = {**_validated(client, "unknown"), "idempotency_key": "unknown-key"}
    first = client.post("/api/start", json=payload)
    assert first.status_code == 200 and first.json()["status"] == "uncertain"

    status = client.get(
        "/api/start/unknown/status", headers={"Idempotency-Key": "unknown-key"})
    assert status.status_code == 200
    assert status.json()["status"] == "uncertain"
    assert status.json()["started"] is False
    assert status.json()["paid_effect_unknown"] is True
    assert status.headers["cache-control"] == "no-store"
    assert "X-LoopLab-Token" in status.headers["vary"]
    assert "Idempotency-Key" in status.headers["vary"]

    replay = client.post("/api/start", json=payload)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "start_uncertain"
    assert len(calls) == 1
    assert client.get("/api/start/unknown/status?idempotency_key=wrong").status_code == 404


def test_exception_after_popen_boundary_is_uncertain_and_no_key_can_spawn_again(
        tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    calls = []

    def ambiguous_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("injected failure after entering spawn helper")

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", ambiguous_spawn)
    payload = {**_validated(client, "boundary"), "idempotency_key": "boundary-key"}

    with pytest.raises(RuntimeError, match="injected failure"):
        client.post("/api/start", json=payload)

    rd = tmp_path / "boundary"
    record = client.app.state.looplab.commands.load_start_record(rd)
    assert record["status"] == "uncertain"
    assert record["phase"] == "failed_after_spawn"
    assert record["paid_effect_unknown"] is True
    assert client.app.state.looplab.commands._spawn_claim_path(rd).exists()

    replay = client.post("/api/start", json=payload)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "start_uncertain"
    other_key = client.post("/api/start", json={**payload, "idempotency_key": "other-key"})
    assert other_key.status_code == 409
    assert other_key.json()["detail"]["code"] == "run_id_conflict"
    assert len(calls) == 1


@pytest.mark.parametrize("event_bytes", [
    _event_line(0, "node_started", {"run_id": "bad-events"}),
    b"not-json\n" + _event_line(1, "run_started", {"run_id": "bad-events"}),
    b'{"type":"run_started","seq":0}\n',
    _event_line(0, "run_started", {"run_id": "another-run"}),
    _event_line(1, "run_started", {"run_id": "bad-events"}),
    _event_line(0, "run_started", {"run_id": "bad-events"}, version=2),
    _event_line(0, "run_started", {"run_id": "bad-events"}, ts=0),
    _event_line(0, "run_started", {"run_id": "bad-events"}, newline=False),
    _event_line(0, "setup_started", {})
    + _event_line(2, "run_started", {"run_id": "bad-events"}),
])
def test_status_requires_valid_correlated_first_run_started_event(
        tmp_path, monkeypatch, event_bytes):
    client = TestClient(make_app(tmp_path))

    def unproven_spawn(*_args, **kwargs):
        Path(kwargs["run_dir"], "events.jsonl").write_bytes(event_bytes)
        return None

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", unproven_spawn)
    payload = {**_validated(client, "bad-events"), "idempotency_key": "event-key"}
    assert client.post("/api/start", json=payload).json()["status"] == "uncertain"

    status = client.get(
        "/api/start/bad-events/status", headers={"Idempotency-Key": "event-key"})
    assert status.status_code == 200
    assert status.json()["status"] == "uncertain"
    assert status.json()["started"] is False
    assert status.json()["paid_effect_unknown"] is True


def test_current_setup_prelude_and_legacy_direct_anchor_are_both_positive(
        tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    event_layouts = {
        "current-layout": (
            _event_line(0, "setup_started", {"phase": "task+data"})
            + _event_line(1, "setup_step", {"step": "workspace fingerprint"})
            + _event_line(2, "run_started", {"run_id": "current-layout"})
        ),
        "legacy-layout": _event_line(
            0, "run_started", {"run_id": "legacy-layout"}),
    }

    def proven_spawn(*_args, **kwargs):
        run_dir = Path(kwargs["run_dir"])
        (run_dir / "events.jsonl").write_bytes(event_layouts[run_dir.name])
        return None

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", proven_spawn)
    for run_id in event_layouts:
        payload = {**_validated(client, run_id), "idempotency_key": f"{run_id}-key"}
        started = client.post("/api/start", json=payload)
        assert started.status_code == 200
        assert started.json()["status"] == "succeeded"
        assert started.json()["started"] is True


def test_start_observation_expands_atomic_setup_and_identity_batch(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))

    def proven_spawn(*_args, **kwargs):
        run_dir = Path(kwargs["run_dir"])
        EventStore(run_dir / "events.jsonl").append_many([
            ("setup_started", {"phase": "task+data"}),
            ("setup_step", {"step": "workspace fingerprint"}),
            ("run_started", {"run_id": run_dir.name}),
        ])
        return None

    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", proven_spawn)
    payload = {**_validated(client, "batch-layout"), "idempotency_key": "batch-layout-key"}

    started = client.post("/api/start", json=payload)

    assert started.status_code == 200
    assert started.json()["status"] == "succeeded"
    assert started.json()["started"] is True


@pytest.mark.parametrize("reconcile_as_stale_reservation", [False, True])
def test_fresh_key_can_replace_safe_pre_popen_failure_exactly_once(
        tmp_path, monkeypatch, reconcile_as_stale_reservation):
    from looplab.serve.routers import control as control_router

    app = make_app(tmp_path)
    client = TestClient(app)
    payload = {**_validated(client, "safe-retry"), "idempotency_key": "failed-key"}
    original_write = control_router.atomic_write_text
    fail_task_write = True

    def fail_first_task_write(path, content):
        nonlocal fail_task_write
        if fail_task_write and Path(path).name == "task.input.json":
            fail_task_write = False
            raise OSError("injected failure before Popen")
        return original_write(path, content)

    monkeypatch.setattr(control_router, "atomic_write_text", fail_first_task_write)
    with pytest.raises(OSError, match="before Popen"):
        client.post("/api/start", json=payload)

    rd = tmp_path / "safe-retry"
    failed_record = app.state.looplab.commands.load_start_record(rd)
    assert failed_record["status"] == "failed"
    assert failed_record["phase"] == "failed_before_spawn"
    assert failed_record["paid_effect_unknown"] is False
    assert not app.state.looplab.commands._spawn_claim_path(rd).exists()
    old_start_id = failed_record["id"]
    if reconcile_as_stale_reservation:
        failed_record.update(status="preparing", phase="reserved", error_code=None)
        app.state.looplab.commands.save_start_record(rd, failed_record)

    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn(calls, args, kwargs),
    )
    retry = client.post("/api/start", json={**payload, "idempotency_key": "fresh-key"})
    assert retry.status_code == 200
    assert retry.json()["status"] == "succeeded"
    assert retry.json()["start_id"] != old_start_id
    assert len(calls) == 1

    replay = client.post("/api/start", json={**payload, "idempotency_key": "fresh-key"})
    assert replay.status_code == 200
    assert replay.json()["start_id"] == retry.json()["start_id"]
    assert len(calls) == 1


def test_stable_status_poll_does_not_rewrite_start_sidecar(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = TestClient(app)
    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn(calls, args, kwargs),
    )
    payload = {**_validated(client, "stable"), "idempotency_key": "stable-key"}
    assert client.post("/api/start", json=payload).status_code == 200
    headers = {"Idempotency-Key": "stable-key"}
    assert client.get("/api/start/stable/status", headers=headers).json()["status"] == "succeeded"

    rd = tmp_path / "stable"
    before = app.state.looplab.commands._start_record_path(rd).read_bytes()
    saved = []
    original = app.state.looplab.commands.save_start_record

    def tracked_save(*args, **kwargs):
        saved.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.looplab.commands, "save_start_record", tracked_save)
    second = client.get("/api/start/stable/status", headers=headers)
    assert second.status_code == 200 and second.json()["status"] == "succeeded"
    assert saved == []
    assert app.state.looplab.commands._start_record_path(rd).read_bytes() == before


def test_unrelated_engine_lock_without_matching_start_meta_is_never_positive(
        tmp_path, monkeypatch):
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", lambda *_args, **_kwargs: None)
    payload = {**_validated(client, "replaced"), "idempotency_key": "original-key"}
    started = client.post("/api/start", json=payload).json()
    rd = tmp_path / "replaced"
    srv.commands.cancel_external_spawn(rd, f"start:{started['start_id']}")
    (rd / "ui_meta.json").write_text(json.dumps({
        "task_file": str(rd / "task.input.json"),
        "start_id": "start_unrelated_incarnation",
    }), encoding="utf-8")
    monkeypatch.setattr("looplab.serve.routers.control._engine_alive", lambda _rd: True)
    monkeypatch.setattr(srv.commands, "engine_alive", lambda _rd: True)

    status = client.get(
        "/api/start/replaced/status", headers={"Idempotency-Key": "original-key"})
    assert status.status_code == 200
    assert status.json()["status"] == "uncertain"
    assert status.json()["started"] is False
    assert status.json()["paid_effect_unknown"] is True


def test_start_status_header_is_owner_gated_no_store_and_mismatch_rejected(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-token")
    app = make_app(tmp_path)
    client = TestClient(app)
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn([], args, kwargs),
    )
    owner = {"X-LoopLab-Token": "owner-token"}
    base = {"run_id": "private-status", "task": _task()}
    token = client.post("/api/start/preflight", json=base, headers=owner).json()["validation_token"]
    payload = {**base, "validation_token": token, "idempotency_key": "private-key"}
    assert client.post("/api/start", json=payload, headers=owner).status_code == 200

    unauthorized = client.get(
        "/api/start/private-status/status", headers={"Idempotency-Key": "private-key"})
    assert unauthorized.status_code == 401
    assert unauthorized.headers["cache-control"] == "no-store"
    assert "Idempotency-Key" in unauthorized.headers["vary"]

    authorized = client.get("/api/start/private-status/status", headers={
        **owner, "Idempotency-Key": "private-key",
    })
    assert authorized.status_code == 200
    assert authorized.json()["status"] == "succeeded"
    assert authorized.headers["cache-control"] == "no-store"

    mismatch = client.get(
        "/api/start/private-status/status?idempotency_key=other-key",
        headers={**owner, "Idempotency-Key": "private-key"},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "idempotency_key_mismatch"


def test_concurrent_same_key_returns_one_start_identity_and_one_popen(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn(calls, args, kwargs),
    )
    payload = {**_validated(client, "concurrent"), "idempotency_key": "one-operation"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: client.post("/api/start", json=payload), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["start_id"] for response in responses}) == 1
    assert len(calls) == 1


def test_initial_response_uses_known_pid_as_positive_executing_evidence(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 4242,
    )
    monkeypatch.setattr(srv.commands, "process_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(srv.commands, "process_identity", lambda pid: "known-generation")
    payload = {**_validated(client, "known-child"), "idempotency_key": "known-key"}

    started = client.post("/api/start", json=payload)
    assert started.status_code == 200
    assert started.json()["status"] == "executing"
    assert started.json()["started"] is True
    assert started.json()["paid_effect_unknown"] is False
    assert len(calls) == 1


def test_keyed_start_requires_free_preflight_token_and_sidecar_contains_only_digests(
        tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    monkeypatch.setattr("looplab.serve.routers.control._spawn_engine", lambda *args, **kwargs: None)
    missing = client.post("/api/start", json={
        "run_id": "needs-validation", "task": _task(), "idempotency_key": "raw-secret-key",
    })
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "launch_validation_required"

    payload = {**_validated(client, "redacted", chat=[
        {"role": "user", "content": "private creation context"},
    ]), "idempotency_key": "raw-secret-key"}
    assert client.post("/api/start", json=payload).status_code == 200
    sidecars = list((tmp_path / ".command-locks").glob("*.start.json"))
    assert len(sidecars) == 1
    raw = sidecars[0].read_text(encoding="utf-8")
    assert "raw-secret-key" not in raw
    assert "private creation context" not in raw
    assert '"task"' not in raw
    record = json.loads(raw)
    assert len(record["idempotency_key_digest"]) == 64
    assert len(record["request_digest"]) == 64


def test_successful_delete_retires_start_identity_and_run_name_can_be_reused(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    calls = []
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn(calls, args, kwargs),
    )
    first_payload = {**_validated(client, "reusable"), "idempotency_key": "first-start"}
    first = client.post("/api/start", json=first_payload).json()
    rd = tmp_path / "reusable"
    srv.commands.cancel_external_spawn(rd, f"start:{first['start_id']}")
    assert srv.commands.load_start_record(rd)["id"] == first["start_id"]

    deleted = client.delete("/api/runs/reusable")
    assert deleted.status_code == 200
    assert not rd.exists()
    assert srv.commands.load_start_record(rd) is None

    second_payload = {**_validated(client, "reusable"), "idempotency_key": "second-start"}
    second = client.post("/api/start", json=second_payload)
    assert second.status_code == 200
    assert second.json()["start_id"] != first["start_id"]
    assert len(calls) == 2


def test_delete_sidecar_retirement_failure_leaves_run_intact(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn([], args, kwargs),
    )
    payload = {**_validated(client, "retire-denied"), "idempotency_key": "retire-key"}
    started = client.post("/api/start", json=payload).json()
    rd = tmp_path / "retire-denied"
    srv.commands.cancel_external_spawn(rd, f"start:{started['start_id']}")
    monkeypatch.setattr(srv.commands, "retire_start_record", lambda *_args, **_kwargs: False)

    deleted = client.delete("/api/runs/retire-denied")
    assert deleted.status_code == 503
    assert rd.exists()
    assert (rd / "events.jsonl").exists()
    assert srv.commands.load_start_record(rd)["id"] == started["start_id"]


def test_partial_delete_restores_exact_start_record(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    monkeypatch.setattr(
        "looplab.serve.routers.control._spawn_engine",
        lambda args, **kwargs: _finish_spawn([], args, kwargs),
    )
    payload = {**_validated(client, "delete-rollback"), "idempotency_key": "rollback-key"}
    started = client.post("/api/start", json=payload).json()
    rd = tmp_path / "delete-rollback"
    srv.commands.cancel_external_spawn(rd, f"start:{started['start_id']}")
    record_before = srv.commands.load_start_record(rd)
    monkeypatch.setattr("shutil.rmtree", lambda *_args, **_kwargs: None)

    deleted = client.delete("/api/runs/delete-rollback")
    assert deleted.status_code == 500
    assert rd.exists()
    assert srv.commands.load_start_record(rd) == record_before
