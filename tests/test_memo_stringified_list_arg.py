"""A list the model serialised as a JSON STRING is decoded, and nothing else is.

THE DEFECT, measured live on `runs/e5small-dr-unified-v7` (the run launched on `e7c9abba`, the
first with deep research working again). Its first memo came back rich — 10 findings, 11 claims,
64 sources — and the console said:

    deep research: emitted memo kept, 1 field(s) refused for shape: open_questions

So the model DID answer the question half of the prompt and the engine dropped it. The emit call's
own arguments survive in `spans.jsonl` — on a `generation` span's `tool_calls[].arguments`, since
the emit is not traced as a `tool` span — and the shape is:

    "open_questions": "[\"Does training the e5-small backbone past the 1-3 applied epochs
                        (toward the documented 15-60) actually lift recall@100 ...\", ...]"

a `str` holding a JSON array of strings, where the schema declares `list[str]`. Note this REFUTES
the hypothesis the task was opened on — that a model asked for two parallel arrays returns one
array of objects. It returned the right structure with an extra layer of quoting.

WHY THIS ONE IS HEALED AND THE OBJECT FORM IS NOT. Decoding is not a judgement: the value either
parses as a list or it does not. Reading `[{"question": ...}]` would need someone to decide which
key is the question, and that guess is what admits two spellings of one field into a durable row.
The recovery therefore fails closed at three points — must be a `str`, must open with `[`, must
decode to a `list` — and healing runs BEFORE validation, so the row only ever holds `list[...]`.

Every assertion below has an input that makes it FAIL; each mutation is named in its message.
"""
from __future__ import annotations

from typing import get_origin

import pytest
from pydantic import ValidationError

from looplab.agents.deep_research import _ClaimOut, _MemoOut, _decoded_json_list


# Verbatim from the v7 emit args, truncated at the sentence — the shape is the point, not the prose.
_V7_OPEN_QUESTIONS = (
    '["Does training the e5-small backbone past the 1-3 applied epochs (toward the documented '
    '15-60) actually lift recall@100, or is the 0.7934 plateau a loss/data ceiling?", '
    '"How much of the plateau is set by negative quality/quantity?"]'
)


@pytest.mark.parametrize("value,expected", [
    ('["a", "b"]', ["a", "b"]),          # the measured v7 shape
    ("[]", []),                          # an empty array is a list and stays one
    ('[{"statement": "c"}]', [{"statement": "c"}]),
    ("not json at all", "not json at all"),
    ("[1, 2", "[1, 2"),                  # opens like an array and does not parse
    ('{"a": 1}', {"a": 1} and '{"a": 1}'),   # decodes to a DICT -> unchanged
    ('"a bare string"', '"a bare string"'),  # decodes to a STR -> unchanged
    ("3", "3"),                          # decodes to an INT -> unchanged
    ("null", "null"),                    # decodes to None -> unchanged
    (["already a list"], ["already a list"]),
    (None, None),
    (7, 7),
])
def test_the_decoder_returns_its_input_unless_the_decode_yields_a_list(value, expected):
    """The truth table of the rule, stated where it can be falsified.

    THIS TEST EXISTS BECAUSE A MUTATION SURVIVED. The first cut of the healer guarded on
    `text.startswith("[")` before decoding, which made the `isinstance(decoded, list)` clause
    unreachable-as-false — valid JSON opening with `[` is always an array — so replacing that whole
    clause with a bare `return decoded` passed all ten tests. The model-level assertions could not
    see it: pydantic refuses a dict for `list[str]` anyway, so the field was refused either way and
    the guard's own rule was never what held the line.

    MUTATION: `return decoded` -> the four non-list decodes below fail.
    """
    assert _decoded_json_list(value) == expected


