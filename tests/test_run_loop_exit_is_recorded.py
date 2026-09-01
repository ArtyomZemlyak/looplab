"""A run that stops says why, in one row, derived from its own final fold.

MEASURED 2026-08-31 over every run on this box: five carry a `pause` or a `run_finished` row and
THREE — rubertlite-dr-unified-v6, rubertlite-dr-unified-v9, e5small-dr-unified-v11 — carry neither.
v11's last durable event is a `trust_scan`, so anyone folding its log sees a run that is still in
flight, forever. Chasing "why did it stop" through that record ruled out a crash (the CLI's own
comment: `_run_engine_guarded` re-raises, and v11 PRINTED its summary), a budget stop (0 rows, no
budget configured, 547,737,877 tokens at cost 0.0), max_nodes (12 of 24), an operator stop, a pause,
and the approval exit (`require_approval=False`) — and then ran out of record.

`_run_with_llm_broker` has THIRTEEN break/return statements and no invariant that any of them
writes a reason. This is the same defect `card_build_done.skipped_reason` (8c7af6a7 — "the nine bare
`stale` exits are gone") and `node_repaired`'s bound (ffdb34e3) fixed one and two levels down.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import anyio
import pytest

from looplab.events.eventstore import EventStore
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_RUN_LOOP_EXITED, RUN_EXIT_REASONS,
                                  run_exit_reason)
from tests.factories import make_engine

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _S:
    """The four flags the deriver reads, defaulting to a run that just stopped."""
    def __init__(self, **kw):
        self.finished = self.stop_requested = self.paused = self.awaiting_approval = False
        for k, v in kw.items():
            setattr(self, k, v)


def test_a_run_that_names_nothing_still_gets_a_row():
    """THE CASE THIS EXISTS FOR. v11's fold sets none of the four flags; the honest answer is
    `unattributed`, and it is a row rather than silence."""
    assert run_exit_reason(_S()) == "unattributed"
    assert "unattributed" in RUN_EXIT_REASONS


def test_the_precedence_is_the_semantics():
    """Mutation: reorder any two branches and one of these flips.

    `finished` wins over everything — a run that wrote its report ended, and a pause latching on the
    way out does not un-end it. `aborted` precedes `paused` because `replay._on_pause` latches a
    pause for an abort too, so "the operator stopped it" is the more specific answer.
    """
    assert run_exit_reason(_S(finished=True)) == "finished"
    assert run_exit_reason(_S(finished=True, paused=True)) == "finished"
    assert run_exit_reason(_S(finished=True, stop_requested=True)) == "finished"
    assert run_exit_reason(_S(stop_requested=True, paused=True)) == "aborted"
    assert run_exit_reason(_S(paused=True)) == "paused"
    assert run_exit_reason(_S(awaiting_approval=True)) == "awaiting_approval"


def test_every_answer_is_in_the_registry():
    """The two-way half: a reason the deriver can return but the vocabulary does not name would be
    a word no reader can branch on."""
    for state in (_S(), _S(finished=True), _S(stop_requested=True), _S(paused=True),
                  _S(awaiting_approval=True)):
        assert run_exit_reason(state) in RUN_EXIT_REASONS


def test_every_registry_word_is_actually_PRODUCIBLE():
    """The other direction, and the one a growing vocabulary gets wrong: a reason no state can
    produce reads as coverage that does not exist. Mutation: add a word to the tuple without
    teaching the deriver to return it."""
    produced = {run_exit_reason(_S()),
                run_exit_reason(_S(finished=True)),
                run_exit_reason(_S(stop_requested=True)),
                run_exit_reason(_S(paused=True)),
                run_exit_reason(_S(awaiting_approval=True))}
    assert produced == set(RUN_EXIT_REASONS), (
        f"unproducible: {sorted(set(RUN_EXIT_REASONS) - produced)}; "
        f"unregistered: {sorted(produced - set(RUN_EXIT_REASONS))}")


def _fn(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_the_receipt_has_one_writer_and_both_exit_kinds_reach_it():
    """Mutation: delete the append, move it inside the `while`, or drop either call site, and this
    goes red.

    The append lives in `_record_run_loop_exit` exactly once (the latch there is what makes two
    call sites exactly-once). `_run_with_llm_broker` reaches it once, AFTER the loop — inside it,
    it fires every turn and stops being the answer to 'why did this run stop' — and `Engine.run`
    reaches it from the `finally` that owns the raising exits, which the original inline append
    silently skipped (a BudgetExceeded hard stop, a provider/store error, cancellation).
    """
    tree = ast.parse((ROOT / "looplab/engine/orchestrator.py").read_text())
    helper = _fn(tree, "_record_run_loop_exit")
    appends = [c for c in ast.walk(helper)
               if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "append"
               and any(isinstance(a, ast.Name) and a.id == "EV_RUN_LOOP_EXITED" for a in c.args)]
    assert len(appends) == 1, f"exactly one exit-receipt append, found {len(appends)}"

    loop_fn = _fn(tree, "_run_with_llm_broker")
    calls = [c for c in ast.walk(loop_fn)
             if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "_record_run_loop_exit"]
    assert len(calls) == 1, "the fall-through exit records the receipt exactly once"
    loops = [w for w in ast.walk(loop_fn) if isinstance(w, (ast.While, ast.For))]
    inside = [w for w in loops for c in ast.walk(w) if c is calls[0]]
    assert not inside, "the exit receipt must sit AFTER the loop"

    run_fn = _fn(tree, "run")
    assert any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "_record_run_loop_exit"
               for c in ast.walk(run_fn)), (
        "Engine.run must record the receipt on the raising exits — without its call the thirteen "
        "breaks are covered and a BudgetExceeded/crash/cancellation ends the run in silence again")


def _exit_rows(run_dir):
    return [e for e in EventStore(run_dir / "events.jsonl").read_all()
            if e.type == EV_RUN_LOOP_EXITED]


def test_a_raising_exit_still_writes_the_receipt(tmp_path):
    """THE EXITS THE INLINE APPEND MISSED. A crash unwinding out of the loop body must still leave
    the one row this feature exists for — before this, exactly the exit classes the v11 chase had
    to rule out by hand (budget stop, crash) were the ones that wrote nothing."""
    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1)

    class _Boom(RuntimeError):
        pass

    def _explode(*_a, **_k):
        raise _Boom("loop body died")

    # `CreationRunawayCounters()` is the first thing the loop constructs AFTER `_enter_run`
    # latched the receipt (a refused re-entry must append nothing), so breaking it is the earliest
    # honest raising exit.
    import looplab.engine.orchestrator as orch
    original = orch.CreationRunawayCounters
    orch.CreationRunawayCounters = _explode
    try:
        with pytest.raises(_Boom):
            anyio.run(engine.run)
    finally:
        orch.CreationRunawayCounters = original
    rows = _exit_rows(tmp_path / "run")
    assert len(rows) == 1, "exactly one receipt, from Engine.run's finally"
    assert rows[0].data["reason"] == "unattributed"
    assert rows[0].data["reason"] in RUN_EXIT_REASONS


def test_a_finished_run_writes_no_row_because_run_finished_is_that_receipt(tmp_path):
    """The `finished` skip. `run_finished` sits inside `QUIET_FINALIZATION_SUFFIX`, whose readers
    (`speculation_quality._quiet_finalization`, `test_finalize_protocol`'s real-run half) demand
    the exact contiguous terminal shape — the first cut of this feature spliced a row between
    `run_finished` and `budget` and the calibration gate refused every run recorded with it."""
    engine = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1)
    state = anyio.run(engine.run)
    assert state.finished
    assert _exit_rows(tmp_path / "run") == [], (
        "a finished run's exit is already named by run_finished; a run_loop_exited row here "
        "breaks the finalization suffix every calibration reader pins")


def test_it_is_diagnostic_and_the_fold_ignores_it():
    """A run's exit reason must not reshape the projection a resume rebuilds. `test_event_types.py`
    enforces the folded/diagnostic partition; this pins which side this type is on."""
    assert EV_RUN_LOOP_EXITED in DIAGNOSTIC_EVENTS
    from looplab.events import replay
    assert EV_RUN_LOOP_EXITED not in getattr(replay, "_HANDLERS", {})


def test_the_deriver_reads_flags_it_does_not_own():
    """NON-VACUITY for the whole file: the deriver must actually consult the state, not return a
    constant. A `getattr(..., False)` over an object with none of the attributes is the v11 shape."""
    src = inspect.getsource(run_exit_reason)
    for flag in ("finished", "stop_requested", "paused", "awaiting_approval"):
        assert flag in src, f"{flag} is not consulted — the deriver cannot tell that state apart"

    class _Nothing:
        pass
    assert run_exit_reason(_Nothing()) == "unattributed", (
        "a state object missing every flag must still produce a legal reason, not raise — the "
        "receipt may never be the thing that breaks a shutdown")
