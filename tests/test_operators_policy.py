"""I7/I11: debug operator, merge operator, multi-parent DAG, policy transitions."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.operators import merge_idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import ASHAPolicy, GreedyTree, MCTSPolicy, make_policy
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def test_merge_idea_means_params():
    parents = [
        Node(id=0, operator="improve", idea=Idea(operator="improve", params={"x": 2.0, "y": 0.0})),
        Node(id=1, operator="improve", idea=Idea(operator="improve", params={"x": 4.0, "y": 2.0})),
    ]
    idea = merge_idea(parents)
    assert idea.operator == "merge"
    assert idea.params == {"x": 3.0, "y": 1.0}
    assert idea.concept_mode == "delta"
    assert idea.concepts_added == idea.concepts_removed == []


def test_policy_debugs_failed_leaf_then_stops():
    st = RunState(direction="min")
    st.nodes[0] = Node(id=0, operator="draft",
                       idea=Idea(operator="draft", params={"x": 1.0}), status=NodeStatus.failed)
    pol = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=1)
    assert pol.next_actions(st) == [{"kind": "debug", "parent_id": 0}]

    # Once a debug child exists for the failed node, no further debug for it.
    st.nodes[1] = Node(id=1, parent_ids=[0], operator="debug",
                       idea=Idea(operator="debug", params={"x": 1.1}), status=NodeStatus.failed)
    assert all(a["kind"] != "debug" for a in pol.next_actions(st))


def test_d7_expand_operator_is_yield_tracked_and_counts_as_improve_family():
    """D7 (§21.8): an `expand` node is its OWN operator in operator_yields (SCORED — its Δmetric/eval-sec
    measured distinctly) yet counts as improve-family in the generation/cadence bookkeeping (it IS a
    generation-producing refinement, so it must not perturb the QD parity)."""
    from looplab.search.policy import KIND_EXPAND, operator_yields
    st = RunState(direction="max")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 0.0}),
                       metric=0.80, status=NodeStatus.evaluated)
    # an expand node built FROM node 0, improving it
    st.nodes[1] = Node(id=1, operator=KIND_EXPAND, parent_ids=[0],
                       idea=Idea(operator=KIND_EXPAND, params={"x": 1.0}),
                       metric=0.90, status=NodeStatus.evaluated, eval_seconds=1.0)
    y = operator_yields(st)
    assert KIND_EXPAND in y and y[KIND_EXPAND]["n"] == 1 and y[KIND_EXPAND]["gain"] > 0   # scored distinctly
    # generation parity counts expand as improve-family (gen = improve+merge+expand)
    gen = sum(1 for n in st.nodes.values() if n.operator in ("improve", "merge", KIND_EXPAND))
    assert gen == 1


def test_d7_capability_expansion_due_helper():
    """D7 gate: due only when the latest concept snapshot's lock-in streak >= threshold; self-resets."""
    from looplab.search.lock_in import capability_expansion_due
    st = RunState()
    st.concept_coverage_snapshots = [{"current_streak": 6, "recent_axis": "loss/decoupled-contrastive"}]
    assert capability_expansion_due(st, streak_threshold=5) == (True, "loss/decoupled-contrastive", 6)
    st.concept_coverage_snapshots = [{"current_streak": 2, "recent_axis": "loss/x"}]
    assert capability_expansion_due(st, streak_threshold=5)[0] is False        # streak too short
    st.concept_coverage_snapshots = [{"current_streak": 9}]                    # no axis
    assert capability_expansion_due(st, streak_threshold=5)[0] is False
    assert capability_expansion_due(RunState(), streak_threshold=5) == (False, None, 0)   # no snapshot


def test_policy_merges_after_improves():
    st = RunState(direction="min")
    # 3 evaluated nodes incl. some 'improve' ops to trigger the merge cadence.
    for i, (op, m) in enumerate([("draft", 5.0), ("improve", 3.0), ("improve", 1.0),
                                 ("improve", 2.0)]):
        st.nodes[i] = Node(id=i, operator=op,
                           idea=Idea(operator=op, params={"x": float(i), "y": 0.0}),
                           metric=m, status=NodeStatus.evaluated)
    # set best via fold-equivalent: lowest metric is node 2 (m=1.0)
    st.best_node_id = 2
    pol = GreedyTree(n_seeds=3, max_nodes=12, merge_every=3, max_merges=2)
    act = pol.next_actions(st)
    assert act[0]["kind"] == "merge"
    assert len(act[0]["parent_ids"]) == 2


