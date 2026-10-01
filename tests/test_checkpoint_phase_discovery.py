"""The suggested decision must resolve through real MCP phases, including paused drains."""
import pytest

from looplab.events.run_generation import run_generation_token
from looplab.harness.checkpoints import ask
from looplab.harness.phases import phase_detail
from tests.test_external_checkpoints import seeded


@pytest.mark.parametrize("checkpoint_phase,expected", [
    ("stage_check", "evaluation"), ("train_monitor", "monitor"),
    ("asha_live", "monitor"), ("deadline_grace", "deadline_grace"),
])
def test_next_step_points_to_a_discoverable_phase_with_the_checkpoint_write(tmp_path, checkpoint_phase, expected):
    rd, store, client = seeded(tmp_path)
    question = ask(rd, 0, 0, checkpoint_phase)
    generation = run_generation_token(store.read_all())
    for brief in (False, True):
        body = client.get("/api/runs/demo/harness-progress", params={
            "expected_generation": generation, "brief": brief}).json()
        step = body["next_step"]
        assert step["phase_id"] == expected
        phase = phase_detail(step["phase_id"])
        assert phase is not None
        assert step["action"] in phase["writes"]
        assert phase["write_access"][step["action"]] == "external_agent"
        raw = body["pending_checkpoints"][0]
        assert (raw["phase_id"] if brief else raw["question"]["phase_id"]) == question["phase_id"]


def test_paused_open_checkpoint_names_the_drain_without_resuming_on_reads(tmp_path):
    rd, store, client = seeded(tmp_path)
    ask(rd, 0, 0, "stage_check")
    store.append("pause", {})
    generation = run_generation_token(store.read_all())
    before = (rd / "events.jsonl").read_bytes()
    body = client.get("/api/runs/demo/harness-progress", params={
        "expected_generation": generation, "brief": True}).json()
    assert body["next_step"]["code"] == "answer_checkpoint"
    assert "paused" in body["next_step"]["detail"]
    assert "in-flight" in body["next_step"]["detail"]
    assert "does not resume" in body["next_step"]["detail"]
    assert (rd / "events.jsonl").read_bytes() == before
