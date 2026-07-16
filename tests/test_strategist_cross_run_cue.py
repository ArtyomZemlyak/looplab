"""PART V §22 — the Strategist cross-run observation cue (proactive injection into its decision brief).

The Strategist already PULLS cross-run via the tool; this adds a bounded PUSH of returned observations and
mixed-evidence records under `cross_run_advisory`. It explicitly does not claim a CoverageFrame. Off => brief
byte-identical; best-effort.
"""
from __future__ import annotations

import orjson

from looplab.agents.strategist import StrategyContext, _strategist_brief
from looplab.core.models import RunState
from looplab.engine.strategy import StrategyCadenceMixin


def test_brief_includes_cross_run_note_when_present():
    ctx = StrategyContext(node_count=3, cross_run_note="8 returned run(s), 5 observed concept(s)")
    brief = _strategist_brief(RunState(), ctx)
    assert "bounded cross-run observations (not coverage): 8 returned run(s)" in brief


def test_brief_omits_note_when_empty():
    brief = _strategist_brief(RunState(), StrategyContext(node_count=3))
    assert "bounded cross-run observations (not coverage)" not in brief


class _Host(StrategyCadenceMixin):
    def __init__(self, memory_dir, *, on):
        self._cross_run_advisory = on
        self.memory_dir = str(memory_dir)


def test_engine_populates_note_from_memory(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in [
        {"statement": "hard-neg helps", "outcome": "supported", "evidence": [1], "run_id": "r1", "task_id": "t"},
        {"statement": "mnr helps", "outcome": "supported", "evidence": [2], "run_id": "rA", "task_id": "t"},
        {"statement": "mnr helps", "outcome": "tested", "evidence": [3], "run_id": "rB", "task_id": "t"},
    ]) + b"\n")
    note = _Host(tmp_path, on=True)._cross_run_note_for_ctx()
    assert "returned run(s)" in note and "mixed-evidence" in note


def test_exact_task_strategist_note_rejects_opposite_direction(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in [
        {"statement": "same", "outcome": "supported", "evidence": [1], "run_id": "r1",
         "task_id": "t", "direction": "max"},
        {"statement": "opposite", "outcome": "supported", "evidence": [2], "run_id": "r2",
         "task_id": "t", "direction": "min"},
    ]) + b"\n")
    host = _Host(tmp_path, on=True)
    note = host._cross_run_note_for_ctx(RunState(run_id="current", task_id="t", direction="max"))
    assert note
    assert host._cross_run_note_receipt["n_lessons"] == 1


def test_d8_exact_task_strategist_note_rejects_opposite_direction(tmp_path):
    from looplab.engine.claims import record_research_claims

    def _claim(statement):
        return [{"statement": statement, "node_ids": [1],
                 "verification": {"verdict": "supported", "method": "llm"}}]

    record_research_claims(tmp_path, run_id="same", task_id="t", direction="max",
                           claims=_claim("same direction research"))
    record_research_claims(tmp_path, run_id="opposite", task_id="t", direction="min",
                           claims=_claim("opposite direction research"))
    host = _Host(tmp_path, on=True)
    note = host._cross_run_note_for_ctx(RunState(run_id="current", task_id="t", direction="max"))
    assert note
    assert host._cross_run_note_receipt["n_research"] == 1


def test_engine_note_empty_when_off(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(
        orjson.dumps({"statement": "x", "outcome": "supported", "evidence": [1], "run_id": "r1"}) + b"\n")
    assert _Host(tmp_path, on=False)._cross_run_note_for_ctx() == ""


def test_engine_note_empty_without_memory_dir():
    assert _Host("", on=True)._cross_run_note_for_ctx() == ""
