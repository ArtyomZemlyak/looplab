"""Typed status waits and resumable continuous assistant work."""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.serve.assistant import WatchTools, WorkCheckpointTools
from looplab.serve.assistant_watch import (
    WATCH_MIN_INTERVAL_S, SessionWatches, WatchRefusal, WatchService, WatchStore, describe_trigger,
    normalize_target_status_trigger, normalize_work_checkpoint)
from looplab.serve.routers.assistant import watch_observe_target


def _service(store, observations, results, *, calls=None, appended=None):
    calls = calls if calls is not None else []
    appended = appended if appended is not None else []

    def _observe(target):
        value = observations["value"]
        return value(target) if callable(value) else value

    def _turn(record, instruction):
        calls.append((record, instruction))
        return results.pop(0)

    return WatchService(
        store, observe_run=lambda _run: None, observe_target=_observe, run_turn_fn=_turn,
        append_turn=lambda session, turn: appended.append((session, turn)))


def _experiment(status="pending", *, generation="g1", attempt=0):
    return {"target": {"kind": "experiment", "run": "demo", "node": 3},
            "present": True, "run_present": True, "run_generation": generation,
            "experiment": 3, "attempt": attempt, "status": status, "states": [status]}


def _checkpoint(status, summary, *, wait=None, todos=None):
    out = {"status": status, "summary": summary, "todos": todos or []}
    if wait is not None:
        out["wait"] = wait
    return out


def test_target_status_has_a_closed_typed_vocabulary():
    trigger = normalize_target_status_trigger({
        "kind": "target_status",
        "target": {"kind": "stage", "run": "demo", "node": 3, "stage": "train"},
        "until": ["ok", "timeout"],
    })
    assert trigger["target"] == {
        "kind": "stage", "run": "demo", "node": 3, "stage": "train"}
    assert "experiment 3 stage train" in describe_trigger(trigger)
    with pytest.raises(WatchRefusal, match="unknown stage state"):
        normalize_target_status_trigger({**trigger, "until": ["complete"]})
    with pytest.raises(WatchRefusal, match="stage name"):
        normalize_target_status_trigger({
            "kind": "target_status",
            "target": {"kind": "stage", "run": "demo", "node": 3}, "until": ["ok"]})


def test_a_typed_status_fires_once_and_pins_generation_and_attempt(tmp_path):
    store = WatchStore(tmp_path)
    observations = {"value": _experiment()}
    calls = []
    service = _service(
        store, observations,
        [{"ok": True, "reply": "experiment finished", "steps": [], "todos": []}], calls=calls)
    record = store.arm(
        session="s1", instruction="summarize it",
        trigger={"kind": "target_status",
                 "target": {"kind": "experiment", "run": "demo", "node": 3},
                 "until": ["evaluated"]})

    service.tick(now=record["created"])
    waiting = store.get(record["id"])
    assert calls == []
    assert waiting["target_seen"] is True
    assert waiting["target_run_generation"] == "g1" and waiting["target_attempt"] == 0

    observations["value"] = _experiment("evaluated")
    service.tick(now=waiting["next_due"])
    assert len(calls) == 1
    assert store.get(record["id"])["status"] == "done"


def test_a_reset_or_retry_is_not_silently_followed(tmp_path):
    store = WatchStore(tmp_path)
    observations = {"value": _experiment()}
    calls, appended = [], []
    service = _service(store, observations, [], calls=calls, appended=appended)
    record = store.arm(
        session="s1", instruction="report",
        trigger={"kind": "target_status",
                 "target": {"kind": "experiment", "run": "demo", "node": 3},
                 "until": ["evaluated"]})
    service.tick(now=record["created"])

    observations["value"] = _experiment("pending", generation="g2", attempt=0)
    service.tick(now=store.get(record["id"])["next_due"])
    settled = store.get(record["id"])
    assert settled["status"] == "failed" and "generation" in settled["last_error"]
    assert calls == []
    assert appended[-1][1]["watch"]["notice"] is True


