"""Systemic vs local failure: when the RUN should stop, not just the node.

`CreationRunawayCounters` is the loop's only run-level no-progress bound, and it resets on any
TERMINAL — but `node_failed` IS a terminal. So a run in which every node fails resets that guard
every turn and grinds on unbounded. Measured on `runs/rubertlite-dr-unified-v2` (2026-08-11): 26
hours and 1,705 provider calls over 6 failed / 0 evaluated nodes, every failure the same environment
defect, re-diagnosed from scratch by a fresh Developer each time.

The line is not "how many failed" but WHETHER ANYTHING HAS EVER WORKED, and the truth table below is
the whole rule. A run with an evaluated node has proved its environment, libraries and data, so a
later failure is about that one idea and only that node and its direction stop — this bound is off.
A run with none has proved nothing, so the N-th failure is evidence about the run.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import systemic_failure_stop_reason


def _state(*nodes: Node, aborted=()) -> RunState:
    st = RunState(goal="g", direction="min")
    st.nodes = {n.id: n for n in nodes}
    st.aborted_nodes = list(aborted)
    return st


def _failed(i: int, reason: str = "crash") -> Node:
    return Node(id=i, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.failed, error_reason=reason)


def _ok(i: int, metric: float = 0.5) -> Node:
    return Node(id=i, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.evaluated, metric=metric)


def test_it_stops_a_run_whose_nodes_all_failed_and_names_the_shape():
    reason = systemic_failure_stop_reason(_state(_failed(0), _failed(1), _failed(2)), 3)
    assert reason is not None
    assert "3 node(s) failed" in reason
    assert "none has ever produced a metric" in reason
    assert "crash" in reason, "the terminal must carry the reasons triage already recorded"


def test_one_evaluated_node_turns_the_bound_off_for_the_rest_of_the_run():
    """The environment, the libraries and the data are proved by a single success. Every failure
    after that is about ONE idea, and stopping the run on it would end a healthy search."""
    st = _state(_ok(0), _failed(1), _failed(2), _failed(3), _failed(4), _failed(5))
    assert systemic_failure_stop_reason(st, 3) is None


def test_it_waits_for_the_threshold_rather_than_stopping_on_the_first_crash():
    assert systemic_failure_stop_reason(_state(_failed(0)), 3) is None
    assert systemic_failure_stop_reason(_state(_failed(0), _failed(1)), 3) is None
    assert systemic_failure_stop_reason(_state(_failed(0), _failed(1), _failed(2)), 3) is not None


def test_resets_and_operator_aborts_are_not_evidence():
    """A `superseded` failure is a node RESET and an aborted node is an operator cancellation.
    Charging either would let ordinary steering end the run."""
    st = _state(_failed(0, "superseded"), _failed(1, "superseded"), _failed(2, "superseded"))
    assert systemic_failure_stop_reason(st, 3) is None

    st = _state(_failed(0), _failed(1), _failed(2), aborted=[0, 1, 2])
    assert systemic_failure_stop_reason(st, 3) is None

    # …and a mix still counts only the real ones.
    st = _state(_failed(0), _failed(1, "superseded"), _failed(2))
    assert systemic_failure_stop_reason(st, 3) is None


def test_a_tombstoned_node_is_not_counted():
    n = _failed(2)
    n.tombstoned = True
    assert systemic_failure_stop_reason(_state(_failed(0), _failed(1), n), 3) is None


def test_repairs_are_not_failures_the_unit_is_the_distinct_node():
    """A node repaired five times and failed is ONE failed idea — `inline_repair_attempts` is what
    bounds the repairs. The folded state keys nodes by id, so this is the shape by construction;
    pinned because counting attempts instead would trip on a single hard node."""
    assert systemic_failure_stop_reason(_state(_failed(0)), 3) is None


def test_pending_and_empty_runs_are_never_stopped():
    assert systemic_failure_stop_reason(_state(), 3) is None
    pending = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                   status=NodeStatus.pending)
    assert systemic_failure_stop_reason(_state(pending), 3) is None


def test_zero_and_junk_thresholds_disable_it_rather_than_stopping_everything():
    st = _state(_failed(0), _failed(1), _failed(2), _failed(3))
    assert systemic_failure_stop_reason(st, 0) is None
    assert systemic_failure_stop_reason(st, -1) is None
    assert systemic_failure_stop_reason(st, None) is None
    assert systemic_failure_stop_reason(st, True) is None, "a bool is an int; True must not mean 1"
    assert systemic_failure_stop_reason(st, 3.0) is None


def test_the_live_run_that_motivated_this_would_have_stopped_at_three_nodes():
    """`rubertlite-dr-unified-v2`: nodes 0, 1 and 2 all ended failed (crash / developer_crash) with
    no metric ever produced, plus several `superseded` resets that must not count. It ran 26 hours."""
    st = _state(_failed(0, "crash"), _failed(1, "developer_crash"), _failed(2, "crash"),
                _failed(3, "superseded"))
    reason = systemic_failure_stop_reason(st, 3)
    assert reason is not None and "3 node(s) failed" in reason
    assert "superseded" not in reason


def test_the_shipped_default_is_on_and_the_engine_reads_it_raw():
    """0 means OFF for every interval knob in the engine, so the Engine must not clamp this one to
    1 — that would turn "never stop the run for me" into "stop after one failure"."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions

    assert Settings().systemic_failure_stop == 3, "operators get the protection by default"
    assert EngineOptions().systemic_failure_stop == 0, "the bare library gains no new terminal"

    from tests.factories import make_engine
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        engine = make_engine(Path(d), options=EngineOptions(systemic_failure_stop=0))
        assert engine.systemic_failure_stop == 0
        engine = make_engine(Path(d) / "b", systemic_failure_stop=7)
        assert engine.systemic_failure_stop == 7
