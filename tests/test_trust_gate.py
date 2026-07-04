"""Trust-gate enforcement in the fold: a `trust_gate_changed` control event engages enforcement,
advisory signals stay advisory, and the Engine rejects an invalid gate value loudly.

Regressions from the deep-review round over the recent feature commits (trust gate). All offline."""
from __future__ import annotations

from looplab.core.models import Event
from looplab.events.replay import fold


def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


def _hacked_run(gate_events):
    return _mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max", "trust_gate": "audit"}),
        ("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.9}),
        ("reward_hack_suspected", {"node_id": 1, "signals": [{"signal": "grader_access"}]}),
        ("node_created", {"node_id": 2, "operator": "draft", "idea": {"operator": "draft"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.5}),
        *gate_events,
    ])


def test_trust_gate_changed_event_engages_enforcement():
    assert fold(_hacked_run([])).best_node_id == 1                     # audit: flag is advisory
    st = fold(_hacked_run([("trust_gate_changed", {"trust_gate": "gate"})]))
    assert st.trust_gate == "gate"
    assert st.best_node_id == 2                                        # flagged node barred from winning
    bad = fold(_hacked_run([("trust_gate_changed", {"trust_gate": "bogus"})]))
    assert bad.trust_gate == "audit"                                   # invalid value ignored


def test_perfect_metric_signal_stays_advisory_under_gate():
    # A legitimately-perfect score (accuracy 1.0 / an achievable 0.0 floor) must still win.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max", "trust_gate": "gate"}),
        ("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}}),
        ("node_evaluated", {"node_id": 1, "metric": 1.0}),
        ("reward_hack_suspected", {"node_id": 1, "signals": [{"signal": "perfect_metric"}]}),
    ]))
    assert st.best_node_id == 1


def test_engine_rejects_invalid_trust_gate(tmp_path):
    import pytest
    from looplab.engine.orchestrator import Engine
    with pytest.raises(ValueError, match="trust_gate"):
        Engine(tmp_path / "run", task=None, researcher=None, developer=None,
               sandbox=None, policy=None, trust_gate="Gate")   # typo must fail loudly, not fail open
