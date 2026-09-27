"""A READY-MADE inject is materialized beside a running Card build, in seconds (doc 68 68.9).

MEASURED on `minionerec-backbones-v10` (2026-09-27, `runs-r/.../events.jsonl`): five `inject_node`
requests whose only file was a finished `MiniOneRec/looplab/experiment.env` (seq 5128-5137,
01:29:37-01:30:27; one more at 01:48:02, seq 5185) — no Developer session needed — were not served
while card-16's speculative build held the producer. `forced_requests.py::_card_phase_serve_operator_
inject` refused them for TWO reasons in turn, and the second is the one the operator could not see:

  1. at `llm_parallel=1` the lane gate `busy >= llm_parallel` (1 >= 1) refused ANY inject beside a
     build, although a ready-made one takes no build lane at all;
  2. after `budget_extend {llm_parallel: 2}` (seq 5209, acked 5210, re-applied live by the control
     watcher) the width gate passed and the NODE-SLOT gate refused: the run had one free slot before
     card-16 was requested (19 = 16 + 3 refunds; ids 0..17 used), card-16's open request OWNED it
     (`_unmaterialized_card_reservations` = 1, so `_node_reservation_slots_remaining` = 0 from seq 5102
     to seq 5218), and the request stayed open after the operator DROPPED card-16 because its build
     was still running (68.7). Re-derived from that log: remaining = 19 - 18 - 1 = 0 at every seq in
     between; the `restart` closed the request (5218) and the first inject became node 18 (5237).

What these pin, on the Card session the incident ran on:
  * a ready-made inject (a repo overlay in `files`, or a script `code`) is served at width 1 while
    the producer's build is still running — and an inject that needs a Developer still waits;
  * the incident's second gate, reproduced: the last slot owned by a live request refuses the inject
    and says why (`operator_request_parked`, `held_by_card_requests: 1`); the DROP of that Card closes
    its request, cancels its build, and the inject takes the freed slot within seconds.
"""
from __future__ import annotations

import time

import anyio
import pytest

import looplab.agents.agent as agent_module
from looplab.engine.node_build import inject_needs_developer
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (EV_BUDGET_EXTEND, EV_CARD_BUILD_DONE, EV_CARD_DROPPED,
                                  EV_INJECT_NODE, EV_OPERATOR_REQUEST_PARKED)
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _engine,
    _request,
    _start,
    _without_research,
)
from tests.test_controls_on_the_fly import _BlockingProducer, _force_watch_tick, _until
from tests.test_dropped_card_build_is_cancelled import _PhasedDeveloper, _long_phase_loop

_IDEA = {"operator": "manual", "params": {"x": 1.0, "y": 1.0},
         "rationale": "operator scaling study: a finished experiment.env"}
_READY_MADE = {
    "files": {"idea": dict(_IDEA),
              "files": {"MiniOneRec/looplab/experiment.env": "BACKBONE=Qwen3.5-2B\nUSERS=300000\n"}},
    "code": {"idea": dict(_IDEA), "code": "print(1)"},
}


def _manual_nodes(engine):
    return [n for n in fold(engine.store.read_all()).nodes.values() if n.operator == "manual"]


def test_the_ready_made_rule_is_one_rule():
    """`_create_injected_node` decides whether to call the Developer with the SAME predicate the lane
    uses to decide whether the inject needs a build lane — two spellings would drift."""
    assert inject_needs_developer({"idea": dict(_IDEA)}) is True
    for req in _READY_MADE.values():
        assert inject_needs_developer(req) is False
    assert inject_needs_developer({"idea": dict(_IDEA), "deleted": ["old.py"]}) is False
    assert inject_needs_developer("not a request") is True       # the conservative side


@pytest.mark.parametrize("shape", sorted(_READY_MADE))
def test_a_ready_made_inject_is_served_beside_a_running_build_at_width_one(
        tmp_path, monkeypatch, shape):
    producer = _BlockingProducer()
    engine, _ = _engine(tmp_path / "run", producer=producer)
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)
    assert engine._llm_parallel == 1, "precondition: one build lane, as v10 ran"
    server = EventStore(engine.store.path)
    observed: dict = {}

    async def operator():
        await _until(producer.started.is_set)
        server.append(EV_INJECT_NODE, dict(_READY_MADE[shape], _command_id="cmd_inject"))
        t0 = time.monotonic()
        await _until(lambda: bool(_manual_nodes(engine)))
        observed["served_after_s"] = time.monotonic() - t0
        observed["producer_still_building"] = not producer.release.is_set()
        producer.release.set()

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(operator)
            with anyio.fail_after(60):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(scenario)

    assert observed["producer_still_building"], (
        "the ready-made inject waited for the producer's build — behind a lane it does not use")
    assert observed["served_after_s"] < 5.0
    state = fold(engine.store.read_all())
    assert state.injects_done == 1
    [node] = _manual_nodes(engine)
    if shape == "files":
        assert node.files == _READY_MADE["files"]["files"], "the overlay is committed as supplied"
    assert producer.calls == 1, "the inject must not have run the Developer"


