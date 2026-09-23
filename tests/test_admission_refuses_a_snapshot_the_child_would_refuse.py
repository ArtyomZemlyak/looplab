"""A command the server will answer by SPAWNING a driver is refused at ADMISSION when that driver
would refuse the run's config snapshot (review 2026-09-22, doc 66 §6 item 6 — the W2-2 tail).

Every driver the UI server starts for an existing run is `looplab resume` (or, for a finalize
handoff on the legacy route, `looplab finalize`), and both read `config.snapshot.json` STRICTLY
(`cli/__init__.py::load_run_settings(strict=True)` -> `refuse_unknown=True`): a setting this build
does not know, a newer snapshot format, or a damaged file is refused at exit 2. The server did not
ask. Driven on the pre-fix tree: a `resume` command on a paused run with no live engine and one
unknown key in its snapshot was ACCEPTED, appended its marked `resume` intent to events.jsonl,
and Popen'd the child — which then exits 2 before engine.lock, so the server sees a crashed
process rather than a coded refusal, and `_monitor` re-spawns the same doomed child until the
command's deadline. The legacy `POST /resume` did the same after a durable `resume_requested`.

Admission now asks the SAME read the child runs (`core/config.py::read_config_snapshot`, through
`serve/engine_proc.py::spawn_snapshot_refusal`) exactly when admission will spawn
(`serve/run_commands.py::admission_spawns_driver`), and refuses before any durable append: a
REJECTED command record (the `/commands` protocol's shape for a refusal — see
`tests/test_fork_from_seq.py`), or a coded 409 on the legacy route. An ABSENT snapshot stays the
child's decision.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.config import (  # noqa: E402
    CONFIG_SNAPSHOT_SCHEMA, CONFIG_SNAPSHOT_SCHEMA_KEY, Settings)
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.control_validation import CONTROL_SPECS, EnginePolicy  # noqa: E402
from looplab.serve.engine_proc import (  # noqa: E402
    SPAWN_SNAPSHOT_REFUSAL_CODES, spawn_snapshot_refusal)
from looplab.serve.run_commands import RunCommandService, admission_spawns_driver  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from tests.factories import command_terminal, post_command  # noqa: E402

UNKNOWN_KEY = "llm_spend_cap_from_a_newer_build"


def _valid() -> dict:
    return Settings().masked_snapshot()


# name -> (builder of the snapshot bytes, the code the server must answer, or None for "the child
# reads it"). Built lazily, inside the test, so `Settings()` sees the suite's isolated environment.
_SNAPSHOTS = {
    "valid": (lambda: json.dumps(_valid()).encode(), None),
    "partial but valid": (lambda: b'{"max_nodes": 3}', None),
    "unknown key": (lambda: json.dumps({**_valid(), UNKNOWN_KEY: 1.0}).encode(),
                    "config_snapshot_incompatible"),
    "newer format": (lambda: json.dumps({**_valid(),
                                         CONFIG_SNAPSHOT_SCHEMA_KEY: CONFIG_SNAPSHOT_SCHEMA + 1
                                         }).encode(), "config_snapshot_incompatible"),
    "malformed format marker": (lambda: json.dumps({**_valid(), CONFIG_SNAPSHOT_SCHEMA_KEY: "3"}
                                                   ).encode(), "config_snapshot_incompatible"),
    "not json": (lambda: b"{not json", "config_snapshot_invalid"),
    "not an object": (lambda: b"[1, 2]", "config_snapshot_invalid"),
    "invalid value": (lambda: json.dumps({**_valid(), "max_nodes": "many"}).encode(),
                      "config_snapshot_invalid"),
    "not utf-8": (lambda: b'\xff\xfe{"max_nodes": 3}', "config_snapshot_invalid"),
}


def _snapshot(name: str) -> bytes:
    return _SNAPSHOTS[name][0]()


def _seed(root: Path, *, snapshot: bytes | None, paused: bool = True, run_id: str = "demo") -> Path:
    rd = root / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "task", "goal": "g",
                                 "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"}, "code": "print(1)"})
    if paused:
        store.append("pause", {})
    (rd / "task.snapshot.json").write_text(
        '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
    if snapshot is not None:
        (rd / "config.snapshot.json").write_bytes(snapshot)
    return rd


class _Engine:
    """The injected liveness probe and spawner: records every Popen the service asks for."""

    def __init__(self, *, alive: bool = False):
        self.alive = alive
        self.spawns: list[list[str]] = []

    def is_alive(self, _rd) -> bool:
        return self.alive

    def spawn(self, args, **_kwargs):
        self.spawns.append(list(args))
        return 4242


def _client(root: Path, engine: _Engine, *, timeout: float = 0.5):
    app = make_app(root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, engine_alive=engine.is_alive, spawn_engine=engine.spawn,
        process_alive=lambda _pid: True, process_identity=lambda _pid: "child",
        startup_timeout=0.05, command_timeout=timeout, poll_interval=0.01,
        max_observation_timeout=timeout * 4)
    return TestClient(app), srv


def _log_bytes(rd: Path) -> bytes:
    return (rd / "events.jsonl").read_bytes()


# ------------------------------------------------------------------------------ the one read

@pytest.mark.parametrize("name", sorted(_SNAPSHOTS))
def test_the_server_refuses_exactly_what_the_child_refuses(tmp_path, name):
    """"The SAME read", driven both ways over one file: the server's verdict and the child's own
    loader agree on every snapshot, valid or not. A server check looser than the child is a crashed
    child again; a stricter one refuses runs the child would have resumed."""
    from looplab.cli import load_run_settings

    expected_code = _SNAPSHOTS[name][1]
    rd = _seed(tmp_path, snapshot=_snapshot(name))
    try:
        load_run_settings(rd, strict=True, require_snapshot=True)   # what `looplab resume` runs
        child_refuses = False
    except Exception:  # noqa: BLE001 — ANY failure of the child's read is a child that exits
        child_refuses = True

    refused = spawn_snapshot_refusal(rd)
    assert (refused is not None) == child_refuses, (name, refused)
    assert (refused or {}).get("code") == expected_code, (name, refused)
    if refused is not None:
        assert refused["code"] in SPAWN_SNAPSHOT_REFUSAL_CODES
        assert refused["message"] and refused["remediation"] and refused["retryable"] is False
        # Never the host path an OSError/decoder would carry into a browser.
        assert str(tmp_path) not in json.dumps(refused)


def test_the_incompatible_refusal_names_the_key_it_would_drop(tmp_path):
    rd = _seed(tmp_path, snapshot=_snapshot("unknown key"))
    assert UNKNOWN_KEY in spawn_snapshot_refusal(rd)["message"]


def test_a_missing_snapshot_is_the_childs_decision_not_the_servers(tmp_path):
    """`resume` refuses a run without one and `finalize` grandfathers it — the child's rule, so the
    server admits and lets the child answer, as before."""
    assert spawn_snapshot_refusal(_seed(tmp_path, snapshot=None)) is None


@pytest.mark.parametrize("policy", list(EnginePolicy))
@pytest.mark.parametrize("alive", [False, True])
def test_admission_spawns_a_driver_exactly_when_the_command_service_will(policy, alive):
    """The rule `_admit` acts on, stated so `_decision` can ask it before the append."""
    expected = (policy is not EnginePolicy.NO_SPAWN
                and (policy is EnginePolicy.RESTART_AFTER_EXIT or not alive))
    assert admission_spawns_driver(policy, alive) is expected


# ------------------------------------------------------------------------------ /commands

@pytest.mark.parametrize("event_type, data", [
    ("resume", {}),
    ("run_abort", {"reason": "finalized"}),          # finalize: ENSURE_DRIVER_PRESERVE_STOP
    ("budget_extend", {"add_nodes": 1}),             # any ENSURE_RUNNING intent on a dead engine
])
def test_a_command_that_would_spawn_is_rejected_before_any_append(tmp_path, event_type, data):
    """THE DEFECT, driven: no live engine, one unknown key -> a coded refusal, the event log
    byte-identical, and no Popen. MUTATION: drop the preflight from `_decision` -> the record is
    accepted, the intent is appended and the child is spawned."""
    assert CONTROL_SPECS[event_type].engine_policy is not EnginePolicy.NO_SPAWN
    rd = _seed(tmp_path, snapshot=_snapshot("unknown key"))
    engine = _Engine(alive=False)
    client, _srv = _client(tmp_path, engine)
    before = _log_bytes(rd)

    response = post_command(client, event_type, data, f"{event_type}-bad-snapshot")

    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "rejected", record
    assert record["error"]["code"] == "config_snapshot_incompatible"
    assert UNKNOWN_KEY in record["error"]["message"]
    assert record["error"]["retryable"] is False
    assert record.get("event_seq") is None
    assert _log_bytes(rd) == before, "nothing was appended"
    assert engine.spawns == [], "no driver was started"
    # The refusal is terminal and durable: the same key replays it, and still spawns nothing.
    again = post_command(client, event_type, data, f"{event_type}-bad-snapshot").json()
    assert again["id"] == record["id"] and again["status"] == "rejected"
    assert _log_bytes(rd) == before and engine.spawns == []


def test_a_damaged_snapshot_is_refused_without_its_text(tmp_path):
    rd = _seed(tmp_path, snapshot=_snapshot("not json"))
    engine = _Engine(alive=False)
    client, _srv = _client(tmp_path, engine)
    before = _log_bytes(rd)

    record = post_command(client, "resume", {}, "resume-damaged").json()

    assert record["status"] == "rejected"
    assert record["error"]["code"] == "config_snapshot_invalid"
    assert str(tmp_path) not in json.dumps(record) and "Expecting" not in json.dumps(record)
    assert _log_bytes(rd) == before and engine.spawns == []


def _ack_the_marked_intent(rd: Path, command_id: str) -> None:
    """What a real engine appends once it has folded the command's marked intent."""
    deadline = time.time() + 30
    while True:
        marked = [e for e in EventStore(rd / "events.jsonl").read_all()
                  if (e.data or {}).get("_command_id") == command_id]
        if marked:
            break
        assert time.time() < deadline, "the intent never reached the log"
        time.sleep(0.01)
    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": command_id, "event_seq": marked[-1].seq})


