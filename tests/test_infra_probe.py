"""The engine asks the BOX before it blames the candidate (`runtime/infra_probe.py`).

Incident 2026-10-06: a container restart wiped `/var/tmp` and the operator's network data mount then
answered `ENOTCONN` for hours. Every eval that ran meanwhile died non-zero, classified `crash`, and
bought a paid triage plus Developer repairs of a training script with nothing wrong in it — a
`reject_idea` closing a lineage over a mount. These tests drive the real `_evaluate` chain with a
declared mount that goes away mid-evaluation, and the probe's own truth table.
"""
from __future__ import annotations

import errno
import os
import shutil
import sys
import threading

import anyio
import pytest

from factories import make_engine
from looplab.events.replay import fold
from looplab.runtime import infra_probe
from looplab.runtime.command_eval import RunResult


# ------------------------------------------------------------------ the probe's truth table

def test_a_healthy_box_answers_with_no_fault(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "x.csv").write_text("a\n1\n")
    targets = infra_probe.declared_targets(
        run_dir=tmp_path, repo_spec={"data": {"d": {"path": str(data)}}},
        interpreter=sys.executable, env={"ROOT": str(data)})
    assert infra_probe.probe(targets) == []
    assert not list(tmp_path.glob(".looplab-infra-probe*")), "the write probe cleans up after itself"


def test_a_declared_mount_that_is_gone_is_a_fault_but_an_undeclared_env_output_is_not(tmp_path):
    targets = infra_probe.declared_targets(
        repo_spec={"data": {"d": {"path": str(tmp_path / "gone")}},
                   "references": [{"name": "r", "path": str(tmp_path / "gone-ref"), "mount": True},
                                  {"name": "ctx", "path": str(tmp_path / "context-only")}]},
        env={"OUT_DIR": str(tmp_path / "not-yet-written")})
    faults = infra_probe.probe(targets)
    assert [(f.role, f.cause) for f in faults] == [("mount", "ENOENT"), ("mount", "ENOENT")]
    assert str(tmp_path / "context-only") not in {p for _r, p in targets}, (
        "a context-only reference is read by agents at build time, never by the eval")


def test_an_infra_errno_on_an_env_path_is_a_fault(tmp_path, monkeypatch):
    real_stat = os.stat
    dead = str(tmp_path / "dead-mount")

    def _stat(path, *a, **k):
        if os.fspath(path) == dead:
            raise OSError(errno.ENOTCONN, "Transport endpoint is not connected", dead)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(infra_probe.os, "stat", _stat)
    faults = infra_probe.probe([("env_path", dead)])
    assert [(f.role, f.cause) for f in faults] == [("env_path", "ENOTCONN")]
    assert "Transport endpoint" in infra_probe.describe(faults)


def test_a_missing_interpreter_is_a_fault(tmp_path):
    faults = infra_probe.probe([("interpreter", str(tmp_path / "envs" / "py" / "bin" / "python"))])
    assert [f.cause for f in faults] == ["missing"]


def test_an_unwritable_run_dir_is_a_fault(tmp_path, monkeypatch):
    def _open(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(infra_probe.os, "open", _open)
    faults = infra_probe.probe([("run_dir", str(tmp_path))])
    assert [(f.role, f.cause) for f in faults] == [("run_dir", "ENOSPC")]


def test_a_hanging_mount_is_a_timeout_and_then_hung_without_a_second_thread(tmp_path, monkeypatch):
    release = threading.Event()
    real_stat = os.stat
    hang = str(tmp_path / "nfs")
    calls = []

    def _stat(path, *a, **k):
        if os.fspath(path) == hang:
            calls.append(path)
            release.wait(5)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(infra_probe.os, "stat", _stat)
    try:
        first = infra_probe.probe_target("mount", hang, timeout=0.05)
        second = infra_probe.probe_target("mount", hang, timeout=0.05)
        assert first is not None and first.cause == "timeout"
        assert second is not None and second.cause == "hung"
        assert len(calls) == 1, "a path known to hang must not stack a second stuck probe"
    finally:
        release.set()


def test_env_values_that_are_not_plain_absolute_paths_are_not_targets(tmp_path):
    targets = infra_probe.declared_targets(env={
        "LR": "3e-4", "NO_PROXY": "a.example,b.example", "REL": "data/x",
        "PATHLIST": f"/a{os.pathsep}/b", "ROOT": "/srv/data"})
    assert targets == [("env_path", "/srv/data")]


# ------------------------------------------------------------------ driven through `_evaluate`

class _FixDev:
    last_files: dict = {}
    last_deleted: list = []

    def __init__(self):
        self.repairs = 0

    def repair(self, idea, code, err):
        self.repairs += 1
        return "raise SystemExit(1)\n"



def _seen_working(store) -> None:
    """An earlier node evaluated on this box, so a declared mount that no longer exists WENT AWAY
    (`infra_probe.admissible_faults`) rather than not having been created yet."""
    store.append("node_created", {
        "node_id": 9, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 9.0}, "rationale": "r"}, "code": "print(9)"})
    store.append("node_evaluated", {"node_id": 9, "generation": 0, "metric": 0.9, "violations": []})

