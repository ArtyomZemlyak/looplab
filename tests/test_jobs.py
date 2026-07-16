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


def test_rejoin_only_receipt_never_recreates_evicted_paid_work(monkeypatch):
    import looplab.serve.jobs as jobs_module

    now = [1_000.0]
    monkeypatch.setattr(jobs_module.time, "time", lambda: now[0])
    registry = JobRegistry()
    first = _run(
        registry, lambda: {"ok": True}, inline_wait=1,
        idempotency_key="durable-paid-claim")
    assert first == {"ok": True}
    receipt = registry.rejoin("durable-paid-claim")
    assert receipt is not None

    for index in range(63):
        registry.put(f"pressure-{index}", status="running", result=None, ts=now[0])
    now[0] += 661
    assert registry.reserve("new-work")["status"] == "running"
    assert registry.get(receipt["job_id"]) is None

    calls = []

    async def rejoin_evicted():
        return await registry.run_as_job(
            lambda: calls.append("duplicate"), inline_wait=0,
            reserved_job_id=receipt["job_id"])

    result = anyio.run(rejoin_evicted)
    assert result["code"] == "job_unknown"
    assert result["ambiguous"] is True
    assert calls == []
    assert registry.rejoin("durable-paid-claim") is None


def test_inline_non_idempotent_jobs_release_capacity_but_idempotent_result_rejoins():
    registry = JobRegistry()

    async def run_many():
        return [
            await registry.run_as_job(
                lambda index=index: {"ok": True, "index": index}, inline_wait=1)
            for index in range(70)
        ]

    results = anyio.run(run_many)

    assert results == [{"ok": True, "index": index} for index in range(70)]
    assert registry._jobs == {}  # no caller ever received these private job ids

    async def run_durable_reports():
        return [
            await registry.run_as_job(
                lambda index=index: {"ok": True, "seq": index},
                inline_wait=1,
                idempotency_key=f"durable-report-{index}",
                consume_inline_result=True,
            )
            for index in range(70)
        ]

    durable_results = anyio.run(run_durable_reports)

    assert durable_results == [{"ok": True, "seq": index} for index in range(70)]
    assert registry._jobs == {}  # replay is owned by the caller's durable ledger

    calls = []
    first = _run(
        registry, lambda: calls.append("paid") or {"ok": True, "value": "kept"},
        inline_wait=1, idempotency_key="durable-report",
    )
    replay = _run(
        registry, lambda: calls.append("duplicate") or {"ok": True},
        inline_wait=1, idempotency_key="durable-report",
    )

    assert first == replay == {"ok": True, "value": "kept"}
    assert calls == ["paid"]
    assert registry.has_identity("durable-report") is True


def test_terminal_poll_consumes_only_receipts_with_durable_replay_policy():
    registry = JobRegistry()

    def wait_for_terminal(job_id: str):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipt = registry.poll(job_id)
            if receipt and receipt.get("status") == "done":
                return receipt
            time.sleep(0.01)
        raise AssertionError("job did not finish")

    durable = registry.reserve("durable-ledger-owner", consume_on_poll=True)
    registry.start_reserved(durable["job_id"], lambda: {"ok": True, "value": "durable"})
    durable_terminal = wait_for_terminal(durable["job_id"])

    assert durable_terminal["result"] == {"ok": True, "value": "durable"}
    assert registry.poll(durable["job_id"]) is None
    assert registry.rejoin("durable-ledger-owner") is None

    generic = registry.reserve("generic-memory-owner")
    registry.start_reserved(generic["job_id"], lambda: {"ok": True, "value": "generic"})
    generic_terminal = wait_for_terminal(generic["job_id"])

    assert generic_terminal["result"] == {"ok": True, "value": "generic"}
    assert registry.poll(generic["job_id"])["result"] == generic_terminal["result"]
    assert registry.rejoin("generic-memory-owner") == generic
