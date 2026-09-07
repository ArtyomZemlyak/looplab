"""The approved write may not be redirected by an ancestor swapped after the proof (doc 52 row 27).

`WriteTools._check` proves containment on a RESOLVED pathname before the approver is asked; the
publish must then never resolve that pathname again. These drive the attack the marker described —
an approver that, between the proof and the publish, renames the target's directory away and puts a
symlink to a directory OUTSIDE the root in its place — against both publish paths (descriptor-relative
where `dir_fd` exists; by name with the ancestor re-check where it does not), and reproduce the escape
with both guards off, so the attack is known to be effective before the guards are credited.
"""
from __future__ import annotations

import os
import stat

import pytest

from looplab.tools import write_tools
from looplab.tools.write_tools import WriteTools

ORIGINAL = "a = 1\n"


def _workspace(tmp_path):
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(ORIGINAL, encoding="utf-8")
    # The victim has the SAME bytes and mode, so the by-name pre-image CAS — which follows the
    # swapped link — passes, exactly as it did for the attack the marker described.
    outside = tmp_path / "outside" / "pkg"
    outside.mkdir(parents=True)
    (outside / "mod.py").write_text(ORIGINAL, encoding="utf-8")
    return root, outside


def _swap_ancestor(root, outside):
    (root / "pkg").rename(root / "pkg.moved")
    os.symlink(outside, root / "pkg", target_is_directory=True)


def _attack_approver(root, outside):
    calls = []

    def approver(action):
        calls.append(action["tool"])
        _swap_ancestor(root, outside)
        return "allow_once"
    approver.calls = calls
    return approver


def _no_litter(directory):
    return not any(p.name.startswith(".mod.py.assistant-") for p in directory.iterdir())


@pytest.mark.parametrize("descriptor_relative", [True, False],
                         ids=["descriptor-relative", "by-name-with-ancestor-recheck"])
def test_an_ancestor_swapped_for_a_symlink_after_approval_cannot_redirect_the_write(
        tmp_path, monkeypatch, descriptor_relative):
    if descriptor_relative and not write_tools._DESCRIPTOR_RELATIVE:
        pytest.skip("this platform has no dir_fd; the by-name path is the one it gets")
    monkeypatch.setattr(write_tools, "_DESCRIPTOR_RELATIVE", descriptor_relative)
    root, outside = _workspace(tmp_path)
    approver = _attack_approver(root, outside)
    tools = WriteTools([root], mode="default", approver=approver, backup_dir=tmp_path / "bak")
    result = tools.execute("write_file", {"path": str(root / "pkg" / "mod.py"), "content": "a = 2\n"})
    assert approver.calls == ["write_file"], "precondition: the approver ran, and swapped the ancestor"
    assert (outside / "mod.py").read_text(encoding="utf-8") == ORIGINAL, result
    assert (root / "pkg.moved" / "mod.py").read_text(encoding="utf-8") == ORIGINAL
    assert result.startswith("(error:") and "no longer a plain directory" in result, result
    assert _no_litter(outside) and _no_litter(root / "pkg.moved")
    assert tools.applied == [], "nothing was applied"


def test_the_defect_is_real_with_both_guards_off_the_write_lands_outside_the_root(tmp_path, monkeypatch):
    # The reproduction the fix is measured against: a by-name publish and no ancestor re-check is
    # the shape the marker described, and the attack above really does redirect it.
    monkeypatch.setattr(write_tools, "_DESCRIPTOR_RELATIVE", False)
    monkeypatch.setattr(write_tools, "_ancestors_are_plain", lambda root, directory: True)
    root, outside = _workspace(tmp_path)
    tools = WriteTools([root], mode="default", approver=_attack_approver(root, outside),
                       backup_dir=tmp_path / "bak")
    result = tools.execute("write_file", {"path": str(root / "pkg" / "mod.py"), "content": "a = 2\n"})
    assert result.startswith("(wrote"), result
    assert (outside / "mod.py").read_text(encoding="utf-8") == "a = 2\n", "the approved write escaped"


