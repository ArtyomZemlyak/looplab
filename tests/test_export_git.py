"""`looplab export-git`: the run's node DAG as git history (doc 67 67.15).

Every test writes a real event log, folds it with the real fold and drives the real `git` binary —
the export is only worth anything if `git log --graph`, `git diff` and `git bisect` read it. The
test's OWN git reads are as hermetic as the export's (`_git_run`): one test below points `GIT_DIR`
at a victim repository, and a helper that inherited it would read — or write — the victim instead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events import git_export
from looplab.events.eventstore import EventStore
from looplab.events.git_export import FastImport, _order, _quoted, safe_tree_path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_GIT_TIMEOUT_S = 60


def _created(node_id, parents, *, code="", files=None, deleted=None, rationale="r", operator=None,
             hypothesis=None, params=None, generation=None, parent_generations=None) -> dict:
    data = {"node_id": node_id, "parent_ids": parents,
            "operator": operator or ("merge" if len(parents) > 1
                                     else ("improve" if parents else "draft")),
            "idea": {"operator": "draft",
                     "params": params if params is not None else {"lr": 0.1 * (int(node_id) + 1)},
                     "rationale": rationale,
                     "hypothesis": hypothesis if hypothesis is not None else f"h{node_id}"},
            "code": code, "files": files or {}, "deleted": deleted or []}
    if generation is not None:
        data["generation"] = generation
    if parent_generations is not None:
        data["parent_generations"] = parent_generations
    return data


def _node(store, node_id, parents, **kw):
    store.append("node_created", _created(node_id, parents, **kw))


def _evaluated(store, node_id, metric, *, generation=0, **extra):
    store.append("node_evaluated", {"node_id": node_id, "generation": generation, "metric": metric,
                                    "violations": [], **extra})


def _started(store, run_id="run"):
    store.append("run_started", {"run_id": run_id, "task_id": "t", "goal": "g", "direction": "max"})


def _store(tmp_path) -> tuple[Path, EventStore]:
    rd = tmp_path / "run"
    rd.mkdir(parents=True)
    return rd, EventStore(rd / "events.jsonl")


def _write_log(rd: Path, rows) -> None:
    """A log with FIXED timestamps (`EventStore.append` stamps the wall clock), in the store's own
    line shape, `seq` dense from 0 as the store's prefix fence requires."""
    rd.mkdir(exist_ok=True)
    (rd / "events.jsonl").write_text("".join(
        json.dumps({"v": 1, "seq": seq, "ts": ts, "type": etype, "data": data,
                    "trace_id": None, "span_id": None}) + "\n"
        for seq, (etype, ts, data) in enumerate(rows)), encoding="utf-8")


def _run(tmp_path) -> Path:
    """0 and 1 are roots; 2 improves 0; 3 merges 1 and 2 and wins; 4 improves 2 and fails."""
    rd, store = _store(tmp_path)
    _started(store)
    _node(store, 0, [], code="print(0)\n")
    _node(store, 1, [], files={"train.py": "EPOCHS = 1\n"})
    _node(store, 2, [0], code="print(2)\n", files={"lib/model.py": "W = 2\n"},
          deleted=["old.py"])
    _node(store, 3, [1, 2], files={"train.py": "EPOCHS = 3\n", "lib/model.py": "W = 3\n"},
          rationale="merge the two lines of work")
    _node(store, 4, [2], code="print(4)\n")
    for node_id, metric in ((0, 0.5), (1, 0.6), (2, 0.7), (3, 0.9)):
        _evaluated(store, node_id, metric)
    store.append("node_failed", {"node_id": 4, "generation": 0, "error": "boom",
                                 "reason": "crash"})
    return rd


