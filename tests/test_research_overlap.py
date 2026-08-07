"""Repeated concurrent deep-research (`_research_overlap_loop`) — keep the reasoning agents busy for
the WHOLE eval window instead of idling a multi-day training after one memo. These tests pin the pure
pieces (content signature, adaptive cadence) and drive the loop itself through a light stub host so no
real Engine/LLM is needed. The loop is advisory-only (records via the BACKGROUND_APPENDABLE path), so
none of this touches folded selection or replay."""
import threading
import types

import anyio

from looplab.engine.orchestrator import Engine
from looplab.engine.research_cadence import ResearchCadenceMixin, research_memo_sig


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

class _LoopStub(ResearchCadenceMixin):
    """Minimal host for `Engine._research_overlap_loop` (called as an unbound method with this as
    `self`). Serves a fixed memo sequence, records via a list, and uses a tiny cadence.

    Inherits the mixin for ONE member: `_research_attempt_step`, the indivisible receipt→provider→
    record unit the loop now runs in a single thread hop (see `test_research_attempt_settlement.py`
    for why the split version burned the gate). The three methods it drives are all overridden
    below, so the real engine's paid sequencing is exercised over these fakes."""
    def __init__(self, memos, *, cap=0, cadence=0.01):
        self._memos = list(memos)
        self._concurrent_research_max_calls = cap
        self._cadence = cadence
        self.compute_calls = 0
        self.recorded = []
        self.attempts = []
        self.recorded_attempts = []
        self.store = types.SimpleNamespace(read_all=lambda: [])

    def _research_repeat_cadence(self):
        return self._cadence

    def _compute_deep_research(self, state, trig, *, trace=True):
        m = self._memos[min(self.compute_calls, len(self._memos) - 1)]
        self.compute_calls += 1
        return m

    def _record_research_attempt(self, state, *, trigger, manual):
        # The loop receipts the initially-due trigger before every paid compute; `repeat` passes
        # ride a timer with no durable gate and are deliberately unreceipted.
        if trigger == "repeat":
            return None
        self.attempts.append(trigger)
        return f"attempt-{len(self.attempts)}"

    def _record_deep_research(self, memo, *, trigger, manual, attempt_id=None):
        self.recorded.append((research_memo_sig(memo), trigger))
        self.recorded_attempts.append(attempt_id)


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
    # `deep_research_every=-1` is the manual-only contract (it was spelled `0` until 2026-08-07,
    # when `0` became "start immediately" — see `engine/cadence.py::deep_research_window`). Turning
    # repeat on must not turn that disabled auto cadence into a hidden paid timer for every
    # long-running evaluation.
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
        deep_research_every=-1,
        _already_researched_at=lambda _state, _n: False,
        _cadence_research_marks=lambda _state: set(),
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


def test_a_provider_that_always_raises_still_spends_the_per_window_cap():
    """`calls` counted SUCCESSES, so a provider that consistently raises — broken auth, endpoint
    down, or a failure after tokens were already charged — never touched
    `concurrent_research_max_calls` and was re-called every cadence tick for the whole eval window.
    The one budget backstop was blind to exactly the failure mode that can spend without producing."""
    class _AlwaysRaises(_LoopStub):
        def _compute_deep_research(self, state, trig, *, trace=True):
            self.compute_calls += 1
            raise RuntimeError("provider is down")

    stub = _AlwaysRaises([_memo("unused")], cap=3, cadence=0.001)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert stub.compute_calls == 3, stub.compute_calls   # attempts are bounded, not just successes
    assert stub.recorded == []                           # and nothing was recorded from a failure


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
    ], research_attempts=[], research_attempts_completed=set())
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


def test_loop_defers_consolidation_to_main_task_in_card_selection_mode():
    merges = {"n": 0}
    stub = _LoopStub([_memo("A"), _memo("B")], cap=2)
    stub._concurrent_consolidate = True
    stub.card_driven_selection = True
    stub._maybe_merge_hypotheses = lambda state: merges.__setitem__("n", merges["n"] + 1)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert merges["n"] == 0


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


