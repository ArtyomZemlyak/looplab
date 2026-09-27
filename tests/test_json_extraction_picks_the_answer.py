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


def test_a_worked_EXAMPLE_carrying_REAL_FIELDS_is_a_TIE_and_the_first_wins():
    """THE HONEST LIMIT, and it was asserted the other way for four days at a real cost.

    A schema can decide "does this object answer the question at all" — which is exactly what
    catches the echo above, since `{"type": "object", "properties": {...}}` carries NONE of the
    declared names. It cannot decide which of two objects that BOTH answer it the model meant: a
    worked example and an answer are the same shape, and only the prose around them says which is
    which.

    Scoring the second half as a COUNT of declared keys buys the leading-example case, and pays for
    it with the TRAILING-example case on every emit model in this tree — none of them declares a
    `required` block, so the score collapses to "more optional keys wins" and any later, fuller
    object is a strict improvement. Reproduced against the real `_StrategyOut` (14 properties, no
    `required`): a reply answering `{"policy": "greedy", "rationale": "seed phase"}` and then
    illustrating a fuller decision returned the ILLUSTRATION — a policy/fidelity/developer switch
    taken from the model's own worked example — while the same reply with no schema returned the
    answer.

    Neither position is safe on its own, so the tie goes to the rule this module already states and
    every caller had before: the FIRST one. What the schema still decides is the echo, which is the
    defect this scoring was written for.
    """
    reply = ('For example one might answer {"operator": "draft"} — but here is my answer:\n'
             '{"operator": "merge", "rationale": "combine 1 and 2", "params": {"w": 0.5}}')
    assert _extract_json(reply, _SCHEMA)["operator"] == "draft"


def test_a_TRAILING_worked_example_does_not_win_over_the_answer():
    """The complement, and the case the count rule got wrong on every model in this tree.

    MUTATION: score the second half as `len(keys & declared)` again -> the illustration wins, and
    on a Strategist reply that is a policy/fidelity/developer switch the run then acts on.
    """
    reply = ('{"operator": "improve", "rationale": "raise lr"}\n'
             'A fuller answer might look like: '
             '{"operator": "merge", "rationale": "x", "params": {"w": 0.5}}')
    assert _extract_json(reply, _SCHEMA)["operator"] == "improve"


def test_a_REQUIRED_block_is_still_counted_and_still_decides():
    """Where the schema DOES say which fields an answer must carry, that is a real discrimination
    and it is used: a candidate missing a required field loses to one that has it, wherever it sits.
    """
    schema = {"properties": {"operator": {}, "rationale": {}, "params": {}},
              "required": ["operator", "rationale"]}
    reply = ('{"operator": "draft"}\n'
             '{"operator": "merge", "rationale": "combine 1 and 2"}')
    assert _extract_json(reply, schema)["operator"] == "merge"
    assert _schema_fit({"operator": "x"}, frozenset({"operator", "rationale"}),
                       frozenset({"operator", "rationale", "params"})) == (1, True)


