"""The engine does not re-fold a prefix the same call chain has just folded (review 2026-09-22, EVT-04).

`fold` is O(log) and dominated by its finalize (the card ledger), so every repeat of an unchanged
prefix is a whole fold spent for nothing: the reviewer's 60-node toy run made 820 full folds over
451,001 events, and 419 of its 815 seam calls re-folded a prefix already folded. Two call chains
made most of the repeats that a chain can remove WITHOUT sharing a folded state between owners
(the declined `shared-fold-memo-races-the-build-worker`, doc 25 ES-12, still refuses that):

* an UNRESERVED build folded the same prefix twice — `_create_node` for its trace label, then
  `_create_node_scoped` for its proposal — and now hands its own fold down;
* the SERIAL eval dispatch folded one quiet prefix three times per evaluation — at the loop top, on
  the resource wait's first tick, and again at admission — and now seeds the wait's tail gate
  (`_fold_if_tail_moved`) with the loop-top fold and reads admission through the same gate.

Driven through a real toy run with the engine's one fold seam (`orchestrator.fold`) wrapped, each
fold attributed to the engine function that asked for it. Measured on the reviewer's own run (the
documented offline smoke, `-s max_nodes=60`): 820 -> 656 folds, 451,001 -> 353,885 events folded.
"""
from __future__ import annotations

import sys

import anyio

from factories import make_engine


def _folds_by_caller(tmp_path, monkeypatch, **engine_kwargs):
    """Run a toy engine with the seam wrapped; return `[(caller, prefix key)]` in fold order."""
    from looplab.engine import orchestrator

    real = orchestrator.fold
    folds: list[tuple[str, tuple]] = []

    def counting_fold(events):
        events = list(events)
        frame = sys._getframe(1)
        if frame.f_code.co_name == "engine_fold":       # reached through `shared.engine_fold`
            frame = frame.f_back
        key = (len(events), events[-1].seq if events else None,
               id(events[-1]) if events else None)
        folds.append((frame.f_code.co_name, key))
        return real(events)

    monkeypatch.setattr(orchestrator, "fold", counting_fold)
    engine = make_engine(tmp_path / "run", **engine_kwargs)
    state = anyio.run(engine.run)
    assert state.finished
    return folds, state


def test_an_unreserved_build_folds_its_proposal_prefix_once(tmp_path, monkeypatch):
    folds, state = _folds_by_caller(tmp_path, monkeypatch, n_seeds=2, max_nodes=4)
    assert len(state.nodes) == 4
    built = [key for caller, key in folds if caller == "_create_node"]
    assert len(built) == 4, "every build here is unreserved and folds once for its label + proposal"
    assert not [caller for caller, _ in folds if caller == "_create_node_scoped"], (
        "_create_node_scoped re-folded the prefix _create_node had just folded for the same build")


def test_the_serial_dispatch_folds_a_quiet_prefix_once_per_evaluation(tmp_path, monkeypatch):
    folds, state = _folds_by_caller(tmp_path, monkeypatch, n_seeds=2, max_nodes=4)
    evaluated = [n for n in state.nodes.values() if n.metric is not None]
    assert len(evaluated) == 4
    dispatch = [key for caller, key in folds
                if caller in ("_dispatch_evals", "_fold_if_tail_moved")]
    # Nothing appends between an evaluation's loop-top fold and its admission on this run, so the
    # tail gate must answer every later read of that prefix from the fold already in hand.
    assert len(dispatch) == len(evaluated), (
        f"the serial dispatcher folded {len(dispatch)} times for {len(evaluated)} evaluations")
    assert len(set(dispatch)) == len(dispatch), "a dispatch fold repeated an identical prefix"
