"""PART V §22 — the Strategist cross-run observation cue (proactive injection into its decision brief).

The Strategist already PULLS cross-run via the tool; this adds a bounded PUSH of returned observations and
mixed-evidence records under `cross_run_advisory`. It explicitly does not claim a CoverageFrame. Off => brief
byte-identical; best-effort.
"""
from __future__ import annotations

import orjson
import pytest

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


def test_strategist_keeps_governance_unavailable_receipt(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps({
        "statement": "hard-neg helps", "outcome": "supported", "evidence": [1],
        "run_id": "r1", "task_id": "t", "direction": "max",
    }) + b"\n")
    (tmp_path / "concept_aliases.jsonl").write_text("future-policy\n", encoding="utf-8")
    host = _Host(tmp_path, on=True)

    assert host._cross_run_note_for_ctx(
        RunState(run_id="current", task_id="t", direction="max")) == ""
    assert host._cross_run_note_receipt["status"] == "unavailable"
    assert host._cross_run_note_receipt["governance"]["ledger"] == "concept_aliases"


def test_engine_populates_note_from_memory(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in [
        {"statement": "hard-neg helps", "outcome": "supported", "evidence": [1], "run_id": "r1",
         "task_id": "t", "direction": "max"},
        {"statement": "mnr helps", "outcome": "supported", "evidence": [2], "run_id": "rA",
         "task_id": "t", "direction": "max"},
        {"statement": "mnr helps", "outcome": "tested", "evidence": [3], "run_id": "rB",
         "task_id": "t", "direction": "max"},
    ]) + b"\n")
    note = _Host(tmp_path, on=True)._cross_run_note_for_ctx(
        RunState(run_id="current", task_id="t", direction="max"))
    assert "returned run(s)" in note and "mixed-evidence" in note


def test_source_completeness_changes_strategist_note_and_semantic_receipt(tmp_path):
    from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule

    complete_dir, partial_dir = tmp_path / "complete", tmp_path / "partial"
    complete_dir.mkdir()
    partial_dir.mkdir()
    capsule = build_concept_capsule(
        run_id="prior", task_id="t", fingerprint=["kind:dataset"], direction="max",
        concepts=["retrieval/rerank"], concept_outcomes={"retrieval/rerank": 0.8},
    )
    legacy = dict(capsule)
    for stem in ("concepts", "concept_outcomes"):
        for suffix in ("total", "omitted", "complete"):
            legacy.pop(f"{stem}_{suffix}")
    ConceptCapsuleStore(complete_dir / "concept_capsules.jsonl").add(capsule)
    ConceptCapsuleStore(partial_dir / "concept_capsules.jsonl").add(legacy)
    state = RunState(run_id="current", task_id="t", direction="max")
    complete_host = _Host(complete_dir, on=True)
    partial_host = _Host(partial_dir, on=True)

    complete_note = complete_host._cross_run_note_for_ctx(state)
    partial_note = partial_host._cross_run_note_for_ctx(state)

    assert "concept source receipt: known=true, complete=true" in complete_note
    assert "concept source receipt: known=true, complete=false" in partial_note
    assert "retained lower bounds only" in partial_note
    assert complete_host._cross_run_note_receipt["concept_source"]["source_complete"] is True
    assert partial_host._cross_run_note_receipt["concept_source"] == {
        "receipt_known": True,
        "source_complete": False,
        "partial_capsules": 1,
        "source_unknown_capsules": 1,
        "source_concepts_omitted": 0,
        "source_outcomes_omitted": 0,
        "source_store_complete": True,
        "source_rows_total": 1,
        "source_rows_quarantined": 0,
        "source_malformed_rows": 0,
        "source_invalid_capsule_rows": 0,
        "source_duplicate_run_rows": 0,
    }
    assert (complete_host._cross_run_note_receipt["corpus_digest"]
            != partial_host._cross_run_note_receipt["corpus_digest"])
    assert (complete_host._cross_run_note_receipt["render_digest"]
            != partial_host._cross_run_note_receipt["render_digest"])


def test_corrupt_claim_store_qualifies_zero_mixed_evidence_in_strategy_note(tmp_path):
    row = {
        "statement": "retained support", "outcome": "supported", "evidence": [1],
        "run_id": "prior", "task_id": "t", "direction": "max",
    }
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(row) + b"\n{broken\n")
    host = _Host(tmp_path, on=True)

    note = host._cross_run_note_for_ctx(
        RunState(run_id="current", task_id="t", direction="max"))

    assert "claim source receipt: known=true, complete=false" in note
    assert "zero mixed-evidence, and absence are lower bounds only" in note
    source = host._cross_run_note_receipt["claim_source"]
    assert source["source_complete"] is False
    assert source["lessons"]["rows_quarantined"] == 1
    assert len(source["snapshot_digest"]) == 64


@pytest.mark.parametrize("persisted_direction", [None, "", "MAX", "sideways", 1])
def test_strategist_note_rejects_missing_or_garbled_persisted_direction(
        tmp_path, persisted_direction):
    row = {
        "statement": "untrusted polarity", "outcome": "supported", "evidence": [1],
        "run_id": "r1", "task_id": "t",
    }
    if persisted_direction is not None:
        row["direction"] = persisted_direction
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(row) + b"\n")
    host = _Host(tmp_path, on=True)

    note = host._cross_run_note_for_ctx(
        RunState(run_id="current", task_id="t", direction="max"))
    if persisted_direction in (None, ""):
        assert note == ""
        assert host._cross_run_note_receipt == {}
    else:
        assert "claim source receipt: known=true, complete=false" in note
        assert host._cross_run_note_receipt["claim_source"]["source_complete"] is False


@pytest.mark.parametrize("current_direction", [None, "", "MAX", "sideways", 1])
def test_strategist_note_rejects_invalid_current_direction(tmp_path, current_direction):
    from types import SimpleNamespace

    row = {
        "statement": "valid persisted evidence", "outcome": "supported", "evidence": [1],
        "run_id": "r1", "task_id": "t", "direction": "max",
    }
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(row) + b"\n")
    host = _Host(tmp_path, on=True)
    state = SimpleNamespace(
        run_id="current", task_id="t", direction=current_direction)

    assert host._cross_run_note_for_ctx(state) == ""
    assert host._cross_run_note_receipt == {}


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
