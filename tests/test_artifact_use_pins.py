"""A consumer reads its artifact in the LIFECYCLE it was accepted against (doc 73 §1.4, round 3).

The defect: `uses` was checked only at inject INTAKE. An inject is queued while stopped and built
later; meanwhile the artifact could be reset, rebuilt or deleted, and the consumer's eval read the new
lifecycle's bytes, a half-rebuilt directory or nothing (a missing workdir was silently dropped) — and
could still "succeed", with nothing recording which lifecycle it read. Now the intake stamps the
producer's lifecycle, `node_created` carries it (`uses_attempts`), the fold inherits it with `uses`,
and the eval reads an artifact only while it is evaluated in that lifecycle with its workdir stamp
intact — else the consumer ends `artifact_unavailable` without running. A log with no pins behaves
exactly as before.

And `eval.artifact_sync`: a bounded pool, and a START row an interrupted copy leaves open.
"""
from __future__ import annotations

import threading
import time

import anyio
import pytest

from factories import make_engine
from looplab.engine import artifact_sync
from looplab.events.eventstore import Event
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult

_CLEAN_NO_METRIC = RunResult(exit_code=0, stdout="prepared", metric=None, timed_out=False, stderr="")
_SCORED = RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False, stderr="")


def _created(engine, nid, **extra):
    engine.store.append("node_created", {
        "node_id": nid, "parent_ids": [], "operator": "inject",
        "idea": {"operator": "inject", "params": {}, "rationale": "r"},
        "code": "print('x')", **extra})


def _evaluate(engine, nid, result):
    calls = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        calls.append(engine._uses_workdirs_env(node))
        return result

    engine._run_eval = fake_run_eval
    anyio.run(engine._evaluate, nid, anyio.CapacityLimiter(1), None)
    return calls


def _produced(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, _CLEAN_NO_METRIC)
    assert fold(engine.store.read_all()).nodes[0].status.value == "evaluated"
    return engine


# ------------------------------------------------------------------ the eval refuses a moved artifact

def test_a_pinned_consumer_reads_the_artifact_it_was_accepted_against(tmp_path):
    engine = _produced(tmp_path)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    calls = _evaluate(engine, 1, _SCORED)
    wd = str((tmp_path / "run" / "nodes" / "node_0").resolve())
    assert calls == [{"LOOPLAB_USES_WORKDIRS": wd}]
    assert fold(engine.store.read_all()).nodes[1].status.value == "evaluated"


def test_a_consumer_whose_artifact_was_reset_never_runs_and_says_why(tmp_path):
    engine = _produced(tmp_path)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    assert fold(engine.store.read_all()).nodes[0].attempt == 1
    calls = _evaluate(engine, 1, _SCORED)
    assert calls == [], "the consumer's eval was never launched"
    node = fold(engine.store.read_all()).nodes[1]
    assert node.status.value == "failed" and node.error_reason == "artifact_unavailable"
    assert "#0" in node.error and "lifecycle 0" in node.error and "rebuilt" in node.error
    assert not any(e.type in ("crash_triaged", "node_repaired") for e in engine.store.read_all()), (
        "nothing of the candidate is at fault: no triage, no repair")


def test_a_rewritten_artifact_workdir_is_refused(tmp_path):
    """Same lifecycle, but the directory no longer holds its files (a re-materialization, a hand
    edit of the stamp): the stamp is the evidence, not the status."""
    engine = _produced(tmp_path)
    (tmp_path / "run" / "nodes" / "node_0" / ".looplab-manifest").write_text("0" * 64)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    assert _evaluate(engine, 1, _SCORED) == []
    node = fold(engine.store.read_all()).nodes[1]
    assert node.error_reason == "artifact_unavailable" and "workdir_changed" in node.error


def test_a_deleted_artifact_is_refused(tmp_path):
    engine = _produced(tmp_path)
    engine.store.append("node_tombstoned", {"node_ids": [0]})
    assert fold(engine.store.read_all()).nodes[0].tombstoned
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    assert _evaluate(engine, 1, _SCORED) == []
    node = fold(engine.store.read_all()).nodes[1]
    assert node.error_reason == "artifact_unavailable" and "deleted" in node.error


