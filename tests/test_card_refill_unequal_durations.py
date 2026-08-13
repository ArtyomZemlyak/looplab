"""The unequal-duration refill regression the Card session's own TODO asked for (backlog F1f/F1g).

`_card_eval_one` carried this note for as long as the session has existed:

    CODEX AGENT: this session-wide first-completion fence prevents the Card path from refilling a
    freed GPU while unrelated long-running siblings finish. Preserve the outer cadence boundary
    without turning one terminal child into head-of-line blocking for every remaining slot; add an
    unequal-duration refill regression.

Nothing in the 8,900-test suite failed when the second GPU went dark for two hours, which is why
the defect survived to be found by an operator watching `nvidia-smi`. Measured across the six
width-2 runs on this box: 115.6 GPU-h of idle second slot against 164.4 GPU-h of work actually
done — 82.6 % of all second-slot time available while the box was busy. Worst single window
`rubert-dr-0807`, 41.8 h at occupancy 1 after having been at 2.

These are TIER-1 tests in CLAUDE.md's ladder: a real `Engine`, real Cards, real admission, real
resource reservation, real `node_eval_started` rows. The only sentinel is `_evaluate` itself, which
is what supplies the two unequal durations — the whole point of the regression. A source pin could
not distinguish "the gate was widened" from "a slot was actually refilled".

Both halves are driven, because the defect had two:

  * the CONSUMER stopped admitting at the first terminal
    (`test_a_freed_slot_is_refilled_while_the_slow_sibling_still_runs`), and
  * the session could not reach the outer boundary until the LAST eval drained, so the latch
    bought nothing it cost a GPU for
    (`test_the_session_reaches_the_outer_boundary_without_draining_the_slow_eval`,
    `test_a_producer_yield_reaches_the_outer_boundary_during_a_long_eval`).
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.models import NodeStatus
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_EVALUATED, EV_NODE_EVAL_STARTED, EV_NODE_FAILED

# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here: these
# tests admit speculation through the production boundary and then replace only the roles.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _commit_speculative_node,
    _engine,
    _request,
    _start,
    _without_research,
)


def _terminalize(engine, node_id: int, *, failed: bool = False) -> None:
    """Write the one terminal `_evaluate` would have written, from the eval child's own task."""

    node = fold(engine.store.read_all()).nodes[node_id]
    if node.status is not NodeStatus.pending:
        return
    if failed:
        engine.store.append(EV_NODE_FAILED, {
            "node_id": node_id,
            "generation": node.attempt,
            "error": "short sibling finished first",
            "eval_seconds": 0.0,
        })
        return
    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": node_id,
        "generation": node.attempt,
        "metric": float(node_id),
        "eval_seconds": 0.0,
    })


def _three_ready_cards(engine) -> None:
    """Three selectable draft Cards and the durable bootstrap request that starts the pipeline."""

    for card_id, x in (("card-1", 0.2), ("card-2", 0.8), ("card-3", 0.6)):
        _add_ready_draft(engine, card_id, x=x)
    _request(engine)


