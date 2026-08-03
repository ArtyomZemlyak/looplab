"""The one task-scope filter across every joined cross-run store (doc 25 EM-08).

`claims_for_memory`, `atlas_for_memory` and `cross_run_retrieve` each open several stores — lessons,
concept capsules, D8 research claims — and join them into a single response. Scoping is per-STORE,
so each had written out the same `str(r.get("task_id") or "") == wanted` filter once per store.

This is an ACCESS BOUNDARY, not a convenience. The comment that used to sit at the atlas site
recorded the failure directly: filtering only the research rows still leaked another task's lessons
and capsules in the same payload. A boundary enforced by three hand-copies is a boundary one of them
can forget, and the forgetting is silent — the response looks complete, just wider than it should be.
"""
from __future__ import annotations

import json

import pytest

from looplab.engine.claims_health import scope_cross_run_sources


def _lesson(task_id, statement="a lesson"):
    return {"task_id": task_id, "run_id": f"run-{task_id}", "statement": statement,
            "claim": statement, "kind": "lesson", "confidence": 0.8}


def _capsule(task_id, concept="loss/contrastive"):
    return {"task_id": task_id, "run_id": f"run-{task_id}", "concept": concept,
            "concept_id": concept, "statement": f"{concept} helped", "n": 3}


def _research(task_id, statement="a research claim"):
    return {"task_id": task_id, "run_id": f"run-{task_id}", "statement": statement,
            "claim": statement, "kind": "research", "confidence": 0.7,
            "urls": ["https://example.invalid/paper"]}


def _task_ids(rows) -> list[str]:
    return [str(row.get("task_id") or "") for row in (rows or [])]


# ------------------------------------------------------------------ the boundary

def test_every_store_is_scoped_not_just_the_research_claims():
    """THE regression. A version that filtered research and forgot the others returned another
    task's lessons and capsules in the same response, and nothing looked wrong."""
    lessons, capsules, research = scope_cross_run_sources(
        task_id="mine",
        lessons=[_lesson("mine"), _lesson("theirs")],
        capsules=[_capsule("mine"), _capsule("theirs")],
        research=[_research("mine"), _research("theirs")])
    assert _task_ids(lessons) == ["mine"]
    assert _task_ids(capsules) == ["mine"]
    assert _task_ids(research) == ["mine"]


@pytest.mark.parametrize("foreign", ["theirs", "MINE", "mine ", " mine", "mine2", "", None])
def test_only_an_EXACT_task_id_is_in_scope(foreign):
    """Case, whitespace and prefix near-misses are all OUT. A `startswith`/casefold filter here would
    hand `mine2`'s lessons to `mine` — a plausible-looking answer about the wrong experiment."""
    lessons, _capsules, _research = scope_cross_run_sources(
        task_id="mine", lessons=[_lesson("mine"), _lesson(foreign)])
    assert _task_ids(lessons) == ["mine"]


def test_a_row_with_no_task_id_at_all_is_out_of_scope():
    """An unattributed row cannot be shown to have belonged to this task, and a scoped read is
    exactly the caller who must not see it."""
    lessons, _capsules, _research = scope_cross_run_sources(
        task_id="mine", lessons=[_lesson("mine"), {"statement": "orphan", "claim": "orphan"}])
    assert _task_ids(lessons) == ["mine"]


def test_a_numeric_task_id_neither_matches_loosely_nor_raises():
    """A hand-edited or foreign JSONL can carry `task_id: 7` or `70`. The boundary has to have an
    answer for junk: compare as text, so `70` is out, and never raise — the row store's own
    validation decides whether a non-string row survives at all, but the scope must not be what
    crashes on it."""
    lessons, _capsules, _research = scope_cross_run_sources(
        task_id="7", lessons=[_lesson(7), _lesson("7"), _lesson(70)])
    assert _task_ids(lessons) == ["7"], "a numeric neighbour must not be scoped in"


# ------------------------------------------------------------------ the None / blank contract

