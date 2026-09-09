"""A paid evaluation carries an attempt-scoped receipt (doc 27).

THE DEFECT, as the mega-review filed it: `node_eval_started` carries `node_id` + `generation` and
nothing else — one row per NODE LIFECYCLE, no attempt, no invocation id, and no completed receipt.
So the boundary the evaluator actually crosses had no record at all: an evaluation may finish paid
or external side effects (a training run, a submission, a remote job) minutes before its terminal
event is appended, and a process death in that gap leaves the node byte-indistinguishable from one
whose evaluator never ran. The resumed process re-invokes it as if for the first time.

The claim/settle pair is that record, and the id is DERIVED so the resumed process can name the
invocation it is repeating rather than mint an unrelated key. Nothing here claims the side effect was
undone — LoopLab cannot make an arbitrary evaluator transactional — the repeat is STAMPED instead,
the way `_ensure_run_setup` already stamps its own at-least-once repeat.

The last two tests DRIVE it: a real `Engine`, a real event log, a death inside the evaluator, and a
second `Engine` over the same run directory that reads the receipt the dead one left.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.models import Event, Idea, NodeStatus
from looplab.engine.evaluate import (EVAL_INVOCATION_OUTCOMES, eval_invocation_id,
                                     eval_invocation_outcome, unsettled_eval_invocations)
from looplab.events.replay import fold
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_EVAL_INVOCATION_CLAIMED,
                                  EV_EVAL_INVOCATION_SETTLED)
from looplab.runtime.sandbox import RunResult
from tests.factories import make_engine


class _Kill(BaseException):
    """A BaseException, so `_evaluate`'s containment cannot absorb it: the closest a test gets to
    the process dying with the evaluator mid-flight."""


def _row(seq, kind, invocation_id, *, node_id=0, generation=0, attempt=0):
    return Event(v=1, seq=seq, ts=1000.0 + seq, type=kind,
                 data={"node_id": node_id, "generation": generation, "attempt": attempt,
                       "invocation_id": invocation_id})


def _engine(run_dir, code="print(1)"):
    engine = make_engine(run_dir, max_nodes=2)
    engine.concurrent_research = False
    engine.store.append("run_started",
                        {"run_id": "receipt", "task_id": "toy", "direction": "min"})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": Idea(operator="draft").model_dump(mode="json"), "code": code})
    return engine


def _drive(engine):
    anyio.run(lambda: engine._evaluate(0, anyio.CapacityLimiter(1), None))


def _rows(engine, kind):
    return [e.data for e in engine.store.read_all() if e.type == kind]


# ------------------------------------------------------------------------- the rules


def test_the_id_names_the_lifecycle_and_the_attempt_and_reproduces_itself():
    """DERIVED, not minted: the resumed process must re-derive the key its dead predecessor used."""
    assert eval_invocation_id("uid-1", 3, 0, 2) == eval_invocation_id("uid-1", 3, 0, 2)
    ids = {eval_invocation_id("uid-1", 3, 0, 2), eval_invocation_id("uid-1", 3, 0, 3),
           eval_invocation_id("uid-1", 3, 1, 2), eval_invocation_id("uid-1", 4, 0, 2),
           eval_invocation_id("uid-2", 3, 0, 2)}
    assert len(ids) == 5, "attempt, generation, node and run each have to separate two invocations"


def test_an_invocation_is_open_when_its_last_row_is_a_claim():
    open_id = eval_invocation_id("uid", 0, 0, 0)
    assert unsettled_eval_invocations([_row(0, EV_EVAL_INVOCATION_CLAIMED, open_id)], 0, 0) == {open_id}
    settled = [_row(0, EV_EVAL_INVOCATION_CLAIMED, open_id),
               _row(1, EV_EVAL_INVOCATION_SETTLED, open_id)]
    assert unsettled_eval_invocations(settled, 0, 0) == frozenset()
    # …and it returns to closed after an interrupted invocation is re-made and settled. Counting
    # claims against settles cannot: the dead invocation has no settle and never will, so every
    # later attempt of that key would be stamped on the strength of a crash two resumes ago.
    reclaimed = settled[:1] + [_row(1, EV_EVAL_INVOCATION_CLAIMED, open_id),
                               _row(2, EV_EVAL_INVOCATION_SETTLED, open_id)]
    assert unsettled_eval_invocations(reclaimed, 0, 0) == frozenset()


def test_a_row_from_another_lifecycle_never_stamps_this_one():
    other = eval_invocation_id("uid", 0, 1, 0)
    rows = [_row(0, EV_EVAL_INVOCATION_CLAIMED, other, generation=1)]
    assert unsettled_eval_invocations(rows, 0, 0) == frozenset()
    assert unsettled_eval_invocations(rows, 0, 1) == {other}


def test_an_intervention_is_not_recorded_as_a_failure():
    """`superseded`/`aborted` say the invocation was CUT; `failed` says the candidate was bad."""
    assert eval_invocation_outcome(True, False, False) == "superseded"
    assert eval_invocation_outcome(True, True, True) == "superseded"     # order is the content
    assert eval_invocation_outcome(False, True, False) == "aborted"
    assert eval_invocation_outcome(False, False, True) == "ok"
    assert eval_invocation_outcome(False, False, False) == "failed"
    assert set(eval_invocation_outcome(s, a, o) for s in (0, 1) for a in (0, 1)
               for o in (0, 1)) <= EVAL_INVOCATION_OUTCOMES


def test_the_receipt_is_diagnostic_so_no_reader_keys_on_its_position():
    """Per-ATTEMPT rows from the eval child. Folded, they would land inside the speculative
    election's compare-and-swap window — the measured cost `_record_eval_start_boundary` documents."""
    assert EV_EVAL_INVOCATION_CLAIMED in DIAGNOSTIC_EVENTS
    assert EV_EVAL_INVOCATION_SETTLED in DIAGNOSTIC_EVENTS


