"""A paid deep-research think must SETTLE — the gate is spent only when the memo lands.

The concurrent-research seams (`orchestrator._spawn_research` one-shot, `_research_overlap_loop`
repeat) share the in-flight eval's task group, and that group is unwound as soon as the evals join
(`_dispatch_evals`/`speculation._run_card_session` `finally`) or when an eval raises. The paid pass
used to be THREE separate awaits — receipt (`research_attempted`, deliberately written BEFORE the
provider call), provider, record (`research_completed`) — so the unwind's cancellation landed on the
leading checkpoint of `anyio.to_thread.run_sync` for the RECORD hop. `CancelledError` is a
BaseException, so the seams' `except Exception` never saw it. The gate was spent, the provider was
paid AND waited for (the compute hop is non-abandonable), and the finished memo was dropped.

Measured live before the fix, `runs/live-cards-0804` (12 nodes, `deep_research_every=3`,
`concurrent_research_repeat=True`): 4 `research_attempted`, 0 `research_completed`, each attempt
followed ~0.2s later by the `node_evaluated` that cancelled it. Completion rate 0%, forever, on any
task whose evals are faster than the research call. `7a2a2ff4`'s "an interrupted think is simply
spent rather than re-paid" is the contract for a hard process KILL, not for the normal path.

The fix is sequencing, not shielding: `research_cadence._research_attempt_step` performs
receipt→provider→record as one SYNC unit and every background seam runs it through a single
non-abandonable `to_thread` hop. A worker thread has no cancellation points, so the cancel is
delivered only after the memo is durable. No new SHIELD is added: the eval-join already waited out
the compute hop (`abandon_on_cancel=False`) and then threw the answer away. What the join now also
waits for is the record — the same bounded work the loop already committed to whenever it reached
it: an optional verify call on the research endpoint's timeout, plus the appends.

These tests pin both halves of the operator-facing property: either the memo lands, or the gate was
never spent.
"""
from __future__ import annotations

import functools
import threading
import types

import anyio
import pytest

from looplab.core.models import ResearchMemo
from looplab.engine.orchestrator import Engine
from looplab.engine.research_cadence import ResearchCadenceMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

# Every wait below is bounded: a regression must fail the assertion, never hang the suite.
_BOUND = 10.0


def _seed(store: EventStore, nodes: int = 3) -> None:
    """A tiny finished run so `fold` gives the research cadence a real node-count to gate on."""
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    for node_id in range(nodes):
        store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": float(node_id)}}, "code": ""})
        store.append("node_evaluated", {
            "node_id": node_id, "metric": float(nodes - node_id), "eval_seconds": 0.1})


class _SlowResearcher:
    """A DeepResearcher whose provider call outlives the eval — the whole point of overlapping it."""

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started, self.release = started, release
        self.calls = 0

    def research(self, state, *, trigger: str) -> ResearchMemo:
        self.calls += 1
        self.started.set()
        assert self.release.wait(_BOUND), "test harness never released the provider call"
        return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                            summary="loss plateaus after warmup",
                            recommended_directions=["raise the warmup ratio"])


def _engine(store: EventStore, researcher) -> Engine:
    eng = Engine.__new__(Engine)
    eng.store = store
    eng.deep_researcher = researcher
    eng._research_verify = False          # no verifier client wired -> the record is appends only
    eng._track_hypotheses = True          # directions also register as open hypotheses
    eng._concurrent_research_max_calls = 1
    eng._concurrent_research_interval_s = 1.0
    return eng


def _types(store: EventStore) -> list[str]:
    return [e.type for e in store.read_all()]


# ------------------------------------------------------------------ repeat mode (the measured bug)

