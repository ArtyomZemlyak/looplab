"""PART IV cross-run Step 5 advisory (§21.20.5) — fold the context pack into the Researcher prompt.

The gated flip from audit-only (Step 2) to a live prompt cue: `_cross_run_advisory_text` renders the
bounded context pack (claims with support AND counter-evidence + coverage) into the researcher hint,
exactly like the E4 prior note. These tests pin the off-switch (byte-identical prompt), the on-path
(pack rendered from the memory dir), and that it stays advisory (best-effort, never raises).
"""
from __future__ import annotations

import orjson

from looplab.core.config import Settings
from looplab.core.models import RunState
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
from looplab.engine.proposal_cues import ProposalCuesMixin


class _Host(ProposalCuesMixin):
    """Minimal host exposing just what `_cross_run_advisory_text` reads (`self` is the engine)."""
    def __init__(self, memory_dir, *, on=True):
        self._cross_run_advisory = on
        self.memory_dir = str(memory_dir)


def _seed(memory_dir, *, lessons=None, capsules=None):
    if lessons:
        (memory_dir / "lessons.jsonl").write_bytes(
            b"\n".join(orjson.dumps(lesson) for lesson in lessons) + b"\n")
    if capsules:
        store = ConceptCapsuleStore(memory_dir / "concept_capsules.jsonl")
        for c in capsules:
            store.add(c)


def _lesson(statement, outcome, evidence, run_id="r1", direction=""):
    row = {"statement": statement, "outcome": outcome, "evidence": evidence,
           "run_id": run_id, "task_id": "t"}
    if direction:
        row["direction"] = direction
    return row


def test_off_is_empty(tmp_path):
    _seed(tmp_path, lessons=[_lesson("hard-neg helps", "supported", [1])])
    assert _Host(tmp_path, on=False)._cross_run_advisory_text(RunState()) == ""


def test_no_memory_dir_is_empty():
    h = _Host("", on=True)
    h.memory_dir = ""
    assert h._cross_run_advisory_text(RunState()) == ""


def test_empty_store_is_empty(tmp_path):
    assert _Host(tmp_path, on=True)._cross_run_advisory_text(RunState()) == ""


def test_on_renders_pack_with_evidence_and_counter_evidence(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("mnr helps", "supported", [1], run_id="rA"),
        _lesson("mnr helps", "tested", [2], run_id="rB"),        # contested -> mixed
        _lesson("hard-neg helps", "supported", [3]),
    ])
    txt = _Host(tmp_path, on=True)._cross_run_advisory_text(RunState())
    assert txt.startswith("\n")                                  # folds into the hint like the E4 prior
    assert "Cross-run evidence" in txt and "counter-evidence" in txt
    assert "mnr helps" in txt and "⚖" in txt                     # the contested claim is surfaced


def test_on_includes_coverage_line_from_capsules(tmp_path):
    _seed(tmp_path,
          lessons=[_lesson("x helps", "supported", [1])],
          capsules=[build_concept_capsule(run_id="r1", fingerprint=["kind:dataset"], direction="max",
                                          concepts=["hard-neg", "distillation"], concept_outcomes={})])
    txt = _Host(tmp_path, on=True)._cross_run_advisory_text(RunState(direction="max"))
    assert "Bounded live concept observations (not coverage)" in txt and "hard-neg" in txt


def test_d8_only_memory_is_visible_and_exact_task_scoped(tmp_path):
    from looplab.engine.claims import record_research_claims

    record_research_claims(str(tmp_path), run_id="rD8", task_id="task-a", claims=[{
        "statement": "doc2query expands recall", "node_ids": [7],
        "verification": {"verdict": "supported", "method": "llm"},
    }])
    same = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="task-a", direction="max"))
    other = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="task-b", direction="max"))
    assert "doc2query expands recall" in same
    assert "doc2query expands recall" not in other


def test_d8_exact_task_advisory_rejects_opposite_direction(tmp_path):
    from looplab.engine.claims import record_research_claims

    def claim(statement):
        return [{
            "statement": statement, "node_ids": [7],
            "verification": {"verdict": "supported", "method": "llm"},
        }]
    record_research_claims(str(tmp_path), run_id="same", task_id="task-a",
                           direction="max", claims=claim("same direction research"))
    record_research_claims(str(tmp_path), run_id="opposite", task_id="task-a",
                           direction="min", claims=claim("opposite direction research"))
    host = _Host(tmp_path, on=True)
    text = host._cross_run_advisory_text(
        RunState(run_id="current", task_id="task-a", direction="max"))
    assert "same direction research" in text
    assert "opposite direction research" not in text
    assert host._cross_run_advisory_receipt["n_research"] == 1


def test_exact_task_advisory_still_rejects_opposite_direction(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("same objective evidence", "supported", [1], run_id="same", direction="max"),
        _lesson("opposite objective contamination", "supported", [2], run_id="opposite", direction="min"),
    ])
    host = _Host(tmp_path, on=True)
    text = host._cross_run_advisory_text(
        RunState(run_id="current", task_id="t", direction="max"))
    assert "same objective evidence" in text
    assert "opposite objective contamination" not in text
    assert host._cross_run_advisory_receipt["n_lessons"] == 1


def test_malformed_store_never_raises(tmp_path):
    (tmp_path / "lessons.jsonl").write_bytes(b"{not json\n\x00\xff\n")
    # best-effort: a garbled store yields "" (or a lenient-parsed subset), never an exception
    assert isinstance(_Host(tmp_path, on=True)._cross_run_advisory_text(RunState()), str)


def test_settings_flag_defaults_off():
    assert Settings().cross_run_advisory is False
