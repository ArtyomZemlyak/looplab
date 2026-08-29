"""The Researcher can RECORD a question it is not pursuing — and recording one must never cost it.

Until this field, only deep research and the operator could put a question on the board. The
Researcher could ANSWER a direction (`parent_card_id`) and, since b5302649, READ the board
(`read_questions`); it had no way to ASK. A question noticed mid-proposal and left in `rationale`
prose is read by nothing.

AN OUTPUT FIELD, NOT A TOOL (engine invariant #1: the engine is the sole writer of domain events).
`EV_HYPOTHESIS_ADDED`'s membership in `BACKGROUND_APPENDABLE` does not license a tool-thread append —
that membership exists for the concurrent research task, whose safety argument is "appending FEWER
rows moves no reader's position", not "any thread may append".

TWO DEFECTS IN THIS FIELD'S OWN FIRST DRAFT, both caught by DRIVING it rather than by reading it,
and both pinned below:

  1. `mode="after"` was too late. Checking only the OUTER type in `mode="before"` let pydantic
     validate each element against `list[str]` and RAISE on a flat `["loss/contrastive", ...]` —
     which is 7d406cc2 exactly (the only `list[list[str]]` in a schema, a model returning the
     natural flat shape, nine good fields discarded with it), re-created inside the field added to
     carry that fix.

  2. The first bounding validator DROPPED unusable statements, so `["q1", "", "q3"]` became a
     2-entry list while `question_concepts` still held 3 rows — and `q3` joined to row 1. That is
     c438f1c9's defect one layer up: POSITION IS THE JOIN.

MUTATIONS are named per assertion; each is a real edit to `core/models.py`.
"""
from __future__ import annotations

from looplab.core.models import Idea, IdeaEmission
from looplab.engine.research_cadence import admit_research_beliefs, question_concept_rows

_Q1 = "Does the negative pool width matter?"
_Q3 = "Is 3 epochs enough?"


def test_a_FLAT_question_concepts_does_not_raise():
    """Defect 1. The shape that cost two deep-research passes one layer up."""
    idea = Idea(operator="tune", open_questions=[_Q1],
                question_concepts=["loss/contrastive", "training/negative-mining"])

    assert idea.question_concepts == [[], []], (
        "MUTATION: move the row-shape healing from `_read_question_concept_rows` (before) to an "
        "`after` validator and this RAISES — pydantic checks each element against list[str] first, "
        "and a malformed decoration takes the whole proposal with it")
    assert idea.open_questions == [_Q1], "the question itself survives its concepts being junk"


def test_a_blank_statement_KEEPS_its_slot():
    """Defect 2, stated as the property that makes the positional join safe."""
    idea = Idea(operator="tune", open_questions=[_Q1, "", _Q3],
                question_concepts=[["training/negative-mining"], ["junk"], ["training/schedule"]])

    assert idea.open_questions == [_Q1, "", _Q3], (
        "MUTATION: drop unusable entries instead of blanking them and this goes red — the list "
        "shortens, `question_concepts` does not, and every later question inherits its neighbour's "
        "concepts")
    assert len(idea.question_concepts) == 3


def test_the_join_is_correct_END_TO_END_across_a_blank():
    """The property both defects threaten, driven through the real shared join."""
    idea = Idea(operator="tune", open_questions=[_Q1, "", _Q3],
                question_concepts=[["training/negative-mining"], ["junk"], ["training/schedule"]])
    joined = question_concept_rows(idea.open_questions, idea.question_concepts)

    assert joined == {_Q1: ["training/negative-mining"], _Q3: ["training/schedule"]}, (
        "the blank holds the position in the payload and is skipped by the join AFTER its index is "
        "read — the two halves of the rule, and either one alone is wrong")
    assert admit_research_beliefs([], idea.open_questions) == [_Q1, _Q3], (
        "and the board never sees the blank at all")


def test_an_emission_survives_every_malformed_shape():
    """`IdeaEmission` deliberately adds NO strict twin — a question must not cost the experiment."""
    for bad in (17, "not a list", None, {"a": 1}):
        emitted = IdeaEmission(operator="tune", concept_mode="full", concepts=[],
                               open_questions=bad, question_concepts=bad)
        assert emitted.open_questions == [] and emitted.question_concepts == [], (
            "MUTATION: give IdeaEmission a strict validator for these fields, as it has for the "
            f"concept envelope, and {bad!r} raises — discarding a whole proposal over a field that "
            "only registers a question. The concept envelope is strict because a wrong membership "
            "corrupts the concept graph; this one is not, because the experiment is worth more")


