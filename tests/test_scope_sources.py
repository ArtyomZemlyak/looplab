from __future__ import annotations

import hashlib
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import orjson
import pytest

from looplab.events.eventstore import EventStore
from looplab.serve import scope_sources
from looplab.serve.scope_sources import (
    ScopeSourceCapacityError,
    ScopeSourceChangedError,
    ScopeSourceCorruptError,
    capture_scope_source,
    probe_scope_log_sig,
    scope_event_size,
)


def _line(seq: object, event_type: str, data: dict | None = None, **extra: object) -> bytes:
    record = {
        "v": 1,
        "seq": seq,
        "ts": float(int(seq)) + 0.25 if type(seq) is int else 0.25,
        "type": event_type,
        "data": data or {},
        **extra,
    }
    return orjson.dumps(record) + b"\n"


def _run(root: Path, run_id: str = "run-a") -> tuple[Path, bytes]:
    run_dir = root / run_id
    run_dir.mkdir()
    raw = b"".join(
        (
            _line(0, "setup_started", {"phase": "task+data"}),
            _line(1, "run_started", {"run_id": run_id, "task_id": "task-a"}),
            _line(2, "run_finished", {"status": "ok"}),
        )
    )
    (run_dir / "events.jsonl").write_bytes(raw)
    return run_dir, raw


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")


def test_capture_freezes_valid_source_and_compatible_revision(tmp_path):
    run_dir, raw = _run(tmp_path)
    task_raw = orjson.dumps({"id": "task-a", "comparison_contract": {"metric": "score"}})
    config_raw = orjson.dumps({"max_iterations": 3})
    (run_dir / "task.snapshot.json").write_bytes(task_raw)
    (run_dir / "config.snapshot.json").write_bytes(config_raw)

    source = capture_scope_source(tmp_path, "run-a")

    assert source.run_dir == run_dir
    assert isinstance(source.events, tuple)
    assert [event.seq for event in source.events] == [0, 1, 2]
    assert source.task_doc == {
        "id": "task-a",
        "comparison_contract": {"metric": "score"},
    }
    assert source.config_doc == {"max_iterations": 3}
    assert source.event_bytes == len(raw)
    assert source.revision["events_digest"] == hashlib.sha256(raw).hexdigest()
    assert source.revision["task_snapshot_digest"] == hashlib.sha256(task_raw).hexdigest()
    assert source.revision["config_snapshot_digest"] == hashlib.sha256(config_raw).hexdigest()
    assert source.revision["tail_seq"] == 2
    assert source.revision["event_count"] == 3
    assert source.revision["event_bytes"] == len(raw)
    assert source.revision["log_sig"][:2] == [
        "run-a",
        source.revision["generation"],
    ]
    assert len(source.revision["log_sig"]) == 7
    assert len(source.revision["generation"]) == 64
    with pytest.raises(FrozenInstanceError):
        source.event_bytes = 0  # type: ignore[misc]


def test_capture_expands_atomic_event_batch_as_contiguous_logical_events(tmp_path):
    run_dir = tmp_path / "batch-run"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "batch-run", "task_id": "task-a"})
    store.append_many([
        ("timeline_note", {"n": 1}),
        ("timeline_note", {"n": 2}),
    ])

    source = capture_scope_source(tmp_path, "batch-run")

    assert [event.seq for event in source.events] == [0, 1, 2]
    assert [event.type for event in source.events] == [
        "run_started", "timeline_note", "timeline_note"]
    assert source.revision["event_count"] == 3
    assert source.revision["tail_seq"] == 2
    assert len((run_dir / "events.jsonl").read_bytes().splitlines()) == 2


def test_capture_uses_stable_missing_snapshot_digest(tmp_path):
    _run(tmp_path)
    source = capture_scope_source(tmp_path, "run-a")
    missing = hashlib.sha256(b"<missing>").hexdigest()
    assert source.task_doc is None
    assert source.config_doc == {}
    assert source.revision["task_snapshot_digest"] == missing
    assert source.revision["config_snapshot_digest"] == missing


