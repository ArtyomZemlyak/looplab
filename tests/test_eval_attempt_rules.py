"""The eval attempt loop's decisions as truth tables (review 2026-09-22, ENG2-06).

`engine/eval_attempt_rules.py` holds what `_evaluate`'s phases used to decide inline: may this
attempt buy a repair (`repair_gate`), what the triage verdict does to it (`triage_verdict_outcome`),
and what the scored terminal row says about its own number (`evaluated_terminal`) — beside the
answer ladder (`_classify_repair_answer`, whose truth table lives in
`tests/test_evaluate_named_rules.py`). Each is driven here over its inputs; then each phase is
DRIVEN with the rule swapped for a double
whose answer no real input produces, so what is proven is that the phase acts on the rule's answer
rather than on a second derivation of its own; and last, on real `ast.Call` nodes, that each phase
asks exactly once. `tests/test_repair_loop_golden.py` is the other half: the loop's event log, byte
for byte, over the scenarios the move had to leave unchanged.
"""
from __future__ import annotations

import itertools

import anyio
import pytest

from looplab.engine import eval_attempt_rules as rules
from looplab.engine import evaluate as ev
from looplab.engine.eval_attempt_rules import (EvaluatedTerminal, RepairGate, RepairGateContext,
                                               TriageVerdictOutcome, evaluated_terminal,
                                               repair_gate, triage_verdict_outcome)
from looplab.engine.failure_diagnosis import REASON_SOURCE_ENGINE
from looplab.engine.metric_salvage import SalvagedMetric
from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, TRIAGE_ACTIONS,
                                   UNANSWERABLE_TRIAGE_ACTION, UNREADABLE_TRIAGE_ACTION)
from looplab.events.eventstore import EventStore
from tests._source_scan import called_names
from tests.test_repair_loop_golden import SCENARIOS


# ------------------------------------------------------------------------- may it buy a repair?

def _ctx(**kw) -> RepairGateContext:
    base = dict(inline_repair=True, reason="crash", repair_reasons=("crash", "timeout"),
                floor_stop=None, developer_repairs=True, has_artifact=True)
    base.update(kw)
    return RepairGateContext(**base)


@pytest.mark.parametrize("inline, repairable, floor, developer, artifact",
                         list(itertools.product((True, False), repeat=5)))
def test_the_gate_is_five_conjuncts_and_names_only_a_floor(inline, repairable, floor, developer,
                                                           artifact):
    """Exhaustive over the five inputs: a repair is bought only when every one holds, and the ONE
    refusal that carries a terminal outcome is a floor with inline repair on — the bound an operator
    has to be told about in words. Stated independently of the implementation, so a reordered or
    dropped conjunct is a red cell, not a reworded one."""
    gate = repair_gate(_ctx(inline_repair=inline, reason="crash" if repairable else "oom",
                            floor_stop="hard limit of 2" if floor else None,
                            developer_repairs=developer, has_artifact=artifact))
    assert gate.buys_repair is (inline and repairable and not floor and developer and artifact)
    if floor and inline:
        assert gate.outcome == ("abandon", "hard limit of 2")
    else:
        assert gate.outcome is None


def test_the_floor_is_named_even_when_another_conjunct_also_failed():
    """The order the terminal has always had: a floor that fired is SAID whatever else closed the
    gate — an operator reading "no repair" on a capped node must learn it was the cap."""
    gate = repair_gate(_ctx(reason="oom", developer_repairs=False, has_artifact=False,
                            floor_stop="the engine ceiling of 50"))
    assert gate == RepairGate(False, ("abandon", "the engine ceiling of 50"))


def test_the_reasons_are_the_operators_and_nothing_is_widened():
    assert repair_gate(_ctx(reason="timeout")).buys_repair
    assert not repair_gate(_ctx(reason="timeout", repair_reasons=("crash",))).buys_repair
    assert not repair_gate(_ctx(repair_reasons=())).buys_repair


# ---------------------------------------------------------------- what the verdict does to it

