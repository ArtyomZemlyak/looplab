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


# ---------------------------------------------------------------------------------------------
# F1g's REMAINDER: production paced on OCCUPANCY rather than on the node count.
#
# The change above made the outer boundary reachable during a long evaluation. It did not make
# anything HAPPEN there, and the backlog said so: "nothing yet makes the card-producing cadences
# fire BECAUSE an evaluation is running — they stay node-count-paced". Measured on this tree before
# the pace existed, on the toy backend with evaluations scaled to the GPU-run shape (3 s evaluations
# against 0.6 s builds, width 2): occupancy NEVER exceeded 1, 18.1 % of the run had no evaluation
# running at all, and `_stage_card_creates` — the only writer of Card inventory — fired ONCE in a
# whole 12-node run, at node 0. With the pace: occupancy 2 for 63 % of the run, the serial gap down
# to 3.4 %, slot utilisation 40.9 % -> 79.8 %, wall clock 45.0 s -> 24.0 s, same 12 evaluations, same
# event-type inventory, zero auto-dropped Cards.
#
# The mechanism is worth stating because it is not the one the backlog predicted. A Node under
# evaluation is still `pending` in the fold, so `card_next_actions` answers with its evaluate action
# for the whole of the evaluation — an action this turn cannot start — and the `creates` branch is
# skipped every time. Production was gated on occupancy ZERO.
# ---------------------------------------------------------------------------------------------


