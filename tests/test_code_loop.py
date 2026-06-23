"""I2/I7: LLM-as-Developer coding loop — asset materialization + error-feedback debug.
Offline, using a fake LLM client (no model needed)."""
from __future__ import annotations

import json
from pathlib import Path

import anyio

from autornd.models import Idea, RunState
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.regression import CodeRegressionTask
from autornd.replay import fold
from autornd.roles import LLMDeveloper
from autornd.sandbox import SubprocessSandbox


# --- code extraction (fences + <think>) ---
class _FenceClient:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete_text(self, messages):
        return self.replies.pop(0)


def test_llm_developer_extracts_code_and_repairs():
    dev = LLMDeveloper(_FenceClient([
        "<think>plan</think>\nSure:\n```python\nprint('{\"metric\": 1.0}')\n```\nDone.",
        "```py\nprint('{\"metric\": 0.5}')\n```",
    ]), brief="contract")
    code1 = dev.implement(Idea(operator="draft", params={"degree": 2.0}))
    assert code1 == 'print(\'{"metric": 1.0}\')'
    code2 = dev.repair(Idea(operator="debug", params={}), code1, "Traceback: boom")
    assert code2 == 'print(\'{"metric": 0.5}\')'


# --- engine: assets are materialized into the node workdir ---
class _StubResearcher:
    def propose(self, state: RunState, parent):
        return Idea(operator="x", params={"degree": 2.0, "lam": 0.0}, rationale="")


class _AssetReadingDeveloper:
    """Writes code that reads the materialized data.json and prints its length as metric."""
    def implement(self, idea: Idea) -> str:
        return ("import json\n"
                "d = json.load(open('data.json'))\n"
                "print(json.dumps({'metric': float(len(d['x']))}))\n")


def test_assets_are_written_to_workdir(tmp_path):
    task = CodeRegressionTask(seed=1, n=40)
    eng = Engine(tmp_path / "run", task=task, researcher=_StubResearcher(),
                 developer=_AssetReadingDeveloper(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1))
    state = anyio.run(eng.run)
    best = state.best()
    assert best is not None and best.metric == 40.0  # code read the 40-row data.json
    assert (tmp_path / "run" / "nodes" / "node_0" / "data.json").exists()


# --- engine: error-feedback debug repairs a failing solution ---
class _BrokenThenFixedDeveloper:
    """First implementation crashes; repair() returns a working script."""
    def __init__(self):
        self.calls = 0

    def implement(self, idea: Idea) -> str:
        self.calls += 1
        return "raise RuntimeError('boom')\n"

    def repair(self, idea: Idea, code: str, error: str) -> str:
        assert "boom" in error  # got the real stderr back
        return "import json\nprint(json.dumps({'metric': 0.123}))\n"


def test_error_feedback_debug_repairs(tmp_path):
    task = CodeRegressionTask(seed=1, n=10)
    eng = Engine(tmp_path / "run", task=task, researcher=_StubResearcher(),
                 developer=_BrokenThenFixedDeveloper(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=4, debug_depth=1))
    state = anyio.run(eng.run)
    # The draft failed; a debug child repaired it and evaluated successfully.
    debug_nodes = [n for n in state.nodes.values() if n.operator == "debug"]
    assert debug_nodes
    fixed = [n for n in debug_nodes if n.metric == 0.123]
    assert fixed, "error-feedback debug should have produced a working solution"
    # That repaired node should be (or tie for) the best.
    assert state.best() is not None and state.best().metric == 0.123


def test_code_regression_loader_and_assets():
    from autornd.tasks import load_task
    t = load_task(Path(__file__).resolve().parents[1] / "examples" / "code_regression_task.json")
    assert t.kind == "code_regression"
    assets = t.assets()
    data = json.loads(assets["data.json"])
    assert len(data["x"]) == 40 and len(data["y"]) == 40
