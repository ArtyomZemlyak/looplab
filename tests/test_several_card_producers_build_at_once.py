"""A Card-mode run builds as many nodes at once as its build width says — not one, whatever it is given.

Measured 2026-09-24 on a one-GPU MiniOneRec run (Card selection, speculation depth 1,
`parallel_build: 2`): in 16 runs no two builds ever overlapped. Builds took 40 min–3 h one after
another against 48-second evaluations, and the GPU was busy 0.74% of 12.7 h. The speculative Card
session had exactly ONE producer — one leased pair, one open request, a positional close — so the
documented "concurrent node BUILDS" axis (`llm_parallel`) never reached it.

What stays exactly as it was: the freshness gate (a prefetch it rejects is still discarded, and the
freed producer takes the next idea), the inventory ceiling `min(depth, card_lane_width)`, and every
log written before a close could name its request `index`.
"""
from __future__ import annotations

import threading
import types

import anyio
import pytest

from looplab.core.models import Idea, NodeStatus
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (
    EV_CARD_BUILD_DONE, EV_CARD_BUILD_REQUESTED, EV_NODE_BUILDING, EV_NODE_EVALUATED,
)
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _commit_speculative_node,
    _engine,
    _Researcher,
    _start,
    _without_research,
)


# ------------------------------------------------------------ the fold: an indexed close

def _queue(tmp_path, rows):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    for card in ("card-0", "card-1", "card-2"):
        store.append(EV_CARD_BUILD_REQUESTED, {"card_id": card, "generation": 0})
    for row in rows:
        store.append(EV_CARD_BUILD_DONE, row)
    return fold(store.read_all())


def _stale(card, **extra):
    return {"card_id": card, "generation": 0, "skipped": "stale", **extra}


def test_a_close_naming_a_later_position_leaves_the_head_open(tmp_path):
    state = _queue(tmp_path, [_stale("card-1", index=1)])
    assert state.card_builds_done == 0
    assert state.card_builds_done_ahead == [1]


def test_closing_the_head_advances_over_everything_already_closed_behind_it(tmp_path):
    state = _queue(tmp_path, [_stale("card-1", index=1), _stale("card-0")])
    assert state.card_builds_done == 2 and state.card_builds_done_ahead == []


def test_outcomes_stay_in_position_order_whatever_order_the_closes_came_in(tmp_path):
    rows = [{"card_id": "card-2", "generation": 0, "skipped": "producer_failed", "index": 2},
            _stale("card-0"), _stale("card-1")]
    state = _queue(tmp_path, rows)
    assert state.card_builds_done == 3
    assert state.card_build_outcomes == ["stale", "stale", "producer_failed"]


@pytest.mark.parametrize("row", [
    _stale("card-1", index=0),          # the index names another request
    _stale("card-1", index=7),          # no such position
    _stale("card-1", index="1"),        # not an int
    _stale("card-1", index=-1),
])
def test_a_close_that_does_not_name_its_own_open_position_is_inert(tmp_path, row):
    state = _queue(tmp_path, [row])
    assert state.card_builds_done == 0 and state.card_builds_done_ahead == []
    assert state.card_build_outcomes == []


def test_a_position_cannot_be_closed_twice(tmp_path):
    state = _queue(tmp_path, [_stale("card-1", index=1), _stale("card-1", index=1)])
    assert state.card_builds_done_ahead == [1] and state.card_build_outcomes == ["stale"]


def test_a_log_without_indexed_closes_folds_exactly_as_before(tmp_path):
    state = _queue(tmp_path, [_stale("card-0"), _stale("card-2"), _stale("card-1")])
    # positional: card-2 does not match head card-1 and is inert, then card-1 closes the head
    assert state.card_builds_done == 2 and state.card_builds_done_ahead == []
    assert state.card_build_outcomes == ["stale", "stale"]


# ------------------------------------------------------------ which setting is the width

def _width_host(*, auto, live, launched, pin=0, overrides=None):
    from looplab.engine.speculation import SpeculationMixin
    host = types.SimpleNamespace(_llm_parallel_startup_auto=auto, _llm_parallel=live,
                                 _llm_parallel_launched=launched)
    state = types.SimpleNamespace(budget_overrides=overrides or {}, llm_parallel=pin)
    return SpeculationMixin._speculative_producer_width(host, state)


def test_an_auto_build_width_keeps_one_producer():
    assert _width_host(auto=True, live=4, launched=4) == 1


