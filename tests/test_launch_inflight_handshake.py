"""The launch-in-flight handshake between the two spawner families (review 2026-09-22, SRV1-09).

Two ledgers record "an engine launch is in flight for this run": the resume CLAIM a log-ledger
spawner appends to `events.jsonl` under `run_lifecycle_lock` (the startup and run-list reconcilers,
their after-exit waiters, the restart hand-off, the legacy resume route), and the spawn LEASE a
command worker writes under the run's sequencer. Each family used to read only its own ledger, so
a command worker could Popen beside a reconciler's still-importing child and the other way round.
The second `looplab resume` is not a harmless no-op on a halted run: it waits for the singleton and
then lifts what the winner left halted.

The cure is a handshake in Dekker's order, because the two families share no lock: each spawner
publishes its own flag and only then reads the other's (`engine/run_lifecycle.py`, "THE
LAUNCH-IN-FLIGHT HANDSHAKE"). These tests DRIVE both halves — the command worker through a real
`/commands` submission, the reconciler through `reconcile_pending_resume` — and pin the order each
half reads in, which is the whole of what makes the handshake exclusive.
"""
from __future__ import annotations

import inspect
import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.engine import run_lifecycle  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve import engine_proc as ep  # noqa: E402
from looplab.serve.command_observation import CommandObservation  # noqa: E402
from looplab.serve.run_commands import LAUNCH_IN_FLIGHT, RunCommandService  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from tests.factories import command_terminal, post_command  # noqa: E402


