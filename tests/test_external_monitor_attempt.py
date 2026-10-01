"""An external monitor's stop authority must use only this attempt's curve."""
import threading
from types import SimpleNamespace

import anyio
import pytest

from looplab.engine.eval_log_plan import eval_log_plan, snapshot_training_logs
from looplab.engine.external_watch import observe_external_eval
from looplab.events.run_generation import run_generation_token
from tests.test_external_checkpoints import seeded


@pytest.mark.parametrize("past,current,expected_kill", [
    ([10 - i / 100 for i in range(800)], [2.] * 400, True),
    ([float("nan")] * 400, [10 - i / 100 for i in range(400)], False),
])
@pytest.mark.parametrize("staged", [True, False])
def test_prior_attempt_curve_cannot_change_current_monitor_authority(tmp_path, past, current, expected_kill, staged):
    rd, store, client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    workdir = rd / "nodes" / "node_0"; workdir.mkdir(parents=True)
    path = workdir / ("train.log" if staged else "eval.log")
    path.write_text("".join(f"old step={i} loss={value}\n" for i, value in enumerate(past)))
    snapshot = snapshot_training_logs(workdir)
    with path.open("a") as fh:
        fh.write("".join(f"current step={i} loss={value}\n" for i, value in enumerate(current)))
    plan = eval_log_plan([{"name": "train", "role": "training", "expect": {"files": ["weights.json"]}}] if staged else [])
    a = SimpleNamespace(workdir=workdir, node_id=0, generation=0, _log_plan=plan,
                        _log_snapshot=snapshot, _live_questions=[], kill_signal={})
    engine = SimpleNamespace(run_dir=rd, _eval_spec={"metric": {"kind": "stdout_json"}},
        _monitor_cadence=lambda: .02, _redact=lambda value: value,
        _train_monitor_kill=True, store=store, _write_lock=anyio.Lock())
    cancel = threading.Event()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(observe_external_eval, engine, a, cancel, "train_monitor")
            try:
                for index in range(2):
                    with anyio.fail_after(5):
                        while len(a._live_questions) <= index:
                            await anyio.sleep(.01)
                    q, = client.get("/api/runs/demo/harness-checkpoints", params={
                        "expected_generation": generation}).json()["pending"]
                    assert "old step=" not in q["observation"]
                    assert q["kill_enabled"] is (expected_kill if index else False)
                    response = client.post("/api/runs/demo/harness-checkpoints", json={
                        "expected_generation": generation, "checkpoint_id": q["checkpoint_id"],
                        "action_id": f"attempt-monitor:{index}", "verdict": "watch",
                        "reason": "Review only the current attempt's measured curve."})
                    assert response.status_code == 200, response.text
            finally:
                cancel.set()
                group.cancel_scope.cancel()

    anyio.run(scenario)
