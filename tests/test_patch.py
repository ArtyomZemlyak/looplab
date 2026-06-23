"""I4: unified-diff path extraction, the out-of-surface gate (reject not strip), and
git apply."""
from __future__ import annotations

import subprocess

import pytest

from autornd.patch import apply_patch, changed_paths, gate

_DIFF = """\
diff --git a/solution.py b/solution.py
--- a/solution.py
+++ b/solution.py
@@ -1 +1 @@
-hello
+goodbye
"""

_EVIL = """\
diff --git a/../../etc/passwd b/../../etc/passwd
--- a/../../etc/passwd
+++ b/../../etc/passwd
@@ -1 +1 @@
-x
+y
"""


def test_changed_paths():
    assert changed_paths(_DIFF) == ["solution.py"]


def test_gate_accepts_in_surface():
    g = gate(_DIFF, allow=["*.py"])
    assert g["ok"] and g["paths"] == ["solution.py"] and g["rejected"] == []


def test_gate_rejects_out_of_surface():
    g = gate(_DIFF, allow=["docs/*"])          # solution.py not allowed
    assert not g["ok"] and "solution.py" in g["rejected"]


def test_gate_rejects_path_traversal():
    g = gate(_EVIL, allow=["**"])              # even "allow everything" can't escape
    assert not g["ok"] and g["rejected"]


def test_gate_rejects_absolute_path():
    diff = "--- a/x\n+++ /tmp/abs.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert not gate(diff, allow=["**"])["ok"]


def test_gate_empty_patch():
    assert not gate("not a diff at all", allow=["**"])["ok"]


def _have_git():
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _have_git(), reason="git not available")
def test_apply_patch_modifies_in_surface_file(tmp_path):
    (tmp_path / "solution.py").write_text("hello\n", encoding="utf-8")
    res = apply_patch(_DIFF, str(tmp_path), allow=["*.py"])
    assert res["applied"], res["error"]
    assert (tmp_path / "solution.py").read_text(encoding="utf-8") == "goodbye\n"


@pytest.mark.skipif(not _have_git(), reason="git not available")
def test_apply_patch_refuses_out_of_surface(tmp_path):
    (tmp_path / "solution.py").write_text("hello\n", encoding="utf-8")
    res = apply_patch(_DIFF, str(tmp_path), allow=["docs/*"])
    assert not res["applied"] and "solution.py" in res["rejected"]
    assert (tmp_path / "solution.py").read_text(encoding="utf-8") == "hello\n"  # untouched


@pytest.mark.skipif(not _have_git(), reason="git not available")
def test_apply_patch_rejects_malformed(tmp_path):
    # in-surface path but the hunk won't apply (file content differs)
    (tmp_path / "solution.py").write_text("totally different\n", encoding="utf-8")
    res = apply_patch(_DIFF, str(tmp_path), allow=["*.py"])
    assert not res["applied"] and res["error"]
