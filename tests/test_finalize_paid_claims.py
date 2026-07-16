from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from looplab.core.models import RunState
from looplab.engine.finalize import ensure_finalize_reflection, ensure_finish_report
from looplab.events.eventstore import EventStore


def _seed_report(path):
    store = EventStore(path)
    store.append(
        "run_started",
        {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"},
    )
    scope = "finalize:paid-report"
    store.append(
        "finalize_step",
        {
            "scope": scope,
            "step": "begun",
            "finish_data": {},
            "finish_report_planned": True,
        },
    )
    return store, scope


def _seed_reflection(path):
    store = EventStore(path)
    store.append(
        "run_started",
        {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"},
    )
    finished = store.append("run_finished", {"reason": "done"})
    return store, f"finish:{finished.seq}", finished.seq


def _report_engine(path, paid):
    store = EventStore(path)

    def write_report(_state, *, trigger, finalize_scope):
        paid()
        store.append(
            "report_generated",
            {
                "content": {"headline": "paid"},
                "trigger": trigger,
                "finalize_scope": finalize_scope,
            },
        )

    return SimpleNamespace(
        store=store,
        report_writer=object(),
        _write_report=write_report,
    )


def _reflection_engine(path, paid):
    store = EventStore(path)

    def write_reflection(_state):
        paid()
        store.append("reflection_note", {"note": "paid reflection"})

    return SimpleNamespace(store=store, _write_reflection_note=write_reflection)


def test_finish_report_claim_requires_fsync_and_ambiguous_claim_is_not_replayed(
        tmp_path, monkeypatch):
    import looplab.events.eventstore as eventstore_module

    path = tmp_path / "events.jsonl"
    seed, scope = _seed_report(path)
    calls: list[int] = []
    engine = _report_engine(path, lambda: calls.append(1))

    with monkeypatch.context() as patch:
        patch.setattr(
            eventstore_module,
            "strict_fsync",
            lambda _fileno: (_ for _ in ()).throw(OSError("sync unavailable")),
        )
        with pytest.raises(OSError, match="sync unavailable"):
            ensure_finish_report(
                engine,
                seed.read_all(),
                scope,
                state=RunState(run_id="r", task_id="t"),
            )

    assert calls == []
    assert any(
        event.type == "finalize_step" and event.data.get("scope") == scope
        and event.data.get("step") == "report_begun"
        for event in EventStore(path).read_all()
    )

    # The write may have reached the log even though its fsync was unconfirmed.  Failing closed means
    # treating that marker as an ambiguous paid attempt, not buying a replacement on restart.
    ensure_finish_report(
        engine,
        EventStore(path).read_all(),
        scope,
        state=RunState(run_id="r", task_id="t"),
    )
    assert calls == []
    assert any(
        event.type == "finalize_step" and event.data.get("step") == "report"
        and event.data.get("outcome") == "prior_attempt_incomplete_not_replayed"
        for event in EventStore(path).read_all()
    )


def test_reflection_claim_requires_fsync_and_ambiguous_claim_is_not_replayed(
        tmp_path, monkeypatch):
    import looplab.events.eventstore as eventstore_module

    path = tmp_path / "events.jsonl"
    _seed, scope, finish_seq = _seed_reflection(path)
    calls: list[int] = []
    engine = _reflection_engine(path, lambda: calls.append(1))

    with monkeypatch.context() as patch:
        patch.setattr(
            eventstore_module,
            "strict_fsync",
            lambda _fileno: (_ for _ in ()).throw(OSError("sync unavailable")),
        )
        with pytest.raises(OSError, match="sync unavailable"):
            ensure_finalize_reflection(engine, scope, finish_seq)

    assert calls == []
    ensure_finalize_reflection(engine, scope, finish_seq)
    assert calls == []
    steps = [
        event.data for event in EventStore(path).read_all()
        if event.type == "finalize_step" and event.data.get("scope") == scope
    ]
    assert [step["step"] for step in steps] == ["reflection_begun", "reflection"]
    assert steps[-1]["outcome"] == "prior_attempt_incomplete_not_replayed"


def test_finish_report_no_writer_preflight_never_requires_paid_lock(tmp_path, monkeypatch):
    pending_path = tmp_path / "pending.jsonl"
    pending_store, pending_scope = _seed_report(pending_path)
    pending_engine = SimpleNamespace(
        store=pending_store,
        report_writer=None,
        _write_report=lambda *_args, **_kwargs: pytest.fail("no provider is configured"),
    )

    def forbidden_guard():
        raise AssertionError("free report preflight must not acquire the paid-effect guard")

    monkeypatch.setattr(pending_store, "paid_effect_guard", forbidden_guard)
    assert ensure_finish_report(
        pending_engine, [], pending_scope, state=RunState(run_id="r", task_id="t")) is False

    completed_path = tmp_path / "completed.jsonl"
    completed_store, completed_scope = _seed_report(completed_path)
    completed_store.append(
        "finalize_step",
        {"scope": completed_scope, "step": "report", "outcome": "completed"},
    )
    completed_engine = SimpleNamespace(
        store=completed_store,
        report_writer=None,
        _write_report=lambda *_args, **_kwargs: pytest.fail("no provider is configured"),
    )
    monkeypatch.setattr(completed_store, "paid_effect_guard", forbidden_guard)
    assert ensure_finish_report(
        completed_engine, [], completed_scope, state=RunState(run_id="r", task_id="t")) is True


@pytest.mark.parametrize(
    "reflection_priors,memory_dir",
    [(False, "configured-but-disabled"), (True, None)],
)
def test_disabled_reflection_finishes_without_paid_lock_or_strict_fsync(
        tmp_path, monkeypatch, reflection_priors, memory_dir):
    import looplab.events.eventstore as eventstore_module

    path = tmp_path / "events.jsonl"
    store, scope, finish_seq = _seed_reflection(path)

    @contextmanager
    def optional_guard(*, required=True):
        assert required is False
        yield

    monkeypatch.setattr(store, "paid_effect_guard", optional_guard)
    monkeypatch.setattr(
        eventstore_module,
        "strict_fsync",
        lambda _fileno: (_ for _ in ()).throw(AssertionError("strict fsync must not run")),
    )
    class DisabledReflectionEngine:
        def __init__(self):
            self.store = store
            self._reflection_priors = reflection_priors
            self.memory_dir = memory_dir

        def _write_reflection_note(self, _state):
            pytest.fail("disabled reflection must be a no-op")

    engine = DisabledReflectionEngine()

    ensure_finalize_reflection(engine, scope, finish_seq)

    steps = [
        event.data for event in EventStore(path).read_all()
        if event.type == "finalize_step" and event.data.get("scope") == scope
    ]
    assert steps == [
        {"scope": scope, "step": "reflection_begun", "outcome": "disabled"},
        {"scope": scope, "step": "reflection", "outcome": "disabled"},
    ]


def test_concurrent_finish_report_attempts_share_one_live_paid_flight(tmp_path):
    path = tmp_path / "events.jsonl"
    seed, scope = _seed_report(path)
    entered, release, second_started, second_finished = Event(), Event(), Event(), Event()
    calls: list[int] = []
    calls_lock = Lock()

    def paid():
        with calls_lock:
            calls.append(1)
        entered.set()
        assert release.wait(5)

    first_engine = _report_engine(path, paid)
    second_engine = _report_engine(path, paid)

    def first_attempt():
        return ensure_finish_report(
            first_engine, seed.read_all(), scope,
            state=RunState(run_id="r", task_id="t"),
        )

    def second_attempt():
        second_started.set()
        try:
            return ensure_finish_report(
                second_engine, EventStore(path).read_all(), scope,
                state=RunState(run_id="r", task_id="t"),
            )
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_attempt)
        assert entered.wait(5)
        second = executor.submit(second_attempt)
        assert second_started.wait(5)
        try:
            # The loser waits for the winner's terminal observation instead of prematurely turning
            # its live claim into an ambiguous/crashed attempt.
            assert not second_finished.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=10) is True
        assert second.result(timeout=10) is True

    events = EventStore(path).read_all()
    assert calls == [1]
    assert sum(
        event.type == "finalize_step" and event.data.get("step") == "report_begun"
        for event in events
    ) == 1
    assert sum(event.type == "report_generated" for event in events) == 1


