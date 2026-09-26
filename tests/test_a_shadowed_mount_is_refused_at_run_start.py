"""A data mount the ROOT repo would shadow is refused at run start, not at the first evaluation.

Measured 2026-09-26 (MiniOneRec inf13): the root repo was copied from a previous run's node
directory and carried that node's `assets` mount symlink. The first node's seed raised
`MountCollision` -- after 27 minutes of deep research, a proposal and a full build, and after the
build had copied the 5.2 GB the link pointed at into the node directory. The collision is a fact
about the task's inputs, so `_setup_phase` now asks the seed's own rule before anything is spent.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine.workspace_seed import (MountCollision, preflight_mount_collision,
                                           root_seed_top_level)
from looplab.events.replay import fold

from factories import make_engine


def _spec(root: Path, data=("assets",)):
    return {"editables": [{"name": ".", "path": str(root)}], "references": [],
            "data": {name: {"path": "/nowhere"} for name in data}}


def _tree(tmp_path: Path, *entries: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    for name in entries:
        (root / name).mkdir()
    return root


def test_the_measured_case_a_copied_mount_link_is_refused(tmp_path):
    root = _tree(tmp_path)
    (tmp_path / "real").mkdir()
    (root / "assets").symlink_to(tmp_path / "real")
    with pytest.raises(MountCollision, match="'assets'"):
        preflight_mount_collision(_spec(root))


def test_a_tree_without_the_name_passes(tmp_path):
    preflight_mount_collision(_spec(_tree(tmp_path, "service")))


def test_no_mounts_means_nothing_to_shadow(tmp_path):
    preflight_mount_collision(_spec(_tree(tmp_path, "assets"), data=()))


def test_the_copy_s_own_ignore_list_applies(tmp_path):
    root = _tree(tmp_path, "__pycache__")
    preflight_mount_collision(_spec(root, data=("__pycache__",)))
    assert "__pycache__" not in root_seed_top_level(root)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


def test_an_untracked_top_level_dir_is_not_a_collision_under_auto(tmp_path):
    """`auto` copies tracked files only, so a gitignored `data/` never reaches the workspace -- the
    false positive the seed-time guard's own comment records for a SOURCE listing."""
    root = _tree(tmp_path, "data")
    (root / "data" / "big.bin").write_bytes(b"x")
    (root / ".gitignore").write_text("data/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "main.py", ".gitignore")
    _git(root, "commit", "-qm", "c")
    preflight_mount_collision(_spec(root, data=("data",)))
    with pytest.raises(MountCollision):
        preflight_mount_collision(_spec(root, data=("data",)), seed_mode="all")


def test_the_engine_refuses_before_run_started(tmp_path):
    root = _tree(tmp_path, "assets")
    (tmp_path / "real").mkdir()
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(root),
                    edit_surface=["*.py"], protect=[], data={"assets": str(tmp_path / "real")},
                    eval=EvalSpec(command=[sys.executable, "main.py"]))
    engine = make_engine(str(tmp_path / "run"), task=task)
    with pytest.raises(MountCollision):
        engine._setup_phase(fold(engine.store.read_all()))
    assert not any(row.type == "run_started" for row in engine.store.read_all())
