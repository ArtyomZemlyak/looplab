"""A paid proposal must not stop the event loop for its whole duration.

`_stage_card_creates` runs one Researcher call per action. Until 2026-08-22 that call sat directly
on the asyncio loop thread with no `await` anywhere in it, so an entire propose phase executed as
ONE event-loop callback and nothing else could progress meanwhile.

Measured on the live engine with py-spy, sampled twice eight minutes apart: asyncio's own
`_run_once` sat BELOW a `threading.join` with no coroutine frame between. The bill was that node 4's
training exited at 21:03:40 and its terminal did not land until a restart 4 hours later — the engine
could not finalise an evaluation whose stages were already over, while both H200s idled and the
board grew to 88 unbuilt cards.

Only the PREPARE half moved. The staging half writes `card_added`, and this module's contract is
that "every selection-affecting event ... is written by the main engine task"; the staging loop says
the same in its own words. `_prepare_node_idea` is the right half to move precisely because it
writes nothing.
"""
import threading
import time

import anyio
import pytest


@pytest.mark.anyio
async def test_the_engine_keeps_turning_while_ITS_OWN_proposal_is_in_flight(tmp_path):
    """The property, driven through `_stage_card_creates` ITSELF.

    A first version of this test built its own `anyio.to_thread` shape and asserted the loop kept
    turning — which proved that anyio works and nothing about the engine. Removing the offload from
    `card_reservation.py` left it green. It was vacuous, and only the mutation said so; this version
    stubs the engine's OWN `_prepare_node_idea` with a blocking call and drives the real lane.
    """
    from tests.test_card_speculation_engine import _engine

    engine, _producer = _engine(tmp_path / "loop-turns", depth=0)
    started = threading.Event()
    release = threading.Event()
    ticks = 0

    observed = {}

    def _blocking_prepare(*_a, **_k):
        # Observe the loop FROM INSIDE the blocking call. Counting ticks from the start of the test
        # cannot discriminate: a frozen loop still shows the ticks it accumulated BEFORE the freeze,
        # which is how the first version of this test passed with the offload removed.
        started.set()
        observed["before"] = ticks
        time.sleep(0.15)
        observed["after"] = ticks
        release.set()
        return None                       # None -> the lane skips staging; the write half is untouched

    engine._prepare_node_idea = _blocking_prepare

    async def _ticker():
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await anyio.sleep(0.005)

    from looplab.events.replay import fold
    state = fold(engine.store.read_all())

    async with anyio.create_task_group() as tg:
        tg.start_soon(_ticker)
        tg.start_soon(engine._stage_card_creates, [{"kind": "draft"}], state)
        with anyio.fail_after(6):
            while not release.is_set():
                await anyio.sleep(0.005)

    assert observed.get("after", 0) > observed.get("before", 0), (

        "the loop must keep turning while the engine's own paid proposal is in flight. The call "
        "watched the loop's own counter across a 0.15s sleep and saw it stand still, which is the "
        "freeze that left a dead node without a terminal for 62 minutes.")


def test_the_staging_half_stayed_on_the_main_task():
    """The half that must NOT move, fenced so a later refactor cannot quietly move it.

    `_stage_prepared_card` appends `card_added`, and the module's contract reserves
    selection-affecting writes for the main engine task. Only `_prepare_node_idea` — which an AST
    pass shows makes zero `store.append` calls — is offloaded.
    """
    import inspect
    from looplab.engine.card_reservation import CardReservationMixin

    src = inspect.getsource(CardReservationMixin._stage_card_creates)
    assert "await anyio.to_thread.run_sync(" in src, "the paid proposal must leave the loop thread"
    prepare_at = src.index("self._prepare_node_idea")
    stage_at = src.index("self._stage_prepared_card")
    offload_at = src.index("await anyio.to_thread.run_sync(")
    assert offload_at < prepare_at < stage_at, "the offload must wrap the PREPARE call"
    tail = src[stage_at - 400:stage_at]
    assert "to_thread" not in tail, (
        "the staging half must remain on the main task — it writes card_added")


def test_the_offloaded_call_writes_no_events():
    """Why this half is the safe one to move, checked rather than trusted."""
    import ast
    import inspect
    from looplab.engine import orchestrator

    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(orchestrator.Engine._prepare_node_idea)))
    appends = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"append", "append_many"}
        and "store" in ast.unparse(node.func.value)
    ]
    assert appends == [], (
        "`_prepare_node_idea` must not write to the durable log: it now runs off the main task, "
        f"and the contract reserves selection-affecting writes for it. Found: {appends}")
