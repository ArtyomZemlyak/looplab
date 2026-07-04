"""FOREAGENT-adapted predict-before-execute (search/foresight.py, arXiv:2601.05930): the LLM world
model predicts which candidate / hypothesis scores best BEFORE any eval — over code (best-of-N) and
over structural/text ideas (the hypothesis panel the numeric surrogate is blind to)."""
from __future__ import annotations

from looplab.agents.roles import _state_brief
from looplab.core.models import (Event, Hypothesis, Idea, Node, NodeStatus, RunState,
                                 hypothesis_id)
from looplab.events.replay import fold
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
    order, conf, reason = rank(_RankClient([1, 0], 0.9), "report", [_A, _B])
    assert order == [1, 0] and conf == 0.9 and reason == "test"


def test_rank_completes_partial_order():
    # model ranks only candidate 2; the missing indices are appended in input order.
    order, _, _ = rank(_RankClient([2]), "r", ["a", "b", "c"])
    assert order == [2, 0, 1]


def test_rank_drops_out_of_range_and_dupes():
    order, _, _ = rank(_RankClient([5, 1, 1, 0]), "r", [_A, _B])
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
# open-hypothesis board prioritization (deep research / human / strategist)
# --------------------------------------------------------------------------- #

def _state_with_open_hyps(statements):
    st = RunState(direction="min", goal="minimize loss")
    for s in statements:
        hid = hypothesis_id(s)
        st.hypotheses[hid] = Hypothesis(id=hid, statement=s, status="open", evidence=[])
    return st


def test_prioritize_board_orders_open_hypotheses():
    st = _state_with_open_hyps(["h zero", "h one", "h two"])
    base = _SeqResearcher([Idea(operator="draft")], _RankClient([2, 0, 1]))
    panel = ForesightPanelResearcher(base, k=2)
    panel._prioritize_board(st, None)
    ids = list(st.hypotheses)                       # insertion order
    assert base._hyp_order == [ids[2], ids[0], ids[1]]
    lp = panel.last_hyp_priority
    assert lp["n"] == 3 and lp["reason"] == "test" and lp["order"] == base._hyp_order
    assert [c["id"] for c in lp["ranked"]] == base._hyp_order   # id->statement pairs for the UI


def test_prioritize_board_noop_below_two():
    st = _state_with_open_hyps(["only one"])
    panel = ForesightPanelResearcher(_SeqResearcher([Idea(operator="draft")], _RankClient([0])), k=2)
    panel._prioritize_board(st, None)
    assert panel.base._hyp_order is None and panel.last_hyp_priority is None


def test_prioritize_board_abstains_on_bad_client():
    st = _state_with_open_hyps(["a", "b"])
    panel = ForesightPanelResearcher(_SeqResearcher([Idea(operator="draft")], _BadClient()), k=2)
    panel._prioritize_board(st, None)
    assert panel.base._hyp_order is None


def test_state_brief_orders_board_by_hyp_order():
    st = _state_with_open_hyps(["alpha belief", "beta belief"])
    ids = [hypothesis_id("alpha belief"), hypothesis_id("beta belief")]
    brief = _state_brief(st, None, hyp_order=[ids[1], ids[0]])     # rank beta first
    assert "ordered by predicted payoff" in brief
    assert brief.index("beta belief") < brief.index("alpha belief")


def test_state_brief_default_insertion_order():
    st = _state_with_open_hyps(["alpha belief", "beta belief"])
    brief = _state_brief(st, None)                                 # no ranking
    assert "ordered by predicted payoff" not in brief
    assert brief.index("alpha belief") < brief.index("beta belief")


def test_propose_prioritizes_board_and_ranks_ideas():
    st = _state_with_open_hyps(["h a", "h b"])
    a = Idea(operator="improve", params={"x": 1.0}, hypothesis="h a")
    b = Idea(operator="improve", params={"x": 2.0}, hypothesis="h b")
    panel = ForesightPanelResearcher(_SeqResearcher([a, b], _RankClient([0, 1])), k=2)
    out = panel.propose(st, None)
    assert panel.base._hyp_order is not None          # the board was prioritized for the base
    assert panel.last_hyp_priority is not None
    assert out.hypothesis == "h a"                    # idea ranking still picks the predicted best


# --------------------------------------------------------------------------- #
# fold: hypothesis_ranked -> RunState.hypothesis_ranking + Hypothesis.priority
# --------------------------------------------------------------------------- #

def test_fold_stamps_priority_and_keeps_trace():
    s0, s1 = "interaction features help", "a deeper tree overfits"
    id0, id1 = hypothesis_id(s0), hypothesis_id(s1)
    evs = [
        Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(type="hypothesis_added", data={"statement": s0, "source": "deep_research"}),
        Event(type="hypothesis_added", data={"statement": s1, "source": "human"}),
        # predictor ranked the deeper-tree card first
        Event(type="hypothesis_ranked", data={"node_id": 3, "order": [id1, id0],
                                              "confidence": 0.8, "reason": "small data favors it",
                                              "ranked": [{"id": id1, "statement": s1},
                                                         {"id": id0, "statement": s0}]}),
    ]
    st = fold(evs)
    assert st.hypotheses[id1].priority == 0 and st.hypotheses[id0].priority == 1
    assert st.hypothesis_ranking["reason"] == "small data favors it"
    assert st.hypothesis_ranking["confidence"] == 0.8


def test_fold_priority_none_without_ranking():
    st = fold([Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
               Event(type="hypothesis_added", data={"statement": "x helps"})])
    assert next(iter(st.hypotheses.values())).priority is None
    assert st.hypothesis_ranking is None


# --------------------------------------------------------------------------- #
# engine emit: _emit_hypothesis_ranked reads role telemetry, consumes it
# --------------------------------------------------------------------------- #

def test_engine_emits_and_consumes_hypothesis_ranked():
    from looplab.engine.orchestrator import Engine

    class _Store:
        def __init__(self):
            self.events = []

        def append(self, t, d):
            self.events.append((t, d))

    class _Researcher:
        last_hyp_priority = {"order": ["a", "b"], "confidence": 0.7, "reason": "r", "n": 2}

    eng = Engine.__new__(Engine)                 # bypass heavy __init__: method only uses store+researcher
    eng.store, eng.researcher = _Store(), _Researcher()
    eng._emit_hypothesis_ranked(5)
    ranked = [e for e in eng.store.events if e[0] == "hypothesis_ranked"]
    assert len(ranked) == 1 and ranked[0][1]["node_id"] == 5 and ranked[0][1]["reason"] == "r"
    eng._emit_hypothesis_ranked(6)               # consumed -> a non-propose node re-emits nothing
    assert len([e for e in eng.store.events if e[0] == "hypothesis_ranked"]) == 1


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
