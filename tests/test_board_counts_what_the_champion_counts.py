"""The card board, the trajectory and the champion count ONE population (review 2026-09-22, EVT-03).

`replay._select_best` elects the champion through `SearchFitness.eligible` — feasible, not
trust-flagged, not operator-aborted, usable robust metric. The card board's record setters
(`events/card_ledger.py::_sota_eligible`) and usable evidence spelled "evaluated & feasible &
metric & not tombstoned", and `events/trajectory.py::running_best` read `feasible_nodes()`, so
neither consulted the trust flags and the board ignored aborts. Driven over two real logs:

* an OPERATOR ABORT after the node was evaluated, and
* `trust_gate=gate` with a hard `reward_hack_suspected` on the node,

each with three root drafts scoring 0.5 / 0.9 / 0.7 (max). The champion is node 2 in both — the
0.9 is excluded — but the board read the EXCLUDED node's card `supported` ("it set a record") and
the champion's card `tested` ("it beat nothing", because the record it beat was never eligible),
and under `gate` the trajectory drew the frontier through the refused 0.9. Now all three read
`core/fitness.py::counts_toward_best`, which `SearchFitness.eligible` IS.
"""
from __future__ import annotations

import itertools

import pytest

from looplab.core.fitness import SearchFitness, counts_toward_best
from looplab.core.models import Event, Idea, Node, NodeStatus
from looplab.events.replay import fold
from looplab.events.trajectory import running_best
from looplab.events.types import (EV_NODE_ABORT, EV_NODE_CREATED, EV_NODE_EVALUATED,
                                  EV_REWARD_HACK_SUSPECTED, EV_RUN_STARTED, EV_TRUST_GATE_CHANGED)

_METRICS = {0: 0.5, 1: 0.9, 2: 0.7}          # node 1 is the one the run excludes


def _node_rows():
    rows = [(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "max"})]
    for nid, metric in _METRICS.items():
        idea = {"operator": "draft", "params": {"x": nid},
                "hypothesis": f"hypothesis number {nid} changes the loss"}
        rows.append((EV_NODE_CREATED, {"node_id": nid, "parent_ids": [], "operator": "draft",
                                       "idea": idea, "code": "", "files": {}, "generation": 0}))
        rows.append((EV_NODE_EVALUATED, {"node_id": nid, "generation": 0, "metric": metric,
                                         "eval_seconds": 1.0, "violations": [], "trials": [],
                                         "extra_metrics": {}, "stdout_tail": ""}))
    return rows


def _fold(rows):
    return fold([Event(seq=i, ts=float(i + 1), type=t, data=d) for i, (t, d) in enumerate(rows)])


def _aborted_after_evaluation():
    return _node_rows() + [(EV_NODE_ABORT, {"node_id": 1, "generation": 0, "reason": "bad run"})]


def _flagged_under_gate():
    return [_node_rows()[0], (EV_TRUST_GATE_CHANGED, {"trust_gate": "gate"})] + _node_rows()[1:] + [
        (EV_REWARD_HACK_SUSPECTED, {"node_id": 1, "generation": 0,
                                    "signals": [{"signal": "metric_file_write"}]})]


def _card_of(st, nid):
    owners = [c for c in st.cards.values() if nid in c.evidence]
    assert len(owners) == 1, f"node {nid} should be the evidence of exactly one card: {owners}"
    return owners[0]


@pytest.mark.parametrize("rows", [_aborted_after_evaluation, _flagged_under_gate],
                         ids=["operator-abort-after-evaluation", "trust-gate-reward-hack"])
def test_the_champions_card_is_supported_and_the_excluded_nodes_card_is_not(rows):
    """MUTATION: drop `counts_toward_best` from `_sota_eligible` (the pre-fix predicate) and both
    logs fold to excluded=`supported`, champion=`tested` — the board contradicting the champion."""
    st = _fold(rows())
    assert st.best_node_id == 2, "precondition: the run itself refuses the 0.9"
    champion, excluded, first = _card_of(st, 2), _card_of(st, 1), _card_of(st, 0)
    assert champion.verdict == "supported", (
        "the champion beat the only ELIGIBLE record (0.5) and its card must say so")
    assert excluded.verdict != "supported", (
        "a number the run refuses to call best set no record on the board either")
    assert excluded.verdict == "open", "its evidence is unusable, the class a failed node is in"
    assert first.verdict == "tested", "node 0 established the record over nothing"


def test_the_trajectory_does_not_draw_the_frontier_through_a_refused_number():
    """Under `gate` the flagged node stays feasible (kept for audit, barred from winning) and still
    occupies its x slot — but it must not ADVANCE the line: the run's best never was 0.9. MUTATION:
    advancers back to `feasible_nodes()` and the series reads [0.5, 0.9, 0.9]."""
    st = _fold(_flagged_under_gate())
    series = running_best(st)
    assert series["evaluated"] == 3, "the flagged experiment was paid for and keeps its x slot"
    assert series["points"] == [[0, 0.5, 0], [2, 0.7, 2]]
    assert series["points"][-1][2] == st.best_node_id, "the line ends on the champion"


def test_the_aborted_node_stays_off_the_trajectory_as_before():
    """The abort half was already right on the trajectory (an aborted node has no x slot); the
    shared predicate must not change that."""
    series = running_best(_fold(_aborted_after_evaluation()))
    assert series["evaluated"] == 2 and series["points"] == [[0, 0.5, 0], [1, 0.7, 2]]


def test_audit_mode_flags_nothing_so_the_board_is_unchanged_there():
    """`trust_gate=audit` (the default) surfaces a reward-hack signal and excludes nothing — so the
    card board keeps crediting the 0.9, exactly as the champion does in that mode."""
    rows = _node_rows() + [(EV_REWARD_HACK_SUSPECTED, {
        "node_id": 1, "generation": 0, "signals": [{"signal": "metric_file_write"}]})]
    st = _fold(rows)
    assert st.best_node_id == 1
    assert _card_of(st, 1).verdict == "supported" and _card_of(st, 2).verdict == "tested"


def _node(nid, metric, *, feasible=True, confirmed=None):
    return Node(id=nid, operator="draft", idea=Idea(operator="draft"), metric=metric,
                status=NodeStatus.evaluated, feasible=feasible, confirmed_mean=confirmed)


def test_counts_toward_best_is_the_champions_filter_on_every_input():
    """ONE predicate: `SearchFitness.eligible` answers exactly what `counts_toward_best` does, over
    every combination of the four clauses — so the champion and the board cannot drift apart
    again by one gaining a clause."""
    for feasible, flagged, aborted, metric in itertools.product(
            (True, False), (False, True), (False, True), (0.5, None, float("nan"))):
        node = _node(3, metric, feasible=feasible)
        f, a = ({3} if flagged else set()), ({3} if aborted else set())
        expected = (feasible and not flagged and not aborted
                    and metric is not None and metric == metric)
        assert counts_toward_best(node, f, a) is expected, (feasible, flagged, aborted, metric)
        assert SearchFitness.eligible(node, f, a) is expected
    # the ROBUST metric decides, as it does for the champion: a confirmed mean stands in.
    assert counts_toward_best(_node(4, 0.5, confirmed=0.4), set(), set())
