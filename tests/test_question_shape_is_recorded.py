"""The operator's "the questions are huge" was an impression for four runs. Now it is a number.

WHAT WAS MEASURED, 2026-09-03, on the live board. v12's twelve questions run 195-469 characters,
median 311 — LONGER than the 23 work cards they are supposed to be broader than (median 164) — while
the emit prompt's own example of a good question is 49 characters ("does distilling from a stronger
teacher help here"). They are not mis-typed: the unambiguous experiment-brief shape, a question
carrying the arms of its own sweep, is 1 of 21 across v11+v12. They are OVER-QUALIFIED, each
embedding the evidence that motivated it.

THE CAUSE was where the rule was written. `_MemoOut.open_questions` carried NO field description
while both of its positionally-aligned siblings did, so the shape rule lived only in prose ~100
lines away — and that prose asks for "broad" without ever saying SHORT.

MY FIRST PREDICATE WAS WRONG AND IS RECORDED AS SUCH: "the question names an exact value" flagged 13
of 21, but v11's hits are the champion score cited as EVIDENCE, which is grounding rather than
over-specification. `_SWEEP_ARMS` is the narrowed replacement, and `test_grounding_is_not_a_sweep`
below is that refutation kept executable.
"""
from __future__ import annotations

import re

import pytest

import types as _types

from looplab.agents.deep_research import _MemoOut
from looplab.engine.research_cadence import (
    ResearchCadenceMixin, classify_research_beliefs, question_shape)
from looplab.events.types import EV_BELIEF_ADMISSION

# The prompt's own example of the target shape, and one real v12 question, verbatim.
GOOD = "does distilling from a stronger teacher help here"
# One real v12 question, verbatim off the live board — 378 characters, and the parenthetical at the
# end is the evidence that belongs in `reasoning` rather than in the question's own name.
V12_LONG = (
    "At a fixed effective batch (~16k), does the per-device batch × accumulation split "
    "(512×32 vs 2048×8 vs 4096×4) change the DCL denominator composition — and therefore "
    "recall@100 — given the contrastive denominator is capped at the per-device batch? "
    "(The 0.7934 plateau was reached at BOTH 512×32/3ep (v2#1) and 2048×1/1ep (v4#13), so the "
    "split itself may be a first-order lever.)")


def test_the_target_shape_and_the_shipped_shape_are_far_apart():
    """The two numbers the fix exists to move, side by side."""
    assert question_shape([GOOD])["chars_median"] == 49
    assert question_shape([V12_LONG])["chars_median"] == 378       # 7.7x the target
    assert len(V12_LONG) == 378, "the fixture must stay the real card, not a paraphrase"


def test_the_median_is_a_median_and_not_a_mean():
    # 10, 20, 300: a mean would say 110 and a run of short questions with one essay would read as
    # uniformly bad. The median is what makes the row describe the BOARD rather than its worst row.
    shape = question_shape(["a" * 10, "b" * 20, "c" * 300])
    assert shape["chars_median"] == 20
    assert shape["chars_max"] == 300


def test_an_even_count_still_reports_an_integer():
    # statistics.median of an even list is a float; 311.5 on the wire is noise to a reader that only
    # compares one median against another.
    shape = question_shape(["a" * 10, "b" * 21])
    assert shape["chars_median"] == 16
    assert isinstance(shape["chars_median"], int)


def test_a_memo_with_no_admitted_questions_reports_zeroes_not_an_error():
    """The cap refused 394 of 413 proposals on v12, so an all-refused memo is the COMMON case."""
    assert question_shape([]) == {
        "n": 0, "chars_median": 0, "chars_max": 0, "with_sweep_arms": 0}


def test_blank_and_non_string_entries_are_not_questions():
    # A blank string has length 0 and would drag the median toward zero — the opposite of the
    # signal — and the classifier's `blank` count already owns that fact.
    assert question_shape(["   ", "", None, 7, GOOD])["n"] == 1


def test_a_question_carrying_the_arms_of_its_own_sweep_is_counted():
    assert question_shape([V12_LONG])["with_sweep_arms"] == 1


def test_grounding_is_not_a_sweep():
    """THE REFUTATION, kept executable. v11's questions cite the champion score to say WHY they are
    worth asking; that is grounding, and counting it would have reported 62% where the real figure
    for over-specification is 1 of 21."""
    grounded = ("Does E5-style cross-encoder knowledge distillation lift recall@100 above the "
                "0.79 plateau this family has not passed?")
    assert question_shape([grounded])["with_sweep_arms"] == 0


def test_the_field_that_shapes_the_question_carries_the_rule():
    """The whole fix: the rule must be on the FIELD, which is the only channel in front of the
    model when the emit tool signature is built — not only in prose the schema does not carry."""
    field = _MemoOut.model_fields["open_questions"]
    assert field.description, "open_questions must carry its own shape rule"
    text = field.description.lower()
    assert "short" in text                      # the size, which the prose never said
    assert "one clause" in text
    # It must name where the evidence goes instead, or "do not carry the evidence" is an instruction
    # with nowhere to put the thing it forbids.
    assert "next_experiments" in text or "reasoning" in text


def test_the_rule_reaches_the_emit_schema_the_provider_renders():
    """A description on the model is worth nothing if it is not in the JSON schema sent as the tool
    signature — the same failure mode as the field that was declared in the prompt and missing from
    the schema entirely (`runs/e5small-dr-unified-v5`: open_questions 0, next_experiments 0)."""
    schema = _MemoOut.model_json_schema()
    assert "short" in schema["properties"]["open_questions"]["description"].lower()


def test_every_positionally_aligned_question_field_now_carries_a_description():
    """The three fields are read as one contract by the model. `open_questions` was the only one
    without a description, and it is the one that governs the shape."""
    for name in ("open_questions", "question_parents", "question_concepts"):
        assert _MemoOut.model_fields[name].description, name


# --- the wiring: the shape must ride on the row that already counts the refusals -----------------

def _cadence():
    rows: list[tuple[str, dict]] = []
    return (_types.SimpleNamespace(store=_types.SimpleNamespace(
        append=lambda kind, data: rows.append((kind, data)))), rows)


def test_the_shape_rides_on_the_admission_row():
    """One row per memo already says how many directions survived. It must also say what they ARE,
    or the operator is back to reading the board by eye — which is how this went unnoticed for four
    runs."""
    cadence, rows = _cadence()
    verdict = classify_research_beliefs([], [GOOD, V12_LONG])
    ResearchCadenceMixin._record_belief_admission(cadence, verdict, 2, True)
    kind, data = rows[0]
    assert kind == EV_BELIEF_ADMISSION
    assert data["shape"]["n"] == 2
    assert data["shape"]["chars_max"] == 378
    assert data["shape"]["with_sweep_arms"] == 1
    # The counts it rides beside are untouched — the row gained a key, it did not change meaning.
    assert data["admitted"] == 2 and data["proposed"] == 2


def test_the_shape_describes_the_ADMITTED_questions_not_the_proposed_ones():
    """A question the cap refused never reaches the board, so its size is not a fact about what the
    run is now steering by. v12's cap refused 394 of 413 proposals; counting those would report a
    board that does not exist."""
    cadence, rows = _cadence()
    verdict = classify_research_beliefs([V12_LONG], [V12_LONG, GOOD])  # the long one is a restate
    ResearchCadenceMixin._record_belief_admission(cadence, verdict, 2, True)
    shape = rows[0][1]["shape"]
    assert shape["n"] == 1
    assert shape["chars_max"] == len(GOOD)
