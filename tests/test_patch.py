"""I4: unified-diff path extraction, the out-of-surface gate (reject not strip), and
git apply."""
from __future__ import annotations

import subprocess

import pytest

from looplab.tools.patch import apply_patch, changed_paths, gate

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


# --- patch gate: protected-file rejection + per-repo surface prefixes (deep audit) ----------------

# A3 — the patch gate rejects a protected file even when it matches the surface
def test_patch_gate_rejects_protected():
    diff = ("diff --git a/ttrain.py b/ttrain.py\n--- a/ttrain.py\n+++ b/ttrain.py\n"
            "@@ -1 +1 @@\n-x\n+y\n")
    assert gate(diff, ["*.py"])["ok"] is True                      # in surface
    g = gate(diff, ["*.py"], ["ttrain.py"])                        # but protected
    assert g["ok"] is False and "ttrain.py" in g["rejected"]
    # case-variant is also rejected
    diff2 = diff.replace("ttrain.py", "Ttrain.PY")
    assert gate(diff2, ["*.py"], ["ttrain.py"])["ok"] is False


# #32 — the patch gate scopes each named repo's surface to its own subdir
def test_gate_prefix_scopes_named_repo_surface():
    diff = ("diff --git a/model/evil.py b/model/evil.py\n--- a/model/evil.py\n"
            "+++ b/model/evil.py\n@@ -1 +1 @@\n-x\n+y\n")
    allow = ["**/*.py", "model/keep/*.py"]                          # model repo's narrow surface
    assert gate(diff, allow)["ok"] is True                          # without prefixes: root glob leaks
    assert gate(diff, allow, prefixes=["model"])["ok"] is False     # scoped: not in model/keep/*
    assert gate(diff.replace("model/evil.py", "model/keep/m.py"), allow, prefixes=["model"])["ok"]
    assert gate(diff.replace("model/evil.py", "top.py"), allow, prefixes=["model"])["ok"]
