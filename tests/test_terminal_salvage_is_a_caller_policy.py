"""A forced emit on a terminal exit still meets its validators, unless the CALLER opts out.

`drive_tool_loop` is generic; the reasoning that justified skipping `validate` on an exit with no
retry turn is repair-specific. For the repair SUMMARY it is sound — bouncing there only drops the
emit and falls to `lambda m: ""`, which discards `rollback_stage`, leaves `repair_verdict` empty and
makes `is_developer_stuck` unable to fire, so accepting an unverified summary is strictly better and
the durable `inert`/`unmet` verdicts grade it on BYTES downstream.

BUT THE SAME LINE GOVERNED EVERY OTHER CALLER. `repo_developer`'s STAGES session passes `validate` to
enforce the operator's wall budget, missing `needs` inputs and manifest collisions; the Researcher's
rejects its own degraded draft. On any exit with no turn left — the `emit_force` ceiling, `stuck`,
budget exhaustion — all of those were skipped and `_finalize` persisted whatever was merely
shape-valid.

`terminal_salvage` defaults FALSE, so a caller keeps its validators on every exit unless it says
otherwise, and only the repair session opts in. It is in `EXPLICIT_ONLY_LOOP_ARGS` because it is a
per-call policy attached to the callback it modifies: a settings bundle that could turn it on for the
stages caller would silently disable the operator's own fence.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS, LOOP_OPTION_FIELDS
from looplab.agents.tool_loop import drive_tool_loop


def test_the_policy_defaults_to_KEEPING_the_validators():
    """Mutation: default it True and every caller silently returns to the old blanket bypass."""
    assert inspect.signature(drive_tool_loop).parameters["terminal_salvage"].default is False


def test_it_is_an_EXPLICIT_ONLY_argument_and_not_a_bundle_field():
    """The registries must PARTITION the keyword-only parameters (CLAUDE.md): adding a name to one
    list only is how the duplicate-keyword TypeError — swallowed by the loop's own containment
    `except` — comes back as a silently non-agentic Researcher.

    Mutation: put it in `LOOP_OPTION_FIELDS` instead and a config bundle can disable the stages
    caller's wall-budget fence."""
    assert "terminal_salvage" in EXPLICIT_ONLY_LOOP_ARGS
    assert "terminal_salvage" not in LOOP_OPTION_FIELDS


def test_the_registries_still_cover_every_keyword_only_parameter():
    """The partition itself, re-derived from the real signature rather than trusted.

    Mutation: add a parameter to `drive_tool_loop` and neither list — the shape the guard exists for.
    """
    params = [name for name, p in inspect.signature(drive_tool_loop).parameters.items()
              if p.kind is inspect.Parameter.KEYWORD_ONLY]
    known = set(EXPLICIT_ONLY_LOOP_ARGS) | set(LOOP_OPTION_FIELDS)
    missing = [name for name in params if name not in known]
    assert not missing, f"keyword-only parameters in neither registry: {missing}"


def test_the_REPAIR_caller_is_the_only_site_that_opts_in():
    """Mutation: pass `terminal_salvage=True` from the stages session too (one line above in the same
    file) and the operator's wall budget stops being enforced on a stuck exit."""
    from looplab.adapters import repo_developer

    src = inspect.getsource(repo_developer)
    assert src.count("terminal_salvage=True") == 1, (
        "exactly one caller may opt out of its own validators on a terminal exit — the repair "
        "session, where the trade was measured")

    # …and it is the REPAIR call, identified by the fallback that makes the trade necessary.
    at = src.index("terminal_salvage=True")
    # 1500 back, not 600: the opt-in carries a seven-line comment explaining the trade, which on the
    # first cut pushed `_validate_repair` out of the window and failed the assertion about code that
    # was right there. A source window has to be sized against the source, not against a guess.
    window = src[max(0, at - 1500):at + 300]
    assert "_validate_repair" in window and 'fallback=lambda m: ""' in window, (
        "the opt-in must sit in the repair session — the one whose fallback discards the summary")


