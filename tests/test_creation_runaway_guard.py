"""Defense-in-depth against the node-creation runaway (the 184MB `node_created(0)` spin): the engine
loop now trips a creation-level anti-stuck guard when it keeps CREATING nodes while none reaches a
terminal, and `fold` no longer SWALLOWS a resource glitch (MemoryError/RecursionError) into an
empty-nodes state that re-mints id 0 forever.

The guard is TWO bounds, because a create lane can stall in two different ways and reporting one as
the other is what made the Card-speculation defect take a day to find (2026-08-05). It used to charge
`len(creates)` — the PLANNED width — once per loop turn, so a Card lane that elected and requested
work every turn without minting a single node ended the run with "node creation not converging (no
node reached terminal)" when not one node had been created. Now:

  * nodes actually MINTED, counted from the log's `node_created` rows, keep the original message;
  * consecutive turns that PLAN creates and mint nothing get their own bound and their own message.

Both are needed. Charging only mints turns the second stall into an unbounded loop; charging only
planned creates is the misdiagnosis. The counts come from the LOG rather than the folded state
because the spin this file is named for folds to no nodes at all."""
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
    """With `fold` stuck returning EMPTY nodes (the spin condition — a leaked fold / swallowed
    glitch), the loop cannot see its own progress. The guard must FINISH the run (bounded), not hang.

    On today's Card lane this particular shape stalls rather than re-mints: the first build lands one
    node, and every later turn plans a create the lane can no longer reserve. The re-minting shape the
    file is named for is the sibling test below; both must be bounded, and each must be reported as
    what it is.
    """
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

    # …and the reason must be TRUE of what happened. Under an empty fold the first build mints one
    # node, its terminal is invisible to the fold, and every later turn plans a create the Card lane
    # can no longer reserve — so this run stalls WITHOUT creating nodes. Blaming node creation here
    # is what sent the speculation-depth investigation to the wrong subsystem.
    reason = str(stuck[0].data.get("reason", ""))
    assert "without creating a node" in reason, reason
    assert "consecutive loop turns" in reason, reason
    assert created == 1, f"this stall mints once, not repeatedly: {created}"


def test_a_genuine_re_minting_spin_still_reports_node_creation_not_converging(tmp_path, monkeypatch):
    """The 184MB spin itself: `fold` stuck on empty nodes while every turn really does append a
    `node_created`. That IS node creation not converging, it must still be BOUNDED, and it must keep
    the message that describes it. The charge is read off the log precisely so this case survives —
    the folded `nodes` this loop can see stays empty forever."""
    import looplab.engine.orchestrator as orch

    def _empty_fold(evs):
        st = RunState()
        st.run_id, st.direction = "r", "min"
        return st

    monkeypatch.setattr(orch, "fold", _empty_fold)
    eng = Engine(tmp_path / "run", task=ToyTask.load(TASK), researcher=_Stub(), developer=_Dev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=4),
                 auto_install_deps=False, card_driven_selection=False)

    # Stand in for the real spin's build: re-mint the SAME id, append nothing else. (The real one
    # got here through a swallowed resource glitch; the shape is what the guard has to survive.)
    def _remint(action, reserved=None):
        eng.store.append("node_created", {
            "node_id": 0, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}}, "code": "x = 1"})

    monkeypatch.setattr(eng, "_create_node", _remint)
    anyio.run(eng.run)

    raw = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    created = sum(1 for e in raw if e.type == "node_created")
    cap = max(4, 4) * 3 + 50
    assert cap < created <= cap + 4, f"the mint charge is no longer bounded: {created}"
    stuck = [e for e in raw if e.type == "run_finished" and "stuck" in str(e.data.get("reason", ""))]
    assert stuck, "expected a stuck run_finished from the creation-runaway guard"
    assert stuck[0].data["reason"] == (
        "stuck: node creation not converging (no node reached terminal)")


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
