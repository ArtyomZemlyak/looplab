"""An operator `inject_node` RESERVES before it spends its receipt (review 2026-09-22, ENG1-07).

THE DEFECT. `_serve_forced_requests` appended `inject_done` FIRST ("CLAIM THE REQUEST BEFORE THE PAID
PRODUCER") and only then ran `_create_injected_node`, whose first act was `_reserve_node_build`. A
reservation that returns None — a pause landing, a slot taken, the proposal-authority CAS lost —
raised "injected idea could not reserve one exact native Card", which the branch recorded as
`inject_failed{reason: materialization_failed}`. The operator's request was SPENT although nothing
had been paid and nothing built. Driven on the base tree (a pause, and separately a concurrent
reservation taking the last slot, each landing inside the reservation's window):

    paused     injects_done=1 inject_failed=['materialization_failed'] nodes=0
    slot_race  injects_done=1 inject_failed=['materialization_failed'] nodes=0

THE FIX. Reserve first (the main task; the Card + `node_building`; no money), then the receipt, then
the paid Developer half through `_offload_build`. What the receipt-first order protected still
holds, and each gap is driven here with a kill that escapes every `except Exception`:
  * a crash BETWEEN the reservation and the receipt leaves a bare `node_building`, which resume's
    `_recover_interrupted_builds` closes; the still-queued request is then served exactly once, on a
    fresh id and Card — no duplicate node, no second live Card;
  * a crash AFTER the receipt, inside the paid session, never re-buys that session.

THE RULE FOR A REFUSAL, now that the request is still queued when one happens: `_reserve_node_build`
names WHY (`card_reservation.py::RESERVATION_REFUSALS`). A RACE leaves the request queued, because
the next turn's own gate re-decides it; a VERDICT on the idea spends it with `inject_failed` naming
the code — and it must, since retrying the same bytes would re-enter this head every turn, forever.
Every code is driven below, and so is the "does not spin" half of each verdict.
"""
from __future__ import annotations

import ast

import anyio
import pytest

from looplab.core.models import Idea
from looplab.engine.card_reservation import (RESERVATION_RACES, RESERVATION_REFUSALS,
                                             RESERVATION_VERDICTS, CardReservationMixin,
                                             _CardReservationPlan)
from looplab.events.eventstore import EventStoreConcurrencyError
from looplab.events.replay import fold
from looplab.events.types import (EV_BUDGET_EXTEND, EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_HINT,
                                  EV_INJECT_DONE, EV_INJECT_FAILED, EV_INJECT_NODE, EV_NODE_BUILDING,
                                  EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED,
                                  EV_NOVELTY_REJECTED, EV_PAUSE, EV_RESUME)
from tests._source_scan import function_tree
from tests.factories import make_engine

_IDEA = {"operator": "manual", "params": {"x": 1.0}, "rationale": "operator idea"}


class _ProcessDied(BaseException):
    """A kill: it escapes every `except Exception`, which is what a process's death does."""


def _engine(tmp_path, *, max_nodes: int = 3, **overrides):
    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=max_nodes, **overrides)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
    return engine


def _serve(engine) -> bool:
    return anyio.run(engine._serve_forced_requests, fold(engine.store.read_all()))


def _kinds(engine) -> list[str]:
    return [event.type for event in engine.store.read_all()]


def _manual_nodes(state) -> list:
    return [node for node in state.nodes.values() if node.operator == "manual"]


def _race_in_the_reservation_window(engine, race) -> None:
    """Land `race` INSIDE the first reservation's window, then let the REAL reservation decide."""
    real = engine._reserve_node_build
    fired: list[bool] = []

    def _racing(*args, **kwargs):
        if not fired:
            fired.append(True)
            race(engine)
        return real(*args, **kwargs)

    engine._reserve_node_build = _racing


def _assert_still_queued(engine) -> None:
    kinds = _kinds(engine)
    assert fold(engine.store.read_all()).injects_done == 0, (
        "the request was SPENT by a reservation that lost a race — nothing was paid or built")
    assert EV_INJECT_DONE not in kinds and EV_INJECT_FAILED not in kinds


