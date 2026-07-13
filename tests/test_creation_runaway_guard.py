"""Defense-in-depth against the node-creation runaway (the 184MB `node_created(0)` spin): the engine
loop now trips a creation-level anti-stuck guard when it keeps CREATING nodes while none reaches a
terminal, and `fold` no longer SWALLOWS a resource glitch (MemoryError/RecursionError) into an
empty-nodes state that re-mints id 0 forever."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.eventstore import EventStore
from looplab.adapters.toytask import ToyTask
from looplab.runtime.sandbox import SubprocessSandbox

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


class _Stub:
    def propose(self, state, parent=None):
        return Idea(operator="draft", params={"x": 1.0}, rationale="seed")


class _Dev:
    def __init__(self):
        self.last_files, self.last_deleted = {}, []

    def implement(self, idea):
        return ""


def test_creation_runaway_guard_terminates(tmp_path, monkeypatch):
    """With `fold` stuck returning EMPTY nodes (the spin condition — a leaked fold / swallowed glitch),
    the loop would re-mint node 0 forever. The guard must FINISH the run (bounded), not hang."""
    import looplab.engine.orchestrator as orch

    fold_calls = 0

    def _empty_fold(evs):
        nonlocal fold_calls
        fold_calls += 1
        # A wall-clock cancel scope cannot interrupt a synchronous no-await busy loop. This explicit
        # deterministic ceiling turns the original infinite regression into an immediate failure.
        assert fold_calls < 500, "creation-runaway guard kept re-folding after its terminal append"
        st = RunState()
        st.run_id, st.direction = "r", "min"      # nodes = {}, finished = False -> the spin state
        return st

    monkeypatch.setattr(orch, "fold", _empty_fold)
    eng = Engine(tmp_path / "run", task=ToyTask.load(TASK), researcher=_Stub(), developer=_Dev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=4),
                 auto_install_deps=False)
    # MUST terminate. The fold-call ceiling above is the test safety net; unlike move_on_after it also
    # bounds regressions that never reach an await checkpoint.
    anyio.run(eng.run)
    assert fold_calls < 500

    raw = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    stuck = [e for e in raw if e.type == "run_finished" and "stuck" in str(e.data.get("reason", ""))]
    assert stuck, "expected a stuck run_finished from the creation-runaway guard"
    created = sum(1 for e in raw if e.type == "node_created")
    assert created <= max(4, 4) * 3 + 50 + 4, f"guard let too many nodes be created: {created}"


def test_fold_reraises_resource_glitch_not_swallow():
    """A MemoryError/RecursionError while building a Node/Idea must PROPAGATE (fail loud), not be
    swallowed into an empty fold — that swallow is what let a transient glitch self-sustain into the
    runaway. A normal data error is still tolerated (skipped)."""
    from looplab.events import replay
    from looplab.core.models import Event

    ev = Event(seq=1, ts=0.0, type="node_created",
               data={"node_id": 0, "operator": "draft", "idea": {"operator": "draft", "params": {}}})

    # a data error (bad idea param type) is still tolerated -> node skipped, fold survives
    bad = Event(seq=2, ts=0.0, type="node_created",
                data={"node_id": 1, "operator": "draft", "idea": {"operator": "draft",
                                                                   "params": {"x": object()}}})
    st = replay.fold([ev, bad])
    assert 0 in st.nodes and 1 not in st.nodes          # good kept, bad skipped (not a crash)

    # a resource glitch inside construction must NOT be swallowed
    real_node = replay.Node
    monkey_calls = {"n": 0}

    def _boom(*a, **k):
        monkey_calls["n"] += 1
        raise MemoryError("simulated pressure")

    replay.Node = _boom
    try:
        with pytest.raises(MemoryError):
            replay.fold([ev])
    finally:
        replay.Node = real_node
    assert monkey_calls["n"] == 1
