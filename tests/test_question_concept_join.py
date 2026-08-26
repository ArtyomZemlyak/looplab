"""`question_concepts[i]` describes `questions[i]` — and the blank is skipped AFTER the index.

THE DEFECT, driven against the tree at 3612516e. `engine/research_cadence.py` carried a review note
saying, verbatim: *"removing blanks before `enumerate` shifts the positional `question_concepts`
join. For ["", "q2"] with [["c1"], ["c2"]], q2 receives c1. Enumerate the original open_questions,
read the same-index concept row, and only then skip an empty statement."* Three lines below it the
code filtered blanks first and enumerated the SHORTENED list — the exact construction the note
forbids, the second unimplemented review note found this day.

Driven standalone with that code, `["", "does temperature drive the plateau?"]` against
`[["loss/contrastive"], ["training/negative-mining"]]` filed the question under `loss/contrastive`.
A question's concept SET is its position in the question lattice, so a shifted row does not merely
mislabel it — it places it under a different parent.

LATENT, NEVER LIVE, AND THE ZERO IS EVIDENCE RATHER THAN COMFORT: over every event log on the box,
173 memos carry an `open_questions` list, **0** contain a blank entry and **0** carry
`question_concepts` at all. That second zero is the SAME defect one layer up — the field could not
reach the durable row until `_assemble` stopped raising on it (7d406cc2), and
`sanitize_research_memo_payload` defaults the key to `[]`. Repairing the carrier is what makes this
reachable, which is why it is fixed now and not when a lattice parent looks wrong.

Extracted rather than corrected in place: #72 adds a SECOND caller (the Researcher's own registered
questions), and CLAUDE.md §0.8's measured lesson is four implementations of one join with every
drift between the copies.

MUTATION for the ordering assertions: restore
`for index, statement in enumerate([q for q in questions if str(q).strip()])`.
"""
from __future__ import annotations

from looplab.engine.research_cadence import question_concept_rows

_Q = "does temperature drive the plateau?"


def test_a_blank_before_a_question_does_not_shift_its_row():
    """The driven case from the review note, with the real names."""
    joined = question_concept_rows(
        ["", _Q], [["loss/contrastive"], ["training/negative-mining"]])

    assert joined == {_Q: ["training/negative-mining"]}, (
        "MUTATION: filter blanks before `enumerate` and this goes red — the question is filed under "
        "`loss/contrastive`, which belongs to the row above it, and lands under the wrong parent in "
        "the question lattice")


def test_two_blanks_shift_by_two():
    """The error is cumulative, so one blank is not the worst case."""
    joined = question_concept_rows(
        ["", "  ", _Q], [["a/one"], ["b/two"], ["c/three"]])

    assert joined == {_Q: ["c/three"]}, (
        "MUTATION: the pre-filter yields `a/one` here — the shift grows with the number of blanks, "
        "so a memo listing several empty questions misfiles every question after them")


def test_a_blank_carries_no_row_of_its_own():
    """A blank is dropped from the RESULT, not merely from the indexing."""
    joined = question_concept_rows(["", _Q], [["a/one"], ["b/two"]])

    assert list(joined) == [_Q], "an empty statement must never become a key"
    assert "" not in joined


def test_the_ordinary_aligned_case_is_untouched():
    """REGRESSION: with no blanks the join is exactly what it always was."""
    joined = question_concept_rows(
        ["q one", "q two"], [["a/one"], ["b/two"]])

    assert joined == {"q one": ["a/one"], "q two": ["b/two"]}


def test_a_short_missing_or_malformed_row_yields_no_concepts():
    """Checked, not trusted — the question is still registered, just without concepts."""
    assert question_concept_rows(["q one", "q two"], [["a/one"]]) == {"q one": ["a/one"]}
    assert question_concept_rows(["q one"], []) == {}
    assert question_concept_rows(["q one"], [None]) == {}
    assert question_concept_rows(["q one"], ["a/one"]) == {}, (
        "MUTATION: drop the `isinstance(row, list)` test and a STRING row is accepted — each "
        "character would read as a concept id downstream")
    assert question_concept_rows(["q one"], [[]]) == {}, "an empty row is no claim, not a claim of []"


def test_absent_inputs_are_silence():
    """Old logs carry neither key; reader-side defaults, invariant #5."""
    assert question_concept_rows([], []) == {}
    assert question_concept_rows(None, None) == {}
