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
from test_run_command_service import (  # noqa: E402
    _STAGED_FINISH_COMMAND_TIMEOUT_S, _SUCCESS_CEILING_S, _ack_marked, _client, _Driver, _seed,
    _types)

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
    assert "re-carved (epoch 1) after node(s) 1 were measured" in record["error"]["message"]
    assert "node_reset" not in _types(rd) and driver.calls == []


# ------------------------------------------------------------------ critic 2026-09-26, 68.3b pass

def test_a_drain_never_attaches_to_a_pending_plain_reset_nor_the_reverse(tmp_path, monkeypatch):
    """MEDIUM (critic 2026-09-26, driven): the unresolved-intent guard matched on the payload alone,
    so a "re-score, then pause" click was answered `retry_existing_command` naming a pending PLAIN
    reset — which the UI attaches to — and the whole search resumed. How a reset is served is part
    of the intent: the second command is now just "another command in progress", which the UI does
    not attach to. Pending = accepted by a server that died before admitting it (a parked worker)."""
    for first_drain in (False, True):
        root = tmp_path / f"first-drain-{first_drain}"
        rd = _seed(root, paused=True)
        client, srv = _client(root, _Driver())
        monkeypatch.setattr(srv.commands, "_start_worker", lambda *a: None)
        pending = _post(client, "node_reset", _reset(), "pending",
                        **({"drain_only": True} if first_drain else {})).json()
        assert pending["status"] == "accepted", pending
        second = _post(client, "node_reset", _reset(), "second",
                       **({} if first_drain else {"drain_only": True}))
        assert second.status_code == 409, second.text
        detail = second.json()["detail"]
        assert detail["code"] == "command_in_progress", detail
        assert "node_reset" not in _types(rd)


def test_a_failed_drain_is_promoted_only_by_a_drains_own_ack(tmp_path):
    """LOW (critic 2026-09-26, driven): a `spawn_failed` drain read `succeeded` after the operator's
    plain resume served the reset — and resumed the whole search. Any ack satisfied it; only a
    DRAIN's ack (`drain_only` on the row) may now."""
    rd = _seed(tmp_path, paused=True)
    (rd / "task.snapshot.json").unlink()     # refused before the spawner: a clean `spawn_failed`
    client, _srv = _client(tmp_path, _Driver(), observation=_SUCCESS_CEILING_S)
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
    client, _srv = _client(tmp_path, driver, observation=_SUCCESS_CEILING_S)
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
    # The subject is the RETRY, not the deadline: at the default 0.25 s the Windows runner settled
    # `timed_out` before the worker reached the missing snapshot's failure.
    client, _srv = _client(tmp_path, driver, timeout=_STAGED_FINISH_COMMAND_TIMEOUT_S)
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


# ------------------------------------------------------------------ critic 2026-09-26, third pass

def _real_ack(rd, *, drain):
    """The engine's OWN ack pass (`engine/orchestrator.py::Engine._ack_commands`), as a drain's loop
    head or control watcher runs it, over the log as it stands."""
    from looplab.engine.orchestrator import Engine

    eng = object.__new__(Engine)
    eng.store = EventStore(rd / "events.jsonl")
    eng._drain_only = drain
    eng._ack_commands(eng.store.read_all())


def _ack_once_appended(client, rd, record):
    """The drain's next ack pass, taken once the command's intent is IN the log — the worker
    appends it off the request thread, and a pass before that has nothing to ack."""
    import time as _time

    deadline = _time.time() + 10
    while _time.time() < deadline:
        current = client.get(f"/api/runs/demo/commands/{record['id']}").json()
        if current.get("event_seq") is not None:
            _real_ack(rd, drain=True)
            return current
        _time.sleep(0.01)
    raise AssertionError(f"the intent was never appended: {current}")


