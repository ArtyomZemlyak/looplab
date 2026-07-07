"""node_reset: re-run an EXISTING node IN PLACE from a stage (propose|implement|eval), never minting a
new id. The operator "fix this node, don't proliferate" control. Covers the fold semantics (reset re-
opens the node; the first terminal AFTER the reset wins) and the engine re-run for a live toy run."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Event, NodeStatus
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def _ev(t, d, s):
    return Event(seq=s, ts=0.0, type=t, data=d)


# --------------------------------------------------------------------------- fold semantics

def _created(nid, code="print(1)"):
    return _ev("node_created", {"node_id": nid, "operator": "draft",
                                "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": code}, nid)


def test_reset_eval_keeps_code_reopens_terminal():
    base = [_created(0), _ev("node_failed", {"node_id": 0, "error": "boom", "reason": "crash"}, 1)]
    st = fold(base + [_ev("node_reset", {"node_id": 0, "from_stage": "eval"}, 2)])
    assert st.nodes[0].status is NodeStatus.pending          # re-opened
    assert st.nodes[0].code == "print(1)"                    # code KEPT (eval-only)
    assert st.nodes[0].rerun_from is None                    # eval needs no re-develop marker
    # a fresh terminal after the reset is accepted (first-terminal-after-reset wins)
    st = fold(base + [_ev("node_reset", {"node_id": 0, "from_stage": "eval"}, 2),
                      _ev("node_evaluated", {"node_id": 0, "metric": 0.9}, 3)])
    assert st.nodes[0].status is NodeStatus.evaluated and st.nodes[0].metric == 0.9


def test_reset_implement_drops_code_keeps_idea_and_marks():
    base = [_created(0), _ev("node_failed", {"node_id": 0, "error": "boom", "reason": "crash"}, 1)]
    st = fold(base + [_ev("node_reset", {"node_id": 0, "from_stage": "implement"}, 2)])
    assert st.nodes[0].status is NodeStatus.pending
    assert st.nodes[0].code == ""                            # code DROPPED (re-develop)
    assert st.nodes[0].idea.params == {"x": 1.0}             # idea KEPT (researcher was fine)
    assert st.nodes[0].rerun_from == "implement"             # engine will re-develop


def test_reset_of_unknown_node_is_noop():
    st = fold([_created(0), _ev("node_reset", {"node_id": 99, "from_stage": "eval"}, 1)])
    assert 99 not in st.nodes and st.nodes[0].status is NodeStatus.pending


# --------------------------------------------------------------------------- engine re-run (live toy)

def _engine(run_dir):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                  n_seeds=2, max_nodes=4)


def test_engine_reruns_reset_node_in_place(tmp_path):
    run_dir = tmp_path / "r"
    anyio.run(_engine(run_dir).run)                          # a full toy run
    store = EventStore(run_dir / "events.jsonl")
    st0 = fold(store.read_all())
    n_before = len(st0.nodes)
    assert n_before >= 2
    target = 1                                               # reset a mid node from implement

    # operator appends the reset intent, then the run is resumed
    store.append("node_reset", {"node_id": target, "from_stage": "implement"})
    anyio.run(_engine(run_dir).run)

    st1 = fold(EventStore(run_dir / "events.jsonl").read_all())
    assert len(st1.nodes) == n_before                        # NO new node minted
    assert st1.nodes[target].rerun_from is None              # the re-run consumed the marker
    assert st1.nodes[target].status is not NodeStatus.pending  # it re-ran to a terminal
    # the target got a SECOND node_created (re-developed in place), same id
    creates = [e for e in EventStore(run_dir / "events.jsonl").read_all()
               if e.type == "node_created" and e.data.get("node_id") == target]
    assert len(creates) >= 2
