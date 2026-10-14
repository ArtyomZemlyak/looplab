"""The cross-run GPU order (`engine/gpu_queue.py`): a FIFO in front of the host lease, and the gate.

Audit 2026-10-14 L-02 / incident C2: the successor run's handoff raced the predecessor's lease
release by ~2 s because ordering lived in agent scripts. These tests drive the ordering LoopLab now
owns: the run that waited longest gets the pool, a dead or stalled waiter never jams the queue, and
a run gated `start_after` another takes no GPU until that run's engine has exited."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from looplab.engine import gpu_queue
from looplab.engine.gpu_queue import (GateReader, GpuQueueTicket, gate_blockers, live_tickets,
                                      queue_status, read_gate, write_gate)
from looplab.engine.resources import ResourceSchedulingMixin
from tests._posix_gates import FLOCK


class _Pool(ResourceSchedulingMixin):
    def __init__(self, lease_path, *, run_dir=None, ids=(0, 1)):
        self._gpu_ids = list(ids)
        self._gpu_physical_ids = {gpu: str(gpu) for gpu in ids}
        self._gpu_mem = {}
        self._free_gpus = list(ids)
        self._gpu_host_lease_path = lease_path
        self._gpu_host_lease_handle = None
        self._gpu_lock = threading.Lock()
        self._gpu_condition = threading.Condition(self._gpu_lock)
        self._gpu_epoch = 0
        self._eval_gpu_reservations = {}
        if run_dir is not None:
            self.run_dir = run_dir


@FLOCK
def test_tickets_are_served_first_come_first_served(tmp_path):
    lease = tmp_path / "gpu-pool.lock"
    first, second = GpuQueueTicket(lease, run_id="a"), GpuQueueTicket(lease, run_id="b")
    assert first.ensure() and second.ensure()
    assert first.is_head() and not second.is_head()
    assert [row["run_id"] for row in live_tickets(lease)] == ["a", "b"]
    first.release()
    assert second.is_head()
    second.release()
    assert live_tickets(lease) == []


@FLOCK
def test_a_released_holder_queues_behind_the_run_that_was_already_waiting(tmp_path):
    """THE FAIRNESS PROPERTY: the historical lease let the holder take the pool straight back
    between two of its own evaluations; now the waiter that came first is served first, and a
    later arrival polling first cannot overtake it."""
    lease = tmp_path / "gpu-pool.lock"
    holder, early, late = _Pool(lease), _Pool(lease), _Pool(lease)
    assert holder._acquire_gpus(2) == [0, 1]
    assert early._acquire_gpus(1) is None          # queued first
    assert late._acquire_gpus(1) is None           # queued second
    holder._release_gpus([0, 1])                   # lease released
    assert late._acquire_gpus(1) is None           # polls first, but is not the head
    assert holder._acquire_gpus(1) is None         # the old holder re-queues at the tail
    assert early._acquire_gpus(1) == [0]           # the longest waiter wins the hand-off
    early._release_gpus([0])
    assert holder._acquire_gpus(1) is None         # `late` is ahead of the re-queued holder
    assert late._acquire_gpus(1) == [0]
    late._release_gpus([0])
    assert holder._acquire_gpus(1) == [0]
    holder._release_gpus([0])


@FLOCK
def test_a_dead_waiters_ticket_is_pruned_not_a_permanent_jam(tmp_path):
    lease = tmp_path / "gpu-pool.lock"
    code = ("import sys; from looplab.engine.gpu_queue import GpuQueueTicket;"
            "t=GpuQueueTicket(sys.argv[1], run_id='dead'); assert t.ensure(); print(t.name)")
    out = subprocess.run([sys.executable, "-c", code, str(lease)], capture_output=True, text=True,
                         check=True, cwd=os.getcwd())
    name = out.stdout.strip()
    assert (gpu_queue.queue_dir(lease) / name).exists()   # the process exited without releasing
    mine = GpuQueueTicket(lease, run_id="alive")
    assert mine.ensure()
    assert mine.is_head()                                  # the dead ticket does not count
    assert not (gpu_queue.queue_dir(lease) / name).exists()
    mine.release()


@FLOCK
def test_a_live_waiter_that_stopped_polling_stops_counting_as_ahead(tmp_path, monkeypatch):
    lease = tmp_path / "gpu-pool.lock"
    stalled, mine = GpuQueueTicket(lease, run_id="stalled"), GpuQueueTicket(lease, run_id="mine")
    assert stalled.ensure() and mine.ensure()
    assert not mine.is_head()
    old = time.time() - gpu_queue.HEARTBEAT_STALE_S - 5
    os.utime(gpu_queue.queue_dir(lease) / stalled.name, (old, old))
    assert mine.is_head()
    row = next(r for r in live_tickets(lease, prune=False) if r["run_id"] == "stalled")
    assert row["alive"] is True and row["waiting"] is False
    stalled.release()
    mine.release()


def test_gate_hold_and_start_after_block_until_released(tmp_path):
    run, before = tmp_path / "b", tmp_path / "a"
    write_gate(run, hold=True, start_after=[str(before)], reason="queued after a")
    gate = read_gate(run)
    assert gate["hold"] is True and gate["start_after"] == [str(before.resolve())]
    running = {str(before.resolve()): True}
    kinds = [b["kind"] for b in gate_blockers(gate, liveness=lambda p: running[str(p)])]
    assert kinds == ["hold", "start_after"]
    write_gate(run, hold=False, start_after=[str(before)])
    gate = read_gate(run)
    assert [b["kind"] for b in gate_blockers(gate, liveness=lambda p: True)] == ["start_after"]
    # An inconclusive probe keeps waiting: a predecessor we cannot judge has not released.
    assert gate_blockers(gate, liveness=lambda p: None)[0]["engine_running"] is None
    assert gate_blockers(gate, liveness=lambda p: False) == []


def test_a_malformed_gate_holds_instead_of_starting_gpu_work(tmp_path):
    (tmp_path / "gpu_gate.json").write_text("{not json")
    gate = read_gate(tmp_path)
    assert gate["hold"] is True and gate["malformed"] is True
    (tmp_path / "gpu_gate.json").write_text(json.dumps({"hold": "yes"}))
    assert read_gate(tmp_path)["hold"] is True


@FLOCK
def test_engine_admission_waits_for_its_gate_then_takes_the_pool(tmp_path):
    lease = tmp_path / "gpu-pool.lock"
    predecessor = tmp_path / "run-a"
    run = tmp_path / "run-b"
    write_gate(run, hold=False, start_after=[str(predecessor)])
    alive = {"a": True}
    engine = _Pool(lease, run_dir=run)
    engine._gpu_gate_reader = GateReader(run, liveness=lambda p: alive["a"], clock=_Clock())
    assert engine._acquire_gpus(1) is None
    assert queue_status(lease)["queue_length"] == 0   # a gated run takes no place in the queue
    alive["a"] = False                                 # the predecessor's engine exited
    engine._gpu_gate_reader._clock.advance(10)
    assert engine._acquire_gpus(1) == [0]
    status = queue_status(lease)
    assert status["holder"] is None or status["holder"]["pid"] == os.getpid()
    holder = json.loads(gpu_queue.holder_path(lease).read_text())
    assert holder["run_id"] == "run-b"
    engine._release_gpus([0])
    assert not gpu_queue.holder_path(lease).exists()


def test_gate_reader_rereads_a_released_hold(tmp_path):
    run = tmp_path / "run"
    write_gate(run, hold=True, start_after=[])
    reader = GateReader(run, liveness=lambda p: False)
    assert [b["kind"] for b in reader.blockers()] == ["hold"]
    time.sleep(0.01)
    write_gate(run, hold=False, start_after=[], reason="released")
    assert reader.blockers() == []
    gpu_queue.clear_gate(run)
    assert reader.blockers() == []


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.mark.parametrize("platform", ["nt"])
def test_queue_is_off_on_windows_and_admission_falls_back(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(gpu_queue, "queue_supported", lambda: False)
    ticket = GpuQueueTicket(tmp_path / "gpu-pool.lock")
    ticket.disabled = True
    assert ticket.ensure() is False and ticket.is_head() is True


def _app_with_runs(tmp_path, *names):
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    from looplab.events.eventstore import EventStore

    root = tmp_path / "runs"
    for name in names:
        (root / name).mkdir(parents=True)
        EventStore(root / name / "events.jsonl").append(
            "run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": "min"})
    return root, TestClient(make_app(root))


def test_gpu_gate_api_orders_runs_and_refuses_cycles(tmp_path):
    root, client = _app_with_runs(tmp_path, "a", "b", "c")
    r = client.put("/api/runs/b/gpu-gate", json={"start_after": ["a"], "reason": "after a"})
    assert r.status_code == 200, r.text
    assert r.json()["gate"]["start_after"] == [str((root / "a").resolve())]
    assert client.put("/api/runs/c/gpu-gate", json={"start_after": ["b"]}).status_code == 200
    cyc = client.put("/api/runs/a/gpu-gate", json={"start_after": ["c"]})
    assert cyc.status_code == 409
    assert client.put("/api/runs/a/gpu-gate", json={"start_after": ["a"]}).status_code == 400
    assert client.put("/api/runs/a/gpu-gate", json={"start_after": ["zz"]}).status_code == 404
    held = client.put("/api/runs/a/gpu-gate", json={"hold": True}).json()
    assert [b["kind"] for b in held["blockers"]] == ["hold"]
    assert client.get("/api/runs/a/gpu-gate").json()["gate"]["hold"] is True
    queue = client.get("/api/gpu-queue").json()
    assert {g["run_id"] for g in queue["gates"]} == {"a", "b", "c"}
    assert "queue" in queue and "holder" in queue
    cleared = client.put("/api/runs/a/gpu-gate", json={"clear": True}).json()
    assert cleared["gate"] is None and read_gate(root / "a") is None


def test_gpu_gate_cli_writes_a_gate_before_the_run_exists(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app

    run = tmp_path / "not-yet"
    other = tmp_path / "first"
    result = CliRunner().invoke(app, ["gpu-gate", str(run), "--hold", "--start-after", str(other),
                                      "--reason", "queued"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["gate"]["hold"] is True and body["gate"]["start_after"] == [str(other.resolve())]
    released = json.loads(CliRunner().invoke(app, ["gpu-gate", str(run), "--release"]).output)
    assert released["gate"]["hold"] is False and released["gate"]["start_after"]
    cleared = json.loads(CliRunner().invoke(app, ["gpu-gate", str(run), "--clear"]).output)
    assert cleared["gate"] is None
    queue = CliRunner().invoke(app, ["gpu-queue"])
    assert queue.exit_code == 0 and "queue_length" in queue.output