def test_developer_crash_pauses_after_first_node_not_the_whole_batch(tmp_path):
    """Architecture review: the developer-crash circuit-breaker must PAUSE on the FIRST crash and STOP
    the rest of the create batch — not build every seed of the batch and pay the full within-call
    retry/backoff on each. A seed batch is proposed all at once (n_seeds drafts)."""
    from looplab.core.models import Idea as _Idea
    from looplab.events.eventstore import EventStore

    class _CrashingDev:
        def implement(self, idea: "_Idea") -> str:
            return "(developer error: LLM unreachable)"

    task = ToyTask.load(TASK_FILE)
    researcher, _ = task.build_roles()
    eng = Engine(tmp_path / "crash", task=task, researcher=researcher, developer=_CrashingDev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8))
    state = anyio.run(eng.run)
    failed = [e for e in EventStore(tmp_path / "crash" / "events.jsonl").read_all()
              if e.type == "node_failed" and e.data.get("reason") == "developer_crash"]
    assert len(failed) == 1, [e.data for e in failed]   # only the FIRST seed built, not the whole batch
    assert state.paused                                 # run auto-paused on the crash


def _ablate_cadence_state():
    """A state where GreedyTree's ablate cadence would fire: past seed phase, best carries >=2
    numeric params, and n_improve >= ablate_every with no refine_block yet."""
    st = RunState(direction="min")
    for i, (op, m) in enumerate([("draft", 5.0), ("improve", 3.0), ("improve", 1.0),
                                 ("improve", 2.0)]):
        st.nodes[i] = Node(id=i, operator=op,
                           idea=Idea(operator=op, params={"x": float(i), "y": 0.0}),
                           metric=m, status=NodeStatus.evaluated)
    st.best_node_id = 2  # lowest metric
    return st


def test_ablate_cadence_fires_when_capable():
    """Baseline: on an ablation-capable run the cadence proposes an ablate."""
    st = _ablate_cadence_state()
    pol = GreedyTree(n_seeds=3, max_nodes=12, ablate_every=1, enable_merge=False)
    assert pol.next_actions(st)[0]["kind"] == "ablate"


def test_ablation_incapable_policy_never_proposes_ablate():
    """H1 regression: a repo/eval-spec run stamps policy.ablation_capable=False; the ablate
    cadence must then NOT propose an ablate (which would spin forever, since the skip creates no
    refine_block node so the cadence never clears). It must fall through to improve instead."""
    st = _ablate_cadence_state()
    # GreedyTree cadence path + operator-bandit path both honor the flag.
    for pol in (GreedyTree(n_seeds=3, max_nodes=12, ablate_every=1, enable_merge=False),
                GreedyTree(n_seeds=3, max_nodes=12, ablate_every=1, enable_merge=False,
                           operator_bandit=True)):
        pol.ablation_capable = False
        acts = pol.next_actions(st)
        assert all(a["kind"] != "ablate" for a in acts), acts
        assert acts[0]["kind"] == "improve"


def test_legal_actions_builder_honors_ablation_capability():
    """H1: the self-driving legal_actions() builder (the ASHA/surrogate action set) is the real
    route that proposes ablate for those policies — it must also drop ablate when the run is
    ablation-incapable. (ASHAPolicy.next_actions itself never emits ablate; legal_actions does.)"""
    from looplab.search.policy import legal_actions
    st = _ablate_cadence_state()
    pol = ASHAPolicy(n_seeds=3, max_nodes=12)
    # capable (flag unset -> getattr default True): ablate IS offered
    assert any(a["kind"] == "ablate" for a in legal_actions(st, pol, max_nodes=12))
    # incapable: ablate is dropped
    pol.ablation_capable = False
    assert all(a["kind"] != "ablate" for a in legal_actions(st, pol, max_nodes=12))


def test_mcts_reward_bounded_and_monotone_both_directions():
    """M2 regression: the UCB reward map must be BOUNDED in (0,2) and direction-correct-monotone for
    BOTH directions, so a large-magnitude metric can't swamp exploration and collapse MCTS to greedy.
    The max branch used to be a bare, unbounded `reward = value`."""
    from looplab.search.policy import _mcts_reward
    vals = [-1e6, -400.0, -3.5, -1.0, -0.01, 0.0, 0.01, 1.0, 3.5, 400.0, 1e6]
    for d in ("min", "max"):
        rs = [_mcts_reward(v, d) for v in vals]
        assert all(0.0 < r < 2.0 for r in rs), (d, rs)          # bounded
        assert _mcts_reward(0.0, d) == 1.0                       # continuous at 0
    # min: lower value -> higher reward (strictly decreasing)
    assert all(_mcts_reward(a, "min") > _mcts_reward(b, "min") for a, b in zip(vals, vals[1:]))
    # max: higher value -> higher reward (strictly increasing)
    assert all(_mcts_reward(a, "max") < _mcts_reward(b, "max") for a, b in zip(vals, vals[1:]))
    # a huge max metric must NOT dominate the ~1.4 exploration bonus: its reward stays < 2
    assert _mcts_reward(1e9, "max") < 2.0