def test_an_inject_that_needs_a_developer_still_waits_for_a_build_lane(tmp_path, monkeypatch):
    """The width gate is unchanged for the paid kind: at width 1 with the producer building, an inject
    with no code of its own waits (and the session hands it on once the lane frees)."""
    producer = _BlockingProducer()
    engine, _ = _engine(tmp_path / "run", producer=producer)
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)
    server = EventStore(engine.store.path)
    observed: dict = {}

    async def operator():
        await _until(producer.started.is_set)
        server.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
        await anyio.sleep(1.0)
        observed["injects_done_while_building"] = fold(engine.store.read_all()).injects_done
        producer.release.set()

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(operator)
            with anyio.fail_after(60):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(scenario)
    assert observed["injects_done_while_building"] == 0


def test_the_v10_sequence_a_dropped_builds_slot_goes_to_the_queued_inject(tmp_path, monkeypatch):
    """The incident's exact order: ready-made injects queued while a Card build owns the run's LAST
    node slot; the operator raises `llm_parallel` to 2 (the watcher applies it live) and the inject
    still cannot be served — the slot, not the lane, is what refuses it, and the park is SAID with the
    slot the open request holds. Then the operator drops the Card: its request closes at once, its
    build is cancelled, and the inject takes the slot."""
    developer = _PhasedDeveloper()
    monkeypatch.setattr(agent_module, "drive_tool_loop", _long_phase_loop(developer))
    engine, _ = _engine(tmp_path / "run", producer=developer)
    _start(engine)
    engine._base_max_nodes = 1            # exactly ONE free node slot before the Card is requested
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)
    assert engine._node_reservation_slots_remaining(fold(engine.store.read_all())) == 0, (
        "precondition: the open request owns the last slot")
    server = EventStore(engine.store.path)
    observed: dict = {}

    async def operator():
        await _until(developer.first_phase.is_set)
        server.append(EV_INJECT_NODE, dict(_READY_MADE["files"], _command_id="cmd_inject"))
        server.append(EV_BUDGET_EXTEND, {"llm_parallel": 2, "_command_id": "cmd_width"})
        assert _force_watch_tick(engine) is True
        observed["width"] = engine._llm_parallel
        await _until(lambda: any(e.type == EV_OPERATOR_REQUEST_PARKED
                                 for e in engine.store.read_all()))
        await anyio.sleep(1.0)
        observed["served_before_drop"] = fold(engine.store.read_all()).injects_done
        observed["parked"] = [e.data for e in engine.store.read_all()
                              if e.type == EV_OPERATOR_REQUEST_PARKED]
        server.append(EV_CARD_DROPPED, {"id": "card-7", "reason": "operator: do not rebuild",
                                        "dropped_by": "operator"})
        t0 = time.monotonic()
        await _until(lambda: bool(_manual_nodes(engine)))
        observed["served_after_drop_s"] = time.monotonic() - t0

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(operator)
            with anyio.fail_after(60):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(scenario)

    assert observed["width"] == 2, "the live llm_parallel was not applied"
    assert observed["served_before_drop"] == 0, "precondition: the slot, not the lane, refused it"
    [parked] = observed["parked"]
    assert (parked["request"], parked["idx"]) == ("inject", 0)
    assert (parked["held_by_card_requests"], parked["limit"]) == (1, 1), parked
    assert "held by open Card build request" in parked["detail"]
    assert observed["served_after_drop_s"] < 5.0, (
        "the dropped Card's request kept the slot while its build ran on (the v10 32 minutes)")
    [done] = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert (done.get("skipped_reason"), done.get("producer_cancelled")) == ("card_dropped", True)
    assert developer.phases_started == ["Developer·stages"], developer.phases_started
    state = fold(engine.store.read_all())
    assert state.injects_done == 1 and len(_manual_nodes(engine)) == 1
