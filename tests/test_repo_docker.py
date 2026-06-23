"""Phase 4 untrusted tier: command-eval sandboxed via `docker run`. Docker isn't required
to run these — we test the argv construction (monkeypatching the docker-CLI probe) and that
run_command_eval routes the wrap to setup (root) and eval (cwd subdir) correctly."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from looplab import command_eval
from looplab.command_eval import make_docker_wrap, run_command_eval

_M = {"kind": "stdout_json", "key": "metric"}


def test_make_docker_wrap_raises_without_docker(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _x: None)
    with pytest.raises(RuntimeError, match="needs the docker CLI"):
        make_docker_wrap("/work/root", "python:3.12-slim")


def test_docker_wrap_builds_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda _x: "/usr/bin/docker")
    root = tmp_path
    wrap = make_docker_wrap(str(root), "img:1", network="none", mem="2g")
    # eval cwd == a subdir -> -w /work/<rel>
    argv = wrap(["python", "train.py"], str(root / "exp"))
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--memory" in argv and argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("-w") + 1] == "/work/exp"
    assert argv[-3:] == ["img:1", "python", "train.py"]
    mount = argv[argv.index("-v") + 1]
    assert mount.endswith(":/work")
    # root cwd -> -w /work (no trailing slash)
    root_argv = wrap(["x"], str(root))
    assert root_argv[root_argv.index("-w") + 1] == "/work"


def test_run_command_eval_applies_wrap_to_setup_and_eval(tmp_path):
    sub = tmp_path / "exp"
    sub.mkdir()
    calls: list[tuple] = []

    def wrap(argv, host_cwd):
        calls.append((list(argv), host_cwd))
        return argv                                  # passthrough -> still runs locally

    res = run_command_eval(
        [sys.executable, "-c", "import json;print(json.dumps({'metric': 3.0}))"],
        str(sub), 60, _M,
        setup=[sys.executable, "-c", "open('dep.txt','w').write('x')"],
        setup_cwd=str(tmp_path), wrap=wrap)
    assert res.metric == 3.0
    assert len(calls) == 2
    # setup wrapped at the root cwd; eval wrapped at the subdir cwd
    assert Path(calls[0][1]) == tmp_path
    assert Path(calls[1][1]) == sub


def test_adapter_metric_is_wrapped_under_untrusted(tmp_path):
    """#1a: the agent-authored `adapter` reader EXECS code, so under the untrusted tier it
    must run through the sandbox wrap (in-container), not directly on the host."""
    from looplab.command_eval import read_metric
    (tmp_path / "LOOPLAB_adapter.py").write_text(
        "def read_metric(workdir):\n    return 2.5\n", encoding="utf-8")
    seen = {}

    def wrap(argv, host_cwd):
        seen["argv"] = list(argv)
        # The wrap targets the container's `python`; rewrite to the host interpreter so the
        # test can actually execute it locally and confirm the value still flows through.
        return [sys.executable] + argv[1:]

    val = read_metric("", str(tmp_path), {"kind": "adapter", "path": "LOOPLAB_adapter.py"},
                      wrap=wrap)
    assert val == 2.5
    assert seen["argv"][0] == "python"               # container python, NOT host sys.executable


def test_adapter_metric_runs_on_host_without_wrap(tmp_path):
    # Sanity: trusted_local path (no wrap) execs with the host interpreter, in-process harness.
    from looplab.command_eval import read_metric
    (tmp_path / "LOOPLAB_adapter.py").write_text(
        "def read_metric(workdir):\n    return -1.0\n", encoding="utf-8")
    assert read_metric("", str(tmp_path),
                       {"kind": "adapter", "path": "LOOPLAB_adapter.py"}) == -1.0


def test_engine_untrusted_builds_docker_wrap(monkeypatch, tmp_path):
    """The engine's command-eval path requests a docker wrap under trust_mode='untrusted'."""
    monkeypatch.setattr(shutil, "which", lambda _x: "/usr/bin/docker")
    made = {}

    def fake_wrap_factory(root, image, **kw):
        made["root"], made["image"] = root, image
        return lambda argv, hc: ["echo"] + list(argv)   # harmless, won't emit a metric

    monkeypatch.setattr(command_eval, "make_docker_wrap", fake_wrap_factory)

    import sys as _s
    from looplab.orchestrator import Engine
    from looplab.policy import GreedyTree
    from looplab.repo_task import EvalSpec, RepoTask
    from looplab.sandbox import SubprocessSandbox

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text("print('{\"metric\": 1.0}')", encoding="utf-8")
    t = RepoTask(id="u", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[_s.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    import anyio
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 trust_mode="untrusted", docker_image="img:2")
    anyio.run(eng.run)
    assert made["image"] == "img:2"
    assert Path(made["root"]).name.startswith("node_")
    assert Path(made["root"]).parent == (tmp_path / "run" / "nodes").resolve()
