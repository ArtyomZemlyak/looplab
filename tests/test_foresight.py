"""FOREAGENT-adapted predict-before-execute (search/foresight.py, arXiv:2601.05930): the LLM world
model predicts which candidate / hypothesis scores best BEFORE any eval — over code (best-of-N) and
over structural/text ideas (the hypothesis panel the numeric surrogate is blind to)."""
from __future__ import annotations

from looplab.agents.roles import _state_brief
from looplab.core.models import (Card, Event, Hypothesis, Idea, Node, NodeStatus, RunState,
                                 hypothesis_id)
from looplab.events.replay import fold
from looplab.search.best_of_n import BestOfNDeveloper
from looplab.search.foresight import ForesightPanelResearcher, rank, verified_report

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

    def repair(self, idea, code, error):
        return self._codes[0]


def test_best_of_n_foresight_picks_predicted_candidate():
    client = _RankClient([1, 0])          # predict candidate 1 (_B) beats 0 (_A)
    dev = BestOfNDeveloper(_Dev([_A, _B], client), n=2)   # foresight on by default
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _B and dev.last_foresight_pick is not None and client.calls == 1
    assert dev.audit_extra()["foresight"] is True
    pick = dev.last_foresight_pick                 # full pick telemetry for the `foresight_selected` event
    assert pick["kind"] == "solution" and pick["chosen"] == 1 and pick["reason"] == "test"


def test_best_of_n_foresight_abstains_falls_back_to_static():
    # predictor errors -> abstain; both candidates tie on static score -> D10 tie-break (bad client
    # -> index 0) -> first candidate. last_foresight_pick stays None.
    dev = BestOfNDeveloper(_Dev([_A, _B], _BadClient()), n=2, listwise=True)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _A and dev.last_foresight_pick is None


def test_best_of_n_foresight_off_is_static_only():
    client = _RankClient([1, 0])
    dev = BestOfNDeveloper(_Dev([_A, _B], client), n=2, foresight=False, listwise=False)
    out = dev.implement(Idea(operator="draft", params={}))
    assert out == _A and client.calls == 0 and dev.last_foresight_pick is None


def test_best_of_n_repair_clears_stale_foresight_pick():
    # a debug-repaired node uses no predictive ranker: the prior implement's pick must not leak into
    # this node's audit / `foresight_selected` event.
    dev = BestOfNDeveloper(_Dev([_A, _B], _RankClient([1, 0])), n=2)
    dev.implement(Idea(operator="draft", params={}))
    assert dev.last_foresight_pick is not None
    dev.repair(Idea(operator="debug", params={}), "x", "err")
    assert dev.last_foresight_pick is None and dev.audit_extra()["foresight"] is False


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
    lf = panel.last_foresight
    assert lf and lf["confidence"] == 0.7 and lf["kind"] == "idea" and lf["chosen"] == 1
    assert len(lf["candidates"]) == 2                # both competing ideas kept for the audit trail


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
        cid = hypothesis_id(s)   # 1 card = 1 hypothesis: cid == hypothesis_id(seed_statement)
        # Populate ONLY the Card board so these tests genuinely PIN the card board as the source — a
        # regression that read state.hypotheses again would leave the board empty and fail.
        st.cards[cid] = Card(id=cid, seed_statement=s, statement=s, verdict="open",
                             status="proposed", evidence=[])
    return st


def test_prioritize_board_orders_open_hypotheses():
    st = _state_with_open_hyps(["h zero", "h one", "h two"])
    base = _SeqResearcher([Idea(operator="draft")], _RankClient([2, 0, 1]))
    panel = ForesightPanelResearcher(base, k=2)
    panel._prioritize_board(st, None)
    ids = list(st.cards)                            # insertion order (card board is the source)
    assert base._hyp_order == [ids[2], ids[0], ids[1]]
    lp = panel.last_hyp_priority
    assert lp["n"] == 3 and lp["reason"] == "test" and lp["order"] == base._hyp_order
    assert [c["id"] for c in lp["ranked"]] == base._hyp_order   # id->statement pairs for the UI


