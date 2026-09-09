"""Cross-run lessons can be scoped to the OPERATOR about to fire (doc 52 §4.3).

Retrieval was task-FINGERPRINT (Jaccard >= 0.34, harmonic recall, top 5) plus ROLE, and nothing
about the action: a merge, a repair and an improve on one task were shown the same five rows, while
the IN-RUN context has had parent-plus-sibling scoping since `events/digest.py::lineage_lessons`.

Four properties, and the last two are why the feature is shaped the way it is:

* the store RECORDS the operators of a lesson's own evidence unconditionally — no prompt bytes, no
  provider call — so the question can be asked of a store that was written either way;
* the read is OPT-IN and RANKS rather than filters (the only field ablation, AIRA-dojo, is null);
* OFF is byte-identical, driven through the real `_directed_idea` rather than asserted;
* a scoped render leaves a `prior_injected` receipt naming its operator, which is what makes the
  default decidable later instead of by taste.
"""
from __future__ import annotations

import json

import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.lesson_hygiene import (MAX_LESSON_OPERATORS, lesson_operator_bucket,
                                           lesson_operators, lesson_rank_key)
from looplab.engine.lessons_priors import LESSON_ROLE_DEVELOPER
from tests.factories import make_engine


# ------------------------------------------------------------------ the rule, as a truth table

@pytest.mark.parametrize("row,operator,expected", [
    ({"operators": ["merge"]}, "merge", 0),                    # this operator's own evidence
    ({"operators": ["merge", "improve"]}, "improve", 0),
    ({}, "merge", 1),                                          # legacy row: untagged = universal
    ({"operators": []}, "merge", 1),                           # recorded, but nothing to record
    ({"operators": ["improve"]}, "merge", 2),                  # tagged, and not with this one
    ({"operators": ["merge"]}, "", 1),                         # nothing to scope BY
    ({"operators": ["merge"]}, None, 1),
    ({"operators": "merge"}, "merge", 1),                       # junk shapes read as untagged...
    ({"operators": [None, 3, ""]}, "merge", 1),
    ("not a row", "merge", 1),
])
def test_the_operator_bucket_truth_table(row, operator, expected):
    """Three buckets, and the whole design is which rows land where. A durable shared row is written
    by another run, an older version of this code or a hand edit, so every unreadable shape has to
    land in the NEUTRAL bucket rather than the demoted one."""
    assert lesson_operator_bucket(row, operator) == expected


def test_the_operator_list_is_bounded_and_de_duplicated():
    row = {"operators": ["merge", "merge", "improve"] + [f"op{i}" for i in range(20)]}
    names = lesson_operators(row)
    assert len(names) <= MAX_LESSON_OPERATORS
    assert names[:2] == ("merge", "improve"), "order is the row's own; only duplicates are dropped"
    assert lesson_operators({"operators": ["x" * 65]}) == (), "an over-long name is not a name"


def test_the_rank_key_is_byte_identical_without_an_operator():
    """Every historical caller passes no operator, and this is the assertion that says the flag
    cannot move a run that did not ask for it: same arity, same tuple."""
    row = {"confidence": 0.8, "evidence_count": 2}
    assert lesson_rank_key(0.5, 3, row) == (-0.5, -1.6, -0.5, -3)
    assert lesson_rank_key(0.5, 3, row, operator=None) == (-0.5, -1.6, -0.5, -3)


def test_the_operator_bucket_outranks_similarity_when_asked():
    """AHEAD of similarity, not as a tie-break. A tie-break on a continuous Jaccard is a knob that
    never fires; the bucket is the whole point, and it is confined to rows that already passed the
    similarity gate and the hygiene filters."""
    mine = {"operators": ["merge"], "confidence": 0.5}
    theirs = {"operators": ["improve"], "confidence": 0.5}
    ranked = sorted([(0.9, 1, theirs), (0.4, 2, mine)],
                    key=lambda t: lesson_rank_key(*t, operator="merge"))
    assert ranked[0][2] is mine, "the operator's own lesson must come first when scoping is asked"
    unscoped = sorted([(0.9, 1, theirs), (0.4, 2, mine)], key=lambda t: lesson_rank_key(*t))
    assert unscoped[0][2] is theirs, "unscoped ranking is still similarity-first"


# ------------------------------------------------------------------ the writer

def _state_with_nodes(operators) -> RunState:
    state = RunState(task_id="t", goal="g")
    for nid, operator in enumerate(operators):
        state.nodes[nid] = Node(id=nid, parent=None, idea=Idea(operator=operator),
                                status=NodeStatus.evaluated, metric=1.0, operator=operator)
    return state


def test_the_distiller_stamps_the_operators_of_its_own_evidence(tmp_path):
    """The fact is written unconditionally — a durable property of the row's evidence, costing no
    prompt bytes and no call — which is what lets the scoping be MEASURED on a store recorded before
    anyone decided to read it."""
    engine = make_engine(tmp_path / "run", memory_dir=str(tmp_path / "mem"))
    state = _state_with_nodes(["improve", "merge", "improve", ""])

    assert engine.lessons._evidence_operators(state, [0, 1, 2]) == ["improve", "merge"]
    assert engine.lessons._evidence_operators(state, [3]) == [], "no operator, nothing to record"
    assert engine.lessons._evidence_operators(state, []) == []
    assert engine.lessons._evidence_operators(state, [99]) == [], "an unknown node is not evidence"


# ------------------------------------------------------------------ the read, driven

