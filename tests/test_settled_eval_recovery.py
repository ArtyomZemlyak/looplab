"""An evaluation that SETTLED `ok` is not re-run because the process died before its terminal.

THE INCIDENT (run minionerec-backbones-v10, node 0, 2026-09-25): `eval_invocation_settled
{"outcome": "ok", "eval_seconds": 26830.284}`, then an external watchdog killed the engine one second
later — before `_eval_write_terminal`. The fold still held the node `pending`, the resume dispatched it
again, `_eval_prepare_workdir` re-materialized (rmtree'd) its workdir, `eval.log` included, and the
evaluator ran from scratch: seven and a half GPU-hours re-bought for a number the log said existed.

These drive it end to end over a real `Engine` and a real event log: a process killed between the
settle and the terminal, then a second process over the same run directory.
"""
from __future__ import annotations

import json
import os
import time

import anyio
import pytest

from looplab.core.models import Event, Idea, NodeStatus
from looplab.engine import settled_recovery
from looplab.engine.evaluate import _workdir_manifest_digest, settled_ok_awaiting_terminal
from looplab.events.replay import fold
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_EVAL_INVOCATION_CLAIMED,
                                  EV_EVAL_INVOCATION_RECOVERED, EV_EVAL_INVOCATION_SETTLED,
                                  EV_WORKSPACE_SEEDED)
from looplab.runtime.sandbox import RunResult
from tests.factories import make_engine

_TERMINALS = ("node_evaluated", "node_failed")


class _Kill(BaseException):
    """A BaseException, so `_evaluate`'s containment cannot absorb it: the process dying."""


def _engine(run_dir):
    engine = make_engine(run_dir, max_nodes=2)
    engine.concurrent_research = False
    return engine


def _seed(engine):
    engine.store.append("run_started", {"run_id": "settled", "task_id": "toy", "direction": "min"})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": Idea(operator="draft").model_dump(mode="json"), "code": "print(1)"})


def _drive(engine):
    anyio.run(lambda: engine._evaluate(0, anyio.CapacityLimiter(1), None))


def _rows(engine, kind):
    return [e.data for e in engine.store.read_all() if e.type == kind]


def _terminals(engine):
    return [e for e in engine.store.read_all() if e.type in _TERMINALS]


def _measured():
    return RunResult(exit_code=0, stdout='training...\n{"metric": 0.25, "recall": 0.5}\n',
                     stderr="warn: slow\n", metric=0.25, timed_out=False,
                     extra_metrics={"recall": 0.5}, extra_metrics_provenance={"recall": "auto"},
                     stages=[{"name": "train", "status": "ok", "exit_code": 0, "seconds": 9.0}])


def _die_before_the_terminal(run_dir, monkeypatch=None, *, strip_record=False):
    """First process: the evaluator runs and settles `ok`, then the process dies before the
    terminal — the exact window the incident's watchdog kill landed in."""
    dead = _engine(run_dir)
    _seed(dead)
    dead._run_eval = lambda *_a, **_kw: _measured()

    async def _killed(_a):
        raise _Kill("killed between the settle and the terminal")

    dead._eval_write_terminal = _killed
    if strip_record:
        # A settle written before the `result` column existed (the incident's own row).
        monkeypatch.setattr(settled_recovery, "settled_result_record", lambda *_a, **_kw: None)
    with pytest.raises(BaseException):
        _drive(dead)
    if strip_record:
        monkeypatch.undo()
    settles = _rows(dead, EV_EVAL_INVOCATION_SETTLED)
    assert [s["outcome"] for s in settles] == ["ok"]
    assert not _terminals(dead), "the gap: a paid, measured evaluation with no terminal"
    assert fold(dead.store.read_all()).nodes[0].status is NodeStatus.pending
    return dead


def _resumed(run_dir, calls):
    resumed = _engine(run_dir)

    def _eval(*_a, **_kw):
        calls.append(1)
        return RunResult(exit_code=0, stdout='{"metric": 0.99}\n', stderr="", metric=0.99,
                         timed_out=False)

    resumed._run_eval = _eval
    return resumed


# ------------------------------------------------------------------------------ the rule


def _row(seq, kind, *, outcome=None, invocation_id="k", generation=0):
    d = {"node_id": 0, "generation": generation, "attempt": 0, "invocation_id": invocation_id}
    if outcome is not None:
        d["outcome"] = outcome
    return Event(v=1, seq=seq, ts=1000.0 + seq, type=kind, data=d)


