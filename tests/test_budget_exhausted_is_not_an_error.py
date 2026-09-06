"""Reaching the operator's spend ceiling is the designed end of a budgeted run, not a crash.

Measured on the 2026-08-24 campaign: ALL ELEVEN `run_finished` rows under `runs-B` carry
`reason: "error"`, and every single one is the spend ceiling -- zero genuine failures. A reader
keying on the class cannot tell a healthy budgeted finish from a crash, and the campaign driver had
to learn the difference from an exit code instead.

The naive repair -- "just use a distinct reason" -- would have broken the finalization protocol,
and that is the second thing these tests pin. `reason == "error"` never meant "crashed": it means
"this terminal event was written by the guarded-abort path rather than by the engine's clean
finish", and six protocol sites key on it. Introducing a new reason without teaching them would
have made a guarded abort look like a clean engine finish.
"""
from __future__ import annotations

import pytest

from looplab.cli.run_cmds import _budget_leaf
from looplab.core.llm import BudgetExceeded
from looplab.events.finalize_scope import GUARDED_ABORT_REASONS, is_guarded_abort


def test_the_ceiling_is_still_the_guarded_abort_class():
    """The protocol half. If this goes red, a budgeted finish is being treated as a CLEAN engine
    finish and finalization will acknowledge a terminal intent that is not its own."""
    assert is_guarded_abort("error")
    assert is_guarded_abort("budget_exhausted")
    assert "budget_exhausted" in GUARDED_ABORT_REASONS
    for clean in ("complete", "converged", "max_nodes", None, ""):
        assert not is_guarded_abort(clean), clean


# Every private spelling of "was this finish written by the guarded path?" that a decision site has
# ever used. Both the `run_finished.reason` and the folded `stop_reason` forms, in both polarities:
# docs/57 (`guarded-abort-class-has-six-private-spellings`) found that the two-file scan this used
# to be had missed TEN older sites, so a ceiling-ended run read as a clean finish for `_enter_run`'s
# abort republish and its `entry_finished` gate, `classify_prior_run`, `_decide_run_abort`, two
# `run_commands` gates, `appstate.phase`, the observation pair `run_commands` reads — and the FOLD's
# own crash-prefix clause in `replay.py::_on_run_finished`, which only this scan found. A NEGATIVE
# pin stays a substring on purpose (CLAUDE.md): what must not come back is the TEXT.
_BANNED_LITERAL_SPELLINGS = (
    'reason") or "").lower() == "error"',
    'reason") or "").lower() != "error"',
    'stop_reason or "").lower() == "error"',
    'stop_reason or "").lower() != "error"',
    '.get("reason") == "error"',
    '.get("reason") != "error"',
)


