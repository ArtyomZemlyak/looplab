"""An in-place rebuild (`Engine._rerun_node`) writes run-global and Card rows only on the MAIN task.

Review 2026-09-22, ENG1-05. `_rerun_node` has run in an `_offload_build` worker since 2026-09-06,
and two of its appends were never moved with it — both driven here through the REAL
`_offload_build` + `_rerun_node` pair, recording the thread of every append:

* THE CRASH PAUSE. On a Developer-crash sentinel it appended the run-global, FOLDED `pause` itself,
  from the worker — the append `_create_node_scoped` deliberately queues through
  `_request_create_pause`, because a worker's byte position against a concurrent `resume` is
  nondeterministic (invariant #1). Queued now, and queued with the node's CURRENT generation: the
  queue used to hard-code 0, which `replay._on_pause` drops for a rebuild (attempt >= 1).
* THE RE-PROPOSAL'S CARD SWAP. `card_auto_dropped` + `card_added` + `node_building` came off the
  worker as separate appends with no tail CAS — `events/types.py` states the Card ledger is
  main-task-written, and `_reserve_on_main_task` documents what a worker-side reservation cost the
  ordinary build (38 of 40 paid proposals lost in its 2x2). They are now planned and committed on
  the main task as ONE `append_many` under a tail CAS.
"""
from __future__ import annotations

import functools
import threading

import anyio

from looplab.events.replay import fold
from looplab.events.types import (EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_NODE_BUILDING,
                                  EV_NODE_RESET, EV_PAUSE)
from tests.factories import make_engine

_CARD_SWAP = (EV_CARD_AUTO_DROPPED, EV_CARD_ADDED, EV_NODE_BUILDING)


class _CrashingDeveloper:
    """The in-band sentinel every build site recognises as a crashed Developer session."""

    def implement(self, idea):
        return "(developer error: provider unreachable)"

    def implement_from(self, idea, parent):
        return "(developer error: provider unreachable)"


def _evaluated_run(run_dir):
    eng = make_engine(run_dir, n_seeds=1, max_nodes=1)
    state = anyio.run(eng.run)
    assert state.nodes[0].metric is not None
    return eng, state.nodes[0]


def _watch_appends(eng) -> list:
    """Record `(method, [types], appended_on_the_main_thread)` for every append to the real store.

    The main thread IS the event loop's thread here (`anyio.run`), so "not the main thread" means
    an `_offload_build` worker."""
    store = eng.store
    real_append, real_many = store.append, store.append_many
    log: list = []

    def append(event_type, data, **kwargs):
        log.append(("append", [event_type], threading.current_thread() is threading.main_thread()))
        return real_append(event_type, data, **kwargs)

    def append_many(records, **kwargs):
        log.append(("append_many", [event_type for event_type, _ in records],
                    threading.current_thread() is threading.main_thread()))
        return real_many(records, **kwargs)

    store.append, store.append_many = append, append_many
    return log


def _drive_rerun(eng, node_id):
    """The run loop's own pair: offload the rebuild, then drain the pause queue on the loop."""
    state = fold(eng.store.read_all())

    async def _drive():
        await eng._offload_build(functools.partial(eng._rerun_node, state.nodes[node_id], state))
        eng._drain_create_pause()

    anyio.run(_drive)


def test_a_rebuild_crash_queues_its_pause_and_the_fold_takes_it(tmp_path):
    eng, node = _evaluated_run(tmp_path / "run")
    eng.store.append(EV_NODE_RESET, {"node_id": node.id, "from_stage": "implement",
                                     "generation": node.attempt})
    eng.developer = _CrashingDeveloper()
    log = _watch_appends(eng)
    _drive_rerun(eng, node.id)

    pauses = [on_main for _method, types, on_main in log if EV_PAUSE in types]
    assert pauses == [True], (
        f"the run-global pause must be appended once, by the MAIN task — got {pauses}")
    state = fold(eng.store.read_all())
    assert state.nodes[node.id].error_reason == "developer_crash"
    assert state.nodes[node.id].attempt == 1
    # The generation it names is the REBUILD's: a generation-0 pause is dropped by the fold.
    assert state.paused is True and state.pause_node_id == node.id
    assert state.pause_generation == 1


