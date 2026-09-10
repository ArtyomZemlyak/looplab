"""The build fan-out refills a lane instead of joining a chunk (doc 52 row 33).

`_handle_create_actions`' parallel build is a bulk-synchronous barrier: `_fan` builds start together
and NOTHING moves until the slowest finishes, so the loop pays the maximum of every chunk instead of
its mean and a fast worker cannot propose from a completed sibling's evidence. AIRA₂ dispatches into
a pool as soon as any worker is free. `Settings.steady_state_build` is that shape.

The headline test DRIVES the difference rather than asserting about the source: four builds, two
lanes, and the first build made slow on purpose. Under the barrier the third build cannot start
until the slow one ends; under the lane it starts as soon as the FAST one does. The same fixture run
with the flag off reproduces the barrier, so the test would fail if the flag did nothing.
"""
from __future__ import annotations

import time

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.config import Settings
from looplab.engine.options import EngineOptions
from tests.factories import TOY_TASK, make_engine


def _timed_engine(run_dir, *, steady: bool, slow_first: float = 0.35):
    """A 2-wide fan-out whose FIRST build is slow, recording every build's start and end."""
    task = ToyTask.load(TOY_TASK)
    engine = make_engine(run_dir, task=task, n_seeds=4, max_nodes=4,
                         steady_state_build=steady)
    engine.parallel_build = 2
    engine.role_factory = task.build_roles
    timeline: list[tuple[str, float]] = []
    real = engine._create_node_guarded

    def _timed(action, pair, reservation, idea, telemetry=None):
        index = len([row for row in timeline if row[0] == "start"])
        timeline.append(("start", time.monotonic()))
        if index == 0 and slow_first:
            time.sleep(slow_first)          # the slowest member of the first chunk
        else:
            time.sleep(0.02)
        try:
            return real(action, pair, reservation, idea, telemetry)
        finally:
            timeline.append(("end", time.monotonic()))

    engine._create_node_guarded = _timed
    return engine, timeline


def _starts_and_ends(timeline):
    starts = [t for kind, t in timeline if kind == "start"]
    ends = [t for kind, t in timeline if kind == "end"]
    return starts, ends


@pytest.mark.parametrize("steady,expected", [(True, "refills"), (False, "waits")])
def test_a_slow_build_blocks_the_next_dispatch_only_under_the_barrier(tmp_path, steady, expected):
    """THE PROPERTY, driven: with two lanes and a slow first build, does the THIRD build start
    before the slow one finishes?"""
    engine, timeline = _timed_engine(tmp_path / expected, steady=steady)
    anyio.run(engine.run)
    starts, ends = _starts_and_ends(timeline)
    if len(starts) < 3:
        pytest.skip(f"the run built {len(starts)} node(s) — too few to observe the refill")
    first_end = ends[0] if ends else None
    assert first_end is not None
    if steady:
        # The third dispatch happened while the slow first build was still running.
        assert starts[2] < max(ends[:2]), (
            "the lane did not refill: the third build waited for the slowest of the first two")
    else:
        # The barrier: nothing starts until the whole chunk has joined.
        assert starts[2] > max(ends[:2]), (
            "the default path is no longer a barrier — this test's contrast is gone")


def test_the_flag_is_off_by_default_everywhere():
    assert Settings().steady_state_build is False
    assert EngineOptions().steady_state_build is False
    assert EngineOptions.from_settings(Settings()).steady_state_build is False


def test_the_lane_run_produces_the_same_kind_of_record_as_the_barrier(tmp_path):
    """Same reservations, same terminals, same fold: what moved is WHEN a build starts, not what a
    build writes. Every node still has its `node_building` receipt before its `node_created`."""
    engine, _ = _timed_engine(tmp_path / "record", steady=True, slow_first=0.0)
    anyio.run(engine.run)
    events = engine.store.read_all()
    building = [e.data["node_id"] for e in events if e.type == "node_building"]
    created = [e.data["node_id"] for e in events if e.type == "node_created"]
    assert created, "the lane built nothing"
    for node_id in created:
        assert node_id in building, f"node {node_id} was created with no reservation receipt"
    # ids are still minted serially and never reused
    assert len(set(building)) == len(building)