def _write_lessons(memory_dir, rows) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "lessons.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _lesson(statement, *, operators=None, confidence=0.6, task_id="toy_quadratic"):
    row = {"task_id": task_id, "run_id": "prior", "statement": statement, "outcome": "supported",
           "direction": "min", "confidence": confidence, "role": LESSON_ROLE_DEVELOPER,
           "fingerprint": ["kind:quadratic"]}
    if operators is not None:
        row["operators"] = list(operators)
    return row


def _engine_with_lessons(tmp_path, rows, **overrides):
    memory_dir = tmp_path / "mem"
    _write_lessons(memory_dir, rows)
    engine = make_engine(tmp_path / "run", memory_dir=str(memory_dir),
                         reflection_priors=True, **overrides)
    # Exactly what `orchestrator._reentry_repin` does at run start: ONE scan, both role texts
    # assigned onto the engine. `_directed_idea` reads the Developer half off that attribute.
    engine._prior_note_text, engine._dev_prior_note_text = \
        engine.lessons.load_reflection_priors_both()
    return engine


def test_the_scoped_render_puts_the_firing_operator_s_lesson_first(tmp_path):
    """DRIVEN through the real prior load and the real render, over a real store on disk."""
    engine = _engine_with_lessons(
        tmp_path,
        # The improve row is the CONFIDENT one, so unscoped ranking puts it first on its own
        # merits. Without that asymmetry a passing test would prove only that the tie fell the
        # convenient way.
        [_lesson("an improve-only finding", operators=["improve"], confidence=0.9),
         _lesson("what fixed a failed merge", operators=["merge"], confidence=0.5)],
        lesson_operator_scope=True)

    unscoped = engine.lessons.dev_prior_note_text
    assert unscoped.index("an improve-only finding") < unscoped.index("what fixed a failed merge")

    scoped = engine.lessons.operator_scoped_prior(LESSON_ROLE_DEVELOPER, "merge")
    assert scoped is not None
    assert scoped.index("what fixed a failed merge") < scoped.index("an improve-only finding"), (
        "the merge's own lesson must lead the prior of a build that is about to merge")
    # RANKS, does not filter: the other operator's row is still there. Withholding it would bet a
    # real loss on an effect the field's only ablation says is null.
    assert "an improve-only finding" in scoped


def test_the_flag_off_reproduces_the_developer_prompt_byte_for_byte(tmp_path):
    """The contract every prompt-touching flag in this tree keeps, driven through the site that
    builds the idea handed to the Developer rather than asserted about the renderer."""
    rows = [_lesson("an improve-only finding", operators=["improve"], confidence=0.9),
            _lesson("what fixed a failed merge", operators=["merge"], confidence=0.5)]
    off = _engine_with_lessons(tmp_path / "off", rows)
    on = _engine_with_lessons(tmp_path / "on", rows, lesson_operator_scope=True)

    idea = Idea(operator="merge", rationale="because")
    state = RunState(task_id="toy_quadratic", goal="g")

    assert off.lessons.operator_scoped_prior(LESSON_ROLE_DEVELOPER, "merge") is None, (
        "with the flag off the scoped render must decline rather than return an unscoped text "
        "under a scoped receipt")
    assert off._developer_prior_text(idea) == off.lessons.dev_prior_note_text
    assert off._directed_idea(idea, state).rationale == (
        "because\n" + off.lessons.dev_prior_note_text.strip())
    # ...and with it ON the very same site delivers a different ORDER of the same rows.
    assert on._directed_idea(idea, state).rationale != off._directed_idea(idea, state).rationale


def test_a_scoped_render_leaves_a_receipt_naming_its_operator(tmp_path):
    """The evidence the marker this closes asks for: `events/prior_citations.py` joins these rows to
    what the proposals cited, so whether operator scoping moves anything becomes a measurement."""
    engine = _engine_with_lessons(
        tmp_path, [_lesson("what fixed a failed merge", operators=["merge"])],
        lesson_operator_scope=True)
    before = len(engine.store.read_all())

    engine.lessons.operator_scoped_prior(LESSON_ROLE_DEVELOPER, "merge", at_node=7, phase="build")

    rows = [e for e in engine.store.read_all()[before:] if e.type == "prior_injected"]
    assert len(rows) == 1, [e.type for e in engine.store.read_all()[before:]]
    assert rows[0].data["operator"] == "merge"
    assert rows[0].data["operator_scoped"] == "merge"
    assert rows[0].data["at_node"] == 7 and rows[0].data["role"] == LESSON_ROLE_DEVELOPER
    assert rows[0].data["rows"], "the receipt must name the lesson rows the prompt was built from"

    from looplab.events.types import DIAGNOSTIC_EVENTS
    assert "prior_injected" in DIAGNOSTIC_EVENTS, (
        "a build worker appends this row, so invariant 1 requires it to be fold-ignored AND "
        "excluded from every seq-equality fence")


def test_the_full_load_receipts_are_not_clobbered_by_a_scoped_render(tmp_path):
    """`prior_receipts` belongs to the last full load — the run-start and refresh records are
    written from it — so a per-build render must not overwrite what those will report."""
    engine = _engine_with_lessons(
        tmp_path, [_lesson("what fixed a failed merge", operators=["merge"])],
        lesson_operator_scope=True)
    before = dict(engine.lessons.prior_receipts)

    engine.lessons.operator_scoped_prior(LESSON_ROLE_DEVELOPER, "merge")

    assert engine.lessons.prior_receipts == before
