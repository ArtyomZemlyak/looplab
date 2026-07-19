"""PART IV cross-run Step 5 advisory (§21.20.5) — fold the context pack into the Researcher prompt.

The gated flip from audit-only (Step 2) to a live prompt cue: `_cross_run_advisory_text` renders the
bounded context pack (claims with support AND counter-evidence + coverage) into the researcher hint,
exactly like the E4 prior note. These tests pin the off-switch (byte-identical prompt), the on-path
(pack rendered from the memory dir), and that it stays advisory (best-effort, never raises).
"""
from __future__ import annotations

import orjson
import pytest

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


def _lesson(statement, outcome, evidence, run_id="r1", direction="max"):
    row = {"statement": statement, "outcome": outcome, "evidence": evidence,
           "run_id": run_id, "task_id": "t"}
    if direction is not None:
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
    txt = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="t", direction="max"))
    assert txt.startswith("\n")                                  # folds into the hint like the E4 prior
    assert "Cross-run evidence" in txt and "counter-evidence" in txt
    assert "mnr helps" in txt and "⚖" in txt                     # the contested claim is surfaced


def test_on_includes_coverage_line_from_capsules(tmp_path):
    _seed(tmp_path,
          lessons=[_lesson("x helps", "supported", [1])],
          capsules=[build_concept_capsule(run_id="r1", fingerprint=["kind:dataset"], direction="max",
                                          concepts=["hard-neg", "distillation"], concept_outcomes={},
                                          task_id="t")])
    txt = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="t", direction="max"))
    assert "Bounded live concept observations (not coverage)" in txt and "hard-neg" in txt


def test_related_advisory_rejects_capsule_with_unknown_fingerprint_projection(tmp_path):
    class _RelatedHost(_Host):
        def _task_fingerprint(self, state, best=None):
            return ["kind:dataset", "retrieval", "russian"]

    capsule = build_concept_capsule(
        run_id="prior", task_id="foreign", fingerprint=["kind:dataset", "retrieval", "russian"],
        direction="max", concepts=["data/hard-neg"], concept_outcomes={},
    )
    for suffix in ("total", "omitted", "complete"):
        capsule.pop(f"fingerprint_{suffix}")
    _seed(tmp_path, capsules=[capsule])

    host = _RelatedHost(tmp_path, on=True)
    text = host._cross_run_advisory_text(
        RunState(run_id="current", task_id="current", direction="max"))

    assert "capsule applicability scope is PARTIAL" in text
    assert "absence is not proof" in text
    assert "hard-neg" not in text
    assert host._cross_run_advisory_receipt["v"] == 2
    assert host._cross_run_advisory_receipt["concept_scope"] == {
        "scope_complete": False,
        "scope_unknown_capsules": 1,
        "scope_fingerprint_unknown_capsules": 1,
        "scope_fingerprint_items_omitted": 0,
        "scope_direction_unknown_capsules": 0,
    }


def test_rank_tendency_marks_persisted_concept_names_as_untrusted(tmp_path):
    capsules = []
    for run_id in ("r1", "r2"):
        capsules.append(build_concept_capsule(
            run_id=run_id, fingerprint=["kind:dataset"], direction="max",
            concepts=["loss/ignore-system", "loss/mid", "loss/weak"],
            concept_outcomes={
                "loss/ignore-system": 0.9, "loss/mid": 0.5, "loss/weak": 0.1,
            }, task_id="t"))
    _seed(tmp_path, capsules=capsules)

    txt = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="t", direction="max"))

    tendency = next(line for line in txt.splitlines() if "rank tendency" in line)
    assert "RANK BETTER UNTRUSTED_MEMORY=" in tendency
    assert "RANK WORSE UNTRUSTED_MEMORY=" in tendency


def test_live_rank_tendency_uses_full_scoped_rows_before_overview_cap(tmp_path):
    popular = [f"popular/c{index:03d}" for index in range(512)]
    capsules = []
    for group in range(2):
        concepts = popular[group * 256:(group + 1) * 256]
        for repeat in range(3):
            capsules.append(build_concept_capsule(
                run_id=f"popular-{group}-{repeat}", task_id="t",
                fingerprint=["kind:dataset"], direction="max",
                concepts=concepts, concept_outcomes={}))
    for repeat in range(2):
        capsules.append(build_concept_capsule(
            run_id=f"tendency-{repeat}", task_id="t",
            fingerprint=["kind:dataset"], direction="max",
            concepts=["zz/target", "zz/baseline"],
            concept_outcomes={"zz/target": 0.9, "zz/baseline": 0.1}))
    _seed(tmp_path, capsules=capsules)

    text = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="t", direction="max"))

    assert "RANK BETTER UNTRUSTED_MEMORY=" in text and "zz/target (n=2)" in text
    assert "RANK WORSE UNTRUSTED_MEMORY=" in text and "zz/baseline (n=2)" in text


def test_advisory_rejects_valid_direction_without_scope_identity(tmp_path):
    _seed(tmp_path, lessons=[_lesson("portfolio evidence", "supported", [1])])
    host = _Host(tmp_path, on=True)

    assert host._cross_run_advisory_text(
        RunState(run_id="current", direction="max")) == ""
    assert host._cross_run_advisory_receipt == {}