def _draining(tmp_path):
    """A drain command whose engine is ALIVE and has served its reset — the moment an operator
    sends something else."""
    rd = _seed(tmp_path, paused=True)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                    "violations": []})
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "b"},
                                  "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 2.0,
                                    "violations": []})
    driver = _Driver()

    def drain_child():
        # What `looplab resume --drain-only` does first on a stopped run: it LIFTS the pause for the
        # drain (it pauses again when the drain ends), so a drain in progress is not a stop — the
        # fake left the seeded pause standing, which a queued intent now reads as the operator's
        # stop (doc 69 69.30) rather than a drain that will ack it.
        from looplab.events.replay import fold

        if fold(store.read_all()).paused:
            store.append("resume", {})
        driver.alive = True
        _real_ack(rd, drain="--drain-only" in driver.calls[-1][0])

    driver.on_spawn = drain_child
    client, _srv = _client(tmp_path, driver, timeout=30.0, observation=60.0)
    drain = _terminal(client, _post(client, "node_reset", _reset(), "drain", drain_only=True).json())
    assert drain["status"] == "succeeded" and "drain_superseded" not in drain, drain
    # The drain's OWN reset is what it was asked to serve, never a plain one it served on the side
    # (the `served_by_drain` mirror is for the latter — critic 2026-09-27, mutant D7).
    assert "served_by_drain" not in drain, drain
    return rd, driver, client


@pytest.mark.parametrize("event_type,data,deferred", [
    ("fork", {"from_node_id": 1, "generation": 0}, True),      # node 0 is the drain's reset
    ("inject_node", {"idea": {"operator": "manual", "params": {"x": 1.0}, "rationale": "r"},
                     "code": "print(1)"}, True),
    ("budget_extend", {"max_eval_seconds": 1e6}, False),
])
def test_a_command_sent_during_a_drain_settles_and_never_locks_the_stop_out(
        tmp_path, event_type, data, deferred):
    """HIGH (critic 2026-09-26, third pass, driven end to end): a drain acked only what it served,
    so a fork, an inject or a budget extension sent while it ran sat `executing` — every `pause` and
    finalize answered 409 `command_in_progress` for the whole drain — and when the drain paused the
    monitor started the search it was waiting for, lifting the pause. The drain now acks it as
    DEFERRED (a budget extension it serves), the command settles at once, and the stop gets in."""
    rd, driver, client = _draining(tmp_path)
    sent = _post(client, event_type, data, "during").json()
    _ack_once_appended(client, rd, sent)           # the drain's next loop head / watcher tick
    settled = _terminal(client, client.get(f"/api/runs/demo/commands/{sent['id']}").json())
    assert settled["status"] == "succeeded", settled
    assert settled.get("deferred_to_next_search", False) is deferred, settled
    stop = _post(client, "pause", {}, "stop")
    assert stop.status_code == 200, stop.text
    driver.alive = False
    spawns = len(driver.calls)
    # `noop` would be the pause ADMITTED and satisfied — what the lockout refused was admission
    # itself; the fake drain lifts the seeded pause as the real one does, so it lands `succeeded`.
    assert _terminal(client, stop.json())["status"] in ("succeeded", "noop")
    assert len(driver.calls) == spawns, "nothing started a search after the drain"


def test_a_plain_reset_a_drain_served_says_so(tmp_path):
    """NIT (critic 2026-09-26, third pass): the mirror of `drain_superseded` — a plain reset sent
    while a drain ran is served BY the drain, which then pauses instead of searching on."""
    rd, _driver, client = _draining(tmp_path)
    sent = _post(client, "node_reset", _reset(generation=1), "plain-during").json()
    _ack_once_appended(client, rd, sent)
    settled = _terminal(client, client.get(f"/api/runs/demo/commands/{sent['id']}").json())
    assert settled["status"] == "succeeded" and settled["served_by_drain"] is True, settled
    assert "deferred_to_next_search" not in settled


