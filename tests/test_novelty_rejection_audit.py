"""The one reject/re-propose/audit protocol both novelty gates share (doc 25 EC-05).

The LLM gate and the semantic gate each carried their own copy of "reject the near-duplicate, ask
the Researcher ONCE more with the duplicate surfaced, then record what actually happened". The two
copies were never the interesting part — the AUDIT is. `novelty_rejected.action` is the only place
the log says whether a paid re-proposal actually changed the idea (`reproposed`), whether the
Researcher handed back the same thing (`kept`), or whether the budget ran out mid-gate
(`budget_exceeded`). A drifted copy would keep gating correctly and quietly stop recording why, and
nothing downstream would go red.

So these pin the protocol itself, and then pin that BOTH gates still route through it with their own
`kind` and their own one differing payload key (`reason` for llm, `similarity` for semantic).
"""
from __future__ import annotations

import types

import pytest

from looplab.core.errors import BudgetExceeded
from looplab.core.models import Idea
from looplab.engine.novelty import NoveltyGateMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


class _Store:
    """Records appends; `read_all` stays real enough for `_prospective_node_id`'s fallback."""

    def __init__(self):
        self.appended: list[tuple[str, dict]] = []

    def append(self, event_type, data):
        self.appended.append((event_type, data))

    def read_all(self):
        return []


class _Gate(NoveltyGateMixin):
    """A bare mixin host — `self` IS the Engine in production, and nothing else is touched here."""

    def __init__(self, stance="balanced"):
        self.store = _Store()
        self.researcher = types.SimpleNamespace()
        self._novelty_stance = stance
        self._novelty_mode = "algo"
        self._novelty_semantic = True
        self._novelty_semantic_threshold = 0.9
        self._novelty_epsilon = 0.0


def _state(tmp_path, *, rationale="the duplicate experiment", failed=False):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {},
                                       "rationale": rationale}})
    if failed:
        s.append("node_failed", {"node_id": 0, "error": "boom", "reason": "sandbox_error"})
    else:
        s.append("node_evaluated", {"node_id": 0, "metric": 0.42})
    return fold(s.read_all())


def _idea(rationale="a fresh proposal"):
    return Idea(operator="improve", params={}, rationale=rationale)


def _reject(gate, state, idea, *, repropose=None, kind="semantic", payload=None):
    return gate._reject_and_repropose(
        state, idea, state.nodes[0], kind=kind, hint="\nNOVELTY GATE: you duplicated #0.",
        payload=payload if payload is not None else {"similarity": 0.97},
        repropose=repropose, researcher=None, prospective_node_id=None)


def _audit(gate) -> dict:
    assert [name for name, _ in gate.store.appended] == ["novelty_rejected"], gate.store.appended
    return gate.store.appended[0][1]


# ------------------------------------------------------------------ the audited outcomes

def test_a_re_proposal_that_changed_the_idea_is_recorded_as_reproposed(tmp_path):
    """`reproposed` is the only evidence in the log that the paid second call bought anything."""
    gate, state = _Gate(), _state(tmp_path)
    out = _reject(gate, state, _idea(), repropose=lambda: _idea("something genuinely different"))
    assert out.rationale == "something genuinely different"
    assert _audit(gate)["action"] == "reproposed"


def test_a_re_proposal_that_handed_back_the_same_idea_is_recorded_as_kept(tmp_path):
    """The Researcher re-emitting the duplicate is the failure mode the audit exists to expose; it
    must NOT read as a successful re-proposal."""
    gate, state = _Gate(), _state(tmp_path)
    idea = _idea()
    out = _reject(gate, state, idea, repropose=lambda: _idea(idea.rationale))
    assert out.rationale == idea.rationale
    assert _audit(gate)["action"] == "kept"


def test_no_repropose_callable_still_audits_the_rejection_as_kept(tmp_path):
    """A gate with nobody to ask still rejected something — the rejection is what the recall
    diagnostic counts, so it has to be on the log."""
    gate, state = _Gate(), _state(tmp_path)
    idea = _idea()
    assert _reject(gate, state, idea, repropose=None) is idea
    assert _audit(gate)["action"] == "kept"