def test_a_spelled_width_is_the_producer_count():
    assert _width_host(auto=False, live=2, launched=2, pin=2) == 2


def test_the_strategist_cannot_widen_past_what_the_run_launched_with():
    assert _width_host(auto=False, live=8, launched=2, pin=2) == 2


def test_a_narrowed_width_narrows_the_producers():
    assert _width_host(auto=False, live=1, launched=2, pin=2) == 1


def test_the_operator_may_widen_it():
    assert _width_host(auto=False, live=4, launched=2, pin=2,
                       overrides={"llm_parallel": 4}) == 4


# ------------------------------------------------------------ the raw lane beside running builds

def _fence_host(requests):
    from looplab.engine.card_reservation import CardReservationMixin
    from looplab.engine.speculation import SpeculationMixin

    class _Host(CardReservationMixin, SpeculationMixin):
        pass

    host = _Host()
    proposal_state = types.SimpleNamespace(
        card_build_requests=[{"card_id": c, "generation": 0} for c in requests],
        card_builds_done=0, card_builds_done_ahead=[])
    return host, proposal_state


def _building(node_id, card, speculative=True):
    return types.SimpleNamespace(type=EV_NODE_BUILDING, data={
        "node_id": node_id, "card_id": card, "card_build_generation": 0,
        "speculative": speculative})


def _now(nodes):
    return types.SimpleNamespace(nodes={n: None for n in nodes})


def test_a_known_build_committing_does_not_void_a_raw_proposal():
    host, proposal = _fence_host(["card-1"])
    events = [_building(5, "card-1")]
    assert host._ceiling_moved_only_by_known_builds(events, _now([5]), proposal, 5) is True


@pytest.mark.parametrize("events", [
    [_building(5, "card-9")],                         # a build requested AFTER the proposal
    [_building(5, "card-1", speculative=False)],      # a serial build
    [_building(5, "card-1"), _building(6, "card-9")],  # a known one AND an unknown one
])
def test_any_other_mover_still_voids_it(events):
    host, proposal = _fence_host(["card-1"])
    ids = [event.data["node_id"] for event in events]
    assert host._ceiling_moved_only_by_known_builds(events, _now(ids), proposal, 5) is False


def test_with_nothing_in_flight_at_proposal_time_the_fence_is_unchanged():
    host, proposal = _fence_host([])
    assert host._ceiling_moved_only_by_known_builds(
        [_building(5, "card-1")], _now([5]), proposal, 5) is False


# ------------------------------------------------------------ driven: a real Card session

class _GatedDeveloper:
    """A pooled Developer whose build holds until the test releases it."""

    gate = False

    def __init__(self, log):
        self.log = log
        self.started = threading.Event()
        self.release = threading.Event()
        self.last_files: dict[str, str] = {}
        self.last_deleted: list[str] = []

    def implement(self, idea: Idea) -> str:
        self.log.append(("start", idea.card_id, self))
        self.started.set()
        if _GatedDeveloper.gate and not self.release.wait(timeout=20):
            raise RuntimeError("the test never released this build")
        self.log.append(("done", idea.card_id, self))
        return "print(1)"


def _two_producer_engine(tmp_path, monkeypatch, *, width):
    engine, _unused = _engine(tmp_path / f"w{width}", depth=1)
    log: list = []
    developers: list[_GatedDeveloper] = []

    def factory():
        developer = _GatedDeveloper(log)
        developers.append(developer)
        return _Researcher(), developer

    engine.role_factory = factory
    engine._llm_parallel = width
    engine._llm_parallel_launched = width
    engine._llm_parallel_startup_auto = False
    engine._eval_parallel = 4
    _without_research(monkeypatch, engine)
    _start(engine)
    _add_ready_draft(engine, "card-0", x=0.1)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.3)
    _GatedDeveloper.gate = False
    consumer = _commit_speculative_node(engine)   # the running eval that makes prefetch pay
    _GatedDeveloper.gate = True

    release_evals = threading.Event()

    async def _held_eval(node_id, _limiter, _max_es):
        await anyio.to_thread.run_sync(release_evals.wait, 30)
        node = fold(engine.store.read_all()).nodes[node_id]
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id, "generation": node.attempt, "metric": 0.0, "eval_seconds": 0.0})

    monkeypatch.setattr(engine, "_evaluate", _held_eval)
    return engine, log, developers, consumer, release_evals


