"""The run-level ceilings at the CARD session's eval admission (review 2026-09-22).

ENG2-02 — THE SPEND CEILING. A `BudgetExceeded` raised inside one adopted evaluation (its triage,
repair, stage check or critic is a paid call) used to leave `speculation.py::_card_eval_one` into
the RUN-scoped eval task group, which cancels every sibling mid-score: the terminal of compute the
run had already bought is lost, and `Engine.run`'s drain cannot help because it runs inside the
cancelled scope. `tests/test_budget_ceiling_drains_the_inflight_eval.py` drives the wrapper against
a host; here it is driven through a real `Engine.run` in Card mode, and the admission half — no new
evaluation while a deferred stop is held — through the real session.

ENG2-05 — THE EVAL-SECOND CEILING. `_dispatch_evals` reserves each lane's worst-case charge before
it starts (`resources.py::eval_time_admission_blocked`), but the Card session's admission never
asked, so N speculative lanes could each start on the last second of `max_eval_seconds` — the exact
"a ceiling times N" the reservation was built to refuse, on the path speculation runs.

Tier 1 in CLAUDE.md's ladder throughout: a real `Engine`, real Cards, real admission and real
`node_eval_started` rows. The only sentinel is `_evaluate`, as in every Card refill test.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.llm import BudgetExceeded
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_EVALUATED, EV_NODE_EVAL_STARTED

# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here: these
# tests admit speculation through the production boundary and then replace only the roles.
from tests.test_card_refill_unequal_durations import (_occupancy_engine, _terminalize,
                                                     _three_ready_cards)
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _admit_unit_speculation_receipt,
    _build_result,
    _engine,
    _start,
    _without_research,
)

CEILING = ("LLM spend ceiling reached: $1.0031 of the $1.0000 set by `llm_budget_usd`. "
           "The run stops here rather than spending more.")


def _started(engine) -> list[int]:
    return [event.data["node_id"] for event in engine.store.read_all()
            if event.type == EV_NODE_EVAL_STARTED]


# --------------------------------------------------------------------------- ENG2-02, end to end

def test_an_eval_that_crosses_the_ceiling_leaves_its_running_sibling_its_terminal(
        tmp_path, monkeypatch):
    """Width 2, a real `Engine.run`. The first evaluation is held; the second — admitted beside it —
    raises the ceiling from inside. The run must still END on the accountant's own exception, and
    the sibling that was burning must land its terminal instead of being cancelled mid-score. No
    evaluation may start after the stop."""
    engine = _occupancy_engine(tmp_path / "child-ceiling", max_nodes=4)
    calls: list[int] = []
    stopped = anyio.Event()
    after_stop: list[int] = []

    async def _eval(node_id, _limiter, _max_es):
        calls.append(node_id)
        if stopped.is_set():
            after_stop.append(node_id)
            return
        if len(calls) == 1:
            # the sibling: held until the ceiling has fired, bounded so a red run cannot hang
            with anyio.move_on_after(30):
                await stopped.wait()
            _terminalize(engine, node_id)
            return
        stopped.set()
        raise BudgetExceeded(CEILING)

    monkeypatch.setattr(engine, "_evaluate", _eval)

    async def _bounded_run():
        # BOUNDED: a regression that never pays the deferred stop does not fail — it keeps turning
        # with admission shut. A deadline turns that into a red test instead of a hung suite.
        with anyio.fail_after(60):
            return await engine.run()

    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(_bounded_run)

    assert str(caught.value) == CEILING, "the stop must be the accountant's own exception"
    assert len(calls) >= 2, f"the precondition (two lanes at once) never happened: {calls}"
    sibling = calls[0]
    evaluated = [event.data["node_id"] for event in engine.store.read_all()
                 if event.type == EV_NODE_EVALUATED]
    assert sibling in evaluated, (
        "the evaluation that was burning beside the one that crossed the ceiling lost its terminal")
    assert after_stop == [], f"an evaluation started after the ceiling fired: {after_stop}"
    assert engine._eval_inflight == set()
    assert engine._eval_budget_stop is None


def test_a_held_spend_ceiling_admits_no_new_evaluation(tmp_path, monkeypatch):
    """The admission half: while a deferred stop is held, a session with admissible pending work
    starts nothing — the stop only lets the evaluations ALREADY paid for finish.

    The pending node is committed BEFORE the stop is held. It used to be built by the session itself
    with the stop already held — which is buying new work after the ceiling, and which a held stop
    now refuses too: the session's gates read it as a terminal intent (review 2026-09-22, the Card
    producers' deferral, `tests/test_card_producer_ceiling.py`)."""
    engine, _producer = _engine(tmp_path / "held-stop", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _three_ready_cards(engine)
    _without_research(monkeypatch, engine)
    head = engine._head_request(fold(engine.store.read_all()))
    built = _build_result(engine, head)
    engine._ensure_speculation_state()
    engine._spec_builds[built.key] = built
    assert engine._serve_card_builds() is True, "precondition: the head's node is committed"
    admitted: list[int] = []

    async def _eval(node_id, _limiter, _max_es):
        admitted.append(node_id)
        _terminalize(engine, node_id)

    monkeypatch.setattr(engine, "_evaluate", _eval)
    engine._eval_budget_stop = BudgetExceeded(CEILING)

    async def _one_session():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            with anyio.move_on_after(2):
                await engine._run_card_session([], fold(engine.store.read_all()), None)

    anyio.run(_one_session)
    pending = [n.id for n in fold(engine.store.read_all()).pending_nodes()]
    assert pending, "the precondition (an admissible pending node) never happened"
    assert admitted == [] and _started(engine) == [], (
        "a new evaluation was admitted while a captured spend ceiling was waiting to be raised")


# --------------------------------------------------------------------------- ENG2-05

@pytest.mark.parametrize("max_es,second_admitted", [
    (61.0, False),     # one 60 s lane in flight leaves 1 s: the second lane cannot be covered
    (500.0, True),     # the control: two worst cases fit, so the refusal above is the allowance's
])
def test_a_card_session_reserves_eval_time_and_refuses_a_lane_the_allowance_cannot_cover(
        tmp_path, monkeypatch, max_es, second_admitted):
    engine, _producer = _engine(tmp_path / f"card-time-{int(max_es)}", depth=2)
    engine._eval_parallel = 2
    engine.timeout = 60.0                       # one lane's worst-case charge
    _start(engine)
    _three_ready_cards(engine)
    _without_research(monkeypatch, engine)
    admitted: list[int] = []
    reserved_at_entry: list[float] = []
    release = anyio.Event()

    async def _held_eval(node_id, _limiter, _max_es):
        admitted.append(node_id)
        reserved_at_entry.append(engine._reserved_eval_seconds())
        with anyio.move_on_after(20):
            await release.wait()
        _terminalize(engine, node_id)

    monkeypatch.setattr(engine, "_evaluate", _held_eval)
    observed: dict = {}

    async def _outer_loop():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg

            async def _observe():
                # Wait until a SECOND node is ready beside the running first one, give the session
                # several turns to admit it, then record what it decided and let the lane settle.
                with anyio.move_on_after(15):
                    while len(fold(engine.store.read_all()).pending_nodes()) < 2:
                        await anyio.sleep(0.02)
                    await anyio.sleep(1.2)
                observed["admitted"] = list(admitted)
                observed["reserved"] = engine._reserved_eval_seconds()
                release.set()

            eval_tg.start_soon(_observe)
            with anyio.move_on_after(25):
                for _turn in range(8):
                    await engine._run_card_session([], fold(engine.store.read_all()), max_es)
                    if release.is_set() and not engine._eval_inflight:
                        break
            release.set()

    anyio.run(_outer_loop)

    assert observed and observed["admitted"], f"the precondition never happened: {observed}"
    if second_admitted:
        assert len(observed["admitted"]) == 2, observed
    else:
        assert observed["admitted"] == observed["admitted"][:1], (
            f"a second lane started on the last second of the allowance: {observed}")
        assert observed["reserved"] == 60.0
    assert reserved_at_entry[0] == 60.0, (
        "the first lane must enter holding its own worst-case charge, taken at admission")
    # released on settle, in `_card_eval_one`'s `finally`: a leaked reservation stops the run
    # admitting anything for the rest of its life
    assert engine._eval_time_reservations == {}