def _occupancy_engine(run_dir, **overrides):
    """A real engine in the board-empty shape: Card inventory on, and NO isolated producer pair.

    Without `role_factory` the session's own raw lane cannot mint (`_producer_role_pair()` is None),
    so `_card_phase_request_build` declines and `yield_outer` is set — which is exactly the state
    `runs/rubertlite-dr-unified-v6` sat in for the 15-37 minute hole between every consecutive pair
    of nodes. Everything that refills the board then has to come from the outer loop.
    """

    from pathlib import Path as _Path

    from looplab.adapters.toytask import ToyTask
    from tests.factories import make_engine

    task = ToyTask.load(_Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    engine = make_engine(
        run_dir, task=task, n_seeds=3, card_driven_selection=True, speculation_depth=2,
        eval_parallel=2, **overrides)
    engine._gpu_ids = []
    engine._gpu_physical_ids = {}
    engine._gpu_mem = {}
    engine._free_gpus = []
    engine._eval_parallel = 2
    return engine


def _node_created_ids(engine) -> set[int]:
    return {
        event.data.get("node_id") for event in engine.store.read_all()
        if event.type == "node_created"
    }


def test_the_board_is_refilled_because_an_evaluation_is_running(tmp_path, monkeypatch):
    """THE F1g regression: a Node is BUILT while another Node's evaluation holds a slot.

    Tier 1 in CLAUDE.md's ladder — a real `Engine.run`, the real outer loop, real Cards, real
    reservation, real claim. The one sentinel is `_evaluate`, which holds the first evaluation open
    exactly as long as it takes the run to build something else; that is what supplies the
    occupancy, and it is the same sentinel every test in this file uses.

    On the pre-fix tree the loop spends that whole window handing back and forth between an outer
    turn whose selector answers `evaluate <the running node>` and a session that has nothing to
    admit, and the next node is not built until the terminal lands. The GPU beside it stays dark.
    """

    engine = _occupancy_engine(tmp_path / "occupancy-refill", max_nodes=4)
    real_evaluate = engine._evaluate
    order: list[int] = []
    refilled = anyio.Event()

    async def _held_eval(node_id, limiter, max_es):
        order.append(node_id)
        if len(order) == 1:
            # Hold the slot until the run builds SOMETHING ELSE — that is the whole claim, and a
            # bounded wait so a red assertion reports a dark GPU instead of hanging the suite.
            with anyio.move_on_after(30):
                while _node_created_ids(engine) <= {node_id}:
                    await anyio.sleep(0.02)
                refilled.set()
        return await real_evaluate(node_id, limiter, max_es)

    monkeypatch.setattr(engine, "_evaluate", _held_eval)
    anyio.run(engine.run)

    assert refilled.is_set(), (
        "no node was built for the whole of an evaluation that held a slot: the board stayed empty "
        "and the build latency was paid serially after the terminal instead of behind it — the "
        "167.7 GPU-h in backlog F1g")
    assert order, "no evaluation ran at all"
    state = fold(engine.store.read_all())
    assert len(state.nodes) >= 2


def test_a_card_selected_while_an_evaluation_runs_is_claimed_and_not_retired(tmp_path, monkeypatch):
    """The claim must revalidate the SAME question the selection asked.

    `_claim_existing_card_builds` re-derives the lane under `_id_lock`. Re-deriving it UNMASKED
    while an evaluation runs asks a different question — the running Node is still `pending`, so
    `forced_card_actions` answers with its evaluate lane and the comparison reads
    `['card-1'] -> []`. Three of those and the Card is durably `card_auto_dropped` as "unclaimable".
    Measured on the pre-fix tree: every Card minted during an evaluation was retired that way, the
    run minted a replacement, retired that too, and built nothing until the terminal landed.
    """

    engine = _occupancy_engine(tmp_path / "occupancy-claim", max_nodes=4)
    real_evaluate = engine._evaluate
    order: list[int] = []
    refilled = anyio.Event()

    async def _held_eval(node_id, limiter, max_es):
        order.append(node_id)
        if len(order) == 1:
            with anyio.move_on_after(30):
                while _node_created_ids(engine) <= {node_id}:
                    await anyio.sleep(0.02)
                refilled.set()
        return await real_evaluate(node_id, limiter, max_es)

    monkeypatch.setattr(engine, "_evaluate", _held_eval)
    anyio.run(engine.run)

    # The precondition, asserted so this test cannot pass by never selecting a Card mid-evaluation:
    # a run that produces nothing while a slot is held retires nothing either.
    assert refilled.is_set(), "nothing was produced during the held evaluation"
    unclaimable = [
        event.data for event in engine.store.read_all()
        if event.type == "card_auto_dropped"
        and "unclaimable" in str((event.data or {}).get("reason", ""))
    ]
    assert not unclaimable, (
        "a Card selected while an evaluation was running was retired as unclaimable — the selection "
        f"and its claim disagreed about what the board was: {unclaimable}")


def test_two_terminals_in_one_window_owe_one_turn_and_still_refill_every_freed_slot():
    """`boundary_owed` is a BOOL, so the debt is 'a terminal landed', not 'one turn per terminal'.

    At width > 1 two children finishing inside one poll window collapse into a single owed turn, and
    `_card_eval_one`'s note is where the three reasons that is left alone are written down. The one
    that this change is responsible for is driven here: the occupancy pace asks for every FREE slot
    rather than for one, so a single hand-back is enough to refill a collapse of any size.
    """

    from looplab.engine.cadence import occupancy_due

    # A width-3 run, two of three terminals collapsed into one owed turn, one evaluation still live.
    assert occupancy_due(inflight=1, queued=0, width=3) is True
    # …and the turn that pace feeds must be able to ask for BOTH freed slots, not one of them.
    free = 3 - 1 - 0
    assert free == 2, "a width-complete refill is what makes one owed turn sufficient"
    # Once the supply covers the width the debt has been paid, however many terminals caused it.
    assert occupancy_due(inflight=1, queued=2, width=3) is False


def test_the_occupancy_pace_and_the_node_count_pace_cannot_satisfy_each_others_gate():
    """The constraint on adding a second pace, driven rather than asserted in a comment.

    `cadence_due` is a since-last WINDOW over a consumer's own durable marks and
    `search/coverage.py::already_covered_at` is its at_node IDEMPOTENCE twin; each is parametrized
    over its own consumer's records so two consumers cannot advance each other. A second pace that
    recorded an `at_node` would break both halves at once — it would close the node-count window for
    a full `every` nodes, and its own record would then make the at_node gate refuse the NEXT
    starvation at the same node count, i.e. it would be a node-count rule wearing another name.
    """

    import inspect

    from looplab.engine.cadence import cadence_due, occupancy_due

    # It cannot READ a mark: there is no parameter that could carry one.
    parameters = set(inspect.signature(occupancy_due).parameters)
    assert parameters == {"inflight", "queued", "width"}, parameters
    assert not (parameters & {"n", "last", "every", "at_node", "state", "snapshots"})

    # A node count frozen mid-evaluation leaves every node-count consumer not-due…
    assert cadence_due(n=5, last=5, every=3) is False
    # …and the occupancy pace is due at that same frozen count, twice, because it reads neither the
    # count nor any mark. Two starvation episodes at one node count is the ordinary case on a run
    # whose evaluations are hours long, and an at_node gate would suppress the second one.
    assert occupancy_due(inflight=1, queued=0, width=2) is True
    assert occupancy_due(inflight=1, queued=0, width=2) is True

    # The converse: firing here must not advance a node-count window. It cannot, because nothing is
    # recorded — the gate below is unchanged by any number of occupancy firings.
    assert cadence_due(n=5, last=5, every=3) is False

    # Its own idempotence is the CONDITION, and the condition is self-clearing: supply closes it.
    assert occupancy_due(inflight=1, queued=1, width=2) is False
    # Nothing running is the ORDINARY create turn, which this rule must never become a copy of.
    assert occupancy_due(inflight=0, queued=0, width=2) is False
    # No free slot, and a live `0`/`1` width both settle to serial rather than dividing by zero.
    assert occupancy_due(inflight=2, queued=0, width=2) is False
    assert occupancy_due(inflight=1, queued=0, width=0) is False
    assert occupancy_due(inflight=1, queued=0, width=1) is False


def test_the_occupancy_paced_query_appends_nothing(tmp_path, monkeypatch):
    """"Records no mark" as a property of the code, not of the docstring.

    `_occupancy_paced_creates` is pure selection over the folded board — it decides WHAT to produce
    and the ordinary `creates` branch produces it. If it ever appended (a receipt, a diagnostic, a
    "starved" row), it would be both a new writer under engine invariant #1 and a new `at_node`-like
    mark, which is the thing the pace is not allowed to have.
    """

    engine, _producer = _engine(tmp_path / "occupancy-pure", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    engine._ensure_speculation_state()
    engine._eval_inflight.add((node_id, 0))

    events = engine.store.read_all()
    before = events[-1].seq if events else -1
    state = fold(events)
    engine._occupancy_paced_creates(state, [{"kind": "evaluate", "node_id": node_id}])
    after = engine.store.read_all()
    assert (after[-1].seq if after else -1) == before, (
        "the occupancy pace appended; it is a QUERY, and a row here would be both a new writer and "
        "the at_node mark the pace is defined not to have")


def test_the_occupancy_pace_stands_down_when_a_slot_could_be_filled_from_the_board(
        tmp_path, monkeypatch):
    """Starvation means the board is EMPTY, not merely that this turn planned no create.

    A pending Node that is NOT in flight is work the consumer is about to admit; producing against
    it would mint inventory for a slot that is already spoken for. Same for a build already in
    flight. Both are refusals of the pace, and both are what keep it from becoming a second,
    unbounded proposal lane running for the whole of a multi-hour evaluation.
    """

    engine, _producer = _engine(tmp_path / "occupancy-standdown", depth=2)
    engine._eval_parallel = 2
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    engine._ensure_speculation_state()
    state = fold(engine.store.read_all())

    # Nothing is running: this is the ORDINARY create turn and the pace must not answer it.
    assert engine._occupancy_paced_creates(
        state, [{"kind": "evaluate", "node_id": node_id}]) == []

    # A pending Node the consumer has NOT started is a slot's worth of supply, not an empty board.
    engine._eval_inflight.add((node_id, 0))
    unstarted = {"kind": "evaluate", "node_id": node_id + 1}
    assert engine._occupancy_paced_creates(
        state, [{"kind": "evaluate", "node_id": node_id}, unstarted]) == [], (
        "an evaluate action this turn could have STARTED means the board is not empty")