@pytest.mark.parametrize("digests", [("d0", None), (None, "d1"), (None, None)])
def test_a_digest_that_degraded_to_none_reads_as_kept_not_as_a_change(tmp_path, digests):
    """The comparison is deliberately conservative: `reproposed` requires BOTH digests to exist AND
    differ. `None` means "identity could not be computed" (an oversized or unhashable idea), not
    "different" — and a plain `original != final` would read all three of these as a real change,
    silently inflating the re-proposal success rate exactly when the evidence is missing.

    Both orderings matter: an oversized ORIGINAL and an oversized REPLACEMENT degrade on opposite
    sides of the comparison, and a guard that only checked one side would let the other through.
    """
    import looplab.engine.novelty as novelty_mod

    gate, state = _Gate(), _state(tmp_path)
    answers = list(digests)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(novelty_mod, "idea_proposal_digest", lambda idea: answers.pop(0))
        _reject(gate, state, _idea(), repropose=lambda: _idea("visibly different text"))
    assert not answers, "the helper must digest the original AND the final, exactly once each"
    assert _audit(gate)["action"] == "kept"


def test_a_budget_stop_mid_gate_is_audited_before_the_run_ends(tmp_path):
    """`BudgetExceeded` must end the run — but the rejection already HAPPENED, and a log missing it
    would replay as "this proposal was never gated"."""
    gate, state = _Gate(), _state(tmp_path)

    def _broke():
        raise BudgetExceeded("out of budget")

    with pytest.raises(BudgetExceeded):
        _reject(gate, state, _idea(), repropose=_broke)
    assert _audit(gate)["action"] == "budget_exceeded"


def test_the_budget_stop_audits_exactly_once_and_does_not_also_record_an_outcome(tmp_path):
    """The re-raise is what stops the second append; a helper that swallowed it would write two
    terminal audits for one rejection."""
    gate, state = _Gate(), _state(tmp_path)

    def _broke():
        raise BudgetExceeded("out of budget")

    with pytest.raises(BudgetExceeded):
        _reject(gate, state, _idea(), repropose=_broke)
    assert len(gate.store.appended) == 1


# ------------------------------------------------------------------ the audit's binding

def test_the_audit_binds_the_proposal_slot_the_duplicate_and_the_live_stance(tmp_path):
    gate, state = _Gate(stance="explore"), _state(tmp_path)
    _reject(gate, state, _idea(), repropose=lambda: _idea("changed"))
    audit = _audit(gate)
    assert audit["node_id"] == 1 and audit["generation"] == 0     # the slot this proposal would fill
    assert audit["near_node"] == 0 and audit["near_generation"] == 0
    assert audit["stance"] == "explore"


def test_the_binding_describes_the_ORIGINAL_proposal_not_the_replacement(tmp_path):
    """The rejected thing is what was rejected. Binding the replacement would make the audit claim a
    proposal that was never gated."""
    gate, state = _Gate(), _state(tmp_path)
    seen = []
    gate._proposal_binding = lambda st, idea, nid=None: (
        seen.append(idea.rationale) or {"node_id": 1, "generation": 0})
    _reject(gate, state, _idea("the original"), repropose=lambda: _idea("the replacement"))
    assert seen == ["the original"]


@pytest.mark.parametrize("kind,payload", [("llm", {"reason": "same loss, reworded"}),
                                          ("semantic", {"similarity": 0.9731})])
def test_each_gates_own_payload_key_survives_into_the_audit(tmp_path, kind, payload):
    """`kind` plus its one differing key is the WHOLE of what legitimately varies between the two
    gates; a merge that dropped it would erase why the rejection fired."""
    gate, state = _Gate(), _state(tmp_path)
    _reject(gate, state, _idea(), repropose=lambda: _idea("changed"), kind=kind, payload=payload)
    audit = _audit(gate)
    assert audit["kind"] == kind
    assert all(audit[key] == value for key, value in payload.items())