def test_fast_eval_join_still_lands_the_paid_memo(tmp_path):
    """THE REGRESSION. Repeat mode + a fast eval: the join cancels the overlap loop while the paid
    think is in flight. Real store, real receipt/compute/record — the memo must become durable and
    close its receipt, not vanish with the gate spent."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store)
    started, release = threading.Event(), threading.Event()
    eng = _engine(store, _SlowResearcher(started, release))

    async def drive():
        async with anyio.create_task_group() as bg_tg:
            bg_tg.start_soon(Engine._research_overlap_loop, eng, "cadence")
            # The "eval": it finishes the instant the paid think has started.
            await anyio.to_thread.run_sync(functools.partial(started.wait, _BOUND),
                                           abandon_on_cancel=True)
            bg_tg.cancel_scope.cancel()   # evals joined -> _dispatch_evals' `finally`
            release.set()                 # the provider answers AFTER the cancel: money already spent

    anyio.run(drive)

    kinds = _types(store)
    assert kinds.count("research_attempted") == 1
    assert kinds.count("research_completed") == 1, (
        "the paid think finished but its memo was discarded — the gate was burned for nothing")
    assert kinds.count("hint") == 1 and kinds.count("hypothesis_added") == 1

    state = fold(store.read_all())
    attempt_id = next(e.data["attempt_id"] for e in store.read_all()
                      if e.type == "research_attempted")
    assert attempt_id in state.research_attempts_completed   # receipt reconciled, not outstanding
    assert [m["summary"] for m in state.research] == ["loss plateaus after warmup"]
    assert Engine._already_researched_at(state, len(state.nodes))   # gate spent WITH something to show


def test_the_split_paid_pass_is_what_burned_the_gate(tmp_path):
    """Mechanism witness: run the OLD three-await shape against the same harness and watch the memo
    disappear. This is the failure `runs/live-cards-0804` recorded 4 times. It also proves the
    harness above really delivers the cancel mid-think (otherwise this would not lose anything)."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store)
    started, release = threading.Event(), threading.Event()
    eng = _engine(store, _SlowResearcher(started, release))

    async def _split_pass():
        # Verbatim shape of the pre-fix `_spawn_research._bg` / overlap-loop body.
        snap = fold(store.read_all())
        try:
            attempt = await anyio.to_thread.run_sync(
                functools.partial(eng._record_research_attempt, snap,
                                  trigger="cadence", manual=False))
            memo = await anyio.to_thread.run_sync(
                functools.partial(eng._compute_deep_research, snap, "cadence", trace=False))
            await anyio.to_thread.run_sync(
                functools.partial(eng._record_deep_research, memo, trigger="cadence",
                                  manual=False, attempt_id=attempt))
        except Exception:  # noqa: BLE001 — the seams' guard, blind to BaseException CancelledError
            pass

    async def drive():
        async with anyio.create_task_group() as bg_tg:
            bg_tg.start_soon(_split_pass)
            await anyio.to_thread.run_sync(functools.partial(started.wait, _BOUND),
                                           abandon_on_cancel=True)
            bg_tg.cancel_scope.cancel()
            release.set()

    anyio.run(drive)

    kinds = _types(store)
    assert kinds.count("research_attempted") == 1    # the gate was spent
    assert kinds.count("research_completed") == 0    # ... and the paid answer was thrown away
    state = fold(store.read_all())
    assert state.research == [] and state.research_attempts_completed == set()
    # And the spent-for-nothing receipt still closes the cadence gate, so the run never re-thinks.
    assert Engine._already_researched_at(state, len(state.nodes))


# --------------------------------------------------------------- one-shot mode (a raising sibling)

