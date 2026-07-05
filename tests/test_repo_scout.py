"""Read-only repo-scout tools (read_repo_file / grep_repo / list_repo) + env_inspect fuzzy suggestion.
These close the gap that made the developer GUESS a repo file's CLI flags (the embedded source is
truncated and there was no tool to read the repo's OWN files) — the direct cause of the --grad_clip
crash on node 35."""
from __future__ import annotations

from looplab.adapters.repo_developer import RepoWriteTools
from looplab.tools.env_inspect import EnvInspectTools, _suggest


def _repo(tmp_path):
    (tmp_path / "train.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--gradient_clip_val', type=float)\n"
        "p.add_argument('--cycle', action='store_true')\n", encoding="utf-8")
    (tmp_path / "model.py").write_text("class Net:\n    pass\n", encoding="utf-8")
    (tmp_path / "ckpt").mkdir()               # heavy dir must be pruned by the scout
    (tmp_path / "ckpt" / "big.py").write_text("SHOULD_NOT_BE_GREPPED = 1\n", encoding="utf-8")
    return RepoWriteTools(surface=["**/*.py"], protected=[],
                          editables=[{"name": ".", "path": str(tmp_path)}])


def test_read_repo_file_line_numbered(tmp_path):
    w = _repo(tmp_path)
    out = w.execute("read_repo_file", {"path": "train.py"})
    assert "--gradient_clip_val" in out and "--cycle" in out
    assert "3\t" in out and "train.py (lines 1.." in out           # line-numbered, header


def test_read_repo_file_range_and_missing(tmp_path):
    w = _repo(tmp_path)
    rng = w.execute("read_repo_file", {"path": "train.py", "start_line": 3, "max_lines": 1})
    assert "--gradient_clip_val" in rng and "--cycle" not in rng    # only line 3
    assert "not found" in w.execute("read_repo_file", {"path": "nope.py"})


def test_grep_repo_finds_the_flag(tmp_path):
    w = _repo(tmp_path)
    g = w.execute("grep_repo", {"query": "add_argument"})
    assert "train.py:3" in g and "gradient_clip_val" in g          # the info minimax lacked
    # restrict to a file
    only = w.execute("grep_repo", {"query": "add_argument", "path": "train.py"})
    assert "train.py" in only
    # a real miss
    assert "not found" in w.execute("grep_repo", {"query": "definitely_absent_zzz"})


def test_scout_prunes_heavy_dirs(tmp_path):
    w = _repo(tmp_path)
    # ckpt/ is a pruned dir -> its file is invisible to grep/list (grep returns a clean "not found",
    # not the ckpt/big.py:1 hit it would find if the dir weren't pruned)
    assert "not found" in w.execute("grep_repo", {"query": "SHOULD_NOT_BE_GREPPED"})
    assert "ckpt/big.py" not in w.execute("list_repo", {})


def test_list_repo_and_staged_overlay(tmp_path):
    w = _repo(tmp_path)
    lst = w.execute("list_repo", {})
    assert "train.py" in lst and "model.py" in lst
    # a file staged this turn shows up + read/grep see it (staged wins over disk)
    w.execute("write_file", {"path": "entry.py", "content": "print('hi')\nMARKER=1\n"})
    assert "entry.py" in w.execute("list_repo", {})
    assert "entry.py:2" in w.execute("grep_repo", {"query": "MARKER"})


def test_read_repo_not_surface_gated(tmp_path):
    # reading is allowed even for a PROTECTED file (you can't write it, but you must be able to read it)
    w = RepoWriteTools(surface=["*.md"], protected={"train.py"},   # train.py protected + off-surface
                       editables=[{"name": ".", "path": str(tmp_path)}])
    (tmp_path / "train.py").write_text("p.add_argument('--lr')\n", encoding="utf-8")
    assert "--lr" in w.execute("read_repo_file", {"path": "train.py"})


def test_env_suggest_points_at_the_real_name():
    # a near-miss of an installed package suggests it ('pytes' -> 'pytest', always installed in dev)
    hint = _suggest("pytes")
    assert "pytest" in hint
    out = EnvInspectTools().execute("pkg_info", {"name": "pytes"})
    assert "not installed" in out and "pytest" in out
