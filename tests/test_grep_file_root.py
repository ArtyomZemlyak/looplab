"""WP-TOOLS T2 (2026-09-29): the scout's `grep` with `root` naming a FILE greps that file.

`root` had to be a directory: 219 calls on MiniOneRec inf13 were answered `(grep: X is not a
searchable directory)` — 123 in `plan_step`, 44 in triage — and the model retried on the parent.

The critic's BLOCKING change is the resolution ORDER. The Developer's scout overlays the code it is
editing, so `root` resolves exactly as `_read_file` resolves a path: staged key -> staged deletion ->
`_resolve` -> regular file -> secret -> readable type -> size. Disk-first would grep the untouched base
copy of `service/latency_engine.py` — 102 of the 175 Developer roots, a file every lineage node had
rewritten — and hand its stale line numbers to `edit_file`; the files that exist only staged would
still fail. Every root that cannot be searched says WHY, never "not a searchable directory".
"""
from __future__ import annotations

import os

import pytest

from tests._posix_gates import POSIX_ONLY_OS_CALLS
from looplab.tools.reposcout import RepoScoutTools

_BASE = "def infer(batch):\n    return batch  # base\n"
_STAGED = "import math\n\ndef infer(batch):\n    return ragged(batch)  # staged\n"


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "service").mkdir(parents=True)
    (root / "service" / "latency_engine.py").write_text(_BASE, encoding="utf-8")
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return root


def _scout(root, **kw):
    return RepoScoutTools(roots=[str(root)], default_root=str(root), **kw)


def test_a_file_root_greps_that_file_with_the_walks_labels_and_line_numbers(tmp_path):
    root = _repo(tmp_path)
    out = _scout(root).execute("grep", {"pattern": "def infer", "root": "service/latency_engine.py"})
    assert out == "service/latency_engine.py:1: def infer(batch):", out
    same = _scout(root).execute("grep", {"pattern": "def infer", "root": "service"})
    assert same == out, "a file root and a walk that reaches the same file must agree byte for byte"
    miss = _scout(root).execute("grep", {"pattern": "absent_zzz", "root": "main.py"})
    assert "not found" in miss and "main.py" in miss


def test_a_staged_file_root_greps_the_STAGED_content_not_the_base_file(tmp_path):
    """MUTATION: resolve `root` on disk first -> the base copy's `return batch  # base` at line 2, a
    line number `edit_file` would then act on in a file whose line 2 is blank."""
    root = _repo(tmp_path)
    staged = {"service/latency_engine.py": _STAGED}
    out = _scout(root, overlay=staged).execute(
        "grep", {"pattern": "return", "root": "service/latency_engine.py"})
    assert out == "service/latency_engine.py:4: return ragged(batch)  # staged", out
    assert "# base" not in out
    # …and an absolute node-workdir spelling reaches the same staged file (the `_overlay_get` rule).
    out = _scout(root, overlay=staged).execute(
        "grep", {"pattern": "return", "root": "/runs/r1/nodes/node_59/service/latency_engine.py"})
    assert "# staged" in out, out


def test_a_file_that_exists_only_staged_is_a_valid_root(tmp_path):
    root = _repo(tmp_path)
    staged = {"optimizations/exp34_mixed_length_batch.py": "def mixed_length_batch():\n    pass\n"}
    out = _scout(root, overlay=staged).execute(
        "grep", {"pattern": "def mixed", "root": "optimizations/exp34_mixed_length_batch.py"})
    assert out == "optimizations/exp34_mixed_length_batch.py:1: def mixed_length_batch():", out


def test_a_directory_root_scopes_the_staged_files_too(tmp_path):
    """The overlay loop ignored `root`, so a search under `service` reported staged files from
    anywhere in the tree."""
    root = _repo(tmp_path)
    staged = {"service/latency_engine.py": _STAGED, "other/far.py": "def infer_far():\n"}
    out = _scout(root, overlay=staged).execute("grep", {"pattern": "def infer", "root": "service"})
    assert "service/latency_engine.py:3:" in out and "other/far.py" not in out, out
    whole = _scout(root, overlay=staged).execute("grep", {"pattern": "def infer"})
    assert "other/far.py:1:" in whole, "no root: every staged file is searched, as before"


