"""Phase 0 of the training-log monitor (`engine/train_monitor.py`): pure log-digest helpers + the
per-eval observer coroutine. Phase 0 is advisory-only — it emits a `train_monitor` TRACE span per tick
and touches NO event store, so `off == today` and even ON it never changes folded state."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import anyio

from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine.train_monitor import (
    TrainingMonitorMixin,
    active_training_log,
    read_training_tail,
    training_log_digest,
)


# --------------------------------------------------------------------------- pure digest helpers
def test_digest_collapses_progress_rerenders_to_the_latest_snapshot():
    # A tqdm/epoch bar re-renders in place (carriage returns): thousands of same-SKELETON snapshots that
    # differ only in numbers must collapse to just the latest, so the digest is the recent NARRATIVE.
    raw = "".join(f"\rEpoch 1: {i}%|### | loss=0.5{i} acc=0.9{i}" for i in range(100)) + "\n"
    raw += "\n".join(f"step {i} loss: {0.5 - i*0.01:.3f}" for i in range(5)) + "\n"
    digest = training_log_digest(raw)
    # exactly one line for the collapsed progress bar (its last snapshot) + the 5 distinct step lines
    assert digest.count("Epoch 1:") == 1
    assert "99%" in digest                                  # kept the LATEST snapshot, not the first
    assert digest.count("step ") == 5


def test_digest_bounds_lines_and_chars():
    raw = "\n".join(f"unique line number {i} with distinct text {i*i}" for i in range(500))
    d = training_log_digest(raw, max_lines=10, max_chars=100000)
    assert d.count("\n") + 1 == 10                          # only the last 10 lines
    assert "line number 499" in d and "line number 490" in d and "line number 489" not in d
    capped = training_log_digest(raw, max_lines=500, max_chars=200)
    assert len(capped) <= 200


def test_digest_empty_and_whitespace():
    assert training_log_digest("") == ""
    assert training_log_digest("   \n\n \r ") == ""


# --------------------------------------------------------------------------- log-file selection + tail
def test_active_log_picks_freshest_stage_and_none_when_absent(tmp_path):
    assert active_training_log(tmp_path) is None            # no *.log yet
    (tmp_path / "setup.log").write_text("installing deps\n")
    train = tmp_path / "train.log"
    train.write_text("epoch 1 loss: 0.4\n")
    import os
    # make train.log unambiguously the freshest regardless of write-order timer resolution
    os.utime(tmp_path / "setup.log", (1, 1))
    assert active_training_log(tmp_path) == train


def test_read_tail_is_bounded_and_digested(tmp_path):
    log = tmp_path / "train.log"
    log.write_text("HEADER-should-be-dropped\n" + "\n".join(
        f"step {i} loss: {1.0/(i+1):.4f}" for i in range(5000)) + "\n")
    tail = read_training_tail(tmp_path, max_read_bytes=2000)
    assert "HEADER-should-be-dropped" not in tail           # only the tail bytes were read
    assert "step 4999" in tail                              # the most recent lines survive
    assert read_training_tail(tmp_path / "nonexistent-dir") == ""


# --------------------------------------------------------------------------- the observer coroutine
class _MonitorStub(TrainingMonitorMixin):
    """Minimal host for the mixin: the coroutine only needs `tracer` + the interval attr."""
    def __init__(self, tracer, interval):
        self.tracer = tracer
        self._train_monitor_interval_s = interval


def _run_monitor(tmp_path, *, workdir, hold_s=0.22):
    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
    stub = _MonitorStub(tracer, interval=0.05)
    cancel = threading.Event()

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(stub._monitor_training, 0, 0, str(workdir), cancel)
            await anyio.sleep(hold_s)          # let it tick a few times
            tg.cancel_scope.cancel()           # eval "finished" -> stop the monitor (as _evaluate does)

    anyio.run(drive)
    spans_file = tmp_path / "spans.jsonl"
    if not spans_file.exists():
        return []
    return [json.loads(ln) for ln in spans_file.read_text().splitlines() if ln.strip()]


def test_monitor_emits_train_spans_and_stops_on_cancel(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("epoch 1 loss: 0.5\nepoch 2 loss: 0.4\n")

    spans = _run_monitor(tmp_path, workdir=wd)
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm, "monitor emitted no train_monitor spans"
    for s in tm:
        assert s["attributes"].get("node_id") == 0
        assert s["attributes"].get("digest_lines", 0) >= 1
        assert s["attributes"].get("digest_chars", 0) > 0


def test_monitor_no_span_without_a_log(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()                                              # no *.log -> nothing to observe
    spans = _run_monitor(tmp_path, workdir=wd)
    assert [s for s in spans if s.get("name") == "train_monitor"] == []


# --------------------------------------------------------------------------- Phase 1: LLM verdict
from looplab.events.types import EV_TRAIN_MONITOR_ALERT  # noqa: E402


class _FakeClient:
    def __init__(self, verdict):
        self._verdict = verdict
        self.calls = 0

    def complete_tool(self, messages, schema):             # the tool_call parser path
        self.calls += 1
        return dict(self._verdict)


class _FakeDeveloper:
    def __init__(self, client):
        self.client = client


class _VerdictHost(TrainingMonitorMixin):
    def __init__(self, tracer, developer, interval=0.05):
        self.tracer = tracer
        self.developer = developer
        self._train_monitor_interval_s = interval
        self._write_lock = anyio.Lock()
        self.store = _FakeStore()


class _FakeStore:
    def __init__(self):
        self.events = []

    def append(self, event_type, data):
        self.events.append((event_type, data))


def _run_verdict_monitor(tmp_path, *, workdir, developer, hold_s=0.22):
    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
    host = _VerdictHost(tracer, developer, interval=0.05)
    cancel = threading.Event()

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_training, 0, 0, str(workdir), cancel, "ctx")
            await anyio.sleep(hold_s)
            tg.cancel_scope.cancel()

    anyio.run(drive)
    spans = ([json.loads(ln) for ln in (tmp_path / "spans.jsonl").read_text().splitlines() if ln.strip()]
             if (tmp_path / "spans.jsonl").exists() else [])
    return host, spans


def test_broken_verdict_appends_alert_event_and_stamps_span(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: nan\nRuntimeError: CUDA error: device-side assert\n")
    client = _FakeClient({"status": "broken", "reason": "loss is nan and a CUDA assert fired",
                          "confidence": 0.95})
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client))

    alerts = [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT]
    assert alerts, "a broken verdict must append an alert event"
    assert alerts[0]["status"] == "broken" and alerts[0]["node_id"] == 0
    assert 0.0 <= alerts[0]["confidence"] <= 1.0
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm and tm[0]["attributes"].get("status") == "broken"


def test_healthy_verdict_stays_trace_only_no_event(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\nstep 3 loss: 0.3\n")
    client = _FakeClient({"status": "healthy", "reason": "loss steadily decreasing", "confidence": 0.8})
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client))

    assert [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT] == []   # clean event log
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm and tm[0]["attributes"].get("status") == "healthy"                       # but traced


def test_unchanged_digest_does_not_re_call_the_llm(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")     # static log across every tick
    client = _FakeClient({"status": "healthy", "reason": "ok", "confidence": 0.7})
    host, _ = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), hold_s=0.3)
    assert client.calls == 1, f"LLM should fire once for a static digest, not per tick (fired {client.calls})"


def test_no_client_degrades_to_trace_only_observation(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\n")
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(None))

    assert [e for e in host.store.events if e[0] == EV_TRAIN_MONITOR_ALERT] == []
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm, "still observes (Phase 0 trace) without an LLM client"
    assert "status" not in tm[0]["attributes"]             # no verdict without a client