def _git_run(repo: Path, *args, check: bool = True) -> subprocess.CompletedProcess:
    """git on `repo` and on nothing else: every `GIT_*` variable dropped (a test sets `GIT_DIR` to a
    victim repository, and `-C` would not override it), no user or system config, no hooks, and a
    timeout — the same discipline `export_cmds.py::_hermetic_git_env` holds the export to."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1", "HOME": str(repo), "XDG_CONFIG_HOME": str(repo),
                "LC_ALL": "C"})
    return subprocess.run(["git", "--git-dir", str(repo / ".git"), "--work-tree", str(repo),
                           "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
                           *args], capture_output=True, env=env, timeout=_GIT_TIMEOUT_S,
                          check=check)


def _git(repo: Path, *args) -> str:
    return _git_run(repo, *args).stdout.decode("utf-8", "replace").strip()


def _tree(repo: Path, ref: str) -> list:
    raw = _git_run(repo, "ls-tree", "-r", "-z", "--name-only", ref).stdout
    return sorted(name.decode("utf-8") for name in raw.split(b"\0") if name)


def _body(repo: Path, ref: str) -> str:
    return _git(repo, "log", "-1", "--format=%B", ref)


def _trailer(repo: Path, ref: str, key: str) -> str:
    return _git(repo, "log", "-1", f"--format=%(trailers:key={key},valueonly)", ref)


def _export(rd: Path, out: Path):
    return CliRunner().invoke(app, ["export-git", str(rd), str(out)])


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")


def test_each_node_is_a_commit_its_parents_are_the_dag_and_the_best_is_checked_out(tmp_path):
    rd = _run(tmp_path)
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "branch champion = node 3, checked out" in result.stdout
    commit = {nid: _git(repo, "rev-parse", f"node-{nid}^{{commit}}") for nid in range(5)}
    parents = {nid: _git(repo, "log", "-1", "--format=%P", commit[nid]).split() for nid in range(5)}
    assert parents[0] == [] and parents[1] == []
    assert parents[2] == [commit[0]]
    assert parents[3] == [commit[1], commit[2]], "a merge keeps both parents, in the log's order"
    assert parents[4] == [commit[2]]
    # TAGS, because a lifecycle never moves; the only branch is the best's.
    refs = _git(repo, "for-each-ref", "--format=%(refname)").split()
    assert {f"refs/tags/node-{nid}" for nid in range(5)} <= set(refs)
    assert [ref for ref in refs if ref.startswith("refs/heads/")] == ["refs/heads/champion"]
    # The tree is the node's OWN files — a whole edit set, not a delta over its parent: node 4
    # improves node 2 and wrote only `solution.py`, so node 2's `lib/model.py` is NOT in it.
    assert _tree(repo, "node-4") == ["solution.py"]
    assert _tree(repo, "node-2") == ["lib/model.py", "solution.py"]
    assert _tree(repo, "node-3") == ["lib/model.py", "train.py"]
    assert _git(repo, "show", "node-2:solution.py") == "print(2)"
    assert _git(repo, "show", "node-3:train.py") == "EPOCHS = 3"
    # The metric and the receipts ride as trailers a script can read.
    assert _trailer(repo, "node-3", "Looplab-Metric") == "0.9"
    assert _trailer(repo, "node-3", "Looplab-Generation") == "0"
    assert _trailer(repo, "node-3", "Looplab-Parents") == "node-1, node-2"
    failed = _body(repo, "node-4")
    assert "Looplab-Status: failed" in failed and "Looplab-Error-Reason: crash" in failed
    assert "Looplab-Feasible" not in failed, "a failed node has no metric to receipt"
    assert "Looplab-Deleted-From-Base: old.py" in _body(repo, "node-2")
    assert _trailer(repo, "node-3", "Looplab-Champion") == "yes"
    # …and the champion is what a checkout holds, with nothing left dirty.
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/champion"
    assert (repo / "train.py").read_text() == "EPOCHS = 3\n"
    assert _git(repo, "status", "--porcelain") == ""
    _git_run(repo, "fsck", "--strict")


def test_one_log_exports_to_the_same_commit_ids(tmp_path):
    rd = _run(tmp_path)
    assert _export(rd, tmp_path / "a").exit_code == 0
    assert _export(rd, tmp_path / "b").exit_code == 0
    for nid in range(5):
        assert (_git(tmp_path / "a", "rev-parse", f"node-{nid}")
                == _git(tmp_path / "b", "rev-parse", f"node-{nid}"))


_GOLDEN_LOG = [
    ("run_started", 1_790_000_000.0, {"run_id": "golden", "task_id": "t", "goal": "g",
                                      "direction": "max"}),
    ("node_created", 1_790_000_100.9, _created(0, [], code="print(0)\n",
                                               files={"lib/a.py": "A = 0\n"},
                                               rationale="a baseline")),
    ("node_evaluated", 1_790_000_200.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                         "violations": []}),
    ("node_created", 1_790_000_300.5, _created(1, [0], code="print(1)\n",
                                               files={"lib/a.py": "A = 1\n"}, deleted=["old.py"],
                                               rationale="a larger step",
                                               parent_generations={"0": 0})),
    ("node_evaluated", 1_790_000_400.0, {"node_id": 1, "generation": 0, "metric": 0.75,
                                         "violations": []}),
]
# Computed ONCE from `_GOLDEN_LOG` with the exporter of this change and hard-coded on purpose
# (review 2026-09-26): the export is documented DETERMINISTIC — a reviewer may pin a commit id — so
# anything a commit object carries (the identity, a date, the `+0000` zone, a trailer, a path's
# bytes, a parent's id) must move THIS string, and be seen moving it. Re-derive it only for a
# deliberate format change, and say so in that commit.
_GOLDEN_CHAMPION = "4fe54af1dff97f0cdecdbcccc42f17fe12141c99"


def test_a_fixed_log_exports_to_the_pinned_commit_id_whatever_the_callers_git_says(
        tmp_path, monkeypatch):
    rd = tmp_path / "run"
    _write_log(rd, _GOLDEN_LOG)
    # A caller whose own git would change every id (sha256), the identity, the line endings — and
    # whose hooks would run on the export's checkout. None of it may reach the export.
    home = tmp_path / "home"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    marker = tmp_path / "a-hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        "[init]\n\tdefaultObjectFormat = sha256\n"
        f"[core]\n\thooksPath = {hooks.as_posix()}\n\tautocrlf = true\n"
        "[user]\n\tname = Mallory\n\temail = mallory@example.invalid\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Mallory")
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert _git(repo, "rev-parse", "node-1") == _GOLDEN_CHAMPION
    # What the id pins, spelled out so a moved id says which part moved: one identity, each commit
    # dated by its OWN node_created row (whole seconds), in UTC.
    assert _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%at|%ct|%ai", "node-1") == (
        "LoopLab <looplab@invalid>|LoopLab <looplab@invalid>|1790000300|1790000300|"
        "2026-09-21 14:18:20 +0000")
    assert _git(repo, "log", "-1", "--format=%at", "node-0") == "1790000100"
    assert not marker.exists(), "the caller's hooks ran on the export"
    assert (repo / "lib" / "a.py").read_bytes() == b"A = 1\n", "the caller's autocrlf was applied"


def test_an_inherited_git_environment_cannot_turn_the_export_on_another_repository(
        tmp_path, monkeypatch):
    rd = _run(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    _git(victim, "init", "--quiet", "--template=", "--initial-branch=main")
    (victim / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(victim, "add", "tracked.txt")
    _git(victim, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "base")
    (victim / "tracked.txt").write_text("an uncommitted edit\n", encoding="utf-8")
    (victim / "staged.txt").write_text("staged, never committed\n", encoding="utf-8")
    _git(victim, "add", "staged.txt")

    def victim_state():
        return (_git(victim, "rev-parse", "HEAD"), _git(victim, "symbolic-ref", "HEAD"),
                _git(victim, "for-each-ref"), (victim / ".git" / "index").read_bytes(),
                sorted(p.name for p in victim.iterdir()),
                (victim / "tracked.txt").read_text(encoding="utf-8"),
                (victim / "staged.txt").read_text(encoding="utf-8"))

    before = victim_state()
    # What every git hook runs with — an absolute GIT_DIR, and for a pre-commit hook GIT_INDEX_FILE
    # too — plus a default hash that would move every commit id.
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(victim))
    monkeypatch.setenv("GIT_INDEX_FILE", str(victim / ".git" / "index"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert victim_state() == before, "the export wrote into the repository GIT_DIR named"
    # …and it landed where it was asked to, as sha1, with the best checked out there.
    assert len(_git(repo, "rev-parse", "node-3")) == 40
    assert (repo / "train.py").read_text(encoding="utf-8") == "EPOCHS = 3\n"
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/champion"


def test_a_child_is_wired_to_the_parent_lifecycle_it_was_built_from(tmp_path):
    """Node 0 is built (code A), repaired in place (A2), scored, bred from, then RESET and rebuilt
    (B). Node 1 was built from generation 0, so its parent is that lifecycle — with the code it held
    when the reset ended it, the repair included — and never node 0's current code."""
    rd = tmp_path / "run"
    _write_log(rd, [
        ("run_started", 1_790_000_000.0, {"run_id": "run", "task_id": "t", "goal": "g",
                                          "direction": "max"}),
        ("node_created", 1_790_000_100.0, _created(0, [], code="print('A')\n",
                                                   files={"a_only.py": "A\n"})),
        ("node_repaired", 1_790_000_110.0, {"node_id": 0, "generation": 0, "attempt": 1,
                                            "code": "print('A2')\n"}),
        ("node_evaluated", 1_790_000_120.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                             "violations": []}),
        ("node_created", 1_790_000_200.0, _created(1, [0], code="print('child')\n",
                                                   parent_generations={"0": 0})),
        ("node_evaluated", 1_790_000_210.0, {"node_id": 1, "generation": 0, "metric": 0.6,
                                             "violations": []}),
        ("node_reset", 1_790_000_300.0, {"node_id": 0, "generation": 0, "from_stage": "implement"}),
        ("node_created", 1_790_000_400.0, _created(0, [], code="print('B')\n", generation=1)),
        ("node_evaluated", 1_790_000_410.0, {"node_id": 0, "generation": 1, "metric": 0.9,
                                             "violations": []}),
    ])
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "1 superseded lifecycle(s) as node-<id>.g<generation>" in result.stdout
    assert _git(repo, "rev-parse", "node-1^") == _git(repo, "rev-parse", "node-0.g0^{commit}")
    assert _git(repo, "show", "node-1^:solution.py") == "print('A2')"
    assert _git(repo, "show", "node-0:solution.py") == "print('B')"
    assert _tree(repo, "node-0.g0") == ["a_only.py", "solution.py"]
    assert _tree(repo, "node-0") == ["solution.py"]
    assert _trailer(repo, "node-1", "Looplab-Parents") == "node-0.g0"
    superseded = _body(repo, "node-0.g0")
    assert superseded.startswith("node 0, generation 0 (superseded): draft — r\n")
    assert ("Looplab-Status: superseded — a node_reset from 'implement' opened node-0; this "
            "lifecycle had reached 'evaluated' and is no longer a candidate") in superseded
    assert _trailer(repo, "node-0.g0", "Looplab-Generation") == "0"
    assert _trailer(repo, "node-0.g0", "Looplab-Metric") == "", "a superseded lifecycle is no candidate"
    assert _trailer(repo, "node-0", "Looplab-Metric") == "0.9"
    assert _trailer(repo, "node-0", "Looplab-Generation") == "1"
    # Each commit is dated by its OWN lifecycle's node_created row.
    assert _git(repo, "log", "-1", "--format=%at", "node-0.g0") == "1790000100"
    assert _git(repo, "log", "-1", "--format=%at", "node-0") == "1790000400"
    assert _git(repo, "log", "-1", "--format=%at", "node-1") == "1790000200"


