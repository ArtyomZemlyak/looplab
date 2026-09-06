"""Doc 52 row 18: the endgame re-scoped.

(a) an ensemble merge's Developer is handed BOTH parents — the primary as its working set, the
    co-parents' code and traces as a bounded block — through every wrapper that forwards
    `implement_from`;
(b) the run's PLAN is a folded event (`EV_PLAN`) with an endgame reserve the DISPATCHER honours:
    inside the reserve breadth is replaced by the top-2 ensemble (once) and champion sweeps;
    the plan re-cuts on a budget change and on a hard stall;
(c) the champion sweep proposes through the k-NN surrogate, and the Strategist's endgame rule
    names `endgame_sweep`, which it may switch off.
"""
from __future__ import annotations

import anyio

from looplab.adapters.repo_developer import co_parent_block
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.node_build import accepts_co_parents
from looplab.engine.plan import (
    HARD_STALL_RUNGS, META_SWEEP, build_plan, endgame_actions, in_endgame, replan)
from looplab.events.replay import fold
from looplab.events.eventstore import Event
from looplab.events.types import EV_PLAN
from tests.factories import make_engine


# ------------------------------------------------------------------ (a) both parents

def _node(i, metric, files, rationale="idea", stages=(), error="", repairs=0):
    n = Node(id=i, operator="improve", idea=Idea(operator="improve", params={"lr": 0.1 * (i + 1)},
                                                 rationale=rationale),
             metric=metric, status=NodeStatus.evaluated, feasible=True, files=files, error=error,
             repairs=repairs)
    n.stages = list(stages)
    return n


def test_the_co_parent_block_shows_traces_and_only_the_files_that_differ():
    base = {"train.py": "shared", "loss.py": "v1 loss"}
    other = _node(7, 0.71, {"train.py": "shared", "loss.py": "v2 loss with a twist", "extra.py": "x" * 7000},
                  rationale="use a sharper loss password=hunter2hunter2",
                  stages=[{"name": "train", "status": "ok", "seconds": 1234.5},
                          {"name": "score", "status": "ok", "seconds": 12.0}], repairs=1)
    block = co_parent_block([other], base)
    assert "=== CO-PARENT SOLUTIONS" in block and "co-parent experiment #7" in block
    assert "metric=0.71" in block and "trace: train:ok 1234s -> score:ok 12s; repairs=1" in block
    assert "--- train.py --- (identical to your working set)" in block and "shared" not in block.split("--- train.py ---")[1][:40]
    assert "--- loss.py ---\nv2 loss with a twist" in block
    assert "chars omitted" in block, "a long co-parent file is capped and says so"
    assert "hunter2hunter2" not in block, "the rationale is redacted like every persisted string"
    assert co_parent_block([], base) .startswith("\n\n=== CO-PARENT")


def test_accepts_co_parents_probes_the_keyword_or_kwargs():
    assert accepts_co_parents(lambda idea, parent, *, co_parents=(): "") is True
    assert accepts_co_parents(lambda idea, parent, **kw: "") is True
    assert accepts_co_parents(lambda idea, parent: "") is False


class _Dev:
    """A Developer that takes the keyword and records what it was handed."""
    is_code_generating = True

    def __init__(self):
        self.calls = []

    def implement(self, idea):
        self.calls.append(("implement", None, ()))
        return ""

    def implement_from(self, idea, parent, *, co_parents=()):
        self.calls.append(("implement_from", parent.id, tuple(n.id for n in co_parents)))
        return ""


class _PlainDev(_Dev):
    def implement_from(self, idea, parent):          # the historical two-argument surface
        self.calls.append(("implement_from", parent.id, "no-keyword"))
        return ""


def test_the_engine_hands_co_parents_only_to_a_developer_that_takes_them(tmp_path):
    a, b = _node(0, 0.5, {"a.py": "a"}), _node(1, 0.6, {"b.py": "b"})
    for dev, expected in ((_Dev(), (1,)), (_PlainDev(), "no-keyword")):
        eng = make_engine(tmp_path / f"run-{expected}", developer=dev, merge_mode="ensemble")
        eng._implement_result(Idea(operator="merge"), a, developer=dev, state=RunState(), co_parents=[b])
        assert dev.calls[-1] == ("implement_from", 0, expected)
        eng._implement_result(Idea(operator="merge"), a, developer=dev, state=RunState())
        assert dev.calls[-1][2] in ((), "no-keyword"), "no co-parents: the historical call"


