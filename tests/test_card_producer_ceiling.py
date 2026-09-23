"""A Card PRODUCER that meets the spend ceiling hands it to the run's deferred-stop sink (review
2026-09-22, the last two `FUNNEL_BACKLOG` rows of `tests/test_containment_census.py`).

The two isolated Card producers — the request-driven build (`speculation.py::_produce_requested_card`)
and the raw-proposal stage (`_prepare_raw_card_stage`) — each wrap their paid work in a blind
`except Exception` that turns ANY fault into a consumed give-up result, so the main task can still
advance the durable gate. That was right for a fault and wrong for the ceiling, which it caught too:
the run's own spend limit became `card_build_done {skipped: "producer_failed"}` — a durable mark that
bars the Card from speculative election for the rest of the run — and the run went on turning until
some OTHER paid call raised the stop. The census found both sites and parked them in its backlog
because the right fix needed a run-level deferred-stop sink, which now exists
(`speculation.py::_eval_budget_stop` / `_raise_deferred_eval_budget_stop`, review ENG2-02).

What holds now, driven through the real producer wrappers and the real Card session:

* the worker lets the ceiling through (`core/containment.py::refuse_budget_stop`), bare or wrapped;
* `_run_isolated_producer` PARKS it — the accountant's own exception, first one wins — owes the
  outer boundary a turn, and stores NO give-up result, so nothing durable blames the Card;
* while a stop is held the session starts no producer and commits no build (its gates read the
  held stop as a terminal intent), closes the open head as `stale`, and hands back;
* the owner raises it: `_raise_deferred_eval_budget_stop`, as it does an evaluation's.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.errors import BudgetExceeded
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_BUILD_DONE
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here: these tests
# admit speculation through the production boundary and then replace only the roles.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _Developer,
    _engine,
    _request,
    _start,
    _without_research,
)

CEILING = ("LLM spend ceiling reached: $1.0031 of the $1.0000 set by `llm_budget_usd`. "
           "The run stops here rather than spending more.")


class _CeilingDeveloper(_Developer):
    """An isolated producer whose build crosses the ceiling (its Developer's provider call raises)."""

    def __init__(self, *, wrapped: bool = False):
        super().__init__()
        self.wrapped = wrapped
        self.stop = BudgetExceeded(CEILING)

    def implement(self, _idea):
        self.calls += 1
        if self.wrapped:                      # the shape a task group inside the Developer produces
            raise BaseExceptionGroup("developer session", [self.stop])
        raise self.stop


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
def test_a_build_producer_that_meets_the_ceiling_parks_it_and_does_not_bar_its_card(
        tmp_path, monkeypatch, wrapped):
    producer = _CeilingDeveloper(wrapped=wrapped)
    engine, _producer = _engine(tmp_path / f"build-ceiling-{wrapped}", producer=producer)
    _start(engine)
    idea = _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)

    async def _bounded_session():
        # BOUNDED: a session that never hands back after the stop is a red test, not a hung suite.
        with anyio.fail_after(20):
            await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(_bounded_session)

    assert producer.calls == 1, "precondition: the producer ran its build"
    events = engine.store.read_all()
    done = [e.data for e in events if e.type == EV_CARD_BUILD_DONE]
    assert not [d for d in done if d.get("skipped") == "producer_failed"], (
        f"the run's spend ceiling was recorded as the Card's producer failure: {done}")
    assert engine._card_requires_serial_fallback(idea.card_id) is False, (
        "the Card is barred from speculative election for a ceiling that was the run's, not its")
    assert engine._eval_budget_stop is producer.stop, "the ceiling was not parked for the owner"
    state = fold(events)
    assert engine._head_request(state) is None, "the head was left open behind the stop"
    assert not state.buildings and not state.pending_nodes(), "a node was built after the stop"
    assert engine._spec_build_inflight == set() and engine._spec_builds == {}
    # …and the owner raises exactly that exception, clearing it
    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(engine._raise_deferred_eval_budget_stop)
    assert caught.value is producer.stop and engine._eval_budget_stop is None


class _CeilingResearcher:
    def __init__(self):
        self.calls = 0
        self.stop = BudgetExceeded(CEILING)

    def propose(self, _state, _parent):
        self.calls += 1
        raise self.stop


def test_a_raw_proposal_that_meets_the_ceiling_stores_no_give_up_and_parks_the_stop(tmp_path):
    engine, producer = _engine(tmp_path / "raw-ceiling")
    _start(engine)
    engine._ensure_speculation_state()
    engine._spec_raw_stage_inflight = True
    events = engine.store.read_all()
    state = fold(events)
    researcher = _CeilingResearcher()

    async def scenario():
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await engine._produce_raw_card_stage(
                {"kind": "draft"}, events, state, 0, (researcher, producer), send)
            assert await receive.receive() == ("raw_proposal", state.search_epoch)

    anyio.run(scenario)

    assert researcher.calls == 1, "precondition: the paid proposal ran"
    assert engine._spec_raw_stage_inflight is False, "the producer slot must still be released"
    assert engine._spec_raw_stage_result is None, (
        "the ceiling was stored as a consumable give-up result")
    assert engine._eval_budget_stop is researcher.stop
    assert engine._eval_boundary_owed is True, "the outer boundary is owed the turn that raises it"


def test_a_held_stop_closes_the_sessions_production_and_commit_gates(tmp_path):
    """The race the gate closes: between the producer parking the stop and the session's next turn
    transferring its boundary debt, a head with no producer and no result must not be closed as
    `producer_failed`, and no new producer may start. A held stop reads as a terminal intent."""
    engine, _producer = _engine(tmp_path / "held-stop-gates")
    _start(engine)
    from looplab.engine.speculation import CardSession

    session = CardSession(max_eval_seconds=None, wall_deadline=None)
    state = fold(engine.store.read_all())
    assert session.open_for_production(engine._session_gates(state, session)), "precondition"
    engine._eval_budget_stop = BudgetExceeded(CEILING)
    gates = engine._session_gates(state, session)
    assert gates.stopping and not session.open_for_production(gates)
    assert not session.open_for_admission(gates)
    engine._eval_budget_stop = None


def test_a_card_mode_run_ends_on_its_producers_ceiling(tmp_path, monkeypatch):
    """End to end through the real `Engine.run`: the run ENDS on the producer's ceiling — the
    accountant's own exception, so the CLI records `run_finished {"reason": "budget_exhausted"}` —
    instead of recording a producer failure and turning on until some other paid call raises."""
    producer = _CeilingDeveloper()
    engine, _producer = _engine(tmp_path / "run-ceiling", producer=producer)
    _start(engine)
    idea = _add_ready_draft(engine)
    _without_research(monkeypatch, engine)

    async def _bounded_run():
        with anyio.fail_after(30):
            return await engine.run()

    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(_bounded_run)

    assert caught.value is producer.stop and producer.calls == 1
    done = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert not [d for d in done if d.get("skipped") == "producer_failed"], done
    assert engine._card_requires_serial_fallback(idea.card_id) is False
    assert engine._eval_budget_stop is None, "a raised stop must not outlive the run that raised it"
