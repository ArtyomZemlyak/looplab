"""Signal-delivery enforcement (§1 of docs/14-agent-framework-mega-review-2026-07-10.md).

Generalizes the hint-registry test to EVERY delivered signal: the engine computes a signal, folds
it, and exactly one site injects it into the consumer's prompt. Each route in
`engine.signal_delivery.SIGNALS` must (a) name an importable+callable injection symbol and (b) have
a probe here that shows the signal's content reaching the rendered output. A signal added to the
registry without a probe FAILS `test_every_route_has_a_probe` — so "the signal silently stopped
being delivered" is a red test, not the next review's finding.
"""
from __future__ import annotations

import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.signal_delivery import SIGNALS, resolve_inject


# --- per-signal probes: build a synthetic input, return (rendered_text, must_contain) ------------

def _probe_trust_flags():
    from looplab.events.digest import trust_reflection
    st = RunState(direction="min", goal="g", trust_gate="gate")
    st.nodes[1] = Node(id=1, operator="draft", idea=Idea(operator="draft", params={}),
                       metric=0.0, status=NodeStatus.evaluated)
    st.reward_hacks = [{"node_id": 1, "signals": [{"signal": "data_leakage:fit_on_test"}]}]
    return trust_reflection(st), "data_leakage:fit_on_test"


def _probe_triage_rationale():
    from looplab.events.digest import _node_line
    n = Node(id=3, operator="debug", idea=Idea(operator="debug", params={}),
             status=NodeStatus.failed, error_reason="crash", error="boom",
             triage_rationale="the approach cannot converge on this data")
    return _node_line(n), "the approach cannot converge"


def _probe_foresight_calibration():
    from looplab.search.foresight import foresight_scoreboard
    st = RunState(direction="min", goal="g")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                       metric=1.0, status=NodeStatus.evaluated)
    st.nodes[1] = Node(id=1, operator="improve", parent_ids=[0],
                       idea=Idea(operator="improve", params={}),
                       metric=0.5, status=NodeStatus.evaluated)   # improved over parent (min)
    st.foresight_selected = [{"node_id": 1, "confidence": 0.9}]
    return foresight_scoreboard(st), "track record"


def _probe_deep_research_memo():
    from looplab.tools.run_tools import RunTools
    st = RunState(direction="min", goal="g")
    st.research = [{"summary": "the leader is overfit", "findings": ["node 4 leaks val stats"],
                    "recommended_directions": ["try CV-gated features"], "at_node": 5}]
    return RunTools()._research_memo(st), "the leader is overfit"


def _probe_operator_yields():
    from looplab.agents.strategist import _fmt_operator_yields
    return _fmt_operator_yields({"improve": {"n": 3, "gain": 0.42}}), "improve"


def _probe_operator_directives():
    from looplab.agents.hints import render_hint_directives
    return render_hint_directives([{"text": "use only sklearn"}]), "use only sklearn"


def _probe_run_states():
    from looplab.serve.llm_context import _attention_states
    st = RunState(direction="min", goal="g", paused=True)
    return _attention_states(st), "PAUSED"


_PROBES = {
    "trust_flags": _probe_trust_flags,
    "triage_rationale": _probe_triage_rationale,
    "foresight_calibration": _probe_foresight_calibration,
    "deep_research_memo": _probe_deep_research_memo,
    "operator_yields": _probe_operator_yields,
    "operator_directives": _probe_operator_directives,
    "run_states": _probe_run_states,
}


def test_every_route_has_a_probe():
    """A new delivered signal MUST come with a delivery probe — the enforcement that stops a signal
    from being registered-but-unverified (the exact §1 failure mode)."""
    registered = {r.name for r in SIGNALS}
    probed = set(_PROBES)
    assert registered == probed, (
        f"signal registry and probes drifted: only-registered={registered - probed}, "
        f"only-probed={probed - registered}")


def test_every_inject_symbol_resolves():
    """Every route's injection site (module:function) must import + be callable — a rename/removal of
    an injection site trips this (like the hint-registry setattr-site scan)."""
    for r in SIGNALS:
        fn = resolve_inject(r)
        assert callable(fn), f"{r.name}: inject {r.inject} is not callable"


@pytest.mark.parametrize("route", SIGNALS, ids=lambda r: r.name)
def test_signal_reaches_rendered_output(route):
    """The signal's content actually appears in the consumer-facing rendering — the L3 injection is
    live, not just declared."""
    text, must_contain = _PROBES[route.name]()
    assert must_contain in text, (
        f"{route.name}: injected via {route.inject} but rendered output did not carry the signal "
        f"(looked for {must_contain!r} in {text!r})")


def test_learning_signals_close_the_loop():
    """A route flagged `closes_loop` must fold an OUTCOME back (L4). Today only foresight_calibration
    claims it; assert its scoreboard reflects the realized hit/miss, not just the prediction."""
    from looplab.search.foresight import foresight_scoreboard
    st = RunState(direction="min", goal="g")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                       metric=1.0, status=NodeStatus.evaluated)
    st.nodes[1] = Node(id=1, operator="improve", parent_ids=[0],
                       idea=Idea(operator="improve", params={}),
                       metric=2.0, status=NodeStatus.evaluated)   # REGRESSED (min): did NOT beat parent
    st.foresight_selected = [{"node_id": 1, "confidence": 0.9}]
    out = foresight_scoreboard(st)
    assert "0 improved" in out, out
    assert any(r.closes_loop for r in SIGNALS)