def test_continuous_work_resumes_from_a_durable_checkpoint_after_restart(tmp_path):
    store = WatchStore(tmp_path)
    observations = {"value": _experiment()}
    calls, appended = [], []
    results = [
        {"ok": True, "reply": "implemented the first half", "steps": [], "todos": [],
         "work_checkpoint": _checkpoint(
             "continue", "Implemented the parser; next add integration tests.",
             todos=[{"content": "add integration tests", "status": "in_progress"}])},
        {"ok": True, "reply": "all checks pass", "steps": [], "todos": [],
         "work_checkpoint": _checkpoint(
             "done", "Integration tests pass and the goal is verified.",
             todos=[{"content": "add integration tests", "status": "completed"}])},
    ]
    service = _service(store, observations, results, calls=calls, appended=appended)
    record = store.arm(
        session="s1", instruction="implement and verify the feature",
        trigger={"kind": "work", "every_s": WATCH_MIN_INTERVAL_S,
                 "initial_todos": [{"content": "implement parser", "status": "in_progress"}]},
        max_wakeups=4)

    service.tick(now=record["created"])
    saved = store.get(record["id"])
    assert saved["status"] == "armed" and saved["wakeups"] == 1
    assert saved["checkpoint"]["summary"].startswith("Implemented the parser")

    # A fresh store/service is the server-restart boundary. The second prompt comes only from disk.
    fresh = WatchStore(tmp_path)
    fresh_service = _service(fresh, observations, results, calls=calls, appended=appended)
    fresh_service.tick(now=saved["next_due"])
    settled = fresh.get(record["id"])
    assert settled["status"] == "done" and settled["wakeups"] == 2
    assert "Implemented the parser" in calls[1][1]
    assert "add integration tests" in calls[1][1]
    assert len(appended) == 2 and appended[-1][1]["work_checkpoint"]["status"] == "done"


def test_waiting_work_uses_server_polls_and_resumes_only_when_the_status_matches(tmp_path):
    store = WatchStore(tmp_path)
    observations = {"value": _experiment()}
    calls = []
    wait = {"target": {"kind": "experiment", "run": "demo", "node": 3},
            "until": ["evaluated"]}
    results = [
        {"ok": True, "reply": "submitted; waiting", "steps": [], "todos": [],
         "work_checkpoint": _checkpoint("waiting", "Experiment 3 was submitted.", wait=wait)},
        {"ok": True, "reply": "verified result", "steps": [], "todos": [],
         "work_checkpoint": _checkpoint("done", "The result was inspected and verified.")},
    ]
    service = _service(store, observations, results, calls=calls)
    record = store.arm(session="s1", instruction="submit, inspect, and report",
                       trigger={"kind": "work", "every_s": WATCH_MIN_INTERVAL_S},
                       max_wakeups=4)
    service.tick(now=record["created"])
    waiting = store.get(record["id"])
    assert waiting["checkpoint"]["status"] == "waiting" and len(calls) == 1
    assert waiting["work_wait_seen"] is True
    assert waiting["work_wait_run_generation"] == "g1" and waiting["work_wait_attempt"] == 0

    service.tick(now=waiting["next_due"])
    still_waiting = store.get(record["id"])
    assert len(calls) == 1, "an unmet dependency must not spend a model call"
    assert still_waiting["last_observation"]["status"] == "pending"

    observations["value"] = _experiment("evaluated")
    service.tick(now=still_waiting["next_due"])
    assert len(calls) == 2
    assert store.get(record["id"])["status"] == "done"


def test_work_wait_fences_the_target_at_handoff_before_the_first_poll(tmp_path):
    store = WatchStore(tmp_path)
    observations = {"value": _experiment()}
    calls = []
    wait = {"target": {"kind": "experiment", "run": "demo", "node": 3},
            "until": ["evaluated"]}
    service = _service(store, observations, [{
        "ok": True, "reply": "submitted; waiting", "steps": [], "todos": [],
        "work_checkpoint": _checkpoint("waiting", "Experiment 3 was submitted.", wait=wait),
    }], calls=calls)
    record = store.arm(session="s1", instruction="submit and verify",
                       trigger={"kind": "work", "every_s": WATCH_MIN_INTERVAL_S})
    service.tick(now=record["created"])
    waiting = store.get(record["id"])
    assert waiting["work_wait_run_generation"] == "g1"

    observations["value"] = _experiment("pending", generation="g2")
    service.tick(now=waiting["next_due"])
    settled = store.get(record["id"])
    assert settled["status"] == "blocked" and "generation" in settled["last_error"]
    assert len(calls) == 1, "a replacement target must not trigger another autonomous cycle"


def test_missing_or_malformed_handoff_blocks_instead_of_guessing_continue(tmp_path):
    store = WatchStore(tmp_path)
    calls, appended = [], []
    service = _service(
        store, {"value": _experiment()},
        [{"ok": True, "reply": "I changed something", "steps": [], "todos": []}],
        calls=calls, appended=appended)
    record = store.arm(session="s1", instruction="make the change",
                       trigger={"kind": "work", "every_s": WATCH_MIN_INTERVAL_S})
    service.tick(now=record["created"])
    settled = store.get(record["id"])
    assert settled["status"] == "blocked"
    assert "not safely resumable" in settled["last_error"]
    assert len(calls) == 1 and appended[-1][1]["watch"]["notice"] is True


