"""A permanent export failure that never names itself is undiagnosable from inside the product.

MEASURED ON v13, live. The span exporter froze at `exported_spans: 3970` and failed every export
after it — `export_failures` 1184 -> 3449 across four health rows, `loss_receipt_failures` 858 ->
2246, `worker_stop_reason: receipt_failed`, `shutdown: False` — while spans.jsonl and its append
journal both stopped at the same instant and the run kept producing spans for three more hours.

The console log carried it 3,449 times and said nothing:

    trace export lost spans: none (export failures: 1) — this is the exporter reporting its own
    loss through the logger, because the durable receipt below rides the writer that may be broken

SIX CANDIDATE CAUSES had to be eliminated from OUTSIDE the process, against the live frozen file:
writability (an O_APPEND open succeeds), ENOSPC (1 PB at 0%), a stale descriptor (/proc/<pid>/fd
holds none), a held flock (LOCK_NB acquires immediately), descriptor/path divergence (fstat ==
stat, inode stable), and a torn tail (the last complete line is 41,646 bytes of valid JSON). None
of them is the cause, and the seventh could not be reached because the delegate's exception was
discarded by two `except Exception: pass` handlers.

Retaining the delta for a later attempt is right. Discarding the REASON is what made this class
undiagnosable, and that is all this change alters — nothing here refuses, retries or reorders.
"""
from __future__ import annotations

import pytest

from looplab.core.tracing import TRACE_EXPORT_ERROR_MAX_CHARS, _bounded_export_error


def test_it_names_the_type_and_the_message():
    out = _bounded_export_error("export", OSError("Stale file handle"))
    assert out == "export: OSError: Stale file handle"


def test_the_phase_is_carried_because_the_two_writers_differ():
    """An export and its loss RECEIPT fail through different paths with different remedies. One
    field, last-wins, is only readable if it says WHICH failed."""
    assert _bounded_export_error("receipt", ValueError("x")).startswith("receipt: ")
    assert _bounded_export_error("export", ValueError("x")).startswith("export: ")


def test_it_is_bounded_because_it_rides_a_durable_row():
    """A provider message can be arbitrarily long, and an unbounded field on a row emitted once per
    failure is a second loss mechanism."""
    out = _bounded_export_error("export", RuntimeError("z" * 10_000))
    assert len(out) == TRACE_EXPORT_ERROR_MAX_CHARS
    assert out.startswith("export: RuntimeError: zzz")


def test_an_exception_whose_str_raises_still_produces_a_reason():
    """The diagnostic may never become the thing that breaks. A __str__ that raises is exactly the
    shape that would turn a reporting path into a second failure."""
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("no string for you")

    out = _bounded_export_error("export", Hostile())
    assert out == "export: Hostile: <unprintable>"


def test_metrics_publishes_the_reason():
    """The engine's `trace_export_health` row publishes this whole dict, so the reason travels with
    the symptom it explains. Read off the source rather than a live exporter: constructing one
    starts a worker thread, and this assertion is about the CONTRACT."""
    import inspect

    from looplab.core.tracing import AsyncJsonlSpanExporter
    src = inspect.getsource(AsyncJsonlSpanExporter.metrics)
    assert '"last_export_error": self._last_export_error,' in src
    # published beside the counter it explains, not in some unrelated projection
    assert '"export_failures"' in src


def test_the_reason_starts_EMPTY_and_RESETS_WITH_THE_PROCESS():
    """Empty means nothing has failed in THIS process — never "failed for no reason", and never a
    missing key a reader must tell apart from an older server (the rule worker_stop_reason follows).

    It initialises in `_reset_process_state`, WITH the counters it explains and not in `__init__`:
    that is the fork boundary, and an error string carried across it would attribute a parent's
    failure to a child that has never written a span."""
    import inspect

    from looplab.core.tracing import AsyncJsonlSpanExporter
    reset = inspect.getsource(AsyncJsonlSpanExporter._reset_process_state)
    assert 'self._last_export_error: str = ""' in reset
    assert "self._export_failures = 0" in reset, "it must sit with the counter it explains"


def test_both_swallow_sites_record_before_the_lock_is_dropped():
    """The two `except Exception` handlers now bind the exception and store it under the SAME lock
    that owns the counters — a reason published without its counter, or after the lock, would be a
    race the health row cannot read consistently."""
    import inspect

    from looplab.core.tracing import AsyncJsonlSpanExporter
    src = inspect.getsource(AsyncJsonlSpanExporter)
    assert 'except Exception as exc:' in src
    assert src.count("_bounded_export_error(") == 2, "both swallow sites must record"
    assert "self._last_export_error = export_error" in src
    assert "self._last_export_error = receipt_error" in src
