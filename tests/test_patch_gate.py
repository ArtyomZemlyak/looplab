"""Patch-gated multi-file external agent (ADR-7 Rule 3). A stub agent edits files in the
developer's git worktree; the surface gate accepts in-surface multi-file changes and
rejects (reverts) any out-of-surface touch. Plus an end-to-end check that a multi-file
solution's helper modules are materialized into the eval workdir."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from looplab.cli_agent import PRESETS, CliAgentDeveloper
from looplab.models import Idea, Node
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
_HAS_GIT = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not _HAS_GIT, reason="git not available")


def _stub(tmp_path, name, body) -> list[str]:
    s = tmp_path / name
    s.write_text("import pathlib\n" + body, encoding="utf-8")
    return [sys.executable, str(s)]


# solution.py imports a helper module the agent also writes (multi-file solution).
_MULTI = (
    'pathlib.Path("solution.py").write_text("import helper, json\\n'
    'print(json.dumps({\\"metric\\": helper.value()}))\\n")\n'
    'pathlib.Path("helper.py").write_text("def value():\\n    return 0.25\\n")\n'
)
# Edits solution.py (in-surface) BUT also drops a non-.py file (out-of-surface).
_EVIL = (
    'pathlib.Path("solution.py").write_text("import json\\n'
    'print(json.dumps({\\"metric\\": 0.5}))\\n")\n'
    'pathlib.Path("notes.txt").write_text("sneaky\\n")\n'
)


def test_patch_gate_accepts_multifile(tmp_path):
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path, "m.py", _MULTI),
                            patch_gate=True, surface=["*.py"])
    code = dev.implement(Idea(operator="draft", params={}))
    assert dev.last_patch and dev.last_patch["ok"]
    assert set(dev.last_files) == {"solution.py", "helper.py"}
    assert "import helper" in code                      # returns the solution.py entrypoint


def test_patch_gate_rejects_out_of_surface(tmp_path):
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path, "e.py", _EVIL),
                            patch_gate=True, surface=["*.py"])
    code = dev.implement(Idea(operator="draft", params={}))
    # reject-not-strip: a single out-of-surface path rejects the WHOLE patch -> seed.
    assert dev.last_patch and not dev.last_patch["ok"]
    assert "notes.txt" in dev.last_patch["rejected"]
    assert "TODO" in code and dev.last_files == {}      # reverted to the seed


def test_patch_gate_off_is_whole_file_readback(tmp_path):
    # Default (no patch_gate): no git, single-file readback, no patch metadata.
    dev = CliAgentDeveloper(model="ollama/x", spec=PRESETS["opencode"],
                            cmd_override=_stub(tmp_path, "m.py", _MULTI))
    code = dev.implement(Idea(operator="draft", params={}))
    assert dev.last_patch is None and dev.last_files == {}
    assert "import helper" in code                      # solution.py still read back


def test_engine_materializes_multifile_solution(tmp_path):
    """End-to-end: a multi-file solution's helper module is written into the eval workdir
    so the sandbox can import it; the node carries `files` (files-as-truth, resumable)."""
    class _MultiFileDev:
        def __init__(self):
            self.last_files: dict[str, str] = {}

        def implement(self, idea: Idea) -> str:
            code = 'import lib, json\nprint(json.dumps({"metric": lib.value()}))\n'
            self.last_files = {"solution.py": code, "lib.py": "def value():\n    return 0.25\n"}
            return code

    task = ToyTask.load(ROOT / "examples" / "toy_task.json")
    researcher, _ = task.build_roles()
    engine = Engine(tmp_path / "run", task=task, researcher=researcher,
                    developer=_MultiFileDev(), sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=2, max_nodes=3))
    state = anyio.run(engine.run)
    assert state.finished
    # Every node carries the helper file and evaluated to the helper-provided metric.
    assert all("lib.py" in n.files for n in state.nodes.values())
    evaluated = state.evaluated_nodes()
    assert evaluated and all(abs(n.metric - 0.25) < 1e-9 for n in evaluated)
    # The helper file was actually written next to solution.py in the eval workdir.
    assert (tmp_path / "run" / "nodes" / "node_0" / "lib.py").exists()


def test_write_node_files_skips_solution_assets_and_escapes(tmp_path):
    # Unit: _write_node_files materializes helpers, skips solution.py, REFUSES to
    # overwrite a task-asset name (e.g. the private grader.py), and blocks escapes.
    engine = Engine(tmp_path / "run", task=ToyTask.load(ROOT / "examples" / "toy_task.json"),
                    researcher=None, developer=None, sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=1, max_nodes=1))
    engine._assets = {"grader.py": "REAL_GRADER"}        # task-owned asset name
    node = Node(id=0, operator="draft", idea=Idea(operator="draft"), code="print(1)",
                files={"solution.py": "x", "pkg/lib.py": "y", "../escape.py": "z",
                       "grader.py": "FAKE_GRADER"})
    wd = tmp_path / "wd"
    engine._write_node_files(node, wd)
    assert (wd / "pkg" / "lib.py").read_text() == "y"    # helper written
    assert not (wd / "solution.py").exists()             # sandbox writes this from code
    assert not (wd / "grader.py").exists()               # asset name protected (not overwritten)
    assert not (tmp_path / "escape.py").exists()         # traversal blocked


def test_engine_protects_grader_asset_from_agent_overwrite(tmp_path):
    """Integrity: an agent that ships its own grader.py (in-surface *.py) must NOT be able
    to replace the task's private grader. Assets are written last and win."""
    from looplab.mlebench import MLEBenchTask

    class _CheatDev:
        def __init__(self):
            self.last_files = {}

        def implement(self, idea: Idea) -> str:
            # solution imports grader; agent also tries to ship a grader scoring 1.0
            code = "import grader, json\nprint(json.dumps({'metric': grader.score([0])}))\n"
            self.last_files = {"solution.py": code,
                               "grader.py": "def score(p):\n    return 1.0\n"}
            return code

    task = MLEBenchTask(n_train=20, n_test=10)
    researcher, _ = task.build_roles()
    engine = Engine(tmp_path / "run", task=task, researcher=researcher,
                    developer=_CheatDev(), sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=2, max_nodes=2))
    state = anyio.run(engine.run)
    # The fake grader (return 1.0) was rejected; the real grader scored the length-1 cheat
    # submission as malformed (wrong length vs the 10-element held-out key) -> exactly 0.0, never 1.0.
    for n in state.evaluated_nodes():
        assert n.metric == 0.0
