"""Regression suite for post-evaluator GPU release, fault-tolerant settlement,
artifact/post-beacon recovery, role-aware log planning, multi-branch monitor rules,
and Antigravity wake-up observability.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import looplab.engine.evaluate as evaluate_mod
import looplab.engine.external_watch as external_watch_mod
import looplab.harness.checkpoints as checkpoints_mod
import looplab.serve.agent_events as ae
from looplab.cli import app as cli_app
from looplab.core.config import Settings
from looplab.core.models import Event, Idea, NodeStatus
from looplab.engine import settled_recovery
from looplab.engine.eval_log_plan import eval_log_plan
from looplab.engine.evaluate import (
    _durable_prior_seconds,
    abandoned_lifecycle_charges,
    settled_ok_awaiting_terminal,
)
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.events.types import (
    EV_EVAL_CANARY_FINISHED,
    EV_EVAL_CANARY_STARTED,
    EV_EVAL_INVOCATION_CLAIMED,
    EV_EVAL_INVOCATION_RECOVERED,
    EV_EVAL_INVOCATION_SETTLED,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_NODE_RESET,
    EV_PHASE_PROGRESS,
    LOG_ROLE_SCORE,
    LOG_ROLE_TRAINING,
    LOG_ROLE_WORK,
    PROGRESS_STAGE_EVAL,
)
from looplab.harness.mcp_server import HarnessAPI
from looplab.harness.monitor_rules import RulesRefused, evaluate as eval_rule, validate_rules, write_rules
from looplab.runtime.sandbox import RunResult
from looplab.serve.server import make_app
from tests.factories import make_engine


class _ProcessKilled(BaseException):
    """Unabsorbed process termination seam (mirrors test_settled_eval_recovery._Kill)."""


@pytest.fixture(autouse=True)
def _no_background_engine_spawn(monkeypatch):
    from looplab.serve import engine_proc
    monkeypatch.setattr("looplab.serve.engine_proc.auto_resume_enabled", lambda: False)
    before = set(engine_proc._spawned_engines)
    yield
    for pid in list(set(engine_proc._spawned_engines) - before):
        proc = engine_proc._spawned_engines.pop(pid, None)
        engine_proc._kill_process_tree(pid)
        if proc is not None and callable(getattr(proc, "wait", None)):
            import subprocess
            try:
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


def test_gpus_yielded_immediately_after_subprocess_before_post_eval_watch_and_terminal(tmp_path, monkeypatch):
    rd = tmp_path / "runs" / "gpu_yield"
    engine = make_engine(
        rd,
        external_harness=True,
        train_monitor=True,
        train_monitor_interval_s=600.0,
        eval_canary=False,
        inline_repair=False,
    )
    engine.concurrent_research = False
    engine._monitor_cadence = lambda: 600.0
    engine._eval_spec = {
        "command": [sys.executable, "-c", "print('step 1 loss=0.4', flush=True); print('{\"metric\": 0.42}', flush=True)"],
        "metric": {"kind": "stdout_json"},
        "timeout": 5,
    }
    settings = Settings(backend="toy", external_harness=True, train_monitor=True)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")), encoding="utf-8")
    engine.store.append("run_started", {"run_id": "gpu_yield", "task_id": "toy", "direction": "max"})
    engine.store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )

    # Configure a synthetic 4-GPU pool + real host lease lock file so `_resource_eval_env`
    # pins all 4 devices and `_release_gpus` frees both the devices and the host lease.
    lease_path = tmp_path / "host-gpu-pool.lock"
    engine._ensure_resource_state()
    engine._gpu_ids = [0, 1, 2, 3]
    engine._gpu_physical_ids = {0: "0", 1: "1", 2: "2", 3: "3"}
    engine._free_gpus = [0, 1, 2, 3]
    engine._gpu_host_lease_path = lease_path
    acquired = engine._acquire_gpus(4)
    assert acquired == [0, 1, 2, 3]
    assert engine._gpu_host_lease_handle is not None

    released_log: list[tuple[str, list[int]]] = []
    real_release = engine._release_gpus

    def track_release(ids):
        if ids:
            released_log.append(("release", list(ids)))
        return real_release(ids)

    monkeypatch.setattr(engine, "_release_gpus", track_release)
    engine._register_eval_resource_reservation(0, 0, {"gpu_ids": [0, 1, 2, 3], "gpu_count": 4})

    seen_during_post_watch: list[dict] = []
    lease_released_during_post_watch: list[bool] = []
    real_observe = external_watch_mod.observe_external_eval

    async def spy_observe(eng, attempt, cancel, phase, *, final_pass=False):
        if final_pass:
            seen_during_post_watch.append(eng._eval_resource_reservation(0, 0) or {})
            lease_released_during_post_watch.append(eng._gpu_host_lease_handle is None)
        return await real_observe(eng, attempt, cancel, phase, final_pass=final_pass)

    monkeypatch.setattr(external_watch_mod, "observe_external_eval", spy_observe)

    anyio.run(lambda: engine._evaluate(0, anyio.CapacityLimiter(1), None))

    assert released_log == [("release", [0, 1, 2, 3])]
    assert seen_during_post_watch, "final_pass observation should have run"
    assert seen_during_post_watch[0].get("gpu_ids") == []
    assert seen_during_post_watch[0].get("yielded_gpus") == 4
    assert lease_released_during_post_watch == [True], "host GPU lease must be released before post-eval watch"
    terminals = [e for e in engine.store.read_all() if e.type in (EV_NODE_EVALUATED, EV_NODE_FAILED)]
    assert len(terminals) == 1 and terminals[0].type == EV_NODE_EVALUATED


def test_post_eval_final_pass_error_and_timeout_do_not_fail_or_hang_completed_eval(tmp_path, monkeypatch):
    # Part A: checkpoints.ask raises OSError on final_pass -> node still evaluates cleanly
    # with advisory_review == "evidence_unavailable".
    rd_err = tmp_path / "runs" / "final_err"
    engine_err = make_engine(
        rd_err,
        external_harness=True,
        train_monitor=True,
        train_monitor_interval_s=600.0,
        eval_canary=False,
        inline_repair=False,
    )
    engine_err.concurrent_research = False
    engine_err._monitor_cadence = lambda: 600.0
    engine_err._eval_spec = {
        "command": [sys.executable, "-c", "print('step 2 loss=0.3', flush=True); print('{\"metric\": 0.75}', flush=True)"],
        "metric": {"kind": "stdout_json"},
        "timeout": 5,
    }
    (rd_err / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy", external_harness=True, train_monitor=True).model_dump(mode="json")),
        encoding="utf-8",
    )
    engine_err.store.append("run_started", {"run_id": "final_err", "task_id": "toy", "direction": "max"})
    engine_err.store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )

    def boom_ask(*_a, **_kw):
        raise OSError("simulated checkpoint sidecar hiccup")

    monkeypatch.setattr(checkpoints_mod, "ask", boom_ask)
    anyio.run(lambda: engine_err._evaluate(0, anyio.CapacityLimiter(1), None))
    events_err = engine_err.store.read_all()
    term_err = [e for e in events_err if e.type in (EV_NODE_EVALUATED, EV_NODE_FAILED)]
    assert len(term_err) == 1 and term_err[0].type == EV_NODE_EVALUATED
    settled_err = [e for e in events_err if e.type == EV_EVAL_INVOCATION_SETTLED]
    assert settled_err[-1].data["execution_timing"]["advisory_review"] == "evidence_unavailable"

    # Part B: hung post-eval settlement is bounded by _POST_EVAL_WATCH_TIMEOUT_S.
    monkeypatch.undo()
    monkeypatch.setattr("looplab.serve.engine_proc.auto_resume_enabled", lambda: False)
    rd_hang = tmp_path / "runs" / "final_hang"
    engine_hang = make_engine(
        rd_hang,
        external_harness=True,
        train_monitor=True,
        train_monitor_interval_s=600.0,
        eval_canary=False,
        inline_repair=False,
    )
    engine_hang.concurrent_research = False
    engine_hang._monitor_cadence = lambda: 600.0
    engine_hang._eval_spec = {
        "command": [sys.executable, "-c", "print('step 2 loss=0.2', flush=True); print('{\"metric\": 0.88}', flush=True)"],
        "metric": {"kind": "stdout_json"},
        "timeout": 5,
    }
    (rd_hang / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy", external_harness=True, train_monitor=True).model_dump(mode="json")),
        encoding="utf-8",
    )
    engine_hang.store.append("run_started", {"run_id": "final_hang", "task_id": "toy", "direction": "max"})
    engine_hang.store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )

    async def hung_observe(*_a, **_kw):
        await anyio.sleep(10.0)

    monkeypatch.setattr(evaluate_mod, "_POST_EVAL_WATCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(external_watch_mod, "observe_external_eval", hung_observe)
    t0 = time.monotonic()
    anyio.run(lambda: engine_hang._evaluate(0, anyio.CapacityLimiter(1), None))
    assert time.monotonic() - t0 < 2.0
    events_hang = engine_hang.store.read_all()
    term_hang = [e for e in events_hang if e.type in (EV_NODE_EVALUATED, EV_NODE_FAILED)]
    assert len(term_hang) == 1 and term_hang[0].type == EV_NODE_EVALUATED
    settled_hang = [e for e in events_hang if e.type == EV_EVAL_INVOCATION_SETTLED]
    assert settled_hang[-1].data["execution_timing"]["advisory_review"] == "evidence_unavailable"


@pytest.mark.parametrize("strip_record", [False, True])
def test_settled_recovery_recovers_artifact_node_without_rerunning_or_wiping_workdir(
    tmp_path, monkeypatch, strip_record
):
    rd = tmp_path / f"artifact_rec_{strip_record}"
    dead = make_engine(rd, max_nodes=2, eval_canary=False, inline_repair=False)
    dead.concurrent_research = False
    dead._eval_spec = {
        "command": [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('SCORE_COMPLETE.json').write_text('{\"ok\": true}'); print('ARTIFACT_SCORE_COMPLETE')",
        ],
        "metric": {"kind": "stdout_json", "key": "recall"},
        "timeout": 5,
    }
    dead.store.append("run_started", {"run_id": "art", "task_id": "toy", "direction": "max"})
    dead.store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "node_kind": "artifact",
            "job_role": "score",
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )

    async def killed_before_terminal(_a):
        raise _ProcessKilled("killed before terminal")

    dead._eval_write_terminal = killed_before_terminal
    if strip_record:
        monkeypatch.setattr(settled_recovery, "settled_result_record", lambda *_a, **_kw: None)
    with pytest.raises(_ProcessKilled):
        anyio.run(lambda: dead._evaluate(0, anyio.CapacityLimiter(1), None))
    if strip_record:
        monkeypatch.undo()

    marker_file = rd / "nodes" / "node_0" / "SCORE_COMPLETE.json"
    assert marker_file.is_file()
    assert fold(dead.store.read_all()).nodes[0].status is NodeStatus.pending

    resumed = make_engine(rd, max_nodes=2, eval_canary=False, inline_repair=False)
    resumed.concurrent_research = False
    resumed._eval_spec = dead._eval_spec
    rerun_calls = []
    resumed._run_eval = lambda *_a, **_kw: rerun_calls.append(1)
    anyio.run(lambda: resumed._evaluate(0, anyio.CapacityLimiter(1), None))

    assert rerun_calls == [], "completed artifact node must not re-run on resume"
    assert marker_file.is_file(), "completed artifact workdir must be preserved"
    events = resumed.store.read_all()
    rec = [e.data for e in events if e.type == EV_EVAL_INVOCATION_RECOVERED]
    assert len(rec) == 1 and rec[0]["action"] == settled_recovery.RECOVERY_FINALIZED
    expected_source = settled_recovery.SOURCE_WORKDIR_LOG if strip_record else settled_recovery.SOURCE_SETTLE_RECORD
    assert rec[0]["source"] == expected_source
    term = [e for e in events if e.type in (EV_NODE_EVALUATED, EV_NODE_FAILED)]
    assert len(term) == 1 and term[0].type == EV_NODE_EVALUATED
    assert term[0].data.get("metric") is None


def test_post_stage_beacon_pre_settle_gap_recovers_artifact_and_charges_full_stage_seconds(tmp_path):
    """Post-beacon pre-settle window: subprocess exited 0 (`phase_progress` stage='eval',
    phase='stage', status='finished', outcome='ok', exit_code=0), then engine died BEFORE
    `eval_invocation_settled`."""
    # 1) Pure accounting + recovery detection over exact event sequence (canary 1.751s + stage 2104.315s).
    events = [
        Event(v=1, seq=1, ts=1000.0, type=EV_EVAL_CANARY_STARTED, data={"node_id": 90, "generation": 0, "attempt": 0}),
        Event(
            v=1,
            seq=2,
            ts=1001.751,
            type=EV_EVAL_CANARY_FINISHED,
            data={"node_id": 90, "generation": 0, "attempt": 0, "outcome": "passed", "eval_seconds": 1.751},
        ),
        Event(
            v=1,
            seq=3,
            ts=1002.0,
            type=EV_EVAL_INVOCATION_CLAIMED,
            data={"node_id": 90, "generation": 0, "attempt": 0, "invocation_id": "inv-90", "canary_ran": True},
        ),
        Event(
            v=1,
            seq=4,
            ts=1002.0,
            type=EV_PHASE_PROGRESS,
            data={
                "stage": PROGRESS_STAGE_EVAL,
                "phase": "stage",
                "status": "started",
                "node_id": 90,
                "generation": 0,
                "name": "eval",
                "index": 0,
                "total": 1,
            },
        ),
        Event(
            v=1,
            seq=5,
            ts=3106.315,
            type=EV_PHASE_PROGRESS,
            data={
                "stage": PROGRESS_STAGE_EVAL,
                "phase": "stage",
                "status": "finished",
                "node_id": 90,
                "generation": 0,
                "name": "eval",
                "index": 0,
                "total": 1,
                "outcome": "ok",
                "exit_code": 0,
            },
        ),
    ]
    prior = _durable_prior_seconds(events, 90, 0)
    assert prior == pytest.approx(2106.066, abs=1e-3)
    found = settled_ok_awaiting_terminal(events, 90, 0)
    assert found is not None
    claim_ts, beacon_seq, beacon_data = found
    assert claim_ts == 1002.0 and beacon_seq == 5 and beacon_data["_from_stage_beacon"] is True

    # Verify abandoned_lifecycle_charges also charges stage-beacon seconds when a reset abandons gen 0
    # even without a canary row.
    reset_events = [
        Event(v=1, seq=0, ts=999.0, type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(
            v=1,
            seq=1,
            ts=999.5,
            type="node_created",
            data={"node_id": 90, "parent_ids": [], "operator": "draft", "idea": Idea(operator="draft").model_dump(mode="json"), "code": "x"},
        ),
        *events[2:],  # no canary rows — only claim + stage started/finished
        Event(v=1, seq=6, ts=3110.0, type=EV_NODE_RESET, data={"node_id": 90, "generation": 0, "new_generation": 1}),
    ]
    charges = abandoned_lifecycle_charges(reset_events, fold(reset_events))
    assert len(charges) == 1
    assert charges[0][0] == 90 and charges[0][1] == 0
    assert charges[0][2] == pytest.approx(2104.315, abs=1e-3)

    # 2) End-to-end Engine recovery when killed inside post-eval settlement BEFORE `eval_invocation_settled`.
    rd = tmp_path / "node90_e2e"
    dead = make_engine(
        rd,
        max_nodes=2,
        external_harness=True,
        train_monitor=True,
        train_monitor_interval_s=600.0,
        eval_canary=False,
        inline_repair=False,
    )
    dead.concurrent_research = False
    dead._monitor_cadence = lambda: 600.0
    dead._eval_spec = {
        "command": [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; time.sleep(0.08); "
            "Path('SCORE_COMPLETE.json').write_text('{\"rows\": 50682}'); print('ARTIFACT_SCORE_COMPLETE', flush=True)",
        ],
        "metric": {"kind": "stdout_json", "key": "recall"},
        "timeout": 5,
    }
    dead.store.append("run_started", {"run_id": "node90", "task_id": "toy", "direction": "max"})
    dead.store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "node_kind": "artifact",
            "job_role": "fit",  # force final_pass observe_external_eval to fire so we can kill pre-settle
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )

    async def kill_in_post_watch(*_a, **_kw):
        raise _ProcessKilled("killed after stage finished beacon, before eval_invocation_settled")

    dead._settle_eval_invocation = kill_in_post_watch
    with pytest.raises(_ProcessKilled):
        anyio.run(lambda: dead._evaluate(0, anyio.CapacityLimiter(1), None))

    mid_events = dead.store.read_all()
    assert not [e for e in mid_events if e.type == EV_EVAL_INVOCATION_SETTLED]
    assert [
        e for e in mid_events
        if e.type == EV_PHASE_PROGRESS and e.data.get("phase") == "stage" and e.data.get("status") == "finished"
    ]
    assert (rd / "nodes" / "node_0" / "SCORE_COMPLETE.json").is_file()

    resumed = make_engine(rd, max_nodes=2, external_harness=True, eval_canary=False, inline_repair=False)
    resumed.concurrent_research = False
    resumed._eval_spec = dead._eval_spec
    reruns = []
    resumed._run_eval = lambda *_a, **_kw: reruns.append(1)
    anyio.run(lambda: resumed._evaluate(0, anyio.CapacityLimiter(1), None))

    assert reruns == [], "single-command artifact with finished stage beacon must finalize from workdir_log"
    assert (rd / "nodes" / "node_0" / "SCORE_COMPLETE.json").is_file()
    final_events = resumed.store.read_all()
    eval_terms = [e for e in final_events if e.type == EV_NODE_EVALUATED]
    assert len(eval_terms) == 1
    assert eval_terms[0].data["eval_seconds"] >= 0.05


def test_job_role_aware_eval_log_plan_and_monitor_rules_any_of_with_rows(tmp_path):
    # 1) eval_log_plan assigns LOG_ROLE_SCORE to score/eval/audit single-command nodes,
    # LOG_ROLE_WORK to prep/smoke/gate, and LOG_ROLE_TRAINING to fit/None.
    for role_name in ("score", "eval", "audit"):
        plan = eval_log_plan([], job_role=role_name)
        assert plan.roles["eval.log"] == (None, LOG_ROLE_SCORE)
        one_stage = eval_log_plan([{"name": "run_score"}], job_role=role_name)
        assert one_stage.roles["run_score.log"] == ("run_score", LOG_ROLE_SCORE)
    for role_name in ("prep", "smoke", "gate"):
        plan = eval_log_plan([], job_role=role_name)
        assert plan.roles["eval.log"] == (None, LOG_ROLE_WORK)
    for role_name in ("fit", None):
        plan = eval_log_plan([], job_role=role_name)
        assert plan.roles["eval.log"] == (None, LOG_ROLE_TRAINING)

    # End-to-end: a `job_role="score"` node with `train_monitor=True` does NOT open spurious
    # train_monitor checkpoint questions!
    rd = tmp_path / "runs" / "score_no_q"
    engine = make_engine(
        rd,
        external_harness=True,
        train_monitor=True,
        train_monitor_interval_s=0.02,
        eval_canary=False,
        inline_repair=False,
    )
    engine.concurrent_research = False
    engine._monitor_cadence = lambda: 0.02
    engine._eval_spec = {
        "command": [
            sys.executable,
            "-c",
            "import time; print('{\"rows\": 4096}', flush=True); time.sleep(0.08); print('JOINT_SCORE_OK', flush=True)",
        ],
        "metric": {"kind": "stdout_json", "key": "recall"},
        "timeout": 5,
    }
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy", external_harness=True, train_monitor=True).model_dump(mode="json")),
        encoding="utf-8",
    )
    engine.store.append("run_started", {"run_id": "score_no_q", "task_id": "toy", "direction": "max"})
    engine.store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "node_kind": "artifact",
            "job_role": "score",
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )
    anyio.run(lambda: engine._evaluate(0, anyio.CapacityLimiter(1), None))
    assert not (rd / "harness_checkpoints.jsonl").exists()
    beacons = [
        e.data for e in engine.store.read_all()
        if e.type == EV_PHASE_PROGRESS and e.data.get("phase") == "stage"
    ]
    assert beacons and all(b["role"] == LOG_ROLE_SCORE for b in beacons)

    # 2) monitor_rules `any_of` + `rows` progress markers + per-rule progress tracking.
    with pytest.raises(RulesRefused):
        validate_rules({
            "train_monitor": {
                "routine_if": {"loss_finite": True},
                "any_of": [{"routine_if": {"loss_finite": True}}],
            }
        })
    rules = write_rules(
        rd,
        {
            "train_monitor": {
                "any_of": [
                    {
                        "rule_id": "fit-loss-progress",
                        "routine_if": {
                            "loss_finite": True,
                            "progress_advances": True,
                            "forbid_patterns": ["Traceback", "CUDA out of memory"],
                        },
                    },
                    {
                        "rule_id": "score-rows-progress",
                        "routine_if": {
                            "progress_advances": True,
                            "require_patterns": [r'"rows"\s*:\s*\d+'],
                            "forbid_patterns": ["Traceback", "CUDA out of memory"],
                        },
                    },
                ]
            }
        },
    )
    v1 = eval_rule(
        rules,
        {"phase_id": "train_monitor", "observation": '{"chunk": 1, "rows": 16384}'},
        previous_by_rule={},
    )
    assert v1 is not None and v1["rule_id"] == "score-rows-progress" and v1["facts"]["progress"] == 16384.0
    # Stalled rows on the same branch returns None; advancing rows returns continue;
    # meanwhile the fit branch's own progress is tracked independently!
    assert (
        eval_rule(
            rules,
            {"phase_id": "train_monitor", "observation": '{"chunk": 1, "rows": 16384}'},
            previous_by_rule={"score-rows-progress": 16384.0},
        )
        is None
    )
    v2 = eval_rule(
        rules,
        {"phase_id": "train_monitor", "observation": "step 10 loss=0.45"},
        previous_by_rule={"score-rows-progress": 16384.0},
    )
    assert v2 is not None and v2["rule_id"] == "fit-loss-progress" and v2["facts"]["progress"] == 10.0


def test_antigravity_preset_and_harness_progress_notify_and_settlement_visibility(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-token-123456")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-token-654321")
    root = tmp_path / "runs"
    rd = root / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")), encoding="utf-8")
    (rd / "task.snapshot.json").write_text(
        json.dumps({"kind": "toy", "id": "demo", "direction": "max", "goal": "g"}),
        encoding="utf-8",
    )

    runner = CliRunner()
    both = runner.invoke(cli_app, ["agent-notify", "--run-root", str(root), "--codex", "--antigravity"])
    assert both.exit_code != 0
    ok = runner.invoke(
        cli_app,
        ["agent-notify", "--run-root", str(root), "--antigravity"],
    )
    assert ok.exit_code == 0, ok.output
    cfg = ae.read_notify_config(root, ae.ANTIGRAVITY_HOOK)
    assert cfg is not None and cfg["preset"] == "antigravity"
    assert cfg["command"] == list(ae.ANTIGRAVITY_PRESET_COMMAND)

    # Seed run with a pending node whose stage finished beacon landed without `eval_invocation_settled`.
    from looplab.events.eventstore import EventStore

    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "uid-demo", "task_id": "demo", "direction": "max"})
    store.append(
        "node_created",
        {
            "node_id": 0,
            "parent_ids": [],
            "operator": "draft",
            "idea": Idea(operator="draft").model_dump(mode="json"),
            "code": "print(1)",
        },
    )
    store.append(
        EV_EVAL_INVOCATION_CLAIMED,
        {"node_id": 0, "generation": 0, "attempt": 0, "invocation_id": "inv-0"},
    )
    store.append(
        EV_PHASE_PROGRESS,
        {
            "stage": PROGRESS_STAGE_EVAL,
            "phase": "stage",
            "status": "finished",
            "node_id": 0,
            "generation": 0,
            "name": "eval",
            "index": 0,
            "total": 1,
            "outcome": "ok",
            "exit_code": 0,
        },
    )

    # Deliver via Antigravity preset using `conversation` alias, then simulate `no_reaction` ledger row.
    ae.write_run_notify(rd, variables={"conversation": "00000000-0000-4000-8000-000000000001"}, hook="antigravity")
    ae.write_notify_config(root, {**ae.antigravity_preset(), "debounce_s": 0}, ae.ANTIGRAVITY_HOOK)
    delivered_argv = []
    pump = ae.AgentEventPump(
        SimpleNamespace(root=root),
        clock=lambda: time.time() + 30.0,
        runner=lambda argv, _t: (delivered_argv.append(argv) or {"ok": True, "exit_code": 0}),
    )
    ae.append_events(root, [{"run_id": "demo", "kind": "node_evaluated", "key": "k1", "summary": "done"}])
    pump.deliver()
    assert len(delivered_argv) == 1
    assert delivered_argv[0][:3] == ["agy", "--conversation", "00000000-0000-4000-8000-000000000001"]

    ae._append_ledger(
        root,
        {
            "ts": time.time() + 100.0,
            "attempt": 2,
            "status": "no_reaction",
            "hook": "antigravity",
            "event_ids": [0],
            "runs": ["demo"],
            "kinds": ["node_evaluated"],
            "error": "no agent request reached the run after the reminder",
        },
    )

    app = make_app(root)
    client = TestClient(app)
    monkeypatch.setattr("looplab.engine.run_lifecycle.engine_liveness", lambda _rd: True)
    gen = run_generation_token(store.read_all())
    headers = {"X-LoopLab-Token": "agent-token-654321"}

    full = client.get(f"/api/runs/demo/harness-progress?expected_generation={gen}", headers=headers)
    assert full.status_code == 200, full.text
    full_body = full.json()
    assert full_body["execution"]["agent_connection"] == "not_measured"
    pending_settle = full_body["execution"]["post_eval_settlement_pending"]
    assert len(pending_settle) == 1
    assert pending_settle[0]["node_id"] == 0 and pending_settle[0]["outcome"] == "ok"
    assert full_body["agent_notify"]["latest_delivery_status"] == "no_reaction"
    assert "no_reaction" in (full_body["agent_notify"]["warning"] or "")

    brief = client.get(f"/api/runs/demo/harness-progress?expected_generation={gen}&brief=true", headers=headers)
    assert brief.status_code == 200, brief.text
    brief_body = brief.json()
    assert brief_body["agent_notify"]["latest_delivery_status"] == "no_reaction"
    assert brief_body["execution"]["post_eval_settlement_pending"] == pending_settle
    assert HarnessAPI._valid_progress(brief_body) is True