def _chain(tmp_path, *, lose_mount: bool):
    data = tmp_path / "mnt" / "corpus"
    data.mkdir(parents=True)
    dev = _FixDev()
    engine = make_engine(tmp_path / "run", developer=dev)
    _seen_working(engine.store)
    engine._inline_repair = True
    engine._inline_repair_attempts = 2
    engine._inline_repair_reasons = ("crash",)
    engine._repo_spec = {"data": {"corpus": {"path": str(data), "mount": True}}}
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"},
        "code": "raise SystemExit(1)"})
    evals, triages = [], []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        evals.append(node.code)
        if lose_mount:
            shutil.rmtree(data)                       # the mount went away under the training
        return RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                         stderr="OSError: [Errno 107] Transport endpoint is not connected")

    def fake_triage(*a, **k):
        triages.append(1)
        return {"action": "repair", "rationale": "fix it", "failure_kind": "crash"}

    engine._run_eval = fake_run_eval
    engine._triage_crash = fake_triage
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = engine.store.read_all()
    return dev, evals, triages, events, fold(events)


def test_a_mount_lost_mid_eval_pauses_the_run_and_charges_nothing_to_the_candidate(tmp_path):
    dev, evals, triages, events, st = _chain(tmp_path, lose_mount=True)
    assert len(evals) == 1 and dev.repairs == 0 and not triages, (
        f"{len(evals)} evals, {len(triages)} triages, {dev.repairs} repairs over a dead mount")
    assert st.paused and st.pause_reason == "infra_unavailable", st.pause_reason
    assert st.nodes[0].status.value == "pending", "the box failing is not a verdict on the node"
    assert not any(e.type in ("node_evaluated", "node_failed") and e.data.get("node_id") == 0
                   for e in events)
    withheld = [e.data for e in events if e.type == "eval_attempt_withheld"]
    assert [(w["at"], w["reason"]) for w in withheld] == [("decide_repair", "infra_unavailable")]
    pause = [e.data for e in events if e.type == "pause"][-1]
    assert "corpus" in pause["detail"] and "ENOENT" in pause["detail"], pause


def test_a_healthy_box_still_triages_and_repairs_the_candidate(tmp_path):
    """The control arm: the same stderr TEXT on a box whose mount answers is the candidate's —
    the text alone never decides (a candidate could print it to buy an uncharged retry)."""
    dev, evals, triages, _events, st = _chain(tmp_path, lose_mount=False)
    assert triages and dev.repairs == 2 and len(evals) == 3
    assert not st.paused
    assert st.nodes[0].status.value == "failed"


@pytest.mark.parametrize("already", ["operator"])
def test_an_already_paused_run_gets_no_second_pause_row(tmp_path, already):
    data = tmp_path / "mnt"
    data.mkdir()
    engine = make_engine(tmp_path / "run")
    engine._repo_spec = {"data": {"corpus": {"path": str(data)}}}
    engine.store.append("pause", {"reason": already})
    _seen_working(engine.store)
    shutil.rmtree(data)

    class _A:
        node_id = 0

    assert anyio.run(engine._eval_infra_pause, _A()) is True
    pauses = [e for e in engine.store.read_all() if e.type == "pause"]
    assert len(pauses) == 1 and fold(engine.store.read_all()).pause_reason == already


# ------------------------------------------------------------------ the box BEFORE the launch

