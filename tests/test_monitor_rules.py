"""Declared routine answers for checkpoints, executed by LoopLab (doc 78 §4; audit L-05).

The agent writes its routine policy once; LoopLab answers matching questions itself and SAYS it
was a rule, escalates everything else, and a failed evaluation never waits on a moot question."""
from __future__ import annotations

import json
import math
import threading
import time
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.harness import monitor_rules as mr
from looplab.harness.checkpoints import answer_for, ask, close_unanswered
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app

RULES = {"train_monitor": {"routine_if": {"loss_finite": True, "progress_advances": True,
                                          "forbid_patterns": ["Traceback", "out of memory"]}},
         "stage_check": {"routine_if": {"require_patterns": ["Saved checkpoint"]},
                         "rule_id": "saved-ckpt"}}


def seeded(tmp_path):
    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(json.dumps(
        Settings(backend="toy", external_harness=True).model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "task", "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    return rd, store


def test_loss_and_progress_readers():
    assert mr.last_loss("step 10 loss=0.5\nstep 20 loss: 0.25") == 0.25
    assert math.isnan(mr.last_loss("{'loss': nan, 'lr': 1e-4}"))
    assert mr.last_loss("train_loss 1.2e-3") == pytest.approx(1.2e-3)
    assert mr.last_loss("no metrics yet") is None
    assert mr.progress_marker("global_step=1200 loss=1") == 1200
    assert mr.progress_marker(" 45%|████ | 450/1000 [00:10<00:12]") == 450
    assert mr.progress_marker("epoch 2.5") == 2.5
    assert mr.progress_marker("loading data") is None


def test_rules_answer_routine_and_escalate_anomalies():
    rules = mr.validate_rules(RULES)
    q = {"phase_id": "train_monitor", "observation": "step 100 loss=0.9"}
    first = mr.evaluate(rules, q, previous_progress=None)
    assert first["verdict"] == "continue" and first["facts"]["progress"] == 100
    assert mr.evaluate(rules, {**q, "observation": "step 200 loss=0.8"},
                       previous_progress=100)["verdict"] == "continue"
    assert mr.evaluate(rules, {**q, "observation": "step 100 loss=0.8"},
                       previous_progress=100) is None                    # no progress
    assert mr.evaluate(rules, {**q, "observation": "step 300 loss=nan"},
                       previous_progress=200) is None                    # non-finite loss
    assert mr.evaluate(rules, {**q, "observation": "step 300 loss=0.7\nTraceback (most"},
                       previous_progress=200) is None                    # forbidden pattern
    assert mr.evaluate(rules, {"phase_id": "deadline_grace", "observation": "x"},
                       previous_progress=None) is None                   # always the agent's
    stage = mr.evaluate(rules, {"phase_id": "stage_check", "observation": "Saved checkpoint to x"},
                        previous_progress=None)
    assert stage["verdict"] == "proceed" and stage["rule_id"] == "saved-ckpt"


def test_rule_validation_refuses_unknown_vocab_and_bad_regex():
    for body in ({"deadline_grace": {"routine_if": {"loss_finite": True}}},
                 {"train_monitor": {"routine_if": {"abort_if": True}}},
                 {"train_monitor": {"routine_if": {"forbid_patterns": ["("]}}},
                 {"train_monitor": {"routine_if": {}}},
                 {"train_monitor": {"routine_if": {"loss_finite": False}}}):
        with pytest.raises(mr.RulesRefused):
            mr.validate_rules(body)


def test_ask_is_answered_by_rule_in_the_same_breath_and_says_so(tmp_path):
    rd, store = seeded(tmp_path)
    mr.write_rules(rd, RULES)
    q1 = ask(rd, 0, 0, "train_monitor", stage="train", observation="step 10 loss=1.0")
    a1 = answer_for(rd, q1["checkpoint_id"])
    assert a1["verdict"] == "continue" and a1["answerer"] == "rule"
    assert a1["rule_facts"]["progress"] == 10
    # The next tick must show progress beyond what the last rule saw.
    q2 = ask(rd, 0, 0, "train_monitor", stage="train", observation="step 10 loss=0.9")
    assert answer_for(rd, q2["checkpoint_id"]) is None
    q3 = ask(rd, 0, 0, "train_monitor", stage="train", observation="step 20 loss=0.8")
    assert answer_for(rd, q3["checkpoint_id"])["answerer"] == "rule"
    q4 = ask(rd, 0, 0, "stage_check", stage="train", observation="done, no save")
    assert answer_for(rd, q4["checkpoint_id"]) is None


def test_agent_and_owner_answers_are_attributed_and_rules_route(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-token-123456")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-token-654321")
    rd, store = seeded(tmp_path)
    client = TestClient(make_app(tmp_path / "runs"))
    agent = {"X-LoopLab-Token": "agent-token-654321"}
    owner = {"X-LoopLab-Token": "owner-token-123456"}
    assert client.get("/api/runs/demo/harness-monitor-rules", headers=agent).json()["rules"] is None
    bad = client.put("/api/runs/demo/harness-monitor-rules",
                     json={"train_monitor": {"routine_if": {"kill_if": True}}}, headers=agent)
    assert bad.status_code == 400
    ok = client.put("/api/runs/demo/harness-monitor-rules", json=RULES, headers=agent)
    assert ok.status_code == 200 and ok.json()["rules"]["declared_by"] == "agent"
    generation = run_generation_token(store.read_all())
    open_q = ask(rd, 0, 0, "stage_check", stage="train", observation="no save line")
    body = {"expected_generation": generation, "checkpoint_id": open_q["checkpoint_id"],
            "action_id": "a1", "verdict": "proceed", "failure_kind": "", "reason": "looked at it"}
    assert client.post("/api/runs/demo/harness-checkpoints", json=body, headers=agent).status_code == 200
    assert answer_for(rd, open_q["checkpoint_id"])["answerer"] == "agent"
    other = ask(rd, 0, 0, "stage_check", stage="train", observation="still no save line")
    body = {**body, "checkpoint_id": other["checkpoint_id"], "action_id": "a2"}
    assert client.post("/api/runs/demo/harness-checkpoints", json=body, headers=owner).status_code == 200
    assert answer_for(rd, other["checkpoint_id"])["answerer"] == "human"
    assert client.put("/api/runs/demo/harness-monitor-rules", json={},
                      headers=agent).json()["rules"] is None


def test_a_failed_evaluation_closes_its_live_questions_instead_of_waiting(tmp_path):
    from looplab.engine.external_watch import attempt_failed, settle_external_observations

    rd, store = seeded(tmp_path)
    q = ask(rd, 0, 0, "train_monitor", stage="train", observation="step 5 loss=1")
    engine = SimpleNamespace(run_dir=rd, _eval_intervention_seen=lambda *a: pytest.fail(
        "a failed evaluation must not wait for an answer"))
    attempt = SimpleNamespace(_seen={}, _live_questions=[q["checkpoint_id"]], node_id=0,
                              generation=0, start_seq=0, node=SimpleNamespace(idea=None),
                              res=SimpleNamespace(exit_code=-11, timed_out=False, metric=None))
    assert attempt_failed(attempt.res)
    started = time.monotonic()
    anyio.run(settle_external_observations, engine, attempt)
    assert time.monotonic() - started < 5
    answer = answer_for(rd, q["checkpoint_id"])
    assert answer["answerer"] == "engine" and "moot" in answer["reason"]
    assert close_unanswered(rd, [q["checkpoint_id"]], "again") == []   # idempotent


def test_attempt_failed_truth_table():
    from looplab.engine.external_watch import answer_source, attempt_failed

    ok = SimpleNamespace(exit_code=0, timed_out=False, metric=0.5)
    assert not attempt_failed(ok) and not attempt_failed(None)
    assert attempt_failed(SimpleNamespace(exit_code=0, timed_out=False, metric=None))
    assert attempt_failed(SimpleNamespace(exit_code=0, timed_out=True, metric=0.1))
    assert answer_source({"answerer": "rule"}) == "rule"
    assert answer_source({"answerer": "human"}) == "operator"
    assert answer_source({}) == "external_agent"
