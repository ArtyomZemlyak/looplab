"""PART V §22 — the Strategist cross-run coverage cue (proactive injection into its decision brief).

The Strategist already PULLS cross-run via the tool; this adds a bounded PUSH: a portfolio coverage note
(runs / thin / contested) folded into its brief under `cross_run_advisory`, so the meta-controller grounds
its explore/exploit dial in what the whole portfolio covered. Off => brief byte-identical; best-effort.
"""
from __future__ import annotations

import orjson

from looplab.agents.strategist import StrategyContext, _strategist_brief
from looplab.core.models import RunState
from looplab.engine.strategy import StrategyCadenceMixin


def test_brief_includes_cross_run_note_when_present():
    ctx = StrategyContext(node_count=3, cross_run_note="8 run(s), 5 concept(s), 1 contested")
    brief = _strategist_brief(RunState(), ctx)
    assert "cross-run coverage (portfolio): 8 run(s)" in brief


def test_brief_omits_note_when_empty():
    brief = _strategist_brief(RunState(), StrategyContext(node_count=3))
    assert "cross-run coverage (portfolio)" not in brief


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
    assert "run(s)" in note and "contested" in note


def test_engine_note_empty_when_off(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(
        orjson.dumps({"statement": "x", "outcome": "supported", "evidence": [1], "run_id": "r1"}) + b"\n")
    assert _Host(tmp_path, on=False)._cross_run_note_for_ctx() == ""


def test_engine_note_empty_without_memory_dir():
    assert _Host("", on=True)._cross_run_note_for_ctx() == ""