def test_a_store_that_is_not_part_of_the_join_stays_None():
    """`claims_for_memory` reads no capsules. Handing it an empty LIST would invite the caller to
    report "this task has no capsules", which is a different claim from "capsules were not read"."""
    lessons, capsules, research = scope_cross_run_sources(
        task_id="mine", lessons=[_lesson("mine")], research=[_research("mine")])
    assert capsules is None
    assert lessons is not None and research is not None


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_scope_filters_NOTHING_rather_than_everything(blank):
    """The callers guard on `if scope_task` before calling. A helper that silently filtered
    everything away on a blank scope would turn a missing argument into an empty, plausible answer —
    "this task has no cross-run history" — instead of an unscoped read."""
    rows = [_lesson("a"), _lesson("b")]
    lessons, _capsules, _research = scope_cross_run_sources(task_id=blank, lessons=rows)
    assert lessons is rows


def test_an_empty_store_stays_empty_rather_than_becoming_None():
    lessons, _capsules, _research = scope_cross_run_sources(task_id="mine", lessons=[])
    assert lessons is not None and list(lessons) == []


def test_read_health_survives_the_filter():
    """The filtered result is still a `_ClaimSourceRows` carrying its source's read health — a scoped
    read of a PARTIALLY readable store must keep saying so, or the caller reports a confident empty
    answer built on an unreadable file."""
    from looplab.engine.claims_health import _claim_source_rows

    source = _claim_source_rows([_lesson("mine"), _lesson("theirs")], research=False)
    lessons, _capsules, _research = scope_cross_run_sources(task_id="mine", lessons=source)
    assert getattr(lessons, "read_health", None) == source.read_health


# ------------------------------------------------------------------ all three callers use it

def test_no_caller_re_derives_the_scope_predicate():
    """A grep guard on the boundary: the inline `str(r.get("task_id") or "") == wanted` comparison
    must exist in exactly one place. A fourth join that writes its own is the leak coming back."""
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1] / "looplab" / "engine"
    # Scoped to the JOINING readers. Elsewhere in the tree `task_id` is compared inside richer
    # single-store visibility predicates (`cross_run_context`, `lessons`, `proposal_cues`,
    # `cross_run_tools`) that also weigh direction, an excluded run, or a fingerprint fallback —
    # different questions, deliberately not folded in here.
    offenders = [f"{path.name}:{index}"
                 for path in (engine / "claims.py", engine / "claims_retrieval.py")
                 for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
                 if 'r.get("task_id") or ""' in line or 'row.get("task_id") or ""' in line]
    assert offenders == [], f"a private task-scope filter came back: {offenders}"


@pytest.mark.parametrize("module,fn", [
    ("looplab.engine.claims", "claims_for_memory"),
    ("looplab.engine.claims", "atlas_for_memory"),
    ("looplab.engine.claims_retrieval", "cross_run_retrieve"),
])
def test_each_joining_reader_calls_the_shared_scope(module, fn):
    import importlib
    import inspect

    source = inspect.getsource(getattr(importlib.import_module(module), fn))
    assert "scope_cross_run_sources(" in source, f"{module}.{fn} no longer shares the boundary"


def test_the_atlas_reader_scopes_lessons_and_capsules_end_to_end(tmp_path):
    """An integration check, because the unit tests above could all pass while a caller simply never
    called the helper: build a memory dir holding two tasks and assert the foreign one is absent."""
    from looplab.engine.claims import atlas_for_memory

    (tmp_path / "lessons.jsonl").write_text(
        "\n".join(json.dumps(_lesson(task, f"{task} lesson text")) for task in ("mine", "theirs")),
        encoding="utf-8")
    (tmp_path / "concept_capsules.jsonl").write_text(
        "\n".join(json.dumps(_capsule(task, f"axis/{task}")) for task in ("mine", "theirs")),
        encoding="utf-8")

    rendered = json.dumps(atlas_for_memory(tmp_path, scope_task="mine"))
    assert "theirs lesson text" not in rendered
    assert "axis/theirs" not in rendered
