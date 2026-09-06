"""The metric trajectory on the run-summary row (doc 52 row 26).

`events/trajectory.py::running_best` is the one spelling of "which experiment moved the frontier",
carried as change points so the cross-run overlay costs nothing per poll. Every property is driven
through a real log folded by the real `fold`; the row test goes through the real `run_summaries`,
the tier the caveat and Mislead-gap tests hold.
"""
from __future__ import annotations

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.trajectory import TRAJECTORY_CAP, TRAJECTORY_VERSION, running_best


def _log(tmp_path, name, *, nodes, direction="max"):
    rd = tmp_path / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": direction})
    for node in nodes:
        store.append("node_created", {
            "node_id": node["id"], "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}, "rationale": "seed"}, "code": "pass\n"})
        if node.get("failed"):
            store.append("node_failed", {"node_id": node["id"], "generation": 0, "reason": "crash"})
            continue
        store.append("node_evaluated", {"node_id": node["id"], "generation": 0,
                                        "metric": node["metric"],
                                        "violations": node.get("violations", [])})
    return rd


def _state(rd):
    return fold(EventStore(rd / "events.jsonl").read_all())


def test_change_points_in_node_order_with_the_holder_of_each_best(tmp_path):
    st = _state(_log(tmp_path, "r", nodes=[
        {"id": 1, "metric": 0.5}, {"id": 2, "metric": 0.7}, {"id": 3, "metric": 0.6},
        {"id": 4, "metric": 0.9}]))
    assert running_best(st) == {
        "version": TRAJECTORY_VERSION, "evaluated": 4, "complete": True,
        # node 3 did not improve on 0.7, so it holds an x slot (index 2) and no point.
        "points": [[0, 0.5, 1], [1, 0.7, 2], [3, 0.9, 4]]}


def test_direction_min_advances_downward(tmp_path):
    st = _state(_log(tmp_path, "r", direction="min", nodes=[
        {"id": 1, "metric": 0.5}, {"id": 2, "metric": 0.7}, {"id": 3, "metric": 0.2}]))
    assert running_best(st)["points"] == [[0, 0.5, 1], [2, 0.2, 3]]


def test_the_line_extends_to_the_last_experiment_holding_the_best(tmp_path):
    st = _state(_log(tmp_path, "r", nodes=[
        {"id": 1, "metric": 0.5}, {"id": 2, "metric": 0.7}, {"id": 3, "metric": 0.6}]))
    # The final triple is the last INDEX with the best's own node, so a drawn step reaches the end.
    assert running_best(st)["points"] == [[0, 0.5, 1], [1, 0.7, 2], [2, 0.7, 2]]


def test_an_infeasible_node_occupies_an_x_slot_but_never_advances(tmp_path):
    st = _state(_log(tmp_path, "r", nodes=[
        {"id": 1, "metric": 0.5},
        {"id": 2, "metric": 0.9, "violations": [{"name": "latency", "value": 9}]},
        {"id": 3, "metric": 0.6}]))
    series = running_best(st)
    assert series["evaluated"] == 3, "the violating experiment was paid for: it stays on the x axis"
    assert series["points"] == [[0, 0.5, 1], [2, 0.6, 3]], "0.9 was rejected by the engine, so the line never claims it"


def test_failed_and_aborted_nodes_are_not_experiments_on_the_axis(tmp_path):
    st = _state(_log(tmp_path, "r", nodes=[
        {"id": 1, "metric": 0.5}, {"id": 2, "failed": True}, {"id": 3, "metric": 0.6},
        {"id": 4, "metric": 0.8}]))
    assert running_best(st)["points"] == [[0, 0.5, 1], [1, 0.6, 3], [2, 0.8, 4]]
    st.aborted_nodes.append(4)
    assert running_best(st)["points"] == [[0, 0.5, 1], [1, 0.6, 3]], "an aborted node is off the axis and off the line"


def test_a_confirmed_mean_is_the_value_the_chart_plots(tmp_path):
    st = _state(_log(tmp_path, "r", nodes=[{"id": 1, "metric": 0.5}, {"id": 2, "metric": 0.7}]))
    st.nodes[2].confirmed_mean = 0.65
    assert running_best(st)["points"] == [[0, 0.5, 1], [1, 0.65, 2]]


def test_nothing_to_draw_is_none_never_a_flat_line(tmp_path):
    assert running_best(_state(_log(tmp_path, "empty", nodes=[]))) is None
    assert running_best(_state(_log(tmp_path, "failed", nodes=[{"id": 1, "failed": True}]))) is None
    only_infeasible = _state(_log(tmp_path, "inf", nodes=[
        {"id": 1, "metric": 0.9, "violations": [{"name": "latency", "value": 9}]}]))
    assert running_best(only_infeasible) is None


def test_more_improvements_than_the_cap_are_subsampled_first_and_last_kept(tmp_path):
    n = TRAJECTORY_CAP + 100
    st = _state(_log(tmp_path, "r", nodes=[{"id": i, "metric": i / n} for i in range(1, n + 1)]))
    series = running_best(st)
    assert series["complete"] is False and series["evaluated"] == n
    assert len(series["points"]) == TRAJECTORY_CAP
    assert series["points"][0] == [0, 1 / n, 1] and series["points"][-1] == [n - 1, 1.0, n]
    indices = [p[0] for p in series["points"]]
    assert indices == sorted(set(indices)), "a subsampled series is still a strictly increasing step"
    assert running_best(st, cap=2)["points"] == [[0, 1 / n, 1], [n - 1, 1.0, n]]


def test_the_summary_row_carries_the_series_and_none_without_a_measured_node(tmp_path):
    pytest.importorskip("fastapi")
    from looplab.serve import run_projections
    from looplab.serve.server import make_app

    _log(tmp_path, "measured", nodes=[{"id": 1, "metric": 0.5}, {"id": 2, "metric": 0.8}])
    _log(tmp_path, "bare", nodes=[])
    srv = make_app(tmp_path).state.looplab
    rows = {row["run_id"]: row for row in run_projections.run_summaries(srv)}
    assert rows["measured"]["trajectory"] == {
        "version": TRAJECTORY_VERSION, "evaluated": 2, "complete": True,
        "points": [[0, 0.5, 1], [1, 0.8, 2]]}
    assert rows["bare"]["trajectory"] is None
