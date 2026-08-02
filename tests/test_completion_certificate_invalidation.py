"""Every candidate-set change retires the completion certificates (doc 25 EV-03).

A confirmation and an approval are statements about a SPECIFIC set of candidates: "these were
re-measured and this one won", "the operator ratified this one". Five fold handlers change that set
— a new node, a tombstone, a reset, an abort, and a resume that reopens a finished run — and each
carried its own verbatim copy of the seven-line retraction.

The pair that matters is `st.confirmed_done` (the FOLDED flag that lets the confirm phase re-run)
and `ctx.best_confirmed` (the THREADED snapshot `_select_best`'s confirm-override reads). Clearing
only the flag leaves the override live, and the reopen site shipped exactly that: an epoch-(N-1)
certificate kept beating epoch-N's metric winner. The bug was fixed in that one copy; nothing stopped
the next copy from being written the same way — which is what a shared helper does stop.

The certificate below deliberately names the metric LOSER. That makes the confirm-override
OBSERVABLE in `best_node_id`: while the certificate stands, selection returns the confirmed node; the
moment it is retired, selection falls back to the metric winner. A test that confirmed the metric
winner would pass identically whether the override was cleared or not.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from looplab.events import replay as R
from looplab.events.replay import fold
from looplab.events.types import (
    EV_APPROVAL_GRANTED, EV_APPROVAL_REQUESTED, EV_BEST_CONFIRMED, EV_NODE_ABORT, EV_NODE_CREATED,
    EV_NODE_EVALUATED, EV_NODE_RESET, EV_NODE_TOMBSTONED, EV_RESUME, EV_RUN_FINISHED,
    EV_RUN_STARTED,
)

METRIC_WINNER = 1        # best metric under direction=min
CONFIRMED = 2            # the certificate's subject: a WORSE metric, so the override shows up
COMPETITOR = 3           # the node each candidate-set change touches, so the winner/subject survive


def _event(seq, type_, data):
    from looplab.core.models import Event
    return Event(seq=seq, ts=float(seq), type=type_, data=data)


def _node(seq, node_id, code):
    return _event(seq, EV_NODE_CREATED, {
        "node_id": node_id, "parent_ids": [] if node_id == METRIC_WINNER else [METRIC_WINNER],
        "operator": "draft", "idea": {"operator": "draft", "params": {}}, "code": code})


def _certified_log():
    """Three evaluated candidates, a confirmation naming the metric loser, and an approval on it."""
    return [
        _event(0, EV_RUN_STARTED, {"goal": "g", "direction": "min", "settings": {}}),
        _node(1, METRIC_WINNER, "a"),
        _event(2, EV_NODE_EVALUATED, {"node_id": METRIC_WINNER, "metric": 1.0}),
        _node(3, CONFIRMED, "b"),
        _event(4, EV_NODE_EVALUATED, {"node_id": CONFIRMED, "metric": 2.0}),
        _node(5, COMPETITOR, "c"),
        _event(6, EV_NODE_EVALUATED, {"node_id": COMPETITOR, "metric": 3.0}),
        # `best_confirmed` is the certificate: it sets BOTH `st.confirmed_done` and the threaded
        # `ctx.best_confirmed`. (`confirm_done` is a different event — the fulfillment gate for a
        # forced-confirm request — and does not certify the search.)
        _event(7, EV_BEST_CONFIRMED, {"node_id": CONFIRMED, "mean": 2.0, "n": 3,
                                      "generations": {METRIC_WINNER: 0, CONFIRMED: 0,
                                                      COMPETITOR: 0}}),
        _event(8, EV_APPROVAL_REQUESTED, {"node_id": CONFIRMED, "generation": 0}),
        _event(9, EV_APPROVAL_GRANTED, {"node_id": CONFIRMED, "generation": 0}),
    ]


CERTIFICATE_FIELDS = ("confirmed_done", "approved", "awaiting_approval",
                      "approval_subject", "approval_generation", "approved_node_id")

# Each entry changes the candidate set WITHOUT touching the winner or the certificate's subject, so
# any surviving override is the stale certificate rather than the node simply becoming ineligible.
CANDIDATE_SET_CHANGES = [
    ("new candidate", [_node(10, 4, "d")]),
    ("tombstone", [_event(10, EV_NODE_TOMBSTONED, {"node_ids": [COMPETITOR]})]),
    ("reset", [_event(10, EV_NODE_RESET, {"node_id": COMPETITOR, "from_stage": "eval",
                                          "generation": 0})]),
    ("abort", [_event(10, EV_NODE_ABORT, {"node_id": COMPETITOR, "generation": 0})]),
    ("reopen after finish", [_event(10, EV_RUN_FINISHED, {"reason": "budget"}),
                             _event(11, EV_RESUME, {})]),
]
IDS = [name for name, _extra in CANDIDATE_SET_CHANGES]


def test_the_baseline_actually_holds_a_live_certificate():
    """Without this, every case below could pass vacuously on a run that was never certified."""
    state = fold(_certified_log())
    assert state.confirmed_done is True
    assert state.approved is True and state.approved_node_id == CONFIRMED
    assert state.best_node_id == CONFIRMED, (
        "the confirm-override must be ACTIVE in the baseline, or the cases below cannot tell a "
        "cleared certificate from one that never applied")


@pytest.mark.parametrize("extra", [e for _n, e in CANDIDATE_SET_CHANGES], ids=IDS)
def test_the_threaded_override_dies_with_the_folded_flag(extra):
    """The shipped bug, as a behavioural assertion.

    `ctx.best_confirmed` is not on `RunState` — it is threaded through the fold and read by
    `_select_best`. A handler that clears `confirmed_done` and forgets it leaves selection pinned to
    the previous epoch's winner, and every state field a coarser test would check looks correct."""
    state = fold(_certified_log() + extra)
    assert state.best_node_id == METRIC_WINNER, (
        f"selection stayed on node {state.best_node_id} after the candidate set changed — an "
        "epoch-(N-1) certificate is still overriding the metric winner")


