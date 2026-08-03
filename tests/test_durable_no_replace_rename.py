"""The one durable no-replace rename that reset and deletion share (doc 25 SC-05).

`reset_route._durable_archive_move` and `deletion_service._durable_no_replace_move` were
near-identical ~45-line ctypes functions: sibling check, `lexists` guard, `_windows_move_write_through`
on nt, `renameat2`/`RENAME_NOREPLACE` on Linux, `renamex_np`/`RENAME_EXCL` on Darwin, the same errno
handling and `strict_fsync_parent`. Only the error strings differed.

The three properties they exist for are all failure-shaped, which is why they get tests rather than
trust:

* a plain `os.rename` REPLACES a raced destination — for an archive name or a quarantine directory
  that destroys whatever a concurrent operation just put there;
* the parent fsync is what keeps a receipt saying "moved" from running ahead of the filesystem;
* an unavailable primitive must raise `ENOTSUP`, never fall back to a replacing rename — a silent
  downgrade of a durability guarantee is worse than a failed operation.
"""
from __future__ import annotations

import errno
import os

import pytest

from looplab.core.atomicio import durable_no_replace_rename


@pytest.fixture
def sibling(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text("a line\n", encoding="utf-8")
    return source, tmp_path / "events.archive.jsonl"


def test_a_sibling_rename_moves_the_bytes_and_frees_the_old_name(sibling):
    source, destination = sibling
    durable_no_replace_rename(source, destination, label="replay archive")
    assert destination.read_text(encoding="utf-8") == "a line\n"
    assert not source.exists()


def test_a_directory_moves_too(tmp_path):
    """The deletion service moves a whole run DIRECTORY into quarantine, not a file."""
    source = tmp_path / "run"
    (source / "nodes").mkdir(parents=True)
    (source / "nodes" / "0.txt").write_text("x", encoding="utf-8")
    destination = tmp_path / "run.deleting"
    durable_no_replace_rename(source, destination, label="deletion quarantine")
    assert (destination / "nodes" / "0.txt").read_text(encoding="utf-8") == "x"


def test_an_existing_destination_is_refused_rather_than_replaced(sibling):
    """THE property. `os.rename` would silently clobber it, destroying an archive (or a quarantine)
    another operation just created."""
    source, destination = sibling
    destination.write_text("someone else got here first\n", encoding="utf-8")
    with pytest.raises(FileExistsError) as exc:
        durable_no_replace_rename(source, destination, label="replay archive")
    assert exc.value.errno == errno.EEXIST
    assert destination.read_text(encoding="utf-8") == "someone else got here first\n"
    assert source.exists(), "the source must survive a refused move"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_DANGLING_symlink_at_the_destination_still_counts_as_occupied(sibling):
    """A broken symlink OCCUPIES the name, and replacing it would silently discard whatever a
    concurrent operation was in the middle of creating there.

    Two layers enforce this and only one of them is ours: `lexists` (not `exists`) gives the early,
    clean error, and `RENAME_NOREPLACE` refuses the same case in the kernel. Verified by teeth-test:
    weakening `lexists` to `exists` alone changes nothing observable, because the flag catches it —
    which is exactly why the flag, not the check, is described as the guarantee.
    """
    source, destination = sibling
    destination.symlink_to(source.parent / "nothing-here")
    with pytest.raises(FileExistsError):
        durable_no_replace_rename(source, destination, label="replay archive")
    assert source.exists() and destination.is_symlink()


def test_a_non_sibling_destination_is_refused(tmp_path):
    """Both callers' invariant: the archive stays in the run dir, the quarantine stays beside the
    run. A cross-directory move would also break the single parent fsync."""
    source = tmp_path / "run" / "events.jsonl"
    source.parent.mkdir()
    source.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        durable_no_replace_rename(source, tmp_path / "elsewhere.jsonl", label="replay archive")


def test_the_label_names_the_operation_in_both_error_paths(sibling):
    """The two callers' messages were the only thing that differed between the copies, so the label
    has to actually reach them — otherwise an operator sees "rename failed" with no idea whether a
    reset or a deletion is stuck."""
    source, destination = sibling
    destination.write_text("occupied", encoding="utf-8")
    for label in ("replay archive", "deletion quarantine"):
        with pytest.raises(FileExistsError) as exc:
            durable_no_replace_rename(source, destination, label=label)
        assert label in str(exc.value)
    with pytest.raises(ValueError) as exc:
        durable_no_replace_rename(source, source.parent / "sub" / "x", label="deletion quarantine")
    assert "deletion quarantine" in str(exc.value)


def test_the_parent_directory_is_fsynced_before_the_call_returns(sibling, monkeypatch):
    """Durability, not tidiness: a receipt that says "moved" must never be ahead of the filesystem.
    A crash between the rename and the fsync can otherwise leave BOTH names missing."""
    import looplab.core.atomicio as atomicio

    synced = []
    monkeypatch.setattr(atomicio, "strict_fsync_parent", lambda path: synced.append(str(path)))
    source, destination = sibling
    durable_no_replace_rename(source, destination, label="replay archive")
    assert synced == [str(destination)]


@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="renameat2 is Linux-specific")
def test_a_missing_kernel_primitive_refuses_instead_of_downgrading(sibling, monkeypatch):
    """The most dangerous failure mode: an old kernel with no `renameat2`. Falling back to a plain
    rename would keep the operation "working" while silently dropping the no-replace guarantee — the
    exact property both callers depend on. It must raise ENOTSUP instead."""
    import ctypes

    class _NoRenameat2:
        def __getattr__(self, name):
            if name == "renameat2":
                raise AttributeError(name)
            return getattr(ctypes.CDLL(None, use_errno=True), name)

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _NoRenameat2())
    source, destination = sibling
    with pytest.raises(OSError) as exc:
        durable_no_replace_rename(source, destination, label="replay archive")
    assert exc.value.errno == errno.ENOTSUP
    assert source.exists() and not destination.exists()


def test_both_callers_delegate_rather_than_keeping_their_own_ctypes():
    """A grep guard on the collapse: `renameat2`/`renamex_np` outside `core/atomicio.py` means a copy
    came back, and a copy is where a no-replace flag quietly becomes a replacing rename."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "looplab"
    offenders = [f"{path.relative_to(root)}:{index}"
                 for path in sorted(root.rglob("*.py")) if path.name != "atomicio.py"
                 for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
                 if "renameat2" in line or "renamex_np" in line]
    assert offenders == [], f"a private no-replace rename came back: {offenders}"


@pytest.mark.parametrize("module,fn,label", [
    ("looplab.serve.reset_route", "_durable_archive_move", "replay archive"),
    ("looplab.serve.deletion_service", "_durable_no_replace_move", "deletion quarantine"),
])
def test_each_caller_keeps_its_own_sibling_message_and_passes_its_label(module, fn, label):
    """The wrappers stay: each owns a domain-specific sibling rule whose message an operator reads
    ("Replay archives must remain in the run directory" vs "deletion quarantine must be a sibling of
    the run"). Only the rename mechanics moved."""
    import importlib
    import inspect

    source = inspect.getsource(getattr(importlib.import_module(module), fn))
    assert f'label="{label}"' in source
    assert "ctypes" not in source
    assert "source.parent != destination.parent" in source, (
        f"{module}.{fn} dropped its own sibling rule along with the mechanics")
