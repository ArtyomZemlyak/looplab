"""No other thread reads what `_apply_strategy` writes while it is writing it (review 2026-09-22,
ENG1-03 step 4d).

THE QUESTION 4d HAD TO ANSWER FIRST. A strategy decision rewrites up to twenty engine attributes one
assignment at a time — the policy, the operator switches, the novelty stance, the eval timeout, the
widths, the Developer and the pools built from it. The plan was a frozen `LiveTreatment` record
swapped by one `replace`, "so the build worker never sees a half-applied strategy", with the proviso
"first measure whether there's a race today". Measured 2026-09-23 on real toy runs with the
`_apply_strategy` window WIDENED (a sleep inside it), a Strategist that changes the strategy at every
consult, slowed evaluations, the chunked lane, the steady-state build lane and card-driven selection:
24 windows, zero reads of a strategy-written attribute from any other thread, zero evaluations in
flight at any window. The cadence block that applies a strategy is offloaded to a worker thread
(`orchestrator.py::Engine._run_cadences`), but the main task AWAITS it, and the build lanes and the
proposal have been joined before the turn reaches it; the one reader that can run beside it — an
evaluation's single `self.timeout` read — reads one attribute atomically. So there was no race for
the record to prevent, and it was not built.

WHAT THIS PINS INSTEAD: that measurement, as a driven property. If a later change lets a build, a
proposal or any other thread read the strategy while it is being rewritten, this goes red and the
record becomes worth its cost. The watched set is DERIVED from `_apply_strategy`'s own assignments,
so a new strategy-written attribute is watched without editing this file, and the detector proves it
can see an overlap (a control thread reading `engine.policy` throughout) before its silence counts.
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import threading
import time
from pathlib import Path

import anyio
import pytest

from looplab.engine import strategy as strategy_module
from looplab.engine.orchestrator import Engine
from tests.factories import make_engine

_WIDEN_S = 0.05


def strategy_written_attributes() -> frozenset:
    """Every `self.<attr>` `_apply_strategy` assigns (plain or augmented), read off its AST."""
    source = textwrap.dedent(inspect.getsource(strategy_module.StrategyCadenceMixin._apply_strategy))
    names = set()
    for node in ast.walk(ast.parse(source)):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else [])
        for target in targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.add(target.attr)
    return frozenset(names)


class _Flipper:
    """A Strategist that changes the strategy at EVERY consult, so every consult is a window."""

    def __init__(self) -> None:
        self.consults = 0

    def decide(self, state, ctx):
        self.consults += 1
        flip = self.consults % 2 == 0
        return {"source": "rule", "rationale": "flip",
                "policy": "mcts" if flip else "greedy",
                "operators": {"complexity_cue": flip, "prefer_sweep": flip,
                              "merge_mode": "hybrid" if flip else "blend"},
                "novelty_stance": "explore" if flip else "exploit",
                "eval_timeout": 30.0 if flip else 40.0}


def _overlapping_reads(monkeypatch, tmp_path: Path, *, control: bool, **engine_kw):
    watched = strategy_written_attributes()
    applying: dict = {"thread": None}
    windows: list[float] = []
    overlaps: list[tuple[str, str]] = []
    lock = threading.Lock()
    real_getattribute = Engine.__getattribute__
    real_apply = Engine._apply_strategy

    def getattribute(self, name):
        owner = applying["thread"]
        if (owner is not None and name in watched
                and threading.current_thread() is not owner):
            frame = sys._getframe(1)
            with lock:
                overlaps.append((name, frame.f_code.co_name))
        return real_getattribute(self, name)

    def apply(self, *args, **kwargs):
        applying["thread"] = threading.current_thread()
        started = time.perf_counter()
        try:
            time.sleep(_WIDEN_S)      # WIDENED: "can anything overlap it", not "did it happen to"
            return real_apply(self, *args, **kwargs)
        finally:
            applying["thread"] = None
            windows.append(time.perf_counter() - started)

    monkeypatch.setattr(Engine, "__getattribute__", getattribute)
    monkeypatch.setattr(Engine, "_apply_strategy", apply)
    engine = make_engine(tmp_path / "run", max_nodes=8, n_seeds=3, strategist=_Flipper(),
                         strategist_every=1, llm_parallel=3, eval_parallel=3, **engine_kw)
    stop = threading.Event()

    def _control_reader():
        while not stop.is_set():
            engine.policy
            time.sleep(0.002)

    reader = threading.Thread(target=_control_reader, name="overlap-control", daemon=True)
    if control:
        reader.start()
    try:
        anyio.run(engine.run)
    finally:
        stop.set()
        if control:
            reader.join(timeout=5)
    return windows, overlaps


def test_the_watched_set_is_the_strategy_the_engine_rewrites():
    watched = strategy_written_attributes()
    # The load-bearing members, so an AST that silently read nothing cannot pass the next test.
    assert {"policy", "timeout", "_eval_parallel", "_merge_mode", "developer"} <= watched, watched


def test_the_detector_sees_an_overlap_when_there_is_one(monkeypatch, tmp_path):
    windows, overlaps = _overlapping_reads(monkeypatch, tmp_path, control=True)
    assert windows, "no strategy was applied, so nothing was measured"
    assert ("policy", "_control_reader") in overlaps, (
        "a thread reading engine.policy throughout the run was not seen inside any window: the "
        "detector cannot see an overlap, so its silence below proves nothing")
    assert {reader for _name, reader in overlaps} == {"_control_reader"}, overlaps


@pytest.mark.parametrize("lane", ["chunked", "steady_state"])
def test_no_thread_reads_the_strategy_while_it_is_rewritten(monkeypatch, tmp_path, lane):
    extra = {"steady_state_build": True} if lane == "steady_state" else {}
    windows, overlaps = _overlapping_reads(monkeypatch, tmp_path, control=False, **extra)
    assert len(windows) >= 2, f"only {len(windows)} strategy application(s) were measured"
    assert overlaps == [], (
        "another thread read a strategy-written attribute while `_apply_strategy` was rewriting "
        f"it: {sorted(set(overlaps))}. A half-applied strategy is now observable — the frozen "
        "`LiveTreatment` record ENG1-03 step 4d declined for want of a race is owed.")
