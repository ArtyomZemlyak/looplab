"""RepoTask (kind="repo", ADR-7): command-based eval with pluggable metric readers,
workspace mount of an existing repo, eval-file protection, and an end-to-end engine run
where an agent edits the repo and the OPERATOR's command/metric scores it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

from looplab.runtime.command_eval import read_metric, run_command_eval
from looplab.core.models import Idea, Node
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EditableSpec, EvalSpec, ReferenceSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import TaskAdapter, load_task

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


def _eng(tmp_path, task, **kw):
    r, d = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)


# --------------------------- metric readers (unit) ---------------------------

def test_editable_path_expands_user_and_env(monkeypatch):
    """A `~`/`$VAR` editable_path must be expanded — otherwise the repo mounts + the Researcher's
    repo_* scout tools resolve a literal `~` dir and come up EMPTY (the live-run symptom)."""
    import os
    norm = os.path.normpath                      # expanduser keeps the trailing "/" — compare normalized
    home = os.path.expanduser("~")
    t = RepoTask(goal="g", editable_path="~/myrepo")
    assert "~" not in t.editable_path and norm(t.editable_path) == norm(os.path.join(home, "myrepo"))
    assert norm(t._editable_mounts()[0]["path"]) == norm(os.path.join(home, "myrepo"))  # mounts carry it
    monkeypatch.setenv("MYREPOROOT", home)
    t2 = RepoTask(goal="g", editable_path="$MYREPOROOT/proj",
                  references=[{"name": "ref", "path": "~/ref"}], data={"d": "~/data.csv"})
    assert norm(t2.editable_path) == norm(os.path.join(home, "proj"))
    assert norm(t2.references[0].path) == norm(os.path.join(home, "ref"))
    assert norm(t2.data["d"]) == norm(os.path.join(home, "data.csv"))


def test_read_metric_stdout_and_regex():
    assert read_metric('noise\n{"metric": 0.5}\n', ".", {"kind": "stdout_json", "key": "metric"}) == 0.5
    assert read_metric("acc=0.91 then acc=0.93", ".",
                       {"kind": "stdout_regex", "pattern": r"acc=([0-9.]+)", "group": 1}) == 0.93
    assert read_metric("nothing here", ".", {"kind": "stdout_json", "key": "metric"}) is None


def test_read_metric_from_file(tmp_path):
    (tmp_path / "metrics.json").write_text('{"val": {"acc": 0.88}}', encoding="utf-8")
    assert read_metric("", str(tmp_path),
                       {"kind": "file_json", "path": "metrics.json", "key": "val.acc"}) == 0.88
    assert read_metric("", str(tmp_path),
                       {"kind": "file_json", "path": "missing.json", "key": "x"}) is None


def test_run_command_eval_over_fixture(tmp_path):
    # Copy the fixture and run its eval command; baseline x=0 -> metric -9.
    import shutil
    wd = tmp_path / "wd"
    shutil.copytree(FIXTURE, wd)
    res = run_command_eval([sys.executable, "ttrain.py"], str(wd), 60,
                           {"kind": "stdout_json", "key": "metric"})
    assert res.exit_code == 0 and res.metric == -9.0
    assert (wd / "metrics.json").exists()                      # framework wrote its file


# ------------------------------ RepoTask shape -------------------------------

def _task(**kw) -> RepoTask:
    kw.setdefault("eval", EvalSpec(command=[sys.executable, "ttrain.py"],
                                   metric={"kind": "stdout_json", "key": "metric"}))
    return RepoTask(id="fix", goal="maximize metric", direction="max",
                    editable_path=str(FIXTURE), edit_surface=["*.json"],
                    protect=["ttrain.py"], **kw)


def test_repo_task_conforms_and_specs():
    t = _task()
    assert isinstance(t, TaskAdapter) and t.direction == "max"
    rs = t.repo_spec()
    assert rs["editable_path"] == str(FIXTURE) and rs["edit_surface"] == ["*.json"]
    assert "ttrain.py" in rs["protected_names"]                # eval entrypoint protected
    assert t.eval_spec()["command"][-1] == "ttrain.py"
    assert "ttrain.py" in t.agent_brief() and "maximize" in t.agent_brief()


def test_protected_names_includes_metric_file():
    t = _task(eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                            metric={"kind": "file_json", "path": "metrics.json", "key": "metric"}))
    assert "metrics.json" in t.repo_spec()["protected_names"]  # can't fake the metrics file


# --------------------------- engine end-to-end -------------------------------

class _EditConfigDev:
    """Stub agent: edits config.json toward the optimum (x=3) AND maliciously tries to
    overwrite the eval entrypoint — which must be ignored (protected)."""
    def __init__(self):
        self.last_files: dict[str, str] = {}

    def implement(self, idea: Idea) -> str:
        self.last_files = {
            "config.json": json.dumps({"x": 3.0}),
            "ttrain.py": "raise SystemExit('cheat: agent overwrote the eval')\n",
        }
        return ""


def test_engine_runs_repo_command_eval_and_protects_eval(tmp_path):
    t = _task()
    researcher, _ = t.build_roles()
    engine = Engine(tmp_path / "run", task=t, researcher=researcher,
                    developer=_EditConfigDev(), sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=2, max_nodes=3))
    state = anyio.run(engine.run)
    assert state.finished
    best = state.best()
    # config.json edit applied (x=3 -> metric 0) AND ttrain.py protected (cheat ignored,
    # so the real eval ran and the metric is the true 0.0, not a crash/fake).
    assert best is not None and best.metric == 0.0
    nd0 = tmp_path / "run" / "nodes" / "node_0"
    assert (nd0 / "metrics.json").exists()                     # framework eval actually ran
    assert "cheat" not in (nd0 / "ttrain.py").read_text(encoding="utf-8")  # eval not overwritten


def test_make_roles_wires_repo_agent():
    from looplab.agents.cli_agent import CliAgentDeveloper
    from looplab.core.config import Settings
    from looplab.adapters.repo_task import NoOpRepoDeveloper
    from looplab.agents.roles import ValidatingDeveloper
    from looplab.adapters.tasks import make_roles
    s = Settings()
    s.backend, s.developer_backend, s.unified_agent = "llm", "opencode", False
    # monkeypatch the kind dispatch by passing the task directly
    _, dev = make_roles(_task(), s)
    assert isinstance(dev, ValidatingDeveloper) and dev.repo_mode is True
    inner = dev.inner
    assert isinstance(inner, CliAgentDeveloper)
    assert inner.seed_dirs == [{"name": ".", "path": str(FIXTURE), "surface": ["*.json"],
                                "protect": ["ttrain.py"], "seed_mode": ""}]
    assert inner.surface == ["*.json"]
    assert inner.patch_gate is True
    assert isinstance(dev.fallback, NoOpRepoDeveloper)        # baseline fallback, not LLM


def test_engine_baseline_when_no_edits(tmp_path):
    # NoOp developer (offline fallback) -> repo unmodified -> baseline metric -9 everywhere.
    t = _task()
    researcher, developer = t.build_roles()                    # RepoResearcher + NoOp
    engine = Engine(tmp_path / "run", task=t, researcher=researcher,
                    developer=developer, sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=2, max_nodes=2))
    state = anyio.run(engine.run)
    assert state.finished and state.best().metric == -9.0


# ------------------------------ live (opt-in) --------------------------------

def _opencode_ready():
    import os
    import urllib.request
    oc = Path(os.environ.get("APPDATA", "")) / "npm" / "opencode.cmd"
    if os.environ.get("LOOPLAB_TEST_OPENCODE") != "1" or not oc.exists():
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            return any("qwen3:8b" in m.get("name", "")
                       for m in json.loads(r.read()).get("models", []))
    except Exception:
        return False


import pytest  # noqa: E402


@pytest.mark.skipif(not _opencode_ready(),
                    reason="set LOOPLAB_TEST_OPENCODE=1 with a working opencode + Ollama")
def test_live_opencode_edits_repo_end_to_end(tmp_path):
    # Full live path: opencode edits config.json (in-surface) of the seeded repo; the
    # operator's command/metric scores it; ttrain.py stays protected.
    from looplab.core.config import Settings
    from looplab.adapters.tasks import make_roles
    s = Settings()
    s.backend, s.developer_backend, s.llm_model = "llm", "opencode", "qwen3:8b"
    t = _task()
    researcher, developer = make_roles(t, s)
    engine = Engine(tmp_path / "run", task=t, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                    timeout=120.0)
    state = anyio.run(engine.run)
    assert state.finished and state.best() is not None
    assert state.best().metric is not None                     # the command-eval produced a metric
    assert (tmp_path / "run" / "nodes" / "node_0" / "metrics.json").exists()


# --- eval-file / protected-name hardening (deep audit + review rounds) ----------------------------

# A1 — an explicit (non-onboarded) adapter metric is protected from agent edits
def test_adapter_metric_protected_explicit_eval():
    t = RepoTask(id="a", editable_path="/x", edit_surface=["*.py"],
                 eval=EvalSpec(command=["python", "t.py"],
                               metric={"kind": "adapter", "path": "LOOPLAB_adapter.py"}))
    assert "LOOPLAB_adapter.py" in t.repo_spec()["protected_names"]


# A2 — protected check is case-insensitive (Windows/NTFS + fnmatch)
def test_write_node_files_case_insensitive_protect(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "ttrain.py").write_text("real", encoding="utf-8")
    t = RepoTask(id="p", editable_path=str(repo), edit_surface=["*.py"], protect=["ttrain.py"],
                 eval=EvalSpec(command=[sys.executable, "ttrain.py"], metric=_M))
    eng = _eng(tmp_path, t)
    wd = tmp_path / "wd"; wd.mkdir()
    (wd / "ttrain.py").write_text("real", encoding="utf-8")
    node = Node(id=0, operator="draft", idea=Idea(operator="draft"),
                files={"Ttrain.PY": "raise SystemExit('cheat')"})
    eng._write_node_files(node, wd)
    # case-variant name must NOT overwrite the protected file
    assert (wd / "ttrain.py").read_text(encoding="utf-8") == "real"


# A5 — reference/data mount names are validated (used as wd/name)
def test_reference_data_names_validated():
    with pytest.raises(ValueError, match="simple subdir"):
        RepoTask(id="r", editable_path="/x", references=[ReferenceSpec(name="../etc", path="/y")])
    with pytest.raises(ValueError, match="collision"):
        RepoTask(id="r", editable_path="/x", editables=[EditableSpec(name="dup", path="/m")],
                 data={"dup": "/d"})


# E — an accepted in-surface deletion is applied to the eval workdir
def test_deletion_applied_in_write_node_files(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "keep.py").write_text("k", encoding="utf-8")
    t = RepoTask(id="d", editable_path=str(repo), edit_surface=["*.py"],
                 eval=EvalSpec(command=[sys.executable, "keep.py"], metric=_M))
    eng = _eng(tmp_path, t)
    wd = tmp_path / "wd"; wd.mkdir()
    (wd / "old.py").write_text("dead", encoding="utf-8")
    node = Node(id=0, operator="draft", idea=Idea(operator="draft"), deleted=["old.py"])
    eng._write_node_files(node, wd)
    assert not (wd / "old.py").exists()                            # accepted deletion took effect


# #33 — protected names are normalized (./ , backslash) to match git-diff paths
def test_protected_names_normalized():
    t = RepoTask(id="n", editable_path="/x", edit_surface=["*.py"], protect=["./secret.py"],
                 eval=EvalSpec(command=["python", "t.py"],
                               metric={"kind": "file_json", "path": "./metrics.json"}))
    pn = t.repo_spec()["protected_names"]
    assert "secret.py" in pn and "metrics.json" in pn


# #80 — RepoTask direction is validated (a typo can't silently flip the objective)
def test_repo_task_direction_validated():
    with pytest.raises(ValueError, match="direction must be"):
        RepoTask(id="d", editable_path="/x", direction="maximize",
                 eval=EvalSpec(command=["python", "t.py"]))


def test_repo_eval_protects_every_reader_path(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text("print('{\"metric\": 1.0}')\n", encoding="utf-8")
    t = RepoTask(
        id="p", direction="max", editable_path=str(repo), edit_surface=["*.py"],
        eval=EvalSpec(
            command=[sys.executable, "run.py"],
            metric={"kind": "file_json", "path": "out/metric.json", "key": "metric"},
            metrics={"aux": {"kind": "file_json", "path": "out/aux.json", "key": "a"}},
            constraints=[{"kind": "file_json", "path": "out/lat.json", "key": "lat",
                          "max": 100, "name": "lat"}],
            cross_check={"kind": "file_json", "path": "out/cross.json", "key": "metric"}))
    protected = set(t._protected_names())
    # every file-based reader (primary + aux metric + constraint + cross-check) is now forge-proof
    for p in ("out/metric.json", "out/aux.json", "out/lat.json", "out/cross.json"):
        assert p in protected, f"{p} not protected: {protected}"
