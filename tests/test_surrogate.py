"""A2 surrogate-guided proposer (BO-lite over the metric history)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.surrogate import SurrogateResearcher
from looplab.adapters.toytask import ToyTask

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


# --- a mid-run strategy switch to BOHB wires the surrogate (not bare ASHA) -------------------------

def test_strategy_switch_to_bohb_wires_surrogate(tmp_path):
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.surrogate import SurrogateResearcher
    from pydantic import BaseModel

    class _T(BaseModel):
        kind: str = "t"; id: str = "t"; goal: str = "g"; direction: str = "min"

    class _R:
        bounds = {"k": (1.0, 9.0)}
        def propose(self, state, parent):
            return Idea(operator="draft", params={"k": 3.0})

    class _D:
        def implement(self, idea):
            return "print('{\"metric\": 0.0}')"

    eng = Engine(tmp_path / "b", task=_T(), researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    assert not isinstance(eng.researcher, SurrogateResearcher)
    eng._apply_strategy({"policy": "bohb", "policy_params": {"n_seeds": 4}})  # collision must not crash
    assert isinstance(eng.researcher, SurrogateResearcher)   # surrogate now wired
    assert eng._policy_name == "bohb"


def _evaluated(nid, params, metric):
    n = Node(id=nid, operator="draft", status=NodeStatus.evaluated, metric=metric,
             idea=Idea(operator="draft", params=params))
    return n


def test_surrogate_works_on_a_task_that_declares_no_bounds():
    """`surrogate_proposer` must mean the same thing on every task, not just the built-in benchmarks.

    Only the bundled benchmark adapters declare numeric bounds; repo and dataset tasks — the ones
    real operators run — declare none, so the CLI's `if _bounds:` gate constructed nothing and the
    setting silently did nothing at all while the settings table advertised it. The wrapper now
    learns the ranges from the run's OWN evaluated params: it still delegates while there is too
    little history, then starts proposing inside the observed region.
    """
    fb = _Fallback()
    fb.bounds = None                                  # a task with no declared search space
    sur = SurrogateResearcher({}, fallback=fb, warmup=3)
    st = RunState(goal="g", direction="min")

    # too little history -> delegate, exactly as before
    before = fb.calls
    sur.propose(st, None)
    assert fb.calls == before + 1

    for i, (lr, wd, m) in enumerate([(0.1, 0.0, 5.0), (0.2, 0.1, 3.0),
                                     (0.3, 0.2, 1.0), (0.4, 0.3, 4.0)]):
        st.nodes[i] = _evaluated(i, {"lr": lr, "weight_decay": wd}, m)

    bounds = sur._effective_bounds(st)
    assert set(bounds) == {"lr", "weight_decay"}, "ranges were not learned from the run's own params"
    assert bounds["lr"][0] < 0.1 and bounds["lr"][1] > 0.4, "the range must allow stepping outside"

    before = fb.calls
    idea = sur.propose(st, None)
    assert fb.calls == before, "with enough history the surrogate must propose, not delegate"
    assert set(idea.params) == {"lr", "weight_decay"}
    assert "surrogate-guided" in idea.rationale

    # a key that never varied defines no interval and is dropped rather than padded into a fake one
    flat = RunState(goal="g", direction="min")
    for i, m in enumerate([5.0, 3.0, 1.0, 4.0]):
        flat.nodes[i] = _evaluated(i, {"lr": 0.1 + i / 100, "seed": 7}, m)
    assert set(sur._effective_bounds(flat)) == {"lr"}


def test_declared_bounds_still_win_verbatim():
    """A task that DOES declare bounds keeps using them — inference is only the fallback."""
    sur = SurrogateResearcher(BOUNDS, fallback=_Fallback(), warmup=2)
    st = RunState(goal="g", direction="min")
    for i, m in enumerate([5.0, 3.0, 1.0]):
        st.nodes[i] = _evaluated(i, {"x": float(i), "y": float(i)}, m)
    assert sur._effective_bounds(st) == BOUNDS
