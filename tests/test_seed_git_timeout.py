"""A WEDGED `git ls-files` is not a MISSING git (review 2026-09-22, ES1-05), and the seed walk
records what it copied (docs/29 F3, `f3-workspace-byte-total`).

`engine/workspace_seed.py::seed_repo_tree` asks `git ls-files` which files to copy, with a 120 s
deadline, and a blind `except Exception` sent every failure to the full-copy fallback. That is right
for "this source is not a git worktree" and exactly wrong for "git hung": on the network mounts this
box measures at 105-950 ms per `lstat`, a listing that times out is a large tree, and the fallback
then deep-copies every untracked checkpoint and dataset into EVERY node workdir — with the
`workspace_seeded` row, the only record, saying the same `copytree` it says for a non-git source.

So a timeout is its own case: under `auto` it still falls back (the documented rung), but at WARNING
and recorded as `copytree:git_timeout`; under an EXPLICIT `tracked` it refuses, because `tracked` is
the operator saying "never copy the untracked tree" and the timeout is the one case where honouring
the fallback would do exactly that.

Tier 1 throughout: a real git repo with a real untracked "checkpoint", the real seeder, the real
engine and its real event log. Only `git ls-files` itself is made to hang, because a real hang costs
the 120 s deadline per test.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.core.errors import EnvironmentRefusal, OperatorRefusal
from looplab.engine import workspace_seed
from looplab.engine.orchestrator import Engine
from looplab.engine.workspace_seed import TrackedSeedTimeout, seed_repo_tree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

from test_repo_protected_seed import _git_repo

_M = {"kind": "stdout_json", "key": "metric"}
_TRAIN = 'import json\nprint(json.dumps({"metric": 1.0}))\n'
_CHECKPOINT = b"\x00" * 4096            # the untracked artifact `auto`/`tracked` exist to skip


def _repo(tmp_path: Path) -> Path:
    repo = _git_repo(tmp_path / "repo", {"train.py": _TRAIN, "cfg/params.yaml": "lr: 0.1\n"})
    (repo / "ckpt").mkdir()
    (repo / "ckpt" / "model.bin").write_bytes(_CHECKPOINT)
    return repo


def _tracked_bytes(repo: Path) -> int:
    """The bytes the two TRACKED files hold ON DISK — what a walk that copies them copies. Read
    back rather than computed from the strings the fixture was handed: `_git_repo` writes with
    `write_text`, which on Windows turns each `\\n` into `\\r\\n`, so the LF length of the
    literals undercounts there by one byte a line (Windows run 51: 58 == 55, 4154 == 4151)."""
    return (repo / "train.py").stat().st_size + (repo / "cfg" / "params.yaml").stat().st_size


def _wedge_ls_files(monkeypatch) -> list:
    """Make `git ls-files` hang past its deadline; every other subprocess runs for real."""
    real_run = subprocess.run
    asked: list = []

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and "ls-files" in argv:
            asked.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return asked


def _engine(tmp_path: Path, task: RepoTask, **kw) -> Engine:
    r, d = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)


def _task(repo: Path, **kw) -> RepoTask:
    return RepoTask(id="seed", direction="max", editable_path=str(repo),
                    eval=EvalSpec(command=[sys.executable, "train.py"], metric=_M), **kw)


def _rows(run_dir: Path, etype: str) -> list[dict]:
    return [json.loads(line)["data"]
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line)["type"] == etype]


# --------------------------------------------------------------------------- the timeout, `auto`

def test_a_wedged_listing_under_auto_falls_back_loudly_and_the_row_names_why(tmp_path, monkeypatch,
                                                                            caplog):
    repo = _repo(tmp_path)
    asked = _wedge_ls_files(monkeypatch)
    eng = _engine(tmp_path, _task(repo))
    with caplog.at_level(logging.WARNING, logger="looplab.engine.workspace_seed"):
        eng._seed_workspace(tmp_path / "run" / "nodes" / "node_0")

    assert asked and all(t for t in asked), "the listing must still run under a deadline"
    materialized = _rows(tmp_path / "run", "workspace_seeded")[-1]["materialized"]
    assert ".[auto]:copytree:git_timeout" in materialized, (
        "the timeout fallback must be distinguishable from a non-git source's plain `copytree`: "
        f"{materialized}")
    # `auto`'s documented rung still runs — the untracked tree WAS copied, and the operator is told.
    assert (tmp_path / "run" / "nodes" / "node_0" / "ckpt" / "model.bin").is_file()
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "ls-files" in r.message]
    assert warned, "a deep copy taken because git hung must be said at WARNING"
    assert "untracked" in warned[0].message and "tracked" in warned[0].message


def test_the_timeout_fallback_is_a_distinct_fact_on_the_seed_result(tmp_path, monkeypatch):
    """The seam every caller shares, below the engine: the count keeps its contract (-1 = a full
    copy), and the reason rides beside it rather than being guessed from it."""
    repo = _repo(tmp_path)
    _wedge_ls_files(monkeypatch)
    result = seed_repo_tree(repo, tmp_path / "dst", None, "auto")
    assert result == -1 and result < 0, "a full copy is still the count's -1"
    assert result.fallback == workspace_seed.GIT_TIMEOUT_FALLBACK == "git_timeout"


# --------------------------------------------------------------------------- the timeout, `tracked`

def test_a_wedged_listing_under_explicit_tracked_refuses_rather_than_deep_copying(tmp_path,
                                                                                 monkeypatch):
    repo = _repo(tmp_path)
    _wedge_ls_files(monkeypatch)
    with pytest.raises(TrackedSeedTimeout) as caught:
        seed_repo_tree(repo, tmp_path / "dst", None, "tracked")
    refusal = caught.value
    # A deliberate refusal is a TYPE: the CLI prints the family as one line, and the eval
    # containment closes the node on it and pauses the run (a fault about the box, not the idea).
    assert isinstance(refusal, EnvironmentRefusal) and isinstance(refusal, OperatorRefusal)
    text = str(refusal)
    assert "ls-files" in text and "tracked" in text and "seed_mode" in text, text
    assert not (tmp_path / "dst" / "ckpt").exists(), "the refusal copied the untracked tree anyway"


def test_an_explicit_tracked_run_closes_the_node_and_pauses_instead_of_copying(tmp_path,
                                                                              monkeypatch):
    """End to end on a real run: fail CLOSED means no deep copy, one `engine_error` terminal for the
    node whose seed was refused, and a pause — the next node would hit the same wedged listing."""
    repo = _repo(tmp_path)
    _wedge_ls_files(monkeypatch)
    eng = _engine(tmp_path, _task(repo, seed_mode="tracked"))
    anyio.run(eng.run)

    failed = _rows(tmp_path / "run", "node_failed")
    assert failed and failed[0]["reason"] == "engine_error", failed
    assert "TrackedSeedTimeout" in failed[0]["error"], failed[0]["error"]
    assert _rows(tmp_path / "run", "pause"), "a refused seed must pause the run, not repeat per node"
    assert not list((tmp_path / "run" / "nodes").rglob("model.bin")), (
        "the untracked checkpoint reached a node workdir under an explicit `tracked` seed")
    assert not _rows(tmp_path / "run", "workspace_seeded"), "a refused seed recorded a seed"


# --------------------------------------------------------------------------- the old path, unchanged

@pytest.mark.parametrize("mode", ["auto", "tracked"])
def test_a_source_git_cannot_list_still_takes_the_documented_full_copy(tmp_path, monkeypatch, mode):
    """Not a git worktree / no git at all: BOTH modes still fall back, with no timeout marker."""
    repo = _repo(tmp_path)

    def no_git(argv, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", no_git)
    result = seed_repo_tree(repo, tmp_path / "dst", None, mode)
    assert result == -1 and result.fallback is None
    assert (tmp_path / "dst" / "ckpt" / "model.bin").is_file()


# --------------------------------------------------------------------------- the byte total

def test_the_row_records_the_bytes_the_tracked_walk_copied(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, _task(repo))
    eng._seed_workspace(tmp_path / "run" / "nodes" / "node_0")

    row = _rows(tmp_path / "run", "workspace_seeded")[-1]
    assert ".[auto]:2 tracked" in row["materialized"], row
    expected = _tracked_bytes(repo)
    assert row["workspace_bytes"] == expected, (
        "the total must be the bytes of the two TRACKED files — the checkpoint was never copied")


def test_the_row_records_the_bytes_a_full_copy_wrote(tmp_path):
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, _task(repo, seed_mode="all"))
    eng._seed_workspace(tmp_path / "run" / "nodes" / "node_0")

    row = _rows(tmp_path / "run", "workspace_seeded")[-1]
    assert ".[all]:copytree" in row["materialized"], row
    expected = _tracked_bytes(repo) + len(_CHECKPOINT)
    assert row["workspace_bytes"] == expected, (
        "a full copy's total must count the untracked checkpoint it copied, and nothing `.git` holds")


def test_a_seam_that_answers_a_plain_count_records_no_total_rather_than_a_guess(tmp_path,
                                                                               monkeypatch):
    """`Engine._seed_repo_tree` is a patch seam that answers an int. An int says nothing about bytes,
    so the row carries NO total — an absent field reads as unknown; a zero would read as empty."""
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, _task(repo))
    monkeypatch.setattr(eng, "_seed_repo_tree", lambda src, dst, ignore, mode="auto": 0)
    eng._seed_workspace(tmp_path / "run" / "nodes" / "node_0")

    row = _rows(tmp_path / "run", "workspace_seeded")[-1]
    assert "workspace_bytes" not in row, row