def _engine_with_mount(tmp_path, *, seen_working=True):
    data = tmp_path / "mnt" / "corpus"
    data.mkdir(parents=True)
    engine = make_engine(tmp_path / "run")
    engine._repo_spec = {"data": {"corpus": {"path": str(data), "mount": True}}}
    if seen_working:
        # The box has been seen WORKING: an earlier node evaluated on it, so a declared mount that
        # no longer exists went away (`infra_probe.admissible_faults`).
        engine.store.append("node_created", {
            "node_id": 1, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": 2.0}, "rationale": "r"}, "code": "print(2)"})
        engine.store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 0.9,
                                               "violations": []})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})
    evals = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        evals.append(node.code)
        return RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False,
                         stderr="")

    engine._run_eval = fake_run_eval
    return engine, data, evals


def test_a_mount_gone_before_the_launch_launches_nothing_and_pauses(tmp_path):
    """The resume-after-restart case: the first thing the run does is launch on a dead mount."""
    engine, data, evals = _engine_with_mount(tmp_path)
    shutil.rmtree(data)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = engine.store.read_all()
    st = fold(events)
    assert evals == [], "an evaluation was launched on a box whose declared mount is gone"
    assert st.paused and st.pause_reason == "infra_unavailable"
    assert st.nodes[0].status.value == "pending"
    withheld = [e.data for e in events if e.type == "eval_attempt_withheld"]
    assert [(w["at"], w["reason"]) for w in withheld] == [("before_launch", "infra_unavailable")]
    assert "was about to launch" in [e.data for e in events if e.type == "pause"][-1]["detail"]


def test_after_the_box_is_fixed_a_resume_evaluates_the_node_uncharged(tmp_path):
    engine, data, evals = _engine_with_mount(tmp_path)
    shutil.rmtree(data)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    data.mkdir(parents=True)                         # the operator remounts…
    engine.store.append("resume", {})                # …and resumes
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert evals == ["print(1)"] and not st.paused
    assert st.nodes[0].status.value == "evaluated" and st.nodes[0].metric == 0.5
    assert not any(e.type in ("node_repaired", "deps_installed", "node_failed")
                   for e in engine.store.read_all()), "the box's fault was charged to the candidate"


def test_a_healthy_box_launches_as_before(tmp_path):
    engine, _data, evals = _engine_with_mount(tmp_path)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert evals == ["print(1)"] and not st.paused
    assert not any(e.type == "eval_attempt_withheld" for e in engine.store.read_all())


# ------------------------------------------------------------------ the ENGINE raising on a broken box

def test_an_oserror_on_a_broken_box_keeps_the_node_pending_and_pauses(tmp_path, monkeypatch):
    """`_materialize` copying the seed off a dead mount raised ENOTCONN, and `engine_error` closed
    the node for good: after the remount and resume the idea was gone."""
    engine, data, _evals = _engine_with_mount(tmp_path)

    def dead_materialize(a):
        shutil.rmtree(data, ignore_errors=True)
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected", str(data))

    monkeypatch.setattr(engine, "_eval_prepare_workdir", dead_materialize)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = engine.store.read_all()
    st = fold(events)
    assert st.nodes[0].status.value == "pending", "a dead mount ended the node"
    assert not any(e.type in ("node_evaluated", "node_failed") and e.data.get("node_id") == 0
                   for e in events)
    assert st.paused and st.pause_reason == "infra_unavailable"


def test_an_oserror_on_a_healthy_box_is_still_engine_error(tmp_path, monkeypatch):
    engine, _data, _evals = _engine_with_mount(tmp_path)

    def broken(a):
        raise OSError(errno.EACCES, "Permission denied", "/somewhere/else")

    monkeypatch.setattr(engine, "_eval_prepare_workdir", broken)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert st.nodes[0].status.value == "failed" and st.nodes[0].error_reason == "engine_error"
    assert st.paused and st.pause_reason == "engine_error"


