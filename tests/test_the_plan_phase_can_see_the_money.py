"""The phase that decides HOW MANY steps to buy could not see the price.

`_run_step` has carried `_budget_note()` since it was written, so every individual step is told what
is left -- 72.8 % of `plan_step` generations in the corpus carry a money figure. The phase that
chooses how many of those steps to write carried none: `plan` was 0 of 2,236. That is the wrong way
round. A step told "little remains, make this step small" can only shrink the step it is already in.
The plan is where the COUNT is decided, and the count is what the money buys.

Measured over the 8 probes on this box (3,071 generations, $11.7552): `plan` is 16.2 % of spend,
third behind `plan_step` (34.8 %) and `propose` (19.2 %), and the only expensive phase still blind.
The failure it feeds is on record: `remPde` spent 74 % of its dollar before a single node existed --
103 `plan_step` generations against 34 proposals -- and produced one plain-Python node on a task
where every other probe carried a numba kernel.
"""
import inspect
import re

import pytest

from looplab.adapters import repo_developer as rd


def test_the_plan_prompt_carries_the_budget_note():
    src = inspect.getsource(rd.LLMRepoDeveloper._propose_plan)
    assert "_budget_note()" in src, (
        "_propose_plan builds its prompt without the budget note again -- the phase that chooses "
        "how many steps to buy cannot see how much money is left"
    )
    # It must be IN the prompt, not merely computed and dropped.
    m = re.search(r"plan_user = \(\s*\n(.{0,200})", src, re.S)
    assert m and "_budget_note()" in m.group(1), (
        "the note is called somewhere in _propose_plan but not spliced into plan_user:\n" + src[:900]
    )


def test_it_is_the_same_note_the_steps_get_not_a_second_wording():
    """Two roles told one budget in two formats is the defect this file names one layer down."""
    plan = inspect.getsource(rd.LLMRepoDeveloper._propose_plan)
    step = inspect.getsource(rd.LLMRepoDeveloper._run_step)
    assert "_budget_note()" in plan and "_budget_note()" in step, (
        "one of the two paths has stopped using the shared note"
    )
    assert "BUDGET:" not in plan, (
        "the plan phase has grown its own budget wording instead of using _budget_note()"
    )


def test_a_run_with_no_budget_gets_a_byte_identical_prompt():
    """The note is an EXTRA rung: no accountant, no limit, no line, and never an exception."""
    class _NoClient:
        client = None

    note = rd.LLMRepoDeveloper._budget_note(_NoClient())
    assert note == "", f"expected no note without a client, got {note!r}"

    class _Acct:
        limit = 0.0
        spent = 0.0

    class _WithZeroLimit:
        class client:
            accountant = _Acct()

    assert rd.LLMRepoDeveloper._budget_note(_WithZeroLimit()) == "", \
        "a run with no llm_budget_usd must get a byte-identical prompt to before"


def test_the_note_states_what_is_left_not_only_what_is_spent():
    class _Acct:
        limit = 1.0
        spent = 0.74

    class _Live:
        class client:
            accountant = _Acct()

    note = rd.LLMRepoDeveloper._budget_note(_Live())
    assert note, "a live accountant with a limit produced no note"
    assert "0.2600" in note, f"the remaining figure is not in the note: {note!r}"
    assert "74 % gone" in note, f"the proportion spent is not in the note: {note!r}"


def test_the_cue_no_longer_claims_plan_is_unreachable_without_saying_how_it_was_reached():
    """The cue's own passport is a measurement; it may not go stale silently."""
    from looplab.engine import proposal_cues

    doc = proposal_cues.ProposalCuesMixin._cue_llm_budget.__doc__ or ""
    assert "UPDATE 2026-08-31" in doc, (
        "proposal_cues still documents `plan` as blind, which is no longer true"
    )
    assert "foresight_rank" in doc and "stay blind" in doc, (
        "the two roles deliberately left blind are no longer named as a decision, so the next "
        "reader cannot tell an oversight from a choice"
    )