# --------------------------------------------------------------------------- merge cadence baseline

def _belief_card(cid):
    return types.SimpleNamespace(
        id=cid, statement=f"belief {cid}", selection_ready=False,
        selection_provenance=types.SimpleNamespace(action_source="none"))


class _BoardState:
    """The slice of RunState `_maybe_merge_hypotheses` reads."""

    def __init__(self, cards):
        self._cards, self.goal, self.nodes = list(cards), "g", {}

    def open_research_cards(self):
        return list(self._cards)


class _NullStore:
    def __init__(self):
        self.appended = []

    def append(self, event_type, data):
        self.appended.append((event_type, data))

    def read_all(self):
        return []


def _merge_engine(store):
    import contextlib

    @contextlib.contextmanager
    def _span(*_a, **_k):
        yield types.SimpleNamespace(set=lambda *a, **k: None)

    eng = object.__new__(Engine)
    eng._track_hypotheses = True
    eng._reflect_client = lambda: object()          # any non-None client enables the pass
    eng._embedder = None
    eng.lessons = None
    eng.store = store
    eng._op_span = _span
    return eng


def test_merge_cadence_baseline_is_the_POST_merge_board(monkeypatch):
    """"Grown by >=2 since the last pass" must mean since the board the last pass LEFT.

    The baseline used to be recorded before the merge, so consolidating 8 open cards down to 4 left
    it at 8: the board then had to re-grow to 10 before the next pass instead of 6. The more
    effective the merge, the longer the blackout it caused, and duplicates re-accumulated far past
    the documented cadence."""
    import looplab.engine.research_cadence as research_cadence
    import looplab.search.hybrid_merge as hybrid_merge

    groups = [{"members": [0, 1, 2], "merged": "m1"}, {"members": [3, 4, 5], "merged": "m2"}]
    monkeypatch.setattr(hybrid_merge, "consolidate", lambda texts, client, **kw: groups)
    # 8 open cards in, 4 left after the two 3-way merges.
    monkeypatch.setattr(research_cadence, "fold",
                        lambda _events: _BoardState(_belief_card(f"c{i}") for i in range(4)))

    store = _NullStore()
    eng = _merge_engine(store)
    eng._maybe_merge_hypotheses(_BoardState(_belief_card(f"c{i}") for i in range(8)))

    assert [t for t, _ in store.appended] == ["hypothesis_merged"] * 2
    assert eng._last_hyp_merge_n == 4          # the board the pass LEFT, not the 8 it found


