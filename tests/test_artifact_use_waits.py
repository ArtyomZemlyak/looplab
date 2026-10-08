"""Artifact pins, round 3c (critic c3): a consumer WAITS for an artifact still being produced, a child
of a consumer is pinned at build time, a race refusal is an engine outcome, and the copy-out's queue
re-checks what it is about to upload.

Each test drives the defect the critic found on f943dbc:

  * item 2 — a consumer pinned to a producer lifecycle that is still pending/running was evaluated
    first and ended `artifact_unavailable` forever; it is now HELD (at the dispatcher's admission, or
    at ADMIT on a lane that admits without asking) and only a lifecycle that can never be produced
    is refused;
  * item 4 — a child built from a consumer after the artifact was re-produced inherited the OLD pin
    and paid a build to fail; the writer now pins it to the producer's current produced lifecycle;
  * item 6 — `_run_eval`'s last-instant refusal returned an exit-2 result that read as the
    candidate's crash; it is now settled on the same verdict, and `artifact_unavailable` is benign
    for the search's failure statistics (its own attention item says what to do);
  * items 1 / 5 / 7 — `eval.artifact_sync`: a queued copy of a node reset meanwhile is refused, a
    copy of an ended engine is not started, a secret cut at the 64 KB capture boundary or printed
    percent-encoded is masked, a worker that cannot start never drains the queue inline, and an
    unfinished copy is surfaced by `looplab inspect` and the attention feed.
"""
from __future__ import annotations

import sys
import threading
import time
from urllib.parse import quote, quote_plus

import anyio
import pytest

from factories import make_engine
from looplab.engine import artifact_sync
from looplab.engine import evaluate as evaluate_mod
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


def _scripted_run_eval(engine, calls):
    """A `_run_eval` that records which node ran and what it was handed to read, and answers the
    artifact (node 0) with a clean no-metric run and every consumer with a score."""

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        calls.append((node.id, engine._uses_workdirs_env(node)))
        return _CLEAN_NO_METRIC if node.id == 0 else _SCORED

    engine._run_eval = fake_run_eval


def _engine(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    return engine


def _status(engine, nid):
    node = fold(engine.store.read_all()).nodes[nid]
    return node.status.value, node.error_reason or None


# ------------------------------------------------------------------ item 2: wait, never a terminal

def test_the_admission_rule_holds_a_consumer_while_its_producer_is_producing(tmp_path):
    from looplab.engine.eval_dispatch import _eval_admission_current, awaited_producers
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    st = fold(engine.store.read_all())
    assert awaited_producers(st, st.nodes[1]) == [(0, 0)]
    assert not _eval_admission_current(st, st.nodes[1], 0, None), "held while #0 is pending"
    assert _eval_admission_current(st, st.nodes[0], 0, None), "the producer itself is admitted"
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": None,
                                           "violations": []})
    st = fold(engine.store.read_all())
    assert awaited_producers(st, st.nodes[1]) == []
    assert _eval_admission_current(st, st.nodes[1], 0, None), "released once it is produced"


def test_a_consumer_queued_before_its_producer_waits_and_then_reads_it(tmp_path, monkeypatch):
    """DRIVEN through the serial dispatcher, consumer FIRST in the batch: on f943dbc it was evaluated
    before its producer and ended `artifact_unavailable (not_evaluated)` for good. Held at ADMISSION:
    its evaluation is never even entered while the producer is pending."""
    monkeypatch.setattr(evaluate_mod, "USE_HOLD_GRACE_S", 0.0)
    monkeypatch.setattr(evaluate_mod, "USE_HOLD_POLL_S", 0.01)
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    calls, entered = [], []
    _scripted_run_eval(engine, calls)
    real_evaluate = engine._evaluate

    async def recording_evaluate(nid, *args, **kwargs):
        entered.append(nid)
        return await real_evaluate(nid, *args, **kwargs)

    engine._evaluate = recording_evaluate
    st = fold(engine.store.read_all())
    anyio.run(lambda: engine._dispatch_evals(
        [{"kind": "evaluate", "node_id": 1}, {"kind": "evaluate", "node_id": 0}], st, None,
        research=False))
    assert entered == [0], "the consumer was held at admission, the producer ran"
    assert [c[0] for c in calls] == [0]
    assert _status(engine, 0) == ("evaluated", None)
    assert _status(engine, 1) == ("pending", None), "held: no terminal, still owed"
    st = fold(engine.store.read_all())
    anyio.run(lambda: engine._dispatch_evals([{"kind": "evaluate", "node_id": 1}], st, None,
                                             research=False))
    wd = str((tmp_path / "run" / "nodes" / "node_0").resolve())
    assert calls[-1] == (1, {"LOOPLAB_USES_WORKDIRS": wd})
    assert _status(engine, 1) == ("evaluated", None)


