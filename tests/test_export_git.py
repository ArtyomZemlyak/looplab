"""`looplab export-git`: the run's node DAG as git history (doc 67 67.15).

Every test writes a real event log, folds it with the real fold and drives the real `git` binary —
the export is only worth anything if `git log --graph`, `git diff` and `git bisect` read it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore
from looplab.events.git_export import _order, safe_tree_path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _node(store, node_id, parents, *, code="", files=None, deleted=None, rationale="r"):
    store.append("node_created", {
        "node_id": node_id, "parent_ids": parents, "operator": "merge" if len(parents) > 1
        else ("improve" if parents else "draft"),
        "idea": {"operator": "draft", "params": {"lr": 0.1 * (node_id + 1)}, "rationale": rationale,
                 "hypothesis": f"h{node_id}"},
        "code": code, "files": files or {}, "deleted": deleted or []})


def _run(tmp_path) -> Path:
    """0 and 1 are roots; 2 improves 0; 3 merges 1 and 2 and wins; 4 improves 2 and fails."""
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    _node(store, 0, [], code="print(0)\n")
    _node(store, 1, [], files={"train.py": "EPOCHS = 1\n"})
    _node(store, 2, [0], code="print(2)\n", files={"lib/model.py": "W = 2\n"},
          deleted=["old.py"])
    _node(store, 3, [1, 2], files={"train.py": "EPOCHS = 3\n", "lib/model.py": "W = 3\n"},
          rationale="merge the two lines of work")
    _node(store, 4, [2], code="print(4)\n")
    for node_id, metric in ((0, 0.5), (1, 0.6), (2, 0.7), (3, 0.9)):
        store.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": metric,
                                        "violations": []})
    store.append("node_failed", {"node_id": 4, "generation": 0, "error": "boom",
                                 "reason": "crash"})
    return rd


def _git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _export(rd: Path, out: Path):
    return CliRunner().invoke(app, ["export-git", str(rd), str(out)])


def test_each_node_is_a_commit_its_parents_are_the_dag_and_the_champion_is_checked_out(tmp_path):
    rd = _run(tmp_path)
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "branch champion = node 3, checked out" in result.output
    commit = {nid: _git(repo, "rev-parse", f"node-{nid}^{{commit}}") for nid in range(5)}
    parents = {nid: _git(repo, "log", "-1", "--format=%P", commit[nid]).split() for nid in range(5)}
    assert parents[0] == [] and parents[1] == []
    assert parents[2] == [commit[0]]
    assert parents[3] == [commit[1], commit[2]], "a merge keeps both parents, in the log's order"
    assert parents[4] == [commit[2]]
    # The tree is the node's OWN files — a whole edit set, not a delta over its parent.
    assert _git(repo, "ls-tree", "-r", "--name-only", "node-3").split() == ["lib/model.py",
                                                                            "train.py"]
    assert _git(repo, "show", "node-2:solution.py") == "print(2)"
    assert _git(repo, "show", "node-3:train.py") == "EPOCHS = 3"
    assert "node-1" not in _git(repo, "ls-tree", "-r", "--name-only", "node-1")
    # The metric and the receipts ride as trailers a script can read.
    trailers = _git(repo, "log", "-1", "--format=%(trailers:key=Looplab-Metric,valueonly)",
                    "node-3")
    assert trailers == "0.9"
    failed = _git(repo, "log", "-1", "--format=%B", "node-4")
    assert "Looplab-Status: failed" in failed and "Looplab-Error-Reason: crash" in failed
    assert "Looplab-Deleted-From-Base: old.py" in _git(repo, "log", "-1", "--format=%B", "node-2")
    assert "Looplab-Champion: yes" in _git(repo, "log", "-1", "--format=%B", "node-3")
    # …and the champion is what a checkout holds, with nothing left dirty.
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/champion"
    assert (repo / "train.py").read_text() == "EPOCHS = 3\n"
    assert _git(repo, "status", "--porcelain") == ""
    subprocess.run(["git", "-C", str(repo), "fsck", "--strict"], check=True, capture_output=True)


def test_one_log_exports_to_the_same_commit_ids(tmp_path):
    rd = _run(tmp_path)
    assert _export(rd, tmp_path / "a").exit_code == 0
    assert _export(rd, tmp_path / "b").exit_code == 0
    for nid in range(5):
        assert (_git(tmp_path / "a", "rev-parse", f"node-{nid}")
                == _git(tmp_path / "b", "rev-parse", f"node-{nid}"))


def test_a_path_a_checkout_could_turn_against_its_reader_is_counted_never_written(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    odd = 'dir/a b"c\n.py'
    _node(store, 0, [], files={"../evil.py": "x", ".git/config": "y", "C:evil.py": "z",
                              "/abs.py": "w", odd: "fine\n"})
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "nothing is checked out" in result.output          # no metric, no champion
    names = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", "node-0"],
                           check=True, capture_output=True).stdout.split(b"\0")
    assert [n for n in names if n] == [odd.encode("utf-8")], names
    assert "Looplab-Skipped-Paths: 4" in _git(repo, "log", "-1", "--format=%B", "node-0")
    assert not (tmp_path / "evil.py").exists()


def test_it_refuses_a_non_empty_target_and_a_missing_git(tmp_path, monkeypatch):
    rd = _run(tmp_path)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine", encoding="utf-8")
    result = _export(rd, busy)
    assert result.exit_code == 2 and "not an empty directory" in result.output
    assert (busy / "keep.txt").read_text() == "mine"
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = _export(rd, tmp_path / "fresh")
    assert result.exit_code == 2 and "git is not installed" in result.output
    assert not (tmp_path / "fresh").exists()


@pytest.mark.parametrize("path,expected", [
    ("a.py", "a.py"), ("dir/sub/a.py", "dir/sub/a.py"), ("dir\\win.py", "dir/win.py"),
    ("../up.py", None), ("dir/../x", None), ("./x", None), ("/abs", None), ("", None),
    (".git/hooks/pre-commit", None), ("sub/.GIT/config", None), ("C:x.py", None),
    ("a\x00b", None), (7, None), ("x" * 5000, None),
])
def test_the_tree_path_rule(path, expected):
    assert safe_tree_path(path) == expected


def test_parents_come_first_whatever_their_ids_and_a_cycle_cannot_hang_it():
    class _N:
        def __init__(self, parents):
            self.parent_ids = parents

    assert _order({0: _N([5]), 5: _N([]), 3: _N([0, 5])}) == [5, 0, 3]
    assert sorted(_order({1: _N([2]), 2: _N([1]), 3: _N([])})) == [1, 2, 3]
    assert json.dumps(_order({})) == "[]"
