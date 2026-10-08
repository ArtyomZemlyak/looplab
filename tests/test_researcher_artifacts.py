"""A Researcher that may PROPOSE artifact nodes (doc 73 §1.4, stage 3; `agents/artifact_ideas.py`),
under `Settings.researcher_artifacts`.

OFF — the default, and what a resumed pre-field run reads — is the historical emit schema, user turn
and node payload byte for byte. ON, the schema shows `node_kind`/`uses`, the user turn lists the
PRODUCED ARTIFACTS, and the fold turns an idea's `node_kind: "artifact"` into an artifact node and its
`uses` into the artifacts a node reads (real artifact producers only).
"""
from __future__ import annotations

import pytest

from looplab.agents.artifact_ideas import (artifact_cue, drop_artifact_fields, emission_model,
                                           researcher_artifacts_enabled, strip_artifact_fields)
from looplab.agents.roles import LLMResearcher
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import (ArtifactIdeaEmission, Idea, IdeaEmission, Node, NodeStatus,
                                 RunState, durable_idea_payload)
from looplab.events.eventstore import Event
from looplab.events.replay import fold


def _created(seq, nid, idea, **top):
    return Event(seq=seq, ts=0.0, type="node_created",
                 data={"node_id": nid, "parent_ids": top.pop("parent_ids", []), "operator": "draft",
                       "idea": {"operator": "draft", **idea}, "code": "x", **top})


def _state_with_artifact() -> RunState:
    st = RunState(direction="max", goal="g")
    st.nodes[4] = Node(id=4, operator="inject", status=NodeStatus.evaluated, kind="artifact",
                       idea=Idea(operator="inject", rationale="tokenize the corpus once"))
    st.nodes[5] = Node(id=5, operator="draft", status=NodeStatus.evaluated, metric=0.5,
                       idea=Idea(operator="draft", rationale="baseline"))
    return st


# ------------------------------------------------------------------------------- the switch
def test_the_switch_has_one_reader_is_off_by_default_and_run_pinned():
    assert Settings().researcher_artifacts is False
    assert researcher_artifacts_enabled(Settings()) is False
    assert researcher_artifacts_enabled(Settings(researcher_artifacts=True)) is True
    assert researcher_artifacts_enabled(object()) is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["researcher_artifacts"] is False
    legacy = {k: v for k, v in Settings(researcher_artifacts=True).masked_snapshot().items()
              if k != "researcher_artifacts"}
    assert settings_from_snapshot(legacy).researcher_artifacts is False


def test_off_is_the_historical_schema_and_payload():
    assert emission_model(False) is IdeaEmission
    props = IdeaEmission.model_json_schema()["properties"]
    assert "node_kind" not in props and "uses" not in props, "hidden from every run's emit tool"
    shown = ArtifactIdeaEmission.model_json_schema()["properties"]
    assert {"node_kind", "uses"} <= set(shown)
    idea = Idea(operator="draft", rationale="r")
    assert "node_kind" not in idea.model_dump() and "uses" not in durable_idea_payload(idea)


def test_off_strips_what_a_model_volunteers():
    args = {"operator": "draft", "node_kind": "artifact", "uses": [3]}
    assert strip_artifact_fields(args, False) == {"operator": "draft"}
    assert strip_artifact_fields(args, True) == args
    idea = Idea(operator="draft", node_kind="artifact", uses=[3])
    assert drop_artifact_fields(idea, False).node_kind is None
    assert drop_artifact_fields(idea, False).uses == []
    assert drop_artifact_fields(idea, True) is idea


def test_the_cue_lists_only_produced_artifacts():
    st = _state_with_artifact()
    cue = artifact_cue(st)
    assert "#4: tokenize the corpus once" in cue and "#5" not in cue
    assert "none yet" in artifact_cue(RunState(direction="max", goal="g"))


# ------------------------------------------------------------------------------- the fold
def test_an_idea_declared_artifact_folds_to_an_artifact_node_and_uses_keep_real_producers():
    rows = [_created(0, 0, {"node_kind": "artifact", "rationale": "prepare"}),
            _created(1, 1, {"rationale": "plain"}),
            _created(2, 2, {"uses": [0, 1, 9]})]
    st = fold(rows)
    assert st.nodes[0].kind == "artifact" and st.nodes[1].kind is None
    assert st.nodes[2].uses == [0], "#1 is an experiment and #9 does not exist: links dropped"