def test_an_uncertain_drain_becomes_retryable_once_its_child_is_gone(tmp_path):
    """LOW (critic 2026-09-26, third pass, driven): the drain-ack guard sat in front of the rung that
    turns `engine_start_uncertain` into a retryable timeout, so a drain whose child never took the
    lock stayed `retryable: false` forever — a plain reset in the same place did not."""
    import time as _time

    from test_run_command_service import (_ADMISSION_MARGIN_S, RunCommandService, TestClient,
                                          make_app)

    for drain in (False, True):
        root = tmp_path / f"drain-{drain}"
        _seed(root, paused=True)
        silent = _Driver()                          # a pid, and never the lock
        app = make_app(root)
        srv = app.state.looplab
        # The command's deadline must fall AFTER its worker has spawned: at 0.15 s the Windows leg's
        # worker had appended the intent but not yet spawned, and the record settled
        # `postcondition_timeout` (master CI run 144; reproduced on Linux with a 0.1 s liveness
        # probe). The startup window that makes the child uncertain stays short.
        srv.commands = RunCommandService(
            srv, engine_alive=silent.is_alive, spawn_engine=silent.spawn,
            process_alive=silent.is_process_alive, startup_timeout=0.05,
            command_timeout=_ADMISSION_MARGIN_S, poll_interval=0.01,
            max_observation_timeout=_ADMISSION_MARGIN_S + 0.5)
        client = TestClient(app)
        body = {"type": "node_reset", "data": _reset(),
                "expected_generation": http_run_generation(client, "demo"),
                **({"drain_only": True} if drain else {})}
        record = client.post("/api/runs/demo/commands", headers={"Idempotency-Key": "k"},
                             json=body).json()
        deadline = _time.time() + 10
        while record["status"] not in ("failed", "timed_out", "succeeded") \
                and _time.time() < deadline:
            _time.sleep(0.01)
            record = client.get(f"/api/runs/demo/commands/{record['id']}").json()
        assert record["error"]["code"] == "engine_start_uncertain", record
        silent.pid_running = False                  # definitive death: no duplicate Popen hazard
        refreshed = client.get(f"/api/runs/demo/commands/{record['id']}").json()
        assert refreshed["error"]["retryable"] is True, (drain, refreshed)


def test_a_drain_a_search_served_since_it_failed_is_not_retried_into_success(tmp_path):
    """NIT (critic 2026-09-26, third pass, driven): GET keeps a failed drain failed when a SEARCH
    served its reset, but a retry re-drove it and settled `succeeded` on that same ack. The retry is
    refused as spent — nothing is left for a drain to evaluate."""
    rd = _seed(tmp_path, paused=True)
    (rd / "task.snapshot.json").unlink()            # a clean `spawn_failed`
    client, _srv = _client(tmp_path, _Driver())
    failed = _terminal(client, _post(client, "node_reset", _reset(), "drain-then-search",
                                     drain_only=True).json())
    assert failed["status"] == "failed" and failed["error"]["retryable"] is True, failed
    _ack_marked(rd)                                  # a plain resume's search served the reset
    read = client.get(f"/api/runs/demo/commands/{failed['id']}").json()
    assert read["status"] == "failed"
    # …and it SAYS so: `retryable: true` offered a button `/retry` then refused (critic 2026-09-27).
    assert read["error"]["retryable"] is False and read["error"]["code"] == "command_intent_spent"
    retry = client.post(f"/api/runs/demo/commands/{failed['id']}/retry")
    assert retry.status_code == 409, retry.text
    assert retry.json()["detail"]["code"] == "command_intent_spent", retry.text


def _executing(client, record):
    import time as _time

    deadline = _time.time() + 10
    while _time.time() < deadline:
        current = client.get(f"/api/runs/demo/commands/{record['id']}").json()
        if current.get("event_seq") is not None:
            return current
        _time.sleep(0.01)
    raise AssertionError(f"the intent was never appended: {current}")