def test_a_freed_slot_is_refilled_while_the_slow_sibling_still_runs(tmp_path, monkeypatch):
    """THE regression, driven end to end: two unequal evals at width 2, and a third node.

    Before the fix the first terminal set `CardSession.consumer_completed` in the eval child's
    `finally`, `open_for_new_work` went false for EVERY slot, and `_card_phase_decide_exit` then
    refused to return until the long sibling landed. The run stopped starting work at the FIRST
    terminal and reached the outer boundary no sooner than it would have anyway. So this test is
    written the way the live run behaves: a MINIMAL outer loop that re-enters the session, with the
    eval task group owned outside it — which is exactly the shape `Engine.run` now has.

    What must happen, and does not on the pre-fix tree:

      turn 0  the bootstrap head builds node 0, admitted and HELD (the slow sibling);
              `speculation_depth` then prefetches card-2 into node 1, admitted into the second slot
              — the run reaches width 2, which it demonstrably could before F1f too;
      node 1 terminates -> the outer boundary is owed a turn -> the session RETURNS, leaving the
              slow sibling running (pre-fix: it could not, and admission was shut for both slots);
      turn 1  a fresh session, with production re-opened, elects card-3, builds it and admits node 2
              into the freed slot **while node 0 is still training**.

    The short sibling FAILS rather than scoring. That is deliberate and it is not a dodge: a
    terminal that moves `best` supersedes a draft-shaped prefetch, and the correct answer is then to
    drop it rather than run it — freshness is downstream of the admission gate and still runs, so
    un-latching does not mean dispatching stale work. This test is about the SLOT, so it removes
    the freshness question and leaves exactly the head-of-line blocking.
    """

    engine, _producer = _engine(tmp_path / "unequal-refill", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _three_ready_cards(engine)
    _without_research(monkeypatch, engine)

    admitted: list[int] = []
    release_slow = anyio.Event()

    async def _unequal_eval(node_id, _limiter, _max_es):
        admitted.append(node_id)
        if len(admitted) == 1:             # the SLOW sibling: held for the whole test
            await release_slow.wait()
            _terminalize(engine, node_id)
            return
        _terminalize(engine, node_id, failed=True)

    monkeypatch.setattr(engine, "_evaluate", _unequal_eval)
    inflight_at_refill: list[set] = []

    async def _minimal_outer_loop():
        # `Engine.run` owns the eval task group; `_run_with_llm_broker` turns inside it. Everything
        # this stand-in leaves out (cadences, acks, control overrides, forced requests) is work the
        # session cannot do — which is the whole reason the boundary has to be reachable at all.
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            try:
                with anyio.move_on_after(30):
                    for _turn in range(6):
                        await engine._run_card_session(
                            [], fold(engine.store.read_all()), None)
                        if len(admitted) >= 3:
                            inflight_at_refill.append(set(engine._eval_inflight))
                            break
            finally:
                # A red assertion must report a dark GPU, never hang the suite.
                release_slow.set()

    anyio.run(_minimal_outer_loop)

    assert len(admitted) >= 3, (
        f"the freed slot was never refilled (admitted={admitted}): the session stopped admitting "
        "at the FIRST terminal and could not return until the LAST eval drained — the shape that "
        "cost 82.6 % of this box's second-slot time")
    # THE CLAIM: the third eval started while the slow sibling was still training.
    assert inflight_at_refill == [{(admitted[0], 0)}], (
        "the refill must overlap the slow sibling, not follow it")
    started = [
        event.data["node_id"] for event in engine.store.read_all()
        if event.type == EV_NODE_EVAL_STARTED
    ]
    assert sorted(started) == sorted(admitted), (
        "every admitted node must carry its durable eval-start boundary, written by the MAIN task "
        "at the dispatch decision (engine invariant #1)")


def test_the_session_reaches_the_outer_boundary_without_draining_the_slow_eval(
        tmp_path, monkeypatch):
    """The other half: a terminal owes the outer loop a turn, and it must be able to TAKE one.

    The old exit gate held the session open on `session.eval_inflight`, so the boundary the latch
    was protecting did not arrive until the LAST terminal while the debt was incurred by the FIRST.
    That asymmetry — stop starting work now, hand back much later — is the defect stated exactly.

    With the eval task group owned by `Engine.run`, the session returns and the next one adopts.
    Driven here by giving the engine a run-scoped group directly, which is what the spine does.
    """

    engine, _producer = _engine(tmp_path / "adopting-session", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _three_ready_cards(engine)
    _without_research(monkeypatch, engine)

    admitted: list[int] = []
    release_slow = anyio.Event()

    async def _unequal_eval(node_id, _limiter, _max_es):
        admitted.append(node_id)
        rank = len(admitted)          # bound at ENTRY: a sibling admitted later must not rename it
        if rank == 1:
            await release_slow.wait()
        _terminalize(engine, node_id, failed=rank != 1)

    monkeypatch.setattr(engine, "_evaluate", _unequal_eval)
    returned = anyio.Event()

    async def _scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            try:
                with anyio.move_on_after(20):
                    await engine._run_card_session(
                        [], fold(engine.store.read_all()), None)
                    returned.set()
                # THE ASSERTION, taken while the slow sibling is still burning a GPU.
                assert returned.is_set(), (
                    "the session never returned: it was waiting for the slowest eval to drain "
                    "before it would let the outer control/Strategist/cadence boundary run")
                assert admitted, "no eval was ever admitted"
                assert engine._eval_inflight == {(admitted[0], 0)}, (
                    "the slow eval must still be in flight and adoptable by the next session")
            finally:
                release_slow.set()

    anyio.run(_scenario)

    # …and the adopted child really did run to its terminal under the RUN-scoped group, after the
    # session that admitted it had already returned.
    state = fold(engine.store.read_all())
    assert state.nodes[admitted[0]].status is NodeStatus.evaluated
    assert engine._eval_inflight == set()


def test_a_producer_yield_reaches_the_outer_boundary_during_a_long_eval(tmp_path, monkeypatch):
    """The SERIAL-GAP half (backlog F1g): `yield_outer` must not sterilize the run mid-eval.

    `_card_phase_request_build` sets `yield_outer` when no durable Card owns the counterfactual next
    action and the raw lane yields nothing — i.e. whenever the board is empty, which mid-eval it
    usually is, because new Cards are produced by OUTER-loop cadences. The session then went
    sterile: no build request, no `node_building`, no `card_added`, for the rest of the evaluation,
    and it still could not return. 167.7 GPU-h across this box's corpus was spent with NO eval
    running at all, paying build latency serially after each terminal instead of hiding it behind
    the eval that was already running.

    `yield_outer` means "the producer needs a fresh outer authority snapshot". The answer to that is
    to RETURN and get one. This test drives exactly that: one long eval, an empty board, and the
    session must hand back while the GPU is still busy.
    """

    engine, _producer = _engine(tmp_path / "serial-gap", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    # No second Card and no isolated raw lane: `_request_card_build` declines and the raw lane has
    # nothing to propose, which is precisely the board state a long eval leaves behind.
    engine._spec_role_pair = None
    engine.role_factory = None

    release_slow = anyio.Event()
    running = anyio.Event()

    async def _slow_eval(admitted_id, _limiter, _max_es):
        running.set()
        await release_slow.wait()
        _terminalize(engine, admitted_id)

    monkeypatch.setattr(engine, "_evaluate", _slow_eval)
    returned = anyio.Event()

    async def _scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            try:
                with anyio.move_on_after(20):
                    await engine._run_card_session(
                        [], fold(engine.store.read_all()), None)
                    returned.set()
                assert running.is_set()
                assert returned.is_set(), (
                    "the producer yielded to the outer loop and the session then refused to go "
                    "there until the eval finished — so the board could not be refilled during "
                    "the very evaluation the prefetch exists to hide a build behind")
            finally:
                release_slow.set()

    anyio.run(_scenario)

    assert fold(engine.store.read_all()).nodes[node_id].status is NodeStatus.evaluated


@pytest.mark.parametrize("failed", [False, True])
def test_an_adopted_eval_blocks_the_finish_gate_until_it_is_drained(tmp_path, monkeypatch, failed):
    """Quiescence now includes running evaluations, not just a still log.

    Doc 33 names this as option 1's dangerous failure: "finalization races a running eval and
    finishes the run over live work". Before the hoist, every finish decision was structurally
    preceded by a session join, so `after_seq` equality was the whole of quiescence. It no longer
    is, and `_refuse_finish_over_adopted_evals` is the guard — it must REFUSE and ask for a drain,
    so the run loop pays the (free, because the run is stopping) wait on its next turn.
    """

    engine, _producer = _engine(tmp_path / f"finish-gate-{failed}", depth=2)
    _start(engine)
    engine._ensure_speculation_state()
    engine._eval_inflight.add((0, 0))

    events = engine.store.read_all()
    after_seq = events[-1].seq if events else -1
    state = fold(events)

    assert engine._finish_if_quiescent({"reason": "aborted"}, after_seq=after_seq) is False
    assert engine._eval_drain_requested is True
    assert not [
        event for event in engine.store.read_all() if event.type == "run_finished"]

    assert engine._finish_with_report_if_quiescent(
        state, {"reason": "aborted"}, after_seq=after_seq) is False
    assert not [
        event for event in engine.store.read_all() if event.type == "run_finished"]

    # Drained -> the same gate now reaches its CAS, and the drain request is consumed.
    engine._eval_inflight.discard((0, 0))
    anyio.run(engine._drain_adopted_evals)
    assert engine._eval_drain_requested is False
    assert engine._finish_if_quiescent({"reason": "aborted"}, after_seq=after_seq) is True
    assert [event for event in engine.store.read_all() if event.type == "run_finished"]
    assert EV_NODE_EVALUATED  # registry constants, never literals (invariant #7)


def test_a_recurring_producer_yield_does_not_ping_pong_with_the_outer_loop(tmp_path, monkeypatch):
    """The bound on the fix: a hand-back is owed once per DEBT, not once per turn.

    `yield_outer` is set on a CONDITION — "no durable Card owns the next action and the raw lane has
    nothing" — which `_card_phase_request_build` re-derives every turn and which, during a long
    evaluation, usually answers the same way. Returning on every one of those would replace the
    barrier with a spin: the outer loop and a fresh session trading turns several times a second for
    the whole evaluation, each round trip a full `read_all()` + `fold()` of the run's entire log, on
    the network mount a run directory usually lives on. So the recurring yield is rate-limited on the
    log TAIL: the outer loop is owed a turn only when something it could act on has changed.

    Driven, not pinned: the second session is entered with the log byte-identical to where the first
    one handed back, and it must STAY OPEN rather than return again.
    """

    engine, _producer = _engine(tmp_path / "no-ping-pong", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    engine._spec_role_pair = None          # producer lane declines: exactly the mid-eval board state
    engine.role_factory = None

    release_slow = anyio.Event()
    running = anyio.Event()

    async def _slow_eval(admitted_id, _limiter, _max_es):
        running.set()
        await release_slow.wait()
        _terminalize(engine, admitted_id)

    monkeypatch.setattr(engine, "_evaluate", _slow_eval)
    second_returned = anyio.Event()

    async def _scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            try:
                # First session: admits the slow eval, the producer yields, and it hands back.
                with anyio.move_on_after(20):
                    await engine._run_card_session(
                        [], fold(engine.store.read_all()), None)
                assert running.is_set()
                assert engine._eval_inflight, "the slow eval must still be adopted"
                tail_after_first = engine.store.read_all()[-1].seq

                # Second session over an UNCHANGED log: the same yield, and no new debt.
                async def _second_session():
                    await engine._run_card_session(
                        [], fold(engine.store.read_all()), None)
                    second_returned.set()

                async with anyio.create_task_group() as probe:
                    probe.start_soon(_second_session)
                    await anyio.sleep(1.5)
                    if not engine._eval_inflight:          # defensive: the eval must still be live
                        raise AssertionError("the slow eval ended under the probe")
                    assert engine.store.read_all()[-1].seq == tail_after_first, (
                        "the probe session appended; this test no longer measures a repeat yield")
                    # THE ASSERTION. The producer's answer has not changed and neither has the log,
                    # so this session owes the outer loop nothing and must still be polling.
                    assert not second_returned.is_set(), (
                        "the session handed back for a producer yield the outer loop had already "
                        "been given — the outer loop and a fresh session now ping-pong, re-reading "
                        "and re-folding the whole log, for as long as the evaluation runs")
                    # Release, which both ends the eval and lets the second session close on a real
                    # debt: the terminal.
                    release_slow.set()
            finally:
                release_slow.set()

    anyio.run(_scenario)

    assert second_returned.is_set()
    # The rate limit must not become a NEW latch: the terminal that arrives when the slow eval is
    # released is a fresh debt, and the session closes on it.
    assert engine._eval_inflight == set()
    assert fold(engine.store.read_all()).nodes[0].status is NodeStatus.evaluated