def test_the_scan_STOPS_at_the_first_object_that_answers_the_schema():
    """The efficiency half, and it is the same rule seen from the side.

    Short-circuiting on "every DECLARED field present" meant a model that legitimately omitted one
    optional field never short-circuited, and the walk ran `text.find("{")` + `raw_decode` to the
    END of the reply. `_JSON_CANDIDATE_CAP` never bounded that — it counts DECODED candidates, and
    the cost is in the FAILED decodes. Measured 0.63 s against 0.0002 s on a 197 KB reply with
    16,001 braces, per structured call on the text-parser path.

    Driven as a complexity claim: 16,000 trailing braces must cost what none of them do.
    """
    import time

    answer = '{"operator": "improve", "rationale": "r"}'
    _extract_json(answer, _SCHEMA)                       # warm
    start = time.perf_counter()
    assert _extract_json(answer + "\n{" * 16_000, _SCHEMA)["operator"] == "improve"
    assert time.perf_counter() - start < 0.05, "the scan must stop at the answer, not at EOF"


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
    degrades to the best of what it did read — and when nothing it read answers the schema, that
    best is REFUSED rather than returned (doc 69 69.17): `{"unrelated": 0}` validated into an
    all-defaults answer the model never gave."""
    from looplab.core.parse import ParseError

    unrelated = "".join('{"unrelated": %d}' % i for i in range(_JSON_CANDIDATE_CAP * 4))
    with pytest.raises(ParseError):
        _extract_json(unrelated + '{"operator": "late", "rationale": "r", "params": {}}', _SCHEMA)
    got = _extract_json('{"operator": "early"}' + unrelated, _SCHEMA)
    assert got == {"operator": "early"}, "an answer among what it read still answers"


def test_an_object_that_answers_nothing_is_never_the_answer():
    """Doc 69 69.17: the best candidate was returned even when it carried not one of the schema's
    names — the schema's own echo, alone in the reply, validated into an all-defaults object and
    the report path published it as the model's. Refused now, on the strict walk and on the lenient
    literal fallback alike; `{}` — every field left to its default — still answers, and a reply
    with no schema is the historical walk, echo and all."""
    from looplab.core.parse import ParseError

    echo = json.dumps(_SCHEMA)
    for reply in (f"The schema is {echo}.", "{'type': 'object', 'title': '_Answer'}"):
        with pytest.raises(ParseError):
            _extract_json(reply, _SCHEMA)
    assert _extract_json("Nothing to change: {}", _SCHEMA) == {}
    assert _extract_json(f"The schema is {echo}.")["type"] == "object"
    assert _extract_json("{'operator': 'improve', 'bogus': 1,}", _SCHEMA)["operator"] == "improve"


def test_an_EMPTY_answer_typed_after_the_echo_still_answers():
    """`{}` and the schema's echo tie at (0, False), and "the first candidate wins ties" handed the
    echo — typed first — a win `_answers` then refused: "…Nothing to change: {}" RAISED where the
    reply answered (critic 2026-09-27, driven). The ECHO ranks below everything else at the same
    fit. MUTATION: rank on the fit alone -> the first reply below raises again."""
    from looplab.core.parse import ParseError

    echo = json.dumps(_SCHEMA)
    assert _extract_json(f"The schema is {echo}. Nothing to change: {{}}", _SCHEMA) == {}
    assert _extract_json(f"Nothing to change: {{}}. (The schema was {echo}.)", _SCHEMA) == {}
    # Two non-answers still answer nothing, whichever is typed first.
    with pytest.raises(ParseError):
        _extract_json(f'The schema is {echo}. Another shape: {{"kind": "x"}}', _SCHEMA)


def test_an_empty_object_in_prose_never_outranks_the_model_s_own_object():
    """The first cut of the rule above let ANY answer outrank ANY non-answer, and `{}` answers: a
    `cfg = {}` in a snippet or a `"{}".format` placeholder after the model's own wrong-shaped object
    then won, and validated into the all-default answer 69.17 is about — the report path published
    an empty report as the model's (critic 2026-09-27, driven). Only the schema's own ECHO ranks
    below `{}`; the model's wrong shape ties with it, wins as the first typed, and is refused, so
    the caller's fallback decides. MUTATION: rank every non-answer below `{}` -> `{}` is returned."""
    from looplab.core.parse import ParseError, _standing

    for reply in ('{"title": "recall improved 12%", "body": "the champion wins"}\nNote: '
                  '`cfg = {}` is the default.',
                  '{"summary_line": "recall improved"}  (render with "{}".format(x))'):
        with pytest.raises(ParseError):
            _extract_json(reply, _SCHEMA)
    # `{}` typed FIRST is still the first candidate, as it always was.
    assert _extract_json('Defaults are {}. {"summary_line": "recall improved"}', _SCHEMA) == {}
    # The echo test is every name a JSON-Schema keyword — one foreign name makes a wrong shape.
    assert _standing({"type": "object", "title": "T", "properties": {}}, (0, False)) == 0
    assert _standing({"title": "x", "body": "y"}, (0, False)) == 1
    assert _standing({}, (0, False)) == 1 and _standing({"operator": "x"}, (0, True)) == 1


def test_a_field_s_alias_is_its_own_spelling_never_drift():
    """`_case_drifted` read `{"Operator": …}` as drift for a field whose ALIAS is `Operator`, sent it
    to the repair — which keys by field name — and the value was lost: the default, or a ParseError
    for a required field (critic 2026-09-27, driven; latent, no parse target has an alias today).
    MUTATION: ignore aliases -> `d` / ParseError."""
    from pydantic import Field

    from looplab.core.parse import _case_drifted

    class _Alias(BaseModel):
        operator: str = Field(default="d", alias="Operator")

    class _AliasReq(BaseModel):
        operator: str = Field(alias="Operator")

    class _Client:
        model = "m"

        def complete_tool(self, messages, json_schema, **kw):
            raise RuntimeError("force the text path")

        def complete_text(self, messages, **kw):
            return '{"Operator": "improve"}'

    msgs = [{"role": "user", "content": "go"}]
    assert not _case_drifted({"Operator": "improve"}, _Alias)
    assert _case_drifted({"OPERATOR": "improve"}, _Alias), "another case of the alias still drifts"
    assert parse_structured(_Client(), msgs, _Alias, "baml").operator == "improve"
    assert parse_structured(_Client(), msgs, _AliasReq, "baml").operator == "improve"