def test_an_oserror_on_a_broken_box_of_a_STOPPING_run_still_writes_its_terminal(tmp_path, monkeypatch):
    """A finalize drains and finishes; a node left pending would end the run without a terminal."""
    engine, data, _evals = _engine_with_mount(tmp_path)

    def dead_materialize(a):
        engine.store.append("run_abort", {"reason": "finalized"})   # the stop lands mid-build
        shutil.rmtree(data, ignore_errors=True)
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected", str(data))

    monkeypatch.setattr(engine, "_eval_prepare_workdir", dead_materialize)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert st.nodes[0].status.value == "failed" and st.nodes[0].error_reason == "engine_error"


def test_an_oserror_raised_DURING_the_eval_inside_its_task_group_pauses_rather_than_ends(tmp_path):
    """critic 2026-10-08: `_run_eval` runs inside RUN_ATTEMPT's task group, so its OSError reaches
    the containment wrapped in an `ExceptionGroup`. The box is asked about every LEAF."""
    engine, data, _ = _engine_with_mount(tmp_path)

    def dead_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        shutil.rmtree(data, ignore_errors=True)
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected", str(data))

    engine._run_eval = dead_run_eval
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert st.nodes[0].status.value == "pending", (st.nodes[0].error_reason, st.pause_reason)
    assert st.paused and st.pause_reason == "infra_unavailable"


def test_a_missing_mount_before_the_box_was_ever_seen_working_launches(tmp_path):
    """critic 2026-10-08: an operator `run_setup` may download INTO a declared mount, and it runs
    inside the launch, after the pre-launch probe. Before any node evaluated, a MISSING source is
    not a fault — pausing there would pause on every resume, forever."""
    engine, data, evals = _engine_with_mount(tmp_path, seen_working=False)
    shutil.rmtree(data)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert evals == ["print(1)"] and not st.paused
    assert st.nodes[0].status.value == "evaluated"


def test_a_mount_that_does_not_ANSWER_is_a_fault_even_before_the_first_evaluation():
    fault = infra_probe.InfraFault("mount", "/mnt/x", "ENOTCONN")
    gone = infra_probe.InfraFault("mount", "/mnt/x", "ENOENT")
    no_python = infra_probe.InfraFault("interpreter", "/var/tmp/env/bin/python", "missing")
    assert infra_probe.admissible_faults([fault, gone, no_python], seen_working=False) == [fault]
    assert infra_probe.admissible_faults([fault, gone, no_python], seen_working=True) == [
        fault, gone, no_python]


def test_a_full_run_dir_is_believed_once_per_lifecycle(tmp_path, monkeypatch):
    """critic 2026-10-08: the candidate's workdir shares the run dir's filesystem, so a script that
    fills the disk would otherwise pause the run, be withheld, re-run on the resume and fill it
    again, forever — never repaired. The second time in one lifecycle it is the candidate's."""
    full = [infra_probe.InfraFault("run_dir", "/run", "ENOSPC")]
    assert infra_probe.disk_full_only(full)
    assert not infra_probe.disk_full_only(full + [infra_probe.InfraFault("mount", "/m", "EIO")])
    assert not infra_probe.disk_full_only([])
    engine, _data, _evals = _engine_with_mount(tmp_path)
    engine._inline_repair = False
    monkeypatch.setattr(infra_probe, "probe", lambda targets, **k: list(full))
    crashed = RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                        stderr="OSError: [Errno 28] No space left on device")
    engine._run_eval = lambda *a, **k: crashed
    # The pre-launch probe would pause first; this drives the post-failure decision alone.
    monkeypatch.setattr(type(engine), "_infra_probe_targets", lambda self: [("run_dir", "/run")])
    real = type(engine)._eval_infra_pause

    async def only_after_failure(self, a, *, failed=True):
        return await real(self, a, failed=failed) if failed else False

    monkeypatch.setattr(type(engine), "_eval_infra_pause", only_after_failure)
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert st.paused and st.nodes[0].status.value == "pending", "the first time, the box is believed"
    engine.store.append("resume", {})
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert not st.paused and st.nodes[0].status.value == "failed", (
        "the same lifecycle filling the disk again is the candidate's failure")


def test_the_tasks_own_interpreter_is_probed(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._repo_spec = {"task_python": "/var/tmp/conda/envs/x/bin/python"}
    targets = engine._infra_probe_targets()
    if getattr(engine.sandbox, "python", None) is not None:
        assert ("interpreter", "/var/tmp/conda/envs/x/bin/python") in targets
