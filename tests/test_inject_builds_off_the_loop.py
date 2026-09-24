"""An operator `inject_node` WITHOUT code builds OFF the event-loop thread (review 2026-09-22, ENG1-07).

`_serve_forced_requests` served an inject by calling `_create_injected_node` directly, so the paid
Developer session it runs for an idea with no ready-made code executed ON the loop thread — the
hold `_offload_build` removed from every other build path (doc 52 row 12): no eval-watcher tick,
no operator abort/reset detection, no train-monitor kill and no control ACK for as long as the
session spends. Measured on the reviewer's reproduction: the inject's `implement` ran on the main
thread with ZERO loop ticks during a 0.3 s call, while the three ordinary builds beside it ticked
28-30 times each.

The offload keeps every write where invariant #1 wants it, and these tests drive each one: the
reservation (`card_added` + `node_building`) is marshalled back to the main task through
`_reserve_on_main_task`, the node's own `node_created` stays the licensed worker append, and a
Developer crash QUEUES its run-global pause for the loop to drain.
"""
from __future__ import annotations

import threading
import time

import anyio
import pytest

from looplab.core.llm import BudgetExceeded
from looplab.events.replay import fold
from looplab.events.types import (EV_CARD_ADDED, EV_INJECT_NODE, EV_NODE_BUILDING,
                                  EV_NODE_FAILED, EV_PAUSE)
from tests.factories import make_engine

_INJECT = {"idea": {"operator": "manual", "params": {"x": 1.0, "y": 1.0},
                    "rationale": "operator idea"}}


class _SlowDeveloper:
    """Wraps the toy Developer; the INJECT's `implement` is slow and records where it ran."""

    is_code_generating = False

    def __init__(self, real, calls, ticks):
        self._real, self._calls, self._ticks = real, calls, ticks

    def __getattr__(self, name):
        return getattr(self._real, name)

    def implement(self, idea):
        before = self._ticks["n"]
        time.sleep(0.3)
        self._calls.append((threading.current_thread() is threading.main_thread(),
                            self._ticks["n"] - before, idea.operator))
        return self._real.implement(idea)


def _watch_appends(eng) -> list:
    store = eng.store
    real = store.append

    def append(event_type, data, **kwargs):
        log.append((event_type, threading.current_thread() is threading.main_thread()))
        return real(event_type, data, **kwargs)

    real_many = store.append_many

    def append_many(records, **kwargs):
        for event_type, _data in records:
            log.append((event_type, threading.current_thread() is threading.main_thread()))
        return real_many(records, **kwargs)

    log: list = []
    store.append, store.append_many = append, append_many
    return log


def _run_with_ticker(eng):
    ticks = {"n": 0}

    async def _ticker():
        while True:
            ticks["n"] += 1
            await anyio.sleep(0.01)

    async def _main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(_ticker)
            try:
                return await eng.run()
            finally:
                tg.cancel_scope.cancel()

    return ticks, _main


def test_an_injected_build_runs_off_the_loop_and_the_loop_keeps_turning(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2)
    ticks, main = _run_with_ticker(eng)
    calls: list = []
    eng.developer = _SlowDeveloper(eng.developer, calls, ticks)
    eng.store.append(EV_INJECT_NODE, dict(_INJECT))
    log = _watch_appends(eng)
    state = anyio.run(main)

    injected = [c for c in calls if c[2] == "manual"]
    assert injected, "the inject never reached its Developer"
    on_main, ticked, _op = injected[0]
    assert not on_main, "the inject's paid Developer session ran on the event-loop thread"
    assert ticked > 0, "the loop did not turn while the inject's Developer spent"
    # The reservation is not the worker's to make (`_reserve_on_main_task`).
    reserved = [on_main for event_type, on_main in log
                if event_type in (EV_CARD_ADDED, EV_NODE_BUILDING)]
    assert reserved and all(reserved), f"a reservation row was appended by a worker: {log}"
    manual = [n for n in state.nodes.values() if n.operator == "manual"]
    assert len(manual) == 1 and manual[0].metric is not None
    assert state.injects_done == 1


class _CrashingDeveloper:
    """The in-band sentinel every build site recognises as a crashed Developer session."""

    def implement(self, idea):
        return "(developer error: provider unreachable)"


def test_an_injected_build_crash_pauses_the_run_from_the_main_task(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2,
                      developer=_CrashingDeveloper())
    eng.store.append(EV_INJECT_NODE, dict(_INJECT))
    log = _watch_appends(eng)
    state = anyio.run(eng.run)

    pauses = [on_main for event_type, on_main in log if event_type == EV_PAUSE]
    assert pauses == [True], f"the run-global pause must be appended once, on the main task: {pauses}"
    assert state.paused and not state.finished
    failed = [n for n in state.nodes.values() if n.error_reason == "developer_crash"]
    assert len(failed) == 1 and state.pause_node_id == failed[0].id


class _CeilingDeveloper:
    def implement(self, idea):
        raise BudgetExceeded("LLM budget exceeded: spent 1.0100 >= budget 1.0000")


def test_the_spend_ceiling_inside_an_injected_build_ends_the_run(tmp_path):
    """The serve handler contained EVERY exception as `materialization_failed`, so a Developer that
    hit the spend ceiling while building an inject let the run go on spending. It is the run's
    stop, as on every other build path."""
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2, developer=_CeilingDeveloper())
    eng.store.append(EV_INJECT_NODE, dict(_INJECT))
    with pytest.raises(BudgetExceeded):
        anyio.run(eng.run)
    events = eng.store.read_all()
    assert not [e for e in events if e.type == "inject_failed"], (
        "the spend ceiling was recorded as an inject that failed to materialize")
    # The reservation is still closed (the operator's request never leaves a bare build behind).
    assert not fold(events).buildings
    assert [e.data["reason"] for e in events if e.type == EV_NODE_FAILED] == ["build_crash"]


class _CountingDeveloper:
    """Records every `implement` the engine asks for; builds nothing new itself."""

    is_code_generating = False

    def __init__(self, real):
        self._real, self.calls = real, []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def implement(self, idea):
        self.calls.append(idea.operator)
        return self._real.implement(idea)


def test_an_inject_carrying_a_file_overlay_is_ready_made_and_skips_the_developer(tmp_path):
    """minionerec-backbones-v8 (2026-09-24): an inject whose only artefact was a finished
    `experiment.env` overlay was built AGAIN through Developer stages/plan/implement (60+ min, GPUs
    idle), because `developer_called` looked at `code` only — the script-solution field. A repo
    candidate IS its overlay. Mutation: key `developer_called` on `code` alone again (the manual
    idea reaches `implement`)."""
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2)
    dev = _CountingDeveloper(eng.developer)
    eng.developer = dev
    overlay = {"cfg/experiment.env": "BACKBONE=lfm25_1.2B\n"}
    eng.store.append(EV_INJECT_NODE, dict(_INJECT, files=dict(overlay)))
    state = anyio.run(eng.run)

    assert "manual" not in dev.calls, "a ready-made overlay was sent back to the Developer"
    manual = [n for n in state.nodes.values() if n.operator == "manual"]
    assert len(manual) == 1 and state.injects_done == 1
    assert dict(manual[0].files or {}) == overlay, "the overlay must be committed exactly as supplied"


def test_an_inject_with_neither_code_nor_files_is_still_built_by_the_developer(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2)
    dev = _CountingDeveloper(eng.developer)
    eng.developer = dev
    eng.store.append(EV_INJECT_NODE, dict(_INJECT))
    anyio.run(eng.run)
    assert "manual" in dev.calls
