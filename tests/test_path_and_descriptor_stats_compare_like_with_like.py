"""A path stat and a descriptor stat are compared like with like, because on Windows they disagree.

Measured on the second Windows CI leg (GitHub Actions run 35804658308, review 2026-09-22 round 2):
every launch through a `task_file` answered `422 task_source_changed` — "task_file changed while it
was being read" — about a file nothing had touched. Ten rows across four test files had that one
cause (the preflight, `/api/start`, the stale-token 409 that became a 422, the web/TUI/API contract,
and the concurrent-start fixture whose first request never reached Popen).

The cause is CPython's, and it is documented in its source: `os.stat`/`os.lstat` go through
`win32_xstat`, which copies the file's CREATION time into `st_ctime` ("ctime is only deprecated from
3.12, so we copy birthtime across"), while `os.fstat` reports FILE_BASIC_INFO.ChangeTime. So
`file_identity(os.lstat(p)) != file_identity(os.fstat(fd))` for one unchanged file, and
`serve/launch.py::read_confined_task_file` compared exactly that pair. The house already has the
ladder that answers it — `serve/routers/misc.py::_read_author_file_safely`: the weak
`same_file_entry` binds the descriptor to the name ACROSS the two interfaces, and the full
`file_identity` only ever compares like with like (two `fstat`s of one descriptor, two `lstat`s of
one name). `core/run_deletion.py::run_deletion_snapshot_token` paired an `lstat` with an `fstat` the
same way for an EMPTY event log, so on Windows a just-created run could never be deleted by identity.

Driven here through `_windows_emulation.windows_path_stat_ctime`, which reproduces exactly that
divergence and nothing else; the strength the full tuple bought on POSIX (a same-size in-place
rewrite with its mtime put back) is re-proved on the descriptor pair.
"""
from __future__ import annotations

import json
import os

import pytest

from _windows_emulation import windows_path_stat_ctime

fastapi = pytest.importorskip("fastapi")


def _toy(goal: str = "g") -> dict:
    return {"kind": "quadratic", "goal": goal, "direction": "min"}


def test_the_double_reproduces_the_windows_divergence(tmp_path, monkeypatch):
    """The emulation proves itself first: one unchanged file, two interfaces, two ctimes."""
    from looplab.core.atomicio import file_identity, same_file_entry

    source = tmp_path / "task.json"
    source.write_text(json.dumps(_toy()), encoding="utf-8")
    answered = windows_path_stat_ctime(monkeypatch)
    fd = os.open(source, os.O_RDONLY)
    try:
        by_name, by_descriptor = os.lstat(source), os.fstat(fd)
    finally:
        os.close(fd)
    assert answered, "the double never fired"
    assert file_identity(by_name) != file_identity(by_descriptor)
    assert same_file_entry(by_name) == same_file_entry(by_descriptor)


def test_a_task_file_nothing_touched_is_read_where_lstat_and_fstat_disagree(tmp_path, monkeypatch):
    """The defect itself: the confined read refused a file nobody had written to since it was made."""
    from looplab.serve.launch import read_confined_task_file

    root = tmp_path / "runroot"
    root.mkdir()
    source = root / "unified.json"
    source.write_text(json.dumps({"task": _toy(), "settings": {"max_nodes": 3}}),
                      encoding="utf-8")
    windows_path_stat_ctime(monkeypatch)

    confined = read_confined_task_file(root, str(source))
    assert json.loads(confined.data) == {"task": _toy(), "settings": {"max_nodes": 3}}


def test_the_preflight_answers_200_where_lstat_and_fstat_disagree(tmp_path, monkeypatch):
    """The same defect at the HTTP boundary the Web UI, the TUI and the API share."""
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(_toy("confirmed")), encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    windows_path_stat_ctime(monkeypatch)

    response = client.post("/api/start/preflight",
                           json={"run_id": "windows-ctime", "task_file": str(task_file)})
    assert response.status_code == 200, response.text


def test_a_same_size_rewrite_during_the_read_is_still_refused(tmp_path, monkeypatch):
    """What the full tuple bought, re-proved on the pair that can still carry it. A rewrite in place
    to the same length with its mtime put back keeps (dev, ino, size, mtime) — only ctime moves, and
    it moves on the DESCRIPTOR's own two observations."""
    from fastapi import HTTPException

    from looplab.serve import launch

    root = tmp_path / "runroot"
    root.mkdir()
    source = root / "task.json"
    original = json.dumps(_toy("aaaa")).encode("utf-8")
    source.write_bytes(original)
    before = os.stat(source)

    real_read = launch._read_bounded

    def _rewrite_in_place(fd):
        data = real_read(fd)
        with open(source, "r+b") as handle:          # same inode, same length, different bytes
            handle.write(json.dumps(_toy("bbbb")).encode("utf-8"))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        return data

    monkeypatch.setattr(launch, "_read_bounded", _rewrite_in_place)
    with pytest.raises(HTTPException) as refused:
        launch.read_confined_task_file(root, str(source))
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "task_source_changed"
    after = os.stat(source)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns), (
        "the fixture failed to keep inode, size and mtime, so this proves nothing about ctime")


def test_an_empty_run_log_has_a_deletion_identity_where_lstat_and_fstat_disagree(
        tmp_path, monkeypatch):
    """`run_deletion_snapshot_token` paired an lstat with an fstat for an EMPTY event log — a run
    that was created and never wrote. On Windows that pair never matched, so the run list could not
    even state the identity a deletion must present."""
    from looplab.core.run_deletion import RunDeletionStorageError, run_deletion_snapshot_token

    log = tmp_path / "demo" / "events.jsonl"
    log.parent.mkdir()
    log.write_bytes(b"")
    first = run_deletion_snapshot_token(log, "")
    windows_path_stat_ctime(monkeypatch)
    token = run_deletion_snapshot_token(log, "")
    assert isinstance(token, str) and len(token) == 64
    # Still a refusal where it must be one: the log gained a byte.
    log.write_bytes(b"x")
    with pytest.raises(RunDeletionStorageError):
        run_deletion_snapshot_token(log, "")
    assert first  # the POSIX token was derivable all along; only the pairing was wrong
