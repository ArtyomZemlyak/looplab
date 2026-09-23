"""The live-log watchdogs' shared tick, written ONCE (review 2026-09-22, ENG3-13 / doc 50 EM-05).

`train_monitor.py::_monitor_training` and `asha_monitor.py::_monitor_asha` each carried the same
scaffold around their own judgement — the resume read, the bounded paid look in a worker with its
cap and pre-call cancel check, the redaction, the kill attribution and the shielded diagnostic
append — and every fix to it had to land twice ("the 2026-08-30 fix landed twice"; the ENG3-02
`finally` did too). It is a handful of free functions now (`engine/train_monitor.py`'s
`recover_last_row`, `watchdog_judge_tick`, `watchdog_redact`, `append_watchdog_row`, and
`monitor_gates.kill_superseded_by`), and this file drives each one directly, then proves BOTH loops
reach them — by driving the loops, not by reading their source.

Every test names the mutation that makes it fail.
"""
from __future__ import annotations

import functools
import threading

import anyio
import pytest

from looplab.core.llm import BudgetExceeded
from looplab.core.models import Event
from looplab.engine import train_monitor as tm
from looplab.engine.monitor_gates import kill_superseded_by
from looplab.engine.train_monitor import (
    JudgeCalls,
    append_watchdog_row,
    recover_last_row,
    watchdog_judge_tick,
    watchdog_redact,
)
from looplab.events.types import (EV_ASHA_RANK, EV_ASHA_VERDICT, EV_NODE_EVALUATED,
                                  EV_TRAIN_MONITOR_ALERT)

from test_asha_monitor import _AshaStub, _JudgeClient, _kill_setup, _run_loop
from test_train_monitor import _FakeClient, _FakeDeveloper, _TRAIN_PLAN, _run_verdict_monitor


class _Span:
    def __init__(self):
        self.attrs: dict = {}

    def set(self, key, value):
        self.attrs[key] = value

    def set_many(self, **kw):
        self.attrs.update(kw)


class _Store:
    def __init__(self, rows=(), fail=False):
        self.rows = [Event(seq=i, ts=0.0, type=t, data=dict(d)) for i, (t, d) in enumerate(rows)]
        self.fail = fail
        self.read_threads: list[int] = []
        self.appended: list = []

    def read_all(self):
        self.read_threads.append(threading.get_ident())
        if self.fail:
            raise OSError("the log's mount just went away")
        return list(self.rows)

    def append(self, event_type, data):
        self.appended.append((event_type, data))


class _Engine:
    """Exactly what the shared functions touch — and no `Engine.__init__`, like the ASHA stub."""

    def __init__(self, store=None, redact=None):
        self.store = store if store is not None else _Store()
        self._write_lock = anyio.Lock()
        if redact is not None:
            self._redact = redact


# ------------------------------------------------------------------------------ the resume read

def test_the_resume_read_answers_the_newest_row_of_exactly_this_lifecycle_off_the_loop():
    """MUTATION: read `engine.store.read_all()` on the loop instead of through the worker hop ->
    the recorded thread IS the loop's; scan for the wrong type or generation -> the wrong row."""
    store = _Store([
        (EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 0, "status": "broken"}),
        (EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 0, "status": "watch"}),
        (EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "generation": 1, "status": "healthy"}),
        (EV_TRAIN_MONITOR_ALERT, {"node_id": True, "generation": 0, "status": "broken"}),
        (EV_ASHA_RANK, {"node_id": 0, "generation": 0, "underperforming": True}),
    ])
    engine = _Engine(store)

    async def main():
        return threading.get_ident(), await recover_last_row(engine, EV_TRAIN_MONITOR_ALERT, 0, 0)

    loop_thread, row = anyio.run(main)
    assert row == {"node_id": 0, "generation": 0, "status": "watch"}
    assert store.read_threads and loop_thread not in store.read_threads


def test_a_resume_read_that_fails_is_no_history_and_never_ends_the_watch():
    """MUTATION: drop the handler -> the OSError leaves the watchdog's first line and it never
    starts watching a multi-hour eval."""
    assert anyio.run(recover_last_row, _Engine(_Store(fail=True)), EV_ASHA_RANK, 0, 0) is None
    assert anyio.run(recover_last_row, _Engine(), EV_ASHA_RANK, 0, 0) is None      # an empty log


# ------------------------------------------------------------------------------ one paid look

def _tick(judge, calls, *, cap=5, cancelled=False, on_raised=None):
    sp, cancel = _Span(), threading.Event()
    if cancelled:
        cancel.set()

    async def main():
        return await watchdog_judge_tick(sp, cancel, judge, calls, cap=cap, on_raised=on_raised)

    return anyio.run(main), sp


def test_a_spent_backstop_asks_nothing_and_says_so():
    """MUTATION: drop the cap check -> the judge is asked past the per-node backstop."""
    asked: list = []
    calls = JudgeCalls()
    calls.started = 3
    (answer, stop), sp = _tick(lambda: asked.append(1) or "verdict", calls, cap=3)
    assert (answer, stop) == (None, False) and asked == [] and calls.started == 3
    assert sp.attrs == {"llm_capped": True}