def test_board_and_idea_ranks_trace_under_distinct_spans():
    # The two Researcher ranking steps must NOT collapse into look-alike trace bands: board
    # prioritization (BEFORE propose) traces as `hyp_prioritize`; idea predict-before-execute
    # (AFTER propose) as `foresight_rank`. Guards the UI fix where the first foresight otherwise
    # read as a superfluous duplicate of the second. Spans export on close, so board (which
    # finishes first) is written before idea — the index order encodes the sequence.
    import os
    import tempfile

    import orjson

    from looplab.core import tracing
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    st = _state_with_open_hyps(["h zero", "h one"])
    a = Idea(operator="improve", params={"x": 1.0}, hypothesis="A")
    b = Idea(operator="improve", params={"x": 2.0}, hypothesis="B")
    panel = ForesightPanelResearcher(_SeqResearcher([a, b], _RankClient([1, 0])), k=2)
    with tempfile.TemporaryDirectory() as d:
        tr = Tracer(JsonlSpanExporter(os.path.join(d, "s.jsonl")), run_id="r")
        tok = tracing._current_tracer.set(tr)
        try:
            panel.propose(st, None)          # board prioritize THEN idea foresight, one trace each
        finally:
            tracing._current_tracer.reset(tok)
        names = [orjson.loads(x)["name"]
                 for x in open(os.path.join(d, "s.jsonl"), "rb").read().splitlines()]
    assert "hyp_prioritize" in names                                  # board step: its own name
    assert "foresight_rank" in names                                  # idea step: the foresight name
    assert names.index("hyp_prioritize") < names.index("foresight_rank")   # board precedes idea


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


def test_surrogate_wrapper_reads_through_foresight_telemetry():
    # H8: `_ensure_surrogate` can make SurrogateResearcher the OUTERMOST researcher mid-run, over a
    # ForesightPanelResearcher fallback. The engine's `_emit_role_telemetry` getattrs the predictive
    # telemetry off (and consume-setattrs None back onto) that outermost handle — the read/write
    # properties must delegate BOTH ways to the fallback, while `client` keeps falling through to
    # None (no generic __getattr__: the cli foresight gate probes it on a bare surrogate wrapper).
    from looplab.search.surrogate import SurrogateResearcher

    panel = ForesightPanelResearcher(_SeqResearcher([Idea(operator="draft")], _RankClient([0])), k=2)
    outer = SurrogateResearcher({}, fallback=panel)
    panel.last_hyp_priority = {"order": ["a"], "n": 1}
    panel.last_foresight = {"kind": "idea", "chosen": 0}
    assert outer.last_hyp_priority == {"order": ["a"], "n": 1}     # reads reach the panel's telemetry
    assert outer.last_foresight == {"kind": "idea", "chosen": 0}
    assert outer.last_foresight_pick is None                       # absent on the fallback -> None
    outer.last_hyp_priority = None                                 # the engine's consume path
    outer.last_foresight = None
    assert panel.last_hyp_priority is None and panel.last_foresight is None
    assert getattr(outer, "client", None) is None                  # the cli gate still falls through


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


