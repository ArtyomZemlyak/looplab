"""`llm_reasoning` and `llm_reasoning_style` are closed vocabularies, checked where every other
closed vocabulary is checked.

They were the two that were not. `Settings._ENUM_FIELDS` exists because a mistyped knob that reads
as a no-op is worse than one that raises, and these two produce BOTH shapes of that failure — probed
against the real `llm.reasoning_body`:

  * `llm_reasoning="banana"` -> `{"reasoning_effort": "banana"}` on an effort-style provider, which
    400s. Loud, but only at the provider — and the client's reject classifier then flips reasoning
    OFF for the rest of its lifetime, so one typo quietly degrades every later call.
  * `llm_reasoning_style="banana"` -> `{}`. The `if/elif` chain matches nothing, the body is empty,
    reasoning is never requested and NOTHING raises anywhere. That is the exact silent
    fall-through the table was built for, on a field whose shipped default is `high`.

THE CHECK IS CASE-INSENSITIVE, AND THAT IS NOT A CONVENIENCE. `reasoning_body` lowercases both
values before reading them, so `llm_reasoning="High"` works today; a case-sensitive membership test
would refuse a config that is doing nothing wrong. The vocabulary is matched the way the READER
matches it, which is why these two live in their own table rather than in `_ENUM_FIELDS`.

The vocabularies are spelled out in `config.py` (core stays import-light and imports nothing above
itself), so this file is what keeps the two spellings from drifting: it derives the reader's own
accepted set by DRIVING it, never by reading the constant back.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.core.llm import reasoning_body

_DECLARED = dict(Settings._CASE_INSENSITIVE_ENUM_FIELDS)


def test_the_declared_vocabularies_are_exactly_what_the_reader_honours():
    """DERIVED, not pinned: every declared value must produce a DISTINCT, intentional request body,
    and nothing outside the table may.

    MUTATION: add "banana" to either tuple -> it shapes an empty/garbage body and this goes red.
    """
    # `style` decides the shape, so the modes are probed against a fixed effort-style provider.
    for mode in _DECLARED["llm_reasoning"]:
        body = reasoning_body("gpt-4", mode, "effort")
        if mode in ("", "off", "none", "false", "0"):
            assert body == {}, f"{mode!r} must send nothing, got {body}"
        else:
            assert "reasoning_effort" in body, f"{mode!r} must request an effort, got {body}"
            assert body["reasoning_effort"] in ("low", "medium", "high"), (
                f"{mode!r} produced {body} — an effort provider accepts only low|medium|high")

    for style in _DECLARED["llm_reasoning_style"]:
        # A qwen model so `auto` resolves to the qwen shape and every branch is reachable.
        body = reasoning_body("qwen3-32b", "high", style)
        if style == "none":
            assert body == {}, "`none` shapes nothing by design and relies on llm_reasoning_extra"
        else:
            assert body, f"style {style!r} shaped nothing — that is the silent fall-through"


def test_a_value_outside_the_vocabulary_produces_the_failure_it_is_refused_for():
    """The evidence that these tuples are not arbitrary. Drives the reader, then the validator."""
    assert reasoning_body("gpt-4", "banana", "effort") == {"reasoning_effort": "banana"}, (
        "an unknown effort is forwarded verbatim — this is the 400 the provider answers")
    assert reasoning_body("gpt-4", "high", "banana") == {}, (
        "an unknown style shapes NOTHING and raises nowhere — the quiet half")

    with pytest.raises(ValueError):
        Settings(llm_reasoning="banana")
    with pytest.raises(ValueError):
        Settings(llm_reasoning_style="banana")


@pytest.mark.parametrize("field,value", [
    ("llm_reasoning", "High"),
    ("llm_reasoning", "OFF"),
    ("llm_reasoning_style", "Qwen"),
    ("llm_reasoning_style", "AUTO"),
])
def test_case_is_accepted_because_the_reader_lowercases(field, value):
    """MUTATION: make the check case-SENSITIVE -> these raise, and a working config breaks."""
    assert Settings(**{field: value}) is not None


def test_every_declared_value_actually_constructs():
    """The table and the model must agree; a typo in the tuple would refuse the default itself."""
    for field, allowed in Settings._CASE_INSENSITIVE_ENUM_FIELDS:
        for value in allowed:
            Settings(**{field: value})
    assert Settings().llm_reasoning == "high", "the shipped default must be in its own vocabulary"
    assert Settings().llm_reasoning_style == "auto"
