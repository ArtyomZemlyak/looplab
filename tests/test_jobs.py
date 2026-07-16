from __future__ import annotations

import json
import threading
import time

import anyio

from looplab.serve.jobs import JobRegistry


def _run(registry: JobRegistry, compute, **kwargs):
    async def invoke():
        return await registry.run_as_job(compute, **kwargs)
    return anyio.run(invoke)


def test_capacity_rejoins_running_identity_and_never_evicts_paid_work(monkeypatch):
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0")
    registry = JobRegistry()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def paid_compute():
        calls.append("paid")
        entered.set()
        assert release.wait(5), "test did not release paid work"
        return {"ok": True, "value": "durable result"}

    first = _run(registry, paid_compute, inline_wait=0, idempotency_key="same-paid-work")
    assert first["status"] == "running" and entered.wait(3)
    for index in range(63):
        registry.put(
            f"occupied-{index}", status="running", result=None, ts=time.time())

    retry = _run(
        registry, lambda: calls.append("duplicate"), inline_wait=0,
        idempotency_key="same-paid-work")
    rejected = _run(
        registry, lambda: calls.append("overflow"), inline_wait=0,
        idempotency_key="different-paid-work")

    assert retry == first
    assert rejected == {
        "ok": False,
        "code": "job_capacity",
        "error_kind": "capacity",
        "error": "background job capacity is temporarily full; retry later",
    }
    assert calls == ["paid"]

    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        completed = registry.get(first["job_id"])
        if completed and completed["status"] == "done":
            break
        time.sleep(0.01)
    assert completed["result"] == {"ok": True, "value": "durable result"}

    # Admission evicts an old completed receipt before running work or the just-completed result.
    registry.put("occupied-0", status="done", result={"ok": True}, ts=0)
    admitted = _run(registry, lambda: {"ok": True, "value": "new"}, inline_wait=1)
    assert admitted == {"ok": True, "value": "new"}
    assert registry.get("occupied-0") is None
    assert registry.get(first["job_id"])["result"]["value"] == "durable result"


def test_uncaught_job_failure_never_exposes_raw_exception():
    registry = JobRegistry()
    secret = "https://user:openrouter-secret@provider.invalid?q=token-value"

    def explode():
        raise RuntimeError(secret)

    result = _run(registry, explode, inline_wait=1)
    encoded = json.dumps(result)

    assert result == {
        "ok": False,
        "code": "job_failed",
        "error_kind": "internal",
        "error": "background job failed",
    }
    assert "openrouter-secret" not in encoded
    assert "token-value" not in encoded


def test_worker_spawn_failure_is_terminal_and_redacted(monkeypatch):
    registry = JobRegistry()
    secret = "https://user:thread-secret@provider.invalid?token=hidden"

    def fail_start(_thread):
        raise RuntimeError(secret)

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    result = _run(
        registry, lambda: {"ok": True}, inline_wait=1,
        idempotency_key="spawn-failure",
    )
    retry = _run(
        registry, lambda: {"ok": True, "duplicate": True}, inline_wait=1,
        idempotency_key="spawn-failure",
    )

    assert result == retry == {
        "ok": False,
        "code": "job_failed",
        "error_kind": "internal",
        "error": "background job failed",
    }
    assert "thread-secret" not in json.dumps(result)


def test_completed_receipt_outlives_the_ten_minute_client_deadline(monkeypatch):
    import looplab.serve.jobs as jobs_module

    now = [1_000.0]
    monkeypatch.setattr(jobs_module.time, "time", lambda: now[0])
    registry = JobRegistry()
    for index in range(64):
        registry.put(
            f"done-{index}", status="done", result={"ok": True}, ts=now[0])

    blocked = registry.reserve("new-work")
    assert blocked["code"] == "job_capacity"
    assert registry.get("done-0") is not None

    now[0] += 661
    admitted = registry.reserve("new-work")
    assert admitted["status"] == "running"
    assert sum(registry.get(f"done-{index}") is not None for index in range(64)) == 63
