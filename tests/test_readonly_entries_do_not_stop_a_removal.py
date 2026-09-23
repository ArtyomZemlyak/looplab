"""A read-only entry must not stop the service removing a tree it owns — on Windows too.

MEASURED on the Windows CI leg (GitHub Actions run 35785582444, review 2026-09-22, WIN-RMTREE):
every run deletion answered 202 and stayed at `purging` — 14 errors in test_durable_op_kit, one in
test_server — because `deletion_service._purge_quarantine` ran a plain `shutil.rmtree` over a
quarantine holding the read fence's own `sitecustomize.py`, which the engine hardens to 0444
(`runtime/read_fence.py::_harden`). On Windows that mode IS the read-only attribute, and
`DeleteFileW` refuses such a file; POSIX consults only the directory, so no Linux run could see it.
A git-seeded node workdir has the same shape (read-only pack files) at `WorkspaceMixin.materialize`.

Driven with Windows' rule reproduced (tests/_windows_emulation.py): the shared helper removes the
tree, the plain call it replaced does not, POSIX behaviour is unchanged, and a refusal the
attribute did not cause is still a refusal.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

from _windows_emulation import FakeMsvcrt, refuse_readonly_unlink
from looplab.core.atomicio import rmtree_readonly_aware
from _posix_gates import DIRECTORY_OPS_IGNORE_READONLY


def _tree(tmp_path):
    root = tmp_path / "quarantine"
    fence = root / ".looplab-fence"
    fence.mkdir(parents=True)
    hardened = fence / "sitecustomize.py"
    hardened.write_text("# the generated fence\n", encoding="utf-8")
    os.chmod(hardened, 0o444)                       # what `read_fence._harden` leaves behind
    (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
    return root


def test_the_emulated_rule_really_refuses_a_plain_rmtree(tmp_path, monkeypatch):
    """The falsifier: without it, a green helper could mean a toothless double."""
    root = _tree(tmp_path)
    refused = refuse_readonly_unlink(monkeypatch)
    with pytest.raises(PermissionError):
        shutil.rmtree(root)
    assert refused and root.exists()


def test_a_readonly_file_is_removed_on_windows(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    refused = refuse_readonly_unlink(monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        rmtree_readonly_aware(root)
    assert refused, "the read-only rule never fired, so nothing was proved"
    assert not root.exists()


@DIRECTORY_OPS_IGNORE_READONLY
def test_posix_behaviour_is_shutil_rmtree_unchanged(tmp_path, monkeypatch):
    """No attribute juggling off Windows: the same refusal propagates as `shutil.rmtree` raised it,
    and the refused file keeps its mode."""
    root = _tree(tmp_path)
    refuse_readonly_unlink(monkeypatch)
    with pytest.raises(PermissionError):
        rmtree_readonly_aware(root)
    assert oct(os.stat(root / ".looplab-fence" / "sitecustomize.py").st_mode & 0o777) == "0o444"


def test_a_refusal_the_attribute_did_not_cause_is_still_raised(tmp_path, monkeypatch):
    """A WRITABLE entry that Windows still refuses (a file another process holds open) must stay a
    failure — clearing an attribute it does not carry cannot help, and a silent pass would report a
    removed tree that is still there."""
    root = tmp_path / "held"
    root.mkdir()
    held = root / "open-elsewhere.log"
    held.write_text("x\n", encoding="utf-8")
    real_unlink = os.unlink

    def _sharing_violation(path, *, dir_fd=None):
        if os.fspath(path).endswith("open-elsewhere.log"):
            raise PermissionError(13, "The process cannot access the file (emulated)", path)
        return real_unlink(path, dir_fd=dir_fd) if dir_fd is not None else real_unlink(path)

    monkeypatch.setattr(os, "unlink", _sharing_violation)
    with monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        with pytest.raises(PermissionError):
            rmtree_readonly_aware(root)
    assert held.exists()


def test_the_deletion_purge_removes_a_quarantine_holding_the_hardened_fence(tmp_path, monkeypatch):
    """The route's own step, under Windows' rules: `_purge_quarantine` takes the quarantine's
    engine.lock through msvcrt, then must remove a tree that holds the 0444 fence file."""
    pytest.importorskip("fastapi")
    from looplab.serve import deletion_service

    root = _tree(tmp_path)
    (root / "engine.lock").write_text("", encoding="utf-8")
    refused = refuse_readonly_unlink(monkeypatch)
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "msvcrt", FakeMsvcrt())
        m.setattr(os, "name", "nt")
        purged = deletion_service._purge_quarantine(root)
    assert refused, "the read-only rule never fired, so nothing was proved"
    assert purged is True and not root.exists()
