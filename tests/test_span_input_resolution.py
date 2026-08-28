"""Rebuilding a span's prompt must follow the carry chain, not stop at the span's own `input`.

`core/tracing.py:634` stores a chained turn as `input=cur[np:]`, `input_carry=np` (an INTEGER
prefix length), `input_from=<parent span id>`. A reader that treats `input` as the whole prompt
sees only the newest messages. Measured cost of that mistake on 2026-08-28: the budget line was
reported in 3 of 32 dsBud step prompts when it is in 35 of 35; the step-feedback block in dsFB was
reported at 49 of 125 against a true 90; the invalid-solution reason in gpt56luna at 155 of 868
against a true 402.
"""
from __future__ import annotations

import json

from benchmarks.algotune.span_input import load, resolve, text


def _msg(role, content):
    return {"role": role, "content": content}


def _write(tmp_path, spans):
    path = tmp_path / "spans.jsonl"
    path.write_text("\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8")
    return path


def _span(sid, *, input_, carry=0, src=None, name="generation", phase="plan_step"):
    attrs = {"input": input_, "phase": phase}
    if src is not None:
        attrs["input_from"] = src
        attrs["input_carry"] = carry
    return {"span_id": sid, "name": name, "attributes": attrs}


def test_the_carried_prefix_is_prepended_and_the_chain_is_followed(tmp_path):
    base = [_msg("system", "S"), _msg("user", "BUDGET: $0.10 of $1.00 spent"), _msg("assistant", "a1")]
    spans = [
        _span("A", input_=base),
        _span("B", input_=[_msg("tool", "t1")], carry=3, src="A"),
        _span("C", input_=[_msg("tool", "t2")], carry=4, src="B"),
    ]
    _, by_id = load(_write(tmp_path, spans))

    assert [m["content"] for m in resolve(by_id["B"], by_id)] == ["S", "BUDGET: $0.10 of $1.00 spent", "a1", "t1"]
    assert [m["content"] for m in resolve(by_id["C"], by_id)] == \
        ["S", "BUDGET: $0.10 of $1.00 spent", "a1", "t1", "t2"]
    # the whole point: the user turn is visible from every later span, not just the first
    for sid in ("A", "B", "C"):
        assert "BUDGET: $0.10 of $1.00 spent" in text(by_id[sid], by_id), \
            f"span {sid} lost the carried prefix -- this is the 3-of-32 bug"


def test_the_carry_truncates_and_is_not_just_concatenation(tmp_path):
    """`input_carry` is a PREFIX LENGTH: a shorter carry drops the tail of the parent's list."""
    base = [_msg("system", "S"), _msg("user", "U"), _msg("assistant", "DROPPED")]
    spans = [_span("A", input_=base), _span("B", input_=[_msg("tool", "t")], carry=2, src="A")]
    _, by_id = load(_write(tmp_path, spans))
    assert [m["content"] for m in resolve(by_id["B"], by_id)] == ["S", "U", "t"]
    assert "DROPPED" not in text(by_id["B"], by_id)


def test_a_full_base_and_an_old_log_are_returned_as_they_are(tmp_path):
    full = [_msg("system", "S"), _msg("user", "U")]
    spans = [
        _span("A", input_=full),                                   # input_carry=0, input_from=None
        {"span_id": "OLD", "name": "generation",                   # pre-carry writer: no keys at all
         "attributes": {"input": [_msg("user", "legacy")]}},
    ]
    _, by_id = load(_write(tmp_path, spans))
    assert [m["content"] for m in resolve(by_id["A"], by_id)] == ["S", "U"]
    assert [m["content"] for m in resolve(by_id["OLD"], by_id)] == ["legacy"]


def test_every_broken_chain_degrades_to_the_span_s_own_input(tmp_path):
    """A measuring tool answers partially; it does not raise."""
    spans = [
        _span("MISSING", input_=[_msg("tool", "x")], carry=3, src="not-in-this-file"),
        _span("JUNK", input_=[_msg("tool", "y")], carry=3, src="SELF"),
        {"span_id": "SELF", "name": "generation",
         "attributes": {"input": [_msg("tool", "z")], "input_from": "JUNK", "input_carry": 2}},
    ]
    _, by_id = load(_write(tmp_path, spans))
    assert [m["content"] for m in resolve(by_id["MISSING"], by_id)] == ["x"]   # parent absent
    assert resolve(by_id["JUNK"], by_id)                                       # cycle: no recursion error
    assert resolve(by_id["SELF"], by_id)

    # a non-integer carry from an older or corrupt writer is ignored, not crashed on
    bad = {"span_id": "BAD", "name": "generation",
           "attributes": {"input": [_msg("tool", "w")], "input_from": "MISSING", "input_carry": ["x"]}}
    path = _write(tmp_path, spans + [bad])
    _, by_id2 = load(path)
    assert [m["content"] for m in resolve(by_id2["BAD"], by_id2)] == ["w"]


def test_a_torn_line_is_skipped_rather_than_fatal(tmp_path):
    path = tmp_path / "spans.jsonl"
    path.write_text(json.dumps(_span("A", input_=[_msg("user", "U")])) + "\n{\"half\": \n", encoding="utf-8")
    spans, by_id = load(path)
    assert len(spans) == 1 and "A" in by_id