def _engine(run_dir, max_nodes):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=max_nodes))


def test_self_repair_is_policy_agnostic(tmp_path):
    """Debug/self-repair now works under every policy (was GreedyTree-only)."""
    from looplab.search.policy import EvolutionaryPolicy, MCTSPolicy

    class _BrokenThenFixed:
        def implement(self, idea):
            return "raise RuntimeError('boom')\n"
        def repair(self, idea, code, error):
            return "import json; print(json.dumps({'metric': 0.1}))\n"

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    for pol in (EvolutionaryPolicy(pop=2, max_nodes=4, debug_depth=1),
                MCTSPolicy(n_seeds=2, max_nodes=4, debug_depth=1)):
        rd = tmp_path / pol.__class__.__name__
        eng = Engine(rd, task=ToyTask.load(TASK_FILE), researcher=_Stub(),
                     developer=_BrokenThenFixed(), sandbox=SubprocessSandbox(), policy=pol)
        state = anyio.run(eng.run)
        assert any(n.operator == "debug" and n.metric == 0.1 for n in state.nodes.values()), \
            f"{pol.__class__.__name__} did not self-repair"


def test_end_to_end_produces_a_merge_node(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", max_nodes=12).run)
    merges = [n for n in state.nodes.values() if n.operator == "merge"]
    assert merges, "expected at least one merge node in a 12-node run"
    assert all(len(n.parent_ids) == 2 for n in merges)  # multi-parent DAG
    assert state.finished and len(state.nodes) == 12


# --- merge: non-numeric params are skipped, not summed -------------------------------------------

def test_merge_idea_skips_non_numeric_params():
    a = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 2.0}))
    # A free-form repo param that isn't numeric must be skipped, not crash sum().
    b = Node(id=1, operator="draft", idea=Idea.model_construct(operator="draft",
                                                               params={"x": 4.0, "name": "linear"}))
    idea = merge_idea([a, b])
    assert idea.params["x"] == 3.0 and "name" not in idea.params


# --- ASHA rung-0 width knob actually takes effect -------------------------------------------------

def test_asha_rung_nodes_overrides_rung0_width():
    p = make_policy("asha", n_seeds=4, max_nodes=20, eta=3, rung_nodes=8)
    assert isinstance(p, ASHAPolicy) and p.rung0 == 8
    # 0 falls back to n_seeds (default, preserving prior behavior).
    assert make_policy("asha", n_seeds=4, max_nodes=20, eta=3, rung_nodes=0).rung0 == 4
    # policy_params colliding with explicit make_policy kwargs must not crash via the strategist path.
    assert make_policy("asha", n_seeds=4, max_nodes=20, rung_nodes=6, eta=2).rung0 == 6


# #27/#31 — MCTS values a candidate by its FEASIBLE descendants only
def test_mcts_ignores_infeasible_descendant_metric():
    st = RunState(direction="max")
    p = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
             status=NodeStatus.evaluated, feasible=True)
    child = Node(id=1, parent_ids=[0], operator="improve", idea=Idea(operator="improve"),
                 metric=99.0, status=NodeStatus.evaluated, feasible=False)   # great but infeasible
    other = Node(id=2, operator="draft", idea=Idea(operator="draft"), metric=2.0,
                 status=NodeStatus.evaluated, feasible=True)
    st.nodes = {0: p, 1: child, 2: other}
    act = MCTSPolicy(n_seeds=1, max_nodes=10).next_actions(st)
    # node 0's only high score is its infeasible child (99) — it must NOT be valued by it, so the
    # feasible node 2 (metric 2 > 1) is the better-valued expansion target.
    assert act and act[0]["kind"] == "improve" and act[0]["parent_id"] == 2


def test_mcts_reward_monotone_for_signed_min_metrics():
    # min-direction reward must stay monotone in the metric even when values are NEGATIVE: a subtree
    # whose best is -5 (excellent for a min task) must outrank one whose best is +0.5 (poor). The old
    # `1/(1+abs(value))` inverted this — abs mapped -5 -> 0.167 BELOW +0.5 -> 0.667.
    st = RunState(direction="min")
    poor = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=0.5,
                status=NodeStatus.evaluated, feasible=True)
    great = Node(id=1, operator="draft", idea=Idea(operator="draft"), metric=-5.0,
                 status=NodeStatus.evaluated, feasible=True)
    st.nodes = {0: poor, 1: great}
    act = MCTSPolicy(n_seeds=2, max_nodes=10).next_actions(st)
    assert act and act[0]["kind"] == "improve" and act[0]["parent_id"] == 1


