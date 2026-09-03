"""The span exporter's worker records WHICH condition retired it, from a closed registry.

`runs/e5small-dr-unified-v12` wrote its last span at 18:20 and kept appending events for the next
ten and a half hours with no exporter thread alive and zero drop counters — because the worker had
already returned before any row could be dropped. `metrics()["worker_alive"]` names the symptom;
until this landed, nothing named the condition, and the five terminal paths were byte-identical
from the outside.

Two properties, and they are different tests on purpose:

  * the reasons are a REGISTRY, guarded in both directions — a stop recorded with an unregistered
    word reads as a diagnosis nobody can look up, and a registered word nothing emits is a decoy
    (`engine/speculation.py::CARD_BUILD_SKIP_REASONS` states the same rule);
  * a CRASH is recorded, which is the one stop that had no record at all. It is driven, not pinned:
    a real exception is raised inside the loop and the resulting state is read back.
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from looplab.core import tracing
from looplab.core.tracing import TRACE_WORKER_STOP_REASONS

SOURCE = Path(tracing.__file__).read_text(encoding="utf-8-sig", errors="replace")


def _recorded_reasons() -> set[str]:
    """Every literal handed to `_retire_worker_locked`, resolved from real `ast.Call` nodes.

    AST, not a substring scan: a commented-out call is not an `ast.Call`, so a reason cannot be
    registered by a comment carrying its text.
    """
    tree = ast.parse(SOURCE)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "_retire_worker_locked"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
        elif isinstance(first, ast.IfExp):
            # `"shutdown" if self._shutdown else "retired"` — both arms are real emissions.
            for arm in (first.body, first.orelse):
                if isinstance(arm, ast.Constant) and isinstance(arm.value, str):
                    found.add(arm.value)
        else:  # pragma: no cover - a non-literal reason defeats the registry entirely
            pytest.fail(f"_retire_worker_locked called with a non-literal reason: "
                        f"{ast.dump(first)[:120]}")
    return found


def test_every_emitted_reason_is_registered():
    """MUTATION: retire with `"exhausted"` -> the word lands on a durable health row and in a log
    line, and no reader can look it up."""
    unregistered = _recorded_reasons() - set(TRACE_WORKER_STOP_REASONS)
    assert not unregistered, f"unregistered stop reasons: {sorted(unregistered)}"


def test_every_registered_reason_is_actually_emitted():
    """The other direction: a registered word nothing emits is a decoy that reads as covered."""
    unemitted = set(TRACE_WORKER_STOP_REASONS) - _recorded_reasons()
    assert not unemitted, f"registered but never recorded: {sorted(unemitted)}"


def test_the_registry_has_no_duplicates():
    assert len(TRACE_WORKER_STOP_REASONS) == len(set(TRACE_WORKER_STOP_REASONS))


def _live_exporter(tmp_path, *, idle_s: float):
    return tracing.AsyncJsonlSpanExporter(tmp_path / "spans.jsonl", worker_idle_s=idle_s)


def _span(name: str = "probe"):
    return {"name": name, "kind": "operation", "trace_id": "t" * 32, "span_id": "s" * 16,
            "parent_id": None, "run_id": "r", "attributes": {}, "events": [],
            "status": "OK", "start": 1.0, "duration_s": 0.1}


def _wait_for_stop(exporter, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not exporter.metrics()["worker_alive"]:
            return
        time.sleep(0.01)
    raise AssertionError("the export worker never retired")


def test_an_idle_retirement_records_idle(tmp_path):
    """Driven end to end through the real exporter: submit nothing, let the idle wait elapse."""
    exporter = _live_exporter(tmp_path, idle_s=0.05)
    try:
        assert exporter.export(_span()) is True
        _wait_for_stop(exporter)
        assert exporter.metrics()["worker_stop_reason"] == "idle"
        assert exporter.metrics()["stopped_idle"] >= 1
    finally:
        exporter.shutdown()


def test_a_crash_inside_the_loop_is_recorded_with_its_type(tmp_path):
    """MUTATION: drop the `except BaseException` wrapper -> the thread dies, `worker_alive` turns
    False with every counter at zero, and the state is byte-identical to an idle retirement."""
    exporter = _live_exporter(tmp_path, idle_s=5.0)
    boom = RuntimeError("the loop broke")

    def _explode():
        raise boom

    exporter._worker_loop = _explode  # the wrapper is what is under test, not the loop
    try:
        with pytest.raises(RuntimeError):
            exporter._worker_main()
        snapshot = exporter.metrics()
        assert snapshot["worker_stop_reason"] == "crashed"
        assert "RuntimeError" in snapshot["worker_stop_detail"]
        assert "the loop broke" in snapshot["worker_stop_detail"]
        assert snapshot["stopped_crashed"] == 1
    finally:
        exporter.shutdown()


def test_a_never_stopped_worker_reports_an_empty_reason_not_a_guess(tmp_path):
    """Empty means "has never stopped in this process". A default of `idle` would make a healthy
    exporter indistinguishable from one that had already retired once."""
    exporter = _live_exporter(tmp_path, idle_s=5.0)
    try:
        assert exporter.metrics()["worker_stop_reason"] == ""
        assert all(exporter.metrics()[f"stopped_{reason}"] == 0
                   for reason in TRACE_WORKER_STOP_REASONS)
    finally:
        exporter.shutdown()


def test_the_health_snapshot_carries_the_reason_beside_the_symptom(tmp_path):
    """`worker_alive: false` and the reason must travel together — the engine publishes this whole
    dict as `trace_export_health`, and a symptom with no condition is what made v12 undiagnosable.
    """
    exporter = _live_exporter(tmp_path, idle_s=0.05)
    try:
        assert exporter.export(_span()) is True
        _wait_for_stop(exporter)
        snapshot = exporter.metrics()
        assert snapshot["worker_alive"] is False
        assert snapshot["worker_stop_reason"] in TRACE_WORKER_STOP_REASONS
    finally:
        exporter.shutdown()