def test_repair_goes_on_and_so_does_a_word_the_table_does_not_know():
    for action in ("repair", "continue", "", None):
        assert triage_verdict_outcome(action, "r", err="e", node_id=3) == TriageVerdictOutcome(False)


def test_abandon_ends_the_attempt_on_the_evals_own_reason_with_the_raw_rationale():
    """The rationale is carried RAW: the terminal redacts and caps it when it writes the row, so a
    copy capped here would be a second, differently-bounded spelling of the same field."""
    long = "x" * 900
    out = triage_verdict_outcome("abandon", long, err="e", node_id=3)
    assert out == TriageVerdictOutcome(True, ("abandon", long))
    raw = {"not": "a string"}
    assert triage_verdict_outcome("abandon", raw, err="e", node_id=3).outcome[1] is raw


def test_reject_idea_is_the_engines_word_and_the_engine_is_credited():
    out = triage_verdict_outcome("reject_idea", "wrong model family", err="e", node_id=3)
    assert out == TriageVerdictOutcome(True, ("reject_idea", "wrong model family"),
                                       reason="idea_rejected", reason_source=REASON_SOURCE_ENGINE)


def test_an_unreachable_judge_is_the_provider_breaker():
    err = "Traceback ...\n" + "y" * 300 + "\nValueError: boom"
    out = triage_verdict_outcome(UNANSWERABLE_TRIAGE_ACTION, "Error code: 402 - out of credits",
                                 err=err, node_id=7)
    assert out.settles and out.reason == "developer_crash"
    assert out.reason_source is None, (
        "the transport failure does not re-credit the classification — the diagnosed source stands")
    assert out.outcome == ("abandon", "the repair-stop judge could not be reached — treating it as "
                                      "a provider failure, not as permission to keep repairing")
    assert out.err.startswith("crash-triage failed: Error code: 402 - out of credits\n[")
    assert out.err.endswith(err[-200:] + "]") and err[:-200] not in out.err
    assert out.pause == ("the crash-triage model could not be reached while deciding whether to "
                         "keep repairing node 7 — Error code: 402 - out of credits")


def test_an_unreadable_answer_is_a_per_node_stop_and_nothing_more():
    out = triage_verdict_outcome(UNREADABLE_TRIAGE_ACTION, "", err="e", node_id=7)
    assert out.settles and out.pause is None and out.reason is None and out.err is None
    assert out.outcome[0] == "abandon" and out.outcome[1].endswith("— no verdict returned")
    capped = triage_verdict_outcome(UNREADABLE_TRIAGE_ACTION, "z" * 1000, err="e", node_id=7)
    assert capped.outcome[1].endswith("— " + "z" * 400)


def test_every_verdict_the_vocabulary_can_hold_has_a_row():
    """Registry-shaped: the table answers every member of `TRIAGE_ACTIONS`, and settles on exactly
    the four that end an attempt — `repair` is the only one that goes on."""
    settling = {a for a in TRIAGE_ACTIONS
                if triage_verdict_outcome(a, "r", err="e", node_id=0).settles}
    assert settling == set(TRIAGE_ACTIONS) - {"repair"}
    assert "repair" in AGENT_TRIAGE_ACTIONS


# ------------------------------------------------------------ what the terminal says of its number

def _salvaged(**kw) -> SalvagedMetric:
    base = dict(metric=0.74, condition="artifact_contract", source="declared_reader",
                reader="stdout_regex", stage="train")
    base.update(kw)
    return SalvagedMetric(**base)


def _terminal(**kw) -> EvaluatedTerminal:
    base = dict(violations=[], metric=0.9, salvaged=None, salvage_cause_repaired=False,
                failure_text="", declaration_repaired=None, metric_salvage="audit", subject=None,
                metric_subject="audit", host_scorer=None, eval_inputs=None, comparability=None,
                applied_params=None)
    base.update(kw)
    return evaluated_terminal(**base)