def test_the_lane_joins_before_the_turn_ends(tmp_path):
    """No build may outlive the turn that started it — the invariant the barrier's join provided
    and the pool's `async with` still does."""
    engine, timeline = _timed_engine(tmp_path / "join", steady=True, slow_first=0.05)
    anyio.run(engine.run)
    starts, ends = _starts_and_ends(timeline)
    assert len(starts) == len(ends), "a build was still running when the run returned"


def test_the_pause_breaker_stops_the_lane_from_starting_more_work(tmp_path):
    """A developer/build crash pauses the create path; the lane must stop DISPATCHING inside the
    turn, which is the guarantee the barrier gave by checking after each chunk.

    Stated as "no build starts between the pause being raised and the turn draining it", because
    that is the property — the drain clears the flag and the NEXT turn is allowed to build again,
    exactly as the barrier path behaves.
    """
    engine, timeline = _timed_engine(tmp_path / "paused", steady=True, slow_first=0.0)
    real_build, real_drain = engine._create_node_guarded, engine._drain_create_pause
    raised: list[float] = []
    drained: list[float] = []

    def _pause_after_one(action, pair, reservation, idea, telemetry=None):
        out = real_build(action, pair, reservation, idea, telemetry)
        if not raised:
            engine._create_paused = True
            raised.append(time.monotonic())
        return out

    def _drain():
        drained.append(time.monotonic())
        return real_drain()

    engine._create_node_guarded = _pause_after_one
    engine._drain_create_pause = _drain
    anyio.run(engine.run)
    assert raised and drained, "the pause was never raised or never drained"
    starts, _ = _starts_and_ends(timeline)
    window = [t for t in starts if raised[0] < t < drained[0]]
    assert not window, f"{len(window)} build(s) started after the pause and before the drain"


def _drop_one_per_proposal(engine, dropped_ideas: list):
    """Make every batch proposal reject one idea beside the one it returns — the shape whose
    node-less Cards the SUCCESS path is responsible for recording."""
    from looplab.core.models import Idea
    real = engine._await_batch_proposal

    async def _with_a_drop(state, width):
        ideas, telemetry, _dropped = await real(state, width)
        if not ideas:
            return ideas, telemetry, _dropped
        rejected = Idea(hypothesis=f"rejected-{len(dropped_ideas)}",
                        rationale="a near-duplicate", operator="tweak", params={})
        dropped_ideas.append(rejected.hypothesis)
        engine._pending_batch_dropped = [{"idea": rejected, "reason": "semantic_duplicate"}]
        return ideas, telemetry, [{"idea": rejected, "reason": "semantic_duplicate"}]

    engine._await_batch_proposal = _with_a_drop


def test_a_reject_beside_an_accepted_idea_still_gets_its_node_less_card(tmp_path):
    """THE SUCCESS PATH OWNS THE DROPS TOO. The two failure paths (no ideas, a degraded fallback)
    record them; a lane that proposed successfully used to return without ever calling
    `_record_dropped_batch_cards`, so a reject that shared its turn with an accepted idea vanished
    from the Card board entirely — the one case where the board is meant to say what was refused
    and why. Driven through the real lane, counting the closed Cards the run actually wrote."""
    engine, _timeline = _timed_engine(tmp_path / "drops", steady=True, slow_first=0.0)
    dropped_ideas: list[str] = []
    recorded: list = []
    real_record = engine._record_dropped_batch_cards

    def _count(dropped):
        recorded.extend(d.get("reason") for d in (dropped or []) if isinstance(d, dict))
        return real_record(dropped)

    engine._record_dropped_batch_cards = _count
    _drop_one_per_proposal(engine, dropped_ideas)
    anyio.run(engine.run)
    assert dropped_ideas, "the fixture never produced a reject"
    assert recorded.count("semantic_duplicate") == len(dropped_ideas), (
        f"{len(dropped_ideas)} reject(s) proposed, {recorded.count('semantic_duplicate')} recorded")