def test_an_eval_reset_is_its_own_lifecycle_dated_by_the_reset(tmp_path):
    rd = tmp_path / "run"
    _write_log(rd, [
        ("run_started", 1_790_000_000.0, {"run_id": "run", "task_id": "t", "goal": "g",
                                          "direction": "max"}),
        ("node_created", 1_790_000_100.0, _created(0, [], code="print(0)\n")),
        ("node_evaluated", 1_790_000_110.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                             "violations": []}),
        ("node_reset", 1_790_000_200.0, {"node_id": 0, "generation": 0, "from_stage": "eval"}),
        ("node_evaluated", 1_790_000_210.0, {"node_id": 0, "generation": 1, "metric": 0.55,
                                             "violations": []}),
    ])
    repo = tmp_path / "repo"
    assert _export(rd, repo).exit_code == 0
    # The same code re-scored: one tree, two lifecycles, each with its own record.
    assert (_git(repo, "rev-parse", "node-0^{tree}")
            == _git(repo, "rev-parse", "node-0.g0^{tree}"))
    assert _git(repo, "log", "-1", "--format=%at", "node-0") == "1790000200"
    assert _trailer(repo, "node-0", "Looplab-Metric") == "0.55"
    assert "a node_reset from 'eval' opened node-0" in _body(repo, "node-0.g0")