def test_one_shot_research_lands_when_a_sibling_eval_unwinds_the_group(tmp_path):
    """One-shot mode leaves `bg_tg` uncancelled by the join, but an eval that RAISES still unwinds
    the shared group and cancels `_bg`. The paid memo must survive that too."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store)
    started, release = threading.Event(), threading.Event()
    eng = _engine(store, _SlowResearcher(started, release))
    eng.concurrent_research = True
    eng._concurrent_research_repeat = False
    eng._due_research_trigger = lambda _state: "cadence"
    snap = fold(store.read_all())

    async def drive():
        # anyio re-raises the body's error as a group once the children have been unwound.
        with pytest.raises(BaseExceptionGroup) as caught:
            async with anyio.create_task_group() as tg:
                eng._spawn_research(tg, snap)
                await anyio.to_thread.run_sync(functools.partial(started.wait, _BOUND),
                                               abandon_on_cancel=True)
                release.set()
                raise RuntimeError("eval blew up")
        assert [str(exc) for exc in caught.value.exceptions] == ["eval blew up"]

    anyio.run(drive)

    kinds = _types(store)
    assert kinds.count("research_attempted") == 1
    assert kinds.count("research_completed") == 1
    state = fold(store.read_all())
    assert len(state.research_attempts_completed) == 1


def test_a_cancel_before_the_paid_step_spends_nothing(tmp_path):
    """The other half of the property: when the group is unwound BEFORE the think starts, the
    receipt is never written, so the trigger is free to fire again. `to_thread.run_sync` takes its
    checkpoint before claiming a worker, which is exactly where this cancel lands."""
    store = EventStore(tmp_path / "events.jsonl")
    _seed(store)
    started, release = threading.Event(), threading.Event()
    researcher = _SlowResearcher(started, release)
    eng = _engine(store, researcher)
    eng.concurrent_research = True
    eng._concurrent_research_repeat = False
    eng._due_research_trigger = lambda _state: "cadence"
    snap = fold(store.read_all())

    async def drive():
        async with anyio.create_task_group() as tg:
            eng._spawn_research(tg, snap)
            tg.cancel_scope.cancel()       # the eval was aborted before research got a turn

    anyio.run(drive)

    assert researcher.calls == 0
    assert "research_attempted" not in _types(store)
    state = fold(store.read_all())
    assert not Engine._already_researched_at(state, len(state.nodes))   # gate NOT spent
    assert eng._due_research_trigger(state) == "cadence"                # it may legitimately re-fire


# --------------------------------------------------------------------------- indivisibility guards

def test_the_paid_step_is_sync_so_one_thread_hop_covers_it():
    import inspect
    step = ResearchCadenceMixin._research_attempt_step
    assert not inspect.iscoroutinefunction(step), (
        "a coroutine could be cancelled between the receipt and the record — that IS the bug")


def test_the_paid_step_records_under_the_same_call_that_receipts():
    """Receipt and record must be observed as one unit: no caller can see the gate spent without the
    memo attempt having been made and (when it produced anything) recorded."""
    seen: list[str] = []

    class _Host(ResearchCadenceMixin):
        def _record_research_attempt(self, state, *, trigger, manual):
            seen.append("attempt")
            return "a1"

        def _compute_deep_research(self, state, trigger, *, trace=True):
            seen.append("compute")
            return types.SimpleNamespace(summary="s", recommended_directions=["d"])

        def _record_deep_research(self, memo, *, trigger, manual, attempt_id=None):
            seen.append(f"record:{attempt_id}")

    sig, recorded = _Host()._research_attempt_step(object(), "cadence")
    assert seen == ["attempt", "compute", "record:a1"]
    assert recorded is True and isinstance(sig, str)


def test_a_converged_repeat_pass_skips_the_record_without_spending_a_gate():
    """The repeat cadence's convergence skip is not a burn: `repeat` passes are unreceipted, so
    declining to re-record identical conclusions spends nothing."""
    memo = types.SimpleNamespace(summary="same", recommended_directions=["same"])

    class _Host(ResearchCadenceMixin):
        def __init__(self):
            self.records = 0
            self.attempts: list[str] = []

        def _record_research_attempt(self, state, *, trigger, manual):
            self.attempts.append(trigger)
            return None if trigger == "repeat" else "a1"

        def _compute_deep_research(self, state, trigger, *, trace=True):
            return memo

        def _record_deep_research(self, memo, *, trigger, manual, attempt_id=None):
            self.records += 1

    host = _Host()
    first_sig, recorded = host._research_attempt_step(object(), "cadence")
    assert recorded is True and host.records == 1
    again_sig, recorded = host._research_attempt_step(object(), "repeat", last_sig=first_sig)
    assert again_sig == first_sig and recorded is False and host.records == 1
    assert host.attempts == ["cadence", "repeat"]   # only the gated trigger ever claims a receipt
