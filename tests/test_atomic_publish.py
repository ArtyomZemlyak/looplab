"""`atomic_write_bytes` publishes the NAME, not only the bytes.

It fsynced the temp file's CONTENT and then renamed with no parent-directory sync. Content and name
live in different places: the bytes are in the file's inode, the rename that gives them the
destination's name is an entry in the parent DIRECTORY. Until that directory is synced a crash can
leave the name pointing at the old inode — or at nothing — with the good bytes on disk under a temp
name nobody will ever look for. That is the exact failure the helper exists to prevent, and it did
the expensive half and skipped the cheap one.

Best-effort, deliberately: this tier's callers write DERIVED artifacts, where an unconfirmed sync
must not fail the write (`best_effort_fsync_parent`'s own docstring argues it). `strict_atomic_
write_bytes` is the fail-closed tier beside it and is unchanged.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _source_scan import function_tree

from looplab.core import atomicio


def _synced_dirs(monkeypatch) -> list[Path]:
    """Record every directory whose descriptor is handed to `best_effort_fsync`."""
    seen: list[Path] = []
    real = atomicio.best_effort_fsync_parent

    def _spy(path):
        seen.append(Path(path).parent.resolve())
        return real(path)

    monkeypatch.setattr(atomicio, "best_effort_fsync_parent", _spy)
    return seen


def test_the_parent_directory_is_published(monkeypatch, tmp_path):
    """THE DEFECT. MUTATION: drop the call -> the rename is unsynced and a crash loses the name."""
    seen = _synced_dirs(monkeypatch)
    target = tmp_path / "sub" / "snapshot.json"
    atomicio.atomic_write_bytes(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'
    assert target.parent.resolve() in seen


def test_it_is_the_DESTINATION_parent_and_not_the_temp_dir(monkeypatch, tmp_path):
    """`mkstemp(dir=p.parent)` keeps the temp beside the destination, so the two are the same
    directory today — pinned because a future "write to a scratch dir then move" would make them
    differ and sync the wrong one, which looks identical from the outside."""
    seen = _synced_dirs(monkeypatch)
    target = tmp_path / "deep" / "nested" / "f.bin"
    atomicio.atomic_write_bytes(target, b"x")
    assert seen == [target.parent.resolve()]


def test_a_failing_directory_sync_does_not_fail_the_write(monkeypatch, tmp_path):
    """The best-effort contract. On an object-store FUSE mount a directory sync can raise; refusing
    to publish a good derived artifact because geesefs would not confirm one fails the operation for
    a durability level this tier never needed.

    MUTATION: use `strict_fsync_parent` here -> every snapshot write on such a mount raises.
    """
    def _boom(_path):
        raise OSError("geesefs says no")

    monkeypatch.setattr(atomicio, "best_effort_fsync_parent", _boom)
    target = tmp_path / "f.json"
    with pytest.raises(OSError):
        atomicio.atomic_write_bytes(target, b"y")
    # ...the bytes still landed: the rename happened before the sync attempt.
    assert target.read_bytes() == b"y"


def test_the_publish_happens_AFTER_the_rename():
    """Ordering, by AST rather than by substring: syncing the directory before the rename publishes
    nothing, and the two calls are one line apart, so a reorder is invisible to review.

    Read from the real function body, where a comment is not a node.
    """
    tree = function_tree(atomicio.atomic_write_bytes)
    order = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "replace")
            or (isinstance(node.func, ast.Name) and node.func.id == "best_effort_fsync_parent"))]
    names = [
        (node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "replace")
            or (isinstance(node.func, ast.Name) and node.func.id == "best_effort_fsync_parent"))]
    pairs = sorted(zip(order, names))
    assert [name for _, name in pairs] == ["replace", "best_effort_fsync_parent"]


def test_atomic_write_text_gets_it_too(monkeypatch, tmp_path):
    """The text spelling delegates, so it must not have its own path."""
    seen = _synced_dirs(monkeypatch)
    target = tmp_path / "t.txt"
    atomicio.atomic_write_text(target, "hello")
    assert target.read_text() == "hello" and seen == [target.parent.resolve()]


def test_no_temp_file_survives(tmp_path):
    """Unchanged, and worth holding: the sync must not have introduced a path that leaves one."""
    atomicio.atomic_write_bytes(tmp_path / "f", b"z")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_the_strict_tier_is_untouched(tmp_path):
    """`strict_atomic_write_bytes` is the fail-closed tier for paid-work claims and keeps its own
    `strict_fsync_parent`. This change must not have merged the two."""
    target = tmp_path / "receipt.json"
    atomicio.strict_atomic_write_bytes(target, b'{"claim": 1}')
    assert target.read_bytes() == b'{"claim": 1}'
