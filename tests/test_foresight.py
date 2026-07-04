"""FOREAGENT-adapted predict-before-execute (search/foresight.py, arXiv:2601.05930): the LLM world
model predicts which candidate / hypothesis scores best BEFORE any eval — over code (best-of-N) and
over structural/text ideas (the hypothesis panel the numeric surrogate is blind to)."""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search import foresight
from looplab.search.best_of_n import BestOfNDeveloper
from looplab.search.foresight import (ForesightPanelResearcher, rank, rank_solutions,
                                      verified_report)

_A = "import json\nprint(json.dumps({'metric': 0.1}))\n"
_B = "import json\nprint(json.dumps({'metric': 0.2}))\n"


class _RankClient:
    """Fake LLM: `complete_tool` returns a fixed ranking; text path is never valid (tool_call only)."""
    def __init__(self, order, confidence=0.7):
        self.order = order
        self.confidence = confidence
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"order": self.order, "confidence": self.confidence, "reason": "test"}

    def complete_text(self, messages):
        return "not json"


class _BadClient:
    def complete_tool(self, messages, json_schema):
        raise RuntimeError("boom")

    def complete_text(self, messages):
        return "nope"


# --------------------------------------------------------------------------- #
# verified_report — the priming "Verified Data Analysis Report"
# --------------------------------------------------------------------------- #

def test_verified_report_assembles_sources():
    r = verified_report(brief="predict churn; metric=AUC",
                        data_profile={"rows": 1000, "cols": 20},
                        memory="node 3 best AUC=0.81")
    assert "TASK / DATA CONTRACT" in r and "predict churn" in r
    assert "DATA PROFILE" in r and "rows" in r
    assert "PRIOR RESULTS" in r and "0.81" in r


def test_verified_report_empty_when_nothing():
    assert verified_report() == ""


# --------------------------------------------------------------------------- #
# rank — the one-call predictive ordering
# --------------------------------------------------------------------------- #

def test_rank_returns_sanitized_order():
    order, conf = rank(_RankClient([1, 0], 0.9), "report", [_A, _B])
    assert order == [1, 0] and conf == 0.9


def test_rank_completes_partial_order():
    # model ranks only candidate 2; the missing indices are appended in input order.
    order, _ = rank(_RankClient([2]), "r", ["a", "b", "c"])
    assert order == [2, 0, 1]


def test_rank_drops_out_of_range_and_dupes():
    order, _ = rank(_RankClient([5, 1, 1, 0]), "r", [_A, _B])
    assert order == [1, 0]


def test_rank_abstains_below_two_items():
    assert rank(_RankClient([0]), "r", [_A]) is None
    assert rank(_RankClient([0]), "r", []) is None


def test_rank_fails_open_on_client_error():
    assert rank(_BadClient(), "r", [_A, _B]) is None
    assert rank(None, "r", [_A, _B]) is None


def test_rank_solutions_returns_index():
    assert rank_solutions(_RankClient([1, 0]), "brief", [_A, _B]) == 1
    assert rank_solutions(_BadClient(), "brief", [_A, _B]) is None


# --------------------------------------------------------------------------- #
# best-of-N integration (code path)
# --------------------------------------------------------------------------- #

class _Dev:
    """Rotating candidate developer with an attached client + brief (so foresight is reachable)."""
    is_code_generating = True

    def __init__(self, codes, client):
        self._codes = codes
        self._i = 0
        self.client = client
        self.brief = "task brief"
        self.last_files = {}
        self.last_deleted = []

    def implement(self, idea):
        c = self._codes[self._i % len(self._codes)]
        self._i += 1
        return c


def test_best_of_n_foresight_picks_predicted_candidate():
    client = _RankClient([1, 0])          # predict candidate 1 (_B) beats 0 (_A)
    dev = BestOfNDeveloper(_Dev([_A, _B], client), n=2)   # foresight on by default
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _B and dev.last_foresight is True and client.calls == 1
    assert dev.audit_extra()["foresight"] is True