def test_the_validating_wrapper_and_the_facade_forward_the_keyword():
    from looplab.agents.roles import ValidatingDeveloper
    from looplab.agents.unified_agent import UnifiedAgent

    inner = _Dev()
    wrapper = ValidatingDeveloper.__new__(ValidatingDeveloper)
    wrapper.inner, wrapper.fallback = inner, None
    wrapper._attempt_loop = lambda idea, fn, fb=None: fn(idea)    # type: ignore[attr-defined]
    a, b = _node(0, 0.5, {}), _node(1, 0.6, {})
    wrapper.implement_from(Idea(operator="merge"), a, co_parents=(b,))
    assert inner.calls[-1] == ("implement_from", 0, (1,))
    plain = _PlainDev()
    wrapper.inner = plain
    wrapper.implement_from(Idea(operator="merge"), a, co_parents=(b,))
    assert plain.calls[-1] == ("implement_from", 0, "no-keyword")

    facade = UnifiedAgent.__new__(UnifiedAgent)
    facade._for_stage = lambda stage: inner                      # type: ignore[attr-defined]
    facade._sync_audit = lambda: None                            # type: ignore[attr-defined]
    facade.implement_from(Idea(operator="merge"), a, co_parents=(b,))
    assert inner.calls[-1] == ("implement_from", 0, (1,))


def test_both_engine_merge_sites_pass_the_co_parents():
    from tests._source_scan import function_tree
    import inspect
    from looplab.engine import speculation, orchestrator
    src = inspect.getsource(speculation)
    assert 'co_parents=parents[1:] if self._merge_mode == "ensemble" else ()' in src
    src2 = inspect.getsource(orchestrator)
    assert 'co_parents=pnodes[1:] if self._merge_mode == "ensemble" else ()' in src2
    assert function_tree  # the scanner is importable (tier-3 pins above are the residue)


# ------------------------------------------------------------------ (b) the plan

def test_build_plan_cuts_the_budget_and_refuses_when_nothing_fits():
    plan = build_plan(max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=0)
    assert plan["endgame_start"] == 8 and plan["reserve"] == 2
    assert [p["nodes"] for p in plan["phases"]] == [3, 5, 2]
    assert plan["phases"][-1] == {"name": "endgame", "nodes": 2, "reserve": True, "kinds": ["merge", "sweep"]}
    assert build_plan(max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=0, endgame_sweep=False)["phases"][-1]["kinds"] == ["merge"]
    assert build_plan(max_nodes=10, n_seeds=3, reserve_frac=0.0, at_node=0) is None
    assert build_plan(max_nodes=2, n_seeds=2, reserve_frac=0.5, at_node=0) is None, "no room for a search AND a reserve"
    small = build_plan(max_nodes=4, n_seeds=1, reserve_frac=0.2, at_node=0)
    assert small["reserve"] == 1 and small["endgame_start"] == 3
    assert in_endgame(plan, 8) and not in_endgame(plan, 7) and not in_endgame(None, 99)


def test_replan_follows_the_budget_and_starts_the_endgame_on_a_hard_stall():
    plan = build_plan(max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=0)
    assert replan(plan, max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=5, stall_rung=0) is None
    grown = replan(plan, max_nodes=20, n_seeds=3, reserve_frac=0.2, at_node=5, stall_rung=0)
    assert grown["reason"] == "budget_changed" and grown["endgame_start"] == 16
    stalled = replan(plan, max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=5, stall_rung=HARD_STALL_RUNGS)
    assert stalled["reason"] == "stagnation" and stalled["endgame_start"] == 5 and stalled["reserve"] == 5
    assert replan(plan, max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=9, stall_rung=9) is None, \
        "already inside the endgame: nothing earlier to start"
    assert replan(plan, max_nodes=10, n_seeds=3, reserve_frac=0.2, at_node=3, stall_rung=9) is None, \
        "a stall inside the seed phase is not a search that stalled"


def _state(n, best_idx=0):
    st = RunState(direction="max")
    for i in range(n):
        st.nodes[i] = Node(id=i, operator="improve" if i else "draft",
                           idea=Idea(operator="draft", params={"x": float(i)}),
                           metric=(1.0 if i == best_idx else 0.1 * i), status=NodeStatus.evaluated,
                           feasible=True)
    st.best_node_id = best_idx
    return st


