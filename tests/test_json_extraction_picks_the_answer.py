"""The model's ANSWER, not the first object it typed.

`_extract_json` returned the first complete top-level JSON object. The text-path hint message ends
by pasting the caller's whole JSON schema (`_walk_parsers`), so a model that echoes or restates it
before answering had its ECHO parsed as the answer: `{"type": "object", "properties": {…}}` decodes
cleanly, is a `dict`, and carries none of the requested fields. It then either fails validation — a
wasted provider call and a fall-through to the next parser — or, for a model whose fields are all
optional with defaults, VALIDATES, returning an object of entirely default values as though the
model had chosen them. A worked example, or a restated few-shot, does the same.

The rule is conservative: candidates are scored against the schema and the FIRST wins every tie, so
an answer changes only when a LATER object matches the schema STRICTLY better. With no schema it is
byte-identical to what it replaces, which is what keeps every direct caller and every older test
green.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from looplab.core.parse import (
    _JSON_CANDIDATE_CAP, _extract_json, _schema_fit, _schema_key_sets, parse_structured)


class _Answer(BaseModel):
    operator: str = "draft"
    rationale: str = ""
    params: dict = {}


_SCHEMA = _Answer.model_json_schema()


def test_a_schema_echo_does_not_win_over_the_answer():
    """THE DEFECT, in the shape the hint message creates. MUTATION: return the first object again ->
    the schema echo is parsed as the reply."""
    reply = (f"Understood, the schema is {json.dumps(_SCHEMA)}.\n\n"
             '{"operator": "improve", "rationale": "raise lr", "params": {"lr": 0.1}}')
    assert _extract_json(reply, _SCHEMA)["operator"] == "improve"


def test_a_worked_EXAMPLE_does_not_win_over_the_answer():
    """The other producer of a leading object: a model that shows its working."""
    reply = ('For example one might answer {"operator": "draft"} — but here is my answer:\n'
             '{"operator": "merge", "rationale": "combine 1 and 2", "params": {"w": 0.5}}')
    assert _extract_json(reply, _SCHEMA)["operator"] == "merge"


def test_the_FIRST_object_still_wins_a_TIE():
    """The conservatism that keeps this from being a second guess. Two objects that answer the schema
    equally well are the model answering twice, and the first is what every caller got before.

    MUTATION: prefer the last on a tie -> a model that revises itself mid-reply silently changes
    which answer the run acts on, in every reply, not only the pathological ones.
    """
    reply = ('{"operator": "draft", "rationale": "a", "params": {}}\n'
             '{"operator": "improve", "rationale": "b", "params": {}}')
    assert _extract_json(reply, _SCHEMA)["operator"] == "draft"


def test_with_NO_schema_it_is_the_historical_first_object_walk():
    """Every direct caller and every pre-existing test takes this path."""
    reply = 'Sure: {"operator": "draft", "params": {"x": 1.0}} note: see {y}'
    assert _extract_json(reply) == {"operator": "draft", "params": {"x": 1.0}}
    assert _extract_json(reply, None) == {"operator": "draft", "params": {"x": 1.0}}


def test_an_UNREADABLE_schema_degrades_to_that_same_walk():
    """`_schema_key_sets` is handed whatever `model_json_schema()` produced. A shape it cannot read
    must mean "no opinion", never an exception out of a parser whose job is tolerating bad input."""
    for junk in ("not a schema", [], {"properties": "nope", "required": 3}, {}):
        assert _extract_json('{"operator": "draft"}', junk) == {"operator": "draft"}


def test_a_perfect_match_short_circuits():
    """An answer carrying every declared field cannot be beaten, so the scan stops — a reply whose
    prose after the answer is megabytes of braces must not be walked."""
    answer = '{"operator": "x", "rationale": "y", "params": {}}'
    assert _extract_json(answer + "{" * 100_000, _SCHEMA)["operator"] == "x"


def test_the_candidate_scan_is_bounded():
    """A reply that opens more than the cap is prose ABOUT json, and the bound is on the work. It
    degrades to the best of what it did read — never to an exception."""
    reply = "".join('{"unrelated": %d}' % i for i in range(_JSON_CANDIDATE_CAP * 4))
    reply += '{"operator": "late", "rationale": "r", "params": {}}'
    got = _extract_json(reply, _SCHEMA)
    assert isinstance(got, dict), "past the cap it still answers"


def test_nothing_json_at_all_still_raises_ParseError():
    from looplab.core.parse import ParseError

    with pytest.raises(ParseError):
        _extract_json("no braces here", _SCHEMA)


def test_the_lenient_python_literal_fallback_still_fires():
    """Small models emit near-JSON. This path is unchanged and must stay reachable — it runs only
    when the strict walk found no dict at all."""
    got = _extract_json("Here: {'operator': 'improve', 'params': {'x': 2.0,}}")
    assert got["operator"] == "improve"


def test_the_fit_is_required_first_then_declared():
    """Compared as a tuple, so a candidate carrying every REQUIRED field beats one that merely
    mentions more optional names."""
    required, declared = frozenset({"a"}), frozenset({"a", "b", "c"})
    assert _schema_fit({"a": 1}, required, declared) > _schema_fit({"b": 1, "c": 2}, required, declared)


def test_a_schema_with_no_required_block_still_discriminates():
    """Several of this repo's models are entirely optional-with-defaults, which is exactly the case
    where a schema echo VALIDATES. MUTATION: score on `required` alone -> everything ties at 0 and
    the echo wins again, silently returning an all-defaults object as the model's choice."""
    required, declared = _schema_key_sets(_SCHEMA)
    assert not required and declared >= {"operator", "rationale", "params"}
    echo = {"type": "object", "properties": {}, "title": "_Answer"}
    assert _schema_fit(echo, required, declared) < _schema_fit({"operator": "x"}, required, declared)


def test_end_to_end_through_parse_structured():
    """THE REAL PATH: the text parser, the real hint message, a client that echoes the schema it was
    handed and then answers. MUTATION: drop the `schema` argument at the call site -> the extractor
    is blind again and this validates the echo into an all-defaults `_Answer`."""
    class _Client:
        model = "m"

        def complete_tool(self, messages, json_schema, **kw):
            raise RuntimeError("force the text path")

        def complete_text(self, messages, **kw):
            schema = messages[-1]["content"].split("schema: ", 1)[1]
            return (f"Sure. The schema you gave me is {schema}\n\nMy answer:\n"
                    '{"operator": "improve", "rationale": "raise lr", "params": {"lr": 0.1}}')

    got = parse_structured(_Client(), [{"role": "user", "content": "go"}], _Answer, "baml")
    assert got.operator == "improve" and got.rationale == "raise lr"
