"""Cost-constrained selection: the MCTS policy may weigh a subtree's EXPENSE (doc 52 row 31).

Every tree policy in `search/policy.py` ranked by metric alone. `budget_aware` was a prompt cue, and
the operator bandit already amortized its yield per eval-second — but the node the search expands
next was chosen with no idea what expanding it costs, which is what MARS's cost-constrained MCTS
balances. This is that balance, off by default, and these tests are about the two things that make
an opt-in selection knob safe: that OFF is byte-identical to the old score, and that ON cannot be
gamed by the absence of a measurement.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.policy import (KIND_IMPROVE, META_SCORES, MCTSPolicy, eval_cost_penalty,
                                   make_policy, subtree_eval_cost)


def _state(*rows, direction="min"):
    """`rows` are `(node_id, parents, metric, eval_seconds)`."""
    st = RunState(direction=direction)
    for nid, parents, metric, seconds in rows:
        node = Node(id=nid, parent_ids=list(parents), operator="improve",
                    idea=Idea(operator="improve", params={}, rationale=""))
        node.status = NodeStatus.evaluated
        node.metric = metric
        node.feasible = True
        node.eval_seconds = seconds
        st.nodes[nid] = node
    return st


def test_the_penalty_is_relative_to_the_runs_own_mean_and_zero_when_off():
    assert eval_cost_penalty(200.0, 100.0, 0.5) == pytest.approx(1.0)
    assert eval_cost_penalty(50.0, 100.0, 0.5) == pytest.approx(0.25)
    assert eval_cost_penalty(200.0, 100.0, 0.0) == 0.0        # the knob at rest
    assert eval_cost_penalty(200.0, 0.0, 0.5) == 0.0          # nothing measured in the run at all


def test_an_unmeasured_subtree_counts_as_average_never_as_free():
    """Otherwise the cheapest thing in every run is the thing nobody has measured, and a cost-aware
    policy would systematically prefer it — the failure mode that makes cost terms untrustworthy."""
    assert eval_cost_penalty(0.0, 100.0, 0.5) == pytest.approx(0.5)   # == the run's mean
    assert eval_cost_penalty(0.0, 100.0, 0.5) > eval_cost_penalty(50.0, 100.0, 0.5)


def test_the_subtree_cost_ignores_the_nodes_selection_already_ignores():
    """The same lifecycle filter the value and the visit count use: a tombstoned descendant must not
    decide the expense, or deleting a node would change where the search goes."""
    st = _state((0, (), 1.0, 10.0), (1, (0,), 0.9, 200.0))
    assert subtree_eval_cost(st, [0, 1]) == pytest.approx(105.0)
    st.nodes[1].tombstoned = True
    assert subtree_eval_cost(st, [0, 1]) == pytest.approx(10.0)
    st.nodes[1].tombstoned = False
    st.aborted_nodes.append(1)
    assert subtree_eval_cost(st, [0, 1]) == pytest.approx(10.0)
    st.aborted_nodes.remove(1)
    st.breed_excluded.add(1)
    assert subtree_eval_cost(st, [0, 1]) == pytest.approx(10.0)


def test_off_is_byte_identical_to_the_score_without_the_term():
    """The default must reproduce the historical decision exactly, not approximately."""
    st = _state((0, (), 1.0, 5.0), (1, (), 1.0, 500.0), (2, (0,), 0.5, 5.0))
    plain = MCTSPolicy(n_seeds=1, max_nodes=9).next_actions(st)
    zero = MCTSPolicy(n_seeds=1, max_nodes=9, cost_weight=0.0).next_actions(st)
    assert plain[0][META_SCORES] == zero[0][META_SCORES]
    assert plain[0]["parent_id"] == zero[0]["parent_id"]


def test_the_term_moves_the_pick_away_from_the_expensive_subtree():
    """Two candidates with the SAME reward and the same visit count; one costs 40x the other. With
    the term off the tie breaks by id (the cheap one is 1, so make the expensive one 0 to prove the
    term did the work)."""
    st = _state((0, (), 0.5, 400.0), (1, (), 0.5, 10.0))
    off = MCTSPolicy(n_seeds=1, max_nodes=9).next_actions(st)[0]
    on = MCTSPolicy(n_seeds=1, max_nodes=9, cost_weight=1.0).next_actions(st)[0]
    assert off["kind"] == KIND_IMPROVE and off["parent_id"] == 0        # tie -> lowest id
    assert on["parent_id"] == 1                                        # cost broke the tie
    assert on[META_SCORES][1] > on[META_SCORES][0]


def test_a_large_enough_gain_still_outranks_a_large_cost():
    """A cost term that always wins is a budget policy, not a search one: the point is the TRADE, and
    the weight is what sets it. The same pair flips at 0.5 — which is the knob doing its job, not a
    defect, and is why the unit is stated in `Settings.mcts_cost_weight` rather than left to taste."""
    st = _state((0, (), 0.01, 400.0), (1, (), 5.0, 10.0))
    gentle = MCTSPolicy(n_seeds=1, max_nodes=9, cost_weight=0.2).next_actions(st)[0]
    assert gentle["parent_id"] == 0        # far better metric, still worth 40x the cost
    steep = MCTSPolicy(n_seeds=1, max_nodes=9, cost_weight=0.5).next_actions(st)[0]
    assert steep["parent_id"] == 1        # at this price the operator asked for the cheap one


def test_a_negative_weight_cannot_turn_expense_into_a_bonus():
    """The same clamp `c` gets, for the same reason: a `-1` here would prefer the slowest subtree
    and be recorded as a legitimate strategy."""
    assert MCTSPolicy(cost_weight=-1.0).cost_weight == 0.0
    assert make_policy("mcts", n_seeds=1, max_nodes=4, cost_weight=-3.0).cost_weight == 0.0


def test_the_setting_reaches_the_policy_and_only_that_policy():
    assert Settings().mcts_cost_weight == 0.0        # off by default
    assert make_policy("mcts", n_seeds=1, max_nodes=4, cost_weight=0.75).cost_weight == 0.75
    # the other policies take the same keyword and ignore it rather than raising
    for name in ("greedy", "evolutionary", "asha"):
        policy = make_policy(name, n_seeds=1, max_nodes=4, cost_weight=0.75)
        assert not hasattr(policy, "cost_weight")


def test_a_strategy_switch_carries_the_weight_forward():
    """A run launched with a cost constraint must not lose it because the Strategist switched
    policies — the same shape as the `ablation_capable` re-stamp beside it."""
    import inspect

    from looplab.engine import strategy

    source = inspect.getsource(strategy)
    assert 'pp.setdefault("cost_weight", getattr(self.policy, "cost_weight", 0.0))' in source