def test_a_plain_score_carries_its_own_violations_and_no_provenance_key():
    own = [{"name": "latency", "value": 3.0, "max": 2.0, "min": None}]
    out = _terminal(violations=own)
    assert out == EvaluatedTerminal(own, None)
    assert out.violations is not own, "the row gets its own list, never the result's"
    assert _terminal(violations=None).violations == []


def test_a_salvage_is_an_account_and_an_enforcement_row():
    s = _salvaged()
    out = _terminal(salvaged=s, salvage_cause_repaired=1, failure_text="F" * 700,
                    metric_salvage="audit")
    assert out.metric_provenance == {**s.as_event(), "cause_repaired": True,
                                     "salvaged_error": "F" * 600}
    assert out.violations == s.violation_rows("audit")
    assert _terminal(salvaged=s, metric_salvage="select").violations == s.violation_rows("select")


def test_a_corrected_declaration_is_measured_and_a_salvage_outranks_it():
    record = {"salvaged": False, "declaration_repaired": True, "stage": "train"}
    out = _terminal(declaration_repaired=record)
    assert out.metric_provenance is record and out.violations == []
    assert _terminal(declaration_repaired={}).metric_provenance == {}, (
        "an empty record is still a record: the key is written, as it always was")
    both = _terminal(salvaged=_salvaged(), declaration_repaired=record)
    assert both.metric_provenance["salvaged"] is True and "declaration_repaired" not in (
        both.metric_provenance)