def test_a_lane_that_admits_without_asking_waits_for_a_running_producer(tmp_path, monkeypatch):
    """A Card lane admits by its own rule; ADMIT holds the consumer while the producer it waits for
    is RUNNING here, then evaluates it against the produced lifecycle."""
    monkeypatch.setattr(evaluate_mod, "USE_HOLD_POLL_S", 0.02)
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    calls = []
    _scripted_run_eval(engine, calls)
    engine._eval_inflight = {(0, 0)}          # the producer's lane, as a Card session records it

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(engine._evaluate, 1, anyio.CapacityLimiter(1), None)
            await anyio.sleep(0.3)
            assert calls == [], "the consumer is waiting, nothing of it ran"
            assert _status(engine, 1) == ("pending", None)
            await engine._evaluate(0, anyio.CapacityLimiter(1), None)
            engine._eval_inflight.clear()

    anyio.run(scenario)
    assert [c[0] for c in calls] == [0, 1]
    assert _status(engine, 1) == ("evaluated", None)


def test_a_producer_running_nowhere_sends_the_consumer_back_to_the_queue(tmp_path, monkeypatch):
    """Not running here: holding the lane could starve the very producer it waits for (width 1),
    so after the grace the attempt goes back to the queue — no terminal, nothing launched."""
    monkeypatch.setattr(evaluate_mod, "USE_HOLD_POLL_S", 0.01)
    monkeypatch.setattr(evaluate_mod, "USE_HOLD_GRACE_S", 0.0)
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    engine.store.append("node_eval_started", {"node_id": 1, "generation": 0})   # a Card boundary
    calls = []
    _scripted_run_eval(engine, calls)
    anyio.run(engine._evaluate, 1, anyio.CapacityLimiter(1), None)
    assert calls == []
    assert _status(engine, 1) == ("pending", None)
    events = engine.store.read_all()
    withheld = [e.data for e in events if e.type == "eval_attempt_withheld"]
    assert withheld and withheld[-1]["reason"] == "artifact_pending" and withheld[-1]["at"] == "admit"
    assert not any(e.type == "node_failed" for e in events)


def test_a_launch_that_meets_a_producing_artifact_is_withheld_not_failed(tmp_path):
    """RUN_ATTEMPT's own check, past ADMIT's hold (here: a hold that ended for a reason of the
    node's own): still producing is a WAIT — back to the queue — never `artifact_unavailable`."""
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    calls = []
    _scripted_run_eval(engine, calls)

    async def no_hold(a):
        return False

    engine._hold_for_pinned_producers = no_hold
    anyio.run(engine._evaluate, 1, anyio.CapacityLimiter(1), None)
    assert calls == [] and _status(engine, 1) == ("pending", None)
    withheld = [e.data for e in engine.store.read_all() if e.type == "eval_attempt_withheld"]
    assert [(w["at"], w["reason"]) for w in withheld] == [("before_launch", "artifact_pending")]


def test_a_failed_producer_lifecycle_is_refused_not_waited_for(tmp_path):
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    engine.store.append("node_failed", {"node_id": 0, "generation": 0, "error": "x",
                                        "reason": "crash"})
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    calls = []
    _scripted_run_eval(engine, calls)
    anyio.run(engine._evaluate, 1, anyio.CapacityLimiter(1), None)
    assert calls == []
    node = fold(engine.store.read_all()).nodes[1]
    assert node.error_reason == "artifact_unavailable" and "(failed)" in node.error


# ------------------------------------------------------------------ item 4: the writer pins children

def _rows(*specs):
    return [Event(seq=i, ts=0.0, type=t, data=d) for i, (t, d) in enumerate(specs)]


def _nc(nid, parents=(), **extra):
    return ("node_created", {"node_id": nid, "parent_ids": list(parents), "operator": "inject",
                             "idea": {"operator": "inject"}, "code": "x", **extra})


