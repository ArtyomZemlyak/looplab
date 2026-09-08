"""Eval lanes reserve the TIME they will charge, not just the time already charged (doc 27).

THE DEFECT, as the mega-review filed it: every admission gate compared `total_eval_seconds` — time
already CHARGED by an evaluation that has already landed its terminal — against `max_eval_seconds`.
Nothing subtracted what the lanes already running are going to charge, so with `max_parallel=N` and
one second of allowance left, N lanes each read "there is time" and each entered. A hard cumulative
ceiling that N lanes can cross at once is that ceiling times N.

Two levels, because the rule and the wiring fail differently. `eval_time_admission_blocked` is the
statable rule and is tested as a truth table; the wiring is DRIVEN over a real `Engine` — one lane
admitted, the next refused while it is still in flight — because the ledger is reached from the
dispatcher through a bound-method lookup that any stub host silently satisfies with 0.0.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.models import Idea
from looplab.engine.resources import eval_time_admission_blocked
from looplab.events.replay import fold
from tests.factories import make_engine


def _engine(run_dir, *, parallel, timeout, nodes=2):
    """A real Engine over the toy task with `nodes` pending nodes and a stubbed `_evaluate`."""
    engine = make_engine(run_dir, max_nodes=8)
    engine.timeout = timeout
    engine._eval_parallel = parallel
    engine.concurrent_research = False
    engine.store.append("run_started",
                        {"run_id": run_dir.name, "task_id": "toy", "direction": "min"})
    for node_id in range(nodes):
        engine.store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": Idea(operator="draft").model_dump(mode="json"), "code": ""})
    ran: list[int] = []

    async def _evaluate(node_id, limiter, max_es):
        async with limiter:
            ran.append(node_id)
            # Long enough that the producer's next admission decision is taken while this lane is
            # unambiguously in flight — which is the exact instant the finding is about.
            await anyio.sleep(0.05)

    engine._evaluate = _evaluate
    return engine, ran


def _dispatch(engine, max_es, count=2):
    state = fold(engine.store.read_all())
    anyio.run(engine._dispatch_evals, [{"node_id": n} for n in range(count)], state, max_es)


# --------------------------------------------------------------------------- the rule


def test_the_rule_refuses_only_the_lanes_that_would_share_one_allowance():
    # Nothing in flight: the lane enters on the historical completed-time rule alone.
    assert eval_time_admission_blocked(0.0, 0.0, 600.0, 100.0) is False
    # …including when its own worst case is larger than the whole remaining allowance. A ceiling
    # below one eval's timeout must still admit ONE evaluation, or the run makes no progress at
    # all and a budget reads as a deadlock.
    assert eval_time_admission_blocked(90.0, 0.0, 600.0, 100.0) is False
    # One lane in flight has committed the balance: the second is what the finding is about.
    assert eval_time_admission_blocked(0.0, 60.0, 60.0, 100.0) is True
    # …and an exact fit is admitted (two 50s lanes under a 100s ceiling).
    assert eval_time_admission_blocked(0.0, 50.0, 50.0, 100.0) is False
    # The completed-time clause is unchanged and is asked FIRST.
    assert eval_time_admission_blocked(100.0, 0.0, 0.0, 100.0) is True
    # No ceiling never refuses.
    assert eval_time_admission_blocked(1e9, 1e9, 1e9, None) is False
    # An unknowable estimate degrades to "stop once the in-flight worst case commits the balance",
    # which is still stricter than counting completed charges alone.
    assert eval_time_admission_blocked(50.0, 60.0, 0.0, 100.0) is True


# --------------------------------------------------------------- driven over a real Engine


def test_a_second_lane_is_refused_while_the_first_is_still_running(tmp_path):
    """max_es=100, one eval worth up to 60s: lane 1 enters, lane 2 does not."""
    engine, ran = _engine(tmp_path / "refused", parallel=4, timeout=60.0)
    _dispatch(engine, 100.0)
    assert ran == [0], "the second lane entered under an allowance the first had already committed"
    # …and it is still pending, so the next spine turn re-offers it once the first lane's REAL cost
    # is in the log — refusing an admission is never a terminal.
    state = fold(engine.store.read_all())
    assert state.nodes[1].status.value == "pending"
    assert engine._reserved_eval_seconds() == 0.0, "a settled lane's reservation must be released"
    assert engine._eval_time_reservations == {}


def test_both_lanes_enter_when_two_worst_cases_fit_the_allowance(tmp_path):
    """The control for the case above: same engine, same batch, a per-eval ceiling that fits twice.

    Without it the refusal above is satisfied by anything at all that stops the batch."""
    engine, ran = _engine(tmp_path / "fits", parallel=4, timeout=10.0)
    _dispatch(engine, 100.0)
    assert sorted(ran) == [0, 1]
    assert engine._reserved_eval_seconds() == 0.0


def test_no_eval_ceiling_reserves_nothing_and_admits_everything(tmp_path):
    engine, ran = _engine(tmp_path / "unbounded", parallel=4, timeout=600.0)
    _dispatch(engine, None)
    assert sorted(ran) == [0, 1]


def test_the_first_lane_enters_even_when_its_worst_case_exceeds_the_whole_allowance(tmp_path):
    """The deadlock guard: an allowance smaller than one eval's timeout still buys one eval."""
    engine, ran = _engine(tmp_path / "one-lane", parallel=4, timeout=600.0)
    _dispatch(engine, 5.0)
    assert ran == [0]


@pytest.mark.parametrize("parallel", [1, 4])
def test_the_reservation_is_released_on_every_path(tmp_path, parallel):
    """Serial and parallel both pair reserve/release in a `finally` — a leaked reservation would
    stop the run admitting anything for the rest of its life."""
    engine, ran = _engine(tmp_path / f"released-{parallel}", parallel=parallel, timeout=1.0)

    async def _boom(node_id, limiter, max_es):
        async with limiter:
            ran.append(node_id)
            raise RuntimeError("evaluation blew up")

    engine._evaluate = _boom
    with pytest.raises(BaseException):
        _dispatch(engine, 100.0, count=1)
    assert ran == [0]
    assert engine._eval_time_reservations == {}


def test_the_estimate_is_the_per_eval_ceiling_the_run_is_planned_around(tmp_path):
    engine, _ran = _engine(tmp_path / "estimate", parallel=1, timeout=42.0)
    node = fold(engine.store.read_all()).nodes[0]
    assert engine._eval_seconds_estimate(node) == 42.0
    assert engine._eval_seconds_estimate(None) == 42.0
    # A governed researcher override raises it: that node really may run that long, and
    # under-reserving exactly the longest nodes is the failure this reservation exists to stop.
    node.idea.eval_timeout = 300.0
    assert engine._eval_seconds_estimate(node) == 300.0
    # Not knowable -> 0.0, never a guess (the rule then falls back to the reserved-only clause).
    engine.timeout = None
    assert engine._eval_seconds_estimate(None) == 0.0
