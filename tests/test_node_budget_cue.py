"""The proposal prompt states the NODE budget and the plan phase (Q-3, the Researcher's context audit).

THE DEFECT, rendered rather than read. A scripted toy run was driven through the real
`cli/__init__.py::_engine` + `Engine.run` with a recording client and the shipped `Settings`: a first
node, three evaluated nodes with one clear winner, two failures with the same error, a proposal the
novelty gate rejected, a deep-research memo + prior-run lessons + open beliefs, and the plan's
endgame reserve (nodes 8-9 of 10). Not one of the Researcher's proposal prompts said how many
experiments the run had left or that the plan had entered its endgame — the proposal for the run's
LAST node read exactly like the one for its fourth. The two budget cues that exist are both silent in
the shipped config (`budget_aware` is off and needs a `max_eval_seconds`; the money cue needs an
`llm_budget_usd` that ships 0), while `max_nodes` bounds every run. The Strategist has been handed
`node_budget_frac` since the endgame reserve landed.

Every test below drives a REAL `Engine` (its own `_set_complexity_hint`, its own node-reservation
arithmetic, a plan row it wrote itself) and reads the prompt a REAL researcher sent. Offline.
"""
from __future__ import annotations

from looplab.agents.roles import LLMResearcher
from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                 settings_from_snapshot)
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.options import EngineOptions
from looplab.engine.proposal_cues import ProposalCuesMixin
from looplab.events.replay import fold
from tests.factories import make_engine


class _Client:
    """`parse_structured(tool_call)` fake: records the first request, emits a valid Idea."""

    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema=None, **_kw):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "r"}


def _engine(tmp_path, name, *, cue=True, max_nodes=10, reserve=0.2):
    return make_engine(tmp_path / name, max_nodes=max_nodes, n_seeds=3,
                       endgame_reserve_frac=reserve, node_budget_cue=cue)


def _grow(engine, n_nodes: int):
    """The engine writes its own plan row, then `n_nodes` evaluated nodes land — real events."""
    engine._ensure_plan(fold(engine.store.read_all()))
    for i in range(n_nodes):
        idea = Idea(operator="draft", params={"x": float(i), "y": 0.0}, rationale=f"idea {i}")
        engine.store.append("node_created", {"node_id": i, "parent_ids": [], "operator": "draft",
                                             "idea": durable_idea_payload(idea), "code": "",
                                             "files": {}, "generation": 0})
        engine.store.append("node_evaluated", {"node_id": i, "generation": 0,
                                               "metric": float(10 - i), "eval_seconds": 0.1,
                                               "extra_metrics": {}, "stdout_tail": "",
                                               "trials": [], "violations": []})
    return fold(engine.store.read_all())


def _user_turn(engine, state) -> str:
    """The user turn a real `LLMResearcher` sends after the engine stamped its cues on it."""
    client = _Client()
    researcher = LLMResearcher(client)
    engine._set_complexity_hint(state, None, researcher=researcher)
    researcher.propose(state, None)
    return next(m["content"] for m in client.messages if m["role"] == "user")


# --------------------------------------------------------------------------- what the role is told
def test_the_budget_and_the_reserve_are_stated_before_the_endgame(tmp_path):
    engine = _engine(tmp_path, "search")
    state = _grow(engine, 4)
    assert state.plan["endgame_start"] == 8          # the engine's own plan, not a hand-built one
    turn = _user_turn(engine, state)
    assert ("Node budget: 4 of this run's 10 experiment(s) exist already, so at most 6 more will "
            "run, this one included.") in turn
    assert ("The run's plan reserves experiments #8–#9 for its endgame (an ensemble of the two "
            "best results and refinements of the champion), so at most 4 more can open a new "
            "direction before that reserve begins.") in turn


def test_inside_the_reserve_the_proposal_is_told_it_is(tmp_path):
    engine = _engine(tmp_path, "endgame")
    turn = _user_turn(engine, _grow(engine, 8))
    assert "at most 2 more will run, this one included." in turn
    assert ("This proposal falls inside the plan's endgame reserve (experiments #8–#9), which the "
            "plan spends on an ensemble of the two best results and refinements of the champion."
            ) in turn


def test_the_last_slot_is_named_as_the_last(tmp_path):
    engine = _engine(tmp_path, "last")
    turn = _user_turn(engine, _grow(engine, 9))
    assert ("Node budget: 9 of this run's 10 experiment(s) exist already — this proposal is for "
            "the run's LAST experiment slot.") in turn