@pytest.mark.parametrize("extra", [e for _n, e in CANDIDATE_SET_CHANGES], ids=IDS)
@pytest.mark.parametrize("field", CERTIFICATE_FIELDS)
def test_each_candidate_set_change_retires_every_certificate_field(extra, field):
    state = fold(_certified_log() + extra)
    value = getattr(state, field)
    assert not value, (
        f"{field}={value!r} survived a candidate-set change: the certificate describes a set that "
        "no longer exists, and nothing else re-derives it")


def test_clearing_approval_takes_the_whole_grant_with_it():
    """A retained `approval_subject` would let a later grant attach to the wrong node."""
    state = fold(_certified_log())
    R._clear_approval(state)
    assert (state.approved, state.awaiting_approval) == (False, False)
    assert state.approval_subject is None
    assert state.approval_generation is None
    assert state.approved_node_id is None


def test_a_finished_run_keeps_its_certificates():
    """The retraction is scoped to an ACTIVE search. A finished run's certificate is its RESULT —
    clearing it on every late event would erase the answer the run exists to produce."""
    finished = fold(_certified_log() + [_event(10, EV_RUN_FINISHED, {"reason": "budget"})])
    assert finished.confirmed_done is True and finished.best_node_id == CONFIRMED
    # ...and a tombstone of an unrelated node after the finish does not retract it either.
    after = fold(_certified_log() + [_event(10, EV_RUN_FINISHED, {"reason": "budget"}),
                                     _event(11, EV_NODE_TOMBSTONED, {"node_ids": [COMPETITOR]})])
    assert after.confirmed_done is True


def _handler_bodies():
    source = Path(R.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, ast.unparse(node)


def test_no_handler_opens_a_sixth_copy():
    """Source guard: the folded flag and the threaded snapshot are cleared together in ONE place.

    `_on_node_tombstoned`'s partial variant is deliberately not matched: it clears the subject half
    and the grant half under two SEPARATE `in affected` conditions, which is a different rule."""
    offenders = [
        name for name, body in _handler_bodies()
        if name not in ("_clear_approval", "_invalidate_completion_certificates")
        and "st.confirmed_done = False" in body and "ctx.best_confirmed = None" in body]
    assert not offenders, (
        "these handlers re-spell the completion-certificate retraction instead of calling "
        f"_invalidate_completion_certificates: {offenders}")


def test_the_source_guard_can_see_a_reintroduced_copy():
    """A scan that matches nothing is indistinguishable from one whose pattern is broken."""
    names = dict(_handler_bodies())
    assert "_invalidate_completion_certificates" in names, "the helper itself vanished"
    body = names["_invalidate_completion_certificates"]
    assert "st.confirmed_done = False" in body and "ctx.best_confirmed = None" in body, (
        "the pattern no longer matches even the definition site, so it would match nothing anywhere")
