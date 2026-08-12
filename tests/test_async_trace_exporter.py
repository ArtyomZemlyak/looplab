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
    real_export = exporter._writer._export_line
    started = threading.Event()
    release = threading.Event()

    def slow_first(line, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(2)
        return real_export(line, **kwargs)

    exporter._writer._export_line = slow_first
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
    real_export = exporter._writer._export_line
    started = threading.Event()
    release = threading.Event()

    def blocked(line, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(2)
        return real_export(line, **kwargs)

    exporter._writer._export_line = blocked
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
    real_export = exporter._writer._export_line
    attempts = 0

    def fail_target_once(line, **kwargs):
        nonlocal attempts
        if b'"unstable"' in line:
            attempts += 1
            raise OSError("storage rejected the row")
        return real_export(line, **kwargs)

    exporter._writer._export_line = fail_target_once
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
    real_export = exporter._writer._export_line
    started = threading.Event()
    release = threading.Event()

    def blocked(line, **kwargs):
        started.set()
        assert release.wait(2)
        return real_export(line, **kwargs)

    exporter._writer._export_line = blocked
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
    real_guarded = exporter._writer._export_line_guarded
    past_precommit = threading.Event()
    release = threading.Event()
    clear_finished = threading.Event()

    def pause_inside_guard(line):
        past_precommit.set()
        assert release.wait(2)
        return real_guarded(line)

    exporter._writer._export_line_guarded = pause_inside_guard
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
    run = inspect.getsource(Engine.run)
    assert "finally:" in run and "shutdown" in run