def test_a_child_is_pinned_to_the_artifact_as_it_is_when_it_is_built():
    from looplab.engine.node_build import inherited_use_pins
    st = fold(_rows(
        _nc(0, node_kind="artifact"),
        ("node_evaluated", {"node_id": 0, "generation": 0, "metric": None, "violations": []}),
        _nc(1, uses=[0], uses_attempts={"0": 0}),
        ("node_reset", {"node_id": 0, "from_stage": "eval"}),
        ("node_evaluated", {"node_id": 0, "generation": 1, "metric": None, "violations": []}),
        _nc(2, uses=[0])))                                       # unpinned (pre-pin) consumer
    assert inherited_use_pins(st, [1]) == {"0": 1}, "re-produced: the child reads lifecycle 1"
    assert inherited_use_pins(st, [2]) == {}, "an unpinned use stays on the historical rule"
    st2 = fold(_rows(
        _nc(0, node_kind="artifact"),
        ("node_evaluated", {"node_id": 0, "generation": 0, "metric": None, "violations": []}),
        _nc(1, uses=[0], uses_attempts={"0": 0}),
        ("node_reset", {"node_id": 0, "from_stage": "eval"})))   # lifecycle 1 still producing
    assert inherited_use_pins(st2, [1]) == {"0": 0}, "not produced: the parent's pin is inherited"


def test_the_child_row_carries_its_pins_and_the_fold_reads_them(tmp_path):
    """DRIVEN through the single emitter: on f943dbc the child row carried no pins and the fold
    handed it the parent's lifecycle 0, which the reset had already superseded."""
    engine = _engine(tmp_path)
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "inject",
                                         "idea": {"operator": "inject"}, "code": "x",
                                         "node_kind": "artifact"})
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": None,
                                           "violations": []})
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": None,
                                           "violations": []})
    engine._emit_node_created(node_id=2, parent_ids=[1], operator="improve",
                              idea={"operator": "improve"}, code="x", files={})
    row = [e.data for e in engine.store.read_all() if e.type == "node_created"][-1]
    assert row["uses_attempts"] == {"0": 1} and "uses" not in row, "uses stays implicit"
    child = fold(engine.store.read_all()).nodes[2]
    assert child.uses == [0] and child.uses_attempts == {"0": 1}


def test_a_child_row_from_before_the_writer_pins_still_inherits():
    st = fold(_rows(_nc(0, node_kind="artifact"), _nc(1, uses=[0], uses_attempts={"0": 3}),
                    _nc(2, parents=[1])))
    assert st.nodes[2].uses_attempts == {"0": 3}, "the reader-side default for old rows"


