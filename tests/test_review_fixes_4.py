"""Regression tests for the deep-review round over the recent feature commits (hypotheses ledger,
memory/lessons, eventstore cache, trust gate, assistant/llm recovery). All offline."""
from __future__ import annotations

import json

from looplab.eventstore import EventStore
from looplab.llm import _apply_native_tool_calls, _extract_native_tool_calls
from looplab.models import Event, hypothesis_id
from looplab.replay import fold
from looplab.sandbox import _json_line_extras


def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


# ── hypotheses ledger ────────────────────────────────────────────────────────────────────────────

def test_malformed_hypothesis_added_does_not_brick_fold():
    # A scripted API client can append any JSON via the control endpoint; one malformed entry
    # must not make every later fold of the run raise.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "x helps", "at_node": "soon"}),      # non-numeric at_node
        ("hypothesis_added", {"statement": "y helps", "id": 5}),                # non-string id
        ("hypothesis_added", {"statement": "z helps", "source": {"who": "me"}}),  # non-string source
    ]))
    stmts = {h.statement for h in st.hypotheses.values()}
    assert {"x helps", "y helps", "z helps"} <= stmts


def test_failed_evidence_hypothesis_returns_to_open_not_testing():
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "bigger net helps"}}),
        ("node_failed", {"node_id": 1, "reason": "crash"}),
        ("run_finished", {"reason": "done"}),
    ]))
    h = st.hypotheses[hypothesis_id("bigger net helps")]
    assert h.status == "open"          # not "testing": nothing is running in a finished run


def test_re_adding_abandoned_hypothesis_reopens_it():
    hid = hypothesis_id("try polars")
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "try polars", "id": hid}),
        ("hypothesis_updated", {"id": hid, "status": "abandoned"}),
        ("hypothesis_added", {"statement": "try polars", "id": hid}),   # mis-click undo: re-add
    ]))
    assert st.hypotheses[hid].status == "open"
    # and an explicit non-abandoned status update also clears the override
    st2 = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "try polars", "id": hid}),
        ("hypothesis_updated", {"id": hid, "status": "abandoned"}),
        ("hypothesis_updated", {"id": hid, "status": "open"}),
    ]))
    assert st2.hypotheses[hid].status == "open"


# ── trust gate ───────────────────────────────────────────────────────────────────────────────────

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
    from looplab.orchestrator import Engine
    with pytest.raises(ValueError, match="trust_gate"):
        Engine(tmp_path / "run", task=None, researcher=None, developer=None,
               sandbox=None, policy=None, trust_gate="Gate")   # typo must fail loudly, not fail open


# ── eventstore incremental cache ────────────────────────────────────────────────────────────────

def test_read_all_tolerates_invalid_event_record(tmp_path):
    p = tmp_path / "events.jsonl"
    es = EventStore(p)
    es.append("run_started", {"run_id": "r"})
    es.append("hint", {"text": "x"})
    with open(p, "a", encoding="utf-8") as f:                      # valid JSON dict, invalid Event
        f.write(json.dumps({"seq": 99, "type": "x", "data": [1, 2, 3]}) + "\n")
    first = [e.seq for e in es.read_all()]
    second = [e.seq for e in es.read_all()]
    third = [e.seq for e in es.read_all()]
    assert first == second == third                                # no duplicated prefix growth
    assert len(first) == 2


# ── sandbox extras ───────────────────────────────────────────────────────────────────────────────

def test_json_line_extras_rejects_nan_and_inf():
    out = '{"metric": 0.5, "loss": NaN, "lr": Infinity, "recall": 0.7}'
    extras = _json_line_extras(out)
    assert extras == {"recall": 0.7}


# ── llm native tool-call recovery ───────────────────────────────────────────────────────────────

def test_native_recovery_ignores_markup_quoted_in_code_blocks():
    quoted = ('Here is how the DSML template looks:\n```\n<tool_calls><invoke name="run_command">'
              '<parameter name="command">rm -rf /</parameter></invoke></tool_calls>\n```\nSafe.')
    calls, clean = _extract_native_tool_calls(quoted)
    assert calls is None and clean == quoted                       # quoted example: untouched
    msg = {"role": "assistant", "content": quoted}
    assert _apply_native_tool_calls(msg).get("tool_calls") is None
    assert msg["content"] == quoted                                # reply not truncated

    leaked = ('<tool_calls><invoke name="read_file"><parameter name="path">/tmp/x</parameter>'
              '</invoke></tool_calls>')
    calls2, clean2 = _extract_native_tool_calls(leaked)            # a REAL leak still recovers
    assert calls2 and calls2[0]["function"]["name"] == "read_file"
    assert clean2 == ""
