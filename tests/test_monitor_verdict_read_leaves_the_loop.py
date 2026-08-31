"""The fresh monitor-verdict read runs on a WORKER thread, and still reaches the judge.

`_evaluate`'s crash path reads this node's `train_monitor_alert` rows FRESH — not from
`events_at_start`, because every alert it wants was appended DURING the attempt that just died. That
read plus the O(whole log) `_durable_monitor_verdicts` walk sat on the event loop, one line below a
`to_thread.run_sync` that was already paying for a worker hop.

THE SEVERITY IN THE OLD NOTE WAS OVERSTATED AND THE NUMBER IS RECORDED HERE RATHER THAN REPEATED.
Measured on this box's real logs: `read_all()` warm + the walk is 1.0 + 0.51 ms on the largest
healthy log (e5small-dr-unified-v4, 10.3 MB, 12,579 events) and 0.7 + 0.22 ms on
rubertlite-dr-unified-v8 — once per FAILED attempt. The note it replaced claimed "every concurrent
eval's terminal and the whole serve/read side stall behind them", which is the right shape and about
three orders of magnitude off the paid propose call it compared itself to. It is folded into the
existing hop because that hop is free and the term grows with the log, not because milliseconds were
being lost.

WHAT MAKES THE MOVE SAFE is that this hop only READS: `EventStore` serializes `append`/`read_all`
through its own locks (invariant #1's own note), and `_durable_monitor_verdicts` is a pure filter.
That is the difference from `_stage_card_creates`' proposal, which needed a capture sink first
because it WROTE folded rows.

Both assertions have an input that makes them fail; the mutations are named in the messages.
"""
from __future__ import annotations

import threading

import anyio
import pytest

from looplab.engine.orchestrator import Engine
from looplab.events.types import EV_TRAIN_MONITOR_ALERT
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from tests.test_auto_install_deps import TASK, ToyTask, _NeverRepairs, _Stub


_CRASH = ("import sys\n"
          "sys.stderr.write('Traceback (most recent call last):\\n"
          "  File \"/w/solution.py\", line 1, in <module>\\n"
          "RuntimeError: boom\\n')\n"
          "sys.exit(1)\n")


class _RecordingJudge(_Stub):
    """A judge that answers without repairing. It deliberately does NOT record the verdicts.

    THE FIRST CUT OF THIS FILE ASSERTED ON `kw["monitor_verdicts"]` HERE AND MEASURED NOTHING: the
    engine never hands the researcher that kwarg — `_triage_crash` renders the rows into the prompt
    through `_format_monitor_verdicts`, and `test_critic_sees_the_diagnosis` pins exactly that
    (`'"monitor_verdicts"' not in src`). The stub saw `[]` for every call and the test read as "the
    move lost the rows" while the read was returning them correctly. The seam the engine actually
    passes them across is `Engine._triage_crash`, so that is what the test below wraps.
    """

    def triage_crash(self, node, error, attempt, **kw):
        return {"action": "keep_going", "rationale": "noted"}


def _engine_with_an_alert(run_dir, judge):
    eng = Engine(run_dir, task=ToyTask.load(TASK), researcher=judge, developer=_NeverRepairs(_CRASH),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 inline_repair=True, inline_repair_attempts=1)
    eng.store.append("run_started",
                     {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": _CRASH})
    # The row the read exists FOR — durable, keyed to this node's generation.
    eng.store.append(EV_TRAIN_MONITOR_ALERT, {
        "node_id": 0, "generation": 0, "status": "broken", "confidence": 0.9,
        "reason": "loss pinned at 8.8534 with IQR 0", "log_role": "training", "stage": "train"})
    return eng


def test_the_fresh_read_does_not_run_on_the_event_loop(tmp_path, monkeypatch):
    """Mutation: read it on the loop again (`_durable_monitor_verdicts(self.store.read_all(), …)`
    outside the hop) and the recorded thread IS the loop's."""
    import looplab.engine.evaluate as ev

    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = ev._durable_monitor_verdicts

    def _recording(events, node_id, generation):
        threads.append(threading.get_ident())
        return real(events, node_id, generation)

    monkeypatch.setattr(ev, "_durable_monitor_verdicts", _recording)
    eng = _engine_with_an_alert(tmp_path / "off-loop", _RecordingJudge())

    async def _bounded() -> bool:
        with anyio.move_on_after(120) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    assert threads, "precondition: the crash path really did read the monitor verdicts"
    assert loop_thread not in threads, (
        f"the fresh read must happen on a WORKER thread, got {threads} against a loop thread of "
        f"{loop_thread}. On the loop it is a `read_all()` plus an O(whole log) walk between two "
        f"awaits, and the hop one line above is already paid for")


def test_the_verdicts_still_REACH_the_judge_after_the_move(tmp_path):
    """Moving a read must not lose it. Mutation: return `[]` from the hop's second element, or
    thread only the tools out of it, and the repair goes back to being blind to what the watchdog
    said — the 17-GPU-hour defect `_durable_monitor_verdicts` was written for."""
    eng = _engine_with_an_alert(tmp_path / "reaches", _RecordingJudge())
    handed: list = []
    real_triage = eng._triage_crash

    def _watched(*a, **kw):
        handed.append(list(kw.get("monitor_verdicts") or []))
        return real_triage(*a, **kw)

    eng._triage_crash = _watched

    async def _bounded() -> bool:
        with anyio.move_on_after(120) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    assert handed, "precondition: the crash path reached the triage at all"
    assert any(v for v in handed), (
        f"the triage must still be handed this node's watchdog verdicts, got {handed}")
    first = next(v for v in handed if v)
    assert "pinned at 8.8534" in str(first), (
        "and they must be the REAL rows, not an empty shell the move produced")