def test_no_writer_report_recovery_waits_for_differently_configured_live_buyer(tmp_path):
    path = tmp_path / "events.jsonl"
    seed, scope = _seed_report(path)
    entered, release, recovery_started, recovery_finished = Event(), Event(), Event(), Event()
    calls: list[int] = []

    def paid():
        calls.append(1)
        entered.set()
        assert release.wait(5)

    paid_engine = _report_engine(path, paid)
    recovery_store = EventStore(path)
    recovery_engine = SimpleNamespace(store=recovery_store, report_writer=None)

    def paid_attempt():
        return ensure_finish_report(
            paid_engine, seed.read_all(), scope,
            state=RunState(run_id="r", task_id="t"),
        )

    def free_recovery():
        recovery_started.set()
        try:
            return ensure_finish_report(
                recovery_engine, recovery_store.read_all(), scope,
                state=RunState(run_id="r", task_id="t"),
            )
        finally:
            recovery_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(paid_attempt)
        assert entered.wait(5)
        second = executor.submit(free_recovery)
        assert recovery_started.wait(5)
        try:
            assert not recovery_finished.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=10) is True
        assert second.result(timeout=10) is True

    events = EventStore(path).read_all()
    assert calls == [1]
    assert sum(event.type == "report_generated" for event in events) == 1
    assert not any(
        event.type == "finalize_step" and event.data.get("step") == "report"
        and event.data.get("outcome") == "prior_attempt_incomplete_not_replayed"
        for event in events
    )


