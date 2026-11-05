from __future__ import annotations

from looplab.events.eventstore import EventStore
from looplab.serve.command_observation import CommandObservationIndex
from looplab.serve.run_commands import RunCommandService


def _observation(tmp_path, *, ack_first):
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("inject_node", {
        "_command_id": "cmd", "idea": {"operator": "inject"}, "code": "print(1)"})
    if ack_first:
        store.append("command_ack", {"command_id": "cmd", "event_seq": 1})
        store.append("pause", {"reason": "operator stop"})
    else:
        store.append("pause", {"reason": "operator stop"})
        store.append("command_ack", {"command_id": "cmd", "event_seq": 1})
    return CommandObservationIndex().observe(path)


def _succeeded(observation):
    service = object.__new__(RunCommandService)
    service._observe = lambda _rd: observation
    service._clear_spawn_claim = lambda _rd, _cid: None
    service._terminal = lambda _path, record, status: {**record, "status": status}
    record = {"id": "cmd", "event_seq": 1, "postcondition": "engine_ack", "drain_only": False}
    return service._succeeded(observation.path, observation.path, record)


def test_ack_before_pause_is_applied_not_deferred(tmp_path):
    observation = _observation(tmp_path, ack_first=True)
    assert observation.engine_ack_before_stop({"id": "cmd", "event_seq": 1})
    result = _succeeded(observation)
    assert result["status"] == "succeeded"
    assert "deferred_until_resume" not in result


def test_ack_after_pause_is_deferred_not_applied(tmp_path):
    observation = _observation(tmp_path, ack_first=False)
    assert not observation.engine_ack_before_stop({"id": "cmd", "event_seq": 1})
    result = _succeeded(observation)
    assert result["status"] == "succeeded"
    assert result["deferred_until_resume"] is True