# ------------------------------------------------------------------ a race leaves it queued

def test_a_pause_landing_in_the_reservation_window_leaves_the_request_queued(tmp_path):
    engine = _engine(tmp_path)
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    _race_in_the_reservation_window(
        engine, lambda e: e.store.append(EV_PAUSE, {"reason": "operator paused"}))

    assert _serve(engine) is True
    _assert_still_queued(engine)
    assert fold(engine.store.read_all()).paused
    assert EV_CARD_ADDED not in _kinds(engine) and EV_NODE_BUILDING not in _kinds(engine), (
        "a refused reservation must leave nothing reserved")

    # The operator resumes, and the SAME request is served — once.
    engine.store.append(EV_RESUME, {})
    assert _serve(engine) is True
    state = fold(engine.store.read_all())
    assert state.injects_done == 1 and len(_manual_nodes(state)) == 1
    assert EV_INJECT_FAILED not in _kinds(engine)
    assert _serve(engine) is False, "a served request must not be served again"


def test_a_slot_taken_in_the_reservation_window_waits_for_budget_instead_of_spending(
        tmp_path, monkeypatch):
    engine = _engine(tmp_path, max_nodes=1)
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    # A concurrent reservation takes the run's only slot inside the window.
    _race_in_the_reservation_window(engine, lambda e: e.store.append(
        EV_NODE_BUILDING, {"node_id": 0, "operator": "draft", "parent_ids": []}))

    assert _serve(engine) is True
    _assert_still_queued(engine)

    # The next turn holds it in the ordinary bounded budget wait — still unspent, not a spin.
    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(anyio, "sleep", _sleep)
    assert _serve(engine) is True
    assert sleeps, "the next turn must wait for budget, not re-try the reservation"
    _assert_still_queued(engine)

    # An operator's budget extension admits it exactly once.
    engine.store.append(EV_BUDGET_EXTEND, {"add_nodes": 1})
    assert _serve(engine) is True
    state = fold(engine.store.read_all())
    assert state.injects_done == 1 and len(_manual_nodes(state)) == 1


