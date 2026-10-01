"""ASHA permission and visible evidence stay bound to the current objective/stage."""
import threading
from types import SimpleNamespace

import anyio

from looplab.engine.asha_monitor import IntermediateSample
from looplab.engine.eval_log_plan import eval_log_plan, snapshot_training_logs
from looplab.engine.external_watch import observe_external_eval
from looplab.harness.checkpoints import ask
from looplab.events.run_generation import run_generation_token
from tests.test_external_checkpoints import seeded


def _drive(tmp_path, monkeypatch, *, retarget=False, stage_change=False, long_log=False):
    import looplab.engine.external_watch as watch

    rd, store, client = seeded(tmp_path)
    if retarget:
        store.append("metric_retarget", {"key": "latency", "direction": "min"})
    generation = run_generation_token(store.read_all())
    workdir = rd / "nodes" / "node_0"; workdir.mkdir(parents=True)
    snapshot = snapshot_training_logs(workdir)
    plan = eval_log_plan([{"name": "first"}, {"name": "second"}, {"name": "score"}])
    text = ("live training output\n" * 1000 if long_log else "") + '{"metric":0.9,"epoch":2}\n'
    (workdir / "first.log").write_text(text)
    monkeypatch.setattr(watch, "latest_intermediate_sample", lambda *args: IntermediateSample(.9, "epoch", 2))
    monkeypatch.setattr(watch, "sibling_final_metrics", lambda *args: [.2])
    monkeypatch.setattr(watch, "sibling_metrics_at_resource", lambda *args: [.2])
    a = SimpleNamespace(workdir=workdir, node_id=0, generation=0, _log_plan=plan,
                        _log_snapshot=snapshot, _live_questions=[], kill_signal={})
    engine = SimpleNamespace(run_dir=rd, store=store, _eval_spec={"metric": {
        "kind": "stdout_json", "key": "metric", "resource_key": "epoch"}},
        _asha_cadence=lambda: .02, _redact=lambda value: value, _asha_live_min_siblings=1,
        _asha_live_quantile=.5, _asha_live_kill=True, _write_lock=anyio.Lock())
    cancel = threading.Event()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(observe_external_eval, engine, a, cancel, "asha_live")
            try:
                for index in range(5 if stage_change else 3):
                    with anyio.fail_after(5):
                        while len(a._live_questions) <= index:
                            await anyio.sleep(.01)
                    q, = client.get("/api/runs/demo/harness-checkpoints", params={
                        "expected_generation": generation}).json()["pending"]
                    assert len(q["observation"]) <= 6000
                    assert "same-resource peers=[0.2]" in q["observation"]
                    assert "minimum before stop=3" in q["observation"]
                    assert '{"metric":0.9,"epoch":2}' in q["observation"]
                    expected = False if retarget else index == (4 if stage_change else 2)
                    assert q["kill_enabled"] is expected
                    if stage_change and index == 1:
                        # New stage must receive a fresh three-tick grace, independent of first.
                        (workdir / "second.log").write_text(text + "new stage\n")
                    response = client.post("/api/runs/demo/harness-checkpoints", json={
                        "expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
                        "action_id": f"asha-guard:{index}", "verdict": "watch",
                        "reason": "Inspect this stage's task-scale evidence before another look."})
                    assert response.status_code == 200, response.text
            finally:
                cancel.set(); group.cancel_scope.cancel()

    anyio.run(scenario)


def test_external_asha_never_grants_task_curve_stop_after_objective_retarget(tmp_path, monkeypatch):
    _drive(tmp_path, monkeypatch, retarget=True)


def test_external_asha_restarts_grace_for_a_new_stage(tmp_path, monkeypatch):
    _drive(tmp_path, monkeypatch, stage_change=True)


def test_long_log_cannot_hide_asha_comparability_or_stop_window(tmp_path, monkeypatch):
    _drive(tmp_path, monkeypatch, long_log=True)