def test_the_v7_shape_is_decoded_into_the_questions_it_holds():
    """MUTATION: drop `_StringifiedListTolerant` from `_MemoOut`'s bases -> ValidationError."""
    out = _MemoOut.model_validate({"summary": "s", "open_questions": _V7_OPEN_QUESTIONS})

    assert len(out.open_questions) == 2, "the two questions v7 emitted must both survive"
    assert out.open_questions[0].startswith("Does training the e5-small backbone")
    assert "0.7934 plateau" in out.open_questions[0]
    assert out.open_questions[1].startswith("How much of the plateau")


def test_the_sibling_fields_of_that_memo_are_untouched():
    """The healer must not disturb a field that arrived correctly.

    MUTATION: make `_decoded_json_list` return `[value]` for any str -> `findings` becomes
    `['f1', 'f2']` nested wrongly / `summary` is not a list field and must never be considered.
    """
    out = _MemoOut.model_validate({
        "summary": "[not a list, just prose that opens with a bracket]",
        "findings": ["f1", "f2"],
        "open_questions": _V7_OPEN_QUESTIONS,
    })

    assert out.findings == ["f1", "f2"]
    assert out.summary == "[not a list, just prose that opens with a bracket]", (
        "summary is a str field; the healer must not decode it even though it opens with '['")


@pytest.mark.parametrize("refused", [
    "not json at all",
    '{"a": 1}',          # decodes, but to a dict
    '"a bare string"',   # decodes, but to a str
    "[1, 2",             # opens with '[' and does not parse
])
def test_what_is_not_a_json_list_is_still_refused(refused):
    """The healer must not become a blanket "try to make it fit".

    MUTATION: return `[value]` on decode failure, or accept a non-list decode -> these all pass
    validation and junk reaches the durable row.
    """
    with pytest.raises(ValidationError):
        _MemoOut.model_validate({"open_questions": refused})


def test_an_element_that_looks_like_json_is_never_decoded():
    """A question whose text reads like an array is a question.

    MUTATION: apply `_decoded_json_list` element-wise -> `[[1, 2]]`, which then fails `list[str]`.
    """
    out = _MemoOut.model_validate({"open_questions": ["[1, 2]"]})

    assert out.open_questions == ["[1, 2]"]


def test_every_list_field_of_the_emit_schema_is_covered():
    """DERIVED from `model_fields`, so a list field added later inherits the tolerance.

    MUTATION: apply the healer to `open_questions` alone -> every other field here raises.
    """
    stringified = {
        "findings": '["f1", "f2"]',
        "recommended_directions": '["d1"]',
        "open_questions": '["q1"]',
        "next_experiments": '["e1"]',
        "question_concepts": '[["loss/contrastive", "training/negative-mining"]]',
        "claims": '[{"statement": "c1", "node_ids": [3], "urls": []}]',
    }
    list_fields = {name for name, field in _MemoOut.model_fields.items()
                   if get_origin(field.annotation) is list}

    assert list_fields, "guard is vacuous if the schema has no list fields"
    assert list_fields == set(stringified), (
        f"a list field moved in _MemoOut; cover it here: {list_fields ^ set(stringified)}")

    for name, raw in stringified.items():
        out = _MemoOut.model_validate({name: raw})
        assert isinstance(getattr(out, name), list) and getattr(out, name), (
            f"{name} was not decoded from its JSON-string form")

    assert _MemoOut.model_validate(
        {"question_concepts": stringified["question_concepts"]}
    ).question_concepts == [["loss/contrastive", "training/negative-mining"]]
    assert _MemoOut.model_validate({"claims": stringified["claims"]}).claims[0].node_ids == [3]


def test_the_nested_claim_schema_is_tolerant_too():
    """`claims` is `list[_ClaimOut]`, so its own lists meet the same hazard one level down.

    MUTATION: leave `_ClaimOut` on a bare `BaseModel` -> the stringified `node_ids` raises.
    """
    out = _MemoOut.model_validate(
        {"claims": [{"statement": "c", "node_ids": "[1, 2]", "urls": '["http://x"]'}]})

    assert out.claims[0].node_ids == [1, 2]
    assert out.claims[0].urls == ["http://x"]
    assert _ClaimOut.model_validate({"node_ids": "[7]"}).node_ids == [7]


