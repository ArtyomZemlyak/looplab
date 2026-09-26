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


def _drain_ack_marked(rd):
    """The ack a DRAIN engine writes (`engine/orchestrator.py::Engine._ack_commands`): the same
    identity as `_ack_marked`'s, marked `drain_only` so the command can tell who served it."""
    events = EventStore(rd / "events.jsonl").read_all()
    intent = [e for e in events if (e.data or {}).get("_command_id")][-1]
    EventStore(rd / "events.jsonl").append("command_ack", {
        "command_id": intent.data["_command_id"], "event_seq": intent.seq, "drain_only": True})
    return intent


def _acking_driver(rd, **kwargs):
    """A driver whose child acks the way the engine it was asked for would: as a drain when
    spawned with `--drain-only`, as a search otherwise."""
    driver = _Driver(**kwargs)

    def ack():
        driver.alive = True
        if "--drain-only" in driver.calls[-1][0]:
            _drain_ack_marked(rd)
        else:
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
    assert "drain_superseded" not in record, "its own drain served it"
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


# ------------------------------------------------------------------ critic 2026-09-26, 68.3b pass

def test_a_drain_never_attaches_to_a_pending_plain_reset_nor_the_reverse(tmp_path):
    """MEDIUM (critic 2026-09-26, driven): the unresolved-intent guard matched on the payload alone,
    so a "re-score, then pause" click was answered `retry_existing_command` naming a pending PLAIN
    reset — which the UI attaches to — and the whole search resumed. How a reset is served is part
    of the intent."""
    rd = _seed(tmp_path, paused=True)
    driver = _Driver(alive=True)            # an engine that never acks: the plain reset stays open
    client, _srv = _client(tmp_path, driver, timeout=0.08, observation=0.25)
    plain = _terminal(client, _post(client, "node_reset", _reset(), "plain-open").json())
    assert plain["status"] == "timed_out", plain
    drain = _post(client, "node_reset", _reset(), "drain-fresh", drain_only=True)
    # Its own record, answered on its own terms (here: the plain reset already moved the node's
    # generation) — never a 409 pointing the UI at the plain command to attach to.
    assert drain.status_code == 200, drain.text
    assert drain.json()["id"] != plain["id"] and drain.json()["status"] == "rejected"

    other = tmp_path / "other"
    rd2 = _seed(other, paused=True)
    driver2 = _Driver()
    driver2.on_spawn = lambda: setattr(driver2, "alive", True)   # starts, never acks
    client2, _srv2 = _client(other, driver2, timeout=0.08, observation=0.25)
    pending_drain = _terminal(client2, _post(client2, "node_reset", _reset(), "drain-open",
                                             drain_only=True).json())
    assert pending_drain["status"] == "timed_out", pending_drain
    again = _post(client2, "node_reset", _reset(), "plain-fresh")
    assert not (again.status_code == 409
                and again.json()["detail"].get("existing_command_id") == pending_drain["id"]), (
        again.text)
    assert "node_reset" in _types(rd2)


def test_a_failed_drain_is_promoted_only_by_a_drains_own_ack(tmp_path):
    """LOW (critic 2026-09-26, driven): a `spawn_failed` drain read `succeeded` after the operator's
    plain resume served the reset — and resumed the whole search. Any ack satisfied it; only a
    DRAIN's ack (`drain_only` on the row) may now."""
    rd = _seed(tmp_path, paused=True)
    (rd / "task.snapshot.json").unlink()     # refused before the spawner: a clean `spawn_failed`
    client, _srv = _client(tmp_path, _Driver())
    failed = _terminal(client, _post(client, "node_reset", _reset(), "drain-spawn-fails",
                                     drain_only=True).json())
    assert failed["status"] == "failed" and failed["error"]["code"] == "spawn_failed", failed
    assert "node_reset" in _types(rd), "the intent is durable; only its serving failed"
    _ack_marked(rd)                          # a plain resume served it
    still = client.get(f"/api/runs/demo/commands/{failed['id']}").json()
    assert still["status"] == "failed", still
    _drain_ack_marked(rd)                    # …and a drain, run from the CLI, served it as one
    promoted = client.get(f"/api/runs/demo/commands/{failed['id']}").json()
    assert promoted["status"] == "succeeded" and promoted["reconciled_from"] == "failed"
    assert "drain_superseded" not in promoted