def test_a_proposal_authority_cas_lost_in_the_window_leaves_the_request_queued(tmp_path):
    """The third race the finding names: an authority-bearing row (an operator hint) lands between
    the reservation's plan and its CAS append, the append loses, and the retry sees the authority
    moved. The REAL `_reserve_node_build` refuses — nothing is stubbed but the timing."""
    engine = _engine(tmp_path)
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    real_plan = engine._plan_native_card
    fired: list[bool] = []

    def _racing_plan(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        if not fired:
            fired.append(True)
            engine.store.append(EV_HINT, {"text": "try a smaller x"})
        return plan

    engine._plan_native_card = _racing_plan
    assert _serve(engine) is True
    _assert_still_queued(engine)

    assert _serve(engine) is True
    state = fold(engine.store.read_all())
    assert state.injects_done == 1 and len(_manual_nodes(state)) == 1


# ------------------------------------------------------------------ a verdict spends it, once

def test_a_card_contract_verdict_spends_the_request_once_and_does_not_spin(tmp_path):
    """A statement the Card contract cannot own (here: a multi-line rationale) is refused the same
    way on every turn. Left queued it would re-enter this head forever — and append one more
    `novelty_rejected` receipt per turn — so it is spent, naming the code."""
    engine = _engine(tmp_path)
    engine.store.append(EV_INJECT_NODE, {"idea": {"operator": "manual", "params": {"x": 1.0},
                                                  "rationale": "line one\nline two"}})
    assert _serve(engine) is True
    events = engine.store.read_all()
    assert [e.data["reason"] for e in events if e.type == EV_INJECT_FAILED] == ["card_contract"]
    assert [e.data.get("skipped") for e in events if e.type == EV_INJECT_DONE] == ["card_contract"]
    assert fold(events).injects_done == 1
    assert _serve(engine) is False, "a spent verdict must not be re-served"
    receipts = [e for e in engine.store.read_all()
                if e.type == EV_NOVELTY_REJECTED and e.data.get("kind") == "card_contract"]
    assert len(receipts) == 1, f"{len(receipts)} contract receipts for one refused request"


def test_an_identical_inject_while_its_twin_is_in_flight_is_spent_as_a_duplicate(tmp_path):
    """Waiting for the twin is not an option: the forced-request head is served before the eval
    dispatch the twin itself needs, so a queued duplicate would hold its own owner hostage."""
    engine = _engine(tmp_path)
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    assert _serve(engine) is True          # built; pending evaluation — its Card is in flight
    assert _serve(engine) is True          # the identical twin
    events = engine.store.read_all()
    assert [e.data["reason"] for e in events if e.type == EV_INJECT_FAILED] == ["card_duplicate"]
    state = fold(events)
    assert state.injects_done == 2 and len(_manual_nodes(state)) == 1
    assert _serve(engine) is False


@pytest.mark.parametrize("budget_full", [False, True])
def test_a_parent_set_no_build_action_carries_is_refused_by_the_validator(
        tmp_path, monkeypatch, budget_full):
    """The rule's other half. A `stale_parents` refusal leaves the request queued BECAUSE the next
    turn's validator re-decides it — which is only true if the validator checks the reservation's
    exact build action. A repeated parent id passed every check `_prepare_injected_node` had and
    failed the snapshot on every turn: with the budget free, that is a spin; with it full, the
    request waited for capacity it could never use. Refused up front, before the budget wait."""
    engine = _engine(tmp_path, max_nodes=1 if budget_full else 3)
    engine.store.append(EV_NODE_CREATED, {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 0.0}}, "code": "c"})
    engine.store.append(EV_NODE_EVALUATED, {"node_id": 0, "generation": 0, "metric": 1.0})
    engine.store.append(EV_INJECT_NODE, {"idea": {"operator": "improve", "params": {"x": 0.1}},
                                         "parent_ids": [0, 0]})
    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(anyio, "sleep", _sleep)
    assert _serve(engine) is True
    events = engine.store.read_all()
    assert [e.data["reason"] for e in events if e.type == EV_INJECT_FAILED] == ["invalid_request"]
    assert sleeps == [], "refused before the budget wait, not after it"
    assert EV_NODE_BUILDING not in _kinds(engine)
    assert _serve(engine) is False, "the refused request must not be re-served every turn"


# ------------------------------------------------------------------ a crash at each gap

def test_a_crash_between_the_reservation_and_the_receipt_re_serves_the_request_once(tmp_path):
    run_dir = tmp_path / "run"
    engine = make_engine(run_dir, n_seeds=1, max_nodes=3)
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    real_append = engine.store.append

    def _dies_at_the_receipt(event_type, data=None, **kwargs):
        if event_type == EV_INJECT_DONE:
            raise _ProcessDied("killed between the reservation and the receipt")
        return real_append(event_type, data, **kwargs)

    engine.store.append = _dies_at_the_receipt
    with pytest.raises(BaseException):
        anyio.run(engine.run)
    died = engine.store.read_all()
    assert EV_NODE_BUILDING in [e.type for e in died], "precondition: the reservation was durable"
    assert fold(died).injects_done == 0, "precondition: the receipt was not"

    resumed = make_engine(run_dir, n_seeds=1, max_nodes=3)
    state = anyio.run(resumed.run)
    events = resumed.store.read_all()
    assert state.injects_done == 1
    assert len([e for e in events if e.type == EV_INJECT_DONE]) == 1
    assert not [e for e in events if e.type == EV_INJECT_FAILED]
    manual = _manual_nodes(state)
    assert len(manual) == 1, f"{len(manual)} nodes for one operator request"
    # One live Card for the request; the interrupted reservation's own Card is CLOSED, not a twin.
    operator_cards = [e.data["id"] for e in events
                      if e.type == EV_CARD_ADDED and e.data.get("source") == "operator"]
    dropped = {e.data["id"]: e.data.get("reason") for e in events if e.type == EV_CARD_AUTO_DROPPED}
    live = [card for card in operator_cards if card not in dropped]
    assert live == [manual[0].idea.card_id]
    assert [dropped[card] for card in operator_cards if card in dropped] == ["build_interrupted"]