def test_a_work_record_without_a_valid_durable_checkpoint_is_not_executable(tmp_path):
    store = WatchStore(tmp_path)
    record = store.arm(session="s1", instruction="make the change",
                       trigger={"kind": "work", "every_s": WATCH_MIN_INTERVAL_S})
    assert WatchStore._valid(record) is True
    assert WatchStore._valid({**record, "checkpoint": None}) is False


def test_checkpoint_tool_and_scheduler_share_the_same_validation():
    tool = WorkCheckpointTools()
    with pytest.raises(WatchRefusal, match="complete TODO list"):
        normalize_work_checkpoint({"status": "continue", "summary": "missing TODO handoff"})
    with pytest.raises(WatchRefusal, match="too large for a resumable handoff"):
        normalize_work_checkpoint({
            "status": "continue", "summary": "oversized TODO handoff",
            "todos": [{"content": "x" * 1000, "status": "pending"} for _ in range(7)]})
    refused = tool.execute("checkpoint_work", {
        "status": "waiting", "summary": "waiting", "todos": [],
        "wait": {"target": {"kind": "stage", "run": "r", "node": 1, "stage": "train"},
                 "until": ["not-a-state"]},
    })
    assert "checkpoint refused" in refused and tool.checkpoints == []
    accepted = tool.execute("checkpoint_work", {
        "status": "waiting", "summary": "training is running", "todos": [],
        "wait": {"target": {"kind": "stage", "run": "r", "node": 1, "stage": "train"},
                 "until": ["ok", "fail"]},
    })
    assert "recorded: waiting" in accepted
    assert normalize_work_checkpoint(tool.checkpoints[-1]) == tool.checkpoints[-1]
    second = tool.execute("checkpoint_work", {
        "status": "done", "summary": "a conflicting second decision", "todos": []})
    assert "already recorded" in second and len(tool.checkpoints) == 1

    with pytest.raises(WatchRefusal, match="only valid for a continue"):
        normalize_work_checkpoint({
            "status": "done", "summary": "done", "todos": [],
            "next_in_s": WATCH_MIN_INTERVAL_S})
    with pytest.raises(WatchRefusal, match="only valid for a waiting"):
        normalize_work_checkpoint({
            "status": "continue", "summary": "continue", "todos": [], "wait": accepted})


def test_agent_tools_arm_typed_status_and_continuous_work_records(tmp_path):
    store = WatchStore(tmp_path)
    tools = WatchTools(SessionWatches(store, "s1", "plan"))
    assert "armed" in tools.execute("watch_status", {
        "target": {"kind": "stage", "run": "demo", "node": 3, "stage": "train"},
        "until": ["ok", "fail"], "instruction": "inspect the result"})
    assert "armed" in tools.execute("work_until_done", {
        "goal": "finish and verify the integration",
        "todos": [{"content": "run integration checks", "status": "pending"}],
        "every_s": WATCH_MIN_INTERVAL_S, "max_cycles": 5})

    typed, work = store.list(session="s1")
    assert typed["trigger"]["kind"] == "target_status"
    assert work["trigger"]["kind"] == "work" and work["max_wakeups"] == 5
    assert work["checkpoint"]["todos"][0]["content"] == "run integration checks"
    assert "cycles" in tools.execute("list_watches", {})


def test_router_projects_only_the_target_status_and_identity(tmp_path):
    class FakeServer:
        def run_dir(self, run_id):
            assert run_id == "demo"
            return Path(tmp_path) / run_id

        def state_payload(self, _rd):
            return {"generation": "gen-a", "state": {"phase": "search", "engine_running": True,
                    "nodes": {"3": {"attempt": 2, "status": "pending", "stages": [
                        {"name": "train", "status": "ok", "seconds": 12.0}]}}}}

    observation = watch_observe_target(
        FakeServer(), {"kind": "stage", "run": "demo", "node": 3, "stage": "train"})
    assert observation == {
        "target": {"kind": "stage", "run": "demo", "node": 3, "stage": "train"},
        "run_present": True, "run_generation": "gen-a", "present": True,
        "experiment": 3, "attempt": 2, "stage": "train", "status": "ok", "states": ["ok"],
    }
    assert "seconds" not in observation, "durable observations stay bounded projections"
