"""One rule for what the Strategist reads as an OUTCOME of the search (critic 2026-09-30, crit_v52).

`core/models.py::search_outcome` answers it: a failure, an evaluated node, or no outcome at all — a
node the operator deleted (`tombstoned`) or aborted, a benign terminal (`BENIGN_TERMINAL_REASONS`),
or one not finished. Its failure half is the fold's own failure-spike rule
(`events/replay.py::_counts_as_current_failure`) and is pinned equal to it here, over every
combination the two rules read.

F1 (MEDIUM, driven): a champion whose six `improve` children the operator deleted read as a HARD
STALL — `improves_since_best` 6, `stall_rung` (2, 7) — so `RuleStrategist` requested paid deep
research and the plan cut a stagnation endgame, although the search had produced no outcome at all.
F6 (LOW, driven): `failure_rate` skipped only deleted nodes, so a proxy skip and an operator abort
made 0.5 and "high failure rate" where the engine counted no failure.
"""
from __future__ import annotations

import itertools

import pytest

from looplab.agents.strategist import (RuleStrategist, StrategyContext, failure_rate,
                                       improves_since_best, stall_rung)
from looplab.core.models import (BENIGN_TERMINAL_REASONS, Event, Idea, Node, NodeStatus, RunState,
                                 search_outcome)
from looplab.events.replay import _counts_as_current_failure, fold


def _node(nid, status, *, reason="", tombstoned=False, operator="draft"):
    return Node(id=nid, operator=operator, idea=Idea(operator=operator), status=status,
                tombstoned=tombstoned, error_reason=reason,
                metric=1.0 if status is NodeStatus.evaluated else None)


# ------------------------------------------------------------------------------------ the rule
_REASONS = ("", "crash", " CRASH ", "timeout", "engine_error", "developer_stuck",
            *sorted(BENIGN_TERMINAL_REASONS), " Proxy_Skipped ")


@pytest.mark.parametrize("status,reason,tombstoned,aborted", list(itertools.product(
    list(NodeStatus), _REASONS, (False, True), (False, True))))
def test_the_failure_half_is_the_fold_s_own_rule(status, reason, tombstoned, aborted):
    node = _node(4, status, reason=reason, tombstoned=tombstoned)
    state = RunState(nodes={4: node}, aborted_nodes=[4] if aborted else [])
    outcome = search_outcome(state, node)
    assert (outcome is True) == _counts_as_current_failure(state, node)
    # …and an evaluated node is an outcome unless the operator removed it from the search.
    assert (outcome is False) == (status is NodeStatus.evaluated and not tombstoned and not aborted)


def test_failure_rate_is_over_outcomes_only():
    """F6 and survivor L2-3. MUTATIONS: count a benign terminal / an aborted node as a failure
    (0.5 -> red); keep the unfiltered denominator (one live failure over three rows reads 0.333)."""
    benign = RunState(nodes={0: _node(0, NodeStatus.evaluated),
                             1: _node(1, NodeStatus.failed, reason="proxy_skipped"),
                             2: _node(2, NodeStatus.failed, reason="aborted"),
                             3: _node(3, NodeStatus.evaluated)},
                      aborted_nodes=[2])
    assert failure_rate(benign) == 0.0
    decision = RuleStrategist().decide(benign, StrategyContext(
        failure_rate=failure_rate(benign), phase="explore", available_policies=["greedy"]))
    assert "failure" not in str((decision or {}).get("rationale"))

    mixed = RunState(nodes={0: _node(0, NodeStatus.evaluated),
                            1: _node(1, NodeStatus.failed, reason="crash"),
                            2: _node(2, NodeStatus.failed, reason="crash", tombstoned=True),
                            3: _node(3, NodeStatus.evaluated, tombstoned=True),
                            4: _node(4, NodeStatus.pending)})
    assert failure_rate(mixed) == 0.5
    assert failure_rate(RunState(nodes={0: _node(0, NodeStatus.pending)})) == 0.0


