"""The Strategist's brief states the node budget and the plan's endgame reserve (doc 69 §6.2, 69.25).

`minionerec-backbones-v10`: the operator's "main axis is the BACKBONE" directive became
`evolutionary` -> `merge_mode: ensemble` -> node 17, on the run's last budget slot. The brief named
how many nodes EXIST (`nodes=`) but not how many the run HAS, nor that the plan's reserve was about
to spend them; the rule Strategist has read `StrategyContext.node_budget_frac` since the reserve
landed, and the model's brief never rendered it. Under `Settings.strategist_budget_brief` every
consult's brief gains one line from the facts the Researcher's per-proposal cue already states
(`engine/proposal_cues.py::_cue_node_budget`): the ceiling the dispatcher opens nodes against and the
folded plan row, "inside" by `engine/plan.py::in_endgame`'s own rule. OFF, the brief is byte for
byte what it was. (69.25's other half — re-planning on an inject batch — is still open.)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.agents.strategist import StrategyContext, _node_budget_note, _strategist_brief
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea, Node, RunState
from looplab.engine.options import EngineOptions
from looplab.engine.plan import build_plan, in_endgame
from tests.factories import make_engine

_KINDS = ["merge", "sweep"]
_SPEND = "an ensemble of the two best results and refinements of the champion"


def _ctx(**kw) -> StrategyContext:
    return StrategyContext(eval_parallel=1, **kw)


def _state(n: int, plan=None) -> RunState:
    state = RunState()
    for i in range(n):
        state.nodes[i] = Node(id=i, parent_ids=[], operator="draft", code="",
                              idea=Idea(operator="draft", params={}, rationale="r"))
    state.plan = plan
    return state


# ------------------------------------------------------------------------------------ the line
def test_the_line_before_the_reserve_names_what_is_left_and_where_the_reserve_begins():
    line = _node_budget_note(_ctx(node_count=12, node_budget_limit=18, endgame_start=15,
                                  endgame_kinds=_KINDS))
    assert line == ("NODE BUDGET: 12 of this run's 18 experiment(s) exist, so at most 6 more will "
                    "run; the plan reserves experiments #15-#17 for its endgame (" + _SPEND + "), "
                    "so at most 3 more can open a new direction before it begins.\n")


def test_the_line_inside_the_reserve_says_what_the_dispatcher_spends_it_on():
    line = _node_budget_note(_ctx(node_count=16, node_budget_limit=18, endgame_start=15,
                                  endgame_kinds=_KINDS))
    assert line == ("NODE BUDGET: 16 of this run's 18 experiment(s) exist, so at most 2 more will "
                    "run; the run is INSIDE the plan's endgame reserve (experiments #15-#17): the "
                    "dispatcher replaces every node the search would open with " + _SPEND
                    + ", whatever policy you choose.\n")


@pytest.mark.parametrize("n", [13, 14, 15, 16, 17, 18])
def test_inside_is_the_dispatcher_s_own_rule_for_a_bounded_episode(n):
    """A stall episode `[14, 17)`: "inside" exactly where `in_endgame` says so; past its end (the
    `reopened` row is the next turn's) no plan clause at all. MUTATION: drop the `used < end` test."""
    plan = {"endgame_start": 14, "endgame_end": 17}
    line = _node_budget_note(_ctx(node_count=n, node_budget_limit=20, endgame_start=14,
                                  endgame_end=17, endgame_kinds=["merge"]))
    assert ("INSIDE the plan's endgame reserve (experiments #14-#16)" in line) is in_endgame(plan, n)
    if n < 14:
        assert "reserves experiments #14-#16" in line
    if n >= 17:
        assert line.endswith("will run.\n")


def test_each_clause_speaks_only_when_it_has_something_to_say():
    assert _node_budget_note(_ctx()) == "", "no budget sent: say nothing"
    assert _node_budget_note(_ctx(node_budget_limit=0)) == ""
    # The model coerces True to 1; a duck-typed context reaches the guard as the bool itself.
    duck = SimpleNamespace(node_budget_limit=True, node_count=0, endgame_start=None,
                           endgame_end=None, endgame_kinds=[])
    assert _node_budget_note(duck) == "", "a bool is not a budget"
    bare = _node_budget_note(_ctx(node_count=3, node_budget_limit=10))
    assert bare == "NODE BUDGET: 3 of this run's 10 experiment(s) exist, so at most 7 more will run.\n"
    # A plan whose reserve cannot be placed or names no kind says nothing about the reserve.
    for start, kinds in ((0, _KINDS), (10, _KINDS), (12, _KINDS), (5, []), (5, ["unknown"])):
        line = _node_budget_note(_ctx(node_count=3, node_budget_limit=10, endgame_start=start,
                                      endgame_kinds=kinds))
        assert line == bare, (start, kinds)
    assert "at most 0 more" in _node_budget_note(_ctx(node_count=12, node_budget_limit=10))


def test_off_the_brief_is_byte_for_byte_the_historical_one():
    """MUTATION: render the line unconditionally -> the default context gains text."""
    state = RunState()
    assert _strategist_brief(state, _ctx()) == _strategist_brief(state, _ctx(endgame_start=5))
    on_ctx = _ctx(node_count=2, node_budget_limit=10, endgame_start=8, endgame_kinds=_KINDS)
    on = _strategist_brief(state, on_ctx)
    assert "NODE BUDGET: 2 of this run's 10" in on
    assert on.replace(_node_budget_note(on_ctx), "", 1) == _strategist_brief(state, _ctx(node_count=2))


# ------------------------------------------------------------------------------------ the engine
def _engine(tmp_path, *, on: bool, max_nodes: int = 18):
    return make_engine(tmp_path / "run", strategist_budget_brief=on, max_nodes=max_nodes)


def test_the_engine_hands_the_dispatcher_s_ceiling_and_the_plan_row_and_nothing_when_off(tmp_path):
    """MUTATIONS: drop the switch; read `policy.max_nodes` instead of the reservation ceiling; drop the
    plan facts."""
    plan = build_plan(max_nodes=18, n_seeds=2, reserve_frac=0.2, at_node=0)
    state = _state(12, plan)
    engine = _engine(tmp_path / "on", on=True)
    ctx = engine._strategy_ctx(state)
    assert ctx.node_budget_limit == engine._hard_node_reservation_limit(state) == 18
    assert (ctx.endgame_start, ctx.endgame_end, ctx.endgame_kinds) == (14, None, _KINDS)
    off = _engine(tmp_path / "off", on=False)._strategy_ctx(state)
    assert off.node_budget_limit is None and off.endgame_start is None and off.endgame_kinds == []


def test_the_ceiling_is_the_admission_s_own_including_refunds(tmp_path, monkeypatch):
    """The number the Researcher's cue and the dispatcher read, not the configured budget."""
    engine = _engine(tmp_path, on=True)
    monkeypatch.setattr(type(engine), "_hard_node_reservation_limit", lambda self, state: 21)
    assert engine._strategy_ctx(_state(3)).node_budget_limit == 21
    monkeypatch.setattr(type(engine), "_hard_node_reservation_limit", lambda self, state: 0)
    assert engine._strategy_ctx(_state(3)).node_budget_limit is None


class _Capture:
    """A client that records what the Strategist sends and answers nothing usable, so the decision
    falls back to the rule — the PROMPT is the subject here, not the answer."""

    def __init__(self):
        self.sent: list = []

    def complete_tool(self, messages, schema=None, **_kw):
        self.sent.append(messages)
        raise RuntimeError("no answer")

    def complete(self, messages, **_kw):
        self.sent.append(messages)
        raise RuntimeError("no answer")

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.sent.append(messages)
        raise RuntimeError("no answer")


def test_the_consult_s_own_prompt_carries_the_line_only_when_on(tmp_path):
    """Driven: the engine builds the context and the LLM Strategist sends its brief. MUTATIONS: drop
    the note from `_strategist_brief`; stop spreading `_node_budget_ctx` into the context."""
    from looplab.agents.strategist import LLMStrategist

    plan = build_plan(max_nodes=18, n_seeds=2, reserve_frac=0.2, at_node=0)
    for on in (True, False):
        engine = _engine(tmp_path / str(on), on=on)
        client = _Capture()
        state = _state(15, plan)
        LLMStrategist(client).decide(state, engine._strategy_ctx(state))
        user = [m["content"] for msgs in client.sent for m in msgs if m["role"] == "user"]
        assert user, "the Strategist sent nothing"
        assert ("the run is INSIDE the plan's endgame reserve (experiments #14-#17)" in user[0]) is on


# ------------------------------------------------------------------------------------ the switch
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_off_at_every_constructor(tmp_path):
    assert Settings().strategist_budget_brief is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["strategist_budget_brief"] is False
    legacy = Settings().masked_snapshot()
    legacy.pop("strategist_budget_brief")
    assert settings_from_snapshot(legacy).strategist_budget_brief is False
    assert EngineOptions().strategist_budget_brief is False
    assert make_engine(tmp_path / "bare")._strategist_budget_brief is False


def test_the_one_reader_defaults_off_for_an_object_that_never_ran_init():
    from looplab.engine.shared import strategist_budget_brief
    assert strategist_budget_brief(SimpleNamespace()) is False
    assert strategist_budget_brief(SimpleNamespace(_strategist_budget_brief=True)) is True
