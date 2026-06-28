"""Cross-run referencing: agents read SIBLING runs (SiblingRunTools), seed an experiment from one
with recorded provenance (Node.origin), and the boss `import` action maps onto the inject pipeline.
Offline — synthetic/real toy runs on disk, no model needed."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.eventstore import EventStore
from looplab.models import NodeStatus, RunState
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.run_tools import SiblingRunTools
from looplab.sandbox import SubprocessSandbox
from looplab.server import _Action, _action_to_control
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _engine(rd, **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=4), **kw)


# --------------------------------------------------------------------------- SiblingRunTools
def test_sibling_tools_read_filter_and_traversal_guard(tmp_path):
    # runA: a real finished toy run (evaluated nodes + code) under the shared run-root.
    rdA = tmp_path / "runA"
    stA = anyio.run(_engine(rdA).run)
    task_id, nidA = stA.task_id, stA.best().id

    # runC: a sibling of a DIFFERENT task — must be excluded from runB's same-task sibling set.
    storeC = EventStore(tmp_path / "runC" / "events.jsonl")
    storeC.append("run_started", {"run_id": "runC", "task_id": "other-task", "direction": "min"})
    storeC.append("node_created", {"node_id": 0, "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}}, "code": "x=1"})

    tools = SiblingRunTools(tmp_path, "runB")
    tools.bind_state(RunState(run_id="runB", task_id=task_id))   # learns self id + task to filter by

    listing = tools.execute("list_sibling_runs", {})
    assert "runA" in listing            # same-task sibling surfaced
    assert "runC" not in listing        # different task excluded
    assert "runB" not in listing        # self is never its own sibling

    detail = tools.execute("read_sibling_experiment", {"run_id": "runA", "node_id": nidA})
    assert "run runA" in detail and f"experiment #{nidA}" in detail
    code = tools.execute("read_sibling_code", {"run_id": "runA", "node_id": nidA})
    assert "from run runA" in code

    # Path-traversal guard: a sibling id escaping the run-root resolves to "no such sibling".
    assert "no such sibling" in tools.execute(
        "read_sibling_experiment", {"run_id": "../runA", "node_id": 0})
    # Unknown sibling soft-fails to a string, never raises.
    assert "no such sibling" in tools.execute(
        "read_sibling_code", {"run_id": "nope", "node_id": 0})


def test_sibling_find_analogous_across_runs(tmp_path):
    rdA = tmp_path / "runA"
    stA = anyio.run(_engine(rdA).run)
    params = dict(stA.best().idea.params)
    tools = SiblingRunTools(tmp_path, "runB")
    tools.bind_state(RunState(run_id="runB", task_id=stA.task_id))
    out = tools.execute("find_analogous_across_runs", {"params": params or {"x": 0.0}})
    assert isinstance(out, str)
    if params:                                   # toy params are numeric → a match in runA
        assert "run runA" in out


# --------------------------------------------------------------------------- provenance round-trip
def test_node_origin_survives_fold(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("node_created", {"node_id": 0, "operator": "manual",
                                  "idea": {"operator": "manual", "params": {}},
                                  "origin": {"run_id": "src", "node_id": 7, "metric": 1.0}})
    st = fold(EventStore(tmp_path / "events.jsonl").read_all())
    assert st.nodes[0].origin == {"run_id": "src", "node_id": 7, "metric": 1.0}


def test_inject_with_origin_round_trips_through_engine(tmp_path):
    rd = tmp_path / "run"
    origin = {"run_id": "runA", "node_id": 3, "metric": 0.42}
    EventStore(rd / "events.jsonl").append("inject_node", {
        "idea": {"operator": "manual", "params": {"x": 0.3}},
        "code": "print('{\"metric\": 0.5}')", "origin": origin})
    state = anyio.run(_engine(rd).run)
    inj = next(n for n in state.nodes.values() if n.operator == "manual")
    assert inj.origin == origin                              # provenance recorded on the node
    assert inj.status is NodeStatus.evaluated and inj.metric == 0.5   # parity: evaluated like any inject


def test_inject_without_origin_has_none(tmp_path):
    rd = tmp_path / "run"
    EventStore(rd / "events.jsonl").append("inject_node", {
        "idea": {"operator": "manual", "params": {}}, "code": "print('{\"metric\": 0.1}')"})
    state = anyio.run(_engine(rd).run)
    inj = next(n for n in state.nodes.values() if n.operator == "manual")
    assert inj.origin is None                                # ordinary inject: no provenance


def test_inject_preserves_multifile_solution(tmp_path):
    """A cross-run import ships the sibling's FULL solution: with ready-made code, explicit files +
    deleted on the request must survive onto the node (else a multi-file repo solution loses its
    helper modules and fails at eval). A code-only inject still gets files={} (backward compat)."""
    rd = tmp_path / "run"
    EventStore(rd / "events.jsonl").append("inject_node", {
        "idea": {"operator": "manual", "params": {}}, "code": "print('{\"metric\": 0.7}')",
        "files": {"helper.py": "X = 1"}, "deleted": ["old.py"]})
    state = anyio.run(_engine(rd).run)
    inj = next(n for n in state.nodes.values() if n.operator == "manual")
    assert inj.files == {"helper.py": "X = 1"}               # helper modules carried through
    assert inj.deleted == ["old.py"]                         # accepted deletions carried through
    assert inj.metric == 0.7


# --------------------------------------------------------------------------- import action mapping
def test_import_action_maps_to_inject_with_source():
    ctrl = _action_to_control(
        _Action(action="import", source_run="runA", source_node=3, node_id=1), None)
    assert ctrl["type"] == "inject_node"
    assert ctrl["data"]["source_run"] == "runA" and ctrl["data"]["source_node"] == 3
    assert ctrl["data"]["parent_id"] == 1                    # seeded under the in-context node
    assert "Import #3 from run runA" in ctrl["label"]


def test_import_action_requires_source():
    assert _action_to_control(_Action(action="import"), None) is None          # no source -> no-op
    assert _action_to_control(_Action(action="import", source_run="runA"), None) is None
