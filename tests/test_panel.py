"""E2 researcher panel + empirical (surrogate) ranking."""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.serve.panel import PanelResearcher, _predict

BOUNDS = {"x": (-5.0, 5.0), "y": (-5.0, 5.0)}


class _SeqResearcher:
    """Returns a fixed queue of candidate ideas, one per call."""
    def __init__(self, ideas):
        self.ideas = list(ideas)
        self.bounds = BOUNDS
        self.i = 0

    def propose(self, state, parent):
        idea = self.ideas[self.i % len(self.ideas)]
        self.i += 1
        return idea


def _state(points):
    st = RunState(direction="min")
    for i, (x, y, m) in enumerate(points):
        st.nodes[i] = Node(id=i, operator="draft",
                           idea=Idea(operator="draft", params={"x": x, "y": y}),
                           metric=m, status=NodeStatus.evaluated, feasible=True)
    return st


def test_predict_knn():
    hist = [({"x": 3.0, "y": -1.0}, 0.1), ({"x": -4.0, "y": 4.0}, 50.0)]
    assert _predict({"x": 3.0, "y": -1.0}, hist, BOUNDS) == 0.1


def test_panel_picks_best_predicted_idea():
    # candidate A near the known-good region, B near the known-bad region; min objective -> pick A.
    cand_a = Idea(operator="improve", params={"x": 3.0, "y": -1.0})
    cand_b = Idea(operator="improve", params={"x": -4.0, "y": 4.0})
    panel = PanelResearcher(_SeqResearcher([cand_a, cand_b]), k=2, warmup=2)
    st = _state([(3.0, -1.0, 0.1), (-4.0, 4.0, 50.0), (2.0, 0.0, 5.0)])
    out = panel.propose(st, None)
    assert out.params == {"x": 3.0, "y": -1.0}
    assert "panel" in out.rationale


def test_panel_bootstraps_before_warmup():
    cand_a = Idea(operator="draft", params={"x": 1.0, "y": 1.0})
    panel = PanelResearcher(_SeqResearcher([cand_a, cand_a]), k=2, warmup=5)
    out = panel.propose(_state([(0.0, 0.0, 1.0)]), None)   # only 1 obs < warmup -> first idea
    assert out.params == {"x": 1.0, "y": 1.0} and "panel" not in out.rationale


def test_k1_passthrough():
    cand = Idea(operator="draft", params={"x": 2.0})
    panel = PanelResearcher(_SeqResearcher([cand]), k=1)
    assert panel.propose(_state([]), None) is cand
