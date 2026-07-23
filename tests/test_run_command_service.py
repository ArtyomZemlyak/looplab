"""CTRL-01: durable/idempotent run command lifecycle and engine-policy contract."""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve import run_commands as run_commands_module  # noqa: E402
from looplab.serve.protocol import CONTROL_EVENTS, PHASE_FINALIZING  # noqa: E402
from looplab.serve.run_commands import (  # noqa: E402
    CONTROL_DATA_FIELDS, CONTROL_SPECS, TERMINAL_STATUSES, EnginePolicy, RunCommandService,
    _process_identity, normalize_control, run_generation_token, task_file_for)
from looplab.serve.server import make_app  # noqa: E402


def _seed(root, run_id="demo", *, paused=False, finished=False, finalizing=False,
          approval=False, spec_approval=False):
    rd = root / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "task", "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"},
        "code": "print(1)",
    })
    if paused:
        store.append("pause", {})
    if approval:
        store.append("approval_requested", {"node_id": 0})
    if spec_approval:
        store.append("spec_proposed", {"eval_spec": {"cmd": "python train.py"}})
        store.append("spec_approval_requested", {})
    if finalizing:
        store.append("run_abort", {"reason": "finalized"})
    if finished:
        store.append("run_finished", {"reason": "done"})
    (rd / "task.snapshot.json").write_text(
        '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
    return rd


class _Driver:
    def __init__(self, *, alive=False, on_spawn=None, error=None, pid_running=True,
                 process_identity="child-generation"):
        self.alive = alive
        self.on_spawn = on_spawn
        self.error = error
        self.pid_running = pid_running
        self.process_identity = process_identity
        self.calls = []

    def is_alive(self, _rd):
        return self.alive

    def is_process_alive(self, _pid):
        return self.pid_running

    def get_process_identity(self, _pid):
        return self.process_identity

    def spawn(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        if self.on_spawn:
            self.on_spawn()
        return 4242


def _client(root, driver, *, startup=0.08, timeout=0.25, observation=None):
    app = make_app(root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, engine_alive=driver.is_alive, spawn_engine=driver.spawn,
        process_alive=driver.is_process_alive,
        process_identity=driver.get_process_identity,
        startup_timeout=startup, command_timeout=timeout, poll_interval=0.01,
        max_observation_timeout=(observation if observation is not None
                                 else max(0.30, timeout * 4)))
    return TestClient(app), srv


def _generation(client, run_id="demo"):
    generation = client.get(f"/api/runs/{run_id}/state").json()["generation"]
    assert isinstance(generation, str) and len(generation) == 64
    return generation


def _post(client, event_type, data=None, key="key-1", *, generation=None):
    expected_generation = generation if generation is not None else _generation(client)
    return client.post("/api/runs/demo/commands", headers={"Idempotency-Key": key},
                       json={"type": event_type, "data": data or {},
                             "expected_generation": expected_generation})


def _terminal(client, record, timeout=1.0, run_id="demo"):
    current = record
    deadline = time.time() + timeout
    while current.get("status") not in TERMINAL_STATUSES and time.time() < deadline:
        time.sleep(0.01)
        current = client.get(f"/api/runs/{run_id}/commands/{record['id']}").json()
    assert current.get("status") in TERMINAL_STATUSES, current
    return current


def _types(rd):
    return [event.type for event in EventStore(rd / "events.jsonl").read_all()]


def test_command_openapi_describes_manual_body_header_records_and_errors(tmp_path):
    rd = _seed(tmp_path)
    app = make_app(tmp_path)
    spec = app.openapi()
    paths = spec["paths"]
    components = spec["components"]["schemas"]
    command_path = "/api/runs/{run_id}/commands"
    record_path = "/api/runs/{run_id}/commands/{command_id}"
    retry_path = record_path + "/retry"

    post = paths[command_path]["post"]
    header = next(item for item in post["parameters"]
                  if item["in"] == "header" and item["name"] == "Idempotency-Key")
    assert header["required"] is True
    assert header["schema"]["minLength"] == 1
    assert header["schema"]["maxLength"] == 512
    body = post["requestBody"]["content"]["application/json"]["schema"]
    assert body["additionalProperties"] is True
    assert set(body["required"]) == {"type", "expected_generation"}
    assert body["properties"]["expected_generation"]["pattern"] == r"^[0-9a-fA-F]{64}$"

    for path, method in ((command_path, "post"), (record_path, "get"), (retry_path, "post")):
        responses = paths[path][method]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/RunCommandRecord")
        assert responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/RunCommandHTTPError")

    record = components["RunCommandRecord"]
    assert record["additionalProperties"] is True
    assert set(record["properties"]["status"]["enum"]) == {
        "accepted", "executing", "succeeded", "noop", "failed", "rejected", "timed_out",
    }
    assert components["RunCommandError"]["additionalProperties"] is True

    # The documentation-only response models must not alter the existing manual parser/wire payload.
    client, _srv = _client(tmp_path, _Driver())
    response = _post(client, "hint", {"text": "schema probe"}, key="schema-probe")
    assert response.status_code == 200
    row = response.json()
    assert row["status"] == "succeeded" and isinstance(row["event_seq"], int)
    assert EventStore(rd / "events.jsonl").read_all()[-1].type == "hint"


def test_task_file_for_prefers_immutable_snapshot_and_validates_legacy_target(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    snapshot = rd / "task.snapshot.json"
    legacy = tmp_path / "mutable-task.json"
    snapshot.write_text('{"task":"snapshot"}', encoding="utf-8")
    legacy.write_text('{"task":"legacy"}', encoding="utf-8")
    (rd / "ui_meta.json").write_text(
        json.dumps({"task_file": str(legacy)}), encoding="utf-8")

    assert task_file_for(rd) == str(snapshot)
    snapshot.unlink()
    assert task_file_for(rd) == str(legacy)
    legacy.unlink()
    assert task_file_for(rd) is None


def _ack_marked(rd, command_id=None):
    """Emit the exact causal acknowledgement a real Engine writes after folding the intent."""
    events = EventStore(rd / "events.jsonl").read_all()
    acked = {(str((event.data or {}).get("command_id")), (event.data or {}).get("event_seq"))
             for event in events if event.type == "command_ack"}
    marked = [event for event in events
              if (event.data or {}).get("_command_id")
              and (command_id is None or (event.data or {}).get("_command_id") == command_id)]
    assert marked, "no marked command intent to acknowledge"
    intent = marked[-1]
    identity = (str((intent.data or {})["_command_id"]), intent.seq)
    if identity not in acked:
        EventStore(rd / "events.jsonl").append(
            "command_ack", {"command_id": identity[0], "event_seq": identity[1]})
    return intent


def _wait_for_intent(rd, command_id, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in EventStore(rd / "events.jsonl").read_all():
            if (event.data or {}).get("_command_id") == command_id:
                return event
        time.sleep(0.005)
    raise AssertionError(f"marked intent for {command_id} was not appended")


def test_every_control_event_has_one_explicit_engine_policy():
    assert set(CONTROL_SPECS) == set(CONTROL_EVENTS)
    assert CONTROL_SPECS["run_abort"].engine_policy is EnginePolicy.ENSURE_DRIVER_PRESERVE_STOP
    assert CONTROL_SPECS["restart"].engine_policy is EnginePolicy.RESTART_AFTER_EXIT
    assert CONTROL_SPECS["restart"].postcondition == "restart_served"
    assert CONTROL_DATA_FIELDS["restart"] == frozenset()
    assert CONTROL_SPECS["set_strategy"].engine_policy is EnginePolicy.ENSURE_RUNNING
    assert {name for name, spec in CONTROL_SPECS.items()
            if spec.engine_policy is EnginePolicy.NO_SPAWN} == {
        "pause", "node_abort", "hint", "annotation", "promote",
        "comment_created", "comment_edited", "comment_resolution_changed", "concept_tag_edited",
        "run_concepts",
        "hypothesis_added", "hypothesis_updated",
        # Layer 6 operator Card steering: folded intents never spawn or wake compute.
        "card_reprioritized", "card_edited", "card_resource_pinned", "card_dropped",
    }


def test_card_control_folds_without_waking_a_dead_engine(tmp_path):
    rd = _seed(tmp_path)
    EventStore(rd / "events.jsonl").append("card_added", {
        "id": "card-1", "statement": "immutable seed", "source": "researcher",
        "idea": {"operator": "draft", "params": {}, "space": {}},
    })
    driver = _Driver(alive=False)
    client, _srv = _client(tmp_path, driver)

    response = _post(
        client, "card_edited", {"id": "card-1", "statement": "display only"},
        key="card-edit-no-spawn",
    )
    assert response.status_code == 200
    record = _terminal(client, response.json())
    assert record["status"] == "succeeded"
    assert driver.calls == []
    event = [event for event in EventStore(rd / "events.jsonl").read_all()
             if event.type == "card_edited"][-1]
    assert event.data["source"] == "operator"
    assert event.data["statement"] == "display only"
    state = fold(EventStore(rd / "events.jsonl").read_all())
    assert state.cards["card-1"].statement == "display only"
    assert state.cards["card-1"].seed_statement == "immutable seed"


def test_idempotency_is_durable_one_event_and_key_digest_only(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    raw_key = "do-not-store-this-idempotency-key"
    body = {"type": "hint", "data": {"text": "try robust scaling"},
            "expected_generation": _generation(client)}

    missing = client.post("/api/runs/demo/commands", json=body)
    assert missing.status_code == 400
    first = client.post("/api/runs/demo/commands", headers={"Idempotency-Key": raw_key}, json=body)
    assert first.status_code == 200 and first.json()["status"] == "succeeded"
    second = client.post("/api/runs/demo/commands", headers={"Idempotency-Key": raw_key}, json=body)
    assert second.json() == first.json()
    assert _types(rd).count("hint") == 1

    conflict = client.post("/api/runs/demo/commands", headers={"Idempotency-Key": raw_key},
                           json={"type": "hint", "data": {"text": "different"},
                                 "expected_generation": body["expected_generation"]})
    assert conflict.status_code == 409
    record_file = next((rd / ".commands").glob("cmd_*.json"))
    raw = record_file.read_text(encoding="utf-8")
    assert raw_key not in raw
    assert hashlib.sha256(raw_key.encode()).hexdigest() in raw
    public = first.json()
    assert {"id", "status", "event_type", "error"}.issubset(public)
    assert "data" not in public and "idempotency_key_digest" not in public and "payload_digest" not in public


def test_new_command_requires_a_strict_observed_generation_but_normalizes_hex_case(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    generation = _generation(client)

    for value in (None, 123, generation[:-1], f" {generation}", "g" * 64):
        response = client.post(
            "/api/runs/demo/commands", headers={"Idempotency-Key": f"invalid-{value!r}"},
            json={"type": "hint", "data": {"text": "must not append"},
                  "expected_generation": value})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_run_generation"

    accepted = client.post(
        "/api/runs/demo/commands", headers={"Idempotency-Key": "uppercase-generation"},
        json={"type": "hint", "data": {"text": "normalized"},
              "expected_generation": generation.upper()})
    assert accepted.status_code == 200 and accepted.json()["status"] == "succeeded"
    record = json.loads(next((rd / ".commands").glob("cmd_*.json")).read_text(encoding="utf-8"))
    assert record["run_generation"] == generation


def test_empty_event_log_exposes_no_generation_and_rejects_new_commands(tmp_path):
    rd = tmp_path / "empty"
    rd.mkdir()
    (rd / "events.jsonl").write_bytes(b"")
    client, _srv = _client(tmp_path, _Driver())

    state = client.get("/api/runs/empty/state")
    assert state.status_code == 200 and state.json()["generation"] is None
    response = client.post(
        "/api/runs/empty/commands", headers={"Idempotency-Key": "before-run-started"},
        json={"type": "hint", "data": {"text": "too early"},
              "expected_generation": "a" * 64})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_generation_unavailable"
    assert not (rd / ".commands").exists()


def test_run_generation_reads_only_the_first_durable_event(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    first = EventStore(rd / "events.jsonl").read_all()[0]
    expected = run_generation_token([first])
    requested_paths = []

    def first_only(path):
        requested_paths.append(Path(path))
        yield first.model_dump(mode="json")
        raise AssertionError("run generation consumed an event after the first durable record")

    monkeypatch.setattr(run_commands_module, "iter_event_jsonl", first_only)
    assert srv.commands.run_generation(rd) == expected
    assert requested_paths == [rd / "events.jsonl"]

    def missing_during_open(_path):
        raise FileNotFoundError("generation replaced before open")
        yield  # pragma: no cover - keep this a generator so failure happens on first next()

    monkeypatch.setattr(run_commands_module, "iter_event_jsonl", missing_during_open)
    assert srv.commands.run_generation(rd) == ""


def test_run_generation_first_record_matches_eventstore_durability_semantics(tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    path = rd / "events.jsonl"
    raw = path.read_bytes()
    first_line = raw.splitlines(keepends=True)[0]
    expected = run_generation_token(EventStore(path).read_all())

    path.write_bytes(b"")
    assert srv.commands.run_generation(rd) == ""
    path.write_bytes(first_line.rstrip(b"\n"))
    assert srv.commands.run_generation(rd) == ""
    path.write_bytes(b"{not-json}\n" + first_line)
    assert srv.commands.run_generation(rd) == ""
    path.write_bytes(b"{}\n" + first_line)
    assert srv.commands.run_generation(rd) == ""
    path.write_bytes(b'{"seq":0,"type":"run_started","data":[]}\n' + first_line)
    assert srv.commands.run_generation(rd) == ""
    path.write_bytes(first_line + b"{not-json}\n")
    assert srv.commands.run_generation(rd) == expected


def test_delayed_first_submit_is_rejected_after_generation_replacement(monkeypatch, tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver()
    _client_unused, srv = _client(tmp_path, driver)
    generation_a = srv.commands.run_generation(rd)
    path_resolved = threading.Event()
    result = []
    original_path = srv.commands._path

    def signal_after_path_validation(*args, **kwargs):
        path = original_path(*args, **kwargs)
        path_resolved.set()
        return path

    monkeypatch.setattr(srv.commands, "_path", signal_after_path_validation)

    def delayed_submit():
        try:
            srv.commands.submit(
                rd, "delayed-first-key", "hint", {"text": "must stay in A"},
                expected_generation=generation_a)
        except HTTPException as exc:
            result.append(exc)

    # Formed for A, but its first sequenced admission occurs only after B replaces the log.
    with srv.commands.sequence(rd):
        worker = threading.Thread(target=delayed_submit)
        worker.start()
        assert path_resolved.wait(1.0)
        (rd / "events.jsonl").rename(rd / "events.jsonl.generation-a")
        EventStore(rd / "events.jsonl").append(
            "run_started", {"run_id": "demo", "task_id": "task-b", "goal": "b",
                            "direction": "min"})
    worker.join(1.0)

    assert len(result) == 1 and result[0].status_code == 409
    assert result[0].detail["code"] == "run_generation_changed"
    assert result[0].detail["expected_generation"] == generation_a
    assert result[0].detail["current_generation"] == srv.commands.run_generation(rd)
    assert not list((rd / ".commands").glob("cmd_*.json"))
    assert _types(rd) == ["run_started"]
    assert driver.calls == []


def test_state_cache_changes_generation_when_replacement_reuses_seq_size_and_mtime(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    first = client.get("/api/runs/demo/state").json()
    old_path = rd / "events.jsonl"
    old_bytes = old_path.read_bytes()
    old_stat = old_path.stat()

    # Keep the log byte length, seq range and mtime identical while changing its first durable event.
    # The file identity/ctime portion of the cache key must still separate generation B from A.
    marker = b'"goal":"g"'
    assert marker in old_bytes
    replacement = old_bytes.replace(marker, b'"goal":"b"', 1)
    ts_start = replacement.index(b'"ts":') + len(b'"ts":')
    ts_end = replacement.index(b",", ts_start)
    timestamp = bytearray(replacement[ts_start:ts_end])
    # Mutating the final fractional digit is flaky for epoch-scale timestamps: two different decimal
    # spellings can round to the same IEEE-754 float and therefore the same canonical generation.
    # Change the units digit instead; length stays fixed and the parsed float differs by exactly one.
    decimal = timestamp.find(b".")
    digit = decimal - 1 if decimal > 0 else len(timestamp) - 1
    assert timestamp[digit] in b"0123456789"
    current_digit = timestamp[digit] - ord("0")
    timestamp[digit] = ord("0") + (current_digit + 1 if current_digit < 9 else 8)
    assert float(timestamp) != float(replacement[ts_start:ts_end])
    replacement = replacement[:ts_start] + bytes(timestamp) + replacement[ts_end:]
    old_path.rename(rd / "events.jsonl.generation-a")
    old_path.write_bytes(replacement)
    os.utime(old_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    new_stat = old_path.stat()
    assert new_stat.st_size == old_stat.st_size and new_stat.st_mtime_ns == old_stat.st_mtime_ns

    second = client.get("/api/runs/demo/state").json()
    assert second["seq"] == first["seq"] and second["max_seq"] == first["max_seq"]
    assert second["state"]["goal"] == "b"
    assert second["generation"] != first["generation"]


def test_runs_summary_cache_invalidates_same_size_and_mtime_log_replacement(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    first = next(row for row in client.get("/api/runs").json() if row["run_id"] == "demo")
    old_path = rd / "events.jsonl"
    old_bytes = old_path.read_bytes()
    old_stat = old_path.stat()

    marker = b'"goal":"g"'
    assert marker in old_bytes
    replacement = old_bytes.replace(marker, b'"goal":"b"', 1)
    old_path.rename(rd / "events.jsonl.generation-a-summary")
    old_path.write_bytes(replacement)
    os.utime(old_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    new_stat = old_path.stat()
    assert new_stat.st_size == old_stat.st_size
    assert new_stat.st_mtime_ns == old_stat.st_mtime_ns
    assert (new_stat.st_ino, new_stat.st_ctime_ns) != (old_stat.st_ino, old_stat.st_ctime_ns)

    second = next(row for row in client.get("/api/runs").json() if row["run_id"] == "demo")
    assert first["goal"] == "g"
    assert second["goal"] == "b"


def test_state_event_count_is_full_folded_projection_count_for_gaps_cache_hits_and_prefixes(tmp_path):
    rd = _seed(tmp_path)
    log = rd / "events.jsonl"
    # Append a third dense row, then break the *tail* seq. A non-dense logical tail fails closed (a seq
    # gap is corruption, not a tolerated repair artifact — the engine appends densely and repair-log
    # truncates the tail), so the recoverable projection is the dense seq-0..1 prefix while the gapped
    # third row is dropped. event_count must still report that full recoverable count — never the raw
    # row total, never a per-request history-prefix length.
    EventStore(log).append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "b"}, "code": "print(2)"})
    rows = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    rows[-1]["seq"] = 7  # tail gap -> the third row is dropped from the recoverable prefix
    log.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
                   encoding="utf-8")
    client, srv = _client(tmp_path, _Driver())

    first = client.get("/api/runs/demo/state").json()
    assert first["event_count"] == 2 and len(rows) == 3  # recoverable folded count, not len(rows)
    assert first["seq"] == first["max_seq"] == 1

    original_events = srv.events
    srv.events = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss"))
    try:
        cached = client.get("/api/runs/demo/state").json()
    finally:
        srv.events = original_events
    assert cached["event_count"] == 2 and cached["seq"] == 1

    historical = client.get("/api/runs/demo/state", params={"seq": 0}).json()
    assert historical["seq"] == 0
    assert historical["event_count"] == 2  # full folded projection, not the history prefix length


def test_resume_running_is_noop_without_event_or_spawn(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver)
    record = _post(client, "resume").json()
    assert record["status"] == "noop"
    assert "resume" not in _types(rd) and driver.calls == []


def test_preexisting_driver_requires_exact_causal_ack_not_unrelated_progress(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=0.10, observation=3.0)
    record = _post(client, "set_strategy", {"strategy": {"policy": "greedy"}}).json()
    intent = _wait_for_intent(rd, record["id"])
    assert driver.calls == []

    # Neither liveness nor unrelated domain progress acknowledges this command. The live driver keeps
    # the observation deadline open through a long evaluation, but success remains exact-ack only.
    EventStore(rd / "events.jsonl").append("policy_decision", {"candidate_scores": {}})
    time.sleep(0.02)
    current = client.get(f"/api/runs/demo/commands/{record['id']}").json()
    assert current["status"] == "executing"
    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": record["id"], "event_seq": intent.seq + 1})
    time.sleep(0.03)
    assert client.get(f"/api/runs/demo/commands/{record['id']}").json()["status"] == "executing"

    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": record["id"], "event_seq": intent.seq})
    assert _terminal(client, record)["status"] == "succeeded"


def test_finalize_from_paused_spawns_resume_without_clearing_stop(tmp_path):
    rd = _seed(tmp_path, paused=True)
    driver = _Driver()

    def finish():
        EventStore(rd / "events.jsonl").append("run_finished", {"reason": "aborted"})

    driver.on_spawn = finish
    client, srv = _client(tmp_path, driver)
    record = _terminal(client, _post(client, "run_abort", {"reason": "finalized"}).json())
    assert record["status"] == "succeeded"
    types = _types(rd)
    assert types.count("run_abort") == 1
    assert "run_reopened" not in types and "resume" not in types
    assert driver.calls and driver.calls[0][0][0] == "resume"
    assert srv.state(rd).finished is True

    again = _post(client, "run_abort", {"reason": "finalized"}, key="finalize-again").json()
    assert again["status"] == "noop"
    assert len(driver.calls) == 1 and _types(rd).count("run_abort") == 1


def test_repeated_pending_finalize_attaches_and_phase_precedes_paused(tmp_path):
    rd = _seed(tmp_path, paused=True, finalizing=True)
    driver = _Driver()

    def finish():
        EventStore(rd / "events.jsonl").append("run_finished", {"reason": "aborted"})

    driver.on_spawn = finish
    client, srv = _client(tmp_path, driver)
    assert srv.phase(srv.state(rd)) == PHASE_FINALIZING
    record = _terminal(client, _post(client, "run_abort", {"reason": "finalized"}).json())
    assert record["status"] == "succeeded" and record.get("attached") is True
    assert _types(rd).count("run_abort") == 1


def test_finalize_payload_is_canonical_and_external_attach_must_match(tmp_path):
    rd = _seed(tmp_path, finalizing=True)
    driver = _Driver()
    client, _srv = _client(tmp_path, driver)

    mismatch = _post(client, "run_abort", {"reason": "different"}, key="external-mismatch").json()
    assert mismatch["status"] == "rejected"
    assert mismatch["error"]["code"] == "finalize_payload_conflict"
    assert driver.calls == [] and _types(rd).count("run_abort") == 1

    null_reason = _post(client, "run_abort", {"reason": None}, key="null-finalize").json()
    unknown = _post(
        client, "run_abort", {"reason": "finalized", "extra": True}, key="unknown-finalize").json()
    assert null_reason["status"] == unknown["status"] == "rejected"
    assert null_reason["error"]["code"] == unknown["error"]["code"] == "invalid_command"

    # Missing reason canonicalizes to the same payload as the existing legacy finalize.
    def finish():
        driver.alive = False
        EventStore(rd / "events.jsonl").append("run_finished", {"reason": "aborted"})

    driver.on_spawn = finish
    matched = _terminal(client, _post(client, "run_abort", {}, key="external-match").json())
    assert matched["status"] == "succeeded" and len(driver.calls) == 1


def test_legacy_empty_finalize_gets_nonempty_canonical_reason(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    response = client.post(
        "/api/runs/demo/control", json={"type": "run_abort", "data": {}})
    assert response.status_code == 200
    abort = [event for event in EventStore(rd / "events.jsonl").read_all()
             if event.type == "run_abort"][-1]
    assert abort.data == {"reason": "finalized"}
    assert fold(EventStore(rd / "events.jsonl").read_all()).stop_requested == "finalized"


def test_attached_finalize_fails_closed_if_external_intent_changes_before_worker(tmp_path):
    from looplab.events.eventstore import write_jsonl_atomic

    rd = _seed(tmp_path, finalizing=True)
    client, srv = _client(tmp_path, _Driver())
    original_start = srv.commands._start_worker
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    record = _post(client, "run_abort", {"reason": "finalized"}, key="attach-then-rewrite").json()
    assert record["status"] == "accepted" and record["attached"] is True

    rows = []
    for event in EventStore(rd / "events.jsonl").read_all():
        row = event.model_dump(mode="json")
        if event.type == "run_abort":
            row["data"] = {"reason": "changed"}
        rows.append(row)
    write_jsonl_atomic(rd / "events.jsonl", rows)

    srv.commands._start_worker = original_start
    current = client.get(f"/api/runs/demo/commands/{record['id']}").json()
    failed = _terminal(client, current)
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "command_intent_missing"
    assert _types(rd).count("run_abort") == 1


def test_attached_finalize_cannot_succeed_against_later_external_abort(tmp_path):
    rd = _seed(tmp_path, finalizing=True)
    client, srv = _client(tmp_path, _Driver())
    original_start = srv.commands._start_worker
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    record = _post(client, "run_abort", {"reason": "finalized"}, key="attach-superseded").json()
    assert record["status"] == "accepted" and record["attached"] is True

    store = EventStore(rd / "events.jsonl")
    store.append("run_abort", {"reason": "different-external-finalize"})
    store.append("run_finished", {"reason": "aborted"})
    srv.commands._start_worker = original_start
    observed = client.get(f"/api/runs/demo/commands/{record['id']}").json()
    assert observed["status"] == "failed"
    assert observed["error"]["code"] == "command_intent_missing"
    assert _types(rd).count("run_abort") == 2


@pytest.mark.parametrize("event_type", ["pause", "restart", "resume"])
def test_stop_and_resume_reject_during_pending_finalize(tmp_path, event_type):
    _seed(tmp_path, finalizing=True)
    client, _srv = _client(tmp_path, _Driver(alive=True))
    record = _post(client, event_type).json()
    assert record["status"] == "rejected"
    assert record["error"] == {
        "code": "finalize_in_progress",
        "message": record["error"]["message"],
        "retryable": True,
        "remediation": record["error"]["remediation"],
    }


def test_finish_seq_only_pending_is_visible_and_rejects_new_commands(tmp_path):
    rd = _seed(tmp_path)
    EventStore(rd / "events.jsonl").append(
        "run_finished", {"reason": "done", "finalization_required": True})
    client, _srv = _client(tmp_path, _Driver())

    state = client.get("/api/runs/demo/state").json()["state"]
    listed = client.get("/api/runs").json()[0]
    assert state["finalization_incomplete"] is True and state["phase"] == PHASE_FINALIZING
    assert listed["finalization_incomplete"] is True and listed["phase"] == PHASE_FINALIZING

    for event_type in ("pause", "restart", "resume"):
        record = _post(client, event_type, key=f"finish-seq-{event_type}").json()
        assert record["status"] == "rejected"
        assert record["error"]["code"] == "finalize_in_progress"


def test_pause_waits_for_folded_pause_and_no_engine(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=0.30)
    record = _post(client, "pause").json()
    threading.Timer(0.04, lambda: setattr(driver, "alive", False)).start()
    done = _terminal(client, record)
    assert done["status"] == "succeeded"
    assert fold(EventStore(rd / "events.jsonl").read_all()).paused is True
    assert driver.calls == []


def test_restart_waits_for_old_owner_then_requires_exact_replacement_serve(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=0.30, observation=1.0)

    command = _post(client, "restart", key="one-durable-restart").json()
    intent = _wait_for_intent(rd, command["id"])
    folded = fold(EventStore(rd / "events.jsonl").read_all())
    assert intent.type == "restart" and folded.paused and folded.resume_pending()

    # The command must not Popen while the owner that folded the pause still holds engine.lock.
    time.sleep(0.05)
    assert driver.calls == []

    def replacement_serves_restart():
        store = EventStore(rd / "events.jsonl")
        store.append("resume", {})
        store.append("resume_served", {})
        driver.alive = True

    driver.on_spawn = replacement_serves_restart
    driver.alive = False
    done = _terminal(client, command, timeout=1.5)
    assert done["status"] == "succeeded"
    assert len(driver.calls) == 1 and driver.calls[0][0][0] == "resume"

    events = EventStore(rd / "events.jsonl").read_all()
    launch = next(event for event in events
                  if event.type == "resume_requested" and event.data.get("launch_claim"))
    served = next(event for event in events if event.type == "resume_served")
    assert intent.seq < launch.seq < served.seq
    assert launch.data["request_seq"] == intent.seq
    assert not fold(events).paused and not fold(events).resume_pending()

    # Lost response/browser retry is record lookup, not a second pause or replacement process.
    duplicate = _post(client, "restart", key="one-durable-restart")
    assert duplicate.status_code == 200 and duplicate.json()["id"] == command["id"]
    assert duplicate.json()["status"] == "succeeded"
    assert _types(rd).count("restart") == 1 and len(driver.calls) == 1


def test_spawn_exception_and_no_progress_startup_are_structured_failures(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(error=OSError("permission denied"))
    client, _srv = _client(tmp_path, driver)
    failed = _terminal(client, _post(client, "budget_extend", {"add_nodes": 2}).json())
    assert failed["status"] == "failed" and failed["error"]["code"] == "spawn_failed"
    assert set(failed["error"]) == {"code", "message", "retryable", "remediation"}
    assert failed["error"]["retryable"] is True
    assert _types(rd).count("budget_extend") == 1

    rd2 = _seed(tmp_path, "other")
    app = make_app(tmp_path)
    srv = app.state.looplab
    silent = _Driver()
    srv.commands = RunCommandService(
        srv, engine_alive=silent.is_alive, spawn_engine=silent.spawn,
        process_alive=silent.is_process_alive,
        startup_timeout=0.05, command_timeout=0.15, poll_interval=0.01,
        max_observation_timeout=0.25)
    client2 = TestClient(app)
    response = client2.post("/api/runs/other/commands", headers={"Idempotency-Key": "silent"},
                            json={"type": "budget_extend", "data": {"add_nodes": 1},
                                  "expected_generation": _generation(client2, "other")})
    record = response.json()
    deadline = time.time() + 1
    while record.get("status") not in TERMINAL_STATUSES and time.time() < deadline:
        time.sleep(0.01)
        record = client2.get(f"/api/runs/other/commands/{record['id']}").json()
    assert record["status"] == "timed_out"
    assert record["error"]["code"] == "engine_start_uncertain"
    assert record["error"]["retryable"] is False
    assert srv.commands.spawn_inflight(rd2)
    assert len(silent.calls) == 1
    assert _types(rd2).count("budget_extend") == 1

    # Neither explicit retry nor a different command may infer child death from the timeout and
    # Popen a second driver. The same quarantine also covers a server restart/new request owner.
    blocked_retry = client2.post(f"/api/runs/other/commands/{record['id']}/retry")
    assert blocked_retry.status_code == 409
    assert blocked_retry.json()["detail"]["code"] == "engine_start_uncertain"
    blocked_new = client2.post(
        "/api/runs/other/commands", headers={"Idempotency-Key": "another-command"},
        json={"type": "hint", "data": {"text": "do not race the cold child"},
              "expected_generation": _generation(client2, "other")})
    assert blocked_new.status_code == 409
    assert blocked_new.json()["detail"]["code"] == "engine_start_uncertain"
    assert len(silent.calls) == 1

    # A definitive PID-dead observation releases quarantine but GET itself remains observational.
    silent.pid_running = False
    refresh = client2.get(f"/api/runs/other/commands/{record['id']}")
    assert refresh.status_code == 200
    refreshed = refresh.json()
    assert refreshed["error"]["code"] == "postcondition_timeout"
    assert refreshed["error"]["retryable"] is True
    assert not srv.commands.spawn_inflight(rd2) and len(silent.calls) == 1

    def retry_driver():
        silent.alive = True
        _ack_marked(rd2, record["id"])

    silent.pid_running = True
    silent.on_spawn = retry_driver
    retried = client2.post(f"/api/runs/other/commands/{record['id']}/retry").json()
    assert _terminal(client2, retried, run_id="other")["status"] == "succeeded"
    assert len(silent.calls) == 2


@pytest.mark.parametrize("event_type", ["approval_granted", "spec_approved"])
def test_approval_commands_validate_the_active_gate(tmp_path, event_type):
    _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    payload = {"node_id": 0} if event_type == "approval_granted" else {}
    record = _post(client, event_type, payload).json()
    assert record["status"] == "rejected"
    assert record["error"]["code"] in {"approval_not_requested", "ratification_not_requested"}
    assert event_type not in _types(tmp_path / "demo")


def test_reset_normalization_is_shared_by_legacy_and_command_routes(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver()

    def start_driver():
        driver.alive = True
        _ack_marked(rd)

    driver.on_spawn = start_driver
    client, _srv = _client(tmp_path, driver)
    legacy_bad = client.post("/api/runs/demo/control",
                             json={"type": "node_reset", "data": {"node_id": 99}})
    assert legacy_bad.status_code == 404
    command_bad = _post(client, "node_reset", {"node_id": 99}, key="bad-reset").json()
    assert command_bad["status"] == "rejected"

    good = _terminal(client, _post(
        client, "node_reset", {"node_id": "0", "from_stage": " train "}, key="good-reset").json())
    assert good["status"] == "succeeded"
    reset = [event for event in EventStore(rd / "events.jsonl").read_all()
             if event.type == "node_reset"][-1]
    assert reset.data["node_id"] == 0 and reset.data["from_stage"] == "train"
    assert "run_reopened" not in _types(rd)


def test_lifecycle_normalizer_requires_exact_attempt_and_parent_snapshot(tmp_path):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    store.append("approval_requested", {"node_id": 0, "generation": 1})
    _client_unused, srv = _client(tmp_path, _Driver())

    lifecycle = {
        "node_abort": {"node_id": 0},
        "node_reset": {"node_id": 0, "from_stage": "eval"},
        "approval_granted": {"node_id": 0},
        "force_confirm": {"node_id": 0},
        "force_ablate": {"node_id": 0},
        "fork": {"from_node_id": 0},
        "promote": {"node_id": 0},
    }
    assert all("generation" in CONTROL_DATA_FIELDS[event_type]
               for event_type in lifecycle)
    for event_type, base in lifecycle.items():
        with pytest.raises(HTTPException) as missing:
            normalize_control(srv, rd, event_type, base)
        assert missing.value.status_code == 409 and "generation is required" in str(missing.value.detail)
        with pytest.raises(HTTPException) as stale:
            normalize_control(srv, rd, event_type, {**base, "generation": 0})
        assert stale.value.status_code == 409 and "not 0" in str(stale.value.detail)
        normalized = normalize_control(srv, rd, event_type, {**base, "generation": 1})
        assert normalized["generation"] == 1

    idea = {"operator": "manual", "params": {}, "rationale": ""}
    assert "parent_generations" in CONTROL_DATA_FIELDS["inject_node"]
    with pytest.raises(HTTPException, match="parent generation is required"):
        normalize_control(srv, rd, "inject_node", {"idea": idea, "parent_id": 0})
    with pytest.raises(HTTPException, match="stale parent"):
        normalize_control(srv, rd, "inject_node", {
            "idea": idea, "parent_id": 0, "parent_generations": {"0": 0}})
    normalized_inject = normalize_control(srv, rd, "inject_node", {
        "idea": idea, "parent_id": 0, "parent_generations": {"0": 1}})
    assert normalized_inject["parent_generations"] == {"0": 1}


@pytest.mark.parametrize("idea", [
    {"operator": "manual", "concept_mode": "future-v2"},
    {"operator": "manual", "concept_mode": "full", "concepts_added": ["x/y"]},
    {"operator": "manual", "concept_mode": "delta", "concepts": ["x/y"]},
    {"operator": "manual", "concept_mode": "full", "concepts": ["bad!"]},
])
def test_inject_rejects_invalid_modern_concept_envelopes_and_upgrades_legacy_fields(tmp_path, idea):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    with pytest.raises(HTTPException) as error:
        normalize_control(srv, rd, "inject_node", {"idea": idea})
    assert error.value.status_code == 400
    legacy_delta = normalize_control(srv, rd, "inject_node", {
        "idea": {"operator": "manual", "concepts_added": ["legacy/x"]}})
    assert legacy_delta["idea"]["concept_mode"] == "delta"
    assert legacy_delta["idea"]["concepts_added"] == ["legacy/x"]
    assert legacy_delta["idea"]["concepts"] == legacy_delta["idea"]["concepts_removed"] == []
    legacy_full = normalize_control(srv, rd, "inject_node", {
        "idea": {"operator": "manual", "concepts": ["legacy/x"]}})
    assert legacy_full["idea"]["concept_mode"] == "full"
    assert legacy_full["idea"]["concepts"] == ["legacy/x"]
    absent = normalize_control(srv, rd, "inject_node", {"idea": {"operator": "manual"}})
    assert "concept_mode" not in absent["idea"] and "concepts" not in absent["idea"]


@pytest.mark.parametrize("concept_fields", [
    {"concepts": [f"axis/c{index:03}" for index in range(65)]},
    {"concepts_added": ["bad!"]},
    {"concepts_added": ["Model/A", "model/a"]},
    {"concepts_added": ["model/a"], "concepts_removed": ["Model/A"]},
])
def test_inject_rejects_invalid_legacy_concept_fields_before_tolerant_reader(tmp_path, concept_fields):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())

    with pytest.raises(HTTPException) as error:
        normalize_control(srv, rd, "inject_node", {
            "idea": {"operator": "manual", **concept_fields},
        })

    assert error.value.status_code == 400


def test_cross_run_import_snapshots_effective_membership_and_strips_partial_source(tmp_path):
    target = _seed(tmp_path, "target")
    exact = _seed(tmp_path, "exact")
    exact_store = EventStore(exact / "events.jsonl")
    exact_store.append("run_concepts", {"concepts": ["base/x"]})
    exact_store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base",
                 "concept_mode": "delta", "concepts_added": ["child/y"]},
        "code": "print(1)",
    })
    partial = _seed(tmp_path, "partial")
    partial_store = EventStore(partial / "events.jsonl")
    partial_store.append("run_concepts", {
        "concepts": [f"base/c{i:03d}" for i in range(65)]})
    partial_store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base",
                 "concept_mode": "delta"},
        "code": "print(1)",
    })
    _client_unused, srv = _client(tmp_path, _Driver())

    imported = normalize_control(srv, target, "inject_node", {
        "source_run": "exact", "source_node": 0})
    assert imported["idea"]["concept_mode"] == "full"
    assert imported["idea"]["concepts"] == ["base/x", "child/y"]
    assert imported["idea"]["concepts_added"] == imported["idea"]["concepts_removed"] == []

    stripped = normalize_control(srv, target, "inject_node", {
        "source_run": "partial", "source_node": 0})
    for field in ("concept_mode", "concepts", "concepts_added", "concepts_removed"):
        assert field not in stripped["idea"]


def test_approval_normalizer_defaults_to_pending_subject_and_requires_active_gate(tmp_path):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 10.0})
    store.append("node_created", {
        "node_id": 7, "generation": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "better"},
    })
    store.append("node_evaluated", {"node_id": 7, "generation": 0, "metric": 1.0})
    store.append("approval_requested", {"node_id": 0, "generation": 0})
    _client_unused, srv = _client(tmp_path, _Driver())

    default = normalize_control(srv, rd, "approval_granted", {})
    assert default == {"node_id": 0, "generation": 0}  # pending subject, not best #7
    explicit = normalize_control(
        srv, rd, "approval_granted", {"node_id": 7, "generation": 0})
    assert explicit == {"node_id": 7, "generation": 0}

    no_gate = _seed(tmp_path, "no-gate")
    with pytest.raises(HTTPException) as approval_error:
        normalize_control(srv, no_gate, "approval_granted", {"node_id": 0, "generation": 0})
    assert approval_error.value.detail["code"] == "approval_not_requested"
    EventStore(no_gate / "events.jsonl").append(
        "spec_proposed", {"eval_spec": {"cmd": "python train.py"}})
    with pytest.raises(HTTPException) as ratify_error:
        normalize_control(srv, no_gate, "spec_approved", {})
    assert ratify_error.value.detail["code"] == "ratification_not_requested"


def test_engine_driving_command_rejects_unknown_liveness_without_spawn(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=False)
    client, srv = _client(tmp_path, driver)
    srv.commands.engine_liveness = lambda _rd: None

    response = _post(client, "resume", key="unknown-liveness-resume")
    assert response.status_code == 200
    record = response.json()
    assert record["status"] == "rejected"
    assert record["error"]["code"] == "engine_liveness_unknown"
    assert record["error"]["retryable"] is False
    assert driver.calls == []
    assert "resume" not in _types(rd)


def test_delayed_approval_command_cannot_rebind_to_a_new_request(tmp_path):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0})
    store.append("approval_requested", {"node_id": 0, "generation": 0})
    client, srv = _client(tmp_path, _Driver(alive=False))
    srv.commands._start_worker = lambda *_args, **_kwargs: None

    admitted = _post(
        client, "approval_granted", {"node_id": 0, "generation": 0},
        key="delayed-approval-request-1").json()
    assert admitted["status"] == "accepted"

    # Request 1 is resolved externally while this admitted worker is delayed, then request 2 opens.
    store.append("approval_granted", {"node_id": 0, "generation": 0})
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.9})
    request_2 = store.append("approval_requested", {"node_id": 0, "generation": 1})
    path = srv.commands._path(rd, admitted["id"])
    srv.commands._execute(rd, path, srv.commands._load(path), claimed=False)

    final = srv.commands._load(path)
    assert final["status"] == "rejected"
    assert final["error"]["code"] == "approval_state_changed"
    assert final["error"]["retryable"] is False
    grants = [event for event in store.read_all() if event.type == "approval_granted"]
    assert len(grants) == 1
    assert all((event.data or {}).get("_command_id") != admitted["id"] for event in grants)
    assert fold(store.read_all()).approval_request_seq == request_2.seq


