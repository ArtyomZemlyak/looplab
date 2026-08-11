from __future__ import annotations

import builtins
import errno
import json
import os
import time
from pathlib import Path

import pytest

from looplab.core.trace_files import open_private_trace_file, trace_file_identity
from looplab.events.traceview import load_span_tail, load_spans, trace_file_revision


def _span(*, sid: str = "span", text: str = "safe") -> dict:
    return {
        "trace_id": "trace",
        "span_id": sid,
        "parent_id": None,
        "name": "generation",
        "kind": "generation",
        "start": 1.0,
        "duration_s": 0.1,
        "status": "ok",
        "attributes": {"node_id": 0, "output": text},
        "events": [],
    }


def _write_span(path: Path, **kwargs) -> None:
    path.write_text(json.dumps(_span(**kwargs)) + "\n", encoding="utf-8")


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_trace_readers_refuse_link_aliases(tmp_path, alias):
    target = tmp_path / "outside.jsonl"
    _write_span(target, text="foreign-secret-marker")
    source = tmp_path / "spans.jsonl"
    if alias == "symlink":
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable")
    else:
        try:
            os.link(target, source)
        except OSError:
            pytest.skip("hard links are unavailable")

    with pytest.raises(OSError):
        load_spans(source)
    with pytest.raises(OSError):
        load_span_tail(source)
    assert trace_file_revision(source) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_trace_reader_rejects_fifo_without_blocking(tmp_path):
    source = tmp_path / "spans.jsonl"
    os.mkfifo(source)

    started = time.monotonic()
    with pytest.raises(OSError):
        with open_private_trace_file(source):
            pass
    assert time.monotonic() - started < 1.0
    with pytest.raises(OSError):
        load_span_tail(source)
    assert trace_file_revision(source) is None


def test_trace_open_rejects_path_replacement_after_descriptor_open(tmp_path):
    source = tmp_path / "spans.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    _write_span(source, sid="original")
    _write_span(replacement, sid="replacement")

    def replacing_open(file, *args, **kwargs):
        stream = builtins.open(file, *args, **kwargs)
        os.replace(replacement, source)
        return stream

    with pytest.raises(OSError) as exc:
        with open_private_trace_file(source, open_file=replacing_open):
            pytest.fail("a swapped descriptor must not be published")
    assert exc.value.errno == getattr(errno, "ESTALE", errno.EIO)


def test_trace_append_mode_creates_and_reopens_private_regular_file(tmp_path):
    source = tmp_path / "spans.jsonl"
    with open_private_trace_file(source, "a+b") as stream:
        stream.write(b"{}\n")
        stream.flush()
        identity = trace_file_identity(os.fstat(stream.fileno()))
    with open_private_trace_file(source, expected_identity=identity) as stream:
        assert stream.read() == b"{}\n"


def test_windows_change_time_controls_destructive_trace_revision(tmp_path, monkeypatch):
    """The clear CAS follows native descriptor ChangeTime and fails closed without it."""
    from looplab.events import traceview

    source = tmp_path / "spans.jsonl"
    _write_span(source, sid="old-a")
    state = {"token": 101}
    descriptors = []

    def change_token(fd, _status=None):
        descriptors.append(fd)
        return state["token"]

    monkeypatch.setattr(traceview, "trace_file_change_token", change_token)
    initial = traceview.trace_file_revision(source)
    before = source.stat()
    replacement = source.read_bytes().replace(b'"old-a"', b'"new-a"')
    assert len(replacement) == before.st_size
    with open(source, "r+b") as stream:
        stream.write(replacement)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = source.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns)
    state["token"] = 102

    changed = traceview.trace_file_revision(source)

    assert changed is not None and changed != initial
    assert descriptors and all(isinstance(fd, int) for fd in descriptors)
    state["token"] = None
    assert traceview.trace_file_revision(source) is None


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "fifo"])
def test_public_trace_routes_fail_closed_for_non_private_source(tmp_path, alias):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    EventStore(run_dir / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task", "goal": "g",
                        "direction": "min"})
    source = run_dir / "spans.jsonl"
    foreign = tmp_path / "outside.jsonl"
    _write_span(foreign, text="foreign-secret-marker")
    if alias == "symlink":
        try:
            source.symlink_to(foreign)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable")
    elif alias == "hardlink":
        try:
            os.link(foreign, source)
        except OSError:
            pytest.skip("hard links are unavailable")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO requires POSIX")
        os.mkfifo(source)

    client = TestClient(make_app(tmp_path))
    started = time.monotonic()
    responses = [
        client.get("/api/runs/demo/trace"),
        client.get("/api/runs/demo/trace/tail"),
        client.get("/api/runs/demo/spans/span"),
        client.get("/api/runs/demo/trace/by_trace/trace"),
        client.get("/api/runs/demo/nodes/0/conversation"),
    ]
    assert time.monotonic() - started < 3.0
    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["projection"]["unavailable"] is True
        assert "foreign-secret-marker" not in response.text


def test_span_tail_fallback_is_bound_to_index_source_identity(tmp_path):
    from looplab.serve.routers.runs import _scan_span_tail

    source = tmp_path / "spans.jsonl"
    foreign = tmp_path / "outside.jsonl"
    _write_span(source, sid="original")
    identity = trace_file_identity(os.stat(source, follow_symlinks=False))
    _write_span(foreign, sid="guessed", text="foreign-secret-marker")
    os.replace(foreign, source)

    assert _scan_span_tail(
        source, "guessed", 0, expected_identity=identity) is None