def test_fold_priority_only_stamped_on_open_cards():
    # a card ranked while OPEN that later gains evidence and resolves must NOT keep a stale priority
    # (models.py contract: priority is None once the card isn't open).
    s0, s1 = "s0 becomes best", "s1 stays open"
    id0, id1 = hypothesis_id(s0), hypothesis_id(s1)
    st = fold([
        Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(type="hypothesis_added", data={"statement": s0}),
        Event(type="hypothesis_added", data={"statement": s1}),
        Event(type="hypothesis_ranked", data={"order": [id0, id1], "confidence": 0.5}),
        Event(type="node_created", data={"node_id": 1, "operator": "draft",
                                         "idea": {"operator": "draft", "hypothesis": s0}}),
        Event(type="node_evaluated", data={"node_id": 1, "metric": 0.9}),   # node 1 becomes best -> s0 supported
    ])
    assert st.hypotheses[id0].status == "supported" and st.hypotheses[id0].priority is None
    assert st.hypotheses[id1].status == "open" and st.hypotheses[id1].priority == 1


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

        def append(self, t, d, **kw):
            self.events.append((t, d))

        def append_many(self, records, **kw):
            self.events.extend(records)

        def read_all(self):
            return []

    class _Researcher:
        last_hyp_priority = {"order": ["a", "b"], "confidence": 0.7, "reason": "r", "n": 2}

    eng = Engine.__new__(Engine)                 # bypass heavy __init__: method only uses store+researcher
    eng.store, eng.researcher = _Store(), _Researcher()
    eng._emit_hypothesis_ranked(5)
    ranked = [e for e in eng.store.events if e[0] == "hypothesis_ranked"]
    assert len(ranked) == 1 and ranked[0][1]["node_id"] == 5 and ranked[0][1]["reason"] == "r"
    eng._emit_hypothesis_ranked(6)               # consumed -> a non-propose node re-emits nothing
    assert len([e for e in eng.store.events if e[0] == "hypothesis_ranked"]) == 1


def test_engine_emits_foresight_selected_for_both_roles():
    from looplab.engine.orchestrator import Engine

    class _Store:
        def __init__(self):
            self.events = []

        def append(self, t, d, **kw):
            self.events.append((t, d))

    class _Dev:
        last_foresight_pick = {"kind": "solution", "chosen": 0, "n": 2, "confidence": 0.6, "reason": "c"}

    class _Res:
        last_foresight = {"kind": "idea", "chosen": 1, "n": 3, "confidence": 0.7, "reason": "i"}

    eng = Engine.__new__(Engine)
    eng.store, eng.developer, eng.researcher = _Store(), _Dev(), _Res()
    eng._emit_foresight_selected(7)
    ev = [e for e in eng.store.events if e[0] == "foresight_selected"]
    assert len(ev) == 2 and {e[1]["kind"] for e in ev} == {"solution", "idea"}
    assert all(e[1]["node_id"] == 7 for e in ev)
    eng._emit_foresight_selected(8)              # consumed on both roles -> no re-emit
    assert len([e for e in eng.store.events if e[0] == "foresight_selected"]) == 2


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


def test_foresight_delegates_developer_surface_for_unified_agent():
    """In UNIFIED mode the SAME agent is researcher+developer. ForesightPanelResearcher must intercept
    propose (predict-before-execute + board prioritization) while DELEGATING the developer surface
    (implement/repair/choose_action/assets) to the wrapped agent, so wrapping once and using it for
    both handles keeps them identical."""
    from looplab.search.foresight import ForesightPanelResearcher

    class FakeUnified:
        client = object()
        bounds = None
        def propose(self, state, parent): return "IDEA"
        def implement(self, idea, parent=None): return "IMPL"
        def repair(self, node, err): return "REPAIR"
        def choose_action(self, state, legal, **k): return 7
        def assets(self): return {"a": 1}

    fp = ForesightPanelResearcher(FakeUnified(), k=2)
    assert fp.implement("x") == "IMPL"
    assert fp.repair("n", "e") == "REPAIR"
    assert fp.choose_action(None, []) == 7
    assert fp.assets() == {"a": 1}
    import pytest
    with pytest.raises(AttributeError):            # missing attr -> AttributeError, not recursion
        _ = fp.no_such_attr_here


# --------------------------------------------------------------------------- #
# prompt minors: reason asked for; hypothesis-board reframing suffix
# --------------------------------------------------------------------------- #

