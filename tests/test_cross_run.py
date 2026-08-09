"""Cross-run referencing: agents read SIBLING runs (SiblingRunTools), seed an experiment from one
with recorded provenance (Node.origin), and the boss `import` action maps onto the inject pipeline.
Offline — synthetic/real toy runs on disk, no model needed."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.events.eventstore import EventStore
from looplab.core.models import NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.tools.run_tools import SiblingRunTools
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.serve.server import _Action, _action_to_control
from looplab.adapters.toytask import ToyTask

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


def test_sibling_direct_read_refuses_a_foreign_task_run(tmp_path):
    """SiblingRunTools is same-task by contract, but read/code take a model-supplied run_id that IS the
    authorization boundary — a guessed FOREIGN-task run_id must be refused (not folded and returned), else
    an operator relying on SiblingRunTools for same-task scope leaks other tasks. Cross-task reads are the
    separate MachineRunsTools. `list_sibling_runs` already excludes other tasks; this closes the direct path."""
    rdA = tmp_path / "runA"
    stA = anyio.run(_engine(rdA).run)
    task_id, nidA = stA.task_id, stA.best().id

    # runC: a fully-formed run of a DIFFERENT task, with a marker string in its code.
    storeC = EventStore(tmp_path / "runC" / "events.jsonl")
    storeC.append("run_started", {"run_id": "runC", "task_id": "other-task", "direction": "min"})
    storeC.append("node_created", {"node_id": 0, "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}}, "code": "FOREIGN_SECRET=1"})

    tools = SiblingRunTools(tmp_path, "runB")
    tools.bind_state(RunState(run_id="runB", task_id=task_id))   # learns self id + task to scope by

    # a same-task sibling stays readable
    assert "run runA" in tools.execute("read_sibling_experiment", {"run_id": "runA", "node_id": nidA})
    # the foreign-task run is REFUSED on both the direct experiment read and the code read — a guessed id
    # cannot cross the task boundary, and its content never leaks.
    detail = tools.execute("read_sibling_experiment", {"run_id": "runC", "node_id": 0})
    assert "not a sibling of task" in detail
    code = tools.execute("read_sibling_code", {"run_id": "runC", "node_id": 0})
    assert "not a sibling of task" in code and "FOREIGN_SECRET" not in code


# --------------------------------------------------------------------------- AllRunsTools
def test_all_runs_tools_span_all_tasks(tmp_path):
    from looplab.tools.run_tools import AllRunsTools

    # runA: a real finished toy run (evaluated nodes + code).
    rdA = tmp_path / "runA"
    stA = anyio.run(_engine(rdA).run)
    task_id, nidA = stA.task_id, stA.best().id

    # runC: a run of a DIFFERENT task — sibling tools EXCLUDE it, AllRunsTools must INCLUDE it.
    storeC = EventStore(tmp_path / "runC" / "events.jsonl")
    storeC.append("run_started", {"run_id": "runC", "task_id": "other-task", "direction": "min"})
    storeC.append("node_created", {"node_id": 0, "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}}, "code": "x=1  # runC code"})

    tools = AllRunsTools(tmp_path, "runB")
    tools.bind_state(RunState(run_id="runB", task_id=task_id))

    descriptions = "\n".join(spec["function"]["description"] for spec in tools.specs())
    assert "configured run root" in descriptions
    assert "machine-wide" in descriptions
    assert "EVERY run on this machine" not in descriptions

    listing = tools.execute("list_all_runs", {})
    assert "under this configured run root" in listing
    assert "on this machine" not in listing
    assert "runA" in listing            # same-task run surfaced
    assert "runC" in listing            # DIFFERENT-task run ALSO surfaced (the whole point)
    assert "runB" not in listing        # self excluded

    # Code + experiment of ANY run readable — including the foreign-task one.
    code = tools.execute("read_run_code", {"run_id": "runC", "node_id": 0})
    assert "from run runC" in code and "runC code" in code
    detail = tools.execute("read_run_experiment", {"run_id": "runA", "node_id": nidA})
    assert "run runA" in detail and f"experiment #{nidA}" in detail

    # Traversal guard + unknown run soft-fail to a string, never raise.
    assert "no such run" in tools.execute("read_run_code", {"run_id": "../runA", "node_id": 0})
    assert "no such run" in tools.execute("read_run_experiment", {"run_id": "nope", "node_id": 0})


def test_all_runs_empty_listing_states_its_real_scope(tmp_path):
    from looplab.tools.run_tools import AllRunsTools

    listing = AllRunsTools(tmp_path, "self").execute("list_all_runs", {})
    assert listing == "(no other runs under this configured run root)"


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


def test_production_lessons_reach_a_bound_reader(tmp_path):
    """Writer→bound-reader contract: a lesson the ENGINE actually writes must survive the polarity fence.

    `CrossRunTools._in_scope` gates every row on `same_live_direction(self._direction, row["direction"])`,
    which fails CLOSED on a missing value. `lessons_distill.py` never persisted `direction` — only
    `store_case` did — so every production lesson was invisible to agent-facing cross-run memory while
    hand-built test fixtures (which manufacture the field) passed. This pins the two ends together.
    """
    from looplab.engine.lessons_distill import LessonDistillMixin
    from looplab.trust.cross_run import same_live_direction

    class _Task:
        kind = "quadratic"

    class _E:
        task = _Task()

        def _reflect_client(self):
            return None          # offline -> the `_winner_lesson` safety net writes the row

    class _Writer(LessonDistillMixin):
        _e = _E()

        def _evidence_sig_map(self, final, ids):
            return {}

    rd = tmp_path / "runA"
    final = anyio.run(_engine(rd).run)
    best = final.best()
    assert best is not None and final.direction

    rows = _Writer().reflect_lessons(final, best, ["fp"])
    assert rows, "the offline winner-lesson safety net must produce a row"
    for row in rows:
        assert row.get("direction") == final.direction, (
            "every lesson writer must persist `direction`; without it a bound CrossRunTools "
            "fails closed and agent-facing cross-run memory is silently empty")
        # the exact predicate the bound reader applies
        assert same_live_direction(final.direction, row.get("direction")) is True
    # and the fence really is fail-closed, so the field is load-bearing rather than decorative
    assert same_live_direction(final.direction, None) is False


def test_sibling_tools_fail_closed_without_an_authoritative_task_id(tmp_path):
    """No task id means UNKNOWN scope, not permission to widen.

    `SiblingRunTools.task_id` starts empty and `bind_state` only fills it from a truthy
    `state.task_id`, so a missing/failed bind (or a legacy log with no `task_id`) left the same-task
    filter and both direct-read guards as no-ops — the default same-task tool silently became
    AllRunsTools, the deliberately-scoped cross-task reader, for an agent that never asked for it.
    """
    rdA = tmp_path / "runA"
    anyio.run(_engine(rdA).run)
    storeC = EventStore(tmp_path / "runC" / "events.jsonl")
    storeC.append("run_started", {"run_id": "runC", "task_id": "other-task", "direction": "min"})
    storeC.append("node_created", {"node_id": 0, "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}}, "code": "FOREIGN=1"})

    # never bound (the `bind_state` hook is OPTIONAL for providers — see tools/_base.py), and bound
    # to a state whose run_started carried no task_id: both leave `task_id` empty.
    for tools in (SiblingRunTools(tmp_path, "runB"), SiblingRunTools(tmp_path, "runB")):
        tools.bind_state(RunState(run_id="runB"))
        assert tools.execute("list_sibling_runs", {}) == "(no sibling runs of this task)", \
            "an unscoped SiblingRunTools listed runs of every task"
        for fn in ("read_sibling_experiment", "read_sibling_code"):
            out = tools.execute(fn, {"run_id": "runC", "node_id": 0})
            assert "not a sibling of task" in out and "FOREIGN" not in out, \
                f"{fn} served a foreign-task run because no task boundary was known"