@pytest.mark.parametrize("drain_pause,event_type,data", [
    (True, "fork", {"from_node_id": 1, "generation": 0}),
    (False, "fork", {"from_node_id": 1, "generation": 0}),
    (False, "budget_extend", {"add_nodes": 1}),
    (False, "node_reset", {"node_id": 1, "generation": 0, "from_stage": "eval"}),
])
def test_a_command_the_drain_never_acked_waits_for_the_search_and_starts_nothing(
        tmp_path, drain_pause, event_type, data):
    """HIGH (critic 2026-09-27, driven: 5 fork trials of 6 fired as the drain's rescored node
    landed). The drain acks at its loop heads, so a fork admitted after its LAST one — here, after
    its own pause — sat `executing` until the drain exited, locking the operator's stop out, and then
    its monitor started a plain `resume`, which lifted the drain's pause and ran the search. On the
    drain's OWN pause (`drain_only`) such a command settles `deferred_to_next_search` and nothing is
    started. On any other pause a fork — and a budget extension, since 2026-09-29 — is a queued
    intent on a stopped run and waits for the operator's resume in its own words
    (`deferred_until_resume`, doc 69 69.30) — while an intent that asks the run to go on, a reset,
    is re-driven exactly as it always was: the drain's rule does not widen to every pause."""
    rd, driver, client = _draining(tmp_path)
    EventStore(rd / "events.jsonl").append("pause", {
        "reason": "drain-only resume: done", **({"drain_only": True} if drain_pause else {})})
    driver.on_spawn = lambda: setattr(driver, "alive", True)       # a later child: a plain search
    # Counted BEFORE the POST: admission runs in a worker thread, so a count taken after the POST
    # returns could already include a start it made and make "nothing started" vacuous.
    spawns = len(driver.calls)
    posted = _post(client, event_type, data, "late").json()
    if event_type in ("fork", "budget_extend") and not drain_pause:
        settled = _terminal(client, posted)                        # settled at admission
        assert settled["status"] == "succeeded", settled
        assert settled.get("deferred_until_resume") is True, settled
        assert "deferred_to_next_search" not in settled, settled
        driver.alive = False                                       # the drain exits
        assert len(driver.calls) == spawns, "nothing started a search over the stop"
        return
    sent = _executing(client, posted)
    assert sent["status"] == "executing", sent
    driver.alive = False                                           # the drain exits, never acking
    if drain_pause:
        settled = _terminal(client, sent)
        assert settled["status"] == "succeeded", settled
        assert settled.get("deferred_to_next_search") is True, settled
        assert len(driver.calls) == spawns, "nothing started the search the drain was asked to leave"
        stop = _post(client, "pause", {}, "stop")
        assert stop.status_code == 200, stop.text
        assert _terminal(client, stop.json())["status"] in ("succeeded", "noop")
        assert len(driver.calls) == spawns
    else:
        import time as _time

        deadline = _time.time() + 10
        while _time.time() < deadline and len(driver.calls) == spawns:
            _time.sleep(0.01)
        assert len(driver.calls) > spawns, "a pause that is not the drain's changes nothing"
        assert "--drain-only" not in driver.calls[-1][0]
        # The search it started serves the extension, and the command settles — its monitor must
        # not outlive the test, polling (and sleeping) into whatever runs next.
        _real_ack(rd, drain=False)
        assert _terminal(client, sent)["status"] == "succeeded"