def test_an_eval_that_ended_while_the_tick_read_buys_nothing_and_ends_the_watch():
    """MUTATION: drop the pre-call `cancel` check -> a verdict about a dead node is paid for."""
    asked: list = []
    calls = JudgeCalls()
    (answer, stop), sp = _tick(lambda: asked.append(1), calls, cancelled=True)
    assert stop is True and asked == [] and calls.started == 0
    assert sp.attrs == {"cancelled_before_call": True}


def test_an_answered_look_runs_in_a_worker_and_is_counted_once():
    """MUTATION: call `judge()` directly on the loop -> the recorded thread is the loop's; drop the
    count -> the backstop never binds."""
    ran_on: list[int] = []
    loop: dict = {}
    calls = JudgeCalls()

    def judge():
        ran_on.append(threading.get_ident())
        return "verdict"

    sp, cancel = _Span(), threading.Event()

    async def main():
        loop["thread"] = threading.get_ident()
        return await watchdog_judge_tick(sp, cancel, judge, calls, cap=5)

    assert anyio.run(main) == ("verdict", False)
    assert calls.started == 1 and ran_on and loop["thread"] not in ran_on
    assert sp.attrs == {}


def test_a_judge_that_raises_is_counted_and_its_callers_bound_committed_before_it_propagates():
    """The tick's own blind handler then skips the tick, as it always did. MUTATION: move the count
    and `on_raised` out of `finally` to after the await -> a raising judge commits neither bound,
    which is the 300-calls-in-5-s reproduction of ENG3-02."""
    calls = JudgeCalls()
    committed: list = []

    def judge():
        raise RuntimeError("endpoint 500")

    with pytest.raises(RuntimeError):
        _tick(judge, calls, on_raised=lambda: committed.append("retire?"))
    assert calls.started == 1 and committed == ["retire?"]


def test_the_spend_ceiling_ends_the_watch_and_is_said_on_the_span():
    """MUTATION: drop the `except BudgetExceeded` -> the stop escapes into the tick's blind handler
    and is re-paid on the next tick (and `on_raised` runs as for an ordinary failure)."""
    calls = JudgeCalls()
    committed: list = []

    def judge():
        raise BudgetExceeded("LLM spend ceiling reached")

    (answer, stop), sp = _tick(judge, calls, on_raised=lambda: committed.append(1))
    assert (answer, stop) == (None, True) and sp.attrs == {"budget_stop": True}
    assert calls.started == 1 and committed == []


def test_cancellation_joins_the_paid_call_instead_of_abandoning_it():
    """Its usage is billed to shared run state, so no detached worker may outlive the node.
    MUTATION: `abandon_on_cancel=True` -> the task group closes while the worker is still paying."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def judge():
        started.set()
        release.wait(5)
        finished.set()
        return "late verdict"

    calls = JudgeCalls()

    async def main():
        timer = threading.Timer(0.2, release.set)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(functools.partial(watchdog_judge_tick, _Span(), threading.Event(),
                                                judge, calls, cap=5))
                await anyio.to_thread.run_sync(started.wait, abandon_on_cancel=True)
                timer.start()
                tg.cancel_scope.cancel()
            return finished.is_set()
        finally:
            release.set()
            timer.cancel()

    assert anyio.run(main) is True, "the group closed before the paid worker finished"
    assert calls.started == 1


# ------------------------------------------------------------------------------ what gets written

def test_the_judge_text_goes_through_the_redaction_funnel_and_is_bounded():
    """MUTATION: drop `[:limit]` -> an unbounded model sentence reaches the durable row; ignore the
    engine's `_redact` -> the raw secret does."""
    engine = _Engine(redact=lambda s: s.replace("sk-SECRET", "[REDACTED]"))
    assert watchdog_redact(engine, "key sk-SECRET leaked") == "key [REDACTED] leaked"
    assert watchdog_redact(engine, "x" * 500) == "x" * 300
    assert watchdog_redact(_Engine(), "plain", limit=3) == "pla"      # no funnel on a bare host
    odd = _Engine()
    odd._redact = "not callable"
    assert watchdog_redact(odd, "kept") == "kept"


def test_a_losing_claim_names_the_winner_and_is_bounded():
    assert kill_superseded_by({"kill": True, "terminal_reason": "asha_underperforming"}) == (
        "asha_underperforming")
    assert kill_superseded_by({"kill": True}) == ""
    assert kill_superseded_by({"terminal_reason": "x" * 100}) == "x" * 64


@pytest.mark.parametrize("shield", [True, False])
def test_a_decided_stop_row_survives_the_cancellation_its_own_claim_set_off(shield):
    """The write lock is the row's next cancellation checkpoint. Hold it, cancel the task, release
    it: shielded, the row lands; unshielded it is lost — the plain best-effort append a row that
    decided nothing keeps. MUTATION: drop the shield -> the decided stop leaves no row."""
    engine = _Engine()

    async def main():
        await engine._write_lock.acquire()
        async with anyio.create_task_group() as tg:
            tg.start_soon(functools.partial(append_watchdog_row, EV_TRAIN_MONITOR_ALERT,
                                            {"node_id": 0, "stop_decided": True}, engine,
                                            shield=shield))
            await anyio.sleep(0.05)                       # the append now waits on the lock
            tg.cancel_scope.cancel()
            engine._write_lock.release()

    anyio.run(main)
    assert engine.store.appended == (
        [(EV_TRAIN_MONITOR_ALERT, {"node_id": 0, "stop_decided": True})] if shield else [])


