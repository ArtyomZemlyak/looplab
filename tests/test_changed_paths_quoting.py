"""changed_paths must handle git C-quoted (non-ASCII) and space-containing paths so the surface
gate accounts for them correctly. Regression: a unicode-named in-surface edit gated as
out-of-surface (mangled path) -> the whole agent patch was reverted as a spurious no-op."""
from __future__ import annotations

from looplab.patch import changed_paths, gate


def _diff(*lines):
    return "\n".join(lines) + "\n"


def test_plain_ascii_diff():
    d = _diff("diff --git a/train.py b/train.py", "index e69..d95 100644",
              "--- a/train.py", "+++ b/train.py", "@@ -1 +1 @@", "-a", "+b")
    assert changed_paths(d) == ["train.py"]


def test_unicode_quoted_path_is_decoded():
    # git core.quotePath=true (the default) emits octal-escaped UTF-8 inside double quotes.
    d = _diff(r'diff --git "a/caf\303\251.py" "b/caf\303\251.py"', "index e69..d95 100644",
              r'--- "a/caf\303\251.py"', r'+++ "b/caf\303\251.py"', "@@ -0,0 +1 @@", "+x = 1")
    assert changed_paths(d) == ["café.py"]


def test_spaced_path_no_garbage_tokens():
    # git does NOT quote a plain space; the +++/--- headers carry the whole path, while the
    # `diff --git a/X b/X` line is ambiguous and must NOT contribute split garbage ('my', 'file.py').
    d = _diff("diff --git a/my file.py b/my file.py",
              "--- a/my file.py", "+++ b/my file.py", "@@ -1 +1 @@", "-a", "+b")
    assert changed_paths(d) == ["my file.py"]


def test_new_file_dev_null_ignored():
    d = _diff("diff --git a/new.py b/new.py", "new file mode 100644",
              "--- /dev/null", "+++ b/new.py", "@@ -0,0 +1 @@", "+x")
    assert changed_paths(d) == ["new.py"]


def test_pure_rename_uses_git_header_fallback():
    # No +++/--- lines -> the `diff --git` line is the only path source (old + new).
    d = _diff("diff --git a/old.py b/new.py", "similarity index 100%",
              "rename from old.py", "rename to new.py")
    assert changed_paths(d) == ["new.py", "old.py"]


def test_gate_accepts_in_surface_unicode_file():
    # The actual bug: a legitimate edit to a unicode-named in-surface file used to gate as
    # out-of-surface (mangled path), reverting the whole patch. It must now pass cleanly.
    d = _diff(r'diff --git "a/caf\303\251.py" "b/caf\303\251.py"',
              r'--- "a/caf\303\251.py"', r'+++ "b/caf\303\251.py"', "@@ -1 +1 @@", "-a", "+b")
    res = gate(d, allow=["**/*.py"])
    assert res["ok"] is True and res["rejected"] == [] and res["paths"] == ["café.py"]