async def _outer_loop(engine, until):
    """What `Engine._run_with_llm_broker` does around sessions, reduced to the part at issue: enter a
    session, and after it hands back pay the boundary (the cadence pass clears the debt) and enter
    the next one — until the test's condition holds."""
    while not until():
        await engine._run_card_session([], fold(engine.store.read_all()), None)
        engine._card_boundary_debt = False
        await anyio.sleep(0.02)


def _live(log):
    live: set = set()
    for what, card, _developer in list(log):
        (live.add if what == "start" else live.discard)(card)
    return len(live)


def _building_now(log):
    live, peak = set(), 0
    for what, card, _developer in log:
        (live.add if what == "start" else live.discard)(card)
        peak = max(peak, len(live))
    return peak


def test_two_producers_build_at_once_and_the_later_request_may_commit_first(tmp_path, monkeypatch):
    engine, log, developers, consumer, release_evals = _two_producer_engine(
        tmp_path, monkeypatch, width=2)

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            done = anyio.Event()
            async with anyio.create_task_group() as tg:
                tg.start_soon(_outer_loop, engine, done.is_set)
                with anyio.fail_after(20):
                    while _live(log) < 2:
                        await anyio.sleep(0.02)
                building = [developer for what, _card, developer in log if what == "start"][-2:]
                # Release the build that started LAST — the later request — first.
                building[-1].release.set()
                with anyio.fail_after(20):
                    while not any(e.type == EV_CARD_BUILD_DONE and "index" in e.data
                                  for e in engine.store.read_all()):
                        await anyio.sleep(0.02)
                for developer in building:
                    developer.release.set()
                with anyio.fail_after(20):
                    while len(fold(engine.store.read_all()).speculative_nodes) < 3:
                        await anyio.sleep(0.02)
                release_evals.set()
                done.set()
                tg.cancel_scope.cancel()

    anyio.run(scenario)

    assert _building_now(log) == 2, "the two builds never overlapped"
    used = {id(developer) for _what, _card, developer in log}
    assert len(used) == 2 and id(engine.developer) not in used, (
        "each concurrent build runs on its own pooled pair, never the primary")
    state = fold(engine.store.read_all())
    assert state.card_builds_done == len(state.card_build_requests) == 3
    assert state.card_builds_done_ahead == []
    assert state.card_build_outcomes == ["committed"] * 3
    assert state.card_build_producer_failed == []
    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert [("index" in close) for close in closes] == [False, True, False], (
        "only the close that overtook the head names its position", closes)
    assert all(state.nodes[n].status is NodeStatus.evaluated for n in state.speculative_nodes)
    assert consumer in state.speculative_nodes


def test_at_width_one_the_second_build_waits_for_the_first(tmp_path, monkeypatch):
    engine, log, developers, _consumer, release_evals = _two_producer_engine(
        tmp_path, monkeypatch, width=1)

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            async with anyio.create_task_group() as tg:
                tg.start_soon(engine._run_card_session, [], fold(engine.store.read_all()), None)
                with anyio.fail_after(20):
                    while _live(log) < 1:
                        await anyio.sleep(0.02)
                await anyio.sleep(0.5)
                assert _building_now(log) == 1
                for _ in range(2):
                    for developer in developers:
                        developer.release.set()
                    await anyio.sleep(0.3)
                with anyio.fail_after(20):
                    while len(fold(engine.store.read_all()).speculative_nodes) < 2:
                        for developer in developers:
                            developer.release.set()
                        await anyio.sleep(0.05)
                release_evals.set()
                tg.cancel_scope.cancel()

    anyio.run(scenario)
    assert _building_now(log) == 1
    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert not any("index" in close for close in closes), "one producer closes positionally"


# ------------------------------------------------------------ the boundary no longer discards a build

