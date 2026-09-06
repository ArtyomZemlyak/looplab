"""Doc 52 row 17 (doc 51 §5): the one uncertainty proxy the search layer has — `knn_idw`'s distance
to the nearest evaluated point — is spent by ALL THREE of its callers. The surrogate always did
(UCB); the K-idea panel now ranks by the same acquisition, and the pre-eval kill ABSTAINS on a
candidate the surrogate cannot see. Every rule here is a pure function of folded state.
"""
from __future__ import annotations

import anyio

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.panel import PanelResearcher, _predict, _predict_with_distance, acquisition
from looplab.search.proxy import ProxyScorer

BOUNDS = {"x": (-10.0, 10.0), "y": (-10.0, 10.0)}


class _Seq:
    def __init__(self, ideas):
        self.ideas, self.bounds, self.i = list(ideas), BOUNDS, 0

    def propose(self, state, parent):
        idea = self.ideas[self.i % len(self.ideas)]
        self.i += 1
        return idea


def _state(points, direction="max"):
    st = RunState(direction=direction)
    for i, (x, y, m) in enumerate(points):
        st.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft", params={"x": x, "y": y}),
                           metric=m, status=NodeStatus.evaluated, feasible=True)
    return st


# ------------------------------------------------------------------ the panel

def test_the_pair_and_the_point_estimate_agree_and_a_nan_abstains():
    hist = [({"x": 0.0, "y": 0.0}, 1.0), ({"x": 4.0, "y": 0.0}, 3.0)]
    assert _predict_with_distance({"x": 1.0, "y": 0.0}, hist, BOUNDS) == (_predict({"x": 1.0, "y": 0.0}, hist, BOUNDS), 1.0)
    assert _predict_with_distance({"x": 0.0, "y": 0.0}, hist, BOUNDS) == (1.0, 0.0), "exact match, no distance"
    assert _predict_with_distance({"x": float("nan"), "y": 0.0}, hist, BOUNDS) is None


def test_acquisition_is_the_surrogates_sign_rule():
    assert acquisition(2.0, 3.0, 0.1, "max") == 2.3 and acquisition(2.0, 3.0, 0.1, "min") == 1.7
    assert acquisition(2.0, 3.0, 0.0, "max") == 2.0, "explore=0 is the point estimate"


def _panel_case(explore, direction):
    # History: a plateau near x=0 (metric 5) and one worse point far away at x=8 (metric 4).
    # SAFE sits on the plateau (prediction ~5, nearest 0.5); BOLD is far from everything
    # (prediction pulled toward the far point, ~4.5, nearest ~4): the point estimate prefers SAFE
    # under `max`, the acquisition prefers BOLD once the distance is worth more than the gap.
    safe = Idea(operator="improve", params={"x": 0.5, "y": 0.0}, rationale="safe")
    bold = Idea(operator="improve", params={"x": 4.0, "y": 4.0}, rationale="bold")
    st = _state([(0.0, 0.0, 5.0), (-1.0, 0.0, 5.0), (1.0, 0.0, 5.0), (8.0, 0.0, 4.0)] if direction == "max"
                else [(0.0, 0.0, 5.0), (-1.0, 0.0, 5.0), (1.0, 0.0, 5.0), (8.0, 0.0, 6.0)], direction)
    return PanelResearcher(_Seq([safe, bold]), k=2, warmup=2, explore=explore).propose(st, None)


def test_the_panel_ranks_by_the_point_estimate_at_explore_zero():
    assert _panel_case(0.0, "max").rationale.startswith("safe")
    assert _panel_case(0.0, "min").rationale.startswith("safe")


def test_the_panel_spends_the_distance_once_explore_is_on_in_both_directions():
    assert _panel_case(0.5, "max").rationale.startswith("bold")
    assert _panel_case(0.5, "min").rationale.startswith("bold")
    assert "[panel: best of 2 by surrogate]" in _panel_case(0.5, "max").rationale


def test_the_cli_wires_the_surrogates_own_exploration_knob():
    import inspect
    from looplab import cli
    src = inspect.getsource(cli)
    assert "PanelResearcher(researcher, k=settings.researcher_panel,\n" \
           "                                         explore=settings.surrogate_explore)" in src
    assert PanelResearcher(_Seq([]), k=2).explore == 0.0, "a bare panel keeps the historical ranking"


# ------------------------------------------------------------------ the proxy

def _eval_node(i, x, m, y=0.0):
    return Node(id=i, operator="improve", idea=Idea(operator="improve", params={"x": float(x), "y": y}),
                metric=float(m), status=NodeStatus.evaluated)


def _ladder(n=6):
    st = RunState(direction="min")
    for i in range(n):
        st.nodes[i] = _eval_node(i, i, i)        # x = metric = 0..5, spacing 1
    st.best_node_id = 0
    return st


def test_score_with_uncertainty_returns_the_pair_and_score_the_estimate():
    st = _ladder()
    sc = ProxyScorer()
    cand = _eval_node(9, 4.5, 0)
    pred, nearest = sc.score_with_uncertainty(st, cand)
    assert nearest == 0.5 and sc.score(st, cand) == pred
    assert sc.support_radius(st, cand) == 1.0, "the evaluated points are one apart"


def test_a_far_candidate_is_never_skipped_on_an_extrapolated_number():
    st = _ladder()
    sc = ProxyScorer(kill_fraction=0.34, warmup=4)
    far = _eval_node(9, 99, 0)
    pred, nearest = sc.score_with_uncertainty(st, far)
    assert nearest == 94.0 and sc.abstains(st, far, nearest) is True
    assert sc.should_skip(st, far, pred, nearest) is False, "predicted doomed, but unseen: kept"
    assert sc.should_skip(st, far, pred) is True, "the historical call (no distance) still skips"
    near = _eval_node(8, 4.6, 0)
    pred, nearest = sc.score_with_uncertainty(st, near)
    assert abs(nearest - 0.4) < 1e-9 and sc.abstains(st, near, nearest) is False
    assert sc.should_skip(st, near, pred, nearest) is True, "inside the explored region: the rule stands"
    assert sc.should_skip(st, _eval_node(7, 0.0, 0), 0.0, 0.0) is False, "an exact match is best-seen"


def test_the_abstain_band_is_fail_safe_with_too_little_support():
    st = RunState(direction="min")
    st.nodes[0] = _eval_node(0, 0.0, 10.0)
    sc = ProxyScorer(kill_fraction=0.5, warmup=1)
    cand = _eval_node(9, 3.0, 0)
    assert sc.support_radius(st, cand) is None
    assert sc.abstains(st, cand, 3.0) is True
    assert sc.abstains(st, cand, float("nan")) is True


def test_the_engine_records_the_distance_and_the_abstention(tmp_path):
    from tests.test_strategist import _engine, _read

    state = anyio.run(_engine(tmp_path / "px", proxy_scorer=ProxyScorer(kill_fraction=0.5, warmup=3),
                              proxy_kill_fraction=0.5).run)
    assert state.finished
    rows = [e.data for e in _read(tmp_path / "px") if e.type == "proxy_scored"]
    assert rows, "the proxy ran and was audited"
    for row in rows:
        assert set(row) >= {"score", "skipped", "nearest", "abstained"}
        assert isinstance(row["nearest"], float) and isinstance(row["abstained"], bool)
        if row["abstained"]:
            assert row["skipped"] is False, "an abstention is never a skip"