def test_retarget_removes_an_already_open_asha_questions_stop_permission(tmp_path):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    q = ask(rd, 0, 0, "asha_live", kill_enabled=True, observation="same-resource peers=[0.2]")
    before = (rd / "harness_checkpoints.jsonl").read_bytes()
    store.append("metric_retarget", {"key": "latency", "direction": "min"})
    pending = client.get("/api/runs/demo/harness-checkpoints", params={"expected_generation": generation}).json()["pending"]
    assert pending[0]["checkpoint_id"] == q["checkpoint_id"]
    assert pending[0]["kill_enabled"] is False
    assert pending[0]["recorded_kill_enabled"] is True
    assert pending[0]["stop_refusal"] == "objective_retargeted"
    step = client.get("/api/runs/demo/harness-progress", params={
        "expected_generation": generation, "brief": True}).json()["next_step"]
    assert "Allowed verdicts: continue, watch." in step["detail"]
    assert "objective was retargeted" in step["detail"]
    assert (rd / "harness_checkpoints.jsonl").read_bytes() == before
    body = {"expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
            "action_id": "retarget-open", "verdict": "abort", "reason": "Old permission cannot stop the new objective."}
    assert client.post("/api/runs/demo/harness-checkpoints", json=body).status_code == 400
    assert (rd / "harness_checkpoints.jsonl").read_bytes() == before
    body["verdict"] = "continue"
    assert client.post("/api/runs/demo/harness-checkpoints", json=body).status_code == 200


def test_retarget_between_accepted_abort_and_consumption_vetoes_the_stop(tmp_path, monkeypatch):
    import looplab.engine.external_watch as watch
    import looplab.harness.checkpoints as checkpoints

    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    workdir = rd / "nodes" / "node_0"; workdir.mkdir(parents=True)
    snapshot = snapshot_training_logs(workdir)
    (workdir / "eval.log").write_text('{"metric":0.9,"epoch":2}\n')
    monkeypatch.setattr(watch, "latest_intermediate_sample", lambda *args: IntermediateSample(.9, "epoch", 2))
    monkeypatch.setattr(watch, "sibling_final_metrics", lambda *args: [.2])
    monkeypatch.setattr(watch, "sibling_metrics_at_resource", lambda *args: [.2])
    original = checkpoints.answer_for
    changed = False

    def consume(*args):
        nonlocal changed
        answer = original(*args)
        if answer is not None and answer["verdict"] == "abort" and not changed:
            store.append("metric_retarget", {"key": "latency", "direction": "min"})
            changed = True
        return answer

    monkeypatch.setattr(checkpoints, "answer_for", consume)
    a = SimpleNamespace(workdir=workdir, node_id=0, generation=0, _log_plan=eval_log_plan([]),
                        _log_snapshot=snapshot, _live_questions=[], kill_signal={})
    engine = SimpleNamespace(run_dir=rd, store=store, _eval_spec={"metric": {
        "kind": "stdout_json", "key": "metric", "resource_key": "epoch"}},
        _asha_cadence=lambda: .02, _redact=lambda value: value, _asha_live_min_siblings=1,
        _asha_live_quantile=.5, _asha_live_kill=True, _write_lock=anyio.Lock())
    cancel = threading.Event()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(observe_external_eval, engine, a, cancel, "asha_live")
            try:
                for index in range(4):
                    with anyio.fail_after(5):
                        while len(a._live_questions) <= index:
                            assert not cancel.is_set(), "A stale task-scale abort stopped the retargeted run"
                            await anyio.sleep(.01)
                    q, = client.get("/api/runs/demo/harness-checkpoints", params={
                        "expected_generation": generation}).json()["pending"]
                    assert q["kill_enabled"] is (index == 2)
                    if index == 3:
                        assert changed and not a.kill_signal
                        assert client.post("/api/runs/demo/harness-checkpoints", json=body).json()["replayed"]
                        assert any(r.type == "asha_rank" and r.data.get("stop_refusal") == "objective_retargeted"
                                   for r in store.read_all())
                        break
                    verdict = "abort" if index == 2 else "watch"
                    body = {
                        "expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
                        "action_id": f"retarget-consume:{index}", "verdict": verdict,
                        "reason": "Review the task-scale rank before the operator changes objective."}
                    response = client.post("/api/runs/demo/harness-checkpoints", json=body)
                    assert response.status_code == 200, response.text
            finally:
                cancel.set(); group.cancel_scope.cancel()

    anyio.run(scenario)
