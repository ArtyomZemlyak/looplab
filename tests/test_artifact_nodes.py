"""Artifact nodes (doc 73 §1.4, stage 2): a node that PRODUCES what other nodes read.

2026-10-06: "at any moment the Assistant or anyone can make a node that, say, prepares a dataset
for the next ten nodes". An inject with `node_kind: "artifact"` succeeds on a clean pipeline with no
metric and is never ranked; a later inject with `uses: [id]` reads its workdir through
`LOOPLAB_USES_WORKDIRS`. Every path that names neither is byte-identical.
"""
from __future__ import annotations

import anyio
import pytest

from factories import make_engine
from looplab.engine.plan import build_plan, plateau_nodes
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult


def _created(engine, nid, **extra):
    engine.store.append("node_created", {
        "node_id": nid, "parent_ids": [], "operator": "inject",
        "idea": {"operator": "inject", "params": {}, "rationale": "prepare the dataset"},
        "code": "print('prepared')", **extra})


def _evaluate(engine, nid, result):
    seen = {}

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        seen["env"] = env
        return result

    engine._run_eval = fake_run_eval
    anyio.run(engine._evaluate, nid, anyio.CapacityLimiter(1), None)
    return seen


_CLEAN_NO_METRIC = RunResult(exit_code=0, stdout="prepared 3 shards", metric=None, timed_out=False,
                             stderr="")


def test_an_artifact_node_succeeds_on_a_clean_pipeline_with_no_metric_and_is_never_ranked(tmp_path):
    engine = make_engine(tmp_path / "run")
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, _CLEAN_NO_METRIC)
    st = fold(engine.store.read_all())
    node = st.nodes[0]
    assert node.kind == "artifact" and node.status.value == "evaluated" and node.metric is None
    assert st.feasible_nodes() == [] and st.best_node_id is None, "an artifact is never the champion"


def test_the_same_result_is_still_a_failure_for_an_experiment(tmp_path):
    """The control arm: nothing changed for a node that is not an artifact."""
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    _created(engine, 0)
    _evaluate(engine, 0, _CLEAN_NO_METRIC)
    node = fold(engine.store.read_all()).nodes[0]
    assert node.kind is None and node.status.value == "failed" and node.error_reason == "no_metric"


def test_an_artifact_that_crashes_is_a_failure(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                                   stderr="Traceback: disk full"))
    assert fold(engine.store.read_all()).nodes[0].status.value == "failed"


def test_a_consumer_reads_the_artifact_workdirs(tmp_path):
    engine = make_engine(tmp_path / "run")
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, _CLEAN_NO_METRIC)
    _created(engine, 1, uses=[0])
    st = fold(engine.store.read_all())
    assert st.nodes[1].uses == [0] and st.nodes[1].kind is None
    env = engine._uses_workdirs_env(st.nodes[1])
    assert env == {"LOOPLAB_USES_WORKDIRS": str((tmp_path / "run" / "nodes" / "node_0").resolve())}
    assert engine._uses_workdirs_env(st.nodes[0]) == {}, "a node that uses nothing carries nothing"


def test_a_plain_node_created_row_folds_exactly_as_before():
    from looplab.events.eventstore import Event
    rows = [Event(seq=0, ts=0.0, type="node_created",
                  data={"node_id": 0, "parent_ids": [], "operator": "draft",
                        "idea": {"operator": "draft"}, "code": "x"})]
    node = fold(rows).nodes[0]
    assert node.kind is None and node.uses == []
    dumped = node.model_dump()
    assert dumped["kind"] is None and dumped["uses"] == []


def test_the_plateau_does_not_count_an_artifact():
    from looplab.core.models import Idea, Node, NodeStatus, RunState
    st = RunState()
    for i in range(10):
        st.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft"),
                           status=NodeStatus.evaluated, metric=1.0 if i == 3 else 5.0)
    st.nodes[7].metric, st.nodes[7].kind = None, "artifact"
    st.best_node_id = 3
    st.plan = build_plan(max_nodes=12, n_seeds=2, reserve_frac=0.5, at_node=0)
    for i in (8, 9):
        st.nodes[i].status = NodeStatus.pending
    assert plateau_nodes(st) == (3, 1), "node 6 counts; the artifact at 7 does not"


