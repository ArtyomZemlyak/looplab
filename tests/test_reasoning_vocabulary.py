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

from looplab.core.config import Settings, settings_from_snapshot
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


@pytest.mark.parametrize("recorded,resumed_as", [
    ("true", "on"), ("1", "on"), ("yes", "on"), ("enabled", "on"),
    ("false", "false"), ("0", "0"),          # already IN the vocabulary; untouched
    ("high", "high"), ("", ""), ("off", "off"),
])
def test_a_run_recorded_with_a_historically_accepted_spelling_still_RESUMES(recorded, resumed_as):
    """THE ASYMMETRY: strict at submit, canonicalizing on reload.

    The closed vocabulary is narrower than the reader that had been accepting these for months, and
    asymmetrically so — `reasoning_body` computes `on = mode not in ("off","none","false","0")`, so
    `false`/`0` are valid OFF spellings AND are in the set, while `true`/`1`/`yes` were equally
    valid ON spellings against a qwen-style endpoint and are not. Refusing them at
    `Settings(**snapshot)` made a healthy run unresumable, unfinalizable, and unreadable by the
    server's own config route, with hand-editing `config.snapshot.json` as the only remedy.

    Mutation: drop `_canonicalize_snapshot_reasoning` from `settings_from_snapshot` -> the four ON
    spellings raise ValidationError here, which is exactly how it shipped."""
    assert settings_from_snapshot({"llm_reasoning": recorded}).llm_reasoning == resumed_as


def test_the_reload_canonicalization_PRESERVES_WHAT_THE_READER_DID():
    """It maps onto meaning, not onto a default. A spelling the reader DISABLED on must not come
    back enabled — that would resume a run under different paid semantics than it recorded, which is
    the very thing invariant #6 exists to prevent."""
    for off_spelling in ("false", "0", "none", "off"):
        resumed = settings_from_snapshot({"llm_reasoning": off_spelling}).llm_reasoning
        assert reasoning_body("qwen3", resumed) == reasoning_body("qwen3", off_spelling), (
            f"{off_spelling!r} disabled reasoning; resuming it must still disable reasoning")
    for on_spelling in ("true", "1", "yes"):
        resumed = settings_from_snapshot({"llm_reasoning": on_spelling}).llm_reasoning
        assert reasoning_body("qwen3", resumed) == {"chat_template_kwargs": {"enable_thinking": True}}


def test_a_fresh_submit_is_STILL_refused():
    """The reload path softens; the submit path does not. A typo must still be caught where it is
    cheap, which is the whole reason the closed vocabulary exists."""
    with pytest.raises(Exception):
        Settings(llm_reasoning="banana")
    with pytest.raises(Exception):
        Settings(llm_reasoning="true")


def test_an_unknown_STYLE_is_not_canonicalized():
    """`llm_reasoning_style` shapes NOTHING when unknown (`reasoning_body` returns `{}`), so there
    is no historical behaviour to preserve — only a field whose default is `auto` silently never
    requesting reasoning. That one resumes with the refusal it deserves."""
    assert reasoning_body("qwen3", "on", "banana") == {}
    with pytest.raises(Exception):
        settings_from_snapshot({"llm_reasoning_style": "banana"})
