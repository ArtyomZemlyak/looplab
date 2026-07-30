"""SETUP_THREAD_APPENDABLE — invariant #1's remaining THREAD-SIDE exception (docs/15 §P4.1).

`_ensure_run_setup` (engine/eval_dispatch.py) appends the FOLDED `run_setup_started` /
`run_setup_finished` events from an eval WORKER THREAD: `_run_eval` runs under `anyio.to_thread`
from `_evaluate`, so those appends are outside `_write_lock` and outside every other documented
exception (BACKGROUND_APPENDABLE / DIAGNOSTIC_EVENTS / the per-node parallel-build seam). That was
argued only in a comment at the append site. These tests turn the argument into a red test:

  1. registry sanity — the set exists, is registered, and does not overlap the other exceptions;
  2. command-keyed neutrality — folding the SAME log with a setup pair spliced at EVERY position
     yields the identical run_setup_open/run_setup_done AND the identical node selection, because
     the fold keys these purely BY COMMAND;
  3. source guard — the thread-side append site asserts its own membership, so a future edit that
     adds a different folded type there trips the assert instead of silently widening the seam.
"""
from __future__ import annotations

import inspect

from looplab.core.models import Event, run_setup_key
from looplab.engine import eval_dispatch
from looplab.events.replay import fold
from looplab.events.types import (ALL_EVENT_TYPES, BACKGROUND_APPENDABLE, DIAGNOSTIC_EVENTS,
                                  EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED,
                                  NON_CARD_SELECTION_BACKGROUND_APPENDABLE,
                                  SETUP_THREAD_APPENDABLE)

_CMD = ["pip", "install", "-r", "requirements.txt"]


def _base_events() -> list[Event]:
    """A tiny two-node run: node 1 wins under direction=min."""
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "min"}),
        ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                          "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""}),
        ("node_evaluated", {"node_id": 0, "metric": 5.0, "eval_seconds": 0.1}),
        ("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                          "idea": {"operator": "improve", "params": {"x": 2.0}}, "code": ""}),
        ("node_evaluated", {"node_id": 1, "metric": 1.0, "eval_seconds": 0.1}),
    ]
    return [Event(seq=i, ts=float(i), type=t, data=d) for i, (t, d) in enumerate(rows)]


def test_registry_sane():
    assert SETUP_THREAD_APPENDABLE, "the thread-side exception set must not be empty"
    assert SETUP_THREAD_APPENDABLE <= ALL_EVENT_TYPES
    # Growing this set is a DECISION, not a drive-by: a new member must also pass the splice test
    # below, so the edit has to happen here.
    assert SETUP_THREAD_APPENDABLE == frozenset({EV_RUN_SETUP_STARTED, EV_RUN_SETUP_FINISHED})
    # It is its own exception, not a re-listing of the others.
    assert SETUP_THREAD_APPENDABLE.isdisjoint(BACKGROUND_APPENDABLE)
    assert SETUP_THREAD_APPENDABLE.isdisjoint(NON_CARD_SELECTION_BACKGROUND_APPENDABLE)
    assert SETUP_THREAD_APPENDABLE.isdisjoint(DIAGNOSTIC_EVENTS)   # these ARE folded


def test_setup_events_fold_by_command_at_every_position():
    """Position in events.jsonl is thread-schedule-dependent, so it must not change the fold."""
    base = _base_events()
    ref = fold(base)
    key = run_setup_key(_CMD)
    for pos in range(1, len(base) + 1):          # after run_started .. at the tail
        pair = [Event(seq=98, ts=98.0, type=EV_RUN_SETUP_STARTED,
                      data={"command": _CMD, "cwd": "/repo", "after_interrupted_attempt": False}),
                Event(seq=99, ts=99.0, type=EV_RUN_SETUP_FINISHED,
                      data={"command": _CMD, "exit_code": 0, "timed_out": False, "stderr_tail": ""})]
        st = fold(base[:pos] + pair + base[pos:])
        assert st.best_node_id == ref.best_node_id, pos       # selection untouched
        assert set(st.nodes) == set(ref.nodes), pos
        assert key in st.run_setup_done, pos                  # ...and keyed purely by command
        assert key not in st.run_setup_open, pos

    # An INTERRUPTED attempt (started, never finished) stays open at every position too.
    for pos in range(1, len(base) + 1):
        st = fold(base[:pos] + [Event(seq=98, ts=98.0, type=EV_RUN_SETUP_STARTED,
                                      data={"command": _CMD, "cwd": "/repo",
                                            "after_interrupted_attempt": False})] + base[pos:])
        assert key in st.run_setup_open and key not in st.run_setup_done, pos


def test_thread_side_append_site_asserts_its_own_membership():
    """The append site must pin membership, so widening it cannot happen silently."""
    src = inspect.getsource(eval_dispatch.EvalDispatchMixin._do_run_setup)
    assert "assert EV_RUN_SETUP_STARTED in SETUP_THREAD_APPENDABLE" in src
    assert "assert EV_RUN_SETUP_FINISHED in SETUP_THREAD_APPENDABLE" in src
    # ...and this is the ONLY thread-side folded append in the module.
    module_src = inspect.getsource(eval_dispatch)
    appends = [line for line in module_src.splitlines() if "self.store.append(" in line]
    assert len(appends) == 2, appends