def test_a_live_engine_is_not_blocked_by_a_bad_snapshot(tmp_path):
    """No spawn, no preflight: a live engine already holds its settings, so a NO_SPAWN intent is
    untouched and a resume of the paused run is appended for it to read. MUTATION: ask the
    snapshot whatever the liveness -> the resume is refused."""
    rd = _seed(tmp_path, snapshot=_snapshot("unknown key"))
    engine = _Engine(alive=True)
    client, _srv = _client(tmp_path, engine, timeout=30.0)

    hint = command_terminal(client, post_command(client, "hint", {"text": "keep going"},
                                                 "hint-live").json())
    assert hint["status"] == "succeeded", hint
    resume = post_command(client, "resume", {}, "resume-live").json()
    assert resume["status"] != "rejected", resume
    _ack_the_marked_intent(rd, resume["id"])
    assert command_terminal(client, resume)["status"] == "succeeded"
    assert engine.spawns == []


def test_a_restart_is_refused_even_with_a_live_engine(tmp_path):
    """A restart ALWAYS ends in a spawn — the replacement owner is its whole point — so it is refused
    up front rather than stopping a live engine whose replacement could never start."""
    rd = _seed(tmp_path, snapshot=_snapshot("newer format"), paused=False)
    engine = _Engine(alive=True)
    client, _srv = _client(tmp_path, engine)
    before = _log_bytes(rd)

    record = post_command(client, "restart", {}, "restart-live").json()

    assert record["status"] == "rejected", record
    assert record["error"]["code"] == "config_snapshot_incompatible"
    assert _log_bytes(rd) == before and engine.spawns == []