def test_the_finalizer_keeps_the_questions_instead_of_refusing_them():
    """End to end through the rung that logged the refusal — the property the run actually needs.

    MUTATION: revert the healer -> `_finalize` drops `open_questions` and keeps the rest, which is
    exactly the v7 console line this change was opened on.
    """
    from looplab.agents.deep_research import DeepResearcher
    from looplab.core.models import ResearchMemo

    finalizer = DeepResearcher.__new__(DeepResearcher)
    memo = finalizer._finalize(
        {"summary": "the run is fresh", "findings": ["f1"], "open_questions": _V7_OPEN_QUESTIONS},
        ResearchMemo(), [])

    assert len(memo.open_questions) == 2, "the questions must reach the memo, not be refused"
    assert memo.findings == ["f1"], "the fields that always validated must still be kept"


# --- ELEMENT healing: the same all-or-nothing, one level down ------------------------------------
# `_finalize` keeps every field that validated, but pydantic refuses a `list[str]` field ENTIRELY
# over ONE `None` inside it — so a memo emitting `["q1", null, "q2"]` loses both real questions and
# the board stays empty, which is the outcome the drop-the-offender rung was written to end.


def test_one_null_element_does_not_cost_the_whole_question_list():
    """MUTATION: delete the `list[str]` branch of `_healed_list_elements` -> pydantic refuses the
    field, `_finalize` drops it, and BOTH real questions are lost over a single null."""
    out = _MemoOut.model_validate(
        {"open_questions": ["Does distillation help?", None, "Does temperature matter?"]})

    assert out.open_questions == ["Does distillation help?", "", "Does temperature matter?"]


def test_a_blanked_question_KEEPS_ITS_SLOT_because_position_is_the_join():
    """`question_concepts[i]` describes `open_questions[i]`, so dropping the bad element would put
    every later question on its neighbour's concepts — `core/models.py::_read_registered_questions`
    argues this from c438f1c9 and this surface must not decide it differently.

    MUTATION: `[i for i in value if isinstance(i, str)]` -> the lists are 2 and 3 long, and
    "Does temperature matter?" silently inherits `["loss/contrastive"]`.
    """
    out = _MemoOut.model_validate({
        "open_questions": ["Does distillation help?", None, "Does temperature matter?"],
        "question_concepts": [["distill/teacher"], ["junk"], ["loss/contrastive"]],
    })

    assert len(out.open_questions) == len(out.question_concepts) == 3
    assert out.question_concepts[2] == ["loss/contrastive"], "the join must not shift"


def test_a_concept_ID_is_DROPPED_where_a_question_is_blanked():
    """The opposite rule, and deliberately so: a row's ids are an unordered SET, and coercing `2`
    to `"2"` would register a concept named "2" on the graph — worse than registering none.

    MUTATION: blank the ids instead of dropping -> `["distill/teacher", ""]`.
    """
    out = _MemoOut.model_validate({"question_concepts": [["distill/teacher", 2], ["loss/x"]]})

    assert out.question_concepts == [["distill/teacher"], ["loss/x"]]


def test_a_row_of_the_WRONG_SHAPE_is_left_for_the_rung_that_REPORTS_it():
    """The one place this healer deliberately differs from `core/models.py`, and the reason is the
    surrounding machinery rather than the data.

    There, blanking a flat row to `[]` is the only way to keep anything at all. Here `_finalize`
    already refuses just the offending key, keeps the other nine fields and LOGS what it dropped —
    so healing the whole-shape case trades a VISIBLE refusal for a silent empty. The first cut of
    this rung did exactly that: `question_concepts: ["flat"]` became `[[], []]`, every concept gone
    and nothing said, which `_finalize`'s own docstring forbids in as many words. It was caught by
    `tests/test_memo_keeps_what_validated.py`, and this is that property from the healer's side.

    MUTATION: `else []` instead of `else row` -> the field validates as empty rows, `_finalize`
    never names it, and the loss becomes invisible.
    """
    with pytest.raises(ValidationError):
        _MemoOut.model_validate({"question_concepts": ["flat"]})
    with pytest.raises(ValidationError):
        _MemoOut.model_validate({"question_concepts": [["ok"], None]})