def _seed(root, run_id="demo"):
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
    (rd / "task.snapshot.json").write_text(
        '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
    return rd


def _claim_a_launch(rd):
    """What a log-ledger spawner leaves in the log right before its Popen: a pending request and
    the `launch_claim` record `_claim_and_spawn_resume` appends for it."""
    store = EventStore(rd / "events.jsonl")
    request = store.append("resume_requested", {"mode": "resume"})
    store.append("resume_requested",
                 {"launch_claim": True, "request_seq": request.seq, "mode": "resume"})
    state = fold(store.read_all())
    assert state.resume_pending() and state.last_resume_launch_seq > state.last_resume_served_seq


class _Driver:
    def __init__(self):
        self.alive = False
        self.calls = []

    def spawn(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return 4242


def _client(root, driver, *, timeout=2.0):
    app = make_app(root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, engine_alive=lambda _rd: driver.alive, spawn_engine=driver.spawn,
        process_alive=lambda _pid: True, process_identity=lambda _pid: "child",
        startup_timeout=0.05, command_timeout=timeout, poll_interval=0.01,
        max_observation_timeout=timeout * 2)
    return TestClient(app), srv


def _ack(rd, command_id):
    events = EventStore(rd / "events.jsonl").read_all()
    intent = next(e for e in events if (e.data or {}).get("_command_id") == command_id)
    EventStore(rd / "events.jsonl").append(
        "command_ack", {"command_id": command_id, "event_seq": intent.seq})


def _record(client, command_id):
    return client.get(f"/api/runs/demo/commands/{command_id}").json()


# --------------------------------------------------------------------------------------------
# The command worker's half: lease up, THEN read the log claim, and step aside for it
# --------------------------------------------------------------------------------------------

def test_a_command_worker_does_not_popen_beside_a_claimed_log_launch(tmp_path):
    """The defect, driven: a reconciler has claimed and launched a resume whose child has not
    reached engine.lock yet. A `/commands` submission that needs a driver used to see no lease and a
    dead lock and Popen a SECOND `looplab resume`. Now it waits for the claimed child — which serves
    its intent once it owns the lock — and never spawns."""
    rd = _seed(tmp_path)
    _claim_a_launch(rd)
    driver = _Driver()
    client, srv = _client(tmp_path, driver)

    command = post_command(client, "budget_extend", {"add_nodes": 1}, key="beside-claim").json()
    time.sleep(0.3)             # admission plus many monitor passes, all inside the claim's grace
    current = _record(client, command["id"])
    assert driver.calls == [], "a second engine was launched beside the claimed one"
    assert current["status"] == "executing" and current.get("waiting_for_spawn") is True

    driver.alive = True         # the claimed child takes engine.lock and acknowledges the intent
    _ack(rd, command["id"])
    assert command_terminal(client, current)["status"] == "succeeded"
    assert driver.calls == []
    # Each pass that stepped aside took its lease down again; none is left to fence later launches.
    assert not srv.commands._spawn_claim_path(rd).exists()


def test_the_worker_spawns_once_the_claim_it_stepped_aside_for_expires(tmp_path, monkeypatch):
    """Both halves may back off in one interleaving, so stepping aside must not be forever: a claim
    whose child died before engine.lock expires on its own clock, and the command monitor's next
    pass launches exactly one driver.

    The expiry is DRIVEN, not waited for: the grace is the clock this verdict asks about, and a
    sub-second real grace raced the app's own start-up on a loaded box (the claim had aged out
    before the first pass looked at it)."""
    rd = _seed(tmp_path)
    _claim_a_launch(rd)
    driver = _Driver()
    client, _srv = _client(tmp_path, driver)

    command = post_command(client, "budget_extend", {"add_nodes": 1}, key="after-expiry").json()
    time.sleep(0.2)
    assert driver.calls == [], "the claim was still fresh"
    monkeypatch.setattr(run_lifecycle, "RESUME_RECONCILE_GRACE_S", 0.0)   # the claim ages out
    deadline = time.time() + 3.0
    while not driver.calls and time.time() < deadline:
        time.sleep(0.02)
    assert len(driver.calls) == 1, "the expired claim still held the worker off"

    driver.alive = True
    _ack(rd, command["id"])
    assert command_terminal(client, _record(client, command["id"]))["status"] == "succeeded"
    assert len(driver.calls) == 1


def test_the_worker_reads_the_log_only_after_its_lease_is_up(tmp_path, monkeypatch):
    """The ORDER is the handshake. Read before the lease, a reconciler could append its claim and
    read no lease in the gap, and both would Popen."""
    rd = _seed(tmp_path)
    driver = _Driver()
    _client_unused, srv = _client(tmp_path, driver)
    command_id = "cmd_" + "c0de" * 8
    path = srv.commands._path(rd, command_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": command_id, "status": "executing", "event_type": "budget_extend",
              "postcondition": "engine_ack", "deadline_at": 0.0, "updated_at": 0.0}
    srv.commands._save(path, record)
    lease_at_read = []

    def observing(observation, now):
        row = json.loads(srv.commands._spawn_claim_path(rd).read_text(encoding="utf-8"))
        lease_at_read.append(row.get("command_id"))
        return False

    monkeypatch.setattr(CommandObservation, "launch_claim_fresh", observing)
    terminalized, pid = srv.commands._spawn_under_claim(
        rd, path, record, command_id, restarting=False)
    assert (terminalized, pid) == (False, 4242)
    assert lease_at_read == [command_id], "the log claim was read before this worker's lease"

    monkeypatch.setattr(CommandObservation, "launch_claim_fresh", lambda _obs, _now: True)
    srv.commands._clear_spawn_claim(rd, command_id)
    terminalized, pid = srv.commands._spawn_under_claim(
        rd, path, record, command_id, restarting=False)
    assert (terminalized, pid) == (False, LAUNCH_IN_FLIGHT)
    assert len(driver.calls) == 1, "a stepped-aside worker still created a process"
    assert not srv.commands._spawn_claim_path(rd).exists(), "the stepped-aside lease stayed up"


def test_only_the_claim_holds_a_worker_not_the_request_grace(tmp_path):
    """`fresh_resume_launch_pending` also fences the request -> claim gap for reset/delete. A worker
    needs only the CLAIM: every log-ledger spawner claims before it Popens, and a request nobody has
    claimed is not a process that could race this one."""
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())

    def claimed() -> bool:
        return srv.commands._observe(rd).launch_claim_fresh(time.time())

    EventStore(rd / "events.jsonl").append("resume_requested", {"mode": "resume"})
    assert run_lifecycle.fresh_resume_launch_pending(rd) is True
    assert claimed() is False
    _claim_a_launch(rd)
    assert claimed() is True
    EventStore(rd / "events.jsonl").append("resume_served", {"engine_owner_boundary": True})
    assert claimed() is False, "a served claim is not in flight"


# --------------------------------------------------------------------------------------------
# The log-ledger half: claim appended, THEN read the lease, and step aside for it
# --------------------------------------------------------------------------------------------

def _zombie(tmp_path, monkeypatch):
    """A run whose durable resume request outlived its spawn: what the reconciler exists to relaunch."""
    rd = _seed(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append("resume_requested", {"mode": "resume"})
    spawns = []
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append((a, k)) or 4343)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_engine_liveness", lambda _rd: False)
    request_ts = fold(store.read_all()).last_resume_request_ts
    return rd, spawns, request_ts


def test_a_reconciler_does_not_popen_beside_a_leased_command_spawn(tmp_path, monkeypatch):
    """The other direction: a command worker's child is importing under its lease when the run
    list's reconciler finds the stale request. It claims, reads the lease, and launches nothing —
    the worker's child serves the request when it takes the lock. The claim stays in the log as a
    bounded quarantine."""
    rd, spawns, request_ts = _zombie(tmp_path, monkeypatch)
    assert ep.reconcile_pending_resume(
        rd, now=request_ts + 31, spawn_inflight=lambda _rd: True) is False
    assert spawns == []
    assert fold(EventStore(rd / "events.jsonl").read_all()).last_resume_launch_seq > 0

    # With no lease the same zombie is relaunched once its claim has aged out, exactly as before.
    claim_ts = fold(EventStore(rd / "events.jsonl").read_all()).last_resume_launch_ts
    assert ep.reconcile_pending_resume(
        rd, now=claim_ts + 31, spawn_inflight=lambda _rd: False) is True
    assert len(spawns) == 1


def test_the_reconciler_reads_the_lease_only_after_its_claim_is_in_the_log(tmp_path, monkeypatch):
    rd, spawns, request_ts = _zombie(tmp_path, monkeypatch)
    claimed_at_read = []

    def observing(run_dir):
        state = fold(EventStore(run_dir / "events.jsonl").read_all())
        claimed_at_read.append(state.last_resume_launch_seq > state.last_resume_served_seq)
        return False

    assert ep.reconcile_pending_resume(rd, now=request_ts + 31, spawn_inflight=observing) is True
    assert claimed_at_read == [True], "the lease was read before this spawner's claim was durable"
    assert len(spawns) == 1


def test_an_unreadable_lease_is_read_as_a_launch_in_flight(tmp_path, monkeypatch):
    rd, spawns, request_ts = _zombie(tmp_path, monkeypatch)

    def unreadable(_rd):
        raise OSError("lease unreadable")

    assert ep.reconcile_pending_resume(rd, now=request_ts + 31, spawn_inflight=unreadable) is False
    assert spawns == [], "uncertain lease evidence was treated as permission to Popen"


def test_the_legacy_routes_own_mirror_is_not_a_foreign_launch(tmp_path):
    """The legacy resume route mirrors its log-claimed launch into the lease and then claims through
    the same helper; reading its own mirror back as a foreign launch would refuse every legacy
    resume. Any OTHER owner's lease still reads as in flight."""
    rd = _seed(tmp_path)
    _client_unused, srv = _client(tmp_path, _Driver())
    srv.commands.begin_external_spawn(rd, "legacy-resume")
    assert srv.commands.spawn_inflight(rd, ignoring="legacy-resume") is False
    assert srv.commands.spawn_inflight(rd) is True
    srv.commands.cancel_external_spawn(rd, "legacy-resume")
    srv.commands.begin_external_spawn(rd, "reset:op")
    assert srv.commands.spawn_inflight(rd, ignoring="legacy-resume") is True


@pytest.mark.parametrize("helper", [
    ep._claim_and_spawn_resume, ep.reconcile_pending_resume, ep._spawn_engine_after_exit,
    ep.install_resume_reconcile_hooks,
])
def test_every_log_ledger_spawner_must_state_its_lease_reader(helper):
    """REQUIRED, not defaulted: a new log-ledger call site cannot reach production without deciding
    whether a command worker may be launching beside it — `None` is a statement, never an omission."""
    parameter = inspect.signature(helper).parameters["spawn_inflight"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