def test_a_live_add_nodes_override_moves_the_number_with_it(tmp_path):
    """The limit is the admission's own (`_hard_node_reservation_limit`), so an operator's
    `add_nodes` reaches the prompt the moment it reaches the admission."""
    engine = _engine(tmp_path, "extended")
    state = _grow(engine, 4)
    state.budget_overrides["add_nodes"] = 5
    assert "4 of this run's 15 experiment(s) exist already, so at most 11 more" in _user_turn(
        engine, state)


def test_a_run_with_no_plan_gets_the_budget_sentence_alone(tmp_path):
    engine = _engine(tmp_path, "no-plan", reserve=0.0)
    state = _grow(engine, 2)
    assert state.plan is None
    turn = _user_turn(engine, state)
    assert "Node budget: 2 of this run's 10 experiment(s) exist already" in turn
    assert "endgame" not in turn


def test_the_sentence_follows_the_plans_own_kinds(tmp_path):
    """A Strategist may switch the champion sweep off (`endgame_sweep=false`); the plan row then
    carries `merge` alone and the cue must not promise refinements the dispatcher will not run."""
    engine = _engine(tmp_path, "merge-only")
    engine._endgame_sweep = False
    turn = _user_turn(engine, _grow(engine, 4))
    assert "for its endgame (an ensemble of the two best results), so" in turn
    assert "refinements of the champion" not in turn


def test_the_agentic_researcher_is_told_the_same_bytes(tmp_path, monkeypatch):
    """`_complexity_hint` is spliced by BOTH researchers through `collect_hint_cues`."""
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher

    engine = _engine(tmp_path, "agentic")
    state = _grow(engine, 4)
    seen: dict = {}

    def _fake_run_phase(client, tools, messages, emit_spec, **kw):
        seen["messages"] = [dict(m) for m in messages]
        return Idea(operator="draft", params={}, rationale="ok")

    monkeypatch.setattr(agent_mod, "run_phase", _fake_run_phase)
    agentic = ToolUsingResearcher(client=object(), tools=None)
    engine._set_complexity_hint(state, None, researcher=agentic)
    agentic.propose(state, None)
    user = next(m["content"] for m in seen["messages"] if m["role"] == "user")
    fragment, _ = engine._cue_node_budget(state, None, agentic)
    assert fragment and fragment in user


# --------------------------------------------------------------------------- what it must not do
def test_off_is_the_historical_prompt_byte_for_byte(tmp_path, monkeypatch):
    """OFF must reproduce the prompt of a tree without the cue: compared against the same engine
    with the cue method removed from the registry's reach."""
    engine = _engine(tmp_path, "off", cue=False)
    state = _grow(engine, 8)
    off = _user_turn(engine, state)
    monkeypatch.setattr(ProposalCuesMixin, "_cue_node_budget", lambda self, s, p, r: ("", []))
    assert off == _user_turn(engine, state)
    assert "Node budget" not in off


def test_the_cue_records_no_steering_entry_and_erases_no_other(tmp_path):
    """`core/cards.py::normalize_steering_context` drops the WHOLE snapshot on a kind outside its
    closed vocabulary, so a steering entry here would erase every other cue's Card receipt."""
    engine = _engine(tmp_path, "steering")
    engine._complexity_cue = True
    state = _grow(engine, 4)
    receipts = {}
    for on in (False, True):
        engine._node_budget_cue = on
        researcher = LLMResearcher(_Client())
        engine._set_complexity_hint(state, None, researcher=researcher)
        assert ("Node budget" in researcher._complexity_hint) is on
        receipts[on] = researcher._steering_context
    assert {"kind": "complexity", "siblings": 4, "level": "advanced"} in receipts[False]
    assert receipts[True] == receipts[False]


def test_the_flag_ships_on_resumes_off_and_is_off_at_every_constructor():
    assert Settings().node_budget_cue is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["node_budget_cue"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items() if k != "node_budget_cue"}
    assert settings_from_snapshot(legacy).node_budget_cue is False
    assert settings_from_snapshot(Settings().masked_snapshot()).node_budget_cue is True
    assert EngineOptions().node_budget_cue is False
    assert EngineOptions.from_settings(Settings()).node_budget_cue is True
