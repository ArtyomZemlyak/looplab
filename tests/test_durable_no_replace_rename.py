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

from _source_scan import iter_sources

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
    run. A cross-directory move would also break the single parent fsync.

    The DEFAULT is what is pinned here. `cross_directory=True` is a third caller's opt-in and is
    driven below; the point of it being a keyword rather than an inference from the paths is that
    passing it is a decision somebody wrote down."""
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
                 for path, source in iter_sources(root) if path.name != "atomicio.py"
                 for index, line in enumerate(source.split("\n"), 1)
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


# ------------------------------------------------------------------------------------------------
# Where the FILESYSTEM cannot provide RENAME_NOREPLACE at all.
#
# MEASURED 2026-08-13: every LoopLab run on this deployment lives under a `fuse.geesefs` mount, where
# `renameat2(..., RENAME_NOREPLACE)` returns EINVAL while the same rename with flags=0 succeeds. FUSE
# does not implement the flag. Failing closed there protects nothing — it makes run deletion
# IMPOSSIBLE, and it had been: every attempt stalled at `phase: quarantining` with `[Errno 22]`, the
# operator saw "the deletion did not complete", and 9.6 GB they had asked to remove stayed on disk.
# ------------------------------------------------------------------------------------------------
import ctypes                                                                    # noqa: E402
import errno as _errno                                                           # noqa: E402
import os as _os                                                                 # noqa: E402

import pytest                                                                    # noqa: E402

from looplab.core import atomicio as _atomicio                                    # noqa: E402


class _FlagRefusingLibc:
    """A libc whose `renameat2` refuses the FLAG the way geesefs does, and performs a plain rename
    when given flags=0 — so the fallback is exercised against a real filesystem move."""

    def __init__(self, code):
        self.code = code
        self.calls = []

    def __getattr__(self, name):
        if name != "renameat2":
            raise AttributeError(name)

        def _call(_olddirfd, oldname, _newdirfd, newname, flags):
            self.calls.append(flags)
            if flags:
                ctypes.set_errno(self.code)
                return -1
            _os.rename(_os.fsdecode(oldname), _os.fsdecode(newname))
            return 0

        _call.argtypes = None
        _call.restype = None
        return _call


@pytest.mark.parametrize("code", [_errno.EINVAL, _errno.ENOSYS, _errno.EOPNOTSUPP])
def test_a_unique_destination_still_moves_when_the_flag_is_unsupported(tmp_path, monkeypatch, code):
    libc = _FlagRefusingLibc(code)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: libc)
    src = tmp_path / "run"
    src.mkdir()
    (src / "events.jsonl").write_text("{}\n")
    dst = tmp_path / ".looplab-delete-quarantine-abc.7f0d1c8e-uuid"

    _atomicio.durable_no_replace_rename(src, dst, label="deletion quarantine",
                                        unique_destination=True)

    assert dst.is_dir() and (dst / "events.jsonl").exists() and not src.exists()
    assert libc.calls[0] != 0, "the flagged call must be tried FIRST, never skipped"


@pytest.mark.parametrize("code", [_errno.EINVAL, _errno.ENOSYS, _errno.EOPNOTSUPP])
def test_a_PREDICTABLE_destination_still_fails_closed(tmp_path, monkeypatch, code):
    """A caller that cannot promise its destination is unforgeable does not get the fallback.

    The replay archive USED to be the live example: it named itself `<name>.reset-<millisecond
    stamp>` from a probe-then-use loop over `lexists` — exactly the TOCTOU the kernel flag closes —
    and it therefore failed closed here. On the geesefs mounts this deployment runs on, where the
    flag itself is refused, that meant replay archiving could not succeed at all, and the run was
    left un-resumable, un-replayable and un-deletable behind its own marker.

    It names archives after its `operation_id` now (`reset_route::_archive_is_operation_unique`), so
    it passes `unique_destination=True` and is covered by the test above. The RULE this pins is
    unchanged and is the reason that change had to move the NAME rather than pass the flag: a
    destination anyone else could construct still fails closed, and no caller gets the fallback by
    asserting it without earning it.
    """
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _FlagRefusingLibc(code))
    src = tmp_path / "events.jsonl"
    src.write_text("{}\n")
    with pytest.raises(OSError) as info:
        _atomicio.durable_no_replace_rename(src, tmp_path / "events.jsonl.reset-1", 
                                            label="replay archive")
    assert info.value.errno == code
    assert src.exists(), "the source must be untouched when the guarantee cannot be given"


def test_a_REAL_rename_failure_is_never_treated_as_an_unsupported_flag(tmp_path, monkeypatch):
    """EXDEV, ENOTEMPTY, EACCES, EBUSY are answers about THIS rename, not about the flag. Treating
    one as "unsupported" would perform the very replace the no-replace contract forbids."""
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _FlagRefusingLibc(_errno.EXDEV))
    src = tmp_path / "run"
    src.mkdir()
    with pytest.raises(OSError) as info:
        _atomicio.durable_no_replace_rename(src, tmp_path / "quarantine.uuid",
                                            label="deletion quarantine",
                                            unique_destination=True)
    assert info.value.errno == _errno.EXDEV
    assert src.is_dir()


def test_a_destination_that_appears_DURING_the_flag_probe_is_refused_not_replaced(
        tmp_path, monkeypatch):
    """The residual race the fallback opens, closed: the kernel is asked about flags first, and a
    destination created in that window must still be refused. Replacing it is the one outcome the
    caller cannot recover from."""
    dst = tmp_path / "quarantine.uuid"

    class _Racer(_FlagRefusingLibc):
        def __getattr__(self, name):
            call = super().__getattr__(name)

            def _raced(*args):
                result = call(*args)
                dst.mkdir()                      # a concurrent writer wins the name
                return result
            return _raced

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _Racer(_errno.EINVAL))
    src = tmp_path / "run"
    src.mkdir()
    with pytest.raises(FileExistsError):
        _atomicio.durable_no_replace_rename(src, dst, label="deletion quarantine",
                                            unique_destination=True)
    assert src.is_dir(), "the source must survive a refused move"


# ------------------------------------------------------------------------------------------------
# The THIRD caller: finishing an interrupted directory move.
#
# `deletion_service._absorb_quarantine_residue` carries a file the geesefs per-object rename left
# behind in the fenced run into the SAME operation's quarantine. Source parent and destination parent
# are two different directories by construction, so it needs the sibling rule lifted — and it needs
# the durability claim to still hold, which across two directories is two fsyncs and not one.
# ------------------------------------------------------------------------------------------------


def test_a_cross_directory_move_carries_the_bytes_when_it_is_asked_for(tmp_path):
    source = tmp_path / "run" / "node_0" / "x.pyc.140558024057008"
    source.parent.mkdir(parents=True)
    source.write_text("residue", encoding="utf-8")
    destination = tmp_path / "quarantine.uuid" / "node_0" / "x.pyc.140558024057008"
    destination.parent.mkdir(parents=True)

    durable_no_replace_rename(source, destination, label="deletion quarantine residue",
                              unique_destination=True, cross_directory=True)

    assert destination.read_text(encoding="utf-8") == "residue" and not source.exists()


def test_a_cross_directory_move_still_refuses_an_occupied_destination(tmp_path):
    """The no-replace contract does not soften with the sibling rule. For the residue absorber this
    is not a detail: a destination that already exists is its proof that the entry is NOT residue of
    its own move, and replacing it would destroy a file the quarantine already carried."""
    source = tmp_path / "run" / "x"
    source.parent.mkdir(parents=True)
    source.write_text("mine", encoding="utf-8")
    destination = tmp_path / "quarantine.uuid" / "x"
    destination.parent.mkdir(parents=True)
    destination.write_text("already carried", encoding="utf-8")

    with pytest.raises(FileExistsError):
        durable_no_replace_rename(source, destination, label="deletion quarantine residue",
                                  unique_destination=True, cross_directory=True)
    assert source.read_text(encoding="utf-8") == "mine"
    assert destination.read_text(encoding="utf-8") == "already carried"


def test_BOTH_parents_are_fsynced_when_the_directories_differ(tmp_path, monkeypatch):
    """The sibling contract's single fsync publishes the new name AND the removal of the old one,
    because they are entries in one directory. Across two, one fsync is half the guarantee: a crash
    can leave the entry visible under both names, which for the residue absorber means a run
    directory that is still non-empty after the quarantine already holds the file."""
    import looplab.core.atomicio as atomicio

    synced = []
    monkeypatch.setattr(atomicio, "strict_fsync_parent", lambda path: synced.append(str(path)))
    source = tmp_path / "run" / "x"
    source.parent.mkdir(parents=True)
    source.write_text("residue", encoding="utf-8")
    destination = tmp_path / "quarantine.uuid" / "x"
    destination.parent.mkdir(parents=True)

    durable_no_replace_rename(source, destination, label="deletion quarantine residue",
                              unique_destination=True, cross_directory=True)

    assert synced == [str(destination), str(source)]


def test_only_the_residue_absorber_may_cross_a_directory():
    """The two original callers' invariants ARE the sibling rule, and the fsync asymmetry above is
    what a forgotten `cross_directory=True` would silently cost. So the opt-in gets the same
    treatment as the fallback: exactly one call site, named, or this goes red."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "looplab"
    callers = sorted({str(path.relative_to(root))
                      for path, source in iter_sources(root)
                      if "cross_directory=True" in source})
    assert callers == ["serve/deletion_service.py"], callers