# ------------------------------------------------------------------ the inject command

@pytest.fixture()
def _client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "inject",
                                  "idea": {"operator": "inject"}, "code": "x", "node_kind": "artifact"})
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "y"})
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 0.5, "violations": []})
    return TestClient(make_app(tmp_path)), store


_KEYS = iter(range(10_000))


def _inject(client, data):
    from tests.factories import post_command
    return post_command(client, "inject_node", data, key=f"inject-{next(_KEYS)}")


def _code(answer):
    body = answer.json()
    return body.get("status"), (body.get("error") or {}).get("code")


def test_uses_must_name_a_produced_artifact(_client):
    client, store = _client
    idea = {"operator": "inject", "rationale": "train on the prepared shards"}
    assert _code(_inject(client, {"idea": idea, "uses": [0]})) == (
        "rejected", "inject_uses_not_produced")
    assert _code(_inject(client, {"idea": idea, "uses": [1]})) == (
        "rejected", "inject_uses_not_artifact")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": None, "violations": []})
    status, code = _code(_inject(client, {"idea": idea, "uses": [0]}))
    assert code is None and status != "rejected", (status, code)
    # (The inject then waits on an engine to acknowledge it; the engine half is driven below.)


def test_node_kind_is_artifact_or_absent(_client):
    client, _store = _client
    status, code = _code(_inject(client, {"idea": {"operator": "inject"}, "node_kind": "dataset"}))
    assert status == "rejected" and code == "invalid_command"


def test_an_artifact_that_prints_a_metric_is_still_never_ranked(tmp_path):
    """An operator's runner prints its metric on every mode; a recorded one would make the
    preparation step a candidate for champion."""
    engine = make_engine(tmp_path / "run")
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, RunResult(exit_code=0, stdout='{"metric": 0.99}', metric=0.99,
                                   timed_out=False, stderr=""))
    st = fold(engine.store.read_all())
    assert st.nodes[0].status.value == "evaluated" and st.nodes[0].metric is None
    assert st.best_node_id is None


def test_an_injected_artifact_carries_its_kind_end_to_end(tmp_path):
    """The engine half of the inject: the request's `node_kind`/`uses` reach `node_created`."""
    from looplab.events.eventstore import EventStore
    from test_control import _engine as control_engine
    rd = tmp_path / "run"
    store = EventStore(rd / "events.jsonl")
    store.append("inject_node", {"idea": {"operator": "manual", "params": {"x": 0.5},
                                          "rationale": "prepare"},
                                 "parent_id": None, "code": None, "node_kind": "artifact"})
    state = anyio.run(control_engine(rd).run)
    art = next(n for n in state.nodes.values() if n.operator == "manual")
    assert art.kind == "artifact" and art.metric is None and art.status.value == "evaluated"
    assert state.best_node_id != art.id
    created = [e.data for e in store.read_all()
               if e.type == "node_created" and e.data.get("node_id") == art.id]
    assert created[0]["node_kind"] == "artifact"
    others = [e.data for e in store.read_all()
              if e.type == "node_created" and e.data.get("node_id") != art.id]
    assert others and all("node_kind" not in d and "uses" not in d for d in others), (
        "a node that is not an artifact keeps its historical payload shape")


