"""The pushed hint carries the FIRST FIVE directions. The memo body carries every one.

THE DEFECT WAS A SENTENCE, and the sentence is the thing that gets believed. `admit_research_beliefs`
documented its own safety with: *"Everything dropped is still recorded — the memo body and the `hint`
row carry the full `recommended_directions` list — so nothing is LOST here"*. The hint half was
false: the append built `DEEP_RESEARCH_HINT_PREFIX + "; ".join(directions[:5])`.

MEASURED on `runs/e5small-dr-unified-v7`, whose third memo was the only one of three with any
content: 8 `recommended_directions`, so directions 6-8 reached the hint not at all. Not a data loss —
the durable `research_completed` row carries all 8 and `read_research_memo` renders directions in
full — but "nothing is lost" is exactly the claim someone leans on when deciding a drop is safe, and
it named the wrong carrier.

THE BOUND IS DELIBERATE AND IS NOT RAISED HERE. The hint is spliced into a PROMPT and `agents/hints.py`
filters on its prefix; a push that grows with whatever the model returned is how a brief becomes the
wall of text this repo keeps trimming. The remedy for a reader that wants all of them is
`read_research_memo`, not a bigger push. So the fix is: say what is true, and make the bound a RULE
(`deep_research_hint_text` + `DEEP_RESEARCH_HINT_DIRECTIONS`) instead of a slice buried in an append,
so a `CLAIM[…]` pin can hold the sentence to the code.

MUTATIONS are named per assertion.
"""
from __future__ import annotations

from looplab.agents.hints import DEEP_RESEARCH_HINT_PREFIX
from looplab.engine.research_cadence import (DEEP_RESEARCH_HINT_DIRECTIONS,
                                             deep_research_hint_text)

_EIGHT = [f"direction-{n}" for n in range(1, 9)]


def test_the_hint_carries_five_of_eight_and_says_so_by_construction():
    """The v7 shape: 8 directions in, 5 in the pushed text."""
    text = deep_research_hint_text(_EIGHT)

    for kept in _EIGHT[:5]:
        assert kept in text
    for dropped in _EIGHT[5:]:
        assert dropped not in text, (
            f"MUTATION: raise DEEP_RESEARCH_HINT_DIRECTIONS and {dropped!r} appears — which is fine "
            "as a decision, but then the CLAIM pin and the docstring must move WITH it, which is the "
            "whole point of pinning the number instead of burying a slice in an append")
    assert text.count("direction-") == 5


def test_the_prefix_is_never_spelled_separately():
    """`agents/hints.py` FILTERS on this prefix — a row whose `source` predates that field is
    recognised by this text alone, so the two must not drift."""
    assert deep_research_hint_text(["a"]).startswith(DEEP_RESEARCH_HINT_PREFIX), (
        "MUTATION: inline a copy of the prefix string here or there and a deep-research hint stops "
        "being recognised as one")


def test_the_text_is_byte_identical_to_the_slice_it_replaced():
    """REGRESSION: this is a PROMPT. The hoist must change no byte of it."""
    for sample in ([], ["only"], _EIGHT[:5], _EIGHT):
        assert deep_research_hint_text(sample) == (
            DEEP_RESEARCH_HINT_PREFIX + "; ".join(sample[:5])), (
            "the pre-hoist expression, reproduced literally — a prompt contract is not refactorable")


def test_fewer_than_the_bound_are_all_carried():
    """REGRESSION: the bound is a ceiling, not a quota."""
    text = deep_research_hint_text(_EIGHT[:3])
    assert text.count("direction-") == 3
    assert "direction-4" not in text


def test_it_accepts_any_iterable_and_an_empty_one():
    """The call site passes a list today; a generator must not silently yield an empty hint."""
    assert deep_research_hint_text(iter(_EIGHT)).count("direction-") == 5, (
        "MUTATION: drop the `list(...)` and a generator is consumed by the slice check before the "
        "join — this is the shape that returns a prefix and nothing else")
    assert deep_research_hint_text([]) == DEEP_RESEARCH_HINT_PREFIX


def test_the_bound_is_the_published_constant():
    """The pin binds the NAME to its value; nothing may read a different five."""
    assert DEEP_RESEARCH_HINT_DIRECTIONS == 5
    assert deep_research_hint_text(_EIGHT).count("direction-") == DEEP_RESEARCH_HINT_DIRECTIONS
