"""The serial build lane may run its PAID work in a worker; it may not mint a Card there.

`orchestrator.py::_offload_build` (2026-09-06) moved the whole of `_create_node` off the loop,
reservation included, and that took two FOLDED appends — `card_added` and `node_building` — off the
main task, against `events/types.py`'s own statement about the Card ledger ("Main-task-written; NONE
are BACKGROUND_APPENDABLE — a monotonic card_id cannot be background-minted") and against
`_offload_build`'s docstring promise that a worker appends only its OWN node's rows.

The money half was the expensive one. `_proposal_authority_seq` fences the reservation's CAS retries
on seq EQUALITY, justified by the window being "microseconds long" with "nothing paid at risk in
it" — true while `_create_node` froze the loop, false the moment it ran in a worker with the loop
still turning. Driven, isolated 2x2, real Engine and real store, the racer appending the
BACKGROUND_APPENDABLE `research_completed`: 38/40 paid proposals silently discarded in the offloaded
cell and 0/40 in all three controls.

Both are one property — the reservation belongs to the main task — so both are asserted here.
"""
from __future__ import annotations

import functools
import pathlib
import tempfile
import threading

import anyio

from looplab.events.eventstore import EventStore
from looplab.events.types import EV_CARD_ADDED, EV_NODE_BUILDING
from tests.factories import make_engine


def _appends_by_thread(engine, run):
    """[(event type, was it appended on THIS thread), …] for one driven build."""
    main = threading.get_ident()
    seen: list[tuple[str, bool]] = []
    real_append, real_many = EventStore.append, EventStore.append_many

    def append(self, type_, data=None, **kw):
        seen.append((type_, threading.get_ident() == main))
        return real_append(self, type_, data, **kw)

    def append_many(self, rows, **kw):
        for row in rows:
            seen.append((row[0], threading.get_ident() == main))
        return real_many(self, rows, **kw)

    EventStore.append, EventStore.append_many = append, append_many
    try:
        run()
    finally:
        EventStore.append, EventStore.append_many = real_append, real_many
    return seen


def test_an_offloaded_build_mints_its_card_on_the_main_task(tmp_path):
    """Driven through the real `_offload_build`, not pinned: the whole point is WHICH THREAD."""
    engine = make_engine(tmp_path / "reservation")

    async def build():
        await engine._offload_build(functools.partial(engine._create_node, {"kind": "draft"}))

    def run():
        try:
            anyio.run(build)
        except Exception:      # noqa: BLE001 - a toy build may end however it likes; the THREAD is the claim
            pass

    seen = _appends_by_thread(engine, run)
    ledger = [(type_, on_main) for type_, on_main in seen
              if type_ in (EV_CARD_ADDED, EV_NODE_BUILDING)]
    assert ledger, (
        "the build appended neither `card_added` nor `node_building` — this test asserts WHERE they "
        "are appended and cannot do that if the reservation never ran")
    off_main = [type_ for type_, on_main in ledger if not on_main]
    assert not off_main, (
        f"the Card ledger was minted in a worker: {off_main}. `events/types.py` says these are "
        "main-task-written, and a worker-side CAS is what discards paid proposals — see "
        "`_reserve_on_main_task`")


def test_the_helper_marshals_only_when_it_is_actually_in_a_worker():
    """The truth table of the seam itself, so the mechanism cannot degrade into always-direct.

    Always-direct is exactly the pre-fix behaviour and would leave the test above the only guard;
    always-marshal would deadlock on the main task, where there is no loop to hand work to.
    """
    from looplab.engine import orchestrator

    engine = make_engine(pathlib.Path(tempfile.mkdtemp()))
    calls: list[str] = []
    engine._reserve_node_build = lambda *a, **k: (   # type: ignore[method-assign]
        calls.append(threading.current_thread().name) or "reserved")

    # On the main task, with no marker: called straight through.
    assert engine._reserve_on_main_task({"kind": "draft"}) == "reserved"
    assert calls == [threading.current_thread().name]

    # Inside a marked worker: the call comes back to the loop thread, not the worker's.
    calls.clear()
    loop_thread = threading.current_thread().name

    def worker():
        orchestrator._OFFLOADED_BUILD.value = True
        try:
            return engine._reserve_on_main_task({"kind": "draft"})
        finally:
            orchestrator._OFFLOADED_BUILD.value = False

    async def drive():
        return await anyio.to_thread.run_sync(worker)

    assert anyio.run(drive) == "reserved"
    assert calls == [loop_thread], (
        f"the reservation ran on {calls}, not on the loop thread — the marshal is not happening")
