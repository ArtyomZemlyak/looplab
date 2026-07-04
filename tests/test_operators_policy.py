"""I7/I11: debug operator, merge operator, multi-parent DAG, policy transitions."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.models import Idea, Node, NodeStatus, RunState
from looplab.operators import merge_idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

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


def test_policy_merges_after_improves():
    st = RunState(direction="min")
    # 3 evaluated nodes incl. some 'improve' ops to trigger the merge cadence.
    for i, (op, m) in enumerate([("draft", 5.0), ("improve", 3.0), ("improve", 1.0),
                                 ("improve", 2.0)]):
        st.nodes[i] = Node(id=i, operator=op,
                           idea=Idea(operator=op, params={"x": float(i), "y": 0.0}),
                           metric=m, status=NodeStatus.evaluated)
    from looplab.replay import fold  # recompute best deterministically
    # set best via fold-equivalent: lowest metric is node 2 (m=1.0)
    st.best_node_id = 2
    pol = GreedyTree(n_seeds=3, max_nodes=12, merge_every=3, max_merges=2)
    act = pol.next_actions(st)
    assert act[0]["kind"] == "merge"
    assert len(act[0]["parent_ids"]) == 2


def _engine(run_dir, max_nodes):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=max_nodes))


def test_self_repair_is_policy_agnostic(tmp_path):
    """Debug/self-repair now works under every policy (was GreedyTree-only)."""
    from looplab.policy import EvolutionaryPolicy, MCTSPolicy

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