def test_the_budget_audit_and_the_outcome_audit_carry_the_SAME_binding(tmp_path):
    """Both exits describe one rejection. A copy that rebuilt the payload on the budget path is
    exactly how the two drifted apart before."""
    common, state = {}, _state(tmp_path)
    for repropose in (lambda: _idea("changed"), _raise_budget):
        gate = _Gate()
        try:
            _reject(gate, state, _idea(), repropose=repropose)
        except BudgetExceeded:
            pass
        audit = dict(_audit(gate))
        common[audit.pop("action")] = audit
    assert common["reproposed"] == common["budget_exceeded"]


def _raise_budget():
    raise BudgetExceeded("out of budget")


# ------------------------------------------------------------------ both gates still route here

def test_the_semantic_gate_routes_its_rejection_through_the_shared_protocol(tmp_path):
    gate, state = _Gate(stance="explore"), _state(tmp_path)
    gate._graded_novelty_precheck = lambda *a, **k: None
    gate._semantic_duplicate = lambda st, idea: (st.nodes[0], 0.9731)
    seen = {}
    gate._reject_and_repropose = lambda st, idea, dup, **kw: seen.update(kw, dup=dup.id) or idea

    gate._apply_novelty_gate(state, _idea(), repropose=lambda: _idea("changed"))
    assert seen["kind"] == "semantic" and seen["payload"] == {"similarity": 0.9731}
    assert seen["dup"] == 0
    assert "near-duplicate of experiment #0" in seen["hint"]


def test_the_semantic_gates_hint_still_carries_the_duplicates_failure_reason(tmp_path):
    """"you already tried X, it FAILED because Z" is the entire value of the re-propose; a hint that
    lost the outcome would ask the Researcher to avoid a duplicate it cannot learn from."""
    gate, state = _Gate(stance="explore"), _state(tmp_path, failed=True)
    gate._graded_novelty_precheck = lambda *a, **k: None
    gate._semantic_duplicate = lambda st, idea: (st.nodes[0], 0.98)
    seen = {}
    gate._reject_and_repropose = lambda st, idea, dup, **kw: seen.update(kw) or idea

    gate._apply_novelty_gate(state, _idea(), repropose=lambda: _idea("changed"))
    assert "it FAILED (sandbox_error: boom)" in seen["hint"]


def test_the_llm_gate_routes_its_rejection_through_the_shared_protocol(tmp_path, monkeypatch):
    from looplab.agents import agent as agent_module

    gate, state = _Gate(), _state(tmp_path)
    gate._reflect_client = lambda: object()
    monkeypatch.setattr(agent_module, "agentic_struct", lambda *a, **k: types.SimpleNamespace(
        is_duplicate=True, near_node_id=0, reason="same loss, only reworded"))
    seen = {}
    gate._reject_and_repropose = lambda st, idea, dup, **kw: seen.update(kw, dup=dup.id) or idea

    gate._llm_novelty_gate(state, _idea(), repropose=lambda: _idea("changed"))
    assert seen["kind"] == "llm" and seen["payload"] == {"reason": "same loss, only reworded"}
    assert seen["dup"] == 0
    assert "NOVELTY GATE (LLM)" in seen["hint"] and "it scored 0.42" in seen["hint"]


def test_neither_gate_kept_a_private_copy_of_the_audit_append():
    """A grep-level guard on the collapse itself: `novelty_rejected` is still appended from other
    places (the graded gate, the numeric nudge, the orchestrator), but the two DEDUP gates must go
    through the shared helper — a re-inlined copy is how they drifted the first time."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "looplab" / "engine" / "novelty.py").read_text(
        encoding="utf-8")
    assert "def _reject_and_repropose(" in text, (
        "_reject_and_repropose was renamed; re-point this guard")
    # Only two sites may see the budget stop: the shared helper (audits, then re-raises) and
    # `_repropose_with_feedback` (re-raises past its keep-the-original fallback). A third means a
    # gate is handling it inline again — which is precisely the copy that drifted.
    assert text.count("except BudgetExceeded:") == 2, (
        "a novelty gate is handling BudgetExceeded itself again instead of through the shared "
        "reject/re-propose protocol")
    assert 'kind": "llm"' not in text and 'kind": "semantic"' not in text, (
        "a gate is re-building the audit payload inline instead of passing `kind=`")
