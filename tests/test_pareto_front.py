"""The non-dominated front, computed where selection can read it (docs/BACKLOG.md §0.1 row 12).

The real algorithm lived only in the browser (`ui/src/panels.jsx::paretoFront`), so a run elected
one champion on one scalar and every secondary objective was a picture. `search/policy.py::
pareto_front` is that front in the search package, and `engine/plan.py::endgame_actions` — the ONE
gate `_plan_gate` applies to every selected action set — is its first consumer: the endgame
ensemble's two parents come off the front instead of off the scalar ranking.

Two properties this file is worth nothing without, and both are driven rather than asserted about:

* the front DROPS an axis it cannot trust or cannot orient, rather than ranking on it — an `auto`
  extra metric is a number the CANDIDATE printed with no gate at all (`core/models.py`
  `EXTRA_METRIC_CHANNELS`), and an unoriented one is the browser's cost-like assumption applied to
  a higher-is-better score, which is a front that is silently backwards;
* with no admissible second axis the endgame pick is BYTE-IDENTICAL to the top-2 ranking it always
  used — which is every run in `runs/` today, and is why this needed no setting.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.plan import build_plan, endgame_actions
from looplab.search.policy import dominates, pareto_front, pareto_objectives


def _node(i, metric, extras=None, channels=None, directions=None):
    return Node(id=i, operator="improve" if i else "draft",
                idea=Idea(operator="draft", params={"x": float(i)}),
                metric=metric, status=NodeStatus.evaluated, feasible=True,
                extra_metrics=extras or {}, extra_metrics_provenance=channels or {},
                extra_metrics_direction=directions or {})


def _state(nodes, direction="max"):
    st = RunState(direction=direction)
    for n in nodes:
        st.nodes[n.id] = n
    best = max(nodes, key=lambda n: n.metric if direction == "max" else -n.metric)
    st.best_node_id = best.id
    return st


def _declared(**extras):
    """An extra-metric triple as the operator's own reader spec records it: value, channel and the
    direction that says which way is better."""
    return (extras,
            {k: "declared" for k in extras},
            {k: "max" for k in extras})


# --------------------------------------------------------------------------- the front itself

def test_one_objective_leaves_only_the_metric_leaders_on_the_front():
    """The corpus as it stands: no run records a second objective, so the front IS the scalar
    answer and nothing downstream may move."""
    st = _state([_node(0, 0.9), _node(1, 0.5), _node(2, 0.9)])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max"}
    # Metric-tied nodes both stay (a tie is not a domination), in `rank_by_metric`'s own
    # `(metric, id)` order — the ordering every search policy already shares.
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [2, 0]


def test_a_second_declared_objective_keeps_the_node_that_trades_metric_for_it():
    vals, chans, dirs = _declared(recall=0.8)
    leader = _node(0, 0.90, {"recall": 0.10}, chans, dirs)
    trader = _node(1, 0.80, vals, chans, dirs)                 # worse metric, far better recall
    loser = _node(2, 0.70, {"recall": 0.05}, chans, dirs)      # worse on BOTH — dominated
    st = _state([leader, trader, loser])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max", "recall": "max"}
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [0, 1]


def test_the_front_is_ordered_best_first_by_the_primary_metric():
    """A front that reordered the champion would be a selection change smuggled in as a diversity
    one — every consumer reads element 0 as the scalar leader."""
    vals, chans, dirs = _declared(recall=0.9)
    st = _state([_node(0, 0.5, vals, chans, dirs), _node(1, 0.9, {"recall": 0.1}, chans, dirs)])
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [1, 0]


def test_an_auto_channel_extra_metric_is_not_an_objective():
    """`auto` is any number off the CANDIDATE's own stdout with no gate. Admitting it as an axis
    would hand the candidate a second dimension it can print its way to the front of."""
    extras = {"recall": 0.99}
    st = _state([_node(0, 0.9, {"recall": 0.1}, {"recall": "auto"}, {"recall": "max"}),
                 _node(1, 0.4, extras, {"recall": "auto"}, {"recall": "max"})])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max"}
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [0], "the auto axis saved node 1"


def test_an_untagged_extra_metric_is_not_an_objective_either():
    """An untagged value reads `unknown`, never `declared` — the reader-side default every log
    written before the channel map existed produces."""
    st = _state([_node(0, 0.9, {"recall": 0.1}, {}, {"recall": "max"}),
                 _node(1, 0.4, {"recall": 0.99}, {}, {"recall": "max"})])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max"}


def test_an_unorientable_axis_is_dropped_rather_than_assumed_cost_like():
    """The browser's front treats every extra metric as lower-is-better. Applied to a declared
    nDCG that is nowhere oriented, that inverts the axis and the failure is silent."""
    st = _state([_node(0, 0.9, {"ndcg": 0.1}, {"ndcg": "declared"}, {}),
                 _node(1, 0.4, {"ndcg": 0.99}, {"ndcg": "declared"}, {})])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max"}
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [0]


def test_an_axis_only_one_node_records_cannot_order_the_pair():
    vals, chans, dirs = _declared(recall=0.9)
    st = _state([_node(0, 0.9), _node(1, 0.4, vals, chans, dirs)])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max"}


def test_two_nodes_disagreeing_about_the_direction_have_not_recorded_one_axis():
    st = _state([_node(0, 0.9, {"cost": 1.0}, {"cost": "declared"}, {"cost": "max"}),
                 _node(1, 0.4, {"cost": 2.0}, {"cost": "declared"}, {"cost": "min"})])
    assert pareto_objectives(st, st.nodes.values()) == {"metric": "max"}


def test_the_run_direction_orients_the_primary_axis():
    st = _state([_node(0, 0.2), _node(1, 0.9)], direction="min")
    assert pareto_objectives(st, st.nodes.values())["metric"] == "min"
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [0]


def test_a_tie_is_not_a_domination():
    a, b = _node(0, 0.5), _node(1, 0.5)
    assert not dominates(a, b, {"metric": "max"}) and not dominates(b, a, {"metric": "max"})


def test_a_node_without_a_usable_metric_is_not_on_the_front():
    st = _state([_node(0, 0.9)])
    st.nodes[1] = _node(1, None)
    st.nodes[2] = _node(2, float("nan"))
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [0]


# ------------------------------------------------------------- and selection reads it

def _endgame(st):
    plan = build_plan(max_nodes=6, n_seeds=2, reserve_frac=0.34, at_node=0)   # endgame at node 4
    return endgame_actions(st, plan, [{"kind": "draft"}, {"kind": "improve", "parent_id": 1}])


def test_the_endgame_ensemble_takes_its_parents_off_the_front():
    """The measured reason: the top-2 by metric are frequently one idea twice (an improve and its
    own parent, separated by noise), and an ensemble of two near-identical models buys nothing.
    Node 1 here is dominated on both axes; the front's second member is node 2, which pays for a
    lower metric with the declared objective node 0 loses on."""
    _v, chans, dirs = _declared(recall=0.0)
    nodes = [_node(0, 0.90, {"recall": 0.10}, chans, dirs),
             _node(1, 0.85, {"recall": 0.05}, chans, dirs),
             _node(2, 0.60, {"recall": 0.95}, chans, dirs),
             _node(3, 0.50, {"recall": 0.01}, chans, dirs),
             _node(4, 0.40, {"recall": 0.00}, chans, dirs)]
    action = _endgame(_state(nodes))[0]
    assert action["kind"] == "merge"
    assert action["parent_ids"] == [0, 2], "the ensemble merged the two scalar leaders again"
    assert action["_reason"] == "endgame: ensemble of the Pareto front's top-2"


def test_with_no_second_objective_the_endgame_pick_is_the_historical_top_two():
    """The inertness property, driven rather than reasoned about: this is every run in `runs/`."""
    nodes = [_node(i, 0.9 - 0.1 * i) for i in range(5)]
    action = _endgame(_state(nodes))[0]
    assert action["parent_ids"] == [0, 1] and action["_chosen"] == 0
    assert action["_reason"] == "endgame: ensemble of the top-2"


def test_a_front_of_one_falls_back_to_the_scalar_pair():
    """A single non-dominated node is not an ensemble. The fallback keeps the reserve's merge slot
    spendable instead of dropping to a sweep the run has other slots for."""
    _v, chans, dirs = _declared(recall=0.0)
    nodes = [_node(0, 0.90, {"recall": 0.90}, chans, dirs),      # dominates every other node
             _node(1, 0.80, {"recall": 0.80}, chans, dirs),
             _node(2, 0.70, {"recall": 0.70}, chans, dirs),
             _node(3, 0.60, {"recall": 0.60}, chans, dirs),
             _node(4, 0.50, {"recall": 0.50}, chans, dirs)]
    st = _state(nodes)
    assert [n.id for n in pareto_front(st, st.nodes.values())] == [0]
    action = _endgame(st)[0]
    assert action["parent_ids"] == [0, 1]
    assert action["_reason"] == "endgame: ensemble of the top-2"