def test_a_drain_a_search_served_says_so(tmp_path):
    """The reset landed and an engine that is NOT a drain acked it — one already running, or another
    spawner's launch in flight. The command succeeds, and says it was superseded."""
    rd = _seed(tmp_path, paused=True)
    driver = _Driver()

    def plain_engine_acks():
        driver.alive = True
        _ack_marked(rd)

    driver.on_spawn = plain_engine_acks
    client, _srv = _client(tmp_path, driver)
    record = _terminal(client, _post(client, "node_reset", _reset(), "drain-superseded",
                                     drain_only=True).json())
    assert record["status"] == "succeeded" and record["drain_superseded"] is True, record


def test_admission_asks_the_drain_again_on_the_log_it_admits_into(tmp_path, monkeypatch):
    """A command accepted by one server and admitted after a restart: the log may have moved in
    between. Admission re-asks the drain over the log PLUS the reset (critic 2026-09-26: dropping
    that ask from `_admit` survived every test), and the re-drive keeps `--drain-only`."""
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                    "violations": []})
    store.append("node_created", {
        "node_id": 1, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "second"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 0.5,
                                    "violations": []})
    store.append("pause", {})
    driver = _acking_driver(rd)
    client, srv = _client(tmp_path, driver)
    parked = []
    real_start = srv.commands._start_worker
    monkeypatch.setattr(srv.commands, "_start_worker", lambda *a: parked.append(a))
    accepted = _post(client, "node_reset", _reset(), "admitted-late", drain_only=True).json()
    assert accepted["status"] == "accepted" and parked, accepted
    store.append("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 0.6,
                                       "search_epoch": 0})
    monkeypatch.setattr(srv.commands, "_start_worker", real_start)
    record = _terminal(client, client.get(f"/api/runs/demo/commands/{accepted['id']}").json())
    assert record["status"] == "rejected" and record["error"]["code"] == "drain_refused", record
    assert "node_reset" not in _types(rd) and driver.calls == []


def test_a_retry_re_drives_the_drain_it_was(tmp_path):
    rd = _seed(tmp_path, paused=True)
    snapshot = rd / "task.snapshot.json"
    kept = snapshot.read_bytes()
    snapshot.unlink()
    driver = _acking_driver(rd)
    client, _srv = _client(tmp_path, driver)
    failed = _terminal(client, _post(client, "node_reset", _reset(), "drain-retry",
                                     drain_only=True).json())
    assert failed["status"] == "failed" and failed["error"]["retryable"] is True, failed
    snapshot.write_bytes(kept)
    retried = _terminal(client, client.post(
        f"/api/runs/demo/commands/{failed['id']}/retry").json())
    assert retried["status"] == "succeeded" and "drain_superseded" not in retried, retried
    assert driver.calls[-1][0][-1] == "--drain-only"


def test_a_pending_resume_refuses_a_drain(tmp_path):
    """LOW (critic 2026-09-26): a drain's engine records itself as serving a pending resume, then
    pauses — the operator's resume consumed into its opposite."""
    rd = _seed(tmp_path, paused=True)
    EventStore(rd / "events.jsonl").append("resume_requested", {"mode": "resume"})
    driver = _acking_driver(rd)
    client, _srv = _client(tmp_path, driver)
    record = _post(client, "node_reset", _reset(), "drain-over-resume", drain_only=True).json()
    assert record["status"] == "rejected" and record["error"]["code"] == "drain_refused", record
    assert "a resume is pending" in record["error"]["message"]
    assert "node_reset" not in _types(rd) and driver.calls == []


def test_a_drain_command_re_parses_no_more_of_the_log_than_a_plain_one(tmp_path, monkeypatch):
    """LOW (critic 2026-09-26, measured 6.4 s per ask on a 70 MB log): each drain ask — at submit,
    at admission, before the spawn — built a fresh EventStore and re-read the whole log. They read
    the shared observation now; only the prospective fold is new work, and it decodes nothing."""
    from test_control_reads_the_log_once import _Accountant

    def decoded(root, drain):
        rd = _seed(root, paused=True)
        store = EventStore(rd / "events.jsonl")
        for index in range(60):
            store.append("hint", {"text": f"h{index}"})
        client, srv = _client(root, _acking_driver(rd))
        srv.commands._observe(rd)            # warm the shared index, as a live server has
        books = _Accountant(monkeypatch)
        record = _terminal(client, _post(client, "node_reset", _reset(), f"cost-{drain}",
                                         **({"drain_only": True} if drain else {})).json())
        assert record["status"] == "succeeded", record
        monkeypatch.undo()
        return books.records

    plain, drain = decoded(tmp_path / "plain", False), decoded(tmp_path / "drain", True)
    assert drain - plain < 60, (plain, drain)