def test_a_run_without_artifacts_writes_no_pins_and_folds_nothing_extra(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    _created(engine, 0)
    from looplab.engine import node_build
    monkeypatch.setattr(node_build, "inherited_use_pins",
                        lambda *a, **k: pytest.fail("no `uses` row: nothing to pin"))
    engine._emit_node_created(node_id=1, parent_ids=[0], operator="improve",
                              idea={"operator": "improve"}, code="x", files={})
    assert "uses_attempts" not in engine.store.read_all()[-1].data


# ------------------------------------------------------------------ item 6: the race is no crash

def test_a_race_after_the_check_is_an_engine_outcome_not_a_crash(tmp_path):
    """RUN_ATTEMPT's check passes, then the producer is reset before `_run_eval` resolves the use:
    on f943dbc the untagged exit-2 result was SETTLE_OUTCOME's crash."""
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    calls = []
    _scripted_run_eval(engine, calls)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    del engine._run_eval                        # the REAL `_run_eval` resolves the use at launch
    original = engine._refuse_unusable_artifacts
    raced = []

    async def check_then_race(a, **kw):
        refused = await original(a, **kw)
        if not raced:
            raced.append(1)
            engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
        return refused

    engine._refuse_unusable_artifacts = check_then_race
    anyio.run(engine._evaluate, 1, anyio.CapacityLimiter(1), None)
    events = engine.store.read_all()
    node = fold(events).nodes[1]
    assert node.status.value == "failed" and node.error_reason == "artifact_unavailable"
    assert "rebuilt" in node.error
    assert not any(e.type in ("crash_triaged", "node_repaired") for e in events)
    settles = [e.data["outcome"] for e in events if e.type == "eval_invocation_settled"
               and e.data["node_id"] == 1]
    assert settles == ["artifact_refused"], "the claimed invocation says nothing launched"


def test_run_eval_tags_the_result_it_never_launched(tmp_path):
    from looplab.engine.eval_dispatch import artifact_refusal
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    node = fold(engine.store.read_all()).nodes[1]
    res = type(engine)._run_eval(engine, node, str(tmp_path / "run" / "nodes" / "node_1"))
    assert [r["why"] for r in artifact_refusal(res)] == ["producing"]
    assert "still being produced in lifecycle 0" in res.stderr
    assert artifact_refusal(_SCORED) == []


def test_artifact_unavailable_is_no_search_failure_but_still_alerts(tmp_path):
    from looplab.core.models import search_outcome
    from looplab.serve.attention import project_run_attention
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    engine.store.append("node_tombstoned", {"node_ids": [0]})
    for nid in (1, 2, 3):
        _created(engine, nid, uses=[0], uses_attempts={"0": 0})
        engine.store.append("node_failed", {"node_id": nid, "generation": 0, "error": "x",
                                            "reason": "artifact_unavailable"})
    events = engine.store.read_all()
    st = fold(events)
    assert search_outcome(st, st.nodes[1]) is None
    assert st.current_failure_count == 0, "three artifact refusals are no failure spike"
    items = project_run_attention("r" * 8, events, engine_running=True)
    alerts = [i for i in items if i["kind"] == "run_failed" and i.get("node_id") in (1, 2, 3)]
    assert sorted(i["node_id"] for i in alerts) == [1, 2, 3]
    assert all("artifact" in i["title"] for i in alerts)
    assert not [i for i in items if i["kind"] == "failure_spike"]


# ------------------------------------------------------------------ item 1: the copy-out queue

def _sync_run(tmp_path, nodes=(1, 2, 3)):
    engine = make_engine(tmp_path / "run")
    for n in nodes:
        wd = tmp_path / "run" / "nodes" / f"node_{n}"
        wd.mkdir(parents=True)
        (wd / ".looplab-manifest").write_text(f"gen0-digest-{n}")
    return engine


def _gated_copy(engine, gate, ran_dir):
    """A copy that waits for `gate`, then records which node it actually copied."""
    ran_dir.mkdir()
    engine._eval_spec = {"artifact_sync": {"command": [
        sys.executable, "-c",
        "import os, sys, time, pathlib\n"
        "while not os.path.exists(sys.argv[1]): time.sleep(0.02)\n"
        "pathlib.Path(sys.argv[2], sys.argv[3]).write_text('ran')\n",
        str(gate), str(ran_dir), "{node_id}"], "timeout": 30}}


def test_a_node_reset_while_its_copy_is_queued_is_refused_not_uploaded(tmp_path):
    """DRIVEN (scratchpad c3drive/queued_reset.py): the pool is full, node 3 waits; it is reset; on
    f943dbc its copy then uploaded lifecycle 1 under a receipt saying lifecycle 0."""
    engine = _sync_run(tmp_path)
    gate, ran = tmp_path / "release", tmp_path / "ran"
    _gated_copy(engine, gate, ran)
    for n in (1, 2, 3):
        artifact_sync.start_artifact_sync(engine, n, 0)
    time.sleep(0.3)
    (tmp_path / "run" / "nodes" / "node_3" / ".looplab-manifest").write_text("gen1-digest-3")
    gate.write_text("go")
    assert artifact_sync.wait_for_inflight(30)
    assert sorted(p.name for p in ran.iterdir()) == ["1", "2"], "node 3's copy never ran"
    receipt = [e.data for e in engine.store.read_all()
               if e.type == "artifact_synced" and e.data["node_id"] == 3]
    assert receipt and receipt[0]["skipped"] == "workdir_changed"
    assert receipt[0]["exit_code"] is None and receipt[0]["workdir_changed"] is True
    assert artifact_sync.unfinished_syncs(engine.store.read_all()) == []


def test_a_copy_queued_past_its_engine_s_end_is_never_started(tmp_path):
    engine = _sync_run(tmp_path)
    gate, ran = tmp_path / "release", tmp_path / "ran"
    _gated_copy(engine, gate, ran)
    for n in (1, 2, 3):
        artifact_sync.start_artifact_sync(engine, n, 0)
    time.sleep(0.3)
    assert artifact_sync.engine_owns_run(engine)
    engine.retire_tracer()                    # `Engine.run`'s last act before the lock is released
    assert not artifact_sync.engine_owns_run(engine)
    gate.write_text("go")
    assert artifact_sync.wait_for_inflight(30)
    assert sorted(p.name for p in ran.iterdir()) == ["1", "2"], "the queued copy never started"
    events = engine.store.read_all()
    assert sorted(e.data["node_id"] for e in events if e.type == "artifact_synced") == [1, 2], (
        "a copy already running closes its own row: its bytes did leave the box")
    assert [r["node_id"] for r in artifact_sync.unfinished_syncs(events)] == [3], (
        "the one never started stays open — the operator's list of copies to run again")


def test_no_worker_thread_never_drains_the_queue_on_the_caller(tmp_path, monkeypatch):
    engine = _sync_run(tmp_path, nodes=(1,))
    engine._eval_spec = {"artifact_sync": {"command": ["true"], "timeout": 30}}
    ran = []
    monkeypatch.setattr(artifact_sync, "_run", lambda *a, **k: ran.append(1))

    real_start = threading.Thread.start

    def no_thread(self):
        if self.name == "looplab-artifact-sync":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    try:
        sync_id = artifact_sync.start_artifact_sync(engine, 1, 0)
        assert ran == [], "f943dbc ran the whole queue inline, on the event loop"
        assert artifact_sync._WORKERS == 0, "the slot is given back"
        assert [r["sync_id"] for r in artifact_sync.unfinished_syncs(engine.store.read_all())] \
            == [sync_id]
    finally:
        monkeypatch.undo()
        with artifact_sync._COND:          # leave the module queue as the next test expects it
            left = [j for j in artifact_sync._QUEUE if j[0] is engine]
            for job in left:
                artifact_sync._QUEUE.remove(job)
            artifact_sync._PENDING -= len(left)


# ------------------------------------------------------------------ item 5: masking at the cut

_SECRET = "zq8Vn3kPw7Lr2Xt9"


def test_a_secret_cut_by_the_capture_boundary_leaves_no_fragment(tmp_path):
    """DRIVEN (scratchpad c3drive/mask_cut.py): the 64 KB tail clamp cut inside the secret and its
    7-character suffix was written to the tool log on f943dbc."""
    from looplab.runtime.sandbox import run_argv
    env = {"MC_SECRET_minio": _SECRET}
    code = ("import os,sys; s=os.environ['MC_SECRET_minio']; "
            "sys.stderr.write('auth failed for key='+s+'\\n'+'x'*(64000-9)+'\\n')")
    _rc, out, err, _t = run_argv([sys.executable, "-c", code], str(tmp_path), 30, env=env)
    assert err.startswith(_SECRET[-7:]), "the fixture still cuts inside the secret"
    artifact_sync.append_tool_log(tmp_path / "artifact_sync.log", ["tool"], out, err, env)
    log = (tmp_path / "artifact_sync.log").read_text()
    assert not any(_SECRET[-k:] in log or _SECRET[:k] in log for k in range(6, len(_SECRET) + 1))


@pytest.mark.parametrize("spelling", [quote(f"{_SECRET}/+ x", safe=""),
                                      quote(f"{_SECRET}/+ x"),
                                      quote_plus(f"{_SECRET}/+ x")])
def test_a_percent_encoded_secret_is_masked(spelling):
    env = {"MC_HOST_minio": f"{_SECRET}/+ x"}
    masked = artifact_sync.mask_tool_text(f"GET https://h/?k={spelling} 403", env)
    assert _SECRET not in masked and "***REDACTED_ENV***" in masked, masked


def test_a_prefix_left_at_the_tail_is_masked_and_ordinary_text_is_not():
    env = {"K": _SECRET}
    assert artifact_sync.mask_tool_text("killed while printing " + _SECRET[:9], env).endswith(
        "***REDACTED_ENV***")
    plain = "nothing secret here, 64000 bytes of progress"
    assert artifact_sync.mask_tool_text(plain, env) == plain


# ------------------------------------------------------------------ item 7: unfinished copies

def test_an_unfinished_copy_is_surfaced_by_inspect_and_attention(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from looplab.cli import app
    from looplab.serve.attention import project_run_attention
    engine = _sync_run(tmp_path, nodes=(4,))
    engine.store.append("artifact_sync_started", {"node_id": 4, "generation": 2,
                                                  "sync_id": "abc123"})
    events = engine.store.read_all()
    live = project_run_attention("r" * 8, events, engine_running=True)
    assert not [i for i in live if i["kind"] == "artifact_sync_unfinished"], (
        "on a live engine an open copy is just queued or uploading")
    gone = project_run_attention("r" * 8, events, engine_running=False)
    item = [i for i in gone if i["kind"] == "artifact_sync_unfinished"]
    assert len(item) == 1 and item[0]["node_id"] == 4 and item[0]["node_generation"] == 2
    assert item[0]["active"] is False
    out = CliRunner().invoke(app, ["inspect", str(tmp_path / "run")]).output
    assert "copy-out unfinished: node 4 lifecycle 2 (sync_id abc123)" in out, out