def test_a_crash_inside_the_paid_session_after_the_receipt_never_re_buys_it(tmp_path):
    """What the receipt-first comment protects, still protected: the receipt precedes the PAID
    half, so a death inside the Developer session loses one queued intent at most — it is never
    bought twice."""
    run_dir = tmp_path / "run"
    bought: list[str] = []

    class _DyingDeveloper:
        is_code_generating = False

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def implement(self, idea):
            if idea.operator == "manual":
                bought.append(idea.operator)
                if len(bought) == 1:
                    raise _ProcessDied("killed inside the paid Developer session")
            return self._real.implement(idea)

    first = make_engine(run_dir, n_seeds=1, max_nodes=3)
    first.developer = _DyingDeveloper(first.developer)
    first.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})
    with pytest.raises(BaseException):
        anyio.run(first.run)
    assert fold(first.store.read_all()).injects_done == 1, "precondition: the receipt was spent"

    resumed = make_engine(run_dir, n_seeds=1, max_nodes=3)
    resumed.developer = _DyingDeveloper(resumed.developer)
    state = anyio.run(resumed.run)
    assert bought == ["manual"], f"the paid session was bought {len(bought)} times"
    assert state.injects_done == 1 and not _manual_nodes(state)
    events = resumed.store.read_all()
    interrupted = [e for e in events if e.type == EV_NODE_FAILED
                   and e.data.get("reason") == "build_interrupted"]
    assert len(interrupted) == 1, "the orphan reservation must be closed on resume"


def test_a_request_the_card_writer_cannot_represent_is_spent_once_and_a_bug_surfaces(tmp_path):
    """The FREE half contains exactly the validation-shaped triple `_plan_native_card` contains:
    a hostile row the validator admitted is spent with its diagnosis (no crash-loop on a durable
    queue head), while any other fault is a bug and propagates, as on the serial build path."""
    engine = _engine(tmp_path)
    engine.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})

    def _unrepresentable(*_args, **_kwargs):
        raise ValueError("the Card writer cannot represent this idea")

    engine._plan_native_card = _unrepresentable
    assert _serve(engine) is True
    events = engine.store.read_all()
    failed = [e.data for e in events if e.type == EV_INJECT_FAILED]
    assert [row["reason"] for row in failed] == ["materialization_failed"]
    assert "cannot represent" in failed[0]["error"]
    assert fold(events).injects_done == 1 and EV_NODE_BUILDING not in _kinds(engine)
    assert _serve(engine) is False

    buggy = _engine(tmp_path / "bug")
    buggy.store.append(EV_INJECT_NODE, {"idea": dict(_IDEA)})

    def _bug(*_args, **_kwargs):
        raise RuntimeError("a defect in the reservation")

    buggy._plan_native_card = _bug
    with pytest.raises(RuntimeError, match="defect"):
        _serve(buggy)
    assert fold(buggy.store.read_all()).injects_done == 0, "a bug must not spend the request"


def test_a_direct_call_still_reserves_in_place_and_raises_when_refused(tmp_path):
    """`_create_injected_node(req)` without a reservation keeps its historical contract — tests
    and direct callers prepare and reserve through it, and a refusal is a raise."""
    engine = _engine(tmp_path)
    engine.store.append(EV_PAUSE, {"reason": "operator paused"})
    with pytest.raises(ValueError, match="could not reserve"):
        engine._create_injected_node({"idea": dict(_IDEA)})
    engine.store.append(EV_RESUME, {})
    engine._create_injected_node({"idea": dict(_IDEA)})
    assert len(_manual_nodes(fold(engine.store.read_all()))) == 1


# ------------------------------------------------------------------ every refusal names itself

def _idea(**overrides) -> Idea:
    return Idea(**{"operator": "draft", "params": {"x": 0.5}, "rationale": "a trial", **overrides})


def _reserve(engine, action=None, idea=None, **kwargs) -> list[str]:
    refusal: list[str] = []
    assert engine._reserve_node_build(action or {"kind": "draft"}, idea or _idea(),
                                      refusal=refusal, **kwargs) is None
    return refusal


def _halted(engine):
    engine.store.append(EV_PAUSE, {"reason": "operator paused"})
    return _reserve(engine)