@pytest.mark.parametrize("make_root, expect", [
    (lambda root: ".env", "looks like a credential/secret file"),
    (lambda root: "weights.bin", "unsupported/binary file type"),
    (lambda root: "big.py", "over grep's 2000000b per-file limit"),
    (lambda root: ".git/config", "repository internals (.git)"),
    (lambda root: ".git", "repository internals (.git)"),
    (lambda root: "service/missing.py", "no such file or directory: service/missing.py"),
])
def test_every_root_that_cannot_be_searched_says_why(tmp_path, make_root, expect):
    root = _repo(tmp_path)
    (root / ".env").write_text("API_KEY=sk-live-123\n", encoding="utf-8")
    (root / "weights.bin").write_bytes(b"\x00\x01API_KEY")
    (root / "big.py").write_text("API_KEY = 1\n" + "x" * 2_000_100, encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("url = https://u:API_KEY@example.invalid\n",
                                          encoding="utf-8")
    out = _scout(root).execute("grep", {"pattern": "API_KEY", "root": make_root(root)})
    assert out.startswith("(grep: ") and expect in out, out
    assert "sk-live" not in out and "example.invalid" not in out
    assert "not a searchable directory" not in out


def test_a_root_deleted_this_session_says_so(tmp_path):
    root = _repo(tmp_path)
    out = _scout(root, deleted=["service/latency_engine.py"]).execute(
        "grep", {"pattern": "infer", "root": "service/latency_engine.py"})
    assert out == ("(grep: service/latency_engine.py was deleted this session — there is nothing "
                   "to search)"), out


def test_a_symlink_out_of_the_roots_and_foreign_directories_are_refused_honestly(tmp_path):
    """`./assets` symlinked out of the roots, `/opt`, a site-packages dir: each is OUTSIDE the
    searchable roots, and the receipt says that instead of "not a directory"."""
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("SECRET_TOKEN = 'abc123'\n", encoding="utf-8")
    try:
        os.symlink(outside, root / "assets")
        os.symlink(outside / "leak.py", root / "leak_link.py")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    scout = _scout(root)
    for named in ("./assets", "leak_link.py", str(outside), "/opt"):
        out = scout.execute("grep", {"pattern": "SECRET_TOKEN", "root": named})
        assert "abc123" not in out, named
        assert out == (f"(grep: {named} is outside the searchable roots — pass a directory or a "
                       "file inside the repo)"), out


@POSIX_ONLY_OS_CALLS
def test_a_fifo_root_is_refused_before_anything_opens_it(tmp_path):
    """A FIFO named like a source file would hang a blocking `open()` forever."""
    root = _repo(tmp_path)
    os.mkfifo(root / "pipe.py")
    out = _scout(root).execute("grep", {"pattern": "x", "root": "pipe.py"})
    assert out == "(grep: pipe.py is not a regular file or a directory — not searched)", out


def test_a_glob_that_excludes_the_named_file_gives_the_no_file_receipt(tmp_path):
    root = _repo(tmp_path)
    scout = _scout(root)
    out = scout.execute("grep", {"pattern": "infer", "root": "service/latency_engine.py",
                                 "glob": "*.md"})
    assert out.startswith("(grep: no searchable file under service/latency_engine.py matches glob "
                          "'*.md'"), out
    kept = scout.execute("grep", {"pattern": "def infer", "root": "service/latency_engine.py",
                                  "glob": "service/*.py"})
    assert kept == "service/latency_engine.py:1: def infer(batch):", "a glob that admits it is moot"


def test_a_directory_that_exists_only_staged_is_searched_not_refused(tmp_path):
    """D2: `optimizations/` holding only files this session wrote answered "(grep: no such file or
    directory: optimizations)" while `read_file` of a file under it returned content. MUTATION:
    drop the staged-directory clause in `_grep_target` -> that refusal again."""
    root = _repo(tmp_path)
    staged = {"optimizations/exp34_mixed_length_batch.py": "def mixed_length_batch():\n    pass\n",
              "optimizations/deeper/exp36.py": "def mixed_exp36():\n    pass\n"}
    scout = _scout(root, overlay=staged)
    assert "def mixed_length_batch" in scout.execute(
        "read_file", {"path": "optimizations/exp34_mixed_length_batch.py"})
    out = scout.execute("grep", {"pattern": "def mixed", "root": "optimizations"})
    assert out.splitlines() == [
        "optimizations/deeper/exp36.py:1: def mixed_exp36():",
        "optimizations/exp34_mixed_length_batch.py:1: def mixed_length_batch():"], out
    assert "not found" in scout.execute("grep", {"pattern": "absent_zzz", "root": "optimizations"})
    assert scout.execute("grep", {"pattern": "x", "root": "nowhere"}) == (
        "(grep: no such file or directory: nowhere)"), "a directory NOTHING has is still refused"