def test_best_of_n_foresight_abstains_falls_back_to_static():
    # predictor errors -> abstain; both candidates tie on static score -> D10 tie-break (bad client
    # -> index 0) -> first candidate. last_foresight stays False.
    dev = BestOfNDeveloper(_Dev([_A, _B], _BadClient()), n=2, listwise=True)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _A and dev.last_foresight is False


def test_best_of_n_foresight_off_is_static_only():
    client = _RankClient([1, 0])
    dev = BestOfNDeveloper(_Dev([_A, _B], client), n=2, foresight=False, listwise=False)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _A and client.calls == 0 and dev.last_foresight is False


# --------------------------------------------------------------------------- #
# hypothesis panel (idea path)
# --------------------------------------------------------------------------- #

class _SeqResearcher:
    def __init__(self, ideas, client=None):
        self.ideas = list(ideas)
        self.i = 0
        self.client = client
        self.bounds = None

    def propose(self, state, parent):
        idea = self.ideas[self.i % len(self.ideas)]
        self.i += 1
        return idea


def _state():
    st = RunState(direction="min", goal="minimize loss")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}),
                       metric=0.5, status=NodeStatus.evaluated, feasible=True)
    return st


def test_panel_picks_predicted_best_hypothesis():
    a = Idea(operator="improve", params={"x": 1.0}, hypothesis="deeper tree overfits")
    b = Idea(operator="improve", params={"x": 2.0}, hypothesis="interaction features help")
    panel = ForesightPanelResearcher(_SeqResearcher([a, b], _RankClient([1, 0])), k=2)
    out = panel.propose(_state(), None)
    assert out.hypothesis == "interaction features help"
    assert "foresight" in out.rationale
    assert panel.last_foresight and panel.last_foresight["confidence"] == 0.7


def test_panel_abstain_returns_first_idea():
    a = Idea(operator="improve", params={"x": 1.0}, hypothesis="A")
    b = Idea(operator="improve", params={"x": 2.0}, hypothesis="B")
    panel = ForesightPanelResearcher(_SeqResearcher([a, b], _BadClient()), k=2)
    out = panel.propose(_state(), None)
    assert out.hypothesis == "A" and "foresight" not in out.rationale
    assert panel.last_foresight is None


def test_panel_passthrough_without_client():
    a = Idea(operator="draft", params={"x": 1.0})
    panel = ForesightPanelResearcher(_SeqResearcher([a, a], client=None), k=2)
    out = panel.propose(_state(), None)
    assert out is a                       # single base.propose, no ranking


def test_panel_k1_passthrough():
    a = Idea(operator="draft", params={"x": 1.0})
    panel = ForesightPanelResearcher(_SeqResearcher([a], _RankClient([0])), k=1)
    assert panel.propose(_state(), None) is a


def test_panel_forwards_engine_hints_to_base():
    a = Idea(operator="draft", params={"x": 1.0}, hypothesis="A")
    b = Idea(operator="draft", params={"x": 2.0}, hypothesis="B")
    base = _SeqResearcher([a, b], _RankClient([0, 1]))
    panel = ForesightPanelResearcher(base, k=2)
    panel._novelty_feedback = "you already tried X"      # engine setattr on the active researcher
    panel.propose(_state(), None)
    assert base._novelty_feedback == "you already tried X"


# --------------------------------------------------------------------------- #
# defaults + wiring
# --------------------------------------------------------------------------- #

def test_foresight_defaults_on():
    from looplab.core.config import Settings
    s = Settings()
    assert s.foresight is True and s.foresight_panel == 2


def test_make_roles_passes_foresight_to_best_of_n():
    from pathlib import Path
    from looplab.core.config import Settings
    from looplab.adapters.tasks import load_task, make_roles
    root = Path(__file__).resolve().parents[1]
    task = load_task(root / "examples" / "code_regression_task.json")
    _r, dev = make_roles(task, Settings(backend="llm", best_of_n=2, unified_agent=False))
    assert isinstance(dev, BestOfNDeveloper) and dev.foresight is True
