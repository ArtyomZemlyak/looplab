"""Repeated concurrent deep-research (`_research_overlap_loop`) — keep the reasoning agents busy for
the WHOLE eval window instead of idling a multi-day training after one memo. These tests pin the pure
pieces (content signature, adaptive cadence) and drive the loop itself through a light stub host so no
real Engine/LLM is needed. The loop is advisory-only (records via the BACKGROUND_APPENDABLE path), so
none of this touches folded selection or replay."""
import threading
import types

import anyio

from looplab.engine.orchestrator import Engine
from looplab.engine.research_cadence import research_memo_sig


def _memo(summary, directions=()):
    return types.SimpleNamespace(summary=summary, recommended_directions=list(directions),
                                 at_node=0, trigger="repeat")


# --------------------------------------------------------------------------- research_memo_sig (pure)

def test_memo_sig_is_stable_and_content_addressed():
    a = _memo("loss plateaus", ["try warmup", "lower LR"])
    b = _memo("loss plateaus", ["try warmup", "lower LR"])
    assert research_memo_sig(a) == research_memo_sig(b)          # identical content -> identical sig


def test_memo_sig_changes_with_summary_or_directions():
    base = _memo("loss plateaus", ["try warmup"])
    assert research_memo_sig(base) != research_memo_sig(_memo("loss diverges", ["try warmup"]))
    assert research_memo_sig(base) != research_memo_sig(_memo("loss plateaus", ["try warmup", "more heads"]))


def test_memo_sig_accepts_dict_payload_equivalently():
    ns = _memo("s", ["d1", "d2"])
    d = {"summary": "s", "recommended_directions": ["d1", "d2"]}
    assert research_memo_sig(ns) == research_memo_sig(d)         # attr and dict access agree


def test_memo_sig_ignores_whitespace_only_directions():
    assert research_memo_sig(_memo("s", ["d", "  "])) == research_memo_sig(_memo("s", ["d"]))


# --------------------------------------------------------------------- adaptive repeat cadence (pure)

class _CadenceHost:
    def __init__(self, cfg, budget):
        self._concurrent_research_interval_s = cfg
        self._budget = budget

    def _experiment_time_budget(self):
        return self._budget


def test_repeat_cadence_uses_config_floor_when_no_budget():
    assert Engine._research_repeat_cadence(_CadenceHost(1800.0, None)) == 1800.0


def test_repeat_cadence_config_is_a_floor_never_more_often():
    # A short budget derives a small pace, but research is expensive -> never faster than the config floor.
    assert Engine._research_repeat_cadence(_CadenceHost(1800.0, 600.0)) == 1800.0


def test_repeat_cadence_stretches_on_a_multi_day_budget():
    # A two-day eval: derived = clamp(172800*0.05=8640, 300, 3600) = 3600 -> re-research ~hourly.
    assert Engine._research_repeat_cadence(_CadenceHost(1800.0, 172800.0)) == 3600.0


def test_repeat_cadence_takes_the_larger_of_floor_and_derived():
    # budget 40000 -> derived = clamp(2000, 300, 3600) = 2000 > 1800 floor.
    assert Engine._research_repeat_cadence(_CadenceHost(1800.0, 40000.0)) == 2000.0


# ------------------------------------------------------------------------ the loop (stub-driven)

class _LoopStub:
    """Minimal host for `Engine._research_overlap_loop` (called as an unbound method with this as
    `self`). Serves a fixed memo sequence, records via a list, and uses a tiny cadence."""
    def __init__(self, memos, *, cap=0, cadence=0.01):
        self._memos = list(memos)
        self._concurrent_research_max_calls = cap
        self._cadence = cadence
        self.compute_calls = 0
        self.recorded = []
        self.store = types.SimpleNamespace(read_all=lambda: [])

    def _research_repeat_cadence(self):
        return self._cadence

    def _compute_deep_research(self, state, trig, *, trace=True):
        m = self._memos[min(self.compute_calls, len(self._memos) - 1)]
        self.compute_calls += 1
        return m

    def _record_deep_research(self, memo, *, trigger, manual):
        self.recorded.append((research_memo_sig(memo), trigger))