def test_the_subject_merges_over_the_account_and_require_mints_the_existing_row():
    unbound = {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    audit = _terminal(subject=unbound, metric_subject="audit")
    assert audit.metric_provenance == unbound and audit.violations == []
    required = _terminal(subject=unbound, metric_subject="require", metric=0.5)
    assert [(v["name"], v["value"], v["salvage"]["condition"]) for v in required.violations] == [
        ("metric_salvaged", 0.5, "metric_subject_unbound")]
    bound = {"subject_bound": True, "subjects": [{"path": "m.bin"}]}
    assert _terminal(subject=bound, metric_subject="require").violations == []
    # …merged OVER the salvage account, keys in the order the row is written
    s = _salvaged()
    merged = _terminal(salvaged=s, subject=unbound, metric_subject="require")
    assert list(merged.metric_provenance) == (list(s.as_event()) + ["cause_repaired",
                                              "salvaged_error"] + list(unbound))
    assert [v["salvage"]["condition"] for v in merged.violations] == [
        "artifact_contract", "metric_subject_unbound"], "own, then salvage, then the require row"


def test_every_later_channel_merges_in_the_rows_order_and_only_when_it_is_a_record():
    out = _terminal(subject={"subject_bound": True}, host_scorer={"program": "score.py"},
                    eval_inputs={"files": []}, comparability={"key": "k"},
                    applied_params={"committed": {}})
    assert list(out.metric_provenance) == ["subject_bound", "host_scorer", "eval_inputs",
                                           "comparability", "applied_params"]
    # a non-dict is not a record, and None writes nothing — never an empty key
    for junk in (None, "x", ["x"], 0):
        assert _terminal(subject=junk, host_scorer=junk, eval_inputs=junk,
                         applied_params=junk).metric_provenance is None
    assert _terminal(comparability={"key": "k"}).metric_provenance == {"comparability": {"key": "k"}}
    assert _terminal(eval_inputs={"files": []}).metric_provenance == {"eval_inputs": {"files": []}}


# --------------------------------------------------------- the phases act on the rules' answers
#
# Each double answers something no real input produces, so a durable row carrying it can only have
# come from the phase acting on the rule's return value — not from a second derivation beside it.

def _events(tmp_path, name):
    return EventStore(tmp_path / name / "events.jsonl").read_all()


def _terminal_row(events):
    return next(e for e in events if e.type in ("node_evaluated", "node_failed"))


def test_decide_repair_acts_on_the_gates_answer(tmp_path, monkeypatch):
    seen = []

    def gate(ctx):
        seen.append(ctx)
        return RepairGate(False, ("abandon", "the double's bound"))

    monkeypatch.setattr(ev, "repair_gate", gate)
    SCENARIOS["repaired_then_scored"](tmp_path / "gate")
    row = _terminal_row(_events(tmp_path, "gate"))
    assert row.type == "node_failed" and row.data["triage_rationale"] == "the double's bound"
    assert seen == [RepairGateContext(inline_repair=True, reason="crash",
                                      repair_reasons=seen[0].repair_reasons, floor_stop=None,
                                      developer_repairs=True, has_artifact=True)]
    assert "crash" in seen[0].repair_reasons


def test_decide_repair_acts_on_the_verdicts_answer(tmp_path, monkeypatch):
    seen = []

    def verdict(action, rationale, *, err, node_id):
        seen.append((action, rationale, node_id))
        return TriageVerdictOutcome(True, ("reject_idea", "the double said so"), reason="setup",
                                    reason_source="declared", err="the double's text",
                                    pause="the double's pause")

    monkeypatch.setattr(ev, "triage_verdict_outcome", verdict)
    SCENARIOS["repaired_then_scored"](tmp_path / "verdict")
    events = _events(tmp_path, "verdict")
    row = _terminal_row(events)
    assert seen == [("repair", "fix the raise", 0)]
    assert (row.data["reason"], row.data["reason_source"], row.data["error"]) == (
        "setup", "declared", "the double's text")
    assert row.data["triage_action"] == "reject_idea"
    assert [e.data["reason"] for e in events if e.type == "pause"] == [
        "auto-paused: the double's pause. Every other node reaches the same endpoint; fix it "
        "(credits, key, base URL, or the endpoint itself) and resume."]


def test_write_terminal_acts_on_the_terminals_answer(tmp_path, monkeypatch):
    seen = []
    real = rules.evaluated_terminal

    def terminal(**kw):
        seen.append(kw)
        real(**kw)
        return EvaluatedTerminal([{"name": "the_double", "value": 1}], {"the_double": True})

    monkeypatch.setattr(ev, "evaluated_terminal", terminal)
    SCENARIOS["full_provenance"](tmp_path / "terminal")
    row = _terminal_row(_events(tmp_path, "terminal"))
    assert row.data["violations"] == [{"name": "the_double", "value": 1}]
    assert row.data["metric_provenance"] == {"the_double": True}
    (kw,) = seen
    assert kw["subject"] == {"subject_bound": True, "subjects": [{"path": "out/model.bin"}]}
    assert kw["host_scorer"]["program"] == "score.py" and kw["metric_subject"] == "audit"


def test_each_phase_asks_its_rule_exactly_once():
    """On real `ast.Call` nodes (`called_names`): a comment naming a rule is not a call, and a
    second call would be a second place the decision is made."""
    decide = called_names(ev.EvaluateMixin._eval_decide_repair)
    assert decide.count("repair_gate") == 1 and decide.count("RepairGateContext") == 1
    assert decide.count("triage_verdict_outcome") == 1
    assert called_names(ev.EvaluateMixin._eval_write_terminal).count("evaluated_terminal") == 1
    assert called_names(ev.EvaluateMixin._eval_apply_repair).count("_classify_repair_answer") == 1
    # …and none of the rules it replaced is re-derived beside it: the moved vocabulary is not
    # spelled in the phases any more (NEGATIVE pins stay substrings on purpose — the text is what
    # must not come back).
    import inspect
    decide_src = inspect.getsource(ev.EvaluateMixin._eval_decide_repair)
    for gone in ("UNANSWERABLE_TRIAGE_ACTION", "UNREADABLE_TRIAGE_ACTION", '"idea_rejected"'):
        assert gone not in decide_src, gone
    write_src = inspect.getsource(ev.EvaluateMixin._eval_write_terminal)
    for gone in ("unbound_subject_violation_rows(", "violation_rows(", "as_event()"):
        assert gone not in write_src, gone