def test_a_lifecycle_the_holdout_requeue_reopened_is_its_own_commit(tmp_path):
    """Critic 2026-09-26, driven: after a holdout is disclosed, the fold RE-OPENS every evaluated
    incumbent as a fresh generation when the search changes again (`replay.py::
    _requeue_partition_bound_results` — here at the `resume` that reopens the finished run). No
    `node_reset` names them, and the export watched only resets: node 1 became a root commit citing
    `node-0.g0 (not in the log)` and both commits were dated 1970."""
    rd = tmp_path / "run"
    _write_log(rd, [
        ("run_started", 1_790_000_000.0, {"run_id": "run", "task_id": "t", "goal": "g",
                                          "direction": "max"}),
        ("node_created", 1_790_000_010.0, _created(0, [], code="print(0)\n")),
        ("node_evaluated", 1_790_000_020.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                             "violations": []}),
        ("node_created", 1_790_000_030.0, _created(1, [0], code="print(1)\n",
                                                   parent_generations={"0": 0})),
        ("node_evaluated", 1_790_000_040.0, {"node_id": 1, "generation": 0, "metric": 0.6,
                                             "violations": []}),
        ("holdout_evaluated", 1_790_000_050.0, {"node_id": 1, "generation": 0, "metric": 0.55,
                                                "search_epoch": 0}),
        ("run_finished", 1_790_000_060.0, {}),
        ("resume", 1_790_000_100.0, {}),
        ("node_evaluated", 1_790_000_110.0, {"node_id": 0, "generation": 1, "metric": 0.51,
                                             "violations": []}),
        ("node_evaluated", 1_790_000_120.0, {"node_id": 1, "generation": 1, "metric": 0.61,
                                             "violations": []}),
        ("node_created", 1_790_000_200.0, _created(2, [1], code="print(2)\n",
                                                   parent_generations={"1": 1})),
        ("node_evaluated", 1_790_000_210.0, {"node_id": 2, "generation": 0, "metric": 0.7,
                                             "violations": []}),
    ])
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "2 superseded lifecycle(s) as node-<id>.g<generation>" in result.stdout
    # Generation 0 of each incumbent is its own commit, dated by its own node_created and wired to
    # the lifecycle it was built from…
    assert _git(repo, "log", "-1", "--format=%at", "node-0.g0") == "1790000010"
    assert _git(repo, "log", "-1", "--format=%at", "node-1.g0") == "1790000030"
    assert _trailer(repo, "node-1.g0", "Looplab-Parents") == "node-0.g0"
    assert _git(repo, "rev-parse", "node-1.g0^") == _git(repo, "rev-parse", "node-0.g0^{commit}")
    requeued = _body(repo, "node-0.g0")
    assert ("Looplab-Status: superseded — the disclosed holdout's epoch rotated at `resume` and "
            "re-opened node-0 for re-evaluation on the newly hidden rows; this lifecycle had reached "
            "'evaluated' and is no longer a candidate") in requeued
    # …and the lifecycle the requeue opened is dated by that rotation, the same code re-scored.
    assert _git(repo, "log", "-1", "--format=%at", "node-0") == "1790000100"
    assert _git(repo, "log", "-1", "--format=%at", "node-1") == "1790000100"
    assert (_git(repo, "rev-parse", "node-1^{tree}") == _git(repo, "rev-parse", "node-1.g0^{tree}"))
    assert _trailer(repo, "node-1", "Looplab-Metric") == "0.61"
    # A child built from the re-opened lifecycle names THAT one.
    assert _trailer(repo, "node-2", "Looplab-Parents") == "node-1"
    assert _git(repo, "rev-parse", "node-2^") == _git(repo, "rev-parse", "node-1^{commit}")


def test_a_reset_after_a_disclosure_ends_the_other_incumbents_lifecycles_too(tmp_path):
    """The same requeue, opened by a stamped `node_reset` of ANOTHER node: node 2's reset re-opens
    nodes 0 and 1 as well, so node-2.g0's parent is node 1's generation 0 — a commit, not a name."""
    rd = tmp_path / "run"
    _write_log(rd, [
        ("run_started", 1_790_000_000.0, {"run_id": "run", "task_id": "t", "goal": "g",
                                          "direction": "max"}),
        ("node_created", 1_790_000_010.0, _created(0, [], code="print(0)\n")),
        ("node_evaluated", 1_790_000_020.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                             "violations": []}),
        ("node_created", 1_790_000_030.0, _created(1, [0], code="print(1)\n",
                                                   parent_generations={"0": 0})),
        ("node_evaluated", 1_790_000_040.0, {"node_id": 1, "generation": 0, "metric": 0.6,
                                             "violations": []}),
        ("node_created", 1_790_000_050.0, _created(2, [1], code="print(2)\n",
                                                   parent_generations={"1": 0})),
        ("node_evaluated", 1_790_000_060.0, {"node_id": 2, "generation": 0, "metric": 0.7,
                                             "violations": []}),
        ("holdout_evaluated", 1_790_000_070.0, {"node_id": 2, "generation": 0, "metric": 0.65,
                                                "search_epoch": 0}),
        ("node_reset", 1_790_000_100.0, {"node_id": 2, "generation": 0, "from_stage": "eval"}),
    ])
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "3 superseded lifecycle(s) as node-<id>.g<generation>" in result.stdout
    assert _trailer(repo, "node-2.g0", "Looplab-Parents") == "node-1.g0"
    assert _git(repo, "rev-parse", "node-2.g0^") == _git(repo, "rev-parse", "node-1.g0^{commit}")
    assert "a node_reset from 'eval' opened node-2" in _body(repo, "node-2.g0")
    assert "epoch rotated at `node_reset` and re-opened node-1" in _body(repo, "node-1.g0")
    for tag in ("node-0", "node-1", "node-2"):
        assert _git(repo, "log", "-1", "--format=%at", tag) == "1790000100", tag


def _disclosed(extra):
    """Nodes 0 and 1 (1 built from 0) evaluated, node 1's holdout disclosed, then `extra` rows."""
    return [
        ("run_started", 1_790_000_000.0, {"run_id": "run", "task_id": "t", "goal": "g",
                                          "direction": "max"}),
        ("node_created", 1_790_000_010.0, _created(0, [], code="print(0)\n")),
        ("node_evaluated", 1_790_000_020.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                             "violations": []}),
        ("node_created", 1_790_000_030.0, _created(1, [0], code="print(1)\n",
                                                   parent_generations={"0": 0})),
        ("node_evaluated", 1_790_000_040.0, {"node_id": 1, "generation": 0, "metric": 0.6,
                                             "violations": []}),
        ("holdout_evaluated", 1_790_000_050.0, {"node_id": 1, "generation": 0, "metric": 0.55,
                                                "search_epoch": 0}),
        *extra,
    ]