def test_only_an_ok_settle_that_is_the_lifecycles_last_invocation_row_is_awaiting_a_terminal():
    claim, ok = _row(1, EV_EVAL_INVOCATION_CLAIMED), _row(2, EV_EVAL_INVOCATION_SETTLED, outcome="ok")
    found = settled_ok_awaiting_terminal([claim, ok], 0, 0)
    assert found is not None and found[0] == 1001.0 and found[1] == 2
    assert settled_ok_awaiting_terminal([claim], 0, 0) is None, "an OPEN claim is the other question"
    assert settled_ok_awaiting_terminal(
        [claim, _row(2, EV_EVAL_INVOCATION_SETTLED, outcome="failed")], 0, 0) is None
    assert settled_ok_awaiting_terminal(
        [claim, ok, _row(3, EV_EVAL_INVOCATION_CLAIMED)], 0, 0) is None, (
        "a later claim means a newer invocation owns the answer")
    assert settled_ok_awaiting_terminal([claim, ok], 0, 1) is None, "another lifecycle's row"


def test_the_recovery_row_is_diagnostic():
    assert EV_EVAL_INVOCATION_RECOVERED in DIAGNOSTIC_EVENTS


# -------------------------------------------------------------- driven over a real Engine


def test_a_death_after_an_ok_settle_is_finalized_from_the_settle_record_without_a_rerun(tmp_path):
    """THE DEFECT. MUTATION: drop `_eval_recover_settled` from the driver -> the resumed process
    calls the evaluator again and records 0.99 instead of the measured 0.25."""
    run_dir = tmp_path / "run"
    dead = _die_before_the_terminal(run_dir)
    settled = _rows(dead, EV_EVAL_INVOCATION_SETTLED)[0]
    assert settled["result"]["metric"] == 0.25, "the settle now carries what it measured"

    calls: list = []
    resumed = _resumed(run_dir, calls)
    _drive(resumed)

    assert calls == [], "the evaluator must not run again for a result the log already holds"
    assert len(_rows(resumed, EV_EVAL_INVOCATION_CLAIMED)) == 1, "no second invocation was opened"
    terminals = _terminals(resumed)
    assert [t.type for t in terminals] == ["node_evaluated"]
    ev = terminals[0].data
    assert ev["metric"] == 0.25
    assert ev["extra_metrics"] == {"recall": 0.5}
    assert ev["eval_seconds"] == settled["eval_seconds"], "charged what the invocation cost, once"
    assert '"metric": 0.25' in ev["stdout_tail"]
    recovered = _rows(resumed, EV_EVAL_INVOCATION_RECOVERED)
    assert recovered == [{"node_id": 0, "generation": 0, "attempt": 0,
                          "invocation_id": settled["invocation_id"], "action": "finalized",
                          "source": "settle_record"}]
    state = fold(resumed.store.read_all())
    assert state.nodes[0].status is NodeStatus.evaluated and state.nodes[0].metric == 0.25

    # IDEMPOTENT: a third process (another crash-and-resume) finds a closed lifecycle.
    again = _resumed(run_dir, calls)
    _drive(again)
    assert calls == []
    assert len(_terminals(again)) == 1, "exactly one terminal across all three processes"
    assert len(_rows(again, EV_EVAL_INVOCATION_RECOVERED)) == 1


def test_a_pre_record_settle_is_finalized_from_the_workdir_log_under_the_current_spec(
        tmp_path, monkeypatch):
    """The incident's own shape: a settle with no `result`. The captured output survives in the
    node workdir's `eval.log`, and the recovery reads it BEFORE materialization would wipe it."""
    run_dir = tmp_path / "run"
    _die_before_the_terminal(run_dir, monkeypatch, strip_record=True)
    workdir = run_dir / "nodes" / "node_0"
    assert workdir.is_dir()
    # The repo path's materialization stamps the manifest it built (`stamp_workdir`); the toy
    # task's workdir holds no source, so the stamp a repo workdir carries is written here.
    node = fold(_engine(run_dir).store.read_all()).nodes[0]
    (workdir / ".looplab-manifest").write_text(_workdir_manifest_digest(node), encoding="ascii")
    (workdir / "eval.log").write_text(
        'epoch 1 loss 0.3\n{"metric": 0.125, "UnseenRecall@20": 0.031}\nteardown\n',
        encoding="utf-8")

    calls: list = []
    resumed = _resumed(run_dir, calls)
    resumed._eval_spec = {"command": ["true"], "metric": {"kind": "stdout_json", "key": "metric"}}
    _drive(resumed)

    assert calls == []
    terminals = _terminals(resumed)
    assert [t.type for t in terminals] == ["node_evaluated"]
    assert terminals[0].data["metric"] == 0.125
    assert terminals[0].data["extra_metrics"] == {"UnseenRecall@20": 0.031}
    (recovered,) = _rows(resumed, EV_EVAL_INVOCATION_RECOVERED)
    assert recovered["action"] == "finalized" and recovered["source"] == "workdir_log"
    assert (workdir / "eval.log").exists(), "nothing re-materialized the workdir"


