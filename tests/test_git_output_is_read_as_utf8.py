"""git hands a non-ASCII path over in its raw UTF-8 bytes, and a Windows runner decodes text with
cp1252 unless told otherwise (review 2026-09-22).

`git ls-files -z` turns git's own path quoting off, and `git apply` names a failing path unquoted,
so both reach Python as UTF-8. Read through the locale codec, a cp1252 box turned `données.csv`
into `donnÃ©es.csv` -- a path that does not exist, so the seeder silently left a TRACKED file out of
every candidate -- and a byte cp1252 leaves undefined (`с` is D1 81) raised: the seeder then copied
the whole untracked tree instead, and `apply_patch` threw out of its caller rather than returning
the refusal. Driven here under `_windows_emulation.windows_text_codec`, the runner's codec.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from _windows_emulation import windows_text_codec
from looplab.engine.workspace_seed import seed_repo_tree
from looplab.tools.patch import apply_patch

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

# One name decodes to mojibake under cp1252 (é = C3 A9), one does not decode at all (с = D1 81).
MOJIBAKE, UNDECODABLE = "données.csv", "тест.py"


def _repo(root, names=(MOJIBAKE, UNDECODABLE, "plain.py")):
    root.mkdir()
    for name in names:
        (root / name).write_text("x = 1\n", encoding="utf-8")
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "seed"], check=True)
    (root / "untracked_artifact.bin").write_bytes(b"\0" * 64)     # what `auto` exists to skip
    return root


def test_the_seeder_copies_every_tracked_file_by_its_real_name(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src")
    windows_text_codec(monkeypatch)
    copied = seed_repo_tree(src, tmp_path / "dst", None)
    assert copied == 3, (
        f"{copied}: -1 is the full-tree fallback a decode error falls into, fewer than 3 is a "
        "tracked file silently left out of the candidate")
    got = sorted(p.name for p in (tmp_path / "dst").iterdir())
    assert got == sorted([MOJIBAKE, UNDECODABLE, "plain.py"]), got


def test_a_name_the_codec_can_decode_is_not_silently_misspelled(tmp_path, monkeypatch):
    """No error to fall back on: `donnÃ©es.csv` simply does not exist, and was skipped."""
    src = _repo(tmp_path / "src", names=(MOJIBAKE, "plain.py"))
    windows_text_codec(monkeypatch)
    assert seed_repo_tree(src, tmp_path / "dst", None) == 2
    assert (tmp_path / "dst" / MOJIBAKE).is_file()


def test_a_refused_patch_on_a_non_ascii_path_is_returned_not_raised(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src")
    diff = (f"diff --git a/{UNDECODABLE} b/{UNDECODABLE}\n--- a/{UNDECODABLE}\n"
            f"+++ b/{UNDECODABLE}\n@@ -1 +1 @@\n-x = 2\n+x = 3\n")      # context that is not there
    windows_text_codec(monkeypatch)
    res = apply_patch(diff, str(src), allow=["*.py"])
    assert not res["applied"] and UNDECODABLE in res["error"], res