def test_a_failed_merge_does_not_consume_the_cadence_window(monkeypatch):
    """A transient LLM/transport failure must not silently skip the next consolidation window."""
    import looplab.search.hybrid_merge as hybrid_merge

    def _boom(*_a, **_k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(hybrid_merge, "consolidate", _boom)
    eng = _merge_engine(_NullStore())
    eng._maybe_merge_hypotheses(_BoardState(_belief_card(f"c{i}") for i in range(6)))

    # Baseline untouched, so the very next pass on the same board still runs.
    assert getattr(eng, "_last_hyp_merge_n", -1) == -1


# ------------------------------------------------ the Card session latches on the SPAWN, not the ASK

class _StartProbe:
    """A task group that records what was started without running it."""

    def __init__(self):
        self.started = []

    def start_soon(self, func, *args):
        self.started.append((func, args))


def _spawn_host(*, on=True, repeat=True, trigger="cadence"):
    async def loop(_trigger):
        return None

    return types.SimpleNamespace(
        concurrent_research=on,
        _concurrent_research_repeat=repeat,
        deep_researcher=object(),
        _due_research_trigger=lambda _state: trigger,
        _research_overlap_loop=loop,
        _research_attempt_step=lambda *_a, **_k: None,
    )


def test_spawn_research_reports_whether_it_actually_started_anything():
    """The return value IS the Card session's latch, so every refusal must be reported as False.

    Latching on the ASK rather than the SPAWN is what made `concurrent_research` a no-op on the
    workload it exists for: the session asks once, at its first admission (node-count 1), while
    `deep_research_every` is 3 — measured on `runs/rubert-dr-0807`, zero research rows on a 3-node,
    hours-per-node GPU run with the feature on.
    """
    state = types.SimpleNamespace()

    off = _spawn_host(on=False)
    tg = _StartProbe()
    assert Engine._spawn_research(off, tg, state) is False
    assert tg.started == []                                  # nothing started -> latch must stay open

    not_due = _spawn_host(trigger=None)
    tg = _StartProbe()
    assert Engine._spawn_research(not_due, tg, state) is False
    assert tg.started == []

    repeating = _spawn_host(repeat=True)
    tg = _StartProbe()
    assert Engine._spawn_research(repeating, tg, state) is True
    assert [args for _fn, args in tg.started] == [("cadence",)]

    one_shot = _spawn_host(repeat=False)
    tg = _StartProbe()
    assert Engine._spawn_research(one_shot, tg, state) is True
    assert len(tg.started) == 1


def test_card_session_binds_the_research_latch_to_the_spawn_result():
    """Both `research_spawned` writers must take their value FROM `_spawn_research`.

    Tier-3 (AST, so a comment cannot satisfy it) and deliberately narrow: it is the exact mutation
    that reintroduces the defect — a constant `True` (or `bool(evals)`) beside a bare call, which
    closes the window on a NOT-DUE answer.  The behavioural half of the property is the return
    contract driven above; this half is what stops the caller from ignoring it.
    """
    import ast
    import inspect

    import looplab.engine.speculation as spec

    tree = ast.parse(inspect.getsource(spec))
    writes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "research_spawned"
    ]
    assert len(writes) == 2, "both Card-session research latch sites must be assignments"

    for node in writes:
        calls = [inner for inner in ast.walk(node.value) if isinstance(inner, ast.Call)]
        assert any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "_spawn_research"
            for call in calls
        ), "research_spawned must be latched from the _spawn_research result, never from a constant"

    # …and the constructor must not pre-latch it either (the `research_spawned=bool(evals)` shape).
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "CardSession":
            assert not [kw for kw in node.keywords if kw.arg == "research_spawned"]


# ------------------------------------------------------ `0` = START IMMEDIATELY (owner, 2026-08-07)

def test_deep_research_window_truth_table():
    """The ONE knob whose `0` is not "off", stated as a function so it has a truth table.

    `cadence_due` reads `every <= 0` as disabled and is shared by five other cadences, so the
    exception cannot live inside it. This is the whole translation; everything else about the
    Deep-Research gate is unchanged.
    """
    from looplab.engine.cadence import DEEP_RESEARCH_OFF, cadence_due, deep_research_window

    assert deep_research_window(0) == 1        # zero-width window -> due at every new node-count
    assert deep_research_window(1) == 1
    assert deep_research_window(3) == 3        # a spelled window passes through untouched
    assert deep_research_window(DEEP_RESEARCH_OFF) == 0        # -1 is the off switch
    assert deep_research_window(-7) == 0                       # …and so is any other negative
    # A junk or bool value must settle to OFF, never to "every node": this gate is read from a
    # resumed snapshot and from partially-built Engines, and `True` is an `int` worth 1 in Python.
    for junk in (True, False, None, "3", 2.0, [], object()):
        assert deep_research_window(junk) == 0, junk

    # …and the composition with the shared gate is what the two call sites actually evaluate.
    assert cadence_due(1, 0, deep_research_window(0)) is True    # first node, shipped default
    assert cadence_due(1, 0, deep_research_window(3)) is False   # first node, the OLD default
    assert cadence_due(3, 0, deep_research_window(3)) is True
    assert cadence_due(1, 0, deep_research_window(DEEP_RESEARCH_OFF)) is False
    # `0` keeps firing after the first pass, but only at a node-count it has not researched yet.
    assert cadence_due(2, 1, deep_research_window(0)) is True
    assert cadence_due(1, 1, deep_research_window(0)) is False


