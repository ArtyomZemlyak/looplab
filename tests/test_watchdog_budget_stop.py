"""The live watchdogs stop at the operator's spend ceiling — and their own bounds hold on failure.

Review 2026-09-22, ENG3-02 (ENG2-12, TAT-01). Both watchdog loops judge a live log on a timer and
wrap each tick in `except Exception: continue`, so that a disk or tracer hiccup skips one tick
instead of disabling the watcher for a multi-hour eval. That handler also absorbed the run's
`BudgetExceeded`, which the judge (`_training_verdict` / `_asha_verdict`) deliberately re-raises —
and both loops counted a call only AFTER the awaited verdict returned, and the training monitor
committed its same-digest retry only on a returned verdict. So a judge that RAISED committed
neither bound: `review/ENG3/budget_swallow.py` measured the training monitor re-invoking a
budget-stopped judge 300 times in 5 s on a log that never changed, past `_MAX_MONITOR_LLM_CALLS`
(200) — and under the legacy accountant every one of those is a provider call billed and THEN
refused.

What holds now, each driven below through the real coroutine: a spend stop ends the watch after
ONE attempt and says so on the tick's span (`budget_stop`); any OTHER raising judge is counted in a
`finally`, so the training monitor re-asks one unchanged digest at most
`_MONITOR_SAME_DIGEST_RETRIES` times and the ASHA judge stays inside `_MAX_ASHA_JUDGE_CALLS`.

Why the watchdog RETURNS rather than re-raising: it shares the eval's task group, and a raise there
would tear down the stage whose training the run is already paying for. The ceiling resurfaces at
the run's next paid call on the main path (the accountant is sticky), which is where it ends the run.
"""
from __future__ import annotations

import json
import threading

import anyio

from looplab.core.llm import BudgetExceeded
from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine import asha_monitor as am
from looplab.engine import train_monitor as tm
from looplab.engine.asha_monitor import AshaMonitorMixin
from looplab.engine.train_monitor import TrainingMonitorMixin

from test_asha_monitor import _AshaStub, _fake_state, _kill_setup

# A window many cadences long: at a 0.01 s cadence the pre-fix loops made one call per tick, so a
# one-second window separates "bounded" (a handful of calls) from "re-asked every tick" (~100).
_WINDOW_S = 1.0


class _EmptyStore:
    def read_all(self):
        return []


class _MonitorHost(TrainingMonitorMixin):
    def __init__(self, tracer):
        self.tracer = tracer
        self._train_monitor_interval_s = 0.01


def _drive_training_monitor(tmp_path, exc):
    """Run the REAL `_monitor_training` over a frozen log with a judge that raises `exc`.

    Returns `(judge_calls, returned_on_its_own, spans)`. The stub's parameters are the real
    `_training_verdict` contract: the loop calls it positionally, and a stub one parameter short
    would raise `TypeError` into the very handler this file is about.
    """
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")    # FROZEN: the digest never changes
    calls = {"n": 0}

    def _verdict(digest, context, stage_context="", trajectory_text="", tools=None,
                 contract_text=""):
        calls["n"] += 1
        raise exc

    outcome: dict = {}

    async def main():
        tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
        host = _MonitorHost(tracer)
        host._monitor_cadence = lambda: 0.01
        host._training_verdict = _verdict
        host.store = _EmptyStore()
        with anyio.move_on_after(_WINDOW_S) as scope:
            await host._monitor_training(0, 0, str(wd), threading.Event())
        outcome["on_its_own"] = not scope.cancelled_caught
        tracer.shutdown()

    anyio.run(main)
    path = tmp_path / "spans.jsonl"
    spans = ([json.loads(line) for line in path.read_text().splitlines() if line.strip()]
             if path.exists() else [])
    return calls["n"], outcome["on_its_own"], spans


def test_a_budget_stopped_training_judge_is_asked_once_and_the_watch_ends(tmp_path):
    """MUTATION: drop the `except BudgetExceeded` around the judge call -> the tick handler swallows
    it and the frozen digest is re-asked every tick (the 300-in-5-s reproduction)."""
    calls, on_its_own, spans = _drive_training_monitor(
        tmp_path, BudgetExceeded("LLM spend ceiling reached"))
    assert calls == 1, f"re-invoked a budget-stopped judge {calls} times"
    assert on_its_own, "the watcher kept running past the operator's spend ceiling"
    stopped = [s for s in spans if s.get("name") == "train_monitor"
               and (s.get("attributes") or {}).get("budget_stop") is True]
    assert len(stopped) == 1, "the stop must be said on the tick's span, not only in its absence"