def _parse_attrs(tmp_path, client, model):
    """`parse_structured` under a real tracer; returns (answer, the structured_parse attributes)."""
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path))
    with tracer.span("propose", kind="operation"):
        answer = parse_structured(client, [{"role": "user", "content": "x"}], model)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return answer, next(r for r in rows if r.get("name") == "structured_parse")["attributes"]


def test_an_exact_key_beside_its_case_variant_is_no_drift_and_no_repair(tmp_path):
    """The exact field name present means the drift rule has nothing to rescue: plain validation
    reads it, and the win is a CLEAN one on the span (`repaired: False`). MUTATION: drop the
    exact-name check -> the same value, recorded `repaired: True` (critic 2026-09-27, e4-08)."""
    class _Op(BaseModel):
        operator: str = "d"

    class _Both:
        def complete_tool(self, messages, schema):
            return {"operator": "a", "Operator": "b"}

    answer, attrs = _parse_attrs(tmp_path, _Both(), _Op)
    assert answer.operator == "a" and attrs["repaired"] is False and attrs["attempts"] == 1


def test_a_drifted_key_whose_value_does_not_fit_is_dropped_not_fatal(tmp_path):
    """`{"note": …, "N": "a few"}` for an int `n`: the drift sent it to the repair, the repair could
    not coerce "a few", and the reply that parsed in ONE call before the drift rule cost a second
    provider call and then the whole parse (critic 2026-09-27, driven). The plain validation the
    drift skipped is the last rung: the junk key is dropped, `note` kept, and the span says the
    answer was repaired. MUTATION: no fallback -> ParseError after two calls."""
    class _Out(BaseModel):
        note: str = ""
        n: int = 0

    class _Junk:
        calls = 0

        def complete_tool(self, messages, schema):
            self.calls += 1
            return {"note": "raise the margin", "N": "a few"}

    client = _Junk()
    answer, attrs = _parse_attrs(tmp_path, client, _Out)
    assert (answer.note, answer.n) == ("raise the margin", 0)
    assert client.calls == 1 and attrs["repaired"] is True


def test_a_CASE_DRIFTED_answer_reaches_the_repair_end_to_end():
    """`_coerce_to_model` matches a key case-insensitively — the H2 repair's documented job — and
    the 69.17 refusal compared a reply's names with the schema's case-SENSITIVELY, so `{"Operator":
    …}` "answered nothing" and was refused before the repair ran; a model with REQUIRED fields, the
    Researcher's own emit model among them, included (critic 2026-09-27, driven). `topK` is a schema
    name that is not lower-case itself, so the schema's side of the fold is driven too. MUTATION:
    drop the case fold of the reply's keys, of the schema's declared names or of its required names
    -> ParseError."""
    class _Req(BaseModel):
        operator: str
        rationale: str = ""

    class _Opt(BaseModel):
        topK: int = 0
        note: str = ""

    class _ReqK(BaseModel):
        topK: int
        note: str = ""

    class _Client:
        model = "m"

        def complete_tool(self, messages, json_schema, **kw):
            raise RuntimeError("force the text path")

        def complete_text(self, messages, **kw):
            return self.reply

    client, msgs = _Client(), [{"role": "user", "content": "go"}]
    for model, reply in ((_Answer, '{"Operator": "improve", "RATIONALE": "raise lr"}'),
                         (_Req, '{"Operator": "improve", "RATIONALE": "raise lr"}'),
                         (_Req, "{'OPERATOR': 'improve', 'Rationale': 'raise lr',}")):
        client.reply = reply
        got = parse_structured(client, msgs, model, "baml")
        assert got.operator == "improve" and got.rationale == "raise lr", reply
    client.reply = '{"TOPK": 3}'
    assert parse_structured(client, msgs, _Opt, "baml").topK == 3
    # A later object carrying the REQUIRED name, drifted in case, still outranks an earlier one
    # carrying only an optional name.
    client.reply = '{"note": "first"} then {"TOPK": 3}'
    assert parse_structured(client, msgs, _ReqK, "baml").topK == 3


def test_a_reply_that_only_echoes_the_schema_is_not_an_answer_end_to_end():
    """THE REAL PATH of 69.17: the text parser, the real hint, a client that only echoes the schema
    it was handed. It raised nothing and returned an all-defaults `_Answer`; now every parser fails
    and the caller's own fallback decides (`serve/report.py`'s deterministic report)."""
    from looplab.core.parse import ParseError

    class _Client:
        model = "m"

        def complete_tool(self, messages, json_schema, **kw):
            raise RuntimeError("force the text path")

        def complete_text(self, messages, **kw):
            return "Sure. The schema you gave me is " + messages[-1]["content"].split("schema: ", 1)[1]

    with pytest.raises(ParseError):
        parse_structured(_Client(), [{"role": "user", "content": "go"}], _Answer, "baml")


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
