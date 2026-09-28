"""External stage and live-eval decisions use the real run ledger and HTTP boundary."""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
import anyio

from looplab.core.config import Settings
from looplab.engine.eval_stages import EvalStagesMixin
from looplab.engine.eval_log_plan import eval_log_plan, snapshot_training_logs
from looplab.engine.external_watch import observe_external_eval
from looplab.engine.asha_monitor import IntermediateSample
from looplab.events.eventstore import EventStore
from looplab.harness.checkpoints import answer_for, ask
from looplab.runtime.command_eval import run_command_eval
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app


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
    return rd, store, TestClient(make_app(tmp_path / "runs"))


def test_stage_check_blocks_and_applies_lifecycle_fenced_verdict(tmp_path):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    worker = SimpleNamespace(run_dir=rd, _redact=lambda text: text)
    node = SimpleNamespace(id=0, attempt=0)
    fn = EvalStagesMixin._external_stage_check_fn(
        worker, node, rd, [{"name": "train", "check": True}], threading.Event())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, "train", "Saved checkpoint", "loss decreases")
        for _ in range(100):
            response = client.get("/api/runs/demo/harness-checkpoints",
                                  params={"expected_generation": generation})
            assert response.status_code == 200, response.text
            questions = response.json()["pending"]
            if questions:
                break
            time.sleep(0.01)
        assert len(questions) == 1 and not future.done()
        q = questions[0]
        assert q["phase_id"] == "stage_check" and q["expectation"] == "loss decreases"
        body = {"expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
                "action_id": "stage-1", "verdict": "fail",
                "failure_kind": "declared_condition_violated", "reason": "Loss never decreased"}
        assert client.post("/api/runs/demo/harness-checkpoints", json={**body,
                           "expected_generation": "0" * 64}).status_code == 409
        assert client.post("/api/runs/demo/harness-checkpoints", json=body).status_code == 200
        assert client.post("/api/runs/demo/harness-checkpoints", json=body).json()["replayed"]
        verdict = future.result(timeout=5)
        assert verdict.kind == "declared_condition_violated"
        assert not client.get("/api/runs/demo/harness-checkpoints",
                              params={"expected_generation": generation}).json()["pending"]


def test_monitor_answer_cannot_claim_kill_without_authority_or_after_terminal(tmp_path):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    q = ask(rd, 0, 0, "asha_live", observation="objective=0.4", kill_enabled=False)
    body = {"expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
            "action_id": "rank-1", "verdict": "abort", "reason": "under threshold"}
    assert client.post("/api/runs/demo/harness-checkpoints", json=body).status_code == 400
    body["verdict"] = "watch"
    assert client.post("/api/runs/demo/harness-checkpoints", json=body).status_code == 200
    assert answer_for(rd, q["checkpoint_id"])["verdict"] == "watch"
    next_q = ask(rd, 0, 0, "train_monitor", observation="loss=1", kill_enabled=True)
    store.append("node_failed", {"node_id": 0, "generation": 0,
                                 "error": "external stop", "reason": "aborted"})
    assert client.post("/api/runs/demo/harness-checkpoints", json=body).json()["replayed"]
    assert client.post("/api/runs/demo/harness-checkpoints", json={**body,
                       "checkpoint_id": next_q["checkpoint_id"],
                       "action_id": "monitor-2", "verdict": "abort"}).status_code == 409


def test_reclaimed_evaluator_hides_stale_unanswered_checkpoint(tmp_path):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0,
                                               "attempt": 0, "invocation_id": "same-id"})
    stale = ask(rd, 0, 0, "stage_check", stage="prep", observation="done")
    store.append("eval_invocation_claimed", {"node_id": 0, "generation": 0,
                                               "attempt": 0, "invocation_id": "same-id",
                                               "after_interrupted_attempt": True})
    assert not client.get("/api/runs/demo/harness-checkpoints",
                          params={"expected_generation": generation}).json()["pending"]
    body = {"expected_generation": generation, "checkpoint_id": stale["checkpoint_id"],
            "action_id": "old-answer", "verdict": "proceed", "reason": "old attempt"}
    assert client.post("/api/runs/demo/harness-checkpoints", json=body).status_code == 409
    fresh = ask(rd, 0, 0, "stage_check", stage="prep", observation="done again")
    assert client.get("/api/runs/demo/harness-checkpoints",
                      params={"expected_generation": generation}).json()["pending"][0][
                          "checkpoint_id"] == fresh["checkpoint_id"]


