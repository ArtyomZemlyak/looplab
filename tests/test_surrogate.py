"""A2 surrogate-guided proposer (BO-lite over the metric history)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.models import Idea, Node, NodeStatus, RunState
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.sandbox import SubprocessSandbox
from looplab.surrogate import SurrogateResearcher
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"
BOUNDS = {"x": (-5.0, 5.0), "y": (-5.0, 5.0)}


class _Fallback:
    def __init__(self): self.bounds = BOUNDS; self.calls = 0
    def propose(self, state, parent):
        self.calls += 1
        return Idea(operator="draft", params={"x": 0.0, "y": 0.0}, rationale="fallback")


def _state_with(points):
    st = RunState(direction="min")
    for i, (x, y, m) in enumerate(points):
        st.nodes[i] = Node(id=i, operator="draft",
                           idea=Idea(operator="draft", params={"x": x, "y": y}),
                           metric=m, status=NodeStatus.evaluated, feasible=True)
    return st


def test_surrogate_bootstraps_via_fallback():
    fb = _Fallback()
    sr = SurrogateResearcher(BOUNDS, fallback=fb, warmup=4)
    sr.propose(_state_with([(0.0, 0.0, 10.0)]), None)   # 1 obs < warmup -> delegate
    assert fb.calls == 1


def test_surrogate_proposes_in_bounds_and_near_optimum():
    # objective minimized near (3, -1); seed history around it + far points.
    pts = [(3.0, -1.0, 0.1), (2.5, -0.5, 1.0), (3.5, -1.5, 1.2), (-4.0, 4.0, 50.0), (4.5, 4.5, 60.0)]
    sr = SurrogateResearcher(BOUNDS, fallback=_Fallback(), seed=1, explore=0.0)
    idea = sr.propose(_state_with(pts), None)
    assert -5.0 <= idea.params["x"] <= 5.0 and -5.0 <= idea.params["y"] <= 5.0
    # with pure exploit it should land in the good region, not the far-bad corner
    assert idea.params["x"] > 0 and idea.params["y"] < 2.0, idea.params


def test_surrogate_is_deterministic_given_seed():
    pts = [(3.0, -1.0, 0.1), (2.0, 0.0, 2.0), (1.0, 1.0, 5.0), (4.0, -2.0, 1.5)]
    a = SurrogateResearcher(BOUNDS, fallback=_Fallback(), seed=7).propose(_state_with(pts), None)
    b = SurrogateResearcher(BOUNDS, fallback=_Fallback(), seed=7).propose(_state_with(pts), None)
    assert a.params == b.params


def test_surrogate_end_to_end_toy(tmp_path):
    task = ToyTask.load(TASK_FILE)
    _r, developer = task.build_roles()
    researcher = SurrogateResearcher(BOUNDS, fallback=task.build_roles()[0], seed=0)
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=12))
    state = anyio.run(eng.run)
    assert state.finished and state.best() is not None
    assert any("surrogate" in n.idea.rationale for n in state.nodes.values())