@pytest.mark.parametrize("descriptor_relative", [True, False],
                         ids=["descriptor-relative", "by-name-with-ancestor-recheck"])
def test_edit_delete_and_the_undo_restores_take_the_same_walk(tmp_path, monkeypatch, descriptor_relative):
    if descriptor_relative and not write_tools._DESCRIPTOR_RELATIVE:
        pytest.skip("this platform has no dir_fd")
    monkeypatch.setattr(write_tools, "_DESCRIPTOR_RELATIVE", descriptor_relative)
    root, outside = _workspace(tmp_path)
    approver = _attack_approver(root, outside)
    tools = WriteTools([root], mode="default", approver=approver, backup_dir=tmp_path / "bak")
    edit = tools.execute("edit_file", {"path": str(root / "pkg" / "mod.py"), "old_str": "a = 1", "new_str": "a = 3"})
    assert edit.startswith("(error:") and (outside / "mod.py").read_text(encoding="utf-8") == ORIGINAL
    # Put the workspace back and attack the delete.
    (root / "pkg").unlink()
    (root / "pkg.moved").rename(root / "pkg")
    delete = tools.execute("delete_file", {"path": str(root / "pkg" / "mod.py")})
    assert delete.startswith("(error:") and (outside / "mod.py").exists(), delete
    assert (root / "pkg.moved" / "mod.py").exists()
    # And the model-invocable Undo: a real write first (no attack), then the swap, then the revert.
    (root / "pkg").unlink()
    (root / "pkg.moved").rename(root / "pkg")
    calm = WriteTools([root], mode="auto", backup_dir=tmp_path / "bak")
    assert calm.execute("write_file", {"path": str(root / "pkg" / "mod.py"), "content": "a = 4\n"}).startswith("(wrote")
    (outside / "mod.py").write_text("a = 4\n", encoding="utf-8")
    _swap_ancestor(root, outside)
    undo = calm.execute("revert_file", {"path": str(root / "pkg" / "mod.py")})
    assert not undo.startswith("(reverted"), undo
    assert (outside / "mod.py").read_text(encoding="utf-8") == "a = 4\n", "the Undo did not restore outside the root"
    assert (root / "pkg.moved" / "mod.py").read_text(encoding="utf-8") == "a = 4\n"


def test_a_write_through_the_walk_creates_missing_directories_and_keeps_the_mode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tools = WriteTools([root], mode="auto", backup_dir=tmp_path / "bak")
    assert tools.execute("write_file", {"path": str(root / "new" / "deep" / "file.txt"), "content": "hi\n"}).startswith("(wrote")
    target = root / "new" / "deep" / "file.txt"
    assert target.read_text(encoding="utf-8") == "hi\n"
    assert (root / "new").is_dir() and not (root / "new").is_symlink()
    os.chmod(target, 0o640)
    assert tools.execute("write_file", {"path": str(root / "new" / "deep" / "file.txt"), "content": "again\n"}).startswith("(wrote")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640, "an existing file keeps its exact mode"
    assert target.read_text(encoding="utf-8") == "again\n"
    assert not any(p.name.startswith(".file.txt.assistant-") for p in target.parent.iterdir())


def test_a_root_replaced_wholesale_is_refused_by_its_identity(tmp_path):
    if not write_tools._DESCRIPTOR_RELATIVE:
        pytest.skip("identity re-check rides on the descriptor path")
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    tools = WriteTools([root], mode="auto", backup_dir=tmp_path / "bak")
    # The whole root directory is swapped for another directory of the same name.
    root.rename(tmp_path / "root.old")
    root.mkdir()
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    result = tools.execute("write_file", {"path": str(root / "f.txt"), "content": "y\n"})
    assert result.startswith("(error:") and "not the directory it was" in result, result
    assert (root / "f.txt").read_text(encoding="utf-8") == "x\n"
