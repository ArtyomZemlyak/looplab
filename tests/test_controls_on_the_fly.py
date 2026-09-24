"""Operator controls are drained WHILE a long main-loop step runs, not after it (2026-09-24).

minionerec-backbones-v8/v9: the server appends a control intent at once, but the engine acked it
(`command_ack`, the `engine_ack` postcondition) and applied it only at the run loop's HEAD. A Card
session whose producer ran a 10-40 min Developer session held the head away: v9's second
`inject_node` (t=564 s) was neither acked nor served 1,700 s later, the server record went
`timed_out`, and `budget_extend{parallel_build: 3}` could not help because the width was read at
that same head and the session never served injects — after card-1 committed it elected card-2.

What these tests pin (`engine/forced_requests.py`, "controls ON THE FLY"):
  * a command sent during a long step is ACKED and its live-safe effect (widths, timeouts) APPLIED
    well before the step ends — through a real `Engine.run`, the server's intent written by a
    second `EventStore` exactly as the server process writes it;
  * the effect is a pure function of the log: a fresh engine re-derives it by replay, and the
    loop head does not duplicate the watcher's ack;
  * a raised `parallel_build` lets the Card session serve a queued inject BESIDE the running
    producer, and at width 1 the session stops electing new Cards and hands the request to the
    outer loop instead of starving it.
"""
from __future__ import annotations

import threading
import time

import anyio
import pytest

from looplab.engine.forced_requests import ForcedRequestsMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (EV_BUDGET_EXTEND, EV_CARD_BUILD_REQUESTED, EV_COMMAND_ACK,
                                  EV_INJECT_DONE, EV_INJECT_NODE)
from tests.factories import make_engine
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _Developer,
    _engine,
    _request,
    _start,
    _without_research,
)

_INJECT = {"idea": {"operator": "manual", "params": {"x": 1.0, "y": 1.0},
                    "rationale": "operator idea"}}
_STEP_S = 4.0


@pytest.fixture
def fast_watch(monkeypatch):
    monkeypatch.setattr(ForcedRequestsMixin, "_CONTROL_WATCH_POLL_S", 0.05)
    monkeypatch.setattr(ForcedRequestsMixin, "_CONTROL_WATCH_GRACE_S", 0.2)


def _acks(events, command_id):
    return [e for e in events
            if e.type == EV_COMMAND_ACK and (e.data or {}).get("command_id") == command_id]


class _LongStepDeveloper:
    """The FIRST build is the long step: while it runs, the 'server' posts a budget_extend and
    watches for the engine's ack and the applied width — from inside the step."""

    is_code_generating = False

    def __init__(self, real, engine, seen):
        self._real, self._engine, self._seen = real, engine, seen
        self._first = True

    def __getattr__(self, name):
        return getattr(self._real, name)

    def implement(self, idea):
        if self._first:
            self._first = False
            server = EventStore(self._engine.store.path)          # a second writer, like the server
            posted = server.append(EV_BUDGET_EXTEND, {
                "parallel_build": 3, "timeout": 77.0, "_command_id": "cmd_width"})
            self._seen["intent_seq"] = posted.seq
            t0 = time.monotonic()
            deadline = t0 + _STEP_S
            while time.monotonic() < deadline and "acked_after_s" not in self._seen:
                if _acks(server.read_all(), "cmd_width"):
                    self._seen["acked_after_s"] = time.monotonic() - t0
                    self._seen["llm_parallel"] = self._engine._llm_parallel
                    self._seen["timeout"] = self._engine.timeout
                time.sleep(0.02)
            # The step keeps running to its full length either way: the ack must not need it to end.
            time.sleep(max(0.0, deadline - time.monotonic()))
            self._seen["step_ended"] = True
        return self._real.implement(idea)


def test_a_command_sent_during_a_long_step_is_acked_and_applied_before_the_step_ends(
        tmp_path, fast_watch):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2)
    assert eng._llm_parallel == 1, "precondition: a serial build slot"
    seen: dict = {}
    eng.developer = _LongStepDeveloper(eng.developer, eng, seen)
    state = anyio.run(eng.run)

    assert seen.get("step_ended"), "precondition: the long step ran"
    assert "acked_after_s" in seen, (
        f"the command was not acked while the {_STEP_S:.0f}s step was running — it waited for the "
        "loop head (the v8/v9 `timed_out` record)")
    assert seen["acked_after_s"] < _STEP_S / 2
    assert seen["llm_parallel"] == 3, "parallel_build was not applied until the step ended"
    assert seen["timeout"] == 77.0, "the eval timeout was not applied until the step ended"

    events = eng.store.read_all()
    acks = _acks(events, "cmd_width")
    assert len(acks) == 1, f"the loop head re-acked what the watcher already acked: {acks}"
    assert acks[0].data["event_seq"] == seen["intent_seq"]
    assert state.budget_overrides.get("parallel_build") == 3


