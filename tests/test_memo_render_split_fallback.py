"""A memo that filled only the SPLIT lists is still readable.

MEASURED over the 20 durable research memos on this box (`e5small-dr-unified-v9` preserved +
`e5small-dr-unified-v10` live): 12 carry `next_experiments`, 78 concrete experiments in total, and
**71 of them (91 %) also appear in `recommended_directions`** because the prompt asks the model to
fill that field with the union "so existing readers keep working". The courtesy usually holds.

**It failed exactly once, and that once cost everything it carried**: one memo filled 7 concrete
experiments with an EMPTY `recommended_directions`. `read_research_memo` — the ONE reader an agent
can call — built its `directions` page from the compat field alone, so that memo's entire concrete
half rendered as nothing. Those 7 are the whole of the 9 % that go missing.

The fix is the READER's fallback, not a new demand on the model, and it is the mirror image of
`research_cadence.py`'s existing `questions = open_questions or directions`.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import inspect
import pathlib

from looplab.tools import run_tools

# `encoding=` is load-bearing: `run_tools.py` carries em-dashes, and a bare `read_text()` decodes
# with the LOCALE codec — cp125x on the Windows CI shards, where this module then dies at COLLECTION
# (`UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f`) and takes its whole shard's
# collection down with it. Observed on every pytest-windows shard since this file landed.
_SRC = pathlib.Path(inspect.getsourcefile(run_tools)).read_text(encoding="utf-8")


def _dirs_for(memo: dict) -> list[str]:
    """Re-derive the renderer's `dirs` rule from its own source, over one memo.

    The rule lives inside a long method that needs a bound provider, a run directory and a fold to
    call; this executes the exact lines instead of re-spelling them, so the test cannot drift into
    asserting a rule the module does not have.
    """
    start = _SRC.index('        dirs = [str(d).strip() for d in (m.get("recommended_directions")')
    end = _SRC.index("        # THE VIEW'S OWN population rule", start)
    block = "\n".join(line[8:] for line in _SRC[start:end].splitlines())
    scope: dict = {"m": memo}
    exec(compile(block, "<dirs-rule>", "exec"), scope)
    return scope["dirs"]


def test_the_union_is_used_UNCHANGED_when_the_model_filled_it():
    """19 of the 20 memos on this box are this case. Mutation: always merge all three lists, and
    every compliant memo renders each entry twice — the model already united them."""
    memo = {"recommended_directions": ["a", "b"], "open_questions": ["a"], "next_experiments": ["b"]}
    assert _dirs_for(memo) == ["a", "b"]


def test_an_EMPTY_union_falls_back_to_the_split_lists():
    """The observed failure, as an assertion: 7 concrete experiments, no compat field, nothing
    rendered. Mutation: drop the fallback and this memo's whole concrete half is unreadable by the
    one tool an agent has for it."""
    memo = {"recommended_directions": [],
            "open_questions": ["does a stronger teacher help"],
            "next_experiments": ["set loss.temperature to 0.01", "raise n_negatives to 10"]}
    assert _dirs_for(memo) == ["does a stronger teacher help",
                               "set loss.temperature to 0.01",
                               "raise n_negatives to 10"]


def test_questions_come_FIRST_in_the_fallback():
    """Order is the memo's own: a family of experiments frames the concrete ones under it. Mutation:
    concatenate the other way and the page opens with single edits whose motivation follows them."""
    memo = {"recommended_directions": [], "open_questions": ["Q"], "next_experiments": ["E"]}
    assert _dirs_for(memo) == ["Q", "E"]


def test_a_memo_with_NOTHING_to_say_still_renders_nothing():
    """Mutation: substitute findings or the summary when both lists are empty, and the page invents
    directions the memo never proposed."""
    assert _dirs_for({"recommended_directions": [], "open_questions": [], "next_experiments": []}) == []
    assert _dirs_for({}) == []


def test_blank_and_non_string_entries_are_dropped_on_BOTH_paths():
    """The compat path already stripped these; the fallback must not be the lax one. Mutation: skip
    the strip/filter on the fallback and a blank line renders as an empty bullet."""
    memo = {"recommended_directions": [], "open_questions": ["  ", "Q"], "next_experiments": ["", "E "]}
    assert _dirs_for(memo) == ["Q", "E"]


def test_the_fallback_is_only_a_FALLBACK_not_a_merge():
    """The distinction that keeps 19 of 20 memos byte-identical. Mutation: `dirs = dirs + split`,
    and a compliant memo's every entry appears twice on the page."""
    memo = {"recommended_directions": ["u"], "open_questions": ["u"], "next_experiments": ["u"]}
    assert _dirs_for(memo) == ["u"]