def test_no_evidence_reruns_the_evaluation_and_says_why(tmp_path, monkeypatch):
    """A settle with no `result` and no command-eval log (here: a toy task) cannot be finalized, so
    the node evaluates exactly as before — but never silently."""
    run_dir = tmp_path / "run"
    _die_before_the_terminal(run_dir, monkeypatch, strip_record=True)

    calls: list = []
    resumed = _resumed(run_dir, calls)
    _drive(resumed)

    assert calls == [1], "no evidence -> the evaluation re-runs"
    (recovered,) = _rows(resumed, EV_EVAL_INVOCATION_RECOVERED)
    assert recovered["action"] == "rerun"
    assert "settle_row_has_no_result" in recovered["reason"]
    assert "no_command_eval_spec" in recovered["reason"]
    terminals = _terminals(resumed)
    assert len(terminals) == 1 and terminals[0].data["metric"] == 0.99


def test_a_workdir_rebuilt_after_the_settle_is_not_evidence(tmp_path, monkeypatch):
    """A resume on the OLD code re-materialized the workdir after the settle (the incident's second
    process did exactly that): whatever `eval.log` holds now is not that invocation's output."""
    run_dir = tmp_path / "run"
    dead = _die_before_the_terminal(run_dir, monkeypatch, strip_record=True)
    dead.store.append(EV_WORKSPACE_SEEDED, {"node_id": 0, "materialized": [], "workspace_bytes": 1})
    (run_dir / "nodes" / "node_0" / "eval.log").write_text('{"metric": 0.5}\n', encoding="utf-8")

    calls: list = []
    resumed = _resumed(run_dir, calls)
    resumed._eval_spec = {"command": ["true"], "metric": {"kind": "stdout_json", "key": "metric"}}
    resumed._run_eval = lambda *_a, **_kw: (calls.append(1), RunResult(
        exit_code=0, stdout='{"metric": 0.99}\n', stderr="", metric=0.99, timed_out=False))[1]
    _drive(resumed)

    assert calls == [1]
    (recovered,) = _rows(resumed, EV_EVAL_INVOCATION_RECOVERED)
    assert recovered["action"] == "rerun"
    assert "workdir_rematerialized_after_settle" in recovered["reason"]
    assert len(_terminals(resumed)) == 1


def test_a_log_older_than_the_claim_is_not_that_invocations_output(tmp_path):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    log = workdir / "eval.log"
    log.write_text('{"metric": 0.5}\n', encoding="utf-8")
    old = time.time() - 3600
    os.utime(log, (old, old))
    spec = {"metric": {"kind": "stdout_json", "key": "metric"}}
    res, reason = settled_recovery.result_from_workdir_log(
        workdir, spec, since=old + 60, pipeline_stages=[], enforce_drift=False)
    assert res is None and reason == "workdir_log_predates_the_invocation"
    res, reason = settled_recovery.result_from_workdir_log(
        workdir, spec, since=old - 60, pipeline_stages=[], enforce_drift=False)
    assert res is not None and res.metric == 0.5 and reason == ""
    # a reader that EXECUTES code is never run by a recovery
    res, reason = settled_recovery.result_from_workdir_log(
        workdir, {"metric": {"kind": "adapter", "key": "metric"}}, since=old - 60,
        pipeline_stages=[], enforce_drift=False)
    assert res is None and reason.startswith("reader_executes_code")


def test_the_record_round_trips_through_the_log(tmp_path):
    rec = settled_recovery.settled_result_record(_measured(), stdout_tail="tail", stderr_tail="")
    rec = json.loads(json.dumps(rec))
    res, reason = settled_recovery.result_from_record(rec)
    assert reason == "" and res.metric == 0.25 and res.stdout == "tail"
    assert res.extra_metrics == {"recall": 0.5} and res.stages[0]["name"] == "train"
    assert settled_recovery.result_from_record({**rec, "v": 99})[0] is None
    assert settled_recovery.result_from_record({**rec, "metric": None})[0] is None