def test_replaying_the_log_re_derives_the_same_control_state(tmp_path, fast_watch):
    run_dir = tmp_path / "run"
    eng = make_engine(run_dir, n_seeds=1, max_nodes=2)
    seen: dict = {}
    eng.developer = _LongStepDeveloper(eng.developer, eng, seen)
    live_state = anyio.run(eng.run)
    assert "acked_after_s" in seen, "precondition: acked on the fly"
    live = (eng._llm_parallel, eng.timeout, eng._eval_parallel)

    # Replay: a fresh engine over the same log derives the same state and the same live knobs.
    events = EventStore(run_dir / "events.jsonl").read_all()
    replayed = fold(events)
    assert replayed.model_dump() == live_state.model_dump()
    fresh = make_engine(run_dir, n_seeds=1, max_nodes=2)
    assert (fresh._llm_parallel, fresh.timeout) != live[:2], "precondition: launch values differ"
    fresh._apply_control_overrides(replayed)
    assert (fresh._llm_parallel, fresh.timeout, fresh._eval_parallel) == live
    # …and re-entering the run (resume) acks nothing twice: the intent's ack is already durable.
    before = len(_acks(events, "cmd_width"))
    fresh._ack_commands(events)
    assert len(_acks(fresh.store.read_all(), "cmd_width")) == before == 1


class _BlockingProducer(_Developer):
    """A Card producer whose build is the v9 long step: it runs until the test releases it."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def implement(self, _idea):
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=20):
            raise RuntimeError("the test never released the producer")
        return self.code


async def _until(predicate, *, timeout=10.0, step=0.05):
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(step)


def _force_watch_tick(engine) -> bool:
    """One watcher tick as if the loop head had been away past the grace period."""
    engine._mark_loop_head()
    engine._loop_head_monotonic -= 3600.0
    return engine._control_watch_tick()


def test_a_raised_parallel_build_serves_a_queued_inject_beside_the_running_producer(
        tmp_path, monkeypatch):
    producer = _BlockingProducer()
    engine, _ = _engine(tmp_path / "run", producer=producer)
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)
    assert engine._llm_parallel == 1, "precondition: one serial build slot (the v9 launch)"
    server = EventStore(engine.store.path)
    observed: dict = {}

    async def operator():
        await _until(producer.started.is_set)
        server.append(EV_INJECT_NODE, dict(_INJECT, _command_id="cmd_inject"))
        await anyio.sleep(1.0)
        # Width 1 and the slot is the producer's: the inject waits (acked, not yet served).
        state = fold(engine.store.read_all())
        observed["served_at_width_1"] = state.injects_done
        server.append(EV_BUDGET_EXTEND, {"parallel_build": 3, "_command_id": "cmd_width"})
        assert _force_watch_tick(engine) is True
        observed["width"] = engine._llm_parallel
        await _until(lambda: fold(engine.store.read_all()).injects_done == 1)
        await _until(lambda: any(n.operator == "manual"
                                 for n in fold(engine.store.read_all()).nodes.values()))
        observed["producer_still_building"] = not producer.release.is_set()
        producer.release.set()

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(operator)
            with anyio.fail_after(60):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(scenario)

    assert observed["served_at_width_1"] == 0
    assert observed["width"] == 3
    assert observed["producer_still_building"], (
        "the inject was served only after the producer's build ended — behind the serial slot")
    events = engine.store.read_all()
    assert len(_acks(events, "cmd_inject")) == 1 and len(_acks(events, "cmd_width")) == 1
    state = fold(events)
    assert state.injects_done == 1 and not state.buildings
    manual = [n for n in state.nodes.values() if n.operator == "manual"]
    assert len(manual) == 1
    assert [e.data["idx"] for e in events if e.type == EV_INJECT_DONE] == [0]
    assert producer.calls == 1


def test_at_width_one_the_session_hands_a_queued_inject_to_the_outer_loop(tmp_path, monkeypatch):
    """v9's starvation: after card-1 committed the session elected card-2 while the inject waited.
    Now it elects nothing while a node-creating operator request is queued and returns as soon as
    the producer lane is idle, so `_serve_forced_requests` serves it on the next outer turn."""
    producer = _BlockingProducer()
    engine, _ = _engine(tmp_path / "run", producer=producer)
    _start(engine)
    _add_ready_draft(engine)
    _add_ready_draft(engine, "card-8", x=0.5)      # something the session COULD elect next
    _request(engine)
    _without_research(monkeypatch, engine)

    async def operator():
        await _until(producer.started.is_set)
        EventStore(engine.store.path).append(EV_INJECT_NODE, dict(_INJECT))
        await _until(lambda: fold(engine.store.read_all()).inject_requests != [])
        producer.release.set()

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(operator)
            with anyio.fail_after(60):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(scenario)

    events = engine.store.read_all()
    requested = [e.data.get("card_id") for e in events if e.type == EV_CARD_BUILD_REQUESTED]
    assert requested == ["card-7"], f"the session elected new Card work over the inject: {requested}"
    state = fold(events)
    assert state.injects_done == 0 and len(state.inject_requests) == 1
    assert engine._operator_node_request_ready(state), "the outer loop must be able to serve it"