def test_a_raising_training_judge_stays_inside_the_same_digest_retry_bound(tmp_path):
    """Any OTHER raise is still a tick hiccup the watcher survives — but it is an unanswered look,
    and it counts toward the same bound a `None` verdict does. MUTATION: move `llm_calls += 1` and
    the retry accounting back after the await -> ~100 calls in the window."""
    calls, on_its_own, _spans = _drive_training_monitor(tmp_path, RuntimeError("endpoint 500"))
    assert calls == tm._MONITOR_SAME_DIGEST_RETRIES, calls
    assert not on_its_own, "an ordinary judge failure must never end the watch"


def test_a_raising_training_judge_still_counts_toward_the_per_node_call_cap(tmp_path, monkeypatch):
    """The per-node backstop is committed in `finally` too: with the retry bound lifted out of the
    way, a judge that raises on every tick is capped by `_MAX_MONITOR_LLM_CALLS`, not by nothing."""
    monkeypatch.setattr(tm, "_MONITOR_SAME_DIGEST_RETRIES", 10_000)
    monkeypatch.setattr(tm, "_MAX_MONITOR_LLM_CALLS", 3)
    calls, on_its_own, spans = _drive_training_monitor(tmp_path, RuntimeError("endpoint 500"))
    assert calls == 3, calls
    assert not on_its_own
    assert any((s.get("attributes") or {}).get("llm_capped") for s in spans
               if s.get("name") == "train_monitor")


class _RaisingJudgeClient:
    """The Developer's client, answering through the same `complete_tool` path production takes
    (`_asha_verdict` -> `structured_judge` -> `parse_structured`), raising `exc` every time."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def complete_tool(self, messages, schema):
        self.calls += 1
        raise self._exc


def _drive_asha_monitor(tmp_path, monkeypatch, exc):
    wd, spec, curves = _kill_setup(tmp_path)
    judge = _RaisingJudgeClient(exc)
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    monkeypatch.setattr("looplab.engine.orchestrator.fold",
                        lambda events: _fake_state([0.8, 0.7, 0.6], curves=curves))
    outcome: dict = {}

    async def main():
        with anyio.move_on_after(_WINDOW_S) as scope:
            await AshaMonitorMixin._monitor_asha(stub, 0, 0, str(wd), threading.Event(), spec,
                                                 "max", {})
        outcome["on_its_own"] = not scope.cancelled_caught

    anyio.run(main)
    return judge.calls, outcome["on_its_own"]


def test_a_budget_stopped_asha_judge_is_asked_once_and_the_watch_ends(tmp_path, monkeypatch):
    """The rank flag persists while the node is behind, so a swallowed stop re-consulted the judge
    on every tick until `_MAX_ASHA_JUDGE_CALLS` — which it never reached, because the count was
    committed after the await. MUTATION: drop the `except BudgetExceeded` -> ~100 calls."""
    calls, on_its_own = _drive_asha_monitor(
        tmp_path, monkeypatch, BudgetExceeded("LLM spend ceiling reached"))
    assert calls == 1, f"re-invoked a budget-stopped judge {calls} times"
    assert on_its_own


def test_a_raising_asha_judge_is_bounded_by_the_judge_call_cap(tmp_path, monkeypatch):
    """A judge that raises every time (a tool derivation failing before `_asha_verdict`'s own
    handler) is re-asked on the next tick — it is not a spared verdict — but only up to the cap.
    `_asha_verdict` itself converts an ordinary failure into no verdict, so the raise is staged one
    level out, where the loop's own handler is what meets it."""
    monkeypatch.setattr(am, "_MAX_ASHA_JUDGE_CALLS", 3)
    attempts = {"n": 0}

    def _raising_tools(*_a, **_k):
        attempts["n"] += 1
        raise OSError("stage log vanished mid-derivation")

    monkeypatch.setattr(am, "monitor_log_tools", _raising_tools)
    wd, spec, curves = _kill_setup(tmp_path)
    judge = _RaisingJudgeClient(RuntimeError("never reached: the tools raise first"))
    stub = _AshaStub(kill=True, min_siblings=3, cadence=0.01, judge=judge)
    monkeypatch.setattr("looplab.engine.orchestrator.fold",
                        lambda events: _fake_state([0.8, 0.7, 0.6], curves=curves))
    outcome: dict = {}

    async def main():
        with anyio.move_on_after(_WINDOW_S) as scope:
            await AshaMonitorMixin._monitor_asha(stub, 0, 0, str(wd), threading.Event(), spec,
                                                 "max", {})
        outcome["on_its_own"] = not scope.cancelled_caught

    anyio.run(main)
    assert attempts["n"] == 3, attempts["n"]
    assert judge.calls == 0
    assert not outcome["on_its_own"], "an ordinary judge failure must never end the watch"