@pytest.mark.parametrize("opener,requeued", [
    # A genuinely NEW candidate after the disclosure (an inject, a fork, a policy action).
    (("node_created", 1_790_000_100.0, _created(2, [1], code="print(2)\n",
                                                parent_generations={"1": 0})), {0, 1}),
    # A tombstone: the candidate set changed, so the epoch rotates and the survivor re-opens.
    (("node_tombstoned", 1_790_000_100.0, {"node_ids": [1]}), {0}),
])
def test_every_kind_of_requeue_is_watched_not_just_resume_and_reset(tmp_path, opener, requeued):
    """Critic 2026-09-26: a fix that watched only `resume` and `node_reset` passed every test while
    an inject or a tombstone after the disclosure still lost the superseded lifecycles."""
    superseded, born = git_export.node_lifecycles(
        [SimpleNamespace(seq=i, ts=ts, type=etype, data=data)
         for i, (etype, ts, data) in enumerate(_disclosed([opener]))])
    assert {node_id for node_id, _gen in superseded} == requeued
    for node_id in requeued:
        assert superseded[(node_id, 0)].requeued_at == opener[0]
        assert born[(node_id, 1)] == 1_790_000_100


def test_watching_the_requeue_pool_copies_nothing_until_a_rotation(monkeypatch):
    """The cost is a copy per RE-OPENED lifecycle, not per node per row: 200 folded rows after a
    disclosure copy nothing (critic 2026-09-26, driven: copying at every row was quadratic)."""
    copies = []
    real = git_export._lifecycle
    monkeypatch.setattr(git_export, "_lifecycle",
                        lambda *a, **k: copies.append(k.get("requeued")) or real(*a, **k))
    quiet = [("annotation", 1_790_000_060.0 + i, {"node_id": 0, "text": f"note {i}"})
             for i in range(200)]
    rows = _disclosed(quiet)
    git_export.node_lifecycles([SimpleNamespace(seq=i, ts=ts, type=etype, data=data)
                                for i, (etype, ts, data) in enumerate(rows)])
    assert copies == []
    rows = _disclosed([*quiet, ("resume", 1_790_000_900.0, {})])
    git_export.node_lifecycles([SimpleNamespace(seq=i, ts=ts, type=etype, data=data)
                                for i, (etype, ts, data) in enumerate(rows)])
    assert copies == [True, True], "exactly the two re-opened lifecycles, once each"


def test_the_champion_is_the_fold_best_and_a_promote_is_published_beside_it(tmp_path):
    rd = _run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    # The promote alias checks no status: here it names a FAILED node, which must never be what a
    # checkout of the export holds. And the real best carries a trust flag `audit` did not enforce.
    store.append("promote", {"node_id": 4})
    store.append("reward_hack_suspected", {"node_id": 3, "generation": 0,
                                           "signals": [{"signal": "leakage"}]})
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "branch champion = node 3, checked out" in result.stdout
    assert "branch promoted = node 4" in result.stdout
    assert _git(repo, "rev-parse", "champion") == _git(repo, "rev-parse", "node-3^{commit}")
    assert _git(repo, "rev-parse", "promoted") == _git(repo, "rev-parse", "node-4^{commit}")
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/champion"
    assert (repo / "train.py").read_text(encoding="utf-8") == "EPOCHS = 3\n"
    assert not (repo / "solution.py").exists(), "node 4's tree was checked out"
    assert _trailer(repo, "node-3", "Looplab-Champion") == "yes"
    assert _trailer(repo, "node-3", "Looplab-Champion-Caveats") == "trust_flagged"
    assert (_trailer(repo, "node-3", "Looplab-Trust-Flagged")
            == "yes (recorded, not enforced under trust_gate=audit)")
    assert _trailer(repo, "node-3", "Looplab-Promoted") == ""
    assert _trailer(repo, "node-4", "Looplab-Promoted") == "yes"
    assert _trailer(repo, "node-4", "Looplab-Champion") == ""


def test_a_metric_ships_with_the_receipts_that_decide_whether_it_counts(tmp_path):
    rd = _run(tmp_path)
    store = EventStore(rd / "events.jsonl")
    # 0.99 outranks the real best for anyone reading `Looplab-Metric` alone. It is a salvaged number
    # the rung excluded, and node 2 is trust-flagged under an ENFORCING gate.
    _node(store, 5, [3], code="print(5)\n")
    _evaluated(store, 5, 0.99, violations=[{"name": "metric_salvaged", "detail": "recovered"}],
               metric_provenance={"salvaged": True})
    store.append("reward_hack_suspected", {"node_id": 2, "generation": 0,
                                           "signals": [{"signal": "leakage"}]})
    store.append("trust_gate_changed", {"trust_gate": "gate"})
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert _trailer(repo, "node-5", "Looplab-Metric") == "0.99"
    assert _trailer(repo, "node-5", "Looplab-Feasible") == "no"
    assert _trailer(repo, "node-5", "Looplab-Violations") == "1 (metric_salvaged)"
    assert _trailer(repo, "node-5", "Looplab-Salvaged") == "yes"
    assert _trailer(repo, "node-5", "Looplab-Counts-Toward-Best") == "no"
    assert _trailer(repo, "node-2", "Looplab-Trust-Flagged") == "yes (enforced by trust_gate=gate)"
    assert _trailer(repo, "node-2", "Looplab-Counts-Toward-Best") == "no"
    assert _trailer(repo, "node-3", "Looplab-Counts-Toward-Best") == "yes"
    assert _trailer(repo, "node-3", "Looplab-Feasible") == "yes"
    assert _trailer(repo, "node-3", "Looplab-Violations") == "0"
    assert _trailer(repo, "node-3", "Looplab-Champion") == "yes"
    assert _trailer(repo, "node-5", "Looplab-Champion") == ""


