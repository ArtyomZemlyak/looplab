"""`eval.parent_workdirs_env`: a node's eval may be told where its parents' workdirs are.

Incident 2026-10-06: "continue node 43 for a second epoch" had no sanctioned way to reach node 43's
final weights — an idea had to carry an absolute path copied by hand, and the operator's runner
already had a fine-tune mode waiting for one. Off by default and byte-identical when off.
"""
from __future__ import annotations

import os
import sys

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.core.models import Idea, Node
from looplab.engine.orchestrator import Engine
from looplab.runtime import command_eval
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_M = {"kind": "stdout_regex", "pattern": r"metric=([0-9.]+)"}


def _engine(tmp_path, *, on: bool):
    src = tmp_path / "src"
    src.mkdir()
    (src / "train.py").write_text("print('metric=1.0')\n")
    task = RepoTask(id="r", direction="max", editable_path=str(src), edit_surface=["*.py"],
                    eval=EvalSpec(command=[sys.executable, "train.py"], metric=_M, cwd=".",
                                  parent_workdirs_env=on))
    researcher, developer = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                  auto_install_deps=False)


def _captured_env(eng, monkeypatch, node, workdir):
    seen: dict = {}
    (workdir / "train.py").write_text("print('metric=1.0')\n")   # the materialized protected script

    def fake(cmd, _cwd, timeout, metric, env=None, **kwargs):
        seen["env"] = env
        return command_eval.RunResult(exit_code=0, stdout="metric=1.0", metric=1.0,
                                      timed_out=False, stderr="")

    monkeypatch.setattr(command_eval, "run_command_eval", fake)
    eng._run_eval(node, str(workdir))
    return seen["env"]


def _child(parents):
    return Node(id=5, operator="improve", parent_ids=list(parents), idea=Idea(operator="improve"))


def test_on_a_child_sees_its_parents_workdirs(tmp_path, monkeypatch):
    eng = _engine(tmp_path, on=True)
    for pid in (3, 4):
        (tmp_path / "run" / "nodes" / f"node_{pid}").mkdir(parents=True)
    wd = tmp_path / "run" / "nodes" / "node_5"
    wd.mkdir(parents=True)
    env = _captured_env(eng, monkeypatch, _child([3, 4, 9]), wd)   # 9 has no workdir: skipped
    want = os.pathsep.join(str((tmp_path / "run" / "nodes" / f"node_{p}").resolve())
                           for p in (3, 4))
    assert env["LOOPLAB_PARENT_WORKDIRS"] == want


def test_off_or_parentless_carries_nothing_new(tmp_path, monkeypatch):
    eng = _engine(tmp_path, on=False)
    (tmp_path / "run" / "nodes" / "node_3").mkdir(parents=True)
    wd = tmp_path / "run" / "nodes" / "node_5"
    wd.mkdir(parents=True)
    env = _captured_env(eng, monkeypatch, _child([3]), wd)
    assert "LOOPLAB_PARENT_WORKDIRS" not in (env or {})
    eng._eval_spec = {**eng._eval_spec, "parent_workdirs_env": True}
    env = _captured_env(eng, monkeypatch, _child([]), wd)
    assert "LOOPLAB_PARENT_WORKDIRS" not in (env or {})


def test_off_keeps_the_eval_spec_dump_byte_identical():
    """The dump feeds the run's setup digests: a field nobody asked for must not move them."""
    off = EvalSpec(command=["python", "x.py"]).model_dump()
    assert "parent_workdirs_env" not in off
    on = EvalSpec(command=["python", "x.py"], parent_workdirs_env=True).model_dump()
    assert on["parent_workdirs_env"] is True


def test_a_declaration_cannot_spoof_the_name():
    """`LOOPLAB_*` is the engine's namespace: an operator's eval_env may not set it."""
    from looplab.core.envsafe import validate_env_map
    clean, reason = validate_env_map("eval_env", {"LOOPLAB_PARENT_WORKDIRS": "/etc"})
    assert clean is None and "LOOPLAB_PARENT_WORKDIRS" in reason