async def _cancel_blocked_paid_task(task, started, release, worker_finished):
    """Cancel ``task`` while its paid sync worker is blocked and report an early detach.

    The timer is only a deadlock breaker for the intentionally blocked worker; synchronization uses
    events. An abandoned host exits before release, while an owned host cannot exit until the timer
    releases the worker. Always join the probe worker so a failing regression test leaks nothing.
    """
    release_timer = threading.Timer(0.25, release.set)
    timer_started = False
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(task)
            await anyio.to_thread.run_sync(started.wait, abandon_on_cancel=True)
            release_timer.start()
            timer_started = True
            tg.cancel_scope.cancel()
        detached = not worker_finished.is_set()
    finally:
        started.set()
        release.set()
        release_timer.cancel()
        if timer_started:
            release_timer.join()
    await anyio.to_thread.run_sync(worker_finished.wait)
    return detached


def test_repeat_mode_does_not_start_without_a_due_trigger():
    # `deep_research_every=0` is the shipped manual-only contract. Turning repeat on must not turn
    # that disabled auto cadence into a hidden paid timer for every long-running evaluation.
    class _TaskGroupProbe:
        def __init__(self):
            self.started = []

        def start_soon(self, func, *args):
            self.started.append((func, args))

    state = types.SimpleNamespace(nodes={0: object()}, research=[], strategy_history=[])
    host = types.SimpleNamespace(
        concurrent_research=True,
        _concurrent_research_repeat=True,
        deep_researcher=object(),
        deep_research_every=0,
        _already_researched_at=lambda _state, _n: False,
        _cadence_research_memos=lambda _state: [],
        _cadence_due=Engine._cadence_due,
        _research_overlap_loop=lambda _trigger: None,
    )
    host._due_research_trigger = lambda current: Engine._due_research_trigger(host, current)
    assert host._due_research_trigger(state) is None
    tg = _TaskGroupProbe()
    Engine._spawn_research(host, tg, state)
    assert tg.started == []


def test_repeat_mode_forwards_the_due_trigger_once():
    class _TaskGroupProbe:
        def __init__(self):
            self.started = []

        def start_soon(self, func, *args):
            self.started.append((func, args))

    async def loop(_trigger):
        return None

    host = types.SimpleNamespace(
        concurrent_research=True,
        _concurrent_research_repeat=True,
        deep_researcher=object(),
        _due_research_trigger=lambda _state: "strategist",
        _research_overlap_loop=loop,
    )
    tg = _TaskGroupProbe()
    Engine._spawn_research(host, tg, types.SimpleNamespace())
    assert tg.started == [(loop, ("strategist",))]


def test_loop_records_new_memos_and_skips_identical_reruns():
    # A -> B -> B -> B -> B: A and B each record once; the converged B re-runs are skipped.
    a, b = _memo("A", ["x"]), _memo("B", ["y"])
    stub = _LoopStub([a, b, b, b, b], cap=5)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert stub.compute_calls == 5                              # cap reached -> loop returned on its own
    assert [sig for sig, _t in stub.recorded] == [research_memo_sig(a), research_memo_sig(b)]


def test_loop_stops_calling_the_llm_past_the_per_window_cap():
    distinct = [_memo(f"m{i}", [f"d{i}"]) for i in range(10)]
    stub = _LoopStub(distinct, cap=3)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert stub.compute_calls == 3                              # never calls past the cap
    assert len(stub.recorded) == 3                              # all three were distinct -> all recorded


def test_loop_first_trigger_label_then_repeat():
    a, b = _memo("A"), _memo("B")
    stub = _LoopStub([a, b], cap=2)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert [t for _s, t in stub.recorded] == ["cadence", "repeat"]   # initial due trigger, then repeats


def test_loop_stops_on_cancellation_when_evals_join():
    # cap=0 (unbounded): the loop only ends via cancellation — the eval-join path in _dispatch_evals.
    stub = _LoopStub([_memo("A")], cap=0, cadence=0.01)

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(Engine._research_overlap_loop, stub, "cadence")
            await anyio.sleep(0.1)
            tg.cancel_scope.cancel()

    anyio.run(drive)                                            # returns cleanly (no leaked task / hang)
    assert stub.compute_calls >= 1                              # it did run while the "eval" was in flight
    assert len(stub.recorded) == 1                              # identical A only recorded once