def test_solution_py_is_the_code_that_ran_both_ways(tmp_path):
    rd, store = _store(tmp_path)
    _started(store)
    # The sandbox writes `solution.py` from `code`; `write_node_files` never writes this key.
    _node(store, 0, [], code="print('ran')\n", files={"solution.py": "print('never ran')\n"})
    # A repo-task node carries no `code`: nothing of the node's is `solution.py` in its checkout.
    _node(store, 1, [0], files={"solution.py": "print('never ran either')\n", "train.py": "x\n"})
    _evaluated(store, 0, 0.5)
    _evaluated(store, 1, 0.6)
    repo = tmp_path / "repo"
    assert _export(rd, repo).exit_code == 0
    assert _git(repo, "show", "node-0:solution.py") == "print('ran')"
    assert _tree(repo, "node-1") == ["train.py"]
    for ref in ("node-0", "node-1"):
        assert _trailer(repo, ref, "Looplab-Skipped-Paths") == (
            "1 (1 never materialized by the engine; the log keeps every one)")


def test_every_name_the_checkout_would_not_hold_is_counted_by_reason(tmp_path):
    rd, store = _store(tmp_path)
    _started(store)
    store.append("data_provenance", {"assets": {"grader.py": "0123456789abcdef"}})
    _node(store, 0, [], code="print('ran')\n", files={
        "solution.py": "print('never ran')\n",          # the entrypoint is `code`
        "grader.py": "forged\n",                         # a task asset: the task's, never the node's
        "dir/win.py": "kept\n", "dir\\win.py": "one file with dir/win.py on Windows\n",
        "a": "a file\n", "a/b.py": "needs `a` to be a directory\n",
        "README.md": "kept\n", "Readme.md": "one file with README.md on NTFS and APFS\n",
        "gone.py": "written, then deleted by the node itself\n",
        ".gitattributes": "* filter=lfs\n",              # would run the reader's filter driver
    }, deleted=["gone.py", "solution.py", "base_only.py"])
    _evaluated(store, 0, 0.5)
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert _tree(repo, "node-0") == ["README.md", "a", "dir/win.py", "solution.py"]
    assert _git(repo, "show", "node-0:solution.py") == "print('ran')"
    assert _git(repo, "show", "node-0:dir/win.py") == "kept"
    assert _git(repo, "show", "node-0:README.md") == "kept"
    assert _trailer(repo, "node-0", "Looplab-Skipped-Paths") == (
        "7 (1 unsafe in a checkout; 2 never materialized by the engine; 3 colliding with another "
        "name on some checkout; 1 removed by the node's own deleted list; the log keeps every one)")
    assert (_trailer(repo, "node-0", "Looplab-Deleted-From-Base")
            == "base_only.py, gone.py, solution.py")
    assert "7 path(s) left out" in result.stdout


def test_a_path_a_checkout_could_turn_against_its_reader_is_counted_never_written(tmp_path):
    rd, store = _store(tmp_path)
    _started(store)
    fine = "dir/a b \u00e9.py"
    _node(store, 0, [], files={"../evil.py": "x", ".git/config": "y", "C:evil.py": "z",
                              "/abs.py": "w", ".git./hooks/post-checkout": "v",
                              "sub/GIT~1/config": "u", "x.py:stream": "t", "NUL.py": "s",
                              fine: "fine\n"})
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert "nothing is checked out" in result.stdout          # no metric, no champion
    assert _tree(repo, "node-0") == [fine]
    assert "Looplab-Skipped-Paths: 8 (8 unsafe in a checkout;" in _body(repo, "node-0")
    assert not (tmp_path / "evil.py").exists()


@pytest.mark.parametrize("path,expected", [
    ("a.py", "a.py"), ("dir/sub/a.py", "dir/sub/a.py"), ("dir\\win.py", "dir/win.py"),
    ("dir/\u00fcber.py", "dir/\u00fcber.py"), ("a b.py", "a b.py"), ("my.git.py", "my.git.py"),
    ("gitignore.txt", "gitignore.txt"), ("console.py", "console.py"), ("com10.py", "com10.py"),
    ("d/" + "x" * 255, "d/" + "x" * 255),
    ("../up.py", None), ("dir/../x", None), ("./x", None), ("/abs", None), ("", None),
    ("a//b", None), (7, None), ("x" * 5000, None), ("d/" + "x" * 256, None),
    # `.git` as NTFS and HFS+ read it, and the 8.3 short names git refuses
    (".git/hooks/pre-commit", None), ("sub/.GIT/config", None), (".git.", None), (".git ", None),
    ("GIT~1/config", None), (".git::$INDEX_ALLOCATION/x", None), (".g\u200cit/config", None),
    (".git\ufeff/config", None), ("gi7eba~1", None), ("GITMOD~1", None), ("MAILMA~3", None),
    # every other name git or a git host interprets
    (".gitmodules", None), (".gitmodules.", None), (".gitattributes", None), (".gitignore", None),
    (".github/workflows/ci.yml", None), (".gitlab-ci.yml", None), (".lfsconfig", None),
    (".mailmap", None),
    # what Windows cannot store, or reads as something else
    ("C:x.py", None), ("x.py:stream", None), ("con", None), ("NUL", None), ("nul.txt", None),
    ("com1.tar.gz", None), ("lpt9", None), ("a<b.py", None), ('q"x.py', None), ("a|b", None),
    ("what?.py", None), ("star*.py", None), ("notes.", None), ("trailing ", None),
    # control characters: C0, DEL, C1
    ("a\x00b", None), ("a\x01b", None), ("a\nb.py", None), ("a\x7fb", None), ("a\x85b", None),
])
def test_the_tree_path_rule(path, expected):
    assert safe_tree_path(path) == expected


def test_a_quoted_path_escapes_what_fast_import_would_misread():
    # `safe_tree_path` admits none of these; the quoting is the second lock and must still hold.
    assert _quoted('a"b\\c\nd\te\x01f\x7fg \u00e9') == (
        b'"a\\"b\\\\c\\nd\\te\\001f\\177g \xc3\xa9"')