# ------------------------------------------------------------------------ the stall (F1, driven)
def _stalled_engine(tmp_path, *, children: str):
    """The critic's run: champion 0 (30 s) and six `improve` children of it, each with a withheld
    attempt carrying spend. `children` says what the operator did to them: nothing, delete, abort."""
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=100)
    engine._endgame_reserve_frac, engine._endgame_stall_nodes = 0.2, 3   # the shipped defaults
    kids = list(range(1, 7))
    rows = [("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"}),
            ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 0.0}}}),
            ("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                "eval_seconds": 30.0})]
    for nid in kids:
        rows += [("node_created", {"node_id": nid, "parent_ids": [0], "parent_generations": {"0": 0},
                                   "operator": "improve",
                                   "idea": {"operator": "improve", "params": {"x": float(nid)}}}),
                 ("eval_attempt_withheld", {"node_id": nid, "generation": 0, "attempt": 0,
                                            "at": "decide_repair", "reason": "paused",
                                            "eval_seconds": 0.5})]
    for kind, data in rows:
        engine.store.append(kind, data)
    engine._ensure_plan(fold(engine.store.read_all()))   # the plan as it stood once 0 was scored
    if children == "deleted":
        engine.store.append("node_tombstoned", {"node_ids": kids})
        assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is True
    elif children == "aborted":
        for nid in kids:
            engine.store.append("node_abort", {"node_id": nid, "generation": 0})
            engine.store.append("node_failed", {"node_id": nid, "generation": 0, "reason": "aborted",
                                                "error": "aborted", "eval_seconds": 0.5})
    return engine


def _reading(engine):
    state = fold(engine.store.read_all())
    ctx = engine._strategy_ctx(state)
    decision = RuleStrategist().decide(state, ctx) or {}
    recut = engine._ensure_plan(state)
    plans = [e.data.get("reason") for e in engine.store.read_all() if e.type == "plan"]
    return (improves_since_best(state), stall_rung(state, 3), ctx.failure_rate,
            decision.get("request_research"), recut, plans)


@pytest.mark.parametrize("children", ["deleted", "aborted"])
def test_nodes_the_operator_removed_are_no_stall_on_the_champion(tmp_path, children):
    """MUTATIONS: drop the removed-node filter from `improves_since_best` (the rule requests deep
    research) or from `stall_rung` (the plan cuts a stagnation endgame) -> red."""
    engine = _stalled_engine(tmp_path, children=children)
    assert _reading(engine) == (0, (0, 0), 0.0, None, False, ["initial"])


def test_the_same_children_left_alone_are_the_hard_stall(tmp_path):
    """The control that makes the test above non-vacuous: six live pushes on the champion are two
    stall windows, the rule asks for research and the plan re-cuts on the stall."""
    improves, rung, rate, research, recut, plans = _reading(
        _stalled_engine(tmp_path, children="pending"))
    assert (improves, rung, rate, research, recut) == (6, (2, 7), 0.0, True, True)
    assert plans[0] == "initial" and len(plans) == 2 and plans[1] != "initial"


def test_the_strategist_s_mean_eval_cost_skips_a_benign_terminal_s_seconds(tmp_path):
    """MUTATION: read `avg_eval_seconds` over every node with seconds (or every node not deleted,
    the rule before) -> six aborts' partial seconds pull the mean down, 30.0 -> 4.71."""
    engine = _stalled_engine(tmp_path, children="aborted")
    assert engine._strategy_ctx(fold(engine.store.read_all())).avg_eval_seconds == 30.0


def test_search_outcome_reads_a_log_the_way_the_fold_does():
    """A folded log, not hand-built nodes: the abort's own terminal and a proxy skip are no
    outcome, the crash is a failure, the evaluated node is not."""
    rows = [("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})]
    for nid in range(4):
        rows.append(("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": float(nid)}}}))
    rows += [("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0}),
             ("node_failed", {"node_id": 1, "generation": 0, "reason": "proxy_skipped"}),
             ("node_abort", {"node_id": 2, "generation": 0}),
             ("node_failed", {"node_id": 2, "generation": 0, "reason": "aborted"}),
             ("node_failed", {"node_id": 3, "generation": 0, "reason": "crash"})]
    state = fold([Event(seq=i + 1, type=t, ts=float(i), data=d) for i, (t, d) in enumerate(rows)])
    assert [search_outcome(state, state.nodes[n]) for n in range(4)] == [False, None, None, True]
    assert state.current_failure_count == 1 and failure_rate(state) == 0.5


def test_a_failure_s_seconds_count_toward_the_mean_eval_cost(tmp_path):
    """crit_v54 survivor ST7: a CRASH is an outcome, and the seconds it burned are what an eval of
    this run costs — only a non-outcome's (a deleted node's, an abort's) are skipped. MUTATION: read
    only evaluated nodes' seconds -> 30.0, not 20.0."""
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=100)
    rows = [("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})]
    for nid in (0, 1):
        rows.append(("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": float(nid)}}}))
    rows += [("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 30.0}),
             ("node_failed", {"node_id": 1, "generation": 0, "reason": "crash", "error": "boom",
                              "eval_seconds": 10.0})]
    for kind, data in rows:
        engine.store.append(kind, data)
    assert engine._strategy_ctx(fold(engine.store.read_all())).avg_eval_seconds == 20.0


def test_an_evaluated_node_is_an_outcome_whatever_its_metric():
    """crit_v54 survivor ST10: the TERMINAL decides, not the number on it. MUTATION: require a
    metric -> an evaluated node without one reads as no outcome and leaves the failure rate."""
    node = Node(id=0, operator="draft", idea=Idea(operator="draft"), status=NodeStatus.evaluated,
                metric=None)
    state = RunState(nodes={0: node, 1: _node(1, NodeStatus.failed, reason="crash")})
    assert search_outcome(state, node) is False
    assert failure_rate(state) == 0.5


@pytest.mark.parametrize("reason", sorted(__import__("looplab.core.models", fromlist=["x"])
                                          .FAILURE_REASONS))
def test_every_registered_failure_reason_is_a_failure_unless_it_is_benign(reason):
    """crit_v54 F6 / ST8: the rule is ONE function now, so what it calls a failure is pinned over
    the whole reason registry, not a hand-picked list. MUTATION: add a reason to the benign set
    inside `search_outcome` (`| {"oom"}`) -> that reason's row is red."""
    node = _node(1, NodeStatus.failed, reason=reason)
    state = RunState(nodes={1: node})
    assert search_outcome(state, node) is (None if reason in BENIGN_TERMINAL_REASONS else True)