def test_approval_append_cas_rejects_an_intervening_event(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0})
    store.append("approval_requested", {"node_id": 0, "generation": 0})
    client, srv = _client(tmp_path, _Driver(alive=False))
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    admitted = _post(
        client, "approval_granted", {"node_id": 0, "generation": 0},
        key="approval-tail-cas").json()

    decide = srv.commands._decision

    def advance_tail(run_dir, event_type):
        decision = decide(run_dir, event_type)
        store.append("diagnostic_only", {})
        return decision

    monkeypatch.setattr(srv.commands, "_decision", advance_tail)
    path = srv.commands._path(rd, admitted["id"])
    srv.commands._execute(rd, path, srv.commands._load(path), claimed=False)

    final = srv.commands._load(path)
    assert final["status"] == "rejected"
    assert final["error"]["code"] == "approval_state_changed"
    assert final["error"]["retryable"] is False
    assert not any(event.type == "approval_granted" for event in store.read_all())


def test_restart_append_cas_cannot_cross_a_concurrent_finalize(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    client, srv = _client(tmp_path, _Driver(alive=False))
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    admitted = _post(client, "restart", key="restart-finalize-race").json()
    assert admitted["status"] == "accepted"

    decide = srv.commands._decision

    def finalize_after_restart_admission(run_dir, event_type):
        result = decide(run_dir, event_type)
        if event_type == "restart":
            store.append("run_abort", {"reason": "operator-finalize-won"})
        return result

    monkeypatch.setattr(srv.commands, "_decision", finalize_after_restart_admission)
    path = srv.commands._path(rd, admitted["id"])
    srv.commands._execute(rd, path, srv.commands._load(path), claimed=False)

    final = srv.commands._load(path)
    assert final["status"] == "rejected"
    assert final["error"]["code"] == "restart_state_changed"
    assert "restart" not in _types(rd) and _types(rd).count("run_abort") == 1


@pytest.mark.parametrize("unavailable", ["tombstoned", "aborted"])
def test_normalizer_rejects_unavailable_inject_parent_and_source(tmp_path, unavailable):
    parent_rd = _seed(tmp_path, "parent")
    source_rd = _seed(tmp_path, "source")
    parent_store = EventStore(parent_rd / "events.jsonl")
    source_store = EventStore(source_rd / "events.jsonl")
    event = ("node_tombstoned", {"node_ids": [0]}) if unavailable == "tombstoned" else (
        "node_abort", {"node_id": 0, "generation": 0})
    parent_store.append(*event)
    source_store.append(*event)
    _client_unused, srv = _client(tmp_path, _Driver())
    idea = {"operator": "manual", "params": {}, "rationale": ""}

    with pytest.raises(HTTPException) as parent_error:
        normalize_control(srv, parent_rd, "inject_node", {
            "idea": idea, "parent_id": 0, "parent_generations": {"0": 0}})
    assert parent_error.value.status_code == 409 and unavailable in str(parent_error.value.detail)

    with pytest.raises(HTTPException) as source_error:
        normalize_control(srv, parent_rd, "inject_node", {
            "source_run": "source", "source_node": 0})
    assert source_error.value.status_code == 409 and unavailable in str(source_error.value.detail)


def test_real_engine_ack_is_exact_idempotent_and_replay_neutral(tmp_path):
    from looplab.engine.orchestrator import Engine

    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    intent = store.append("hint", {"text": "x", "_command_id": "cmd_" + "a" * 32})
    engine = object.__new__(Engine)
    engine.store = store

    snapshot = store.read_all()
    late = store.append("hint", {"text": "late", "_command_id": "cmd_" + "b" * 32})
    Engine._ack_commands(engine, snapshot)
    assert not any((event.data or {}).get("command_id") == "cmd_" + "b" * 32
                   for event in store.read_all() if event.type == "command_ack")
    Engine._ack_commands(engine, store.read_all())
    events = store.read_all()
    acks = [event for event in events if event.type == "command_ack"]
    assert len(acks) == 2
    assert acks[0].data == {"command_id": "cmd_" + "a" * 32, "event_seq": intent.seq}
    assert acks[1].data == {"command_id": "cmd_" + "b" * 32, "event_seq": late.seq}
    # Replay neutrality holds modulo the deliberately appended late hint; command_ack itself folds out.
    assert fold(events).model_dump(mode="json") == fold(snapshot + [late]).model_dump(mode="json")


def test_command_http_is_no_store_for_success_validation_and_early_auth(monkeypatch, tmp_path):
    _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    ok = _post(client, "hint", {"text": "cache contract"})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert "x-looplab-token" in ok.headers["vary"].lower()

    malformed = client.post(
        "/api/runs/demo/commands", headers={"Idempotency-Key": "broken",
                                            "Content-Type": "application/json"}, content="{")
    assert malformed.status_code == 400 and malformed.headers["cache-control"] == "no-store"
    missing = client.get("/api/runs/demo/commands/cmd_" + "0" * 32)
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"

    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    authed_app = make_app(tmp_path)
    denied = TestClient(authed_app).get("/api/runs/demo/commands/cmd_" + "0" * 32)
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"


def test_budget_extend_normalizes_canonical_and_legacy_parallel_axes(tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    data = normalize_control(srv, rd, "budget_extend", {
        "eval_parallel": 0, "llm_parallel": "0",
        "max_parallel": 1024, "parallel_build": 64,
    })
    assert data == {"eval_parallel": 0, "llm_parallel": 0,
                    "max_parallel": 1024, "parallel_build": 64}
    for bad in ({"eval_parallel": 1025}, {"llm_parallel": 65},
                {"max_parallel": -1}, {"parallel_build": True},
                {"eval_parallel": "9" * 5000}):
        with pytest.raises(HTTPException) as exc:
            normalize_control(srv, rd, "budget_extend", bad)
        assert exc.value.status_code == 400


def test_set_strategy_accepts_canonical_totals_lanes_and_atomic_card_scoring(tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    data = normalize_control(srv, rd, "set_strategy", {"strategy": {
        "eval_parallel": "0", "llm_parallel": 4,
        "llm_lane_limits": {"build": "0", "deep_research": 1, "engine": 2},
        "card_scoring": {
            "stance": "explore", "novelty_weight": 0.25, "coverage_weight": 1,
        },
    }})
    assert data == {"strategy": {
        "eval_parallel": 0, "llm_parallel": 4,
        "llm_lane_limits": {"build": 0, "deep_research": 1, "engine": 2},
        "card_scoring": {
            "stance": "explore", "novelty_weight": 0.25, "coverage_weight": 1.0,
        },
    }}
    assert normalize_control(srv, rd, "set_strategy", {
        "strategy": {"llm_lane_limits": {}},
    }) == {"strategy": {"llm_lane_limits": {}}}

    for strategy in (
        {"max_parallel": 2},
        {"llm_lane_limits": []},
        {"llm_lane_limits": {"unknown": 1}},
        {"llm_lane_limits": {"build": True}},
        {"llm_lane_limits": {"build": 65}},
        {"card_scoring": {"stance": "explore", "novelty_weight": 0.5}},
        {"card_scoring": {
            "stance": "wild", "novelty_weight": 0.5, "coverage_weight": 0.5,
        }},
        {"card_scoring": {
            "stance": "explore", "novelty_weight": True, "coverage_weight": 0.5,
        }},
    ):
        with pytest.raises(HTTPException) as exc:
            normalize_control(srv, rd, "set_strategy", {"strategy": strategy})
        assert exc.value.status_code == 400


def test_set_strategy_command_persists_explicit_empty_lane_clear(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver)

    record = _post(
        client,
        "set_strategy",
        {"strategy": {"llm_lane_limits": {}}},
        key="clear-lane-limits",
    ).json()
    intent = _wait_for_intent(rd, record["id"])

    assert intent.data["strategy"] == {"llm_lane_limits": {}}
    _ack_marked(rd, record["id"])
    assert _terminal(client, record)["status"] == "succeeded"


@pytest.mark.parametrize("event_type,data", [
    ("budget_extend", {"add_nodes": 0}),
    ("budget_extend", {"max_eval_seconds": "not-a-number"}),
    ("hint", {"text": []}),
    ("set_strategy", {"strategy": {"policy": "asha", "secret": "leak"}}),
    ("set_strategy", {"strategy": {"policy": "asha", "policy_params": {"c": 2}}}),
    ("resume", {"secret": "must not be persisted"}),
    ("restart", {"secret": "must not be persisted"}),
    ("pause", {"ignored": True}),
    ("fork", {"from_node_id": 0, "junk": True}),
    ("force_confirm", {"node_id": 999}),
    ("force_confirm", {"node_id": 0.9}),
    ("inject_node", {"idea": "not-an-object"}),
    ("inject_node", {"idea": {"foo": "bar"}}),
    ("inject_node", {"idea": {"rationale": "missing operator"}}),
    ("inject_node", {"idea": {"operator": "manual", "params": ["lr", 0.1]}}),
    ("inject_node", {"idea": {"operator": "manual"}, "files": {"x.py": ["not", "text"]}}),
    ("inject_node", {"idea": {"operator": "manual"}, "deleted": "x.py"}),
    ("inject_node", {"idea": {"operator": "manual"}, "origin": "other-run"}),
    ("inject_node", {"idea": {"operator": "manual"}, "files": {"../escape.py": "x"}}),
    ("inject_node", {"idea": {"operator": "manual"}, "files": {"report.txt:secret": "x"}}),
    ("inject_node", {"idea": {"operator": "manual"}, "files": {"CON.txt": "x"}}),
    ("inject_node", {"idea": {"operator": "manual"}, "parent_id": 0.9}),
    ("inject_node", {"idea": {"operator": "manual"}, "parent_ids": [0.9]}),
    ("inject_node", {"idea": {"operator": "manual"}, "parent_id": 0, "parent_ids": [0]}),
    ("inject_node", {"idea": {"operator": "manual"}, "surprise": True}),
    ("hypothesis_updated", {"id": "h", "status": []}),
    ("hypothesis_updated", {"id": "h", "status": "abandond"}),
])
def test_malformed_control_payload_is_rejected_before_durable_intent(tmp_path, event_type, data):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    response = _post(client, event_type, data, key=f"bad-{event_type}-{len(str(data))}")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert event_type not in _types(rd)


def test_same_key_is_observational_new_key_conflicts_and_explicit_retry_reuses_intent(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(error=OSError("first start failed"))
    client, _srv = _client(tmp_path, driver)
    body = {"add_nodes": 2}
    failed = _terminal(client, _post(client, "budget_extend", body, key="durable-budget").json())
    assert failed["status"] == "failed" and len(driver.calls) == 1

    same = _post(client, "budget_extend", body, key="durable-budget")
    assert same.status_code == 200 and same.json()["status"] == "failed"
    assert len(driver.calls) == 1 and _types(rd).count("budget_extend") == 1
    duplicate = _post(client, "budget_extend", body, key="different-key")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "retry_existing_command",
        "existing_command_id": failed["id"],
        "message": "An unresolved identical control intent already exists.",
        "remediation": duplicate.json()["detail"]["remediation"],
    }
    assert failed["id"] in duplicate.json()["detail"]["remediation"]

    driver.error = None

    def start_and_ack():
        driver.alive = True
        _ack_marked(rd, failed["id"])

    driver.on_spawn = start_and_ack
    retried = client.post(f"/api/runs/demo/commands/{failed['id']}/retry")
    assert retried.status_code == 200 and retried.headers["cache-control"] == "no-store"
    done = _terminal(client, retried.json())
    assert done["status"] == "succeeded" and done["id"] == failed["id"]
    assert done.get("retry_count") == 1
    assert len(driver.calls) == 2 and _types(rd).count("budget_extend") == 1


def test_semantically_equivalent_additive_payload_cannot_bypass_unresolved_guard(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=0.08, observation=0.25)
    first = _terminal(client, _post(
        client, "budget_extend", {"add_nodes": "1"}, key="semantic-budget-a").json())
    assert first["status"] == "timed_out"

    duplicate = _post(client, "budget_extend", {"add_nodes": 1}, key="semantic-budget-b")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "retry_existing_command"
    assert duplicate.json()["detail"]["existing_command_id"] == first["id"]
    assert _types(rd).count("budget_extend") == 1


@pytest.mark.parametrize(("event_type", "data", "different_type", "different_data"), [
    ("budget_extend", {"add_nodes": 2}, "fork", {"from_node_id": 0}),
    ("fork", {"from_node_id": 0}, "inject_node", {
        "idea": {"operator": "manual", "params": {}, "rationale": "legacy guard"},
        "parent_id": 0,
    }),
    ("inject_node", {
        "idea": {"operator": "manual", "params": {}, "rationale": "durable guard"},
        "parent_id": 0,
    }, "budget_extend", {"add_nodes": 3}),
])
@pytest.mark.parametrize("legacy_kind", ["identical", "different", "resume"])
def test_legacy_mutation_cannot_overtake_retryable_terminal_additive_intent(
        tmp_path, legacy_kind, event_type, data, different_type, different_data):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    # Keep enough headroom above the 80ms startup floor for a loaded Windows CI worker to append and
    # enter observation before the absolute ceiling. The behavior under test is the retryable
    # terminal guard, not scheduler precision at a 10ms margin.
    client, _srv = _client(tmp_path, driver, timeout=0.08, observation=0.30)
    command = _terminal(client, _post(
        client, event_type, data, key="legacy-terminal-guard").json())
    assert command["status"] == "timed_out", command
    assert command["error"]["retryable"] is True
    before = _types(rd)
    assert before.count(event_type) == 1

    if legacy_kind == "identical":
        response = client.post(
            "/api/runs/demo/control",
            json={"type": event_type, "data": data})
    elif legacy_kind == "different":
        response = client.post(
            "/api/runs/demo/control",
            json={"type": different_type, "data": different_data})
    else:
        response = client.post("/api/runs/demo/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "command_retry_required"
    assert response.json()["detail"]["existing_command_id"] == command["id"]
    assert response.json()["detail"]["current_status"] == "timed_out"
    assert command["id"] in response.json()["detail"]["remediation"]
    assert _types(rd) == before


def test_legacy_mutations_cannot_overtake_failed_retryable_additive_intent(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver(error=OSError("spawn failed")))
    command = _terminal(client, _post(
        client, "budget_extend", {"add_nodes": 2}, key="legacy-failed-guard").json())
    assert command["status"] == "failed"
    assert command["error"]["code"] == "spawn_failed"
    assert command["error"]["retryable"] is True
    before = _types(rd)
    assert before.count("budget_extend") == 1

    responses = [
        client.post("/api/runs/demo/control", json={
            "type": "budget_extend", "data": {"add_nodes": 2}}),
        client.post("/api/runs/demo/control", json={
            "type": "inject_node", "data": {
                "idea": {"operator": "manual", "params": {}, "rationale": "do not bypass"},
                "parent_id": 0,
            }}),
    ]
    for response in responses:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "command_retry_required"
        assert response.json()["detail"]["existing_command_id"] == command["id"]
        assert response.json()["detail"]["current_status"] == "failed"
    assert _types(rd) == before


def test_legacy_guard_reconciles_late_ack_and_ignores_safe_nonretryable_failure(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=0.04, observation=0.09)
    command = _terminal(client, _post(
        client, "budget_extend", {"add_nodes": 2}, key="legacy-late-ack").json())
    assert command["status"] == "timed_out"
    intent = _wait_for_intent(rd, command["id"])
    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": command["id"], "event_seq": intent.seq})

    # The legacy check performs observation-only reconciliation before deciding.  A proven late
    # completion becomes terminal history and must not brick compatibility.
    allowed_after_ack = client.post(
        "/api/runs/demo/control", json={"type": "hint", "data": {"text": "after ack"}})
    assert allowed_after_ack.status_code == 200
    reconciled = client.get(f"/api/runs/demo/commands/{command['id']}").json()
    assert reconciled["status"] == "succeeded"
    assert reconciled["reconciled_from"] == "timed_out"

    # A failed row whose exact durable intent disappeared is converted to a nonretryable terminal
    # record by reconciliation.  It likewise cannot become a permanent legacy lock.
    from looplab.events.eventstore import write_jsonl_atomic
    events = EventStore(rd / "events.jsonl").read_all()
    # Drop the command's exact durable intent while keeping the surviving log densely sequenced. A
    # non-dense logical sequence fails closed (a mid-log seq hole is corruption, not a torn tail), and
    # the scenario under test is a *missing intent*, not a gapped log -- a real repaired/compacted log
    # renumbers its survivors, so mirror that here (the removed intent is gone from marked_intent either
    # way, so the reconciliation to command_intent_missing still fires).
    surviving = [event.model_dump(mode="json") for event in events
                 if (event.data or {}).get("_command_id") != command["id"]]
    for new_seq, row in enumerate(surviving):
        row["seq"] = new_seq
    write_jsonl_atomic(rd / "events.jsonl", surviving)
    record_path = rd / ".commands" / f"{command['id']}.json"
    row = json.loads(record_path.read_text(encoding="utf-8"))
    row["status"] = "failed"
    row["error"] = {"code": "command_worker_failed", "message": "old failure",
                    "retryable": True, "remediation": "retry"}
    record_path.write_text(json.dumps(row), encoding="utf-8")

    allowed_after_missing = client.post(
        "/api/runs/demo/control", json={"type": "annotation", "data": {
            "node_id": 0, "text": "safe terminal history"}})
    assert allowed_after_missing.status_code == 200
    safe = client.get(f"/api/runs/demo/commands/{command['id']}").json()
    assert safe["status"] == "failed"
    assert safe["error"]["code"] == "command_intent_missing"
    assert safe["error"]["retryable"] is False


def test_timed_out_command_reconciles_a_late_exact_ack_without_reappend(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, startup=0.05, timeout=0.12)
    record = _post(client, "set_strategy", {"strategy": {"policy": "asha"}}, key="late").json()
    intent = _wait_for_intent(rd, record["id"])
    timed = _terminal(client, record, timeout=1.5)
    assert timed["status"] == "timed_out" and timed["error"]["code"] == "postcondition_timeout"
    count = _types(rd).count("set_strategy")

    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": record["id"], "event_seq": intent.seq})
    reconciled = client.get(f"/api/runs/demo/commands/{record['id']}").json()
    assert reconciled["status"] == "succeeded" and reconciled["reconciled_from"] == "timed_out"
    assert _post(client, "set_strategy", {"strategy": {"policy": "asha"}}, key="late").json()[
        "status"] == "succeeded"
    assert _types(rd).count("set_strategy") == count == 1


def test_reload_finalize_reattaches_existing_record_without_event_or_spawn_duplication(tmp_path):
    rd = _seed(tmp_path, paused=True, finalizing=True)
    driver = _Driver()

    def start_only():
        driver.alive = True

    driver.on_spawn = start_only
    client, srv = _client(tmp_path, driver, timeout=0.20)
    first = _post(client, "run_abort", {"reason": "finalized"}, key="browser-before-reload").json()
    deadline = time.time() + 1
    while not driver.calls and time.time() < deadline:
        time.sleep(0.005)
    assert driver.calls and srv.phase(srv.state(rd)) == PHASE_FINALIZING

    attached = _post(client, "run_abort", {"reason": "finalized"}, key="browser-after-reload")
    assert attached.status_code == 200 and attached.json()["id"] == first["id"]
    assert len(driver.calls) == 1 and _types(rd).count("run_abort") == 1

    driver.alive = False
    EventStore(rd / "events.jsonl").append("run_finished", {"reason": "aborted"})
    assert _terminal(client, first)["status"] == "succeeded"


def test_attach_finalize_completion_between_decision_and_continuation_is_causal(tmp_path):
    rd = _seed(tmp_path, paused=True, finalizing=True)
    driver = _Driver(alive=True)
    client, srv = _client(tmp_path, driver, timeout=0.12, observation=0.5)
    queued = []
    original_start = srv.commands._start_worker
    srv.commands._start_worker = lambda *args: queued.append(args)
    record = _post(client, "run_abort", {"reason": "finalized"}, key="attach-race").json()
    assert record["status"] == "accepted" and record.get("attached") is True and len(queued) == 1

    # Completion can land after command-record creation but before its worker starts. Attachment
    # carries the exact external intent seq+digest, so the worker observes this completion directly
    # without re-running fresh admission or hiding it inside a later baseline.
    EventStore(rd / "events.jsonl").append("run_finished", {"reason": "aborted"})
    driver.alive = False
    srv.commands._start_worker = original_start
    original_start(*queued[0])
    done = _terminal(client, record)
    assert done["status"] == "succeeded"
    assert driver.calls == [] and "resume" not in _types(rd)


def test_finalize_finished_but_live_is_engine_finishing_not_false_noop(tmp_path):
    rd = _seed(tmp_path, finished=True)
    client, _srv = _client(tmp_path, _Driver(alive=True))
    record = _post(client, "run_abort", {"reason": "finalized"}).json()
    assert record["status"] == "rejected"
    assert record["error"]["code"] == "engine_finishing" and record["error"]["retryable"] is True
    assert _types(rd).count("run_abort") == 0


def test_preappend_active_command_blocks_stale_contradictory_preflight(tmp_path):
    rd = _seed(tmp_path, paused=True)
    client, srv = _client(tmp_path, _Driver())
    # Deterministically hold the worker in the accepted→intent window that used to let the second
    # submit decide against stale folded state.
    srv.commands._start_worker = lambda *_args, **_kwargs: None

    resume = _post(client, "resume", key="resume-first")
    assert resume.status_code == 200 and resume.json()["status"] == "accepted"
    pause = _post(client, "pause", key="pause-second")
    assert pause.status_code == 409
    assert pause.json()["detail"]["code"] == "command_in_progress"
    assert pause.json()["detail"]["existing_command_id"] == resume.json()["id"]
    legacy_pause = client.post("/api/runs/demo/control", json={"type": "pause", "data": {}})
    assert legacy_pause.status_code == 409
    assert legacy_pause.json()["detail"]["existing_command_id"] == resume.json()["id"]
    legacy_resume = client.post("/api/runs/demo/resume")
    assert legacy_resume.status_code == 409
    assert legacy_resume.json()["detail"]["existing_command_id"] == resume.json()["id"]
    assert "resume" not in _types(rd) and _types(rd).count("pause") == 1  # seed pause only

    # The same rule protects finalize→resume, while a finalize reload attaches even before fold.
    # Terminalize the held fixture record without running it, then create a fresh run for clarity.
    record_path = rd / ".commands" / f"{resume.json()['id']}.json"
    row = json.loads(record_path.read_text(encoding="utf-8"))
    row["status"] = "rejected"
    record_path.write_text(json.dumps(row), encoding="utf-8")
    finalize = _post(client, "run_abort", {"reason": "finalized"}, key="finalize-first")
    assert finalize.status_code == 200 and finalize.json()["status"] == "accepted"
    reload_finalize = _post(client, "run_abort", {"reason": "finalized"}, key="finalize-reload")
    assert reload_finalize.status_code == 200 and reload_finalize.json()["id"] == finalize.json()["id"]
    semantic_reload = _post(
        client, "run_abort", {"reason": "  finalized  "}, key="finalize-semantic-reload")
    assert semantic_reload.status_code == 200
    assert semantic_reload.json()["id"] == finalize.json()["id"]
    mismatched = _post(client, "run_abort", {"reason": "beta"}, key="finalize-beta")
    assert mismatched.status_code == 409
    assert mismatched.json()["detail"]["code"] == "finalize_payload_conflict"
    assert mismatched.json()["detail"]["existing_command_id"] == finalize.json()["id"]
    blocked_resume = _post(client, "resume", key="resume-after-finalize")
    assert blocked_resume.status_code == 409
    assert blocked_resume.json()["detail"]["existing_command_id"] == finalize.json()["id"]
    assert _types(rd).count("run_abort") == 0


def test_card_drop_bypasses_active_driver_gate_and_cancels_live_work(tmp_path):
    rd = _seed(tmp_path, paused=True)
    EventStore(rd / "events.jsonl").append("card_added", {
        "id": "card-live", "statement": "candidate under evaluation", "source": "engine",
        "idea": {"operator": "draft", "params": {}, "space": {}},
    })
    client, srv = _client(tmp_path, _Driver())
    # Hold a driver command before its intent append. Collaboration must not share this global gate:
    # the evaluator watches card_dropped specifically to terminate already-running paid work.
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    active = _post(client, "resume", key="held-driver")
    assert active.status_code == 200 and active.json()["status"] == "accepted"

    dropped = _post(
        client, "card_dropped", {"id": "card-live", "reason": "operator cancelled"},
        key="drop-during-driver",
    )
    assert dropped.status_code == 200
    assert dropped.json()["status"] == "succeeded"
    events = EventStore(rd / "events.jsonl").read_all()
    drop = [event for event in events if event.type == "card_dropped"][-1]
    assert drop.data == {
        "id": "card-live", "reason": "operator cancelled", "dropped_by": "operator",
        "_command_id": dropped.json()["id"],
    }
    assert fold(events).cards["card-live"].status == "dropped"


def test_explicit_retry_is_blocked_while_a_different_command_is_active(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(error=OSError("start failed"))
    client, srv = _client(tmp_path, driver)
    failed = _terminal(
        client, _post(client, "budget_extend", {"add_nodes": 1}, key="failed-a").json())
    assert failed["status"] == "failed"

    srv.commands._start_worker = lambda *_args, **_kwargs: None
    active = _post(client, "set_strategy", {"strategy": {"policy": "asha"}}, key="active-b").json()
    assert active["status"] == "accepted"
    retry = client.post(f"/api/runs/demo/commands/{failed['id']}/retry")
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "command_in_progress"
    assert retry.json()["detail"]["existing_command_id"] == active["id"]
    stored = json.loads((rd / ".commands" / f"{failed['id']}.json").read_text(encoding="utf-8"))
    assert stored["status"] == "failed" and stored.get("retry_count", 0) == 0


def test_finalize_error_is_not_success_reload_reattaches_and_retry_preserves_stop(tmp_path):
    rd = _seed(tmp_path, paused=True)
    driver = _Driver()
    attempts = 0

    def finish_attempt():
        nonlocal attempts
        attempts += 1
        driver.alive = False
        EventStore(rd / "events.jsonl").append(
            "run_finished", {"reason": "error" if attempts == 1 else "aborted",
                             "error": "wrap-up failed" if attempts == 1 else None})

    driver.on_spawn = finish_attempt
    client, srv = _client(tmp_path, driver, startup=0.05, timeout=0.25)
    failed = _terminal(
        client, _post(client, "run_abort", {"reason": "finalized"}, key="final-error").json())
    assert failed["status"] == "failed" and failed["error"]["code"] == "engine_failed"
    state = srv.state(rd)
    assert state.finished and state.stop_requested and state.stop_reason == "error"
    assert srv.phase(state) == PHASE_FINALIZING

    reload_record = _post(client, "run_abort", {"reason": "finalized"}, key="lost-key").json()
    assert reload_record["id"] == failed["id"] and reload_record["status"] == "failed"
    assert _types(rd).count("run_abort") == 1 and attempts == 1

    retried = client.post(f"/api/runs/demo/commands/{failed['id']}/retry").json()
    assert retried["id"] == failed["id"]
    done = _terminal(client, retried, timeout=2.5)
    assert done["status"] == "succeeded" and attempts == 2
    assert _types(rd).count("run_abort") == 1
    assert [event.data.get("reason") for event in EventStore(rd / "events.jsonl").read_all()
            if event.type == "run_finished"][-2:] == ["error", "aborted"]


def test_cross_service_os_sequencer_excludes_the_same_run(tmp_path):
    rd = _seed(tmp_path)
    app = make_app(tmp_path)
    srv = app.state.looplab
    first = RunCommandService(srv)
    second = RunCommandService(srv)
    attempting = threading.Event()
    entered = threading.Event()

    def contender():
        attempting.set()
        with second.sequence(rd):
            entered.set()

    with first.sequence(rd):
        thread = threading.Thread(target=contender)
        thread.start()
        assert attempting.wait(timeout=1)
        time.sleep(0.08)
        assert not entered.is_set()
    thread.join(timeout=2)
    assert entered.is_set() and not thread.is_alive()

    # Contention past the configured bound must fail closed — never fall through into `yield` as the
    # old broad OSError handler did after msvcrt's internal retry window.
    holder = RunCommandService(srv, lock_acquire_timeout=0.20, poll_interval=0.01)
    waiter = RunCommandService(srv, lock_acquire_timeout=0.08, poll_interval=0.01)
    attempting = threading.Event()
    entered = threading.Event()
    errors = []

    def timed_contender():
        attempting.set()
        try:
            with waiter.sequence(rd):
                entered.set()
        except Exception as exc:  # asserted below
            errors.append(exc)

    with holder.sequence(rd):
        thread = threading.Thread(target=timed_contender)
        thread.start()
        assert attempting.wait(timeout=1)
        thread.join(timeout=1)
        assert not entered.is_set()
        assert errors and getattr(errors[0], "status_code", None) == 503

    # Production uses one service instance. Its in-process RLock must honor the same acquisition
    # ceiling instead of blocking forever before the bounded OS-lock loop is even reached.
    same = RunCommandService(srv, lock_acquire_timeout=0.08, poll_interval=0.01)
    attempting = threading.Event()
    errors = []

    def same_service_contender():
        attempting.set()
        try:
            with same.sequence(rd):
                pass
        except Exception as exc:  # asserted below
            errors.append(exc)

    with same.sequence(rd):
        thread = threading.Thread(target=same_service_contender)
        thread.start()
        assert attempting.wait(timeout=1)
        thread.join(timeout=1)
        assert errors and getattr(errors[0], "status_code", None) == 503


def test_spawn_claim_alive_transition_blocks_same_spawn_decision(tmp_path):
    rd = _seed(tmp_path)
    app = make_app(tmp_path)
    srv = app.state.looplab
    probes = iter((False, True))
    service = RunCommandService(srv, engine_alive=lambda _rd: next(probes))
    service._record_spawn_claim(rd, "cmd_" + "c" * 32, 4242)

    # Reproduce the caller pattern: the outer liveness probe sees dead, but the leased child takes
    # engine.lock before the inner lease probe. This invocation must still refuse Popen.
    may_spawn = False
    if not service.engine_alive(rd):
        may_spawn = not service._recent_spawn_claim(rd)
    assert may_spawn is False
    assert not service._spawn_claim_path(rd).exists()  # retired only for the NEXT decision


def test_windows_case_aliases_share_command_lock_and_spawn_claim(tmp_path):
    if os.path.normcase("CaseAlias") != os.path.normcase("casealias"):
        pytest.skip("filesystem identity is case-sensitive on this platform")
    app = make_app(tmp_path)
    service = app.state.looplab.commands
    upper = tmp_path / "CaseAlias"
    lower = tmp_path / "casealias"
    assert service._sequence_path(upper) == service._sequence_path(lower)
    assert service._spawn_claim_path(upper) == service._spawn_claim_path(lower)
    assert service._start_record_path(upper) == service._start_record_path(lower)


def test_start_record_sidecar_atomic_roundtrip_and_exact_retirement(tmp_path):
    app = make_app(tmp_path)
    service = app.state.looplab.commands
    rd = tmp_path / "not-materialized-yet"
    first = {
        "id": "start_0123456789abcdef0123456789abcdef",
        "status": "accepted",
        "created_at": 1.0,
    }

    assert service.load_start_record(rd) is None
    service.save_start_record(rd, first)
    assert service.load_start_record(rd) == first
    path = service._start_record_path(rd)
    assert path.parent == tmp_path / ".command-locks"
    assert path.name.endswith(".start.json")

    updated = {**first, "status": "executing", "updated_at": 2.0}
    service.save_start_record(rd, updated)
    assert service.load_start_record(rd) == updated
    assert service.retire_start_record(rd, "start_different") is False
    assert service.load_start_record(rd) == updated
    assert service.retire_start_record(rd, first["id"]) is True
    assert service.load_start_record(rd) is None
    assert service.retire_start_record(rd, first["id"]) is False


def test_start_record_malformed_and_symlink_evidence_fail_closed(tmp_path):
    app = make_app(tmp_path)
    service = app.state.looplab.commands
    rd = tmp_path / "future-run"
    path = service._start_record_path(rd)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(HTTPException) as malformed:
        service.load_start_record(rd)
    assert malformed.value.status_code == 503
    assert malformed.value.detail["code"] == "start_record_unavailable"
    with pytest.raises(HTTPException):
        service.retire_start_record(rd, "start_any")
    assert path.exists()

    path.unlink()
    target = tmp_path / "attacker-owned-start.json"
    target.write_text('{"id":"start_attacker"}', encoding="utf-8")
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(HTTPException) as linked:
        service.load_start_record(rd)
    assert linked.value.status_code == 409
    with pytest.raises(HTTPException):
        service.save_start_record(rd, {"id": "start_replacement"})
    with pytest.raises(HTTPException):
        service.retire_start_record(rd, "start_attacker")
    assert json.loads(target.read_text(encoding="utf-8"))["id"] == "start_attacker"


def test_monitor_reensures_dead_preexisting_driver_and_heartbeats_long_pause(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    # CODEX AGENT: this case observes several heartbeat periods, so its absolute lease must be wider
    # than the deliberately tiny sliding timeout.  Under a loaded CI runner the old implicit 300 ms
    # ceiling could expire while the test thread was descheduled, correctly retiring the claim before
    # the assertion and turning a scheduling delay into a false lifecycle failure.
    client, srv = _client(tmp_path, driver, timeout=0.10, observation=1.5)

    command = _post(client, "set_strategy", {"strategy": {"policy": "mcts"}}, key="dies").json()
    _wait_for_intent(rd, command["id"])

    def restart_and_ack():
        driver.alive = True
        _ack_marked(rd, command["id"])

    driver.on_spawn = restart_and_ack
    driver.alive = False
    assert _terminal(client, command, timeout=1.5)["status"] == "succeeded"
    assert len(driver.calls) == 1

    # A long live pause keeps one execution owner and refreshes its claim; repeated GET cannot start
    # a duplicate worker, and destructive mutation is excluded until the postcondition lands.
    driver.calls.clear()
    driver.on_spawn = None
    driver.alive = True
    pause = _post(client, "pause", key="long-pause").json()
    _wait_for_intent(rd, pause["id"])
    claim = rd / ".commands" / f".{pause['id']}.executing"
    deadline = time.time() + 1
    while not claim.exists() and time.time() < deadline:
        time.sleep(0.005)
    assert claim.exists()
    first_mtime = claim.stat().st_mtime_ns
    for _ in range(4):
        client.get(f"/api/runs/demo/commands/{pause['id']}")
        time.sleep(0.04)
    assert claim.stat().st_mtime_ns > first_mtime
    with pytest.raises(Exception) as blocked:
        with srv.commands.destructive_guard(rd, "delete run"):
            pass
    assert getattr(blocked.value, "status_code", None) == 409
    driver.alive = False
    assert _terminal(client, pause)["status"] == "succeeded"


def test_stale_execution_claim_needs_owner_death_not_just_age(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True, process_identity="same-server-generation")
    _client_unused, srv = _client(tmp_path, driver, timeout=0.10)
    command_id = "cmd_" + "e" * 32
    claim = srv.commands._exec_path(rd, command_id)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({
        "pid": os.getpid(),
        "process_identity": "same-server-generation",
        "created_at": time.time() - 120,
    }), encoding="utf-8")
    old = time.time() - 120
    os.utime(claim, (old, old))

    # A suspended live worker may miss every heartbeat. Neither GET/retry ownership nor destructive
    # guards may silently erase its claim based only on mtime.
    assert srv.commands._claim_execution(rd, command_id) is False
    assert claim.exists()
    assert command_id in srv.commands._active_command_ids(rd)


def test_stale_execution_claim_reclaims_dead_or_recycled_owner(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True, process_identity="new-generation")
    _client_unused, srv = _client(tmp_path, driver, timeout=0.10)
    command_id = "cmd_" + "f" * 32
    claim = srv.commands._exec_path(rd, command_id)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({
        "pid": 4242,
        "process_identity": "old-generation",
        "created_at": time.time() - 120,
    }), encoding="utf-8")
    old = time.time() - 120
    os.utime(claim, (old, old))

    # A live-but-reused PID is definitive evidence that the original worker process is gone.
    assert srv.commands._claim_execution(rd, command_id) is True
    replacement = json.loads(claim.read_text(encoding="utf-8"))
    assert replacement["pid"] == os.getpid()
    assert replacement["process_identity"] == "new-generation"
    srv.commands._release_execution(rd, command_id)


def test_cross_scheme_identity_mismatch_is_inconclusive_for_live_claims(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True, process_identity="psutil:current-generation")
    _client_unused, srv = _client(tmp_path, driver, timeout=0.10)
    spawn_claim = {
        "pid": 4242,
        "process_identity": "windows-filetime:stored-generation",
    }

    # Different source encodings can represent the same live process with different bytes. They
    # must not authorize another engine spawn, worker claim, or destructive operation.
    assert srv.commands._claim_child_definitely_gone(spawn_claim) is False
    spawn_claim["process_identity"] = "legacy-untagged-generation"
    assert srv.commands._claim_child_definitely_gone(spawn_claim) is False

    claim = srv.commands._exec_path(rd, "cmd_" + "1" * 32)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({
        "pid": 4242,
        "process_identity": "windows-filetime:stored-generation",
        "created_at": time.time() - 120,
    }), encoding="utf-8")
    assert srv.commands._execution_owner_definitely_gone(claim) is False
    assert srv.commands._execution_owner_exactly_alive(claim) is False
    row = json.loads(claim.read_text(encoding="utf-8"))
    row["process_identity"] = "legacy-untagged-generation"
    claim.write_text(json.dumps(row), encoding="utf-8")
    assert srv.commands._execution_owner_definitely_gone(claim) is False


def test_same_scheme_identity_mismatch_proves_pid_reuse_for_live_claims(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True, process_identity="psutil:current-generation")
    _client_unused, srv = _client(tmp_path, driver, timeout=0.10)
    row = {"pid": 4242, "process_identity": "psutil:stored-generation"}

    assert srv.commands._claim_child_definitely_gone(row) is True
    claim = srv.commands._exec_path(rd, "cmd_" + "2" * 32)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({
        **row,
        "created_at": time.time() - 120,
    }), encoding="utf-8")
    assert srv.commands._execution_owner_definitely_gone(claim) is True
    assert srv.commands._execution_owner_exactly_alive(claim) is False


def test_execution_claim_publishes_complete_owner_before_exclusive_link(tmp_path, monkeypatch):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    real_link = os.link
    observed = []

    def inspecting_link(source, target, *args, **kwargs):
        row = json.loads(Path(source).read_text(encoding="utf-8"))
        assert row["pid"] == os.getpid()
        assert row["process_identity"] == "child-generation"
        assert not os.path.exists(target)
        observed.append(row)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", inspecting_link)
    command_id = "cmd_" + "a" * 32
    assert srv.commands._claim_execution(rd, command_id) is True
    claim = srv.commands._exec_path(rd, command_id)
    assert json.loads(claim.read_text(encoding="utf-8")) == observed[0]
    assert not list(claim.parent.glob(f".{claim.name}.*.tmp"))
    srv.commands._release_execution(rd, command_id)


def test_command_record_save_retries_transient_windows_replace_denial(tmp_path, monkeypatch):
    import looplab.serve.run_commands as command_module

    target = tmp_path / "record.json"
    real_write = command_module.atomic_write_text
    attempts = []

    def sharing_violation_then_write(path, payload):
        attempts.append(path)
        if len(attempts) < 3:
            exc = PermissionError("destination is briefly open")
            exc.winerror = 5
            raise exc
        return real_write(path, payload)

    monkeypatch.setattr(command_module, "atomic_write_text", sharing_violation_then_write)
    RunCommandService._save(target, {"status": "executing"})
    assert len(attempts) == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "executing"}


def test_unknown_active_claims_have_guarded_recovery_but_live_owner_cannot_be_forced(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True, process_identity="same-server-generation")
    client, srv = _client(tmp_path, driver)
    phrase = "I verified no LoopLab command or run activity is active"
    directory = rd / ".commands"
    directory.mkdir(parents=True, exist_ok=True)
    execution = directory / (".cmd_" + "b" * 32 + ".executing")
    activity = directory / ".activity_crash.json"
    execution.write_text("", encoding="utf-8")
    activity.write_text("{partial", encoding="utf-8")
    old = time.time() - 30
    os.utime(execution, (old, old))
    os.utime(activity, (old, old))

    needs_confirmation = client.post(
        "/api/runs/demo/resolve-activity-claims", json={"confirmation": ""})
    assert needs_confirmation.status_code == 409
    assert needs_confirmation.json()["detail"]["code"] == "active_claim_confirmation_required"
    resolved = client.post(
        "/api/runs/demo/resolve-activity-claims", json={"confirmation": phrase})
    assert resolved.status_code == 200
    assert resolved.json()["count"] == 2
    assert not execution.exists() and not activity.exists()

    live = directory / (".cmd_" + "c" * 32 + ".executing")
    live.write_text(json.dumps({
        "pid": os.getpid(),
        "process_identity": "same-server-generation",
        "created_at": old,
    }), encoding="utf-8")
    refused = client.post(
        "/api/runs/demo/resolve-activity-claims", json={"confirmation": phrase})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "active_claim_owner_alive"
    assert live.exists()


@pytest.mark.skipif(os.name != "nt", reason="native process FILETIME fallback is Windows-only")
def test_windows_process_identity_without_psutil(monkeypatch):
    real_import = builtins.__import__

    def import_without_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("test optional dependency absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_psutil)
    identity = _process_identity(os.getpid())
    assert isinstance(identity, str)
    scheme, digest = identity.split(":", 1)
    assert scheme == "windows-filetime" and len(digest) == 64
    int(digest, 16)


def test_slow_spawn_keeps_lease_past_startup_window_and_late_ack_succeeds(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver()
    client, srv = _client(
        tmp_path, driver, startup=0.05, timeout=0.10, observation=0.50)
    command = _post(client, "budget_extend", {"add_nodes": 1}, key="slow-child").json()
    intent = _wait_for_intent(rd, command["id"])
    time.sleep(0.14)  # well past startup_timeout; detached child has not exposed engine.lock yet
    current = client.get(f"/api/runs/demo/commands/{command['id']}").json()
    assert current["status"] == "executing" and current.get("startup_slow") is True
    assert len(driver.calls) == 1 and srv.commands.spawn_inflight(rd)
    blocked = client.delete("/api/runs/demo")
    assert blocked.status_code == 409
    assert any(reason in blocked.json()["detail"] for reason in ("active command", "engine start"))
    retry = client.post(f"/api/runs/demo/commands/{command['id']}/retry")
    assert retry.status_code == 409 and len(driver.calls) == 1

    driver.alive = True
    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": command["id"], "event_seq": intent.seq})
    assert _terminal(client, current)["status"] == "succeeded"
    assert len(driver.calls) == 1


def test_expired_external_start_lease_quarantines_a_still_running_pid(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True)
    _client_unused, srv = _client(tmp_path, driver, observation=0.30)
    srv.commands.record_external_spawn(rd, "start", 4242)
    claim_path = srv.commands._spawn_claim_path(rd)
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["expires_at"] = time.time() - 1
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    assert srv.commands.spawn_inflight(rd) is True
    quarantined = json.loads(claim_path.read_text(encoding="utf-8"))
    assert quarantined["quarantined"] is True and quarantined["expires_at"] is None

    driver.pid_running = False
    assert srv.commands.spawn_inflight(rd) is False
    assert not claim_path.exists()


def test_spawn_quarantine_recognizes_pid_reuse_by_creation_identity(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True, process_identity="original-child")
    _client_unused, srv = _client(tmp_path, driver, observation=0.30)
    srv.commands.record_external_spawn(rd, "start", 4242)
    claim_path = srv.commands._spawn_claim_path(rd)
    row = json.loads(claim_path.read_text(encoding="utf-8"))
    row["expires_at"] = time.time() - 1
    claim_path.write_text(json.dumps(row), encoding="utf-8")

    driver.process_identity = "recycled-unrelated-process"
    assert srv.commands.spawn_inflight(rd) is False
    assert not claim_path.exists()


def test_malformed_spawn_claim_is_fail_closed_and_cannot_be_cleared(tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    claim_path = srv.commands._spawn_claim_path(rd)
    claim_path.write_text("{not-json", encoding="utf-8")

    assert srv.commands.spawn_inflight(rd) is True
    srv.commands.cancel_external_spawn(rd, "start")
    assert claim_path.exists()


def test_unknown_spawn_claim_has_explicit_recovery_but_exact_live_child_cannot_be_forced(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=True)
    client, srv = _client(tmp_path, driver)
    phrase = "I verified no LoopLab engine process is running"

    srv.commands.begin_external_spawn(rd, "start")  # crash-window owner: PID was never persisted
    claim_path = srv.commands._spawn_claim_path(rd)
    row = json.loads(claim_path.read_text(encoding="utf-8"))
    row["created_at"] = time.time() - 30
    row["quarantined_at"] = time.time() - 30
    row["expires_at"] = None
    row["quarantined"] = True
    claim_path.write_text(json.dumps(row), encoding="utf-8")

    needs_confirmation = client.post(
        "/api/start/demo/resolve-claim", json={"confirmation": ""})
    assert needs_confirmation.status_code == 409
    assert needs_confirmation.json()["detail"]["code"] == "spawn_claim_confirmation_required"
    resolved = client.post(
        "/api/start/demo/resolve-claim", json={"confirmation": phrase})
    assert resolved.status_code == 200 and resolved.json()["resolved"] is True
    assert not claim_path.exists()

    # Creation identity matches: even the explicit phrase cannot clear a definitely-live child.
    srv.commands.record_external_spawn(rd, "start", 4242)
    live = client.post("/api/start/demo/resolve-claim", json={"confirmation": phrase})
    assert live.status_code == 409
    assert live.json()["detail"]["code"] == "engine_start_uncertain"
    assert claim_path.exists()


def test_live_but_unacknowledging_driver_has_bounded_observation_ceiling(tmp_path):
    _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(
        tmp_path, driver, startup=0.05, timeout=0.08, observation=0.28)
    command = _post(client, "set_strategy", {"strategy": {"policy": "asha"}}, key="wedged").json()
    timed = _terminal(client, command, timeout=1.0)
    assert timed["status"] == "timed_out" and timed["error"]["code"] == "postcondition_timeout"
    assert driver.calls == []


def test_delete_is_excluded_by_active_command_then_permitted_after_terminal(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(alive=True)
    client, _srv = _client(tmp_path, driver, timeout=0.10)
    pause = _post(client, "pause", key="delete-guard").json()
    _wait_for_intent(rd, pause["id"])

    blocked = client.delete("/api/runs/demo")
    assert blocked.status_code == 409 and pause["id"] in blocked.json()["detail"]
    assert rd.exists()
    driver.alive = False
    assert _terminal(client, pause)["status"] == "succeeded"
    deleted = client.delete("/api/runs/demo")
    assert deleted.status_code == 200 and not rd.exists()
    time.sleep(0.05)
    assert driver.calls == []


def test_reset_active_guard_and_external_spawn_lease_prevent_second_popen(monkeypatch, tmp_path):
    rd = _seed(tmp_path, finished=True)
    driver = _Driver()
    client, srv = _client(tmp_path, driver, startup=0.05, timeout=0.20)
    original_start = srv.commands._start_worker
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    pending = _post(client, "budget_extend", {"add_nodes": 1}, key="before-reset").json()
    assert pending["status"] == "accepted"
    blocked = client.post("/api/runs/demo/reset")
    assert blocked.status_code == 409 and pending["id"] in blocked.json()["detail"]

    pending_path = rd / ".commands" / f"{pending['id']}.json"
    row = json.loads(pending_path.read_text(encoding="utf-8"))
    row["status"] = "rejected"
    pending_path.write_text(json.dumps(row), encoding="utf-8")
    srv.commands._start_worker = original_start

    from looplab.serve.routers import control as control_router

    reset_spawns = []

    def reset_spawn(args, **kwargs):
        reset_spawns.append((args, kwargs))
        EventStore(rd / "events.jsonl").append(
            "run_started", {"run_id": "demo", "task_id": "task", "goal": "g", "direction": "min"})
        return 9001

    monkeypatch.setattr(control_router, "_spawn_engine", reset_spawn)
    reset = client.post("/api/runs/demo/reset")
    assert reset.status_code == 200 and len(reset_spawns) == 1
    assert srv.commands.spawn_inflight(rd)
    delete_during_start = client.delete("/api/runs/demo")
    assert delete_during_start.status_code == 409
    assert "engine start is still in progress" in delete_during_start.json()["detail"]

    before_lock = _post(client, "budget_extend", {"add_nodes": 2}, key="after-reset")
    assert before_lock.status_code == 409
    assert before_lock.json()["detail"]["code"] == "engine_start_uncertain"
    assert driver.calls == []

    # Once the reset child exposes engine.lock, the lease is safely retired and the exact same
    # not-yet-reserved request is blocked for the transition-observing decision, then admitted on
    # the next observation without a second Popen.
    driver.alive = True
    transition = _post(client, "budget_extend", {"add_nodes": 2}, key="after-reset")
    assert transition.status_code == 409
    command = _post(client, "budget_extend", {"add_nodes": 2}, key="after-reset").json()
    intent = _wait_for_intent(rd, command["id"])
    assert driver.calls == []  # the now-live reset engine owns the command; no second Popen
    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": command["id"], "event_seq": intent.seq})
    assert _terminal(client, command)["status"] == "succeeded"


def test_reset_rejects_delayed_first_post_but_same_key_replays_old_terminal_record(
        monkeypatch, tmp_path):
    rd = _seed(tmp_path, finished=True)
    driver = _Driver()
    client, srv = _client(tmp_path, driver)
    generation_a = _generation(client)
    body = {"type": "hint", "data": {"text": "belongs to A"},
            "expected_generation": generation_a}
    first = client.post(
        "/api/runs/demo/commands", headers={"Idempotency-Key": "accepted-in-a"}, json=body)
    assert first.status_code == 200 and first.json()["status"] == "succeeded"
    original_record = first.json()

    from looplab.serve.routers import control as control_router

    def reset_spawn(_args, **_kwargs):
        EventStore(rd / "events.jsonl").append(
            "run_started", {"run_id": "demo", "task_id": "task-b", "goal": "b",
                            "direction": "min"})
        return 9002

    monkeypatch.setattr(control_router, "_spawn_engine", reset_spawn)
    reset = client.post("/api/runs/demo/reset")
    assert reset.status_code == 200
    generation_b = _generation(client)
    assert generation_b != generation_a

    delayed = client.post(
        "/api/runs/demo/commands", headers={"Idempotency-Key": "first-arrives-after-reset"},
        json={"type": "hint", "data": {"text": "must not cross"},
              "expected_generation": generation_a})
    assert delayed.status_code == 409
    assert delayed.json()["detail"] == {
        "code": "run_generation_changed",
        "expected_generation": generation_a,
        "current_generation": generation_b,
        "message": "The run was reset or replaced after this command was formed.",
        "remediation": (
            "Refresh the run, review its current state, and form a new command with a new "
            "idempotency key and current generation."),
    }

    # Lost-response recovery uses the exact old, valid token. Idempotency lookup wins over equality
    # with B, so the terminal A record remains unchanged and no B event/worker is created.
    replay = client.post(
        "/api/runs/demo/commands", headers={"Idempotency-Key": "accepted-in-a"}, json=body)
    assert replay.status_code == 200 and replay.json() == original_record
    assert _types(rd) == ["run_started"]
    assert len(list((rd / ".commands").glob("cmd_*.json"))) == 1
    assert driver.calls == []

    # Syntax remains mandatory even for an idempotent key; only a valid stale token gets recovery.
    for invalid in (None, "not-a-generation"):
        invalid_replay = client.post(
            "/api/runs/demo/commands", headers={"Idempotency-Key": "accepted-in-a"},
            json={"type": "hint", "data": {"text": "belongs to A"},
                  "expected_generation": invalid})
        assert invalid_replay.status_code == 400
        assert invalid_replay.json()["detail"]["code"] == "invalid_run_generation"

    retry = client.post(f"/api/runs/demo/commands/{original_record['id']}/retry")
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "run_generation_changed"


def test_get_quarantines_stale_or_unbound_nonterminal_records_without_starting_worker(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver()
    client, srv = _client(tmp_path, driver)
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    pending = _post(
        client, "budget_extend", {"add_nodes": 1}, key="pending-generation-a").json()
    assert pending["status"] == "accepted"

    (rd / "events.jsonl").rename(rd / "events.jsonl.generation-a")
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "task-b", "goal": "b",
                        "direction": "min"})
    stale = client.get(f"/api/runs/demo/commands/{pending['id']}").json()
    assert stale["status"] == "failed"
    assert stale["error"]["code"] == "run_generation_changed"
    assert stale["error"]["retryable"] is False
    assert driver.calls == [] and _types(rd) == ["run_started"]

    # Pre-upgrade accepted records have no generation proof. They are observable but equally inert.
    path = rd / ".commands" / f"{pending['id']}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row.pop("run_generation", None)
    row["status"] = "accepted"
    row["error"] = None
    path.write_text(json.dumps(row), encoding="utf-8")
    legacy = client.get(f"/api/runs/demo/commands/{pending['id']}").json()
    assert legacy["status"] == "failed"
    assert legacy["error"]["code"] == "run_generation_unavailable"
    assert driver.calls == [] and _types(rd) == ["run_started"]


def test_reset_archive_failure_rolls_back_and_never_spawns(monkeypatch, tmp_path):
    rd = _seed(tmp_path, finished=True)
    (rd / "spans.jsonl").write_text('{"name":"keep"}\n', encoding="utf-8")
    before_events = (rd / "events.jsonl").read_bytes()
    before_spans = (rd / "spans.jsonl").read_bytes()
    client, _srv = _client(tmp_path, _Driver())
    from looplab.serve.routers import control as control_router

    spawns = []
    monkeypatch.setattr(control_router, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))
    original_rename = type(rd).rename

    def fail_spans_rename(self, target):
        if self.name == "spans.jsonl":
            raise OSError("simulated archive failure")
        return original_rename(self, target)

    monkeypatch.setattr(type(rd), "rename", fail_spans_rename)
    response = client.post("/api/runs/demo/reset")
    assert response.status_code == 500 and "no engine was started" in response.json()["detail"]
    assert spawns == []
    assert (rd / "events.jsonl").read_bytes() == before_events
    assert (rd / "spans.jsonl").read_bytes() == before_spans
    assert not list(rd.glob("events.jsonl.reset-*"))


def test_reset_spawn_lease_failure_restores_archived_run(monkeypatch, tmp_path):
    rd = _seed(tmp_path, finished=True)
    (rd / "spans.jsonl").write_text('{"name":"keep"}\n', encoding="utf-8")
    before_events = (rd / "events.jsonl").read_bytes()
    before_spans = (rd / "spans.jsonl").read_bytes()
    client, srv = _client(tmp_path, _Driver())
    from looplab.serve.routers import control as control_router

    spawns = []
    monkeypatch.setattr(control_router, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))
    monkeypatch.setattr(
        srv.commands,
        "begin_external_spawn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("lease write failed")),
    )

    with pytest.raises(OSError, match="lease write failed"):
        client.post("/api/runs/demo/reset")
    assert spawns == []
    assert (rd / "events.jsonl").read_bytes() == before_events
    assert (rd / "spans.jsonl").read_bytes() == before_spans
    assert not list(rd.glob("*.reset-*"))


def test_external_spawn_record_failure_keeps_lease_and_reset_archives(monkeypatch, tmp_path):
    resume_rd = _seed(tmp_path, "resume-me", paused=True)
    reset_rd = _seed(tmp_path, "reset-me", finished=True)
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    from looplab.serve.routers import control as control_router

    spawns = []

    def fake_spawn(args, **kwargs):
        spawns.append((args, kwargs))
        return 7788

    monkeypatch.setattr(control_router, "_spawn_engine", fake_spawn)
    monkeypatch.setattr(
        srv.commands, "record_external_spawn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("claim update failed")))

    # Popen returned, so a persistence error must leave the preclaim in place. A second legacy
    # resume observes already_starting and cannot call Popen again.
    with pytest.raises(OSError, match="claim update failed"):
        client.post("/api/runs/resume-me/resume")
    assert srv.commands._spawn_claim_path(resume_rd).exists()
    resumed_again = client.post("/api/runs/resume-me/resume")
    assert resumed_again.status_code == 409
    assert resumed_again.json()["detail"]["code"] == "engine_start_uncertain"
    assert len(spawns) == 1

    # Reset must not roll archived files back underneath a child that may already be using the new
    # run directory; its preclaim likewise remains the duplicate-spawn quarantine.
    with pytest.raises(OSError, match="claim update failed"):
        client.post("/api/runs/reset-me/reset")
    assert srv.commands._spawn_claim_path(reset_rd).exists()
    assert not (reset_rd / "events.jsonl").exists()
    assert list(reset_rd.glob("events.jsonl.reset-*"))
    assert len(spawns) == 2


def test_spawn_stderr_close_failure_cannot_turn_successful_popen_into_failure(monkeypatch, tmp_path):
    from looplab.serve import engine_proc

    class _BadClose:
        def close(self):
            raise OSError("FUSE close failed")

    class _Proc:
        pid = 8877

    monkeypatch.setattr(engine_proc, "open", lambda *_args, **_kwargs: _BadClose(), raising=False)
    monkeypatch.setattr(engine_proc.subprocess, "Popen", lambda *_args, **_kwargs: _Proc())
    rd = tmp_path / "spawn-close"
    assert engine_proc._spawn_engine(["resume", str(rd)], run_dir=rd) == 8877


def test_clear_trace_is_excluded_by_active_command_and_does_not_rewrite(tmp_path):
    rd = _seed(tmp_path)
    spans = rd / "spans.jsonl"
    spans.write_text(
        '{"attributes":{"node_id":0},"name":"target"}\n'
        '{"attributes":{"node_id":1},"name":"keep"}\n', encoding="utf-8")
    before = spans.read_bytes()
    client, srv = _client(tmp_path, _Driver())
    srv.commands._start_worker = lambda *_args, **_kwargs: None
    active = _post(client, "budget_extend", {"add_nodes": 1}, key="trace-active").json()
    assert active["status"] == "accepted"

    blocked = client.post("/api/runs/demo/nodes/0/clear_trace")
    assert blocked.status_code == 409 and active["id"] in blocked.json()["detail"]
    assert spans.read_bytes() == before
    path = rd / ".commands" / f"{active['id']}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["status"] = "rejected"
    path.write_text(json.dumps(row), encoding="utf-8")
    cleared = client.post("/api/runs/demo/nodes/0/clear_trace")
    assert cleared.status_code == 200 and cleared.json()["removed"] == 1
    assert b'"node_id":0' not in spans.read_bytes() and b'"node_id":1' in spans.read_bytes()


def test_retry_fails_closed_if_recorded_marked_intent_was_removed(tmp_path):
    from looplab.events.eventstore import write_jsonl_atomic

    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    done = _post(client, "node_abort", {"node_id": 0}, key="removed-intent").json()
    assert done["status"] == "succeeded" and _types(rd).count("node_abort") == 1
    events = EventStore(rd / "events.jsonl").read_all()
    write_jsonl_atomic(rd / "events.jsonl", [
        event.model_dump(mode="json") for event in events if event.type != "node_abort"])
    path = rd / ".commands" / f"{done['id']}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["status"] = "timed_out"
    row["error"] = {"code": "postcondition_timeout", "message": "missing", "retryable": True,
                    "remediation": "retry"}
    path.write_text(json.dumps(row), encoding="utf-8")

    retried = client.post(f"/api/runs/demo/commands/{done['id']}/retry")
    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "command_not_retryable"
    failed = client.get(f"/api/runs/demo/commands/{done['id']}").json()
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "command_intent_missing"
    assert failed["error"]["retryable"] is False
    assert _types(rd).count("node_abort") == 0


def test_get_fails_closed_if_marker_seq_survives_but_intent_type_changed(tmp_path):
    from looplab.events.eventstore import write_jsonl_atomic

    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    done = _post(client, "node_abort", {"node_id": 0}, key="rewritten-intent").json()
    assert done["status"] == "succeeded"
    events = EventStore(rd / "events.jsonl").read_all()
    rewritten = []
    for event in events:
        row = event.model_dump(mode="json")
        if event.type == "node_abort":
            row["type"] = "hint"
            row["data"] = {"text": "different action", "_command_id": done["id"]}
        rewritten.append(row)
    write_jsonl_atomic(rd / "events.jsonl", rewritten)

    observed = client.get(f"/api/runs/demo/commands/{done['id']}").json()
    assert observed["status"] == "failed"
    assert observed["error"]["code"] == "command_intent_missing"
    retry = client.post(f"/api/runs/demo/commands/{done['id']}/retry")
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "command_not_retryable"


def test_natural_finalize_completion_between_decision_and_abort_append_is_satisfied(
        monkeypatch, tmp_path):
    """A natural wrap-up in the decision→intent window must not start another engine."""
    rd = _seed(tmp_path)
    driver = _Driver()
    client, srv = _client(tmp_path, driver)
    original_decision = srv.commands._decision
    finalize_decisions = 0
    scope = "finish:natural-race"

    def finish_after_worker_decision(run_dir, event_type):
        nonlocal finalize_decisions
        result = original_decision(run_dir, event_type)
        if event_type == "run_abort":
            finalize_decisions += 1
            # submit() performs the first preflight.  _execute() captures baseline_seq before the
            # second one; publish a complete natural finalization after that decision but before
            # _execute() appends this command's marked run_abort intent.
            if finalize_decisions == 2:
                store = EventStore(rd / "events.jsonl")
                store.append("finalize_step", {"scope": scope, "step": "begun"})
                store.append("run_finished", {
                    "reason": "budget",
                    "finalize_scope": scope,
                })
                store.append("finalize_step", {"scope": scope, "step": "complete"})
        return result

    monkeypatch.setattr(srv.commands, "_decision", finish_after_worker_decision)
    command = _post(
        client, "run_abort", {"reason": "finalized"}, key="natural-finish-race").json()
    done = _terminal(client, command)

    assert done["status"] == "succeeded"
    events = EventStore(rd / "events.jsonl").read_all()
    finish = next(event for event in events if event.type == "run_finished")
    complete = next(event for event in events
                    if event.type == "finalize_step" and event.data.get("step") == "complete")
    abort = next(event for event in events if event.type == "run_abort")
    assert finish.seq < complete.seq < abort.seq
    assert sum(event.type == "run_finished" for event in events) == 1
    assert driver.calls == []


def test_old_empty_command_reservation_is_healed_for_same_key(tmp_path):
    rd = _seed(tmp_path)
    client, _srv = _client(tmp_path, _Driver())
    key = "stale-empty-reservation"
    command_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    reservation = rd / ".commands" / f"{command_id}.json"
    reservation.parent.mkdir(parents=True, exist_ok=True)
    reservation.write_bytes(b"")
    old = time.time() - 2.0
    os.utime(reservation, (old, old))

    response = _post(client, "hint", {"text": "recover"}, key=key)

    assert response.status_code == 200
    assert response.json()["id"] == command_id
    assert response.json()["status"] == "succeeded"
    stored = json.loads(reservation.read_text(encoding="utf-8"))
    assert stored["id"] == command_id and stored["status"] == "succeeded"
    assert _types(rd).count("hint") == 1


def test_thread_start_failure_releases_execution_claim(monkeypatch, tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    key = "thread-start-failure"
    command_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def fail_start(_thread):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread creation failed"):
        srv.commands.submit(
            rd, key, "pause", {}, expected_generation=srv.commands.run_generation(rd))

    assert not srv.commands._exec_path(rd, command_id).exists()


def test_recorded_dead_spawn_pid_retires_claim_before_expiry(tmp_path):
    rd = _seed(tmp_path)
    driver = _Driver(pid_running=False)
    _client_unused, srv = _client(tmp_path, driver, observation=5.0)
    srv.commands.record_external_spawn(rd, "start", 4242)
    claim_path = srv.commands._spawn_claim_path(rd)
    row = json.loads(claim_path.read_text(encoding="utf-8"))
    assert row["expires_at"] > time.time()

    assert srv.commands.spawn_inflight(rd) is False
    assert not claim_path.exists()


def test_finalize_postcondition_requires_explicit_scope_complete_after_hard_kill(tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    store = EventStore(rd / "events.jsonl")
    baseline = store.read_all()[-1].seq
    command_id = "cmd_" + "9" * 32
    intent = store.append("run_abort", {
        "reason": "finalized",
        "_command_id": command_id,
    })
    scope = f"abort:{intent.seq}"
    store.append("finalize_step", {"scope": scope, "step": "begun"})
    store.append("run_finished", {
        "reason": "aborted",
        "finalize_scope": scope,
    })
    _raw, semantic_digest = srv.commands._payload(
        "run_abort", {"reason": "finalized"})
    record = {
        "id": command_id,
        "event_type": "run_abort",
        "data": {"reason": "finalized"},
        "semantic_payload_digest": semantic_digest,
        "event_seq": intent.seq,
        "observe_after_seq": baseline,
        "postcondition": "finished_and_stopped",
    }

    # The engine died after its terminal event but before durable projections completed.
    assert srv.commands._postcondition(rd, record) is False
    store.append("finalize_step", {"scope": scope, "step": "complete"})
    assert srv.commands._postcondition(rd, record) is True


def test_finalize_postcondition_requires_finish_seq_marker_without_scope(tmp_path):
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    store = EventStore(rd / "events.jsonl")
    baseline = store.read_all()[-1].seq
    intent = store.append("run_abort", {"reason": "finalized"})
    finish = store.append(
        "run_finished", {"reason": "aborted", "finalization_required": True})
    _raw, semantic_digest = srv.commands._payload(
        "run_abort", {"reason": "finalized"})
    record = {
        "id": "cmd_" + "8" * 32,
        "event_type": "run_abort",
        "data": {"reason": "finalized"},
        "semantic_payload_digest": semantic_digest,
        "attached": True,
        "attached_event_seq": intent.seq,
        "attached_semantic_payload_digest": semantic_digest,
        "observe_after_seq": baseline,
        "postcondition": "finished_and_stopped",
    }

    assert srv.commands._postcondition(rd, record) is False
    store.append("finalization_finished", {"finish_seq": finish.seq})
    assert srv.commands._postcondition(rd, record) is True


def test_command_and_lock_sidecar_symlinks_are_rejected(monkeypatch, tmp_path):
    rd = _seed(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, rd / ".commands", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    client, _srv = _client(tmp_path, _Driver())
    response = _post(client, "hint", {"text": "must stay in run"})
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert list(outside.iterdir()) == []

    (rd / ".commands").unlink()
    events = rd / "events.jsonl"
    backup = rd / "events.real.jsonl"
    events.rename(backup)
    outside_events = outside / "events.jsonl"
    outside_events.write_bytes(backup.read_bytes())
    before = outside_events.read_bytes()
    os.symlink(outside_events, events)
    response = _post(client, "hint", {"text": "events must stay in run"}, key="events-link")
    assert response.status_code == 409 and outside_events.read_bytes() == before
    events.unlink()
    backup.rename(events)

    lock_target = tmp_path / "lock-outside"
    lock_target.mkdir()
    os.symlink(lock_target, tmp_path / ".command-locks", target_is_directory=True)
    response = _post(client, "hint", {"text": "lock must stay in root"}, key="lock-link")
    assert response.status_code == 409
    assert list(lock_target.iterdir()) == []