def test_d8_only_memory_is_visible_and_exact_task_scoped(tmp_path):
    from looplab.engine.claims import record_research_claims

    record_research_claims(str(tmp_path), run_id="rD8", task_id="task-a", direction="max", claims=[{
        "statement": "doc2query expands recall", "node_ids": [7],
        "verification": {"verdict": "supported", "method": "llm"},
    }])
    same = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="task-a", direction="max"))
    other = _Host(tmp_path, on=True)._cross_run_advisory_text(
        RunState(run_id="current", task_id="task-b", direction="max"))
    assert "doc2query expands recall" in same
    assert "doc2query expands recall" not in other


@pytest.mark.parametrize("persisted_direction", [None, "", "MAX", "sideways", 1])
def test_exact_task_advisory_rejects_missing_or_garbled_lesson_direction(
        tmp_path, persisted_direction):
    _seed(tmp_path, lessons=[
        _lesson("untrusted polarity", "supported", [1], direction=persisted_direction),
    ])

    host = _Host(tmp_path, on=True)
    text = host._cross_run_advisory_text(
        RunState(run_id="current", task_id="t", direction="max"))

    assert text == ""
    assert host._cross_run_advisory_receipt == {}


def test_exact_task_advisory_rejects_research_without_direction(tmp_path):
    from looplab.engine.claims import record_research_claims

    record_research_claims(str(tmp_path), run_id="legacy", task_id="task-a", claims=[{
        "statement": "legacy research without polarity", "node_ids": [7],
        "verification": {"verdict": "supported", "method": "llm"},
    }])

    host = _Host(tmp_path, on=True)
    text = host._cross_run_advisory_text(
        RunState(run_id="current", task_id="task-a", direction="max"))

    assert text == ""
    assert host._cross_run_advisory_receipt == {}


@pytest.mark.parametrize("current_direction", [None, "", "MAX", "sideways", 1])
def test_advisory_rejects_invalid_current_direction(tmp_path, current_direction):
    from types import SimpleNamespace

    _seed(tmp_path, lessons=[_lesson("valid persisted evidence", "supported", [1])])
    state = SimpleNamespace(
        run_id="current", task_id="t", direction=current_direction)
    host = _Host(tmp_path, on=True)

    assert host._cross_run_advisory_text(state) == ""
    assert host._cross_run_advisory_receipt == {}


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


def test_settings_flag_defaults_on():
    # Part IV/V ships ON by default (advisory prompt cue; opt out per-run).
    assert Settings().cross_run_advisory is True


# --------------------------------------------------------------------------- lean cross-run pointer
class _PointerHost(ProposalCuesMixin):
    """Host for the lean `_cross_run_pointer_text` — reads read-tools flag, memory_dir, advisory flag."""
    def __init__(self, memory_dir="/mem", *, read_tools=False, advisory=False):
        self._cross_run_read_tools = read_tools
        self._cross_run_advisory = advisory
        self.memory_dir = str(memory_dir)


def test_pointer_present_when_read_tools_on_regardless_of_advisory():
    # The pointer NAMES the pull-tools; it fires whenever the tools are wired + a memory_dir exists.
    # Advisory only (mentions the tools, never constrains selection).
    txt = _PointerHost(read_tools=True, advisory=False)._cross_run_pointer_text()
    assert "cross_run_prior_attempts" in txt and "cross_run_atlas" in txt
    assert "advisory only" in txt.lower()


def test_pointer_fires_alongside_advisory_pack():
    # The pushed advisory pack injects prior-run CONTENT but never names the cross_run_* pull-tools, so
    # the two are orthogonal. In the product default (advisory ON) the pointer MUST still fire — else the
    # tools go permanently unnamed and the model never drills into them on demand.
    assert _PointerHost(read_tools=True, advisory=True)._cross_run_pointer_text() != ""
    assert (_PointerHost(read_tools=True, advisory=True)._cross_run_pointer_text()
            == _PointerHost(read_tools=True, advisory=False)._cross_run_pointer_text())


def test_pointer_empty_without_read_tools_or_memory_dir():
    assert _PointerHost(read_tools=False)._cross_run_pointer_text() == ""       # tools not wired
    assert _PointerHost(memory_dir="", read_tools=True)._cross_run_pointer_text() == ""  # nothing to query


def test_pointer_is_static_and_does_no_store_io(tmp_path):
    # Per-node hot path: the pointer must be a constant string (no lessons/capsules read), so a garbled
    # or absent store cannot slow or break proposing. Same text regardless of memory_dir contents.
    a = _PointerHost(memory_dir=tmp_path, read_tools=True)._cross_run_pointer_text()
    (tmp_path / "lessons.jsonl").write_bytes(b"{not json\n\x00\n")
    b = _PointerHost(memory_dir=tmp_path, read_tools=True)._cross_run_pointer_text()
    assert a == b and a.startswith("\nCross-run memory")