def test_capture_ignores_only_a_torn_final_fragment_but_hashes_it(tmp_path):
    run_dir, raw = _run(tmp_path)
    torn = b'{"v":1,"seq":3,"type":"node_started"'
    (run_dir / "events.jsonl").write_bytes(raw + torn)

    source = capture_scope_source(tmp_path, "run-a")

    assert [event.seq for event in source.events] == [0, 1, 2]
    assert source.event_bytes == len(raw + torn)
    assert source.revision["events_digest"] == hashlib.sha256(raw + torn).hexdigest()


@pytest.mark.parametrize(
    "bad_line",
    [
        b'{"v":1,"seq":3,"type":\n',
        b"[]\n",
        b"\n",
    ],
)
def test_capture_rejects_every_corrupt_complete_line(tmp_path, bad_line):
    run_dir, raw = _run(tmp_path)
    (run_dir / "events.jsonl").write_bytes(raw + bad_line)
    with pytest.raises(ScopeSourceCorruptError):
        capture_scope_source(tmp_path, "run-a")


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (_line(3, "node_started", {}, v=2), "unsupported version"),
        (_line("3", "node_started"), "invalid event"),
        (_line(4, "node_started"), "not contiguous"),
    ],
)
def test_capture_rejects_version_noninteger_and_sequence_gaps(
    tmp_path, replacement, message
):
    run_dir, raw = _run(tmp_path)
    (run_dir / "events.jsonl").write_bytes(raw + replacement)
    with pytest.raises(ScopeSourceCorruptError, match=message):
        capture_scope_source(tmp_path, "run-a")


@pytest.mark.parametrize("started_id", ["run-b", None])
def test_capture_rejects_missing_or_wrong_correlated_run_started(tmp_path, started_id):
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    records = [_line(0, "setup_started")]
    if started_id is not None:
        records.append(_line(1, "run_started", {"run_id": started_id}))
    (run_dir / "events.jsonl").write_bytes(b"".join(records))
    with pytest.raises(ScopeSourceCorruptError, match="run_started"):
        capture_scope_source(tmp_path, "run-a")


def test_capture_rejects_nonfinite_timestamp(tmp_path):
    run_dir, raw = _run(tmp_path)
    nonfinite = b'{"v":1,"seq":3,"ts":1e400,"type":"node_started","data":{}}\n'
    (run_dir / "events.jsonl").write_bytes(raw + nonfinite)
    with pytest.raises(ScopeSourceCorruptError):
        capture_scope_source(tmp_path, "run-a")


def test_capture_enforces_event_and_snapshot_capacity(tmp_path, monkeypatch):
    run_dir, raw = _run(tmp_path)
    with pytest.raises(ScopeSourceCapacityError):
        capture_scope_source(tmp_path, "run-a", event_budget_bytes=len(raw) - 1)

    (run_dir / "task.snapshot.json").write_bytes(b'{"long":"value"}')
    monkeypatch.setattr(scope_sources, "MAX_SCOPE_TASK_BYTES", 4)
    with pytest.raises(ScopeSourceCapacityError, match="task.snapshot.json"):
        capture_scope_source(tmp_path, "run-a")

    (run_dir / "task.snapshot.json").unlink()
    (run_dir / "config.snapshot.json").write_bytes(b'{"long":"value"}')
    monkeypatch.setattr(scope_sources, "MAX_SCOPE_CONFIG_BYTES", 4)
    with pytest.raises(ScopeSourceCapacityError, match="config.snapshot.json"):
        capture_scope_source(tmp_path, "run-a")


@pytest.mark.parametrize("payload", [b"[]", b"not-json"])
def test_capture_rejects_invalid_or_nonobject_config(tmp_path, payload):
    run_dir, _ = _run(tmp_path)
    (run_dir / "config.snapshot.json").write_bytes(payload)
    with pytest.raises(ScopeSourceCorruptError, match="config snapshot"):
        capture_scope_source(tmp_path, "run-a")


@pytest.mark.parametrize("run_id", ["../run-a", "nested/run-a", "nested\\run-a", "C:run-a"])
def test_capture_rejects_non_child_run_ids(tmp_path, run_id):
    _run(tmp_path)
    with pytest.raises(ScopeSourceCorruptError, match="direct child"):
        capture_scope_source(tmp_path, run_id)