def test_a_finalize_that_rode_on_the_drain_starts_the_engine_that_finalizes(tmp_path):
    """HIGH (critic 2026-09-27, driven): a finalize admitted after the drain's last ack pass rode
    on it, and when the drain exited the monitor settled it `deferred_to_next_search` — but its
    postcondition is the FINISH, which no search writes. Nothing was started, the run kept its
    pending finalize, and the `resume` the UI offered next was refused `finalize_in_progress`. Only
    an `engine_ack` command waits for the search; a finalize starts the engine that finalizes."""
    from looplab.events.replay import fold

    rd, driver, client = _draining(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
    sent = _executing(client, _post(client, "run_abort", {"reason": "finalized"}, "fin").json())
    assert sent["status"] == "executing" and not sent.get("spawned_by_command"), sent
    spawns = len(driver.calls)
    # What `looplab resume` does with a pending `run_abort`: it finalizes, and exits.
    driver.on_spawn = lambda: store.append("run_finished", {"reason": "aborted"})
    driver.alive = False                                           # the drain exits, never acking
    settled = _terminal(client, sent)
    assert settled["status"] == "succeeded", settled
    assert "deferred_to_next_search" not in settled, settled
    started = driver.calls[spawns:]
    assert len(started) == 1 and "--drain-only" not in started[0][0], started
    assert fold(store.read_all()).finished is True


def test_a_command_re_driven_after_its_worker_died_still_waits_for_the_search(
        tmp_path, monkeypatch):
    """MEDIUM (critic 2026-09-27, driven): the deferral was asked by the monitor's re-spawn only.
    A command that rode on the drain and lost its worker (a server restart) is re-driven through
    ADMISSION when it is next read, and admission's spawn ladder started the plain `resume` the rule
    exists to refuse — lifting the drain's pause and running the search."""
    rd, driver, client = _draining(tmp_path)
    svc = client.app.state.looplab.commands
    real_monitor = svc._monitor
    monkeypatch.setattr(svc, "_monitor", lambda *a, **k: None)     # the worker dies after admission
    sent = _executing(client, _post(client, "fork", {"from_node_id": 1, "generation": 0},
                                    "rode").json())
    assert sent["status"] == "executing" and not sent.get("spawned_by_command"), sent
    EventStore(rd / "events.jsonl").append("pause", {
        "reason": "drain-only resume: done", "drain_only": True})
    driver.alive = False                                           # the drain exits, unwatched
    monkeypatch.setattr(svc, "_monitor", real_monitor)
    spawns = len(driver.calls)
    driver.on_spawn = lambda: setattr(driver, "alive", True)       # any child: a plain search
    settled = _terminal(client, client.get(f"/api/runs/demo/commands/{sent['id']}").json())
    assert settled["status"] == "succeeded", settled
    assert settled.get("deferred_to_next_search") is True, settled
    assert len(driver.calls) == spawns, driver.calls[spawns:]


@pytest.mark.parametrize("event_type,data,waits", [
    ("fork", {"from_node_id": 1, "generation": 0}, True),
    ("run_abort", {"reason": "finalized"}, False),
])
def test_a_command_admitted_after_the_drain_exited_is_decided_by_the_log(
        tmp_path, event_type, data, waits):
    """LOW (critic 2026-09-27, driven): a fork admitted onto the drain's pause a moment AFTER the
    drain exited started a plain `resume` at admission, while the same fork a moment BEFORE settled
    `deferred_to_next_search` — which of the two it was is a race the operator cannot see. The log
    decides now: on a drain's own pause an ack command waits for the search, and a finalize still
    starts the engine that finalizes."""
    rd, driver, client = _draining(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
    driver.alive = False                                           # the drain has already exited
    spawns = len(driver.calls)
    driver.on_spawn = (lambda: store.append("run_finished", {"reason": "aborted"})) \
        if event_type == "run_abort" else (lambda: setattr(driver, "alive", True))
    settled = _terminal(client, _post(client, event_type, data, f"after-{event_type}").json())
    assert settled["status"] == "succeeded", settled
    assert settled.get("deferred_to_next_search", False) is waits, settled
    started = driver.calls[spawns:]
    if waits:
        assert started == [], started
        assert _post(client, "pause", {}, "stop").status_code == 200, "the stop gets in"
    else:
        assert len(started) == 1 and "--drain-only" not in started[0][0], started


def test_a_drain_that_exits_between_the_read_and_the_probe_is_read_after_it(tmp_path):
    """LOW (critic 2026-09-27, driven): admission asked the rule of the log it had read BEFORE its
    lock probe; the drain wrote its pause and its last ack pass and exited in between, the stale read
    showed no drain pause, and a plain search was started for a fork whose record said it waits.
    Once a probe finds no engine the log is read again, and that read decides."""
    rd, driver, client = _draining(tmp_path)
    svc = client.app.state.looplab.commands
    store = EventStore(rd / "events.jsonl")
    real_state = svc._engine_state
    exited: list = []

    def _state(rd_):
        if not exited and any(e.type == "fork" and e.data.get("_command_id")
                              for e in store.read_all()):
            exited.append(True)          # the drain: its pause, its last ack pass, the lock released
            store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
            _real_ack(rd, drain=True)
            driver.alive = False
            return False
        return real_state(rd_)

    svc._engine_state = _state
    spawns = len(driver.calls)
    driver.on_spawn = lambda: setattr(driver, "alive", True)          # any child: a plain search
    settled = _terminal(client, _post(client, "fork", {"from_node_id": 1, "generation": 0},
                                      "race").json())
    assert exited and settled["status"] == "succeeded", settled
    assert settled.get("deferred_to_next_search") is True, settled
    assert driver.calls[spawns:] == [], "nothing was started"


def test_an_intent_the_drain_served_in_that_window_reads_served_not_deferred(tmp_path):
    """The other half of the re-read (critic 2026-09-27, mutant 5, which 190 tests survived): the
    drain SERVED the intent — a budget extension it applies — then paused and exited between the
    read and the probe. The re-read's postcondition settles it as served; without that half the
    rule saw only the drain's pause and recorded it as waiting for a search it never needed."""
    rd, driver, client = _draining(tmp_path)
    svc = client.app.state.looplab.commands
    store = EventStore(rd / "events.jsonl")
    real_state = svc._engine_state
    exited: list = []

    def _state(rd_):
        if not exited and any(e.type == "budget_extend" and e.data.get("_command_id")
                              for e in store.read_all()):
            exited.append(True)          # the drain: it applies the extension, pauses and exits
            _real_ack(rd, drain=True)
            store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
            driver.alive = False
            return False
        return real_state(rd_)

    svc._engine_state = _state
    spawns = len(driver.calls)
    driver.on_spawn = lambda: setattr(driver, "alive", True)          # any child: a plain search
    settled = _terminal(client, _post(client, "budget_extend", {"max_eval_seconds": 1e6},
                                      "served-race").json())
    assert exited and settled["status"] == "succeeded", settled
    assert settled.get("deferred_to_next_search") is not True, settled
    assert driver.calls[spawns:] == [], "nothing was started"


def test_the_monitors_rung_reads_the_log_again_once_no_engine_is_found(tmp_path):
    """The monitor's re-spawn rung asked the rule of the log it read before its own lock probe too
    (critic 2026-09-27, second pass): a drain another command started pauses, acks and exits
    between the rung's read and its probe, and the stale read spawned a plain search for a fork that
    waits. Once the probe finds no engine the rung reads the log again."""
    rd, driver, client = _draining(tmp_path)
    svc = client.app.state.looplab.commands
    store = EventStore(rd / "events.jsonl")
    real_state = svc._engine_state
    phase = {"now": "alive"}

    def _state(rd_):
        if phase["now"] == "gone":            # the monitor's loop head: no engine, no drain pause yet
            phase["now"] = "drain"
            driver.alive = False
            return False
        if phase["now"] == "drain":           # the rung's own probe: a drain paused, acked, exited
            phase["now"] = "done"
            store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
            _real_ack(rd, drain=True)
            return False
        return real_state(rd_)

    svc._engine_state = _state
    sent = _executing(client, _post(client, "fork", {"from_node_id": 1, "generation": 0},
                                    "rung").json())
    assert sent["status"] == "executing" and not sent.get("spawned_by_command"), sent
    spawns = len(driver.calls)
    driver.on_spawn = lambda: setattr(driver, "alive", True)          # any child: a plain search
    phase["now"] = "gone"
    settled = _terminal(client, sent)
    assert phase["now"] == "done", phase
    assert settled["status"] == "succeeded" and settled.get("deferred_to_next_search") is True, settled
    assert driver.calls[spawns:] == [], "nothing was started"


def test_a_folded_intent_a_drain_deferred_is_not_said_to_wait(tmp_path):
    """`deferred_to_next_search` is an ENGINE-ACK command's account (critic 2026-09-27, mutant D6):
    an intent whose postcondition is its own fold was applied the moment it folded, whatever a drain
    later acked it as."""
    rd, _driver, client = _draining(tmp_path)
    sent = _terminal(client, _post(client, "hint", {"text": "look at node 1"}, "hint").json())
    assert sent["status"] == "succeeded" and sent["postcondition"] != "engine_ack", sent
    _real_ack(rd, drain=True)
    acks = [e.data for e in EventStore(rd / "events.jsonl").read_all() if e.type == "command_ack"
            and e.data.get("command_id") == sent["id"]]
    assert acks and acks[-1].get("deferred") is True, acks
    svc = client.app.state.looplab.commands
    again = svc._succeeded(rd, svc._path(rd, sent["id"]), dict(sent))
    assert "deferred_to_next_search" not in again, again


def test_what_waits_for_the_next_search_is_a_stated_rule():
    """`_left_for_the_next_search`'s truth table: only a command that RODE on the engine alive when
    it was admitted (never one this service started an engine for), never the drain's own reset
    (its drain serves it), never an intent asking for the search itself, and only on the drain's own
    pause."""
    from looplab.engine.run_boundary import DRAIN_LEFT_FOR_THE_SEARCH
    from looplab.serve.run_commands import RunCommandService

    class _Observation:
        def __init__(self, paused):
            self.paused = paused

        def drain_paused(self):
            return self.paused

    rule = RunCommandService._left_for_the_next_search
    ack = {"postcondition": "engine_ack"}
    rode = {"event_type": "fork", **ack}
    assert rule(rode, _Observation(True)) is True
    assert rule(rode, _Observation(False)) is False
    assert rule({**rode, "spawned_by_command": True}, _Observation(True)) is False
    assert rule({"event_type": "node_reset", "drain_only": True, **ack}, _Observation(True)) is False
    assert rule({"event_type": "node_reset", **ack}, _Observation(True)) is True, \
        "a plain reset rode on it"
    for kind in DRAIN_LEFT_FOR_THE_SEARCH:
        assert rule({"event_type": kind, **ack}, _Observation(True)) is False, kind
    # Only an ACK waits for a search: a finalize's postcondition is the finish no search writes
    # (critic 2026-09-27), and a record whose postcondition is anything else never defers.
    assert rule({"event_type": "run_abort", "postcondition": "finished_and_stopped"},
                _Observation(True)) is False
    assert rule({"event_type": "fork"}, _Observation(True)) is False


def test_the_drains_pause_is_read_off_the_latest_row_that_moves_the_run(tmp_path):
    """`drain_paused` reads the LATEST pause/resume/reopen/restart/finish row: a drain's pause the
    run has since left is not one, and a row that moves nothing (a hint, a terminal) keeps it."""
    rd, _driver, client = _draining(tmp_path)
    svc = client.app.state.looplab.commands
    store = EventStore(rd / "events.jsonl")

    def paused():
        return svc._observe(rd).drain_paused()

    assert paused() is False, "the drain in `_draining` never paused"
    store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
    assert paused() is True
    store.append("hint", {"text": "later"})
    assert paused() is True, "a row that moves nothing keeps the drain's pause"
    for mover in ("resume", "run_reopened", "restart", "run_finished"):
        store.append(mover, {})
        assert paused() is False, mover
        store.append("pause", {"reason": "drain-only resume: done", "drain_only": True})
        assert paused() is True
    store.append("pause", {"reason": "operator"})
    assert paused() is False, "an operator's pause after the drain's is not the drain's"