def test_real_pipeline_waits_for_external_check_before_next_stage(tmp_path):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    workdir = rd / "nodes" / "node_0"
    workdir.mkdir(parents=True)
    stages = [{"name": "prep", "command": [sys.executable, "-c", "print('prepared')"],
               "check": True},
              {"name": "score", "command": [sys.executable, "-c",
                "import json; open('score_ran','w').write('yes'); print(json.dumps({'metric': 0.9}))"]}]
    worker = SimpleNamespace(run_dir=rd, _redact=lambda text: text)
    fn = EvalStagesMixin._external_stage_check_fn(
        worker, SimpleNamespace(id=0, attempt=0), workdir, stages, threading.Event())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_command_eval, [sys.executable, "-c", "pass"],
                             str(workdir), 15, {"kind": "stdout_json", "key": "metric"},
                             stages=stages, check_fn=fn)
        for _ in range(200):
            questions = client.get("/api/runs/demo/harness-checkpoints",
                                   params={"expected_generation": generation}).json()["pending"]
            if questions:
                break
            time.sleep(0.01)
        assert len(questions) == 1
        assert not (workdir / "score_ran").exists()
        response = client.post("/api/runs/demo/harness-checkpoints", json={
            "expected_generation": generation, "checkpoint_id": questions[0]["checkpoint_id"],
            "action_id": "prep-ok", "verdict": "proceed", "reason": "prep completed"})
        assert response.status_code == 200, response.text
        result = future.result(timeout=10)
        assert (workdir / "score_ran").read_text() == "yes"
        assert result.metric == 0.9


def test_enabled_live_monitor_opens_question_and_agent_answers(tmp_path):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    workdir = rd / "nodes" / "node_0"
    workdir.mkdir(parents=True)
    snapshot = snapshot_training_logs(workdir)
    (workdir / "eval.log").write_text("epoch 1 loss=1.5\n")
    a = SimpleNamespace(workdir=workdir, node_id=0, generation=0,
                        _log_plan=eval_log_plan([]), _log_snapshot=snapshot,
                        _live_questions=[], kill_signal={})
    engine = SimpleNamespace(run_dir=rd, _eval_spec={"metric": {"kind": "stdout_json"}},
                             _monitor_cadence=lambda: 0.02, _redact=lambda value: value,
                             _train_monitor_kill=False, store=store, _write_lock=anyio.Lock())
    cancel = threading.Event()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(observe_external_eval, engine, a, cancel, "train_monitor")
            with anyio.fail_after(4):
                while not a._live_questions:
                    await anyio.sleep(0.01)
            questions = client.get("/api/runs/demo/harness-checkpoints",
                                   params={"expected_generation": generation}).json()["pending"]
            assert len(questions) == 1 and questions[0]["phase_id"] == "train_monitor"
            assert questions[0]["kill_enabled"] is False
            response = client.post("/api/runs/demo/harness-checkpoints", json={
                "expected_generation": generation, "checkpoint_id": questions[0]["checkpoint_id"],
                "action_id": "monitor-ok", "verdict": "continue", "reason": "loss is finite"})
            assert response.status_code == 200, response.text
            with anyio.fail_after(4):
                while not any(event.type == "train_monitor_alert" for event in store.read_all()):
                    await anyio.sleep(0.01)
            cancel.set()
            group.cancel_scope.cancel()

    anyio.run(scenario)
    assert answer_for(rd, a._live_questions[0])["verdict"] == "continue"
    assert any(event.type == "train_monitor_alert" and
               event.data.get("source") == "external_agent"
               for event in store.read_all())


def test_asha_stop_requires_same_resource_evidence_and_grace(tmp_path, monkeypatch):
    import looplab.engine.external_watch as watch

    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    workdir = rd / "nodes" / "node_0"
    workdir.mkdir(parents=True)
    snapshot = snapshot_training_logs(workdir)
    (workdir / "eval.log").write_text('{"metric": 0.9, "epoch": 2}\n')
    monkeypatch.setattr(watch, "latest_intermediate_sample",
                        lambda *args: IntermediateSample(0.9, "epoch", 2))
    monkeypatch.setattr(watch, "sibling_final_metrics", lambda *args: [0.2])
    monkeypatch.setattr(watch, "sibling_metrics_at_resource", lambda *args: [0.2])
    a = SimpleNamespace(workdir=workdir, node_id=0, generation=0,
                        _log_plan=eval_log_plan([]), _log_snapshot=snapshot,
                        _live_questions=[], kill_signal={})
    engine = SimpleNamespace(run_dir=rd, store=store,
                             _eval_spec={"metric": {"kind": "stdout_json", "key": "metric",
                                                    "resource_key": "epoch"}},
                             _asha_cadence=lambda: 0.02, _redact=lambda value: value,
                             _asha_live_min_siblings=1, _asha_live_quantile=0.5,
                             _asha_live_kill=True, _write_lock=anyio.Lock())
    cancel = threading.Event()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(observe_external_eval, engine, a, cancel, "asha_live")
            for index in range(3):
                with anyio.fail_after(5):
                    while len(a._live_questions) <= index:
                        await anyio.sleep(0.01)
                q = client.get("/api/runs/demo/harness-checkpoints",
                               params={"expected_generation": generation}).json()["pending"][-1]
                assert q["kill_enabled"] is (index == 2)
                response = client.post("/api/runs/demo/harness-checkpoints", json={
                    "expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
                    "action_id": f"rank-{index}",
                    "verdict": "watch" if index < 2 else "abort",
                    "reason": "below same-resource peers"})
                assert response.status_code == 200, response.text
            with anyio.fail_after(5):
                while not cancel.is_set():
                    await anyio.sleep(0.01)
            group.cancel_scope.cancel()

    anyio.run(scenario)
    assert a.kill_signal["terminal_reason"] == "asha_underperforming"
    assert any(event.type == "asha_rank" and event.data.get("checkpoint_id")
               for event in store.read_all())
