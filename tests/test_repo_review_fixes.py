"""Regression tests for the /code-review findings fixed in the RepoTask feature."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

from looplab.command_eval import build_command, run_command_eval
from looplab.config import Settings
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.repo_task import EvalSpec, NoOpRepoDeveloper, RepoTask
from looplab.sandbox import SubprocessSandbox
from looplab.tasks import make_roles

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


# #1 — agent_brief must not crash when eval is None (onboarding task)
def test_agent_brief_handles_none_eval():
    t = RepoTask(id="o", editable_path=str(FIXTURE), onboard=True, eval=None)
    assert isinstance(t.agent_brief(), str)            # no AttributeError


def test_make_roles_onboard_task_does_not_crash():
    s = Settings()
    s.backend, s.developer_backend = "llm", "opencode"
    t = RepoTask(id="o", editable_path=str(FIXTURE), onboard=True, eval=None)
    _, dev = make_roles(t, s)                           # used to crash in agent_brief()
    assert dev is not None


# #2 — a repo task with no eval AND no onboarder must fail loudly, not silently no-op
def test_engine_raises_without_eval_or_onboarder(tmp_path):
    t = RepoTask(id="o", editable_path=str(FIXTURE), onboard=True, eval=None)
    r, d = t.build_roles()
    with pytest.raises(ValueError, match="no eval and no onboarder"):
        Engine(tmp_path / "run", task=t, researcher=r, developer=d,
               sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))


# #3 — cli_overrides param-search must NOT wire the editing agent even with a preset backend
def test_make_roles_param_search_ignores_agent_backend():
    s = Settings()
    s.backend, s.developer_backend, s.unified_agent = "llm", "opencode", False
    t = RepoTask(id="p", direction="max", editable_path=str(FIXTURE), protect=["ttrain_cli.py"],
                 params={"x": (-5.0, 5.0)},
                 eval=EvalSpec(command=[sys.executable, "ttrain_cli.py"],
                               params_style="cli_overrides", metric=_M))
    _, dev = make_roles(t, s)
    assert isinstance(dev, NoOpRepoDeveloper)           # baseline, not a ValidatingDeveloper


# #4 — confirm's requested 'full' profile, when undefined, runs the BASE command (full eval),
#      never silently the cheap 'smoke' overrides
def test_build_command_missing_requested_profile_uses_base():
    es = {"command": ["python", "t.py"], "timeout": 999,
          "profiles": {"smoke": {"overrides": ["s=1"], "timeout": 5}}}
    cmd, t = build_command(es, {}, "full")             # 'full' not defined
    assert cmd == ["python", "t.py"] and t == 999      # base command + base timeout, not s=1/5


# #10 — a configured timeout of 0 is honored (not coerced to the 600 default)
def test_build_command_zero_timeout_honored():
    es = {"command": ["python", "t.py"], "profiles": {"smoke": {"overrides": [], "timeout": 0}}}
    assert build_command(es, {}, "smoke")[1] == 0


# #8 — setup runs at its own cwd (repo root), separate from the eval command's cwd (a subdir)
def test_setup_cwd_separate_from_eval_cwd(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "main.py").write_text(
        'import json, os\n'
        'print(json.dumps({"metric": 1.0 if os.path.exists(os.path.join("..","dep.txt")) else 0.0}))\n',
        encoding="utf-8")
    res = run_command_eval([sys.executable, "main.py"], str(sub), 60, _M,
                           setup=[sys.executable, "-c", "open('dep.txt','w').write('x')"],
                           setup_cwd=str(tmp_path))
    assert res.metric == 1.0                            # setup created dep.txt at root, not in sub


# #5 — the error-feedback repair loop fires for a repo task even when the failing node had
#      empty files (e.g. after a baseline fallback)
def test_repair_fires_for_repo_with_empty_files(tmp_path):
    class _Dev:
        def __init__(self):
            self.last_files: dict = {}
            self.repaired = False

        def implement(self, idea: Idea) -> str:
            self.last_files = {}                        # no edits -> baseline eval fails
            return ""

        def repair(self, idea: Idea, code: str, error: str) -> str:
            self.repaired = True
            self.last_files = {"config.json": json.dumps({"needed_x": 3.0})}
            return ""

    t = RepoTask(id="r", direction="max", editable_path=str(FIXTURE), edit_surface=["*.json"],
                 protect=["ttrain_strict.py"],
                 eval=EvalSpec(command=[sys.executable, "ttrain_strict.py"], metric=_M))
    r, _ = t.build_roles()
    dev = _Dev()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=4))
    anyio.run(eng.run)
    assert dev.repaired                                 # repair fired despite empty parent.files