def test_capture_rejects_symlink_run_directory(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    _run(real_root)
    _symlink(tmp_path / "run-a", real_root / "run-a", directory=True)
    with pytest.raises(ScopeSourceCorruptError, match="symlink|reparse"):
        capture_scope_source(tmp_path, "run-a")


def test_capture_rejects_symlink_event_file(tmp_path):
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    target = tmp_path / "outside.jsonl"
    target.write_bytes(_line(0, "run_started", {"run_id": "run-a"}))
    _symlink(run_dir / "events.jsonl", target)
    with pytest.raises(ScopeSourceCorruptError, match="symlink|reparse"):
        capture_scope_source(tmp_path, "run-a")


def test_capture_detects_growth_during_descriptor_read(tmp_path, monkeypatch):
    run_dir, _ = _run(tmp_path)
    original = scope_sources._read_exact
    changed = False

    def grow_after_read(descriptor, expected_size):
        nonlocal changed
        raw = original(descriptor, expected_size)
        if not changed:
            changed = True
            with (run_dir / "events.jsonl").open("ab") as handle:
                handle.write(_line(3, "node_started"))
        return raw

    monkeypatch.setattr(scope_sources, "_read_exact", grow_after_read)
    with pytest.raises(ScopeSourceChangedError, match="changed while reading"):
        capture_scope_source(tmp_path, "run-a")


def test_capture_detects_same_size_aba_rewrite_during_descriptor_read(
    tmp_path, monkeypatch
):
    run_dir, raw = _run(tmp_path)
    event_path = run_dir / "events.jsonl"
    original = scope_sources._read_exact
    original_mtime = event_path.stat().st_mtime_ns
    changed = False

    if os.name == "nt":
        # FILE_BASIC_INFO.ChangeTime is a timestamp, not a generation counter.  A fast A/B/A rewrite
        # can complete inside one Windows clock tick and make two genuine observations numerically
        # equal.  Drive the already-tested comparison branch with a deterministic generation delta;
        # fixed sleeps merely turn this safety test into a timing lottery.
        native_change_time = scope_sources._descriptor_change_time

        def deterministic_change_time(descriptor):
            observed = native_change_time(descriptor)
            assert type(observed) is int
            return observed + int(changed)

        monkeypatch.setattr(
            scope_sources, "_descriptor_change_time", deterministic_change_time)

    def rewrite_and_restore(descriptor, expected_size):
        nonlocal changed
        captured = original(descriptor, expected_size)
        if not changed:
            changed = True
            replacement = raw.replace(b'"status":"ok"', b'"status":"no"')
            assert len(replacement) == len(raw)
            event_path.write_bytes(replacement)
            event_path.write_bytes(raw)
            os.utime(event_path, ns=(original_mtime, original_mtime))
        return captured

    monkeypatch.setattr(scope_sources, "_read_exact", rewrite_and_restore)
    with pytest.raises(ScopeSourceChangedError, match="changed while reading"):
        capture_scope_source(tmp_path, "run-a")


def test_capture_detects_path_swap_before_final_validation(tmp_path, monkeypatch):
    run_dir, raw = _run(tmp_path)
    replacement = run_dir / "replacement.jsonl"
    replacement.write_bytes(raw)
    original = scope_sources._revalidate_file
    swapped = False

    def swap_then_validate(captured):
        nonlocal swapped
        if captured.path.name == "events.jsonl" and not swapped:
            swapped = True
            replacement.replace(captured.path)
        return original(captured)

    monkeypatch.setattr(scope_sources, "_revalidate_file", swap_then_validate)
    with pytest.raises(ScopeSourceChangedError, match="changed during capture"):
        capture_scope_source(tmp_path, "run-a")


def test_probe_reads_bounded_first_event_and_returns_compatible_sig(tmp_path):
    _run(tmp_path)
    source = capture_scope_source(tmp_path, "run-a")
    signature = probe_scope_log_sig(tmp_path, "run-a")
    assert signature == source.revision["log_sig"]
    assert len(signature) == 7


def test_probe_does_not_parse_later_lines(tmp_path):
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    first = _line(0, "setup_started")
    (run_dir / "events.jsonl").write_bytes(first + b"broken-complete-line\n")
    signature = probe_scope_log_sig(tmp_path, "run-a")
    assert signature[0] == "run-a"
    assert len(signature[1]) == 64


def test_probe_rejects_oversize_log_and_first_line(tmp_path, monkeypatch):
    run_dir, raw = _run(tmp_path)
    monkeypatch.setattr(scope_sources, "MAX_SCOPE_EVENT_BYTES", len(raw) - 1)
    with pytest.raises(ScopeSourceCapacityError, match="per-run"):
        probe_scope_log_sig(tmp_path, "run-a")

    monkeypatch.setattr(scope_sources, "MAX_SCOPE_EVENT_BYTES", 1024)
    (run_dir / "events.jsonl").write_bytes(b"{" + b"x" * 20 + b"}\n")
    with pytest.raises(ScopeSourceCapacityError, match="first event"):
        probe_scope_log_sig(tmp_path, "run-a", first_line_limit=8)


def test_probe_detects_log_growth_during_read(tmp_path, monkeypatch):
    run_dir, _ = _run(tmp_path)
    original = scope_sources._read_first_complete_line
    changed = False

    def grow_after_first(descriptor, file_size, limit):
        nonlocal changed
        line = original(descriptor, file_size, limit)
        if not changed:
            changed = True
            with (run_dir / "events.jsonl").open("ab") as handle:
                handle.write(_line(3, "node_started"))
        return line

    monkeypatch.setattr(scope_sources, "_read_first_complete_line", grow_after_first)
    with pytest.raises(ScopeSourceChangedError, match="changed while reading"):
        probe_scope_log_sig(tmp_path, "run-a")


def test_probe_rejects_wrong_first_run_started_identity(tmp_path):
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_bytes(
        _line(0, "run_started", {"run_id": "run-b"})
    )
    with pytest.raises(ScopeSourceCorruptError, match="run_started"):
        probe_scope_log_sig(tmp_path, "run-a")


def test_capture_rejects_event_file_appearing_through_optional_alias(tmp_path):
    """A non-regular direct child is rejected even if its target contains valid JSON."""
    run_dir, _ = _run(tmp_path)
    (run_dir / "task.snapshot.json").mkdir()
    with pytest.raises(ScopeSourceCorruptError, match="regular file"):
        capture_scope_source(tmp_path, "run-a")


def test_capture_event_bytes_is_the_raw_file_size(tmp_path):
    run_dir, raw = _run(tmp_path)
    torn = b"x" * 17
    (run_dir / "events.jsonl").write_bytes(raw + torn)
    source = capture_scope_source(tmp_path, "run-a")
    assert source.event_bytes == (run_dir / "events.jsonl").stat().st_size


def test_scope_event_size_returns_stat_size_without_parsing(tmp_path):
    run_dir, raw = _run(tmp_path)
    corrupt = b"not-json\n"
    (run_dir / "events.jsonl").write_bytes(raw + corrupt)
    assert scope_event_size(tmp_path, "run-a") == len(raw + corrupt)


def test_scope_event_size_enforces_limit_and_direct_regular_file(tmp_path, monkeypatch):
    run_dir, raw = _run(tmp_path)
    monkeypatch.setattr(scope_sources, "MAX_SCOPE_EVENT_BYTES", len(raw) - 1)
    with pytest.raises(ScopeSourceCapacityError, match="per-run"):
        scope_event_size(tmp_path, "run-a")

    monkeypatch.setattr(scope_sources, "MAX_SCOPE_EVENT_BYTES", len(raw) + 1)
    (run_dir / "events.jsonl").unlink()
    (run_dir / "events.jsonl").mkdir()
    with pytest.raises(ScopeSourceCorruptError, match="regular file"):
        scope_event_size(tmp_path, "run-a")


def test_scope_event_size_rejects_non_child_run_id(tmp_path):
    _run(tmp_path)
    with pytest.raises(ScopeSourceCorruptError, match="direct child"):
        scope_event_size(tmp_path, "../run-a")