def test_the_gate_replaces_breadth_inside_the_reserve_and_nothing_outside_it():
    plan = build_plan(max_nodes=6, n_seeds=2, reserve_frac=0.34, at_node=0)   # endgame at node 4
    breadth = [{"kind": "draft"}, {"kind": "improve", "parent_id": 1}]
    assert endgame_actions(_state(3), plan, breadth) == breadth, "outside the reserve: untouched"
    pending = [{"kind": "evaluate", "node_id": 4}]
    assert endgame_actions(_state(4), plan, pending) == pending, "evaluations are never touched"
    assert endgame_actions(_state(4), plan, []) == [], "the finish is never touched"
    first = endgame_actions(_state(4), plan, breadth)
    assert first[0]["kind"] == "merge" and first[0]["parent_ids"][0] == 0, "first: the top-2 ensemble"
    st = _state(5)
    st.nodes[4].operator = "merge"                                            # minted in the reserve
    sweep = endgame_actions(st, plan, breadth)
    assert sweep == [{"kind": "improve", "parent_id": 0, META_SWEEP: True, "_chosen": 0,
                      "_reason": "endgame: champion sweep (k-NN surrogate)"}]
    plain = endgame_actions(st, plan, breadth, sweep=False)
    assert plain[0]["kind"] == "improve" and META_SWEEP not in plain[0]
    keep = [{"kind": "improve", "parent_id": 0, "_card_id": "c1"}, {"kind": "draft", "_card_id": "c2"}]
    assert endgame_actions(st, plan, keep) == keep[:1], "a Card that already refines the champion keeps its slot"


def test_the_fold_applies_the_plan_and_an_old_log_reads_none():
    row = build_plan(max_nodes=8, n_seeds=3, reserve_frac=0.25, at_node=3)
    st = fold([Event(seq=0, ts=0.0, type="run_started", data={"run_id": "r", "task_id": "t", "direction": "min"}),
               Event(seq=1, ts=1.0, type=EV_PLAN, data=row),
               Event(seq=2, ts=2.0, type=EV_PLAN, data={"bogus": True}),
               Event(seq=3, ts=3.0, type=EV_PLAN, data={**row, "reason": "stagnation", "at_node": 5})])
    assert st.plan["reason"] == "stagnation" and [h["reason"] for h in st.plan_history] == ["initial", "stagnation"]
    assert fold([]).plan is None and fold([]).plan_history == []


def test_a_toy_run_writes_the_plan_and_spends_its_reserve_on_the_endgame(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=2, max_nodes=8, endgame_reserve_frac=0.25)
    state = anyio.run(eng.run)
    assert state.finished and state.plan is not None
    assert state.plan["endgame_start"] == 6 and state.plan["reserve"] == 2
    plans = [e for e in eng.store.read_all() if e.type == EV_PLAN]
    assert len(plans) == 1 and plans[0].data["at_node"] == 0
    created = [e for e in eng.store.read_all() if e.type == "node_created"]
    assert plans[0].seq < created[0].seq, "the plan precedes the first creation"
    reserve = [n for n in state.nodes.values() if n.id >= state.plan["endgame_start"]]
    assert reserve and all(n.operator in ("merge", "improve") for n in reserve)
    assert any(n.operator == "merge" for n in reserve), "the top-2 ensemble was minted in the reserve"
    bare = make_engine(tmp_path / "bare", n_seeds=2, max_nodes=8)
    assert anyio.run(bare.run).plan is None, "a bare Engine keeps the historical dispatch"


def test_a_sweep_action_proposes_through_the_surrogate(tmp_path):
    from looplab.search.surrogate import SurrogateResearcher
    eng = make_engine(tmp_path / "run", n_seeds=2, max_nodes=8, endgame_reserve_frac=0.25)
    sweeper = eng._sweep_researcher(eng.researcher)
    assert isinstance(sweeper, SurrogateResearcher) and sweeper.fallback is eng.researcher
    assert eng._sweep_researcher(eng.researcher) is sweeper, "built once"


# ------------------------------------------------------------------ (c) the Strategist

def test_the_endgame_rule_names_the_sweep_and_a_strategist_may_switch_it_off(tmp_path):
    from looplab.agents.strategist import RuleStrategist, StrategyContext, validate_strategy
    ctx = StrategyContext(node_count=8, phase="exploit", node_budget_frac=0.85,
                          available_policies=["greedy"], available_developers=["default"])
    decision = RuleStrategist()._decide_machinery(RunState(), ctx)
    assert decision["operators"] == {"merge_mode": "ensemble", "ablate_every": 0, "endgame_sweep": True}
    clean = validate_strategy({"policy": "greedy", "operators": {"endgame_sweep": False, "merge_mode": "ensemble"}},
                              ctx)
    assert clean["operators"]["endgame_sweep"] is False
    eng = make_engine(tmp_path / "run", endgame_reserve_frac=0.25)
    assert eng._endgame_sweep is True
    eng._apply_strategy({"policy": "greedy", "operators": {"endgame_sweep": False}})
    assert eng._endgame_sweep is False