def test_a_repropose_commits_its_card_swap_on_the_main_task_as_one_batch(tmp_path):
    eng, node = _evaluated_run(tmp_path / "run")
    old_card = node.idea.card_id
    eng.store.append(EV_NODE_RESET, {"node_id": node.id, "from_stage": "propose",
                                     "generation": node.attempt})
    log = _watch_appends(eng)
    _drive_rerun(eng, node.id)

    swap = [(method, types, on_main) for method, types, on_main in log
            if set(types) & set(_CARD_SWAP)]
    assert swap == [("append_many", list(_CARD_SWAP), True)], (
        f"the Card swap and its claim must land as ONE main-task batch, got {swap}")
    state = fold(eng.store.read_all())
    rebuilt = state.nodes[node.id]
    assert rebuilt.attempt == 1 and rebuilt.rerun_from is None
    assert rebuilt.idea.card_id not in (None, old_card)
    assert state.cards[old_card].status == "dropped"
    assert state.cards[rebuilt.idea.card_id].evidence == [node.id]


def test_a_rejected_replacement_closes_the_rebuild_on_the_main_task(tmp_path, monkeypatch):
    """A replacement that cannot form a native Card closes THIS lifecycle `proposal_rejected` and
    retires the card it would have superseded — from the main task, like the rest of the commit."""
    from looplab.engine.card_reservation import _CardReservationPlan
    from looplab.events.types import EV_NODE_FAILED

    eng, node = _evaluated_run(tmp_path / "run")
    old_card = node.idea.card_id
    eng.store.append(EV_NODE_RESET, {"node_id": node.id, "from_stage": "propose",
                                     "generation": node.attempt})
    monkeypatch.setattr(eng, "_plan_native_card",
                        lambda *a, **k: _CardReservationPlan("duplicate", None, None, None))
    log = _watch_appends(eng)
    _drive_rerun(eng, node.id)
    closing = [(types, on_main) for _m, types, on_main in log
               if set(types) & {EV_NODE_FAILED, EV_CARD_AUTO_DROPPED, EV_CARD_ADDED,
                                EV_NODE_BUILDING}]
    assert closing == [([EV_CARD_AUTO_DROPPED], True), ([EV_NODE_FAILED], True)], closing
    state = fold(eng.store.read_all())
    assert state.nodes[node.id].error_reason == "proposal_rejected"
    assert state.cards[old_card].status == "dropped"


def test_a_node_aborted_while_the_researcher_proposed_writes_nothing(tmp_path):
    """The lifecycle fence is asked on the COMMIT's fold: an abort that lands while the paid
    proposal runs leaves no Card swap and no claim behind."""
    eng, node = _evaluated_run(tmp_path / "run")
    eng.store.append(EV_NODE_RESET, {"node_id": node.id, "from_stage": "propose",
                                     "generation": node.attempt})
    real = eng.researcher

    class _AbortedMidProposal:
        def __getattr__(self, name):
            return getattr(real, name)

        def propose(self, state, parent):
            eng.store.append("node_abort", {"node_id": node.id, "generation": 1})
            return real.propose(state, parent)

    eng.researcher = _AbortedMidProposal()
    log = _watch_appends(eng)
    _drive_rerun(eng, node.id)
    assert not [types for _m, types, _on_main in log if set(types) & set(_CARD_SWAP)]
    assert node.id in fold(eng.store.read_all()).aborted_nodes


def test_the_card_commit_is_a_tail_cas_that_replans_on_a_moved_log(tmp_path, monkeypatch):
    """A row landing between the plan and the append must not be written over: the batch loses
    the CAS, re-plans against the new tail, and lands after it."""
    eng, node = _evaluated_run(tmp_path / "run")
    eng.store.append(EV_NODE_RESET, {"node_id": node.id, "from_stage": "propose",
                                     "generation": node.attempt})
    real_plan = eng._plan_native_card
    plans = {"n": 0}

    def _racing_plan(*args, **kwargs):
        plans["n"] += 1
        if plans["n"] == 1:
            # A concurrent writer (a control intent) lands after this plan read its tail.
            eng.store.append("hint", {"text": "a racing operator hint"})
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(eng, "_plan_native_card", _racing_plan)
    _drive_rerun(eng, node.id)
    events = eng.store.read_all()
    assert plans["n"] == 2, "the commit did not re-plan after losing its tail CAS"
    hint = next(e.seq for e in events if e.type == "hint")
    swap = [e for e in events if e.type in _CARD_SWAP and e.seq > hint]
    assert [e.type for e in swap] == list(_CARD_SWAP)
    assert fold(events).nodes[node.id].attempt == 1
