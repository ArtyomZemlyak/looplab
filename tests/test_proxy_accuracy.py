"""Is the proxy that KILLS candidates any good? — the number it never had (doc 52 row 31).

`search/proxy.py` records `node_failed reason="proxy_skipped"` for a candidate below the kill
fraction: it decides, at a cost of a whole experiment, on a k-NN prediction. The pre-execution
judges the field ships measure themselves before being trusted with that (predict-before-execute
61.5 % pairwise; Rehearse's judge falling 82.8 -> 56.9 % "while remaining willing to decide").
LoopLab had no such number on any run.

The property these hold is the one an accuracy figure usually loses: it must say what it was
measured OVER. A killed node has no realized metric, so the number is computed among the survivors
the proxy already approved, and both the function and the command carry that caveat.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events.eventstore import EventStore
from looplab.search.proxy import pairwise_accuracy


def _state(scored, *, direction="min", killed=()):
    """`scored` is `{node_id: (predicted, realized | None)}`."""
    st = RunState(direction=direction)
    for nid, (predicted, realized) in scored.items():
        node = Node(id=nid, parent_ids=[], operator="improve",
                    idea=Idea(operator="improve", params={}, rationale=""))
        node.status = NodeStatus.evaluated if realized is not None else NodeStatus.pending
        node.metric = realized
        node.feasible = realized is not None
        st.nodes[nid] = node
        st.proxy_scores[nid] = predicted
    st.proxy_skipped.extend(killed)
    return st


def test_a_proxy_that_orders_every_pair_correctly_reads_as_one():
    report = pairwise_accuracy(_state({1: (1.0, 1.0), 2: (2.0, 2.0), 3: (3.0, 3.0)}))
    assert report["pairs"] == 3 and report["concordant"] == 3
    assert report["accuracy"] == pytest.approx(1.0)
    assert report["scored"] == 3 and report["evaluated"] == 3


def test_a_proxy_that_inverts_the_order_reads_as_zero():
    report = pairwise_accuracy(_state({1: (3.0, 1.0), 2: (2.0, 2.0), 3: (1.0, 3.0)}))
    assert report["accuracy"] == pytest.approx(0.0) and report["pairs"] == 3


def test_no_measurable_pair_is_not_zero_accuracy():
    """The distinction a kill switch is armed or disarmed on: "it got none right" and "there was
    nothing to be right about" are opposite facts."""
    assert pairwise_accuracy(_state({1: (1.0, 5.0)}))["accuracy"] is None
    # two nodes with the SAME realized metric carry no ordering either
    assert pairwise_accuracy(_state({1: (1.0, 5.0), 2: (2.0, 5.0)}))["accuracy"] is None
    assert pairwise_accuracy(_state({1: (1.0, None), 2: (2.0, None)}))["accuracy"] is None


def test_the_measurement_never_counts_a_node_selection_ignores():
    st = _state({1: (1.0, 1.0), 2: (2.0, 2.0), 3: (3.0, 3.0)})
    assert pairwise_accuracy(st)["evaluated"] == 3
    st.nodes[3].tombstoned = True
    assert pairwise_accuracy(st)["evaluated"] == 2
    st.nodes[3].tombstoned = False
    st.aborted_nodes.append(3)
    assert pairwise_accuracy(st)["evaluated"] == 2
    st.aborted_nodes.remove(3)
    st.breed_excluded.add(3)
    assert pairwise_accuracy(st)["evaluated"] == 2


def test_a_killed_node_is_counted_but_cannot_enter_the_accuracy():
    """The bias the report must carry: the kill removes its own evidence."""
    st = _state({1: (1.0, 1.0), 2: (2.0, 2.0), 9: (9.0, None)}, killed=[9])
    report = pairwise_accuracy(st)
    assert report["killed"] == 1 and report["killed_evaluated"] == 0
    assert report["pairs"] == 1 and report["accuracy"] == pytest.approx(1.0)
    # a killed node that WAS evaluated anyway (re-run, injection) is the one counterfactual
    st.nodes[9].status = NodeStatus.evaluated
    st.nodes[9].metric = 0.5
    st.nodes[9].feasible = True
    assert pairwise_accuracy(st)["killed_evaluated"] == 1


def test_a_prediction_tie_is_no_ordering_rather_than_a_wrong_one():
    """A k-NN proxy with one neighbour predicts the same number for everything. Counting those pairs
    as discordant would report it at 0 % — a confident-sounding verdict about a scorer that offered
    the kill nothing to rank by."""
    report = pairwise_accuracy(_state({1: (2.0, 1.0), 2: (2.0, 5.0), 3: (3.0, 9.0)}))
    assert report["tied_predictions"] == 1
    assert report["pairs"] == 2 and report["accuracy"] == pytest.approx(1.0)
    flat = pairwise_accuracy(_state({1: (2.0, 1.0), 2: (2.0, 5.0)}))
    assert flat["accuracy"] is None and flat["tied_predictions"] == 1


def test_the_direction_does_not_change_the_ordering_question():
    """The proxy predicts the METRIC, so concordance is the same relation under min and max — the
    place a sign flip would silently invert the verdict."""
    scored = {1: (1.0, 1.0), 2: (2.0, 2.0)}
    assert pairwise_accuracy(_state(scored, direction="min"))["accuracy"] == pytest.approx(1.0)
    assert pairwise_accuracy(_state(scored, direction="max"))["accuracy"] == pytest.approx(1.0)


def test_the_command_reports_the_number_and_the_caveat(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    for nid, predicted, metric in ((0, 1.0, 1.0), (1, 2.0, 3.0), (2, 3.0, 2.0)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "improve",
                                      "idea": {"operator": "improve", "params": {"x": nid},
                                               "rationale": ""}})
        store.append("proxy_scored", {"node_id": nid, "score": predicted})
        store.append("node_evaluated", {"node_id": nid, "metric": metric})
    store.append("node_created", {"node_id": 3, "parent_ids": [], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {"x": 9},
                                           "rationale": ""}})
    store.append("proxy_scored", {"node_id": 3, "score": 9.0, "skipped": True})
    store.append("node_failed", {"node_id": 3, "reason": "proxy_skipped", "error": "skipped"})
    result = CliRunner().invoke(app, ["proxy-accuracy", str(rd)])
    assert result.exit_code == 0, result.output
    assert "proxy scored 4 candidate(s)" in result.output and "1 were KILLED" in result.output
    assert "pairwise accuracy: 66.7% (2 of 3 ordered pairs)" in result.output
    assert "the kill's own error rate is not in this number" in result.output
    assert "BELOW THE FIELD'S OWN BAR" not in result.output      # 66.7% is above 60%


def test_the_command_refuses_to_read_an_unmeasurable_run_as_a_good_one(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    result = CliRunner().invoke(app, ["proxy-accuracy", str(rd)])
    assert result.exit_code == 0
    assert "NOT MEASURABLE" in result.output and "not 0 %" in result.output
