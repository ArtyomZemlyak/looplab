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

from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_RUN_LOOP_EXITED, RUN_EXIT_REASONS,
                                  run_exit_reason)

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


def test_the_loop_appends_it_exactly_once_and_after_the_loop():
    """Mutation: delete the append, or move it inside the `while`, and this goes red.

    Once, because a row per turn is not a receipt; AFTER the loop, because every one of the
    thirteen exits has to pass through it — that is what makes the derivation complete without
    touching a single `break`.
    """
    src = (ROOT / "looplab/engine/orchestrator.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_run_with_llm_broker")
    appends = [c for c in ast.walk(fn)
               if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "append"
               and any(isinstance(a, ast.Name) and a.id == "EV_RUN_LOOP_EXITED" for a in c.args)]
    assert len(appends) == 1, f"exactly one exit receipt, found {len(appends)}"
    loops = [w for w in ast.walk(fn) if isinstance(w, (ast.While, ast.For))]
    inside = [w for w in loops for c in ast.walk(w) if c is appends[0]]
    assert not inside, (
        "the exit receipt must sit AFTER the loop — inside it, it fires every turn and stops being "
        "the answer to 'why did this run stop'")


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