def test_an_idea_with_no_valid_uses_still_inherits_its_parents():
    rows = [_created(0, 0, {"node_kind": "artifact"}),
            _created(1, 1, {"uses": [0]}),
            _created(2, 2, {"uses": [7]}, parent_ids=[1])]
    assert fold(rows).nodes[2].uses == [0]


def test_a_top_level_inject_key_still_wins():
    rows = [_created(0, 0, {"node_kind": "artifact"}), _created(1, 1, {"node_kind": "artifact"}),
            _created(2, 2, {"uses": [1]}, uses=[0])]
    assert fold(rows).nodes[2].uses == [0]


# ------------------------------------------------------------------------------- driven prompts
class _Client:
    def __init__(self, reply=None):
        self.messages = None
        self.schema = None
        self.reply = reply or {"operator": "draft", "params": {"x": 1.0}, "rationale": "r",
                               "concept_mode": "full"}

    def complete_tool(self, messages, json_schema=None, **_kw):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
            self.schema = json_schema
        return dict(self.reply)


def _user(messages):
    return next(m["content"] for m in messages if m["role"] == "user")


def test_the_plain_researcher_off_is_byte_for_byte_and_on_shows_the_list():
    st = _state_with_artifact()
    off_client, on_client = _Client(), _Client()
    LLMResearcher(off_client).propose(st, None)
    on = LLMResearcher(on_client, artifact_ideas=True)
    on.propose(st, None)
    assert "PRODUCED ARTIFACTS" not in _user(off_client.messages)
    assert _user(on_client.messages) == _user(off_client.messages).replace(
        "\nPropose the next Idea", artifact_cue(st) + "\nPropose the next Idea", 1)


def test_the_plain_researcher_carries_an_artifact_idea_only_when_on():
    reply = {"operator": "draft", "params": {}, "rationale": "prepare", "concept_mode": "full",
             "node_kind": "artifact"}
    st = _state_with_artifact()
    assert LLMResearcher(_Client(reply)).propose(st, None).node_kind is None
    assert LLMResearcher(_Client(reply), artifact_ideas=True).propose(st, None).node_kind == "artifact"


def test_the_tool_using_researcher_schema_and_user_turn(monkeypatch):
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher
    seen = {}

    def _fake(client, tools, messages, emit_spec, **kw):
        seen.setdefault("calls", []).append(([dict(m) for m in messages], emit_spec))
        return Idea(operator="draft", params={}, rationale="ok")

    monkeypatch.setattr(agent_mod, "run_phase", _fake)
    st = _state_with_artifact()
    ToolUsingResearcher(client=object(), tools=None).propose(st, None)
    ToolUsingResearcher(client=object(), tools=None, artifact_ideas=True).propose(st, None)
    (off_msgs, off_spec), (on_msgs, on_spec) = seen["calls"]
    off_props = off_spec["function"]["parameters"]["properties"]
    on_props = on_spec["function"]["parameters"]["properties"]
    assert off_spec["function"]["parameters"] == IdeaEmission.model_json_schema()
    assert "uses" in on_props and "uses" not in off_props
    assert "PRODUCED ARTIFACTS" in _user(on_msgs) and "PRODUCED ARTIFACTS" not in _user(off_msgs)


@pytest.mark.parametrize("on", [False, True])
def test_the_tool_using_finalize_keeps_the_fields_only_when_on(on):
    from looplab.agents.agent import ToolUsingResearcher
    r = ToolUsingResearcher(client=object(), tools=None, artifact_ideas=on)
    idea = r._finalize({"operator": "draft", "params": {}, "rationale": "prep",
                        "concept_mode": "full", "node_kind": "artifact", "uses": [4]}, [])
    assert (idea.node_kind == "artifact" and idea.uses == [4]) is on
    assert idea.rationale == "prep", "OFF is a normal idea, never the fallback draft"

