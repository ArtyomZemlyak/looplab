"""Phase 4 multi-editable workspace: several editable repos mounted at named subdirs, each
with its own surface/protect, edited in one experiment and scored by one eval that may span
them."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from looplab.agents.cli_agent import CliAgentDeveloper
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EditableSpec, EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox

_M = {"kind": "stdout_json", "key": "metric"}


def _two_repos(tmp_path):
    a, b = tmp_path / "ra", tmp_path / "rb"
    a.mkdir(); b.mkdir()
    (b / "val.txt").write_text("4.0", encoding="utf-8")
    # run.py lives in repo A, reads a value from repo B's mount (../b) -> proves both mounted.
    (a / "run.py").write_text(
        'import json, pathlib\n'
        'v = float(pathlib.Path("..","b","val.txt").read_text())\n'
        'print(json.dumps({"metric": v}))\n', encoding="utf-8")
    return a, b


def test_repo_spec_namespaces_surface_and_protect():
    t = RepoTask(id="m", editable_path="/root/repo", edit_surface=["**/*.py"],
                 protect=["secret.py"],
                 editables=[EditableSpec(name="model", path="/x/model",
                                         surface=["src/**/*.py"], protect=["weights.bin"])])
    rs = t.repo_spec()
    assert {e["name"] for e in rs["editables"]} == {".", "model"}
    assert "**/*.py" in rs["edit_surface"]               # root repo: unprefixed
    assert "model/src/**/*.py" in rs["edit_surface"]     # named repo: prefixed
    assert "secret.py" in rs["protected_names"]
    assert "model/weights.bin" in rs["protected_names"]


def test_file_metric_protection_namespaced_under_eval_cwd():
    # The eval reads metrics.json relative to cwd "model/"; the protected name must be the
    # workspace-relative path the agent would write, else the metric file isn't actually guarded.
    t = RepoTask(id="m", editable_path="/x",
                 eval=EvalSpec(command=["python", "t.py"], cwd="model",
                               metric={"kind": "file_json", "path": "metrics.json",
                                       "key": "metric"}))
    assert "model/metrics.json" in t.repo_spec()["protected_names"]
    # cwd="." stays unprefixed (single-repo at root)
    t2 = RepoTask(id="m", editable_path="/x",
                  eval=EvalSpec(command=["python", "t.py"],
                                metric={"kind": "file_json", "path": "metrics.json"}))
    assert "metrics.json" in t2.repo_spec()["protected_names"]


def test_requires_at_least_one_editable():
    with pytest.raises(ValueError, match="needs an editable source"):
        RepoTask(id="m")


def test_editable_names_must_be_simple_and_distinct():
    with pytest.raises(ValueError, match="simple subdir"):
        RepoTask(id="m", editables=[EditableSpec(name="a/b", path="/x")])
    with pytest.raises(ValueError, match="duplicate"):
        RepoTask(id="m", editables=[EditableSpec(name="a", path="/x"),
                                    EditableSpec(name="a", path="/y")])


def test_cli_agent_seed_dir_shorthand_normalizes():
    d = CliAgentDeveloper(model="m", seed_dir="/some/repo")
    assert d.seed_dirs == [{"name": ".", "path": "/some/repo"}]


def test_seed_workspace_mounts_each_repo_at_its_subdir(tmp_path):
    a, b = _two_repos(tmp_path)
    t = RepoTask(id="m", direction="max",
                 editables=[EditableSpec(name="a", path=str(a), surface=["**/*.py"]),
                            EditableSpec(name="b", path=str(b), surface=["**/*.txt"])],
                 eval=EvalSpec(command=[sys.executable, "run.py"], cwd="a", metric=_M))
    eng = Engine(tmp_path / "run", task=t, researcher=t.build_roles()[0],
                 developer=t.build_roles()[1], sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1))
    wd = tmp_path / "ws"
    eng._seed_workspace(wd)
    assert (wd / "a" / "run.py").is_file()
    assert (wd / "b" / "val.txt").is_file()


def test_seed_workspace_rejects_a_mount_that_shadows_a_root_repo_entry(tmp_path):
    # Regression: the root repo is seeded at the workspace root FIRST, so a data/reference mount whose
    # name matches a top-level repo entry (the repo ships a `data/` dir and the operator mounts a real
    # dataset named `data`) had its dst already occupied — link_input silently skipped it, the eval read
    # the repo's placeholder instead of the declared source, and WORKSPACE_SEEDED falsely reported the
    # mount succeeded. The collision must fail loud instead of silently invalidating the run.
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "data").mkdir()                                    # the top-level entry the mount name shadows
    (repo / "data" / "placeholder.txt").write_text("REPO PLACEHOLDER", encoding="utf-8")
    real = tmp_path / "real_data"; real.mkdir()
    (real / "x.csv").write_text("1,2,3\n", encoding="utf-8")
    t = RepoTask(id="r", direction="max", editable_path=str(repo), data={"data": str(real)},
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    eng = Engine(tmp_path / "run", task=t, researcher=t.build_roles()[0],
                 developer=t.build_roles()[1], sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1))
    with pytest.raises(RuntimeError, match="collides with a top-level entry of the root repo"):
        eng._seed_workspace(tmp_path / "ws")


def test_seed_workspace_allows_a_non_colliding_mount(tmp_path):
    # A mount whose name does NOT match a root-repo top-level entry mounts normally (the real source,
    # not a repo placeholder).
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text("print('hi')\n", encoding="utf-8")
    real = tmp_path / "real_data"; real.mkdir()
    (real / "x.csv").write_text("1,2,3\n", encoding="utf-8")
    t = RepoTask(id="r", direction="max", editable_path=str(repo), data={"dataset": str(real)},
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    eng = Engine(tmp_path / "run", task=t, researcher=t.build_roles()[0],
                 developer=t.build_roles()[1], sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1))
    wd = tmp_path / "ws"
    eng._seed_workspace(wd)
    assert (wd / "run.py").is_file()
    assert (wd / "dataset").is_symlink() or (wd / "dataset" / "x.csv").is_file()   # real source mounted


def test_engine_end_to_end_eval_spans_two_repos(tmp_path):
    a, b = _two_repos(tmp_path)
    t = RepoTask(id="m", direction="max",
                 editables=[EditableSpec(name="a", path=str(a)),
                            EditableSpec(name="b", path=str(b))],
                 eval=EvalSpec(command=[sys.executable, "run.py"], cwd="a", metric=_M))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    state = anyio.run(eng.run)
    assert state.best() is not None and state.best().metric == 4.0   # read repo B via mount
