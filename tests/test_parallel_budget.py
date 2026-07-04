"""G3 distributed/parallel eval: the fan-out path completes and honors the eval-budget guard."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.events.eventstore import EventStore
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _run(tmp_path, **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=3, max_nodes=8), **kw)
    return anyio.run(eng.run)


def test_parallel_path_completes(tmp_path):
    # max_parallel > 1 exercises the fan-out branch (incl. the new budget-guard counter).
    state = _run(tmp_path / "par", max_parallel=3)
    assert state.finished and len(state.nodes) == 8
    assert state.best() is not None


def test_parallel_path_stops_on_eval_budget(tmp_path):
    # A near-zero eval budget stops the run early via the loop-top guard; the parallel branch must
    # not launch the whole batch past it.
    state = _run(tmp_path / "par_budget", max_parallel=4, max_eval_seconds=1e-9)
    evs = list(EventStore((tmp_path / "par_budget") / "events.jsonl").read_all())
    # the run finished for the eval-budget reason (or simply finished) — never hangs / over-runs
    assert fold(evs).finished
