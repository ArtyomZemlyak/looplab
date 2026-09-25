"""A re-proposal that is still a duplicate is dropped where nothing is reserved yet; and the novelty
judge is told which prior experiments never ran their idea.

Measured 2026-09-25 on MiniOneRec inf12: the gate rejected "de-duplicate the first decode step" four
times, each time against nodes whose code never contained it (a flag revert, a fused MLP); the
Researcher handed the idea back reworded, the second proposal was never judged, and it became card-17
and card-18 and two builds.
"""
from __future__ import annotations

import types

from looplab.agents import agent as agent_module
from tests.test_novelty_rejection_audit import _Gate, _idea, _state


def _llm_gate(monkeypatch, verdicts):
    """`agentic_struct` answers from `verdicts` in order; the messages it was sent are recorded."""
    sent: list = []

    def fake(_client, _tools, msgs, _model, **_kw):
        sent.append(msgs)
        dup = verdicts.pop(0) if verdicts else False
        return types.SimpleNamespace(is_duplicate=dup, near_node_id=0 if dup else None,
                                     reason="same idea reworded" if dup else "novel")

    monkeypatch.setattr(agent_module, "agentic_struct", fake)
    return sent


def _gate():
    gate = _Gate()
    gate._reflect_client = lambda: object()
    gate._repropose_with_feedback = lambda repropose, hint, idea, researcher=None: repropose()
    return gate


def test_a_reproposal_that_is_still_a_duplicate_is_dropped_before_any_reservation(tmp_path, monkeypatch):
    gate, state = _gate(), _state(tmp_path)
    _llm_gate(monkeypatch, [True, True])
    out = gate._llm_novelty_gate(state, _idea("dedup step 1"), repropose=lambda: _idea("dedup step one"),
                                 drop_repeated_duplicate=True)
    assert out is None
    actions = [data.get("action") for name, data in gate.store.appended if name == "novelty_rejected"]
    assert actions == ["reproposed", "dropped"], actions


def test_a_reproposal_that_is_novel_is_kept(tmp_path, monkeypatch):
    gate, state = _gate(), _state(tmp_path)
    _llm_gate(monkeypatch, [True, False])
    out = gate._llm_novelty_gate(state, _idea("dedup step 1"), repropose=lambda: _idea("fp8 decode"),
                                 drop_repeated_duplicate=True)
    assert out is not None and out.rationale == "fp8 decode"


def test_without_the_flag_the_reproposal_is_returned_unjudged_as_before(tmp_path, monkeypatch):
    """A caller that already holds a reservation cannot drop the idea; it keeps the old behaviour."""
    gate, state = _gate(), _state(tmp_path)
    sent = _llm_gate(monkeypatch, [True, True])
    out = gate._llm_novelty_gate(state, _idea("dedup step 1"), repropose=lambda: _idea("dedup step one"))
    assert out is not None and out.rationale == "dedup step one" and len(sent) == 1


def test_the_judge_sees_each_prior_outcome_and_the_never_ran_rule(tmp_path, monkeypatch):
    gate, state = _gate(), _state(tmp_path)
    sent = _llm_gate(monkeypatch, [False])
    gate._llm_novelty_gate(state, _idea("anything"))
    system, user = sent[0][0]["content"], sent[0][1]["content"]
    assert "NEVER RAN is not a tried idea" in system and "inert_path" in system
    assert "#0 node_operator=" in user and "outcome=metric=0.42" in user


def test_an_inert_node_reads_as_failed_inert_path(tmp_path):
    from looplab.engine.novelty import _prior_outcome
    node = types.SimpleNamespace(status=types.SimpleNamespace(value="failed"), error_reason="inert_path")
    assert _prior_outcome(node) == "failed:inert_path"
