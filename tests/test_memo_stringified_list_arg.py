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
