"""The memo prompt promises what the engine actually registers.

The board block told the model "Your `recommended_directions` are registered as OPEN BELIEFS" — the
legacy UNION field. `research_cadence.py` registers `questions`, which is `open_questions` when the
memo filled it and the union only as a fallback, so every `next_experiments` entry riding the union
was promised a board row it never gets. Prompt strings are contracts (CLAUDE.md).

MEASURED on the live `e5small-dr-unified-v11`: its three non-empty memos all drew the split —
`open_questions` 4/3/2, `next_experiments` 6/8/5, and `recommended_directions` exactly their sum
(10/11/7). So the promise was false about 19 of 28 entries, and a model told the union is registered
has less reason to route a broad question into the channel the split was measured on v5 to need.

These tests pin the PROMISE against the REGISTRATION, so the two cannot drift apart again — which is
how they drifted in the first place, the split landing on one side only.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

import inspect

import pytest


def _board_block(state) -> str:
    from looplab.agents import deep_research

    return "\n".join(deep_research.state_brief(state).splitlines())


def test_the_promise_names_open_questions_and_not_the_union():
    """Mutation: restore "Your `recommended_directions` are registered as OPEN BELIEFS" and the
    model is told its concrete experiments become board rows, which they never do."""
    from looplab.agents import deep_research

    src = inspect.getsource(deep_research)
    assert "Your `open_questions` are registered as OPEN BELIEFS" in src
    assert "Your `recommended_directions` are registered as OPEN BELIEFS" not in src, (
        "the union is registered only as a FALLBACK; naming it unconditionally is the false promise")


def test_the_prompt_says_what_next_experiments_ARE_instead_of_leaving_it_to_inference():
    """Deleting the false half is not enough — the model still has to know where its concrete work
    goes, or it may route everything into `open_questions` to get a row.

    Mutation: drop the `next_experiments` sentence and the correction becomes a pure deletion."""
    from looplab.agents import deep_research

    src = inspect.getsource(deep_research)
    assert "`next_experiments` are NOT board rows" in src


def test_the_FALLBACK_is_stated_because_the_engine_really_has_one():
    """`research_cadence` uses `open_questions ... or directions`, so a memo that draws no split DOES
    get its union registered. Saying only the first half would be a new, opposite falsehood.

    Mutation: drop the parenthetical and the promise is wrong for an unsplit memo."""
    from looplab.agents import deep_research

    src = inspect.getsource(deep_research)
    assert "no question/experiment split" in src and "recommended_directions` list is registered" in src


def test_the_engine_really_prefers_the_split_list():
    """The other side of the contract, read from the registration itself rather than assumed — if
    this ever flips, the prompt above becomes false again and this test is what says so.

    Mutation: change the registration to `directions` and the promise no longer matches the code.
    """
    from looplab.engine import research_cadence

    src = inspect.getsource(research_cadence)
    assert 'memo_d.get("open_questions", [])' in src and "or directions" in src, (
        "the board registration must still prefer the split list, with the union as fallback")


def test_the_dedup_and_cap_warning_survived_the_rewording():
    """It was true before and is true of whatever is registered; losing it in an edit would drop a
    real constraint the engine enforces at the append site.

    Mutation: delete the cap clause and the model is never told its duplicate is dropped."""
    from looplab.agents import deep_research

    src = inspect.getsource(deep_research)
    assert "duplicates an open belief" in src and "past its cap" in src
    assert "retiring a belief is the operator's call" in src
