"""PART IV Phase 2c — replace the foresight world-model's SELF-REPORTED confidence (measured Pearson≈0
with realized outcome, §21.12) with a CALIBRATED §12-verifier score.

Locks in that, with `foresight_verify` on: after the K-idea ranker picks the predicted-best candidate the
§12 verifier scores it and THAT calibrated score becomes the confidence the `foresight_min_confidence`
gate and the telemetry (`confidence_source`) read; with the flag off (or the verifier unavailable) the
self-reported confidence is used unchanged — the byte-identical historical path."""
from __future__ import annotations

from looplab.core.config import Settings
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.foresight import ForesightPanelResearcher
from looplab.trust.verifier import foresight_criteria


class _SeqResearcher:
    """Emits a fixed sequence of ideas (one per base.propose call), like the shipped foresight tests."""
    def __init__(self, ideas, client=None):
        self.ideas = list(ideas)
        self.i = 0
        self.client = client
        self.bounds = None

    def propose(self, state, parent):
        idea = self.ideas[self.i % len(self.ideas)]
        self.i += 1
        return idea


class _VerifyClient:
    """One fake LLM serving BOTH calls from `complete_tool`, dispatched by the requested schema: the
    ranker (order/confidence) and the §12 verifier (verdicts). `verdict` drives the calibrated score
    ("strong_yes"->1.0, "strong_no"->0.0, ""->unparseable so the verifier degrades)."""
    def __init__(self, order, *, self_conf=0.9, verdict="strong_yes"):
        self.order = order
        self.self_conf = self_conf
        self.verdict = verdict
        self.verify_calls = 0
        self.rank_calls = 0

    def complete_tool(self, messages, json_schema):
        if "verdicts" in str(json_schema):                       # the verifier's _Verdicts schema
            self.verify_calls += 1
            return {"verdicts": [self.verdict, self.verdict], "rationales": ["r", "r"]}
        self.rank_calls += 1                                     # the ranker's _Ranking schema
        return {"order": self.order, "confidence": self.self_conf, "reason": "rank"}

    def complete_text(self, messages):
        return "not json"


def _state():
    st = RunState(direction="min", goal="minimize loss")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}),
                       metric=0.5, status=NodeStatus.evaluated, feasible=True)
    return st


def _ideas():
    return [Idea(operator="improve", params={"x": 1.0}, hypothesis="deeper tree overfits"),
            Idea(operator="improve", params={"x": 2.0}, hypothesis="interaction features help")]


# --------------------------------------------------------------------------- #
# The criteria preset
# --------------------------------------------------------------------------- #

def test_foresight_criteria_shape():
    crits = foresight_criteria()
    assert [c.key for c in crits] == ["improves_objective", "sound_and_feasible"]
    assert crits[0].weight == 1.0                                # the primary criterion drives the gate


# --------------------------------------------------------------------------- #
# The confidence replacement
# --------------------------------------------------------------------------- #

def test_verifier_score_replaces_self_reported_confidence():
    client = _VerifyClient([1, 0], self_conf=0.5, verdict="strong_yes")   # verifier -> 1.0, self -> 0.5
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client), k=2, verify_score=True)
    out = panel.propose(_state(), None)
    assert out.hypothesis == "interaction features help"        # still the predicted-best pick
    lf = panel.last_foresight
    assert lf["confidence"] == 1.0 and lf["confidence_source"] == "verifier"
    assert client.verify_calls >= 1                             # the verifier actually ran


def test_flag_off_keeps_self_reported_confidence():
    client = _VerifyClient([1, 0], self_conf=0.5, verdict="strong_yes")
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client), k=2, verify_score=False)
    panel.propose(_state(), None)
    lf = panel.last_foresight
    assert lf["confidence"] == 0.5 and lf["confidence_source"] == "self"
    assert client.verify_calls == 0                            # no extra calls when off


def test_verifier_score_drives_the_abstain_gate():
    # ranker is self-confident (0.9, would pass a 0.6 gate) but the verifier says NO (0.0 < 0.6) -> abstain
    client = _VerifyClient([1, 0], self_conf=0.9, verdict="strong_no")
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client), k=2, verify_score=True,
                                     min_confidence=0.6)
    out = panel.propose(_state(), None)
    assert out.hypothesis == "deeper tree overfits"           # abstained -> the FIRST proposal
    assert "foresight" not in out.rationale
    assert panel.last_foresight is None                        # nothing committed/recorded


def test_verifier_gate_admits_a_high_calibrated_score():
    # symmetric to the above: a strong_yes verdict (1.0) clears the same 0.6 gate the self-conf would too
    client = _VerifyClient([1, 0], self_conf=0.9, verdict="strong_yes")
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client), k=2, verify_score=True,
                                     min_confidence=0.6)
    out = panel.propose(_state(), None)
    assert out.hypothesis == "interaction features help"
    assert panel.last_foresight["confidence"] == 1.0 and panel.last_foresight["confidence_source"] == "verifier"


def test_degrades_to_self_reported_when_verifier_unavailable():
    # a blank verdict is unparseable -> the verifier produces no usable score -> keep the self-reported conf
    client = _VerifyClient([1, 0], self_conf=0.7, verdict="")
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client), k=2, verify_score=True)
    panel.propose(_state(), None)
    lf = panel.last_foresight
    assert lf["confidence"] == 0.7 and lf["confidence_source"] == "self"


def test_verify_samples_are_honored():
    client = _VerifyClient([1, 0], verdict="yes")
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client), k=2, verify_score=True,
                                     verify_samples=2)
    panel.propose(_state(), None)
    assert client.verify_calls == 2                            # exactly `verify_samples` verifier calls


def test_verifier_confidence_returns_none_without_a_client():
    # direct unit of the defensive client-None branch (propose() short-circuits earlier, so it's the one
    # path not reachable end-to-end): _verifier_confidence must degrade to None, not raise.
    panel = ForesightPanelResearcher(_SeqResearcher(_ideas(), client=None), k=2, verify_score=True)
    assert panel.client is None
    assert panel._verifier_confidence(_state(), _ideas()[0], "report") is None


def test_settings_defaults():
    s = Settings()
    assert s.foresight_verify is False and s.foresight_verify_samples == 3
