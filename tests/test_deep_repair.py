"""C3 deep test-driven repair: structured failure context handed to repair."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


class _BrokenThenFixed:
    """Fails first; records the error text passed to repair so we can assert C3 enrichment."""
    def __init__(self):
        self.repair_errors = []

    def implement(self, idea):
        return "raise RuntimeError('boom')\n"

    def repair(self, idea, code, error):
        self.repair_errors.append(error)
        return "import json; print(json.dumps({'metric': 0.1}))\n"


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="x", params={"x": 1.0, "y": 1.0})


def _run(tmp_path, **kw):
    dev = _BrokenThenFixed()
    eng = Engine(tmp_path, task=ToyTask.load(TASK), researcher=_Stub(), developer=dev,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=4, debug_depth=1), **kw)
    anyio.run(eng.run)
    return dev


def test_deep_repair_enriches_error(tmp_path):
    dev = _run(tmp_path / "deep", deep_repair=True)
    assert dev.repair_errors, "repair should have been called"
    assert any("failure kind" in e and "reproduction" in e for e in dev.repair_errors)


def test_default_repair_passes_raw_error(tmp_path):
    dev = _run(tmp_path / "plain")
    assert dev.repair_errors
    assert all("failure kind" not in e for e in dev.repair_errors)
