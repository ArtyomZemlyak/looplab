"""A run with only measured nodes still needs an engine before its next candidate."""
import pytest

from looplab.events.run_generation import run_generation_token
from looplab.harness.phases import phase_detail
from tests.test_external_progress import _run


@pytest.mark.parametrize("alive,label", [(False, "stopped"), (None, "unknown")])
def test_idle_run_requires_observed_recovery_when_engine_is_unavailable(tmp_path, monkeypatch, alive, label):
    rd, store, client = _run(tmp_path)
    monkeypatch.setattr("looplab.engine.run_lifecycle.engine_liveness", lambda _: alive)
    args = {"expected_generation": run_generation_token(store.read_all())}
    before = (rd / "events.jsonl").read_bytes()
    full = client.get("/api/runs/demo/harness-progress", params=args).json()
    brief = client.get("/api/runs/demo/harness-progress", params={**args, "brief": True}).json()
    assert full["finish_pending_nodes"] == []
    assert not any(full["recorded_lifecycle"].values())
    step = full["next_step"]
    assert brief["next_step"] == step
    assert step["code"] == "inspect_lifecycle"
    assert label in step["title"].lower()
    assert step["phase_id"] == "recovery" and step["action"] is None
    assert "before submitting" in step["detail"]
    assert "Agent connection: not measured" in step["detail"]
    assert phase_detail(step["phase_id"]) is not None
    assert any("/command-receipt?" in ref for ref in step["reads"])
    assert not any("/commands/{command_id}" in ref for ref in step["reads"])
    assert (rd / "events.jsonl").read_bytes() == before


def test_idle_engine_probe_changes_advice_without_new_events_or_automatic_resume(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path)
    args = {"expected_generation": run_generation_token(store.read_all()), "brief": True}
    before = (rd / "events.jsonl").read_bytes()
    bodies = []
    for alive in (True, False, None, True):
        monkeypatch.setattr("looplab.engine.run_lifecycle.engine_liveness", lambda _: alive)
        bodies.append(client.get("/api/runs/demo/harness-progress", params=args).json())
    assert [body["next_step"]["code"] for body in bodies] == [
        "choose_direction", "inspect_lifecycle", "inspect_lifecycle", "choose_direction"]
    assert len({body["event_seq"] for body in bodies}) == 1
    assert "waits for an explicit external decision" in bodies[0]["next_step"]["detail"]
    assert "does not trigger an internal agent takeover" in bodies[-1]["next_step"]["detail"]
    assert all(body["execution"]["agent_connection"] == "not_measured" for body in bodies)
    assert (rd / "events.jsonl").read_bytes() == before