def test_concurrent_research_is_due_at_the_very_first_node_under_the_shipped_default():
    """The measured defect, driven end to end through the real `_due_research_trigger`.

    On `runs/rubert-dr-0804/0805/0807` — 1.5-4 h per node, `deep_research_every=3`,
    `concurrent_research=true` — the first eval admission sits at node-count 1 and the trigger
    answered None, so the overlapped think never started and all three runs recorded zero
    `research_attempted`/`research_completed` rows. Under the shipped default it answers "cadence"
    at that same admission, which is the entire feature.
    """
    def _host(every):
        return types.SimpleNamespace(
            deep_researcher=object(),
            deep_research_every=every,
            _already_researched_at=lambda _state, _n: False,
            _cadence_research_marks=lambda _state: set(),
            _cadence_due=Engine._cadence_due,
        )

    first_eval = types.SimpleNamespace(nodes={0: object()}, research=[], strategy_history=[])

    shipped = _host(0)
    assert Engine._due_research_trigger(shipped, first_eval) == "cadence"

    old_default = _host(3)
    assert Engine._due_research_trigger(old_default, first_eval) is None
    three_nodes = types.SimpleNamespace(
        nodes={i: object() for i in range(3)}, research=[], strategy_history=[])
    assert Engine._due_research_trigger(old_default, three_nodes) == "cadence"

    # OFF still means off — a manual `deep_research` control event and the Strategist's
    # `request_research` remain the only triggers, exactly as `0` used to behave.
    assert Engine._due_research_trigger(_host(-1), first_eval) is None
    assert Engine._due_research_trigger(_host(-1), three_nodes) is None

    # …and a run with no nodes yet has nothing to research: the concurrent seam only fires beside a
    # RUNNING eval, so "immediately" means "with the first evaluation", not "before the first idea".
    empty = types.SimpleNamespace(nodes={}, research=[], strategy_history=[])
    assert Engine._due_research_trigger(shipped, empty) is None


def test_serial_cadence_needs_a_wired_researcher_but_a_request_does_not():
    """A cadence schedules a stage; with no stage there is nothing to schedule.

    `_run_deep_research` records a STUB memo when no model is wired, on purpose, so a MANUAL
    request's counter gate advances and the loop cannot spin. At the old `every=3` the serial
    cadence inherited that and wrote two rows per three nodes on an offline run
    (`tests/data/golden_run_events.jsonl` has them at n=2/5/8). At the shipped `0` that would be two
    rows per NODE, each claiming a completed think nobody could have run — so the cadence branch now
    requires the same non-None researcher `_due_research_trigger` has always required, and the
    request branches deliberately do not.
    """
    ran = []

    def _host(*, researcher):
        return types.SimpleNamespace(
            deep_researcher=researcher,
            deep_research_every=0,
            _already_researched_at=lambda _state, _n: False,
            _cadence_research_marks=lambda _state: set(),
            _cadence_due=Engine._cadence_due,
            _outstanding_manual_research=lambda _state: 0,
            _run_deep_research=lambda state, *, trigger, manual: (
                ran.append(trigger) or state),
        )

    def _state(**over):
        base = dict(nodes={0: object()}, research=[], strategy_history=[],
                    research_requests=[], research_served=0, pending_nodes=lambda: [])
        base.update(over)
        return types.SimpleNamespace(**base)

    ResearchCadenceMixin._maybe_deep_research(_host(researcher=None), _state())
    assert ran == [], "an offline run must not record a cadence think it cannot perform"

    ResearchCadenceMixin._maybe_deep_research(_host(researcher=object()), _state())
    assert ran == ["cadence"]

    # A manual request is answered even with no model wired — that is what advances its gate.
    ran.clear()
    ResearchCadenceMixin._maybe_deep_research(
        _host(researcher=None), _state(research_requests=[{"id": "r1"}]))
    assert ran == ["manual"]

    # …and so is a Strategist `request_research`, for the same reason.
    ran.clear()
    ResearchCadenceMixin._maybe_deep_research(
        _host(researcher=None),
        _state(strategy_history=[{"at_node": 1, "strategy": {"request_research": True}}]))
    assert ran == ["strategist"]