def test_a_build_held_at_the_boundary_is_decided_by_the_next_sessions_claim(tmp_path, monkeypatch):
    """Width 1: the eval terminal owes the outer loop a turn while the build still runs. The session
    waits the build out (it is not adopted at width 1), keeps its result, hands back — and the NEXT
    session's claim decides it. Until 2026-09-24 it was closed `commit_not_allowed` right there."""
    engine, log, developers, consumer, release_evals = _two_producer_engine(
        tmp_path, monkeypatch, width=1)

    async def _terminal_eval(node_id, _limiter, _max_es):
        node = fold(engine.store.read_all()).nodes[node_id]
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id, "generation": node.attempt, "metric": 0.0, "eval_seconds": 0.0})

    monkeypatch.setattr(engine, "_evaluate", _terminal_eval)

    held: list = []

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            async with anyio.create_task_group() as tg:
                tg.start_soon(engine._run_card_session, [], fold(engine.store.read_all()), None)
                with anyio.fail_after(20):
                    while _live(log) < 1:
                        await anyio.sleep(0.02)
                    # the consumer's eval lands while the build is still running
                    while not any(e.type == EV_NODE_EVALUATED for e in engine.store.read_all()):
                        await anyio.sleep(0.02)
                for developer in developers:
                    developer.release.set()
            # first session returned: the result is held, its request still open
            state = fold(engine.store.read_all())
            assert not [e for e in engine.store.read_all()
                        if e.type == EV_CARD_BUILD_DONE and e.data.get("skipped_reason")
                        == "commit_not_allowed"]
            assert engine._spec_builds and engine._card_boundary_debt is True
            assert len(engine._outstanding_requests(state)) == 1
            held.extend(engine._spec_builds)
            engine._card_boundary_debt = False            # the outer cadence pass
            with anyio.fail_after(20):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(scenario)
    (card_id, generation), = held
    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE
              and e.data["card_id"] == card_id and e.data["generation"] == generation]
    assert len(closes) == 1, closes
    # decided by the claim — committed, or refused by the claim's own re-check — never discarded
    assert "node_id" in closes[0] or closes[0].get("skipped_reason") in {
        "not_selected_now", "idea_changed", "card_action_changed"}, closes


def test_a_wide_session_hands_back_while_its_build_still_runs(tmp_path, monkeypatch):
    """Width 2: the build is ADOPTED, so a session owed the outer boundary returns at once instead of
    holding the Strategist and every cadence behind hours of Developer work; the next session
    commits the build when it lands."""
    engine, log, developers, consumer, release_evals = _two_producer_engine(
        tmp_path, monkeypatch, width=2)

    async def _terminal_eval(node_id, _limiter, _max_es):
        node = fold(engine.store.read_all()).nodes[node_id]
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id, "generation": node.attempt, "metric": 0.0, "eval_seconds": 0.0})

    monkeypatch.setattr(engine, "_evaluate", _terminal_eval)
    returned_while_building = []

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            with anyio.fail_after(20):
                await engine._run_card_session([], fold(engine.store.read_all()), None)
            returned_while_building.append(len(engine._adopted_producers()))
            assert engine._adopted_producers(), "the build outlives the session that started it"
            for developer in developers:
                developer.release.set()
            with anyio.fail_after(20):
                while engine._adopted_producers():
                    await anyio.sleep(0.02)
            engine._card_boundary_debt = False
            with anyio.fail_after(20):
                await _outer_loop(engine, lambda: not engine._outstanding_requests(
                    fold(engine.store.read_all())))

    anyio.run(scenario)
    assert returned_while_building and returned_while_building[0] >= 1
    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert not any(c.get("skipped_reason") == "commit_not_allowed" for c in closes), closes


def test_a_build_on_a_retired_developer_is_closed_not_committed(tmp_path, monkeypatch):
    engine, log, developers, consumer, release_evals = _two_producer_engine(
        tmp_path, monkeypatch, width=2)

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            done = anyio.Event()
            async with anyio.create_task_group() as tg:
                tg.start_soon(_outer_loop, engine, done.is_set)
                with anyio.fail_after(20):
                    while _live(log) < 1:
                        await anyio.sleep(0.02)
                engine._drop_producer_pool()             # the Strategist swapped the backend
                for developer in developers:
                    developer.release.set()
                with anyio.fail_after(20):
                    while not any(e.type == EV_CARD_BUILD_DONE
                                  and e.data.get("skipped_reason") == "builder_replaced"
                                  for e in engine.store.read_all()):
                        for developer in developers:
                            developer.release.set()
                        await anyio.sleep(0.05)
                release_evals.set()
                done.set()
                tg.cancel_scope.cancel()

    anyio.run(scenario)
    replaced = [e.data for e in engine.store.read_all()
                if e.type == EV_CARD_BUILD_DONE and e.data.get("skipped_reason") == "builder_replaced"]
    assert replaced and all("node_id" not in r for r in replaced)