def test_an_unpinned_consumer_behaves_exactly_as_before(tmp_path):
    """A log written before `uses_attempts`: existence only, even after the producer was reset."""
    engine = _produced(tmp_path)
    _created(engine, 1, uses=[0])
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    calls = _evaluate(engine, 1, _SCORED)
    assert calls == [{"LOOPLAB_USES_WORKDIRS": str((tmp_path / "run" / "nodes" / "node_0").resolve())}]
    assert fold(engine.store.read_all()).nodes[1].status.value == "evaluated"


def test_run_eval_itself_never_launches_without_a_pinned_artifact(tmp_path):
    """The race between RUN_ATTEMPT's check and the env build: `_run_eval` refuses to spawn."""
    engine = _produced(tmp_path)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    node = fold(engine.store.read_all()).nodes[1]
    spawned = []
    engine.sandbox.run = lambda *a, **k: spawned.append(1)
    res = type(engine)._run_eval(engine, node, str(tmp_path / "run" / "nodes" / "node_1"))
    assert spawned == [] and res.exit_code == 2 and res.metric is None
    assert "used artifact #0" in res.stderr


# ------------------------------------------------------------------ the record: node_created + fold

def _rows(*specs):
    return [Event(seq=i, ts=0.0, type=t, data=d) for i, (t, d) in enumerate(specs)]


def _nc(nid, parents=(), **extra):
    return ("node_created", {"node_id": nid, "parent_ids": list(parents), "operator": "inject",
                             "idea": {"operator": "inject"}, "code": "x", **extra})


def test_pins_fold_and_survive_inheritance():
    st = fold(_rows(
        _nc(0, node_kind="artifact"), _nc(1, node_kind="artifact"),
        _nc(2, uses=[0], uses_attempts={"0": 3, "9": 1, "1": 2}),
        _nc(3, uses=[1, 0], uses_attempts={"1": 4, "0": 5}),
        _nc(4, parents=[2, 3]),
        _nc(5, parents=[4]),
        _nc(6, uses=[0])))
    assert st.nodes[2].uses_attempts == {"0": 3}, "a pin for an id not used is dropped"
    assert st.nodes[4].uses == [0, 1] and st.nodes[4].uses_attempts == {"0": 3, "1": 4}, (
        "first parent's pin wins, each use keeps the lifecycle its consumer read")
    assert st.nodes[5].uses_attempts == {"0": 3, "1": 4}
    assert st.nodes[6].uses_attempts == {}, "explicit uses and no pins: the historical rule"
    assert "uses_attempts" not in st.nodes[2].model_dump(), "fold-internal, like parent_generations"


def test_the_inject_and_rebuild_writers():
    from looplab.core.models import Idea, Node, RunState
    from looplab.engine.node_build import inject_use_pins, rebuilt_use_pins
    assert inject_use_pins({"uses": [3], "uses_attempts": {"3": 1, "4": 0}}) == {
        "uses_attempts": {"3": 1}}
    assert inject_use_pins({"uses": [3]}) == {}, "a request queued before the key writes none"
    assert inject_use_pins({"uses": [3], "uses_attempts": {"3": True}}) == {}
    st = RunState()
    st.nodes[3] = Node(id=3, operator="inject", idea=Idea(operator="inject"), attempt=2)
    consumer = Node(id=5, operator="inject", idea=Idea(operator="inject"), uses=[3, 7],
                    uses_attempts={"3": 0, "7": 1})
    assert rebuilt_use_pins(consumer, st) == {"3": 2, "7": 1}, (
        "a rebuild re-pins to the producer as it is now; a vanished one keeps its pin")


def test_an_injected_consumer_carries_its_pins_onto_node_created(tmp_path):
    """The engine half: the request's server-stamped `uses_attempts` reaches `node_created` and the
    fold. (Both requests queued before the run; a finished run does not serve a later inject.)"""
    from looplab.events.eventstore import EventStore
    from test_control import _engine as control_engine
    rd = tmp_path / "run"
    store = EventStore(rd / "events.jsonl")
    store.append("inject_node", {"idea": {"operator": "manual", "params": {"x": 0.5},
                                          "rationale": "prepare"},
                                 "parent_id": None, "code": None, "node_kind": "artifact"})
    store.append("inject_node", {"idea": {"operator": "consume", "params": {"x": 0.5},
                                          "rationale": "train on it"},
                                 "parent_id": None, "code": None, "uses": [0],
                                 "uses_attempts": {"0": 0, "5": 1}})
    state = anyio.run(control_engine(rd).run)
    art = next(n for n in state.nodes.values() if n.operator == "manual")
    assert art.id == 0
    created = [e.data for e in store.read_all()
               if e.type == "node_created" and e.data.get("operator") == "consume"]
    assert created and created[0]["uses_attempts"] == {"0": 0}, "cut to the ids it uses"
    consumer = next(n for n in state.nodes.values() if n.operator == "consume")
    assert consumer.uses_attempts == {"0": 0}