def test_loop_cancellation_joins_the_paid_research_worker():
    release = threading.Event()
    worker_finished = threading.Event()

    async def drive():
        started = threading.Event()
        stub = _LoopStub([_memo("unused")], cap=1)

        def _blocking_compute(state, trig, *, trace=True):
            started.set()
            release.wait()
            worker_finished.set()
            return None

        stub._compute_deep_research = _blocking_compute
        return await _cancel_blocked_paid_task(
            lambda: Engine._research_overlap_loop(stub, "cadence"),
            started,
            release,
            worker_finished,
        )

    detached = anyio.run(drive)
    assert detached is False
    assert worker_finished.is_set()


def test_converged_backoff_never_drops_below_the_interval_floor():
    # Diff-review finding: with a user interval_s > the default 3600 cap, the converged backoff must
    # not re-call MORE often than the floor. The loop passes cap=max(base, 3600), so a base of 7200
    # stays >= 7200 across the geometric backoff.
    from looplab.engine.train_monitor import next_monitor_sleep
    base = 7200.0
    for streak in range(0, 8):
        s = next_monitor_sleep(base, status="healthy", healthy_streak=streak, cap=max(base, 3600.0))
        assert s >= base, (streak, s)
    # And the default-interval case still backs OFF (base 1800 <= 3600 cap grows toward the cap).
    assert next_monitor_sleep(1800.0, status="healthy", healthy_streak=6, cap=max(1800.0, 3600.0)) > 1800.0


def test_repeat_memos_are_excluded_from_the_serial_cadence_gates():
    # Replay-review finding: a repeated overlap memo (trigger="repeat") must NOT advance the
    # node-count cadence marker or count as "already researched" for the serial between-nodes pass.
    state = types.SimpleNamespace(research=[
        {"at_node": 2, "trigger": "cadence"},
        {"at_node": 5, "trigger": "repeat"},     # recorded mid-eval by the overlap loop
        {"at_node": 5, "trigger": "repeat"},
    ])
    counted = Engine._cadence_research_memos(state)
    assert [m["at_node"] for m in counted] == [2]                 # only the real cadence memo counts
    assert Engine._already_researched_at(state, 2) is True        # real memo blocks re-firing at 2
    assert Engine._already_researched_at(state, 5) is False       # repeat memos at 5 are invisible


def test_loop_consolidates_the_board_each_tick_when_enabled():
    # Phase 2: with concurrent_consolidate on, the loop dedups the hypothesis board every tick
    # (self-gated inside _maybe_merge_hypotheses). Here we just prove it is INVOKED on the loop.
    merges = {"n": 0}
    stub = _LoopStub([_memo("A"), _memo("B")], cap=2)
    stub._concurrent_consolidate = True
    stub._maybe_merge_hypotheses = lambda state: merges.__setitem__("n", merges["n"] + 1)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert merges["n"] >= 2                       # ran on the active ticks, before the research cap


def test_loop_does_not_consolidate_when_disabled():
    # Default (flag absent/False): the board consolidation is NOT invoked from the loop (== today).
    merges = {"n": 0}
    stub = _LoopStub([_memo("A"), _memo("B")], cap=2)
    stub._maybe_merge_hypotheses = lambda state: merges.__setitem__("n", merges["n"] + 1)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert merges["n"] == 0                       # getattr(_concurrent_consolidate, False) gate holds


def test_loop_without_initial_trigger_waits_a_full_cadence_first():
    # No due trigger -> the first tick sleeps a full cadence; a "short eval" cancels before it fires.
    stub = _LoopStub([_memo("A")], cap=0, cadence=0.2)

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(Engine._research_overlap_loop, stub, None)
            await anyio.sleep(0.05)                             # shorter than one cadence
            tg.cancel_scope.cancel()

    anyio.run(drive)
    assert stub.compute_calls == 0                              # never researched a short window
