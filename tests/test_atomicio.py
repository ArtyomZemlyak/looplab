from __future__ import annotations

import os

import pytest

import looplab.core.atomicio as atomicio


def _temp_files(directory) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp"))


def test_strict_atomic_write_syncs_contents_then_publishes_directory(tmp_path, monkeypatch):
    target = tmp_path / "receipt.json"
    calls: list[tuple[str, object]] = []
    real_replace = atomicio._strict_replace

    def record_file_sync(fileno: int) -> None:
        calls.append(("file", os.fstat(fileno).st_size))

    def record_replace(source, destination) -> None:
        source_path = os.fspath(source)
        calls.append(("replace", source_path))
        assert os.path.dirname(source_path) == os.fspath(tmp_path)
        assert os.path.basename(source_path).startswith(".receipt.json.")
        real_replace(source, destination)

    def record_parent_sync(path) -> None:
        calls.append(("parent", path))
        assert target.read_bytes() == b'{"ok":true}'

    monkeypatch.setattr(atomicio, "strict_fsync", record_file_sync)
    monkeypatch.setattr(atomicio, "_strict_replace", record_replace)
    monkeypatch.setattr(atomicio, "strict_fsync_parent", record_parent_sync)

    atomicio.strict_atomic_write_bytes(target, b'{"ok":true}')

    assert [name for name, _value in calls] == ["file", "replace", "parent"]
    assert calls[0] == ("file", len(b'{"ok":true}'))
    assert calls[2] == ("parent", target)
    assert target.read_bytes() == b'{"ok":true}'
    assert _temp_files(tmp_path) == []


def test_strict_atomic_write_durably_publishes_new_parent_chain_before_temp(
    tmp_path, monkeypatch
):
    outer = tmp_path / "new"
    inner = outer / "nested"
    target = inner / "receipt.json"
    calls: list[tuple[str, object]] = []
    real_mkstemp = atomicio.tempfile.mkstemp
    real_replace = atomicio._strict_replace

    def record_directory(directory) -> None:
        calls.append(("directory", directory))
        directory.mkdir(exist_ok=True)

    def record_mkstemp(*args, **kwargs):
        calls.append(("temp", kwargs["dir"]))
        return real_mkstemp(*args, **kwargs)

    def record_file_sync(fileno: int) -> None:
        calls.append(("file", os.fstat(fileno).st_size))

    def record_replace(source, destination) -> None:
        calls.append(("replace", destination))
        real_replace(source, destination)

    monkeypatch.setattr(atomicio, "_strict_publish_directory", record_directory)
    monkeypatch.setattr(atomicio, "strict_fsync_parent", lambda path: calls.append(("parent", path)))
    monkeypatch.setattr(atomicio.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(atomicio, "strict_fsync", record_file_sync)
    monkeypatch.setattr(atomicio, "_strict_replace", record_replace)

    atomicio.strict_atomic_write_bytes(target, b"durable")

    assert calls == [
        ("directory", outer),
        ("directory", inner),
        ("temp", os.fspath(inner)),
        ("file", len(b"durable")),
        ("replace", target),
        ("parent", target),
    ]
    assert target.read_bytes() == b"durable"
    assert _temp_files(inner) == []


def test_strict_atomic_write_sync_failure_preserves_destination_and_cleans_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "receipt.json"
    target.write_bytes(b"previous")
    failure = OSError("strict sync unavailable")

    def fail_sync(_fileno: int) -> None:
        raise failure

    monkeypatch.setattr(atomicio, "strict_fsync", fail_sync)

    with pytest.raises(OSError) as caught:
        atomicio.strict_atomic_write_bytes(target, b"replacement")

    assert caught.value is failure
    assert target.read_bytes() == b"previous"
    assert _temp_files(tmp_path) == []


def test_strict_atomic_write_replace_failure_is_propagated_and_cleans_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "receipt.json"
    failure = PermissionError("rename denied")
    parent_sync_called = False

    monkeypatch.setattr(atomicio, "strict_fsync", lambda _fileno: None)

    def fail_replace(_source, _destination) -> None:
        raise failure

    def record_parent_sync(_path) -> None:
        nonlocal parent_sync_called
        parent_sync_called = True

    monkeypatch.setattr(atomicio, "_strict_replace", fail_replace)
    monkeypatch.setattr(atomicio, "strict_fsync_parent", record_parent_sync)

    with pytest.raises(PermissionError) as caught:
        atomicio.strict_atomic_write_text(target, "replacement")

    assert caught.value is failure
    assert not target.exists()
    assert parent_sync_called is False
    assert _temp_files(tmp_path) == []


def test_strict_atomic_write_parent_sync_failure_is_propagated_without_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "receipt.json"
    failure = OSError("directory sync unavailable")

    monkeypatch.setattr(atomicio, "strict_fsync", lambda _fileno: None)

    def fail_parent_sync(_path) -> None:
        raise failure

    monkeypatch.setattr(atomicio, "strict_fsync_parent", fail_parent_sync)

    with pytest.raises(OSError) as caught:
        atomicio.strict_atomic_write_bytes(target, b"published")

    assert caught.value is failure
    assert target.read_bytes() == b"published"
    assert _temp_files(tmp_path) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through rename contract")
def test_windows_strict_replace_requests_write_through(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    calls = []

    def record_move(src, dst, *, replace):
        calls.append((src, dst, replace))
        os.replace(src, dst)

    source.write_bytes(b"durable")
    monkeypatch.setattr(atomicio, "_windows_move_write_through", record_move)

    atomicio._strict_replace(source, destination)

    assert calls == [(source, destination, True)]
    assert destination.read_bytes() == b"durable"


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through directory publication")
def test_windows_missing_directory_is_published_by_write_through_move(tmp_path, monkeypatch):
    directory = tmp_path / "new"
    calls = []

    def record_move(src, dst, *, replace):
        calls.append((dst, replace))
        os.rename(src, dst)

    monkeypatch.setattr(atomicio, "_windows_move_write_through", record_move)

    atomicio._strict_publish_directory(directory)

    assert calls == [(directory, False)]
    assert directory.is_dir()
    assert _temp_files(tmp_path) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through integration")
def test_windows_strict_atomic_write_publishes_a_missing_parent_chain(tmp_path):
    target = tmp_path / "new" / "nested" / "receipt.json"

    atomicio.strict_atomic_write_bytes(target, b"durable")

    assert target.read_bytes() == b"durable"
    assert _temp_files(target.parent) == []
