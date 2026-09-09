"""Bounded local trace-export queue: backpressure, lifecycle barriers and fork ownership."""
from __future__ import annotations

import inspect
import os
import threading
import time
from types import SimpleNamespace

import orjson
import pytest

from looplab.core.tracing import (
    AsyncJsonlSpanExporter, JsonlSpanExporter, Tracer, _span_jsonl_line)
from looplab.events.traceview import build_trace_view


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _rows(path):
    return [orjson.loads(line) for line in path.read_bytes().splitlines()]


def test_queue_backpressure_is_nonblocking_bounded_and_durably_counted(tmp_path):
    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(
        path, run_id="run", max_queue_spans=1, max_queue_bytes=64_000,
        loss_receipt_interval_s=0.01)
    # THE WORKER'S SEAM IS THE BATCH ONE (doc 34 D-02): it drains the queue and hands the writer
    # every row it took in ONE hardened append, so a double that intercepts single rows would let
    # the real filesystem work through and stop blocking anything.
    real_export = exporter._writer._export_lines
    started = threading.Event()
    release = threading.Event()

    def slow_first(lines, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(2)
        return real_export(lines, **kwargs)

    exporter._writer._export_lines = slow_first
    try:
        assert exporter.export({"name": "active"}) is True
        assert started.wait(1)
        assert exporter.export({"name": "queued"}) is True
        then = time.monotonic()
        assert exporter.export({"name": "dropped"}) is False
        assert time.monotonic() - then < 0.1, "backpressure waited behind filesystem I/O"
        assert exporter.metrics()["buffered_bytes"] <= 64_000

        release.set()
        assert exporter.force_flush(timeout_millis=2_000) is True
        metrics = exporter.metrics()
        assert metrics["accepted_spans"] == 2
        assert metrics["exported_spans"] == 2
        assert metrics["dropped_spans"] == metrics["dropped_queue_full"] == 1
        assert metrics["queued_spans"] == metrics["buffered_bytes"] == 0
        assert metrics["worker_alive"] is False  # force_flush retires, not merely drains, the worker

        rows = _rows(path)
        assert [row["name"] for row in rows if row["name"] != "looplab.exporter.loss"] == [
            "active", "queued"]
        receipt = next(row for row in rows if row["name"] == "looplab.exporter.loss")
        attrs = receipt["attributes"]
        assert receipt["kind"] == "internal" and receipt["run_id"] == "run"
        assert attrs["looplab.exporter.dropped_spans"] == 1
        assert attrs["looplab.exporter.dropped.queue_full"] == 1

        state = SimpleNamespace(run_id="run", task_id="task", total_eval_seconds=0.0)
        summary = build_trace_view(state, [receipt])["summary"]
        assert summary["dropped_spans"] == 1
        assert summary["export_failures"] == 0
        assert summary["exporter_loss_receipts"] == 1
        assert summary["exporter_metrics_partial"] is False
        partial = build_trace_view(state, [receipt], total_spans=2)["summary"]
        assert partial["dropped_spans"] == 1
        assert partial["exporter_metrics_partial"] is True
    finally:
        release.set()
        exporter.shutdown(timeout_millis=2_000)


def test_byte_budget_charges_the_worker_owned_row_not_only_the_deque(tmp_path):
    path = tmp_path / "spans.jsonl"
    first = {"name": "large", "payload": "x" * 2_000}
    budget = len(_span_jsonl_line(first))
    exporter = AsyncJsonlSpanExporter(
        path, max_queue_spans=100, max_queue_bytes=budget,
        loss_receipt_interval_s=0.01)
    real_export = exporter._writer._export_lines
    started = threading.Event()
    release = threading.Event()

    def blocked(lines, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(2)
        return real_export(lines, **kwargs)

    exporter._writer._export_lines = blocked
    try:
        assert exporter.export(first)
        assert started.wait(1)
        # The deque is empty now, but the worker still retains first's bytes. Count-only or
        # deque-only accounting would admit this row and violate the advertised memory bound.
        assert exporter.export({"name": "second"}) is False
        assert exporter.metrics()["dropped_queue_bytes"] == 1
        assert exporter.metrics()["buffered_bytes"] == budget
        release.set()
        assert exporter.force_flush(timeout_millis=2_000)
    finally:
        release.set()
        exporter.shutdown(timeout_millis=2_000)


def test_one_drain_pays_the_hardened_ladder_once_for_the_whole_batch(tmp_path, monkeypatch):
    """doc 34 D-02, closed 2026-09-08: the ladder is amortized per FLUSH, not per span.

    Each exported row used to run the whole thing for itself — a guarded open (~12 `stat` calls),
    a torn-tail heal read, the before/after identity stats and a 4 KiB receipt-journal write —
    which on the 14,507-span node this codebase measures elsewhere is ~43k opens on the ONE
    exporter worker thread whose queue is bounded and drop-newest, i.e. a throughput number that
    turns into lost spans on a slow mount.

    Driven, not asserted about: the worker is held inside its first delegate attempt while five more
    rows queue up behind it, and on release it must take all five in ONE append — one guarded open,
    one receipt — with every row on disk exactly once.
    """
    import looplab.core.tracing as tracing_mod

    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(path, max_queue_spans=16)
    opens = []
    real_open = tracing_mod._open_export_append
    monkeypatch.setattr(tracing_mod, "_open_export_append",
                        lambda p, **kw: opens.append(p) or real_open(p, **kw))

    drains: list[int] = []
    real_export = exporter._writer._export_lines
    started = threading.Event()
    release = threading.Event()

    def gated(lines, **kwargs):
        rows = list(lines)
        drains.append(len(rows))
        if not started.is_set():
            started.set()
            assert release.wait(2)
        return real_export(rows, **kwargs)

    exporter._writer._export_lines = gated
    try:
        assert exporter.export({"name": "first"})
        assert started.wait(1)                     # the worker owns row 1 and is inside the ladder
        for i in range(5):
            assert exporter.export({"name": f"queued-{i}"})
        assert _wait(lambda: exporter.metrics()["queued_spans"] == 5)
        release.set()
        assert exporter.force_flush(timeout_millis=5_000) is True

        assert drains == [1, 5], (
            f"the worker must take the whole queue per drain, took {drains}")
        # Two guarded opens per drain — the source and its receipt journal — so the whole ladder
        # is four opens for six spans where it used to be twelve.
        assert opens.count(path) == 2, (
            f"one hardened open of the source per DRAIN, not one per span: {opens.count(path)}")
        assert len(opens) == 4, f"and one receipt journal open per drain: {opens}"
        assert exporter.metrics()["exported_spans"] == 6
        names = [row["name"] for row in _rows(path)]
        assert names == ["first"] + [f"queued-{i}" for i in range(5)]
    finally:
        release.set()
        exporter.shutdown(timeout_millis=2_000)


def test_a_batched_drain_writes_ONE_receipt_binding_the_whole_appended_range(tmp_path):
    """The receipt is what makes a torn append detectable, so amortizing it must not weaken it.

    A drain writes one receipt whose `[before_size, after_size)` region is the bytes it appended and
    whose `append_sha256` is over exactly those bytes — the same proof the reader
    (`events/span_index.py::_validated_append_transition`) has always checked, at flush granularity.
    """
    import hashlib

    from looplab.core.trace_append import (
        SPAN_APPEND_JOURNAL_NAME, SPAN_APPEND_RECEIPT_SCHEMA)

    path = tmp_path / "spans.jsonl"
    writer = JsonlSpanExporter(path)
    writer.export({"name": "before-the-batch"})
    before_size = path.stat().st_size

    rows = [_span_jsonl_line({"name": f"batched-{i}"}) for i in range(4)]
    writer._export_lines(rows)

    receipts = _rows(tmp_path / SPAN_APPEND_JOURNAL_NAME)
    assert len(receipts) == 2, "one receipt for the single row, one for the four-row drain"
    receipt = receipts[-1]
    assert receipt["schema"] == SPAN_APPEND_RECEIPT_SCHEMA
    assert receipt["before_size"] == before_size
    assert receipt["after_size"] == path.stat().st_size
    appended = path.read_bytes()[receipt["before_size"]:receipt["after_size"]]
    assert appended == b"".join(rows)
    assert receipt["append_sha256"] == hashlib.sha256(appended).hexdigest()


def test_an_empty_drain_appends_nothing_and_writes_no_receipt(tmp_path):
    """A batch of nothing is not a zero-length append: it must not enter the receipt chain."""
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME

    path = tmp_path / "spans.jsonl"
    JsonlSpanExporter(path)._export_lines([])
    assert not path.exists() and not (tmp_path / SPAN_APPEND_JOURNAL_NAME).exists()


def test_a_row_that_violates_the_physical_contract_is_refused_before_the_lock(tmp_path):
    """The per-row contract is still per ROW, and one bad row refuses the whole drain untouched."""
    path = tmp_path / "spans.jsonl"
    writer = JsonlSpanExporter(path)
    good = _span_jsonl_line({"name": "good"})
    for bad in (b'{"name":"no-newline"}', b'{"a":1}\n{"b":2}\n', "not-bytes\n"):
        with pytest.raises(ValueError):
            writer._export_lines([good, bad])
    assert not path.exists(), "a refused drain must not have appended its good rows first"


def test_sporadic_submits_reuse_one_worker_until_a_flush_barrier(tmp_path):
    exporter = AsyncJsonlSpanExporter(tmp_path / "spans.jsonl", worker_idle_s=5.0)
    assert exporter.export({"name": "one"})
    assert _wait(lambda: exporter.metrics()["exported_spans"] == 1)
    worker = exporter._worker
    assert worker is not None and worker.is_alive()

    time.sleep(0.05)
    assert exporter.export({"name": "two"})
    assert exporter._worker is worker
    assert _wait(lambda: exporter.metrics()["exported_spans"] == 2)
    assert exporter.force_flush(timeout_millis=1_000)
    assert exporter.metrics()["worker_alive"] is False
    assert exporter.shutdown(timeout_millis=1_000)


def test_custom_sync_export_path_keeps_the_legacy_no_sidecar_contract(tmp_path):
    from looplab.core.trace_files import TRACE_WRITER_LOCK_NAME

    custom_dir = tmp_path / "embedded"
    path = custom_dir / "spans.jsonl"
    JsonlSpanExporter(path).export({"name": "custom"})
    assert path.exists()
    assert not (custom_dir / TRACE_WRITER_LOCK_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock failure seam")
def test_required_lifecycle_sidecar_contention_fails_before_source_append_but_custom_degrades(
        tmp_path, monkeypatch):
    import fcntl

    path = tmp_path / "spans.jsonl"

    def contended(_fd, _operation):
        raise BlockingIOError("lifecycle writer is owned")

    monkeypatch.setattr(fcntl, "flock", contended)
    exporter = JsonlSpanExporter(path, lifecycle_fence=True)
    with pytest.raises(OSError, match="lifecycle writer lock"):
        exporter.export({"name": "must-not-append"})
    assert not path.exists()

    custom = tmp_path / "custom" / "spans.jsonl"
    JsonlSpanExporter(custom).export({"name": "best-effort-data-lock"})
    assert _rows(custom) == [{"name": "best-effort-data-lock"}]


def test_post_shutdown_export_never_serializes_or_resurrects_a_worker(tmp_path, monkeypatch):
    import looplab.core.tracing as tracing

    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(path, lifecycle_fence=True)
    assert exporter.shutdown(timeout_millis=1_000)

    def must_not_serialize(_span):
        raise AssertionError("post-shutdown serialization ran")

    monkeypatch.setattr(tracing, "_span_jsonl_line", must_not_serialize)
    assert exporter.export(object()) is False
    metrics = exporter.metrics()
    assert metrics["dropped_shutdown"] == 1
    assert metrics["dropped_serialization_error"] == 0
    assert metrics["worker_alive"] is False
    assert not path.exists()
    # Idempotent shutdown remains a no-op: the rejected misuse is process-local and cannot schedule
    # a durable receipt behind the lifecycle boundary.
    assert exporter.shutdown(timeout_millis=1_000)
    assert not path.exists()


def test_worker_start_failure_is_a_nonthrowing_drop_and_can_be_receipted(tmp_path, monkeypatch):
    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(path)
    real_start = threading.Thread.start

    def exhausted(_thread):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", exhausted)
    assert exporter.export({"name": "not-stranded"}) is False
    metrics = exporter.metrics()
    assert metrics["accepted_spans"] == metrics["queued_spans"] == 0
    assert metrics["buffered_bytes"] == 0
    assert metrics["dropped_worker_start"] == 1

    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert exporter.force_flush(timeout_millis=1_000)
    rows = _rows(path)
    assert [row["name"] for row in rows] == ["looplab.exporter.loss"]
    assert rows[0]["attributes"]["looplab.exporter.dropped.worker_start"] == 1
    assert exporter.shutdown(timeout_millis=1_000)


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock independence regression")
def test_live_index_lock_does_not_block_the_hot_export_queue(tmp_path):
    """A cold index rebuild owns .spans-index.lock, never the lifecycle writer fence."""
    import fcntl

    lock_fd = os.open(tmp_path / ".spans-index.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    exporter = AsyncJsonlSpanExporter(
        tmp_path / "spans.jsonl", lifecycle_fence=True)
    try:
        assert exporter.export({"name": "while-index-builds"})
        assert exporter.force_flush(timeout_millis=1_000)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        exporter.shutdown(timeout_millis=1_000)


def test_delegate_failure_is_attempted_once_and_reported_without_recursion(tmp_path):
    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(path, loss_receipt_interval_s=0.01)
    real_export = exporter._writer._export_lines
    attempts = 0

    def fail_target_once(lines, **kwargs):
        nonlocal attempts
        if any(b'"unstable"' in row for row in lines):
            attempts += 1
            raise OSError("storage rejected the row")
        return real_export(lines, **kwargs)

    exporter._writer._export_lines = fail_target_once
    assert exporter.export({"name": "unstable"})
    assert exporter.force_flush(timeout_millis=2_000)
    assert attempts == 1, "retry could double-export after an ambiguous post-write failure"
    metrics = exporter.metrics()
    assert metrics["export_failures"] == 1 and metrics["dropped_spans"] == 0
    rows = _rows(path)
    assert [row["name"] for row in rows] == ["looplab.exporter.loss"]
    assert rows[0]["attributes"]["looplab.exporter.export_failures"] == 1
    summary = build_trace_view(
        SimpleNamespace(run_id="run", task_id="task", total_eval_seconds=0.0), rows
    )["summary"]
    assert summary["dropped_spans"] == 0 and summary["export_failures"] == 1
    assert exporter.shutdown(timeout_millis=1_000)


def test_ambiguous_loss_receipt_failure_never_retries_or_double_counts(tmp_path):
    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(
        path, max_queue_bytes=1, loss_receipt_interval_s=0.01)
    # STILL THE SINGLE-ROW SEAM, on purpose: a loss receipt is written through `_export_line` so it
    # cannot be evicted by — or batched behind — the queue whose loss it reports.
    real_export = exporter._writer._export_line
    receipt_attempts = 0

    def append_then_raise(line, **kwargs):
        nonlocal receipt_attempts
        assert b'"looplab.exporter.loss"' in line
        receipt_attempts += 1
        real_export(line, **kwargs)
        raise OSError("post-append identity check failed")

    exporter._writer._export_line = append_then_raise
    assert exporter.export({"name": "cannot-fit"}) is False
    assert _wait(lambda: exporter.metrics()["loss_receipt_failures"] == 1)
    # The first attempt did commit. Repeated barriers must not retry its delta and turn one drop
    # into two durable receipts (ordinary accepted rows use the same ambiguity rule).
    assert exporter.force_flush(timeout_millis=1_000)
    assert exporter.force_flush(timeout_millis=1_000)
    assert receipt_attempts == 1
    rows = _rows(path)
    assert len(rows) == 1
    summary = build_trace_view(
        SimpleNamespace(run_id="run", task_id="task", total_eval_seconds=0.0), rows
    )["summary"]
    assert summary["dropped_spans"] == 1
    assert exporter.shutdown(timeout_millis=1_000)


def test_force_flush_is_a_no_late_append_barrier_for_a_trace_rewrite(tmp_path):
    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(path, lifecycle_fence=True)
    real_export = exporter._writer._export_lines
    started = threading.Event()
    release = threading.Event()

    def blocked(lines, **kwargs):
        started.set()
        assert release.wait(2)
        return real_export(lines, **kwargs)

    exporter._writer._export_lines = blocked
    try:
        assert exporter.export({"name": "old-one"})
        assert started.wait(1)
        assert exporter.export({"name": "old-two"})
        assert exporter.force_flush(timeout_millis=10) is False
        release.set()
        assert exporter.force_flush(timeout_millis=2_000) is True

        replacement = b'{"name":"replacement"}\n'
        path.write_bytes(replacement)
        time.sleep(0.1)
        assert path.read_bytes() == replacement, "a drained worker appended behind trace clear"
        assert exporter.metrics()["worker_alive"] is False
    finally:
        release.set()
        exporter.shutdown(timeout_millis=2_000)


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock race regression")
def test_timed_out_shutdown_fences_a_row_waiting_behind_trace_clear_lock(tmp_path):
    """A timeout may stop waiting, but must not permit an old row to commit after clear/reset."""
    import fcntl
    from looplab.core.trace_files import TRACE_WRITER_LOCK_NAME
    from looplab.events.span_index import span_destructive_write_guard

    path = tmp_path / "spans.jsonl"
    lock_path = tmp_path / TRACE_WRITER_LOCK_NAME
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    exporter = AsyncJsonlSpanExporter(path, lifecycle_fence=True)
    clear_finished = threading.Event()

    try:
        assert exporter.export({"name": "stale-attempt"})
        assert _wait(lambda: exporter.metrics()["buffered_bytes"] > 0)
        assert exporter.force_flush(timeout_millis=10) is False
        assert exporter.shutdown(timeout_millis=0) is False

        def clear_trace():
            with span_destructive_write_guard(path, required=True):
                path.write_bytes(b'{"name":"replacement"}\n')
            clear_finished.set()

        clear = threading.Thread(target=clear_trace)
        clear.start()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_fd = -1
        clear.join(timeout=2)
        assert not clear.is_alive() and clear_finished.is_set()
        assert _wait(lambda: not exporter.metrics()["worker_alive"])
        time.sleep(0.05)
        assert path.read_bytes() == b'{"name":"replacement"}\n'
        assert exporter.metrics()["dropped_shutdown_timeout"] == 1
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        exporter.shutdown(timeout_millis=1_000)


def test_writer_past_precommit_finishes_before_destructive_rewrite(tmp_path):
    """The other timeout ordering: committed writer first, then clear; never clear then late append."""
    from looplab.events.span_index import span_destructive_write_guard

    path = tmp_path / "spans.jsonl"
    exporter = AsyncJsonlSpanExporter(path, lifecycle_fence=True)
    real_guarded = exporter._writer._export_lines_guarded
    past_precommit = threading.Event()
    release = threading.Event()
    clear_finished = threading.Event()

    def pause_inside_guard(lines):
        past_precommit.set()
        assert release.wait(2)
        return real_guarded(lines)

    exporter._writer._export_lines_guarded = pause_inside_guard
    assert exporter.export({"name": "old-but-guarded"})
    assert past_precommit.wait(1)
    assert exporter.force_flush(timeout_millis=10) is False
    assert exporter.shutdown(timeout_millis=0) is False

    def clear_trace():
        with span_destructive_write_guard(path, required=True):
            path.write_bytes(b'{"name":"replacement"}\n')
        clear_finished.set()

    clear = threading.Thread(target=clear_trace)
    clear.start()
    assert not clear_finished.wait(0.05), "clear bypassed the active trace-writer guard"
    release.set()
    clear.join(timeout=2)
    assert not clear.is_alive() and clear_finished.is_set()
    assert _wait(lambda: not exporter.metrics()["worker_alive"])
    assert path.read_bytes() == b'{"name":"replacement"}\n'
    assert exporter.metrics()["exported_spans"] == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork regression")
def test_fork_child_discards_parent_owned_queue_without_double_export(tmp_path):
    import fcntl
    import signal
    from looplab.core.trace_files import TRACE_WRITER_LOCK_NAME

    path = tmp_path / "spans.jsonl"
    lock_fd = os.open(tmp_path / TRACE_WRITER_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    exporter = AsyncJsonlSpanExporter(
        path, lifecycle_fence=True, max_queue_spans=4)
    assert exporter.export({"name": "parent-active"})
    assert _wait(lambda: exporter.metrics()["buffered_bytes"] > 0)
    assert exporter.export({"name": "parent-queued"})

    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions are made from the parent
        try:
            os.close(lock_fd)  # do not keep the parent's shared flock description alive
            ok = exporter.export({"name": "child"})
            flushed = exporter.force_flush(timeout_millis=2_000)
        except BaseException:
            os._exit(3)
        os._exit(0 if ok and flushed else 4)

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    assert exporter.force_flush(timeout_millis=2_000)
    status = None
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate
                break
            time.sleep(0.01)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    assert status is not None and os.waitstatus_to_exitcode(status) == 0
    names = [row["name"] for row in _rows(path)]
    assert sorted(names) == ["child", "parent-active", "parent-queued"]
    assert all(names.count(name) == 1 for name in names)
    assert exporter.shutdown(timeout_millis=1_000)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork regression")
@pytest.mark.parametrize("deceptive_raw_kind", ["proxy", "fileio-subclass"])
def test_fork_child_raw_closes_a_row_buffered_between_write_and_flush(
        tmp_path, monkeypatch, deceptive_raw_kind):
    """Child cleanup must distrust a proxy raw.close that flushes the parent's buffered row."""
    import builtins
    import io
    import signal
    import looplab.core.tracing as tracing

    path = tmp_path / "spans.jsonl"
    real_open = builtins.open
    wrote_before_flush = threading.Event()
    release = threading.Event()

    class DeceptiveRaw:
        def __init__(self, buffered):
            self._buffered = buffered

        def close(self):
            # Calling this in the fork child would flush its copy of the parent's pending row.
            self._buffered.close()

    class DeceptiveFileIO(io.FileIO):
        """A matching-fd FileIO subclass whose inherited close dynamically calls this flush."""

        def __init__(self, buffered):
            self._buffered = buffered
            super().__init__(buffered.fileno(), mode="rb+", closefd=False)

        def flush(self):
            self._buffered.close()

    class PauseAfterBufferedWrite:
        def __init__(self, inner):
            self._inner = inner
            self.raw = (DeceptiveRaw(inner) if deceptive_raw_kind == "proxy"
                        else DeceptiveFileIO(inner))

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def write(self, data):
            written = self._inner.write(data)
            wrote_before_flush.set()
            assert release.wait(3)
            return written

    def controlled_open(target, *args, **kwargs):
        stream = real_open(target, *args, **kwargs)
        return PauseAfterBufferedWrite(stream) if os.fspath(target) == os.fspath(path) else stream

    monkeypatch.setattr(tracing, "open", controlled_open, raising=False)
    exporter = AsyncJsonlSpanExporter(path, lifecycle_fence=True)
    assert exporter.export({"name": "parent"})
    assert wrote_before_flush.wait(1)
    assert path.stat().st_size == 0, "test must fork while the row exists only in BufferedRandom"

    pid = os.fork()
    if pid == 0:  # pragma: no cover - parent checks callback completion and durable bytes
        sentinel_fd = -1
        try:
            # Simulate a delayed quarantined finalizer after the child has opened another file.
            # The inherited fd must remain occupied by its /dev/null tombstone, so this close cannot
            # flush into or close the newly opened descriptor.
            if len(tracing._EXPORT_FORK_QUARANTINE) != 1:
                os._exit(3)
            quarantined = tracing._EXPORT_FORK_QUARANTINE.pop()
            sentinel_fd = os.open(
                tmp_path / "child-sentinel", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            quarantined.raw.close()
            os.write(sentinel_fd, b"safe")
            os.close(sentinel_fd)
            sentinel_fd = -1
        except BaseException:
            if sentinel_fd >= 0:
                try:
                    os.close(sentinel_fd)
                except OSError:
                    pass
            os._exit(4)
        os._exit(0)

    release.set()
    assert exporter.force_flush(timeout_millis=2_000)
    status = None
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate
                break
            time.sleep(0.01)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    assert status is not None and os.waitstatus_to_exitcode(status) == 0
    assert (tmp_path / "child-sentinel").read_bytes() == b"safe"
    rows = _rows(path)
    assert [row["name"] for row in rows] == ["parent"]
    assert exporter.shutdown(timeout_millis=1_000)


def test_tracer_facade_and_finalizer_place_the_flush_barriers():
    from looplab.engine import finalize
    from looplab.engine.orchestrator import Engine

    assert hasattr(Tracer, "force_flush") and hasattr(Tracer, "shutdown")
    finalizer = inspect.getsource(finalize.finalize_run)
    assert finalizer.index("_flush_trace_exporter(engine)") < finalizer.index("load_span_tail(")
    # `Engine.run` still ends its exporter's lifetime in a `finally` -- but the shutdown call itself
    # now lives in `retire_tracer`, because the CLI's guarded-abort handler defers that retirement
    # and runs it after the terminal report (docs/53 §2c). Assert BOTH halves: the `finally` reaches
    # the retirement, and the retirement is still a BOUNDED shutdown. Asserting only the first would
    # go green on a `retire_tracer` that had quietly stopped shutting anything down.
    run = inspect.getsource(Engine.run)
    assert "finally:" in run and "retire_tracer()" in run
    retire = inspect.getsource(Engine.retire_tracer)
    assert "shutdown" in retire and "TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS" in retire
