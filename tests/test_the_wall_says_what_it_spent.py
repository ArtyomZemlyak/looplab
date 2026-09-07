"""A session cut by the wall clock recorded the word "time" and nothing else.

Measured 2026-09-01 across 30 probes: TWELVE plan-step sessions were cut, every one of them by the
1200 s wall and not one by the money ceiling `_step_cost_ceiling` installs. The obvious next
question -- how close did the money ceiling come? -- turned out to be unanswerable from the corpus.
The recorded row was `{"step": 4, "cutoff": "time"}`. No seconds, no spend, and no session window
from which the spend could be reconstructed.

So "the money ceiling never fires, the wall fires first" stood for weeks as an assertion nobody
could check, and the two readings it is compatible with call for opposite repairs: a ceiling set so
high it is decorative, or one that is about to bite and has simply lost the race. A bound whose
distance from firing is unobservable cannot be tuned, defended, or removed.

The wall branch now reports the same "$X of $Y for this session" pair the money branch always
carried -- and the money branch is the one that never runs.
"""
from __future__ import annotations

import pytest

from looplab.agents.tool_loop import _note_budget, _spend_detail


class _Acct:
    def __init__(self, spent):
        self.spent = spent


class _Client:
    def __init__(self, spent=None):
        if spent is not None:
            self.accountant = _Acct(spent)


def test_the_detail_names_both_the_spend_and_the_ceiling():
    d = _spend_detail(_Client(0.37), 0.05, 0.25)
    assert "$0.3200" in d, d          # 0.37 - 0.05, the spend of THIS session
    assert "$0.2500" in d, d


def test_a_wall_cut_with_no_money_ceiling_still_reports_the_spend():
    """`cost_budget_usd` is 0 for every session except the plan step, and those sessions hit the
    wall too. Reporting nothing there would leave the commonest cut as silent as before."""
    d = _spend_detail(_Client(0.90), 0.10, 0.0)
    assert "$0.8000" in d, d
    assert "no money ceiling" in d, d


def test_an_unknowable_spend_is_reported_as_nothing_rather_than_zero():
    """No accountant means the spend cannot be known, and 0.0 is a real reading. Conflating them is
    how a money ceiling silently becomes a ceiling on nothing (see `_accountant_spend`)."""
    assert _spend_detail(_Client(), 0.0, 0.25) == ""
    assert _spend_detail(_Client(0.5), None, 0.25) == ""


def test_the_payload_carries_the_detail_through_to_the_observer():
    seen = []
    _note_budget(seen.append, "time", turns=7, seconds=1200.4,
                 detail=_spend_detail(_Client(0.42), 0.02, 0.25))
    assert len(seen) == 1
    p = seen[0]
    assert p["kind"] == "time"
    assert p["seconds"] == 1200.4
    assert "$0.4000 of $0.2500" in p["detail"], p


def test_an_empty_detail_is_omitted_from_the_envelope():
    """The key set is what a reader branches on, so an always-present empty detail would make every
    ordinary cutoff look like it carried one."""
    seen = []
    _note_budget(seen.append, "time", turns=1, seconds=5.0, detail="")
    assert "detail" not in seen[0], seen[0]


def test_the_developer_keeps_the_numbers_and_not_only_the_word():
    """`last_budget_exhausted` is a durable vocabulary other code compares against a KIND, so the
    numbers ride a second attribute rather than widening it into prose."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._note_session_budget({"kind": "time", "turns": 9, "seconds": 1200.1,
                    "detail": "$0.4000 of $0.2500 for this session"})
    assert dev.last_budget_exhausted == "time", "the kind column changed shape"
    facts = dev.last_budget_facts
    assert facts["seconds"] == 1200.1
    assert "$0.4000 of $0.2500" in facts["detail"]


# --- driven through the REAL loop, because the helper alone proves nothing -----------------------
# The first version of this file tested `_spend_detail` and `_note_budget` in isolation and stayed
# GREEN under the exact defect being fixed: replacing the wall branch's `detail=` argument with `""`
# broke nothing, because nothing asserted that the wall branch calls the helper at all. Mutation is
# what found that; unit tests on both ends of a wire do not test the wire.

class _SpendingAcct:
    def __init__(self):
        self.spent = 0.0


class _NeverEmits:
    """A model that keeps calling a tool and never emits, and whose accountant ticks as it goes."""

    def __init__(self, with_accountant=True):
        self.calls = 0
        if with_accountant:
            self.accountant = _SpendingAcct()

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.calls += 1
        if getattr(self, "accountant", None):
            self.accountant.spent += 0.05
        import time as _t
        _t.sleep(0.02)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": "look", "arguments": "{}"}}]}

    def complete_text(self, messages):
        return "text"


class _LookTools:
    def specs(self):
        return [{"type": "function", "function": {"name": "look", "description": "look",
                                                  "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return "looked"


_EMIT = {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}}


def _run(client, **kw):
    from looplab.agents.tool_loop import drive_tool_loop
    seen = {}
    drive_tool_loop(client, _LookTools(), [{"role": "user", "content": "go"}], _EMIT,
                    time_budget_s=0.05, finalize=lambda a: "done",
                    fallback=lambda m: "fell back", on_budget=seen.update, **kw)
    return seen


def test_the_WALL_BRANCH_itself_reports_the_spend():
    """The defect exactly: a session cut by the clock said "time" and no money at all."""
    seen = _run(_NeverEmits())
    assert seen.get("kind") == "time", seen
    assert "detail" in seen, (
        "the wall cut carries no detail, so the corpus records the word and not the number -- this "
        "is the state that made 'the money ceiling never fires' uncheckable for weeks"
    )
    assert "$" in seen["detail"] and "for this session" in seen["detail"], seen["detail"]


def test_the_wall_branch_names_the_ceiling_when_there_is_one():
    seen = _run(_NeverEmits(), cost_budget_usd=999.0)   # high enough that only the wall can fire
    assert seen["kind"] == "time", seen
    assert "of $999.0000" in seen["detail"], seen["detail"]


def test_a_wall_cut_without_an_accountant_still_reports_the_kind():
    """No accountant means no spend to report; the cut must still be announced, and must not
    claim $0.0000."""
    seen = _run(_NeverEmits(with_accountant=False))
    assert seen["kind"] == "time"
    assert "detail" not in seen, seen
