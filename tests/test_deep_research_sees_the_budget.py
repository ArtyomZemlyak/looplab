"""The one stage that can spend the whole budget was the one that could not see it.

Measured over the probe corpus on 2026-08-28, resolving each run's span-input chain: a `BUDGET:`
line appears in **2 of 2,549** `deep_research` generations (0 %), against 84 % for `plan_step`,
85 % for `propose`, 91 % for `plan`, 82 % for `repropose` and 100 % for `strategist_consult`.

That stage is also the only one with neither a turn cap nor a money cap of its own --
`agent_max_turns` and `agent_time_budget_s` both default to 0, meaning unlimited. `opus5` spent
**$1.0204 of a $1.00 run in ten `deep_research` generations, produced ZERO nodes**, and ended at
`finalize_step: abandoned / error_terminal`. Corpus median share for the stage is 12.9 %; opus5's
was 100 %.

The refuter is `test_the_user_turn_leads_with_the_budget`: drop `self._budget_note() +` from the
user turn and it fails.
"""
from looplab.agents.deep_research import DeepResearcher


class _Acct:
    def __init__(self, limit, spent):
        self.limit, self.spent = limit, spent


class _Client:
    def __init__(self, acct):
        self.accountant = acct


def _researcher(acct):
    r = DeepResearcher.__new__(DeepResearcher)
    r.client = _Client(acct) if acct is not None else None
    return r


def test_the_note_states_spent_limit_and_what_is_left():
    note = _researcher(_Acct(1.0, 0.25))._budget_note()
    assert "0.2500" in note and "1.0000" in note and "0.7500" in note
    assert "25 % gone" in note


def test_the_note_says_why_it_matters_to_THIS_stage():
    # A bare number is what `plan_step` gets; this stage needs the consequence, because nothing
    # else stops it -- no turn cap, no money cap. opus5 spent the entire run here.
    note = _researcher(_Acct(1.0, 0.9))._budget_note()
    assert "leaves no money for experiments" in note


def test_a_run_with_no_budget_gets_a_byte_identical_prompt():
    for acct in (None, _Acct(0.0, 0.0), _Acct(float("inf"), 0.0), _Acct("nonsense", 0.0)):
        assert _researcher(acct)._budget_note() == "", (
            "an unbudgeted run must see exactly the prompt it saw before this change")


def test_a_missing_accountant_never_raises():
    class _Bare:
        pass
    r = DeepResearcher.__new__(DeepResearcher)
    r.client = _Bare()
    assert r._budget_note() == ""


def test_the_user_turn_leads_with_the_budget():
    import inspect
    src = inspect.getsource(DeepResearcher.research)
    assert "self._budget_note() + state_brief(state)" in src, (
        "the note must LEAD the user turn; appended after the brief it competes with the board "
        "rows the brief already fills to its character budget")


# ---------------------------------------------------------------- the note must MOVE inside a session

def test_the_loop_is_handed_a_live_callable_not_a_rendered_string():
    """Measured on dsBN, 2026-08-28: the user-turn note read "$0.0000 of $1.0000 spent" for all
    SEVEN generations of the first research session and "$0.3210" for all four of the second,
    because a prompt built at session start is replayed every turn. `plan_step` behaves the same
    ($0.0935 eight times running). opus5 spent its entire $1.0204 inside ONE session, so a
    session-start figure would have read $0.0000 for all ten of its generations.

    The refuter: pass `self._budget_note()` (a string, rendered once) instead of
    `self._budget_note` (the callable) and this fails.
    """
    import inspect
    src = inspect.getsource(DeepResearcher.research)
    assert "budget_note=self._budget_note," in src
    assert "budget_note=self._budget_note()" not in src, (
        "a rendered string freezes at session start, which is the defect this fixes")


def test_the_shared_loop_injects_only_when_the_figure_changes():
    from looplab.agents import tool_loop
    import inspect
    src = inspect.getsource(tool_loop.drive_tool_loop)
    assert '_note != _last_budget_note[0]' in src, (
        "re-injecting an unchanged note every turn is noise the model pays for")
    assert '"role": "user", "content": "Reminder — " + _note.strip()' in src, (
        "system authority belongs to instructions, not to a spend reminder")


def test_a_caller_that_passes_no_callable_is_untouched():
    from looplab.agents import tool_loop
    import inspect
    sig = inspect.signature(tool_loop.drive_tool_loop)
    assert sig.parameters["budget_note"].default is None, (
        "every existing caller must keep a byte-identical message list")