def test_a_repaired_element_is_SAID_OUT_LOUD(caplog):
    """Healing keeps more than refusing does, but a malformed memo is still a fact about the
    model's output — the same reason `_finalize` and `_admissible_beliefs` log what they discard.

    MUTATION: drop the WARNING -> element repair becomes the invisible loss this whole rung was
    corrected for, one level below the field-level refusal that IS reported.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _MemoOut.model_validate({"open_questions": ["a", None], "findings": ["f"]})
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "open_questions" in logged and "repaired" in logged
    assert "findings" not in logged, "a field that arrived clean must not be reported"

    # A DECODE is not a repair: `_decoded_json_list` fails closed unless the decode yields a list,
    # so it is a lossless re-reading of exactly what the model sent and announcing it would be noise.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _MemoOut.model_validate({"open_questions": '["a", "b"]'})
    assert not caplog.records, "a clean stringified list loses nothing and must stay quiet"


def test_a_junk_node_id_is_dropped_rather_than_fabricated():
    """`node_ids` is an unordered evidence set with no positional join, so a bad element is dropped;
    coercing it would FABRICATE a citation, which is what `trust/memo_verify.py` exists to catch.
    `isinstance(True, int)` is True and a bool is not a node id.

    MUTATION: keep bools -> `node_ids == [1, True, 3]` and a claim cites node 1 twice.
    """
    out = _ClaimOut.model_validate({"statement": "c", "node_ids": [1, None, True, "x", 3]})

    assert out.node_ids == [1, 3]


def test_the_two_healings_COMPOSE_in_the_only_order_that_works():
    """A field arriving as a JSON STRING has no elements to inspect until the decode has run, so
    element healing is only reachable after `_decoded_json_list`.

    MUTATION: heal elements BEFORE decoding -> the value is still a `str`, the element branch is
    skipped, and the null inside refuses the field exactly as before.
    """
    out = _MemoOut.model_validate({"open_questions": '["a", null, "b"]'})

    assert out.open_questions == ["a", "", "b"]


def test_a_clean_payload_is_returned_untouched():
    """Identity, not equality: the validator must leave a well-formed emit byte-identical rather
    than rebuilding it, so nothing about a healthy memo depends on this rung having run."""
    from looplab.agents.deep_research import _healed_list_elements

    clean = ["a", "b"]
    assert _healed_list_elements(list[str], clean) is clean
    assert _healed_list_elements(list[list[str]], [["x"]]) == [["x"]]
    # A shape the rung deliberately does NOT heal — what a healed nested model would be is a guess.
    nested = [{"statement": "c"}]
    assert _healed_list_elements(list[_ClaimOut], nested) is nested


def test_the_annotations_are_matched_by_EQUALITY_and_never_by_identity():
    """`list[str] is list[str]` is FALSE — every subscription builds a fresh `types.GenericAlias` —
    so an identity test makes every branch unreachable and the whole rung inert with nothing red.
    That was the first cut here, and it is the same shape as `_decoded_json_list`'s dead
    `startswith` fast path recorded above.

    MUTATION: `==` -> `is` in `_healed_list_elements` -> this fails and so does every element test.
    """
    from looplab.agents.deep_research import _healed_list_elements

    assert list[str] is not list[str], "the premise: identity does not hold for a GenericAlias"
    # So the branch must be reachable through a SEPARATELY CONSTRUCTED annotation object, which is
    # exactly what `model_fields[...].annotation` hands it at runtime.
    annotation = _MemoOut.model_fields["open_questions"].annotation
    assert annotation is not list[str] and annotation == list[str]
    assert _healed_list_elements(annotation, ["a", None]) == ["a", ""]