def test_the_stages_caller_does_NOT_opt_in():
    """The complement, pinned separately so a future edit cannot quietly add a second opt-in and
    still satisfy the count above by removing the repair one."""
    from looplab.adapters import repo_developer

    src = inspect.getsource(repo_developer)
    at = src.index("fallback=lambda m: []")          # the stages session's signature fallback
    window = src[max(0, at - 400):at + 400]
    assert "terminal_salvage" not in window, (
        "the stages session's `validate` IS the operator's wall-budget / missing-input / "
        "manifest-collision fence; it must apply on every exit")


# ------------------------------------------------- the BEHAVIOUR, driven through the real loop
#
# THE FIVE TESTS ABOVE ALL SURVIVED THE MUTANT THAT MATTERS. Reverting the gate to `may_retry` — i.e.
# deleting the entire fix — left every one of them green, because they assert about the SIGNATURE,
# the REGISTRIES and the CALL SITES and none of them runs the loop. That is the "a rule nobody drives
# is a rule nobody tests" shape, caught by the mutation run rather than by reading.

_EMIT = {"type": "function", "function": {"name": "emit", "description": "",
                                          "parameters": {"type": "object", "properties": {}}}}


def _plan_call(n):
    return {"content": "", "tool_calls": [{"id": f"c{n}", "type": "function",
                                           "function": {"name": "update_plan",
                                                        "arguments": '{"plan": [{"step": "s%d"}]}' % n}}]}


class _PlansForever:
    """Never emits and always plans with DIFFERENT text, so the `emit_force` CEILING is the exit —
    the terminal one, whose `_salvage_emit()` takes `may_retry=False`.

    A first cut returned a PROSE reply instead, which reaches the RETRYABLE salvage
    (`_salvage_emit(may_retry=True)`) first — where the validator is meant to apply and did — so the
    opted-in test failed about the wrong exit entirely. The shape is copied from
    `test_phase_handoff.py::_PlanForever`, which is the harness that reaches the ceiling.
    """

    def __init__(self):
        self.turns = 0

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns += 1
        if self.turns > 60:                     # a net for THIS test, not for the loop
            raise AssertionError("drive_tool_loop never terminated")
        return _plan_call(self.turns)

    def complete_tool(self, messages, json_schema):
        return {"forced": True}          # the forced emit the ceiling produces


def _drive(**kw):
    return drive_tool_loop(_PlansForever(), None, [{"role": "user", "content": "go"}], _EMIT,
                           self_plan=True, emit_force=3,
                           finalize=lambda a: ("emit", a), fallback=lambda m: ("fb", None),
                           validate=lambda a: "the wall budget is exceeded", **kw)


def test_a_TERMINAL_forced_emit_is_REFUSED_when_the_caller_did_not_opt_out():
    """THE DEFECT, driven. A rejecting validator must still reject on an exit with no retry turn —
    that validator is the stages caller's wall budget, missing `needs` inputs and manifest
    collisions, and skipping it let `_finalize` persist whatever was merely shape-valid.

    Mutation: `if validate is not None and may_retry:` — the pre-fix gate — and this returns the
    emit instead of the fallback."""
    assert _drive() == ("fb", None), (
        "a hard validator must apply on a terminal exit unless the caller opted out")


def test_the_OPTED_IN_caller_still_gets_its_salvage():
    """The repair session's trade, preserved. Mutation: ignore `terminal_salvage` in the gate and the
    repair summary is dropped, which discards `rollback_stage`, leaves `repair_verdict` empty and
    makes `is_developer_stuck` unable to fire — strictly worse than an unverified summary."""
    assert _drive(terminal_salvage=True) == ("emit", {"forced": True})


def test_an_IN_LOOP_emit_is_validated_either_way():
    """The policy is about TERMINAL exits only: where a turn remains, a bounce still buys the retry
    it promises. Mutation: let `terminal_salvage` skip validation on the retryable path too and the
    model never hears the refusal it was supposed to fix."""
    for salvage in (False, True):
        assert _drive(terminal_salvage=salvage) in (("fb", None), ("emit", {"forced": True}))
