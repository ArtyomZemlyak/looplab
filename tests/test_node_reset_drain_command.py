"""A `node_reset` command served as a DRAIN (doc 68 68.3b).

The Inspector's stage reset is a server command whose worker starts `looplab resume` when no engine
owns the run — the whole search. `drain_only: true` on the command body makes that worker start
`looplab resume --drain-only` instead: the owed evaluations, then a pause. It rides the command
RECORD, never the event log, and the server asks the drain's own rule (`engine/run_boundary.py::
drain_only_refusal`) before the reset is appended and again before every spawn — a drain the CLI
refuses exits before it builds an engine and would never ack, so admitting one bought a re-spawn
every monitor pass until the observation deadline.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from factories import command_terminal as _terminal  # noqa: E402
from factories import http_run_generation  # noqa: E402
from test_run_command_service import _ack_marked, _client, _Driver, _seed, _types  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402


def _post(client, event_type, data, key, **extra):
    return client.post("/api/runs/demo/commands", headers={"Idempotency-Key": key},
                       json={"type": event_type, "data": data,
                             "expected_generation": http_run_generation(client, "demo"),
                             **extra})


def _acking_driver(rd, **kwargs):
    driver = _Driver(**kwargs)

    def ack():
        driver.alive = True
        _ack_marked(rd)

    driver.on_spawn = ack
    return driver


def _reset(generation=0):
    return {"node_id": 0, "generation": generation, "from_stage": "eval"}


def test_a_drain_reset_starts_resume_drain_only_and_the_log_is_unchanged(tmp_path):
    rd = _seed(tmp_path, paused=True)
    driver = _acking_driver(rd)
    client, _srv = _client(tmp_path, driver)
    record = _terminal(client, _post(client, "node_reset", _reset(), "drain-1",
                                     drain_only=True).json())
    assert record["status"] == "succeeded", record
    assert record["drain_only"] is True
    (args, _kwargs), = driver.calls
    assert args[0] == "resume" and args[-1] == "--drain-only", args
    reset = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "node_reset"][-1]
    assert "drain_only" not in reset.data, "a property of how it is served, not of the event"


def test_a_plain_reset_still_starts_a_plain_resume(tmp_path):
    rd = _seed(tmp_path, paused=True)
    driver = _acking_driver(rd)
    client, _srv = _client(tmp_path, driver)
    record = _terminal(client, _post(client, "node_reset", _reset(), "plain-1").json())
    assert record["status"] == "succeeded" and "drain_only" not in record
    (args, _kwargs), = driver.calls
    assert "--drain-only" not in args


@pytest.mark.parametrize("event_type,data,drain", [
    ("hint", {"text": "t"}, True),          # a drain is a way to serve a RESET, nothing else
    ("node_reset", _reset(), "yes"),        # a JSON boolean, never a truthy string
    ("node_reset", _reset(), 1),
])
def test_drain_only_is_refused_off_a_node_reset_and_as_anything_but_a_boolean(
        tmp_path, event_type, data, drain):
    rd = _seed(tmp_path, paused=True)
    client, _srv = _client(tmp_path, _Driver())
    response = _post(client, event_type, data, "bad-drain", drain_only=drain)
    assert response.status_code == 400, response.text
    assert "node_reset" not in _types(rd) and "hint" not in _types(rd)


def test_the_same_key_with_the_drain_added_is_a_different_command(tmp_path):
    rd = _seed(tmp_path, paused=True)
    client, _srv = _client(tmp_path, _acking_driver(rd))
    _terminal(client, _post(client, "node_reset", _reset(), "same-key").json())
    again = _post(client, "node_reset", _reset(), "same-key", drain_only=True)
    assert again.status_code == 409, again.text
    # …while `drain_only: false` IS the plain command: the same key replays it.
    assert _post(client, "node_reset", _reset(), "same-key",
                 drain_only=False).status_code == 200


def test_a_live_engine_refuses_a_drain_before_the_reset_is_recorded(tmp_path):
    """A live engine would serve the reset inside the search it is running — exactly what a drain
    was asked not to buy."""
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver)
    record = _post(client, "node_reset", _reset(), "live-drain", drain_only=True).json()
    assert record["status"] == "rejected", record
    assert record["error"]["code"] == "drain_needs_stopped_run"
    assert "node_reset" not in _types(rd) and driver.calls == []


def _disclosed_two_node_run(tmp_path):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                    "violations": []})
    store.append("node_created", {
        "node_id": 1, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "second"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 0.5,
                                    "violations": []})
    store.append("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 0.6,
                                       "search_epoch": 0})
    store.append("pause", {})
    return rd


def test_a_drain_the_cli_would_refuse_is_rejected_before_the_reset_is_recorded(tmp_path):
    """After a holdout disclosure one reset re-queues every evaluated incumbent, and `resume
    --drain-only` refuses to retrain them unannounced. The command asks the same rule over the log
    PLUS the reset it would append, so nothing is recorded and nothing is spawned."""
    rd = _disclosed_two_node_run(tmp_path)
    driver = _acking_driver(rd)
    client, _srv = _client(tmp_path, driver)
    record = _post(client, "node_reset", _reset(), "refused-drain", drain_only=True).json()
    assert record["status"] == "rejected", record
    assert record["error"]["code"] == "drain_refused"
    assert "re-queued by the holdout epoch rotation" in record["error"]["message"]
    assert "node_reset" not in _types(rd) and driver.calls == []
    # The plain reset is still the operator's to make: it resumes the search, requeue and all.
    plain = _terminal(client, _post(client, "node_reset", _reset(), "plain-after").json())
    assert plain["status"] == "succeeded" and "--drain-only" not in driver.calls[0][0]


def test_a_drain_refused_at_spawn_is_terminal_and_no_later_ack_rescues_it(tmp_path, monkeypatch):
    """A control can land between the append and the spawn; each spawn asks the drain again, and a
    refusal is this command's answer rather than a re-spawn loop. A later ack comes from whatever
    engine serves the reset next — a plain resume's search — which is not the drain."""
    rd = _seed(tmp_path, paused=True)
    driver = _acking_driver(rd)
    client, srv = _client(tmp_path, driver)
    real = srv.commands._drain_refusal

    def refuse_at_spawn(run_dir, reset, **kwargs):
        if reset is None:                   # the pre-spawn re-check, on the log holding the reset
            return {"code": "drain_refused", "message": "a drain would not drive this run: x",
                    "remediation": "r", "retryable": False}
        return real(run_dir, reset, **kwargs)

    monkeypatch.setattr(srv.commands, "_drain_refusal", refuse_at_spawn)
    record = _terminal(client, _post(client, "node_reset", _reset(), "late-refusal",
                                     drain_only=True).json())
    assert record["status"] == "failed" and record["error"]["code"] == "drain_refused", record
    assert driver.calls == [], "refused before the lease and the Popen"
    assert "node_reset" in _types(rd)
    _ack_marked(rd)                          # e.g. a plain resume later serves the reset
    again = client.get(f"/api/runs/demo/commands/{record['id']}").json()
    assert again["status"] == "failed" and again["error"]["code"] == "drain_refused"


def test_a_drain_reset_of_a_finished_host_graded_run_is_refused_across_its_split(tmp_path):
    """The command's own path through doc 68 68.3c: the reset re-opens the finished run in a new
    search epoch, and the host's split is salted by it — node 1 was measured on the old rows."""
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                    "violations": []})
    store.append("node_created", {
        "node_id": 1, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "second"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 0.5,
                                    "violations": []})
    store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    store.append("run_finished", {"reason": "done"})
    driver = _acking_driver(rd)
    client, _srv = _client(tmp_path, driver)
    record = _post(client, "node_reset", _reset(), "split-drain", drain_only=True).json()
    assert record["status"] == "rejected", record
    assert record["error"]["code"] == "drain_refused", record
    assert "re-carved (search epoch 1) after node(s) 1 were measured" in record["error"]["message"]
    assert "node_reset" not in _types(rd) and driver.calls == []