class _RecordingRankClient(_RankClient):
    """_RankClient that also captures the messages of the FIRST ranking call."""
    def __init__(self, order, confidence=0.7):
        super().__init__(order, confidence)
        self.messages = None

    def complete_tool(self, messages, json_schema):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return super().complete_tool(messages, json_schema)


def test_system_prompt_asks_for_reason():
    # Telemetry stamps `reason` onto hypothesis_ranked/foresight_selected as "the model's analysis
    # trace" — the prompt must actually ask for it (it was an unprompted schema field before).
    from looplab.search.foresight import _SYSTEM
    assert "`reason`" in _SYSTEM


def test_rank_kind_hypothesis_appends_board_reframing():
    c = _RecordingRankClient([1, 0])
    rank(c, "r", ["a", "b"], kind="hypothesis")
    sys = c.messages[0]["content"]
    assert "untested HYPOTHESES" in sys and "EXPECTED PAYOFF" in sys
    c2 = _RecordingRankClient([1, 0])
    rank(c2, "r", ["a", "b"])                          # default (idea/code) framing: no suffix
    assert "untested HYPOTHESES" not in c2.messages[0]["content"]


def test_prioritize_board_uses_hypothesis_framing():
    st = _state_with_open_hyps(["h zero", "h one"])
    client = _RecordingRankClient([1, 0])
    panel = ForesightPanelResearcher(_SeqResearcher([Idea(operator="draft")], client), k=2)
    panel._prioritize_board(st, None)
    assert "untested HYPOTHESES" in client.messages[0]["content"]


def test_rank_and_agentic_share_the_user_message():
    # `rank` now renders its user turn through `_rank_user_msg` (the byte-duplicate twin is gone).
    from looplab.search.foresight import _rank_user_msg
    c = _RecordingRankClient([1, 0])
    rank(c, "some report", ["cand a", "cand b"], goal="g", direction="max")
    assert c.messages[1]["content"] == _rank_user_msg("some report", ["cand a", "cand b"], "g", "max")


def test_rank_agentic_falls_back_to_one_shot_without_tools(monkeypatch):
    """rank_agentic with no tools is exactly the one-shot rank() — the agentic loop only engages when
    introspection tools are wired."""
    import looplab.search.foresight as f
    seen = {"n": 0}
    monkeypatch.setattr(f, "rank", lambda *a, **k: (seen.__setitem__("n", seen["n"] + 1) or ([0, 1], 0.5, "r")))
    out = f.rank_agentic(object(), None, "report", ["a", "b"])
    assert out == ([0, 1], 0.5, "r") and seen["n"] == 1


def test_rank_agentic_runs_tool_loop_and_sanitizes(monkeypatch):
    """With tools, rank_agentic drives a tool loop, then sanitizes the emitted order (distinct, valid,
    dropped indices appended)."""
    import looplab.search.foresight as f

    def fake_loop(client, tools, msgs, emit_spec, **kw):
        # drive_tool_loop calls finalize(args) with the emit tool-call ARGUMENTS (a dict)
        return kw["finalize"]({"order": [1, 1, 9, 0], "confidence": 0.8, "reason": "because"})

    monkeypatch.setattr("looplab.agents.agent.drive_tool_loop", fake_loop)
    out = f.rank_agentic(object(), object(), "report", ["a", "b"])
    assert out == ([1, 0], 0.8, "because")     # dup + out-of-range sanitized


def test_rank_agentic_falls_back_when_loop_yields_nothing(monkeypatch):
    import looplab.search.foresight as f
    monkeypatch.setattr("looplab.agents.agent.drive_tool_loop",
                        lambda *a, **k: k["finalize"](None))       # emit gave junk -> None
    monkeypatch.setattr(f, "rank", lambda *a, **k: ([0, 1], 0.3, "fallback"))
    out = f.rank_agentic(object(), object(), "report", ["a", "b"])
    assert out == ([0, 1], 0.3, "fallback")