def test_only_a_diagnostic_row_may_take_the_watchdog_append():
    """Both watchdogs append from a task beside the main one, so a folded row there would land at a
    thread-dependent position (invariant #1). MUTATION: drop the assertion -> it is written."""
    engine = _Engine()
    with pytest.raises(AssertionError):
        anyio.run(functools.partial(append_watchdog_row, EV_NODE_EVALUATED, {"node_id": 0},
                                    engine, shield=False))
    assert engine.store.appended == []


# ------------------------------------------------------------------------------ both loops use them

def _spy_on_the_shared_tick(monkeypatch) -> dict:
    """Wrap each shared function ON `train_monitor`, which both loops resolve them through — the
    training loop as its own globals, the ASHA loop by a function-local import at call time — and
    record what each call was FOR, so a loop that re-inlines ONE of two call sites is still caught."""
    seen: dict = {"recover_last_row": [], "watchdog_judge_tick": [], "watchdog_redact": [],
                  "append_watchdog_row": []}
    real = {name: getattr(tm, name) for name in seen}

    async def recover(engine, event_type, *a, **k):
        seen["recover_last_row"].append(event_type)
        return await real["recover_last_row"](engine, event_type, *a, **k)

    async def tick(*a, **k):
        seen["watchdog_judge_tick"].append(k.get("cap"))
        return await real["watchdog_judge_tick"](*a, **k)

    def redact(engine, text, *a, **k):
        seen["watchdog_redact"].append(text)
        return real["watchdog_redact"](engine, text, *a, **k)

    async def append(event_type, *a, **k):
        seen["append_watchdog_row"].append(event_type)
        return await real["append_watchdog_row"](event_type, *a, **k)

    for name, spy in (("recover_last_row", recover), ("watchdog_judge_tick", tick),
                      ("watchdog_redact", redact), ("append_watchdog_row", append)):
        monkeypatch.setattr(tm, name, spy)
    return seen


def test_the_training_loop_goes_through_every_shared_piece(tmp_path, monkeypatch):
    """MUTATION: re-inline any one of them in `_monitor_training` — its own `read_all` hop, its own
    `to_thread` call, its own `_redact` idiom at either of its two sites (the reason, the cited
    locator) or its own shielded append — and the spy never sees that call."""
    seen = _spy_on_the_shared_tick(monkeypatch)
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: nan\nRuntimeError: CUDA error: device-side assert\n")
    client = _FakeClient({"status": "broken", "reason": "nan loss", "confidence": 0.95,
                          "fault": "implementation", "evidence_source": "code",
                          "evidence_locator": "train.py:1"})
    host, _spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), plan=_TRAIN_PLAN,
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events))
    assert any(t == EV_TRAIN_MONITOR_ALERT for t, _d in host.store.events)
    assert seen["recover_last_row"] == [EV_TRAIN_MONITOR_ALERT], seen
    assert seen["watchdog_judge_tick"] and set(seen["watchdog_judge_tick"]) == {
        tm._MAX_MONITOR_LLM_CALLS}, seen
    assert {"nan loss", "train.py:1"} <= set(seen["watchdog_redact"]), seen
    assert EV_TRAIN_MONITOR_ALERT in seen["append_watchdog_row"], seen


def test_the_asha_loop_goes_through_every_shared_piece(tmp_path, monkeypatch):
    """The ASHA stub never ran `Engine.__init__` and has no `TrainingMonitorMixin` — the object that
    would break a mixin method, which is why these are free functions. MUTATION: re-inline any one
    of them in `_monitor_asha`, the rank row's append and the verdict row's append included."""
    from looplab.engine import asha_monitor as am

    seen = _spy_on_the_shared_tick(monkeypatch)
    wd, spec, curves = _kill_setup(tmp_path)
    signal: dict = {}
    judge = _JudgeClient({"status": "stop", "reason": "flat while peers climbed", "confidence": 0.95})
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    _run_loop(stub, wd, spec, "max", signal, monkeypatch, finals=[0.8, 0.7, 0.6], curves=curves,
              window=0.2, until=lambda s: bool(signal.get("kill")))
    assert signal.get("terminal_reason") == "asha_underperforming"
    assert any(t == EV_ASHA_VERDICT for t, _d in stub.store.events)
    assert seen["recover_last_row"] == [EV_ASHA_RANK], seen
    assert seen["watchdog_judge_tick"] and set(seen["watchdog_judge_tick"]) == {
        am._MAX_ASHA_JUDGE_CALLS}, seen
    assert "flat while peers climbed" in seen["watchdog_redact"], seen
    assert {EV_ASHA_RANK, EV_ASHA_VERDICT} <= set(seen["append_watchdog_row"]), seen