# ------------------------------------------------------------------ the intake stamps the lifecycle

class _Srv:
    def state(self, rd):
        from looplab.events.eventstore import EventStore
        return fold(EventStore(rd / "events.jsonl").read_all())


def test_the_intake_stamps_the_produced_lifecycle_and_refuses_a_forged_one(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from looplab.events.eventstore import EventStore
    from looplab.serve.control_validation import normalize_control
    rd = tmp_path / "demo"
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "inject",
                                  "idea": {"operator": "inject"}, "code": "x", "node_kind": "artifact"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": None, "violations": []})
    store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": None, "violations": []})
    idea = {"operator": "inject", "rationale": "train on the prepared shards"}
    data = normalize_control(_Srv(), rd, "inject_node", {"idea": idea, "uses": [0]})
    assert data["uses_attempts"] == {"0": 1}, "the lifecycle the operator saw produced"
    with pytest.raises(HTTPException):
        normalize_control(_Srv(), rd, "inject_node",
                          {"idea": idea, "uses": [0], "uses_attempts": {"0": 0}})


# ------------------------------------------------------------------ artifact_sync: bound + start row

def _sync_engine(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._eval_spec = {"artifact_sync": {"command": ["true"], "timeout": 60.0}}
    for nid in range(6):
        (tmp_path / "run" / "nodes" / f"node_{nid}").mkdir(parents=True)
    return engine


def test_copies_run_on_a_bounded_pool(tmp_path, monkeypatch):
    engine = _sync_engine(tmp_path)
    active, peak, lock = [0], [0], threading.Lock()

    def slow_run(engine_, node_id, generation, argv, workdir, run_dir, timeout, env=None,
                 sync_id=None, cwd=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        engine_.store.append("artifact_synced", {
            "node_id": node_id, "generation": generation, "sync_id": sync_id, "command": [],
            "exit_code": 0, "timed_out": False, "seconds": 0.0, "stderr_tail": ""})

    monkeypatch.setattr(artifact_sync, "_run", slow_run)
    before = threading.active_count()
    ids = [artifact_sync.start_artifact_sync(engine, nid, 0) for nid in range(6)]
    assert threading.active_count() - before <= artifact_sync.MAX_CONCURRENT_SYNCS
    assert artifact_sync.wait_for_inflight(30)
    assert peak[0] == artifact_sync.MAX_CONCURRENT_SYNCS, peak
    events = engine.store.read_all()
    assert sorted(e.data["node_id"] for e in events if e.type == "artifact_synced") == list(range(6))
    assert artifact_sync.unfinished_syncs(events) == []
    assert len(set(ids)) == 6 and all(ids)


def test_an_interrupted_copy_is_visible_as_started_never_finished(tmp_path, monkeypatch):
    engine = _sync_engine(tmp_path)
    release = threading.Event()

    def blocked_run(engine_, node_id, generation, argv, workdir, run_dir, timeout, env=None,
                    sync_id=None, cwd=None):
        release.wait(30)              # the engine "dies" here: no `artifact_synced` row

    monkeypatch.setattr(artifact_sync, "_run", blocked_run)
    sync_id = artifact_sync.start_artifact_sync(engine, 3, 2)
    try:
        open_ = artifact_sync.unfinished_syncs(engine.store.read_all())
        assert open_ == [{"node_id": 3, "generation": 2, "sync_id": sync_id}]
    finally:
        release.set()
        assert artifact_sync.wait_for_inflight(30)


def test_a_real_copy_closes_its_start_row(tmp_path):
    engine = _sync_engine(tmp_path)
    sync_id = artifact_sync.start_artifact_sync(engine, 0, 0)
    assert artifact_sync.wait_for_inflight(30)
    events = engine.store.read_all()
    started = [e for e in events if e.type == "artifact_sync_started"]
    synced = [e for e in events if e.type == "artifact_synced"]
    assert [e.data["sync_id"] for e in started] == [sync_id] == [e.data["sync_id"] for e in synced]
    assert started[0].seq < synced[0].seq
    assert artifact_sync.unfinished_syncs(events) == []