def test_a_snapshot_that_breaks_after_submit_is_caught_at_admission(tmp_path):
    """The verdict is re-taken under the sequencer immediately before the append (`_admit` asks
    `_decision` again), so a snapshot that turns unreadable between the submit and the worker is
    still refused before anything is written. MUTATION: ask only at submit -> appended + spawned."""
    rd = _seed(tmp_path, snapshot=_snapshot("valid"))
    engine = _Engine(alive=False)
    client, srv = _client(tmp_path, engine)
    srv.commands._start_worker = lambda *_args, **_kwargs: None      # hold the accepted record
    accepted = post_command(client, "resume", {}, "resume-then-break").json()
    assert accepted["status"] == "accepted", accepted

    (rd / "config.snapshot.json").write_bytes(_snapshot("unknown key"))
    before = _log_bytes(rd)
    del srv.commands._start_worker                                  # release: GET re-drives it
    record = command_terminal(client, client.get(
        f"/api/runs/demo/commands/{accepted['id']}").json())

    assert record["status"] == "rejected", record
    assert record["error"]["code"] == "config_snapshot_incompatible"
    assert _log_bytes(rd) == before and engine.spawns == []


def test_a_missing_snapshot_still_spawns_through_commands(tmp_path):
    """The control: with no snapshot the command is admitted and the driver started, as before."""
    _seed(tmp_path, snapshot=None)
    engine = _Engine(alive=False)
    client, _srv = _client(tmp_path, engine)

    record = post_command(client, "resume", {}, "resume-no-snapshot").json()

    assert record["status"] != "rejected", record
    deadline = time.time() + 30
    while not engine.spawns:
        assert time.time() < deadline, "the driver was never started"
        time.sleep(0.01)
    assert engine.spawns[0][0] == "resume"


# ------------------------------------------------------------------------------ legacy /resume

def _legacy_client(tmp_path, monkeypatch):
    import looplab.serve.routers.control as control_router

    spawns: list[list[str]] = []

    def spawn(args, **_kwargs):
        spawns.append(list(args))
        return 9201

    monkeypatch.setattr(control_router, "_spawn_engine", spawn)
    return TestClient(make_app(tmp_path)), spawns


def test_the_legacy_resume_route_answers_a_coded_409_before_any_append(tmp_path, monkeypatch):
    """Same refusal on the deprecated route, as the HTTP status it speaks. MUTATION: drop its
    preflight -> 200, a `resume_requested` handoff in the log and a spawned child."""
    rd = _seed(tmp_path, snapshot=_snapshot("unknown key"))
    client, spawns = _legacy_client(tmp_path, monkeypatch)
    before = _log_bytes(rd)

    response = client.post("/api/runs/demo/resume")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "config_snapshot_incompatible" and UNKNOWN_KEY in detail["message"]
    assert _log_bytes(rd) == before and spawns == []


def test_the_legacy_resume_route_still_spawns_on_a_readable_snapshot(tmp_path, monkeypatch):
    rd = _seed(tmp_path, snapshot=_snapshot("valid"))
    client, spawns = _legacy_client(tmp_path, monkeypatch)

    response = client.post("/api/runs/demo/resume")

    assert response.status_code == 200, response.text
    assert len(spawns) == 1 and spawns[0][0] == "resume"
    assert "resume_requested" in [e.type for e in EventStore(rd / "events.jsonl").read_all()]