def test_the_emit_schema_carries_both_fields_and_their_contract():
    """The DESCRIPTION is the contract surface — `agent.py` hands this schema to the model."""
    props = IdeaEmission.model_json_schema()["properties"]

    assert "open_questions" in props and "question_concepts" in props
    assert "NOT pursuing" in props["open_questions"]["description"], (
        "MUTATION: drop the description and the model is handed an unexplained list. 7d406cc2 is "
        "the evidence that the description is what a model actually obeys")
    assert "POSITION" in props["question_concepts"]["description"]


def test_the_payload_is_bounded():
    """It rides a DURABLE payload, so one proposal may not write an unbounded list into the log."""
    idea = Idea(operator="tune", open_questions=[f"q{n}" for n in range(200)],
                question_concepts=[["a/b"]] * 200)

    assert len(idea.open_questions) == 8, (
        "MUTATION: remove the slice in `_read_registered_questions` and 200 questions land in "
        "`node_created`. The bound is payload hygiene, deliberately looser than the BOARD cap in "
        "`admit_research_beliefs` — restating that number here is how two caps come to disagree")
    assert len(idea.question_concepts) == 8, "both lists bound alike, or the join shifts"
    assert len(Idea(operator="t", open_questions=["x" * 5_000]).open_questions[0]) == 500


def test_absence_is_silence_and_the_field_round_trips():
    """Additive with reader-side defaults (invariant #5): an old log folds identically."""
    assert Idea(operator="tune").open_questions == []
    assert Idea(operator="tune").question_concepts == []

    idea = Idea(operator="tune", open_questions=[_Q1], question_concepts=[["training/x"]])
    back = Idea(**idea.model_dump())
    assert back.open_questions == [_Q1] and back.question_concepts == [["training/x"]], (
        "it rides durable_idea_payload -> node_created -> Idea(**d['idea']) like parent_card_id")


def test_a_malformed_ELEMENT_costs_the_question_and_never_the_proposal():
    """Defect 1 above was fixed for the ROW and not for what is INSIDE a row.

    `mode="before"` healed `list[list[str]]`'s outer and row shapes, and pydantic then validated
    each row's elements and raised on `[["distill/teacher", 2]]` — a perfectly well-formed row with
    one non-string id — before `_bounded_question_concepts` (mode="after") could coerce anything.
    Same for `open_questions`: `["ok", 3]`, and the very ordinary `[{"question": …, "why": …}]` a
    model returns when asked for research questions. Every one of those raised, so
    `agent.py::_validate_emit` bounced the emit and `_finalize` degraded to
    `Idea(operator, params={}, rationale=…)` — params, card_id, parent_card_id, hypothesis, space
    and footprint all discarded over an advisory decoration. That is 7d406cc2's ALL-OR-NOTHING loss,
    re-created one field over, in the field written to carry its fix.
    """
    idea = Idea(operator="sweep", params={"lr": 0.001}, rationale="r",
                question_concepts=[["distill/teacher", 2]],
                open_questions=["ok", 3, {"question": "q", "why": "w"}])

    assert idea.params == {"lr": 0.001}, "MUTATION: drop the element healing and this is {}"
    assert idea.question_concepts == [["distill/teacher"]], (
        "a non-string ID is DROPPED, not blanked — the ids within a row are an unordered set, so "
        "coercing 2 to '2' would register a concept named '2' on the graph")
    assert idea.open_questions == ["ok", "", ""], (
        "a non-string QUESTION keeps its slot as a blank — position is the join with "
        "question_concepts, and admit_research_beliefs drops the blank from the board")


def test_the_STRICT_producer_schema_heals_these_too():
    """`IdeaEmission` is strict about `card_id`/`concepts`/`footprint` and deliberately inherits
    these two validators unchanged — so the Researcher's own emit path gets the same tolerance.
    Pinned because a strict twin added later would silently restore the all-or-nothing loss."""
    emission = IdeaEmission.model_validate({
        "operator": "sweep", "params": {"lr": 0.001}, "concept_mode": "full", "concepts": [],
        "card_id": "card-3", "parent_card_id": "card-7",
        "open_questions": ["does a stronger teacher help", 3],
        "question_concepts": [["distill/teacher", 2]],
    })
    assert emission.params == {"lr": 0.001}
    assert (emission.card_id, emission.parent_card_id) == ("card-3", "card-7")
    assert emission.question_concepts == [["distill/teacher"]]
    assert emission.open_questions == ["does a stronger teacher help", ""]