# ------------------------------------------------------------- driven over a real Engine


def test_one_evaluation_brackets_its_evaluator_with_a_claim_and_a_settle(tmp_path):
    engine = _engine(tmp_path / "settled")
    engine._run_eval = lambda *_a, **_kw: RunResult(
        exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)
    _drive(engine)

    claims, settles = _rows(engine, EV_EVAL_INVOCATION_CLAIMED), _rows(engine, EV_EVAL_INVOCATION_SETTLED)
    assert len(claims) == 1 and len(settles) == 1
    assert claims[0]["invocation_id"] == settles[0]["invocation_id"]
    assert claims[0]["attempt"] == 0 and "after_interrupted_attempt" not in claims[0]
    assert settles[0]["outcome"] == "ok" and settles[0]["eval_seconds"] >= 0.0
    # The receipt is EVIDENCE about an invocation, never a second authority for the node: exactly
    # one terminal, exactly as before (invariant #2).
    terminals = [e for e in engine.store.read_all()
                 if e.type in ("node_evaluated", "node_failed")]
    assert len(terminals) == 1
    assert unsettled_eval_invocations(engine.store.read_all(), 0, 0) == frozenset()


def test_a_death_inside_the_evaluator_leaves_a_claim_the_next_process_reads(tmp_path):
    """THE DEFECT, end to end. MUTATION: drop the claim -> the second process cannot tell that the
    evaluator already ran once, which is exactly the state the item describes."""
    run_dir = tmp_path / "resumed"
    dead = _engine(run_dir)

    def _die(*_a, **_kw):
        raise _Kill("the box went down while the evaluator was running")

    dead._run_eval = _die
    with pytest.raises(BaseException):
        _drive(dead)

    events = dead.store.read_all()
    assert len(_rows(dead, EV_EVAL_INVOCATION_CLAIMED)) == 1
    assert _rows(dead, EV_EVAL_INVOCATION_SETTLED) == [], "a death cannot settle anything"
    assert not [e for e in events if e.type in ("node_evaluated", "node_failed")], (
        "the gap this receipt exists for: paid work, no terminal")
    assert fold(events).nodes[0].status is NodeStatus.pending
    open_ids = unsettled_eval_invocations(events, 0, 0)
    assert len(open_ids) == 1

    # A second process over the SAME run directory re-runs the same attempt of the same lifecycle.
    resumed = make_engine(run_dir, max_nodes=2)
    resumed.concurrent_research = False
    resumed._run_eval = lambda *_a, **_kw: RunResult(
        exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)
    _drive(resumed)

    claims = _rows(resumed, EV_EVAL_INVOCATION_CLAIMED)
    assert len(claims) == 2, "the resumed attempt claims its invocation like any other"
    assert claims[1]["invocation_id"] == claims[0]["invocation_id"], (
        "the id is derived, so the resumed process names the invocation it is repeating")
    assert claims[1]["invocation_id"] in open_ids
    assert claims[1]["after_interrupted_attempt"] is True, (
        "an at-least-once repeat is stamped, not presented as a first attempt")
    settles = _rows(resumed, EV_EVAL_INVOCATION_SETTLED)
    assert len(settles) == 1 and settles[0]["outcome"] == "ok"
    assert unsettled_eval_invocations(resumed.store.read_all(), 0, 0) == frozenset()
    # …and the node reached exactly one terminal across BOTH processes.
    terminals = [e for e in resumed.store.read_all()
                 if e.type in ("node_evaluated", "node_failed")]
    assert len(terminals) == 1