def test_no_message_field_can_forge_a_trailer_or_hide_one(tmp_path):
    rd, store = _store(tmp_path)
    _started(store, run_id="run\x00id")
    _node(store, 0, [], code="print(0)\n", operator="improve\n\nLooplab-Metric: 99",
          rationale="a\x00b\u202e", hypothesis="h\x00\n\nLooplab-Promoted: yes",
          deleted=["x\x00y.py"])
    _evaluated(store, 0, 0.5)
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    # The CLI runs `fsck --strict` before it reports success: a NUL anywhere would fail it.
    assert result.exit_code == 0, result.output
    assert _git(repo, "log", "-1", "--format=%s", "node-0") == (
        "node 0: improve Looplab-Metric: 99 — a b")
    assert _trailer(repo, "node-0", "Looplab-Metric") == "0.5"
    assert _trailer(repo, "node-0", "Looplab-Promoted") == "", "nothing was promoted"
    assert _trailer(repo, "node-0", "Looplab-Operator") == "improve Looplab-Metric: 99"
    assert _trailer(repo, "node-0", "Looplab-Run") == "run id"
    assert _trailer(repo, "node-0", "Looplab-Deleted-From-Base") == "x y.py"
    body = _git_run(repo, "cat-file", "commit", "node-0").stdout
    assert b"\x00" not in body and "\u202e".encode("utf-8") not in body


def test_the_message_carries_each_receipt_in_its_one_format(tmp_path):
    rd, store = _store(tmp_path)
    _started(store)
    _node(store, 0, [], code="print(0)\n", params={f"p{i:03d}": i * 1.5 for i in range(300)})
    _evaluated(store, 0, 0.1 + 0.2)
    _node(store, 1, [0], code="print(1)\n")
    _evaluated(store, 1, 1e-07)
    store.append("node_tombstoned", {"node_ids": [1]})
    # LAST: a new candidate after a disclosed holdout rotates it away (`_invalidate_disclosed_holdout`).
    store.append("node_confirmed", {"node_id": 0, "generation": 0, "mean": 0.25, "std": 0.02,
                                    "seeds": 3})
    store.append("holdout_evaluated", {"node_id": 0, "generation": 0, "metric": 0.2})
    repo = tmp_path / "repo"
    assert _export(rd, repo).exit_code == 0
    body = _body(repo, "node-0")
    assert "Looplab-Metric: 0.30000000000000004" in body          # repr: never a rounded print
    assert "Looplab-Confirmed-Mean: 0.25 (std 0.02, 3 seeds)" in body
    assert "Looplab-Holdout-Metric: 0.2" in body
    params = _trailer(repo, "node-0", "Looplab-Params")
    assert params.startswith('{"p000": 0.0, "p001": 1.5') and params.endswith(" … (cut)")
    assert len(params) == 2000 + len(" … (cut)")
    assert "Looplab-Status: evaluated (tombstoned)" in _body(repo, "node-1")
    assert _trailer(repo, "node-1", "Looplab-Metric") == "1e-07"
    assert _trailer(repo, "node-1", "Looplab-Counts-Toward-Best") == "no"


def test_a_parent_with_a_larger_id_is_committed_first_and_named_once(tmp_path):
    rd, store = _store(tmp_path)
    _started(store)
    _node(store, 5, [], code="print(5)\n")
    _node(store, 3, [5], code="print(3)\n")
    _node(store, 1, [5, 5], code="print(1)\n")        # one parent, named twice
    for node_id, metric in ((5, 0.5), (3, 0.6), (1, 0.7)):
        _evaluated(store, node_id, metric)
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    five = _git(repo, "rev-parse", "node-5^{commit}")
    assert _git(repo, "log", "-1", "--format=%P", "node-3").split() == [five]
    assert _git(repo, "log", "-1", "--format=%P", "node-1").split() == [five]


def test_a_self_parent_a_string_id_and_an_impossible_date_do_not_break_it(tmp_path):
    rd = tmp_path / "run"
    _write_log(rd, [
        ("run_started", 1_790_000_000.0, {"run_id": "run", "task_id": "t", "goal": "g",
                                          "direction": "max"}),
        # A numeric-string id: the fold accepts it as node 0, so its date must come from it too.
        ("node_created", 1_790_000_100.0, _created("0", [], code="print(0)\n")),
        ("node_evaluated", 1_790_000_110.0, {"node_id": 0, "generation": 0, "metric": 0.5,
                                             "violations": []}),
        ("node_reset", 1_790_000_200.0, {"node_id": 0, "generation": 0, "from_stage": "implement"}),
        # A rebuild naming ITSELF as parent, which the fold accepts — at a date past what git can
        # hold (fast-import refuses 2**64; `fsck --strict` refuses anything from 2**63).
        ("node_created", 2.0 ** 64, _created(0, [0], code="print(1)\n", generation=1,
                                             parent_generations={"0": 1})),
        ("node_evaluated", 1_790_000_300.0, {"node_id": 0, "generation": 1, "metric": 0.6,
                                             "violations": []}),
    ])
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    assert result.exit_code == 0, result.output
    assert _git(repo, "log", "-1", "--format=%P", "node-0") == ""
    assert _git(repo, "log", "-1", "--format=%at", "node-0") == "0"
    assert _git(repo, "log", "-1", "--format=%at", "node-0.g0") == "1790000100"
    assert _git(repo, "show", "node-0:solution.py") == "print(1)"


def test_parents_come_first_whatever_their_ids_and_a_cycle_cannot_hang_it():
    assert _order({0: [5], 5: [], 3: [0, 5]}) == [5, 0, 3]
    assert _order({1: [2], 2: [1], 3: []}) == [3, 1, 2]
    assert _order({(0, 1): [(1, 0)], (0, 0): [], (1, 0): [(0, 0)]}) == [(0, 0), (1, 0), (0, 1)]
    assert json.dumps(_order({})) == "[]"
    # Only the cycle is broken: 5 (the smallest key ON it) is released, and 1 — a descendant of
    # the cycle with a SMALLER key — still comes after its parent instead of being emitted early.
    assert _order({5: [6], 6: [5], 1: [5], 3: []}) == [3, 5, 1, 6]


