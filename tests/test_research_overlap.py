"""Repeated concurrent deep-research (`_research_overlap_loop`) — keep the reasoning agents busy for
the WHOLE eval window instead of idling a multi-day training after one memo. These tests pin the pure
pieces (content signature, adaptive cadence) and drive the loop itself through a light stub host so no
real Engine/LLM is needed. The loop is advisory-only (records via the BACKGROUND_APPENDABLE path), so
none of this touches folded selection or replay."""
import types

import anyio
import pytest

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
