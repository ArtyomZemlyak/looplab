"""I22 diversity archive + I21 HITL approval (offline)."""
from __future__ import annotations

from pathlib import Path

import anyio

from autornd.archive import DiversityArchive
from autornd.eventstore import EventStore
from autornd.models import Idea, Node, NodeStatus, RunState
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.replay import fold
from autornd.sandbox import SubprocessSandbox
from autornd.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


# ---- I22 diversity archive ----
def _n(i, x, y, m):
    return Node(id=i, operator="improve", metric=m, status=NodeStatus.evaluated,
                idea=Idea(operator="improve", params={"x": x, "y": y}))


def test_archive_keeps_best_per_niche():
    st = RunState(direction="min")
    # two nodes in the same niche (x≈0,y≈0), one in another (x≈5,y≈5)
    for n in [_n(0, 0.1, 0.1, 5.0), _n(1, 0.2, 0.2, 3.0), _n(2, 5.0, 5.0, 9.0)]:
        st.nodes[n.id] = n
    arch = DiversityArchive(resolution=1.0).build(st)
    assert len(arch) == 2                                  # two niches
    # within the (0,0) niche the better metric (node 1, m=3) wins
    by_id = {n.id for n in arch.values()}
    assert by_id == {1, 2}


def test_archive_summary_emitted_on_run(tmp_path):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=10))
    state = anyio.run(eng.run)
    assert state.archive is not None and state.archive["niches"] >= 1
    assert any(e.type == "diversity_archive"
               for e in EventStore(tmp_path / "run" / "events.jsonl").read_all())


# ---- I21 HITL ----
def _hitl_engine(rd):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=4), require_approval=True)


def test_hitl_pauses_then_finishes_on_approval(tmp_path):
    rd = tmp_path / "run"
    s1 = anyio.run(_hitl_engine(rd).run)
    # Paused: not finished, awaiting approval, request recorded, best chosen but unconfirmed.
    assert not s1.finished and s1.awaiting_approval and not s1.approved
    events = list(EventStore(rd / "events.jsonl").read_all())
    assert any(e.type == "approval_requested" for e in events)
    assert not any(e.type == "run_finished" for e in events)

    # A human approves (as the `approve` CLI does).
    EventStore(rd / "events.jsonl").append("approval_granted", {"node_id": s1.best().id})

    # Resume -> finishes.
    s2 = anyio.run(_hitl_engine(rd).run)
    assert s2.finished and s2.approved
    assert s2.best() is not None


def test_no_approval_required_finishes_directly(tmp_path):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    state = anyio.run(eng.run)
    assert state.finished and not state.awaiting_approval