def _no_slot(engine):
    engine.store.append(EV_NODE_BUILDING, {"node_id": 0, "operator": "draft", "parent_ids": []})
    engine.store.append(EV_NODE_BUILDING, {"node_id": 1, "operator": "draft", "parent_ids": []})
    engine.store.append(EV_NODE_BUILDING, {"node_id": 2, "operator": "draft", "parent_ids": []})
    return _reserve(engine)


def _stale_parents(engine):
    return _reserve(engine, {"kind": "improve", "parent_ids": [7]})


def _stale_anchor(engine):
    return _reserve(engine, scored_against=5)          # no node 5: the anchor is not scorable


def _card_contract(engine):
    return _reserve(engine, idea=_idea(rationale="line one\nline two"))


def _card_duplicate(engine):
    assert engine._reserve_node_build({"kind": "draft"}, _idea()) is not None
    return _reserve(engine)


def _card_rebind(engine):
    return _reserve(engine, idea=_idea(card_id="card-77"))


def _card_unplannable(engine):
    engine._plan_native_card = lambda *a, **k: _CardReservationPlan("mystery", None, None, None)
    return _reserve(engine)


def _authority_moved(engine):
    real_plan = engine._plan_native_card

    def _racing_plan(*args, **kwargs):
        engine._plan_native_card = real_plan
        plan = real_plan(*args, **kwargs)
        engine.store.append(EV_HINT, {"text": "moved under the CAS"})
        return plan

    engine._plan_native_card = _racing_plan
    return _reserve(engine)


def _cas_exhausted(engine):
    def _always_loses(*_args, expected_last_seq=-1, **_kwargs):
        raise EventStoreConcurrencyError(engine.store.path, expected_last_seq, expected_last_seq + 1)

    engine.store.append_many = _always_loses
    return _reserve(engine)


_DRIVERS = {
    "halted": _halted, "no_slot": _no_slot, "stale_parents": _stale_parents,
    "stale_anchor": _stale_anchor, "card_contract": _card_contract,
    "card_duplicate": _card_duplicate, "card_rebind": _card_rebind,
    "card_unplannable": _card_unplannable, "authority_moved": _authority_moved,
    "cas_exhausted": _cas_exhausted,
}


@pytest.mark.parametrize("code", sorted(_DRIVERS))
def test_every_reservation_refusal_names_exactly_its_own_code(tmp_path, code):
    assert _DRIVERS[code](_engine(tmp_path)) == [code]


def test_the_vocabulary_is_exactly_the_codes_the_reservation_can_reach():
    assert set(_DRIVERS) == RESERVATION_REFUSALS
    assert RESERVATION_RACES | RESERVATION_VERDICTS == RESERVATION_REFUSALS
    assert not RESERVATION_RACES & RESERVATION_VERDICTS


def test_a_caller_that_passes_no_list_sees_the_historical_answer(tmp_path):
    engine = _engine(tmp_path)
    engine.store.append(EV_PAUSE, {"reason": "operator paused"})
    assert engine._reserve_node_build({"kind": "draft"}, _idea()) is None


def test_no_refusal_in_the_reservation_returns_without_naming_itself():
    """For the refusal a future edit adds: a bare `return None` in `_plan` would reach the inject
    path with no code, which spends the request — safe against a spin, wrong for a race. So every
    `return None` there must directly follow a `_refused(...)` statement (AST, not text: a comment
    naming `_refused` must not satisfy it)."""
    fn = function_tree(CardReservationMixin._reserve_node_build)
    plan = next(node for node in ast.walk(fn)
                if isinstance(node, ast.FunctionDef) and node.name == "_plan")
    bare: list[int] = []
    for node in ast.walk(plan):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for index, stmt in enumerate(body):
            if not (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is None):
                continue
            previous = body[index - 1] if index else None
            named = (isinstance(previous, ast.Expr) and isinstance(previous.value, ast.Call)
                     and getattr(previous.value.func, "id", None) == "_refused")
            if not named:
                bare.append(stmt.lineno)
    assert not bare, f"`_plan` refuses without naming a code at relative line(s) {bare}"
