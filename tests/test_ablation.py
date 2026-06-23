"""I7: ablation-driven refinement — probe each param's impact, refine the top one."""
from __future__ import annotations

from pathlib import Path

import anyio

from autornd.eventstore import EventStore
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.sandbox import SubprocessSandbox
from autornd.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _engine(rd, ablate_every):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=3, max_nodes=12, ablate_every=ablate_every,
                                    enable_merge=False))


def test_ablation_produces_refine_block_and_impacts(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", ablate_every=1).run)
    assert state.finished

    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    ablate_events = [e for e in events if e.type == "ablate"]
    assert ablate_events, "expected at least one ablation pass"
    # Impacts were measured for both params of the toy objective.
    imp = ablate_events[0].data["impacts"]
    assert set(imp) == {"x", "y"} and all(v >= 0 for v in imp.values())

    # A refine_block node exists and is a single-parent child.
    refines = [n for n in state.nodes.values() if n.operator == "refine_block"]
    assert refines and all(len(n.parent_ids) == 1 for n in refines)


def test_ablation_off_by_default(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", ablate_every=0).run)
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert not any(e.type == "ablate" for e in events)
    assert not any(n.operator == "refine_block" for n in state.nodes.values())