def test_ordering_a_long_chain_is_not_quadratic():
    n = 20_000
    chain = {i: ([i + 1] if i + 1 < n else []) for i in range(n)}
    started = time.perf_counter()
    assert _order(chain) == list(range(n - 1, -1, -1))
    # The rescan-everything loop took 14.3 s at 16,000; this is O(n log n) and takes well under 0.5.
    assert time.perf_counter() - started < 5.0


def test_it_refuses_a_non_empty_target_and_a_missing_git(tmp_path, monkeypatch):
    rd = _run(tmp_path)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine", encoding="utf-8")
    result = _export(rd, busy)
    assert result.exit_code == 2 and "not an empty directory" in result.stderr
    assert result.stdout == ""
    assert (busy / "keep.txt").read_text() == "mine"
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = _export(rd, tmp_path / "fresh")
    assert result.exit_code == 2 and "git is not installed" in result.stderr
    assert not (tmp_path / "fresh").exists()


def test_a_git_older_than_the_object_format_option_is_refused_up_front(tmp_path, monkeypatch):
    """The docs promised exit 2 for a git older than 2.29; the code let `git init
    --object-format` fail with exit 1 (critic 2026-09-26, driven with a wrapper that rejects the
    option). The version is asked first, and read off the real git here."""
    from looplab.cli import export_cmds

    real = export_cmds._git_version(shutil.which("git"))
    assert isinstance(real, tuple) and len(real) == 2 and real >= export_cmds._MIN_GIT, real
    rd = _run(tmp_path)
    repo = tmp_path / "repo"
    monkeypatch.setattr(export_cmds, "_git_version", lambda git: (2, 20))
    result = _export(rd, repo)
    assert result.exit_code == 2, result.output
    assert "git 2.20 is too old — export-git needs git 2.29 or later" in result.stderr
    assert not repo.exists() and _leftovers(tmp_path) == []
    # No system gitattributes reaches the export either: they would rewrite the checked-out bytes.
    assert export_cmds._hermetic_git_env(str(tmp_path))["GIT_ATTR_NOSYSTEM"] == "1"


def test_it_refuses_a_file_a_target_inside_the_run_and_an_empty_run(tmp_path):
    rd = _run(tmp_path)
    a_file = tmp_path / "file.txt"
    a_file.write_text("mine", encoding="utf-8")
    result = _export(rd, a_file)
    assert result.exit_code == 2 and "not an empty directory" in result.stderr
    assert a_file.read_text(encoding="utf-8") == "mine"
    inside = rd / "export"
    result = _export(rd, inside)
    assert result.exit_code == 2 and "inside the run directory" in result.stderr
    assert not inside.exists()
    empty_run, store = _store(tmp_path / "second")
    _started(store)
    result = _export(empty_run, tmp_path / "nothing")
    assert result.exit_code == 2 and "no nodes yet" in result.stderr
    assert not (tmp_path / "nothing").exists()


def test_it_refuses_a_symlinked_or_dangling_target(tmp_path):
    rd = _run(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    link = tmp_path / "link"
    _symlink(link, empty)
    result = _export(rd, link)
    assert result.exit_code == 2 and "is a symbolic link" in result.stderr
    assert not any(empty.iterdir())
    dangling = tmp_path / "dangling"
    _symlink(dangling, tmp_path / "nowhere")
    result = _export(rd, dangling)
    assert result.exit_code == 2 and "symbolic link to nothing" in result.stderr
    assert not (tmp_path / "nowhere").exists()


def _leftovers(tmp_path: Path) -> list:
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(".export-git")]


def test_a_git_failure_exits_1_says_why_and_leaves_nothing_behind(tmp_path, monkeypatch):
    rd = _run(tmp_path)
    repo = tmp_path / "repo"

    def stream(data: bytes):
        return lambda events, state, **_: FastImport(stream=data, commits=1, superseded=0,
                                                     skipped_paths=0, champion=0, promoted=None)

    # fast-import refuses the stream: the `fatal:` line is what is printed, not "dumping crash
    # report to …", whose file the cleanup has already removed.
    monkeypatch.setattr(git_export, "fast_import_stream", stream(b"no-such-command\n"))
    result = _export(rd, repo)
    assert result.exit_code == 1, result.output
    assert "git fast-import failed: fatal:" in result.stderr and "crash report" not in result.stderr
    assert not repo.exists() and _leftovers(tmp_path) == []
    # fast-import accepts a tree `fsck --strict` refuses: that is a failed export too.
    monkeypatch.setattr(git_export, "fast_import_stream", stream(
        b"commit refs/tags/node-0\nmark :1\ncommitter X <x@y> 0 +0000\ndata 2\nx\ndeleteall\n"
        b'M 100644 inline ".git./config"\ndata 2\nx\n\ndone\n'))
    result = _export(rd, repo)
    assert result.exit_code == 1 and "git fsck --strict failed: error" in result.stderr
    assert "hasDotgit" in result.stderr
    assert not repo.exists() and _leftovers(tmp_path) == []
    # …and nothing half-built refuses the re-run.
    monkeypatch.undo()
    assert _export(rd, repo).exit_code == 0


def test_a_truncated_log_says_so_on_every_commit(tmp_path):
    rd = _run(tmp_path)
    good = EventStore(rd / "events.jsonl").read_all()
    with open(rd / "events.jsonl", "ab") as fh:
        fh.write(b"{this line is not json\n")
        fh.write(json.dumps({"v": 1, "seq": len(good) + 1, "ts": 1.0, "type": "node_created",
                             "data": _created(9, [])}).encode("utf-8") + b"\n")
    repo = tmp_path / "repo"
    result = _export(rd, repo)
    # The readable prefix is exported, as `export-bundle` and `export-notebook` do — and said, on
    # stderr for the operator AND on every commit for whoever reads the repository.
    assert result.exit_code == 0, result.output
    assert "[INCOMPLETE RECORD]" in result.stderr
    for nid in range(5):
        note = _trailer(repo, f"node-{nid}", "Looplab-Log-Incomplete")
        assert note.startswith("[INCOMPLETE RECORD] run's event log stops being readable at line"), note
    assert "node-9" not in _git(repo, "tag")