def test_the_lane_spends_the_batch_capabilities_before_the_next_proposal(tmp_path):
    """`_pending_batch_novelty_gated` is a ONE-SHOT gate bypass keyed on object identity, and the
    lane reserves every Idea it accepts — so leaving the list populated carries an already-built
    proposal into the next iteration as a live bypass. The chunked path clears both lists once its
    reservations are durable; this asserts the lane does too, observed at each proposal."""
    engine, _timeline = _timed_engine(tmp_path / "spend", steady=True, slow_first=0.0)
    real = engine._await_batch_proposal
    seen_at_entry: list[int] = []

    async def _observe(state, width):
        seen_at_entry.append(len(getattr(engine, "_pending_batch_novelty_gated", None) or []))
        return await real(state, width)

    engine._await_batch_proposal = _observe
    anyio.run(engine.run)
    assert len(seen_at_entry) > 1, "only one proposal ran — the property is about the NEXT one"
    assert seen_at_entry[1:] == [0] * len(seen_at_entry[1:]), (
        f"a spent capability survived into a later proposal: {seen_at_entry}")


def test_no_lane_ever_holds_the_PRIMARY_role_pair(tmp_path):
    """The fourth invariant the barrier was protecting, and the one the lane forgot.

    `_build_role_pairs` returns `[(self.researcher, self.developer)] + pool`, so pair 0 IS the
    primary pair. Under the barrier that was safe — propose-all, build-all, join, so no proposal
    ever ran while a build held those objects. This lane exists to remove that join, so from the
    moment pair 0 is leased the main task's next `_await_batch_proposal` runs
    `self.researcher.propose()` on an object a lane is building with, and under the shipped
    `unified_agent` the same object is the developer too.

    What that costs, driven before the fix: a build's own `last_foresight` read back as None
    because a concurrent proposal's `finally` had nulled it — `foresight_selected` silently never
    written for that node — and, in the mirror order, one node's ranking stamped onto the next.
    That is exactly the mis-attribution the per-build pooled roles exist to prevent
    (`engine/audit.py`: "read THIS build's pooled researcher so a concurrent sibling's prediction
    is not cross-wired onto this node").

    `speculation.py::_producer_role_pair` already refuses a pair equal to the primary and says why.
    This asserts the same of every lane the steady path dispatches, by recording what each build
    was actually handed.
    """
    task = ToyTask.load(TOY_TASK)
    engine = make_engine(tmp_path / "run", task=task, n_seeds=4, max_nodes=4,
                         steady_state_build=True)
    engine.parallel_build = 2
    engine.role_factory = task.build_roles

    leased: list[tuple] = []
    handed: list[list] = []
    real = engine._create_node_guarded
    real_lane = engine._steady_state_build_lane

    def _record(action, pair, reservation, idea, telemetry=None):
        leased.append(pair)
        return real(action, pair, reservation, idea, telemetry)

    async def _watch_lane(creates, state, pairs):
        handed.append(list(pairs))
        return await real_lane(creates, state, pairs)

    engine._create_node_guarded = _record
    engine._steady_state_build_lane = _watch_lane
    anyio.run(engine.run)

    assert leased, "no build ran — the fixture no longer drives the lane"
    assert handed, "the steady lane never ran — the fixture no longer drives it"
    for pair in leased:
        assert pair is not None and isinstance(pair, tuple) and len(pair) == 2, pair
        assert pair[0] is not engine.researcher, (
            "a lane was handed the PRIMARY researcher while the main task keeps proposing on it")
        assert pair[1] is not engine.developer, (
            "a lane was handed the PRIMARY developer while the main task keeps proposing on it")

    # …and the lane is still a LANE. The fix mints one extra POOLED pair rather than dropping one,
    # so the width the operator asked for survives; simply excluding pair 0 would leave a single
    # lane at the common `parallel_build=2` and silently turn the flag into the barrier it
    # replaces. Asserted on what the lane was HANDED, not on what scheduling happened to use —
    # `free_pairs` reuses a returned pair, so a short run can legitimately run every build on one.
    for pairs in handed:
        assert len(pairs) >= 2, (
            f"the steady lane was handed {len(pairs)} pair(s) — it is the barrier again, and the "
            "flag is inert at the width an operator most often sets")
        for pair in pairs:
            assert pair[0] is not engine.researcher and pair[1] is not engine.developer, (
                "the primary pair reached the lane's leasable set")