def test_concurrent_reflection_attempts_share_one_live_paid_flight(tmp_path):
    path = tmp_path / "events.jsonl"
    _seed, scope, finish_seq = _seed_reflection(path)
    entered, release, second_started, second_finished = Event(), Event(), Event(), Event()
    calls: list[int] = []
    calls_lock = Lock()

    def paid():
        with calls_lock:
            calls.append(1)
        entered.set()
        assert release.wait(5)

    first_engine = _reflection_engine(path, paid)
    second_engine = _reflection_engine(path, paid)

    def first_attempt():
        ensure_finalize_reflection(first_engine, scope, finish_seq)

    def second_attempt():
        second_started.set()
        try:
            ensure_finalize_reflection(second_engine, scope, finish_seq)
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_attempt)
        assert entered.wait(5)
        second = executor.submit(second_attempt)
        assert second_started.wait(5)
        try:
            assert not second_finished.wait(0.2)
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    events = EventStore(path).read_all()
    assert calls == [1]
    assert sum(
        event.type == "finalize_step" and event.data.get("step") == "reflection_begun"
        for event in events
    ) == 1
    assert sum(
        event.type == "finalize_step" and event.data.get("step") == "reflection"
        for event in events
    ) == 1


def test_disabled_reflection_recovery_waits_for_differently_configured_live_buyer(tmp_path):
    path = tmp_path / "events.jsonl"
    _seed, scope, finish_seq = _seed_reflection(path)
    entered, release, recovery_started, recovery_finished = Event(), Event(), Event(), Event()
    calls: list[int] = []

    def paid():
        calls.append(1)
        entered.set()
        assert release.wait(5)

    paid_engine = _reflection_engine(path, paid)

    class DisabledRecoveryEngine:
        def __init__(self):
            self.store = EventStore(path)
            self._reflection_priors = False
            self.memory_dir = None

        def _write_reflection_note(self, _state):
            pytest.fail("disabled recovery must not dispatch reflection")

    recovery_engine = DisabledRecoveryEngine()

    def paid_attempt():
        ensure_finalize_reflection(paid_engine, scope, finish_seq)

    def free_recovery():
        recovery_started.set()
        try:
            ensure_finalize_reflection(recovery_engine, scope, finish_seq)
        finally:
            recovery_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(paid_attempt)
        assert entered.wait(5)
        second = executor.submit(free_recovery)
        assert recovery_started.wait(5)
        try:
            assert not recovery_finished.wait(0.2)
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)

    events = EventStore(path).read_all()
    assert calls == [1]
    assert sum(event.type == "reflection_note" for event in events) == 1
    steps = [
        event.data for event in events
        if event.type == "finalize_step" and event.data.get("scope") == scope
    ]
    assert [step["step"] for step in steps] == ["reflection_begun", "reflection"]
    assert all(step.get("outcome") != "disabled" for step in steps)