def test_a_rebuilt_artifact_is_still_an_artifact(tmp_path):
    """A `node_reset` from implement re-emits `node_created` for the same id, and the fold builds a
    fresh Node from that row: the rebuild must carry `node_kind`/`uses`, or the preparation step
    silently becomes a ranked experiment."""
    from looplab.events.eventstore import EventStore
    from test_control import _engine as control_engine
    rd = tmp_path / "run"
    store = EventStore(rd / "events.jsonl")
    store.append("inject_node", {"idea": {"operator": "manual", "params": {"x": 0.5},
                                          "rationale": "prepare"},
                                 "parent_id": None, "code": None, "node_kind": "artifact"})
    state = anyio.run(control_engine(rd).run)
    art = next(n for n in state.nodes.values() if n.operator == "manual")
    store.append("node_reset", {"node_id": art.id, "from_stage": "implement"})
    state = anyio.run(control_engine(rd).run)
    rebuilt = state.nodes[art.id]
    assert rebuilt.attempt == 1 and rebuilt.kind == "artifact" and rebuilt.metric is None
    creates = [e.data for e in store.read_all()
               if e.type == "node_created" and e.data.get("node_id") == art.id]
    assert len(creates) == 2 and creates[-1]["node_kind"] == "artifact"


def test_an_artifact_is_never_ranked_on_a_retargeted_objective():
    """An import or a declared reader may leave an artifact the key a retarget names; it is still
    never ranked, and it is not counted as an UNRANKED experiment either."""
    from looplab.core.models import objective_coverage
    from looplab.events.eventstore import Event
    rows, seq = [], iter(range(100))

    def add(kind, data):
        rows.append(Event(seq=next(seq), ts=0.0, type=kind, data=data))

    add("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    for nid, extra in ((0, {"node_kind": "artifact"}), (1, {})):
        add("node_created", {"node_id": nid, "parent_ids": [], "operator": "inject",
                             "idea": {"operator": "inject"}, "code": "x", **extra})
        add("node_evaluated", {"node_id": nid, "generation": 0, "metric": None if nid == 0 else 0.1,
                               "violations": [], "extra_metrics": {"FUR@200": 0.9 - nid / 2},
                               "extra_metrics_provenance": {"FUR@200": "declared"}})
    add("metric_retarget", {"key": "FUR@200"})
    st = fold(rows)
    assert st.nodes[0].metric is None and st.nodes[1].metric == 0.4
    assert st.best_node_id == 1
    assert objective_coverage(st) == ("FUR@200", [1], [])


def test_a_failed_artifact_is_never_salvaged(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    asked = []
    engine._salvage_eval_metric = lambda *a, **k: asked.append(1)
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, RunResult(exit_code=1, stdout='{"metric": 0.5}', metric=0.5,
                                   timed_out=False, stderr="Traceback: killed"))
    assert fold(engine.store.read_all()).nodes[0].status.value == "failed"
    assert asked == [], "salvage is not even asked for an artifact"


def test_a_child_of_a_consumer_inherits_what_it_uses():
    """critic 2026-10-08: an improve/merge/ablation of a consumer copies code reading
    `LOOPLAB_USES_WORKDIRS`; the fold gives it its parents' `uses` (artifacts only)."""
    from looplab.events.eventstore import Event
    rows, seq = [], iter(range(100))

    def add(kind, data):
        rows.append(Event(seq=next(seq), ts=0.0, type=kind, data=data))

    for nid, extra in ((0, {"node_kind": "artifact"}), (1, {"node_kind": "artifact"}),
                       (2, {"uses": [0]}), (3, {"uses": [1, 0]})):
        add("node_created", {"node_id": nid, "parent_ids": [], "operator": "inject",
                             "idea": {"operator": "inject"}, "code": "x", **extra})
    add("node_created", {"node_id": 4, "parent_ids": [2, 3], "operator": "merge",
                         "idea": {"operator": "merge"}, "code": "y"})
    add("node_created", {"node_id": 5, "parent_ids": [4], "operator": "improve",
                         "idea": {"operator": "improve"}, "code": "z"})
    add("node_created", {"node_id": 6, "parent_ids": [2], "operator": "improve",
                         "idea": {"operator": "improve"}, "code": "w", "uses": []})
    st = fold(rows)
    assert st.nodes[4].uses == [0, 1] and st.nodes[5].uses == [0, 1]
    assert st.nodes[6].uses == [], "an explicit `uses` — even empty — is what the writer said"
