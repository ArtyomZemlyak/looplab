"""E2 researcher panel + empirical (surrogate) ranking."""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.panel import PanelResearcher, _predict

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


def test_a_nan_param_abstains_instead_of_winning_the_panel():
    """A NaN param is isinstance-numeric, so it survives `numeric_params`, makes every distance NaN,
    and `knn_idw` degrades that to a NaN prediction. `propose` screened only for None and
    `is_better` is a bare `<` — every NaN comparison is False — so a NaN became `best_pred` and was
    then undisplaceable: the malformed candidate won and the good one lost."""
    hist = [({"x": 3.0, "y": -1.0}, 0.1), ({"x": -4.0, "y": 4.0}, 50.0)]
    assert _predict({"x": float("nan"), "y": 0.0}, hist, BOUNDS) is None   # abstain, not NaN

    nan_idea = Idea(operator="improve", params={"x": float("nan"), "y": 0.0},
                    rationale="nan idea")
    good = Idea(operator="improve", params={"x": 3.0, "y": -1.0}, rationale="good idea")
    # NaN proposed FIRST is the losing order: it would seize best_pred before `good` is scored.
    panel = PanelResearcher(_SeqResearcher([nan_idea, good]), k=2, warmup=2)
    out = panel.propose(_state([(3.0, -1.0, 0.1), (-4.0, 4.0, 50.0), (2.0, 0.0, 5.0)]), None)
    assert out.params == {"x": 3.0, "y": -1.0}, out.params


def test_the_panel_does_not_learn_from_a_gate_flagged_cheater():
    """Under `trust_gate=gate` a hard-flagged node keeps its inflated metric and stays FEASIBLE, so
    fitting the k-NN on `feasible_nodes()` teaches it to propose near the cheated params. Both
    sibling predictors use `breedable_nodes()` for exactly this reason."""
    st = _state([(3.0, -1.0, 5.0), (-4.0, 4.0, 6.0), (2.0, 0.0, 7.0)])
    st.nodes[3] = Node(id=3, operator="draft",
                       idea=Idea(operator="draft", params={"x": 0.0, "y": 0.0}),
                       metric=0.0001, status=NodeStatus.evaluated, feasible=True)   # the cheater
    st.breed_excluded = {3}
    assert 3 in {n.id for n in st.feasible_nodes()}          # still feasible…
    assert 3 not in {n.id for n in st.breedable_nodes()}     # …but never bred from

    near_cheat = Idea(operator="improve", params={"x": 0.0, "y": 0.0}, rationale="near cheater")
    honest = Idea(operator="improve", params={"x": 3.0, "y": -1.0}, rationale="honest")
    panel = PanelResearcher(_SeqResearcher([near_cheat, honest]), k=2, warmup=2)
    out = panel.propose(st, None)
    assert out.params == {"x": 3.0, "y": -1.0}, (
        "the panel ranked a candidate on the cheater's params best — it fitted on the flagged node")


def test_the_warmup_turn_makes_exactly_one_researcher_call():
    """The gate ran AFTER the K-way fan-out, so every pre-warmup turn paid for K proposals and threw
    K-1 away — `hist` depends only on `state` and is computable first."""
    cand = Idea(operator="draft", params={"x": 1.0, "y": 1.0})
    base = _SeqResearcher([cand, cand, cand, cand])
    panel = PanelResearcher(base, k=4, warmup=5)
    out = panel.propose(_state([(0.0, 0.0, 1.0)]), None)     # 1 obs < warmup
    assert base.i == 1, f"{base.i} researcher calls on a warmup turn — K-1 were discarded"
    assert out.params == {"x": 1.0, "y": 1.0} and "panel" not in out.rationale

    # …and a ranked turn still fans out to K.
    base_ranked = _SeqResearcher([cand, cand])
    ranked = PanelResearcher(base_ranked, k=2, warmup=2)
    ranked.propose(_state([(3.0, -1.0, 0.1), (-4.0, 4.0, 50.0)]), None)
    assert base_ranked.i == 2