def test_mcts_reward_stays_bounded_for_large_negative_metrics():
    # The negative-branch reward must be BOUNDED, else it swamps the UCB exploration term (c≈1.4) and the
    # policy degenerates to pure greedy on negative-metric tasks (log-likelihood ≈ -400 → old `1-value`
    # gave reward ≈ 400). With the bounded `2 - 1/(1-value)` map two subtrees near -400 map to nearly
    # identical rewards, so their UCB scores are within exploration reach (the old map differed by ~4).
    st = RunState(direction="min")
    a = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=-402.0,
             status=NodeStatus.evaluated, feasible=True)
    b = Node(id=1, operator="draft", idea=Idea(operator="draft"), metric=-398.0,
             status=NodeStatus.evaluated, feasible=True)
    st.nodes = {0: a, 1: b}
    act = MCTSPolicy(n_seeds=2, max_nodes=10).next_actions(st)
    scores = act[0]["_scores"]
    # bounded+compressed: the two large-negative subtrees are within a whisker of each other (old
    # unbounded `1-value` would have put them ~4.0 apart, forcing pure exploitation of the -402 subtree).
    assert abs(scores[0] - scores[1]) < 0.1


# --- Batch-5 search corner-case regressions -------------------------------------------------------

def test_greedytree_merge_every_zero_no_zerodivision():
    from looplab.search.policy import GreedyTree
    assert GreedyTree(merge_every=0).merge_every == 1   # 0 would ZeroDivision in n_improve // merge_every


def test_mcts_negative_c_clamped_to_zero():
    from looplab.search.policy import MCTSPolicy
    assert MCTSPolicy(c=-5).c == 0.0                    # a negative c flips UCB exploration into a penalty


def test_hybrid_cluster_has_a_similarity_floor():
    from looplab.search.hybrid_merge import cluster_near_duplicates
    unrelated = ["gradient boosting tabular", "convolutional images", "arima forecast",
                 "reinforcement agent", "random forest", "transformer attention"]
    clusters = cluster_near_duplicates(unrelated, k=3)
    assert len(clusters) >= len(unrelated) - 1          # not collapsed into one giant cluster


def _asha_rung0(extra):
    # 4 evaluated draft survivors (min: 0.1<0.2<0.5<0.6, top-2 = nodes 0,1) + caller-supplied children.
    st = RunState(direction="min")
    for i, m in [(0, 0.1), (1, 0.2), (2, 0.5), (3, 0.6)]:
        st.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft", params={}),
                           metric=m, status=NodeStatus.evaluated, feasible=True)
    st.nodes.update(extra)
    return st


def test_asha_one_failed_promotion_keeps_survivor_repromotable():
    # Batch-5 behaviour preserved: node 0's ONLY promotion crashed → it stays re-promotable (one
    # transient crash must not abandon a possibly-best lineage), so it is still the chosen parent.
    child = {10: Node(id=10, parent_ids=[0], operator="improve",
                      idea=Idea(operator="improve", params={}), status=NodeStatus.failed)}
    # debug_depth=0 isolates the PROMOTION logic (skip the debug-failed-leaf step).
    act = ASHAPolicy(n_seeds=4, max_nodes=20, eta=2, debug_depth=0).next_actions(_asha_rung0(child))
    assert act and act[0]["kind"] == "improve" and act[0]["parent_id"] == 0


def test_asha_retires_a_deterministically_crashing_survivor():
    # Code-review pass: after `_ASHA_MAX_FAILED_PROMOTIONS` (2) failed promotions and no live child, the
    # survivor is RETIRED — else a lineage that crashes deterministically is re-promoted every iteration
    # (chosen = lowest-id survivor), starving siblings and burning the node budget. So the next promotion
    # goes to the OTHER survivor (node 1), not node 0.
    kids = {10: Node(id=10, parent_ids=[0], operator="improve",
                     idea=Idea(operator="improve", params={}), status=NodeStatus.failed),
            11: Node(id=11, parent_ids=[0], operator="improve",
                     idea=Idea(operator="improve", params={}), status=NodeStatus.failed)}
    act = ASHAPolicy(n_seeds=4, max_nodes=20, eta=2, debug_depth=0).next_actions(_asha_rung0(kids))
    assert act and act[0]["kind"] == "improve" and act[0]["parent_id"] == 1