def test_every_protocol_site_uses_the_predicate_not_a_literal():
    """A site added later with a bare `== "error"` would silently exclude the ceiling. The scan is
    the whole `looplab/` tree, not a file list: the file list is how eight sites went unseen."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "looplab"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for banned in _BANNED_LITERAL_SPELLINGS:
            if banned in src:
                offenders.append(f"{path.relative_to(root.parent)}: {banned}")
    assert not offenders, (
        "decision sites still keying on the literal instead of `is_guarded_abort`:\n  "
        + "\n  ".join(offenders))


def test_the_ban_list_would_have_caught_the_ten_sites():
    """The scan is only as good as its spellings. Each shape the eight sites actually used must be
    matched, or widening the file list bought nothing."""
    for historical in (
        'str(state.stop_reason or "").lower() == "error"',          # orchestrator, run_commands
        'str(prior.stop_reason or "").lower() == "error"',          # cli/run_cmds
        'str(state.stop_reason or "").lower() != "error"',          # control_validation
        'str(st.stop_reason or "").lower() == "error"',             # serve/appstate
        '(event.data or {}).get("reason") == "error"',              # command_observation
        'str((event.data or {}).get("reason") or "").lower() != "error"',
        'if d.get("reason") != "error":',                            # events/replay (the fold)
    ):
        assert any(b in historical for b in _BANNED_LITERAL_SPELLINGS), historical


# ------------------------------------------------------------------ the eight sites, DRIVEN.
# Each takes the ceiling's own reason and must answer exactly as it answers `error`; the value
# of the pin above is that it cannot be satisfied by a comment, the value of these is that they
# say which decision a literal would have flipped.

def _stopped(reason: str, *, finished: bool = True):
    import types
    return types.SimpleNamespace(stop_requested=True, finished=finished, stop_reason=reason,
                                 paused=False, finalization_pending=lambda: False)


@pytest.mark.parametrize("reason", GUARDED_ABORT_REASONS)
def test_classify_prior_run_keeps_a_guarded_finish_pending(reason):
    from looplab.cli.run_cmds import classify_prior_run

    assert classify_prior_run(_stopped(reason), []) == "pending_finalize"
    assert classify_prior_run(_stopped("complete"), []) == "finished"


@pytest.mark.parametrize("reason", GUARDED_ABORT_REASONS)
def test_an_operator_abort_on_a_guarded_finish_is_appended_not_a_noop(reason):
    from looplab.serve.control_validation import _decide_run_abort

    decision, _ = _decide_run_abort(None, None, "run_abort", _stopped(reason), False, False)
    assert decision == "append"
    decision, _ = _decide_run_abort(None, None, "run_abort", _stopped("complete"), False, False)
    assert decision == "noop"


@pytest.mark.parametrize("reason", GUARDED_ABORT_REASONS)
def test_the_run_phase_of_a_guarded_finish_is_still_finalizing(reason):
    from looplab.serve.appstate import AppState
    from looplab.serve.protocol import PHASE_FINALIZING, PHASE_FINISHED

    assert AppState.phase(None, _stopped(reason)) == PHASE_FINALIZING
    assert AppState.phase(None, _stopped("complete")) == PHASE_FINISHED


@pytest.mark.parametrize("reason", GUARDED_ABORT_REASONS)
def test_the_command_observation_reads_a_guarded_finish_as_a_domain_failure(reason):
    """The observation is what `run_commands` reads its own answers off, so it must name a finish
    exactly as the service does: a guarded finish is a failure to retry, never a clean one."""
    import types

    from looplab.core.models import Event
    from looplab.events.types import EV_RUN_FINISHED
    from looplab.serve.command_observation import CommandObservation

    guarded = Event(seq=5, type=EV_RUN_FINISHED, data={"reason": reason, "error": "x"})
    clean = Event(seq=6, type=EV_RUN_FINISHED, data={"reason": "complete"})
    obs = types.SimpleNamespace(_run_finishes=[guarded, clean])

    failure = CommandObservation.domain_failure_after(obs, 0)
    assert failure is not None and failure.seq == 5
    assert CommandObservation.domain_failure_after(obs, 5) is None
    assert CommandObservation.has_non_error_finish_after(obs, 0)
    obs_only_guarded = types.SimpleNamespace(_run_finishes=[guarded])
    assert not CommandObservation.has_non_error_finish_after(obs_only_guarded, 0), (
        "a guarded finish was counted as the clean one a finalize command waits for")


@pytest.mark.parametrize("reason", GUARDED_ABORT_REASONS)
def test_the_fold_keeps_a_crash_prefix_across_a_guarded_finish(reason):
    """The ninth site, in `replay.py::_on_run_finished` — found by the tree-wide scan and not by
    the review that counted six. A guarded finish is written from a mid-build exception, so the
    in-flight build marker is what resume recovery reads to append the missing `node_failed`; the
    fold retained it for `error` and, keyed on the literal, cleared it for the ceiling."""
    from looplab.core.models import Event
    from looplab.events.replay import fold

    def _log(final_reason: str):
        return [
            Event(type="run_started", data={"run_id": "r", "task_id": "t"}),
            Event(type="node_building", data={"node_id": 3, "operator": "draft",
                                              "parent_ids": []}),
            Event(type="run_finished", data={"reason": final_reason, "error": "x"}),
        ]

    assert 3 in fold(_log(reason)).buildings, "the guarded finish dropped the crash prefix"
    assert 3 not in fold(_log("complete")).buildings, "a clean finish must still clear it"


def test_a_bare_budget_exception_is_recognised():
    assert _budget_leaf(BudgetExceeded("spend ceiling reached: $1.0007")) is not None
    assert _budget_leaf(RuntimeError("something else")) is None
    assert _budget_leaf(None) is None


def test_a_ceiling_hit_inside_a_task_group_is_still_recognised():
    """The case the old code could not see at all.

    `_run_engine_guarded`'s own docstring records it: anything raised inside the eval task group
    escapes as the GROUP's "unhandled errors in a TaskGroup (1 sub-exception)" and the leaf message
    never reaches the event. So a ceiling hit on the concurrent path was recorded with neither its
    class nor its sentence.
    """
    leaf = BudgetExceeded("spend ceiling reached: $1.0107")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [leaf])
    assert _budget_leaf(group) is leaf

    nested = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [leaf])])
    assert _budget_leaf(nested) is leaf


def test_it_is_found_through_a_cause_chain_but_cannot_hang_on_a_cycle():
    leaf = BudgetExceeded("spend ceiling reached: $1.00")
    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = leaf
    assert _budget_leaf(wrapper) is leaf

    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _budget_leaf(a) is None          # bounded depth, not a hang


def test_an_ordinary_crash_keeps_saying_error():
    """The falsifier for a change that relabels every failure as a budget stop."""
    assert _budget_leaf(ValueError("solver blew up")) is None
    grp = BaseExceptionGroup("boom", [ValueError("x"), KeyError("y")])
    assert _budget_leaf(grp) is None
