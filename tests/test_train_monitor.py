"""Phase 0 of the training-log monitor (`engine/train_monitor.py`): pure log-digest helpers + the
per-eval observer coroutine. Phase 0 is advisory-only — it emits a `train_monitor` TRACE span per tick
and touches NO event store, so `off == today` and even ON it never changes folded state."""
from __future__ import annotations

import json
import threading

import anyio
import pytest

from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.core.models import Event
from looplab.engine.train_monitor import (
    TrainingMonitorMixin,
    active_training_log,
    claim_watchdog_kill,
    read_training_tail,
    read_training_tail_raw,
    snapshot_training_logs,
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


def test_digest_preserves_windows_crlf_records_but_collapses_bare_cr_redraws():
    raw = "step 1 loss: 0.5\r\nstep 2 loss: 0.4\r\nprogress 10%\rprogress 90%\r\n"
    assert training_log_digest(raw).splitlines() == [
        "step 1 loss: 0.5",
        "step 2 loss: 0.4",
        "progress 90%",
    ]


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


def test_attempt_snapshot_excludes_old_bytes_and_reads_only_crlf_append(tmp_path):
    log = tmp_path / "train.log"
    log.write_bytes(b'{"metric": 0.01, "step": 1}\r\n')
    snapshot = snapshot_training_logs(tmp_path)

    assert read_training_tail_raw(tmp_path, snapshot=snapshot) == ""
    with open(log, "ab") as fh:
        fh.write(b'{"metric": 0.75, "step": 2}\r\n')

    assert read_training_tail_raw(tmp_path, snapshot=snapshot) == (
        '{"metric": 0.75, "step": 2}\r\n')

    empty_dir = tmp_path / "fresh"
    empty_dir.mkdir()
    empty_snapshot = snapshot_training_logs(empty_dir)
    (empty_dir / "train.log").write_bytes(b"current attempt\n")
    assert read_training_tail_raw(empty_dir, snapshot=empty_snapshot) == "current attempt\n"


def test_attempt_snapshot_detects_truncate_regrow_and_rotation(tmp_path):
    log = tmp_path / "train.log"
    log.write_bytes(b"OLD-ATTEMPT\n" * 12)
    snapshot = snapshot_training_logs(tmp_path)

    # Same path/inode can be truncated and regrow past its old offset before the first poll. The
    # boundary probe must still identify it as a fresh file and read from byte zero.
    fresh = "NEW-ATTEMPT\n" * 20
    log.write_bytes(fresh.encode("utf-8"))
    assert read_training_tail_raw(tmp_path, snapshot=snapshot) == fresh

    # Rotation that only renames the old inode to another *.log path must not make those old bytes look
    # like a newly-created current-attempt file.
    second_snapshot = snapshot_training_logs(tmp_path)
    rotated = tmp_path / "rotated.log"
    log.rename(rotated)
    assert read_training_tail_raw(tmp_path, snapshot=second_snapshot) == ""

    # A replacement at the original path has a new identity and must be read from byte zero.
    log.write_bytes(b"REPLACEMENT\n")
    import os
    os.utime(rotated, (1, 1))
    assert read_training_tail_raw(tmp_path, snapshot=second_snapshot) == "REPLACEMENT\n"


# --------------------------------------------------------------------------- the observer coroutine
class _MonitorStub(TrainingMonitorMixin):
    """Minimal host for the mixin: the coroutine only needs `tracer` + the interval attr."""
    def __init__(self, tracer, interval):
        self.tracer = tracer
        self._train_monitor_interval_s = interval


def test_monitor_cancellation_joins_the_paid_verdict_worker(tmp_path):
    """The monitor may be advisory, but its shared client's cost ledger is not.

    Exercise cancellation exactly while the sync verdict call is blocked. Event handshakes pin that
    point; an abandoned call unwinds the monitor before the timer releases the worker, while an owned
    call keeps the monitor task alive until the worker returns.
    """
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")
    release = threading.Event()
    worker_finished = threading.Event()

    async def drive():
        started = threading.Event()
        tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
        host = _MonitorStub(tracer, interval=0.02)
        host._monitor_cadence = lambda: 0.0

        def _blocking_verdict(digest, context):
            started.set()
            release.wait()
            worker_finished.set()
            return None

        host._training_verdict = _blocking_verdict
        cancel = threading.Event()

        async def _monitor():
            await host._monitor_training(0, 0, str(wd), cancel)

        release_timer = threading.Timer(0.25, release.set)
        timer_started = False
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_monitor)
                await anyio.to_thread.run_sync(started.wait, abandon_on_cancel=True)
                release_timer.start()
                timer_started = True
                tg.cancel_scope.cancel()
            detached = not worker_finished.is_set()
        finally:
            started.set()
            release.set()
            release_timer.cancel()
            if timer_started:
                release_timer.join()
        await anyio.to_thread.run_sync(worker_finished.wait)
        return detached

    detached = anyio.run(drive)
    assert detached is False
    assert worker_finished.is_set()


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
    def __init__(self, tracer, developer, interval=0.05, redact=None, kill=False, kill_confidence=0.8):
        self.tracer = tracer
        self.developer = developer
        self._train_monitor_interval_s = interval
        self._train_monitor_kill = kill
        self._train_monitor_kill_confidence = kill_confidence
        self._write_lock = anyio.Lock()
        self.store = _FakeStore()
        self.kill_signal: dict = {}
        self.cancel = threading.Event()
        if redact is not None:
            self._redact = redact


class _FakeStore:
    def __init__(self):
        self.events = []

    def append(self, event_type, data):
        self.events.append((event_type, data))

    def read_all(self):
        return [Event(seq=index, ts=0.0, type=event_type, data=dict(data))
                for index, (event_type, data) in enumerate(self.events)]


def _run_verdict_monitor(tmp_path, *, workdir, developer, hold_s=0.22, redact=None,
                         kill=False, kill_confidence=0.8, prior_events=()):
    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
    host = _VerdictHost(tracer, developer, interval=0.05, redact=redact,
                        kill=kill, kill_confidence=kill_confidence)
    host.store.events.extend((event_type, dict(data)) for event_type, data in prior_events)

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_training, 0, 0, str(workdir), host.cancel, "ctx", host.kill_signal)
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


def test_healthy_transition_records_explicit_recovery_event(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    logf = wd / "train.log"
    logf.write_text("step 1 loss: nan\n")

    class _RecoveringClient:
        def __init__(self):
            self.calls = 0

        def complete_tool(self, messages, schema):
            self.calls += 1
            if self.calls == 1:
                with open(logf, "a", encoding="utf-8") as fh:
                    fh.write("step 2 loss: 0.4\n")       # make the next digest observable
                return {"status": "broken", "reason": "loss is nan", "confidence": 0.9}
            return {"status": "healthy", "reason": "finite loss is decreasing", "confidence": 0.9}

    host, _spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(_RecoveringClient()), hold_s=0.3)
    alerts = [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT]
    assert [event["status"] for event in alerts[:2]] == ["broken", "healthy"]


def test_resumed_monitor_closes_pre_crash_alert_in_same_generation(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 8 loss: 0.2\n")
    client = _FakeClient({"status": "healthy", "reason": "recovered", "confidence": 0.9})
    host, _spans = _run_verdict_monitor(
        tmp_path,
        workdir=wd,
        developer=_FakeDeveloper(client),
        prior_events=[(EV_TRAIN_MONITOR_ALERT, {
            "node_id": 0, "generation": 0, "status": "broken", "reason": "pre-crash",
            "confidence": 0.9,
        })],
    )
    alerts = [data for event_type, data in host.store.events
              if event_type == EV_TRAIN_MONITOR_ALERT]
    assert [event["status"] for event in alerts] == ["broken", "healthy"]


def test_unchanged_digest_does_not_re_call_the_llm(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")     # static log across every tick
    client = _FakeClient({"status": "healthy", "reason": "ok", "confidence": 0.7})
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), hold_s=0.3)
    # The invariant is "NOT re-called per tick": at most once for an unchanged digest. `<=` (not `==`)
    # so a slow CI runner that fits fewer ticks in the window never flakes; the span proves it did tick.
    assert client.calls <= 1, f"static digest must not re-call the LLM per tick (fired {client.calls})"
    assert [s for s in spans if s.get("name") == "train_monitor"], "monitor should have ticked at least once"


def test_verdict_recheck_after_s_flows_through_the_loop(tmp_path):
    # Phase-2 self-pacing end-to-end: a verdict's `recheck_after_s` (>= base) is honored by the loop and
    # surfaced as the span's `next_check_s`, so the observer really does control the next cadence.
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("epoch 2 val steady\n")
    client = _FakeClient({"status": "watch", "reason": "keeping an eye on it",
                          "confidence": 0.6, "recheck_after_s": 0.2})   # base here is the 0.05 config
    _host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), hold_s=0.3)
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm and any(s["attributes"].get("next_check_s") == 0.2 for s in tm), \
        "the loop must honor the verdict's recheck_after_s and stamp it on the span"


def test_no_client_degrades_to_trace_only_observation(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\n")
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(None))

    assert [e for e in host.store.events if e[0] == EV_TRAIN_MONITOR_ALERT] == []
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm, "still observes (Phase 0 trace) without an LLM client"
    assert "status" not in tm[0]["attributes"]             # no verdict without a client


def test_watch_verdict_alerts_and_confidence_is_clamped(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("epoch 3 val_loss rising\n")
    client = _FakeClient({"status": "watch", "reason": "val loss ticking up", "confidence": 1.7})
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client))

    alerts = [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT]
    assert alerts and alerts[0]["status"] == "watch"       # non-healthy that isn't 'broken' still alerts
    assert alerts[0]["confidence"] == 1.0                  # out-of-range confidence clamped into [0, 1]


@pytest.mark.parametrize(
    "confidence",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_non_finite_broken_confidence_stays_observable_but_cannot_kill(tmp_path, confidence):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: nan\n")
    client = _FakeClient({"status": "broken", "reason": "loss diverged", "confidence": confidence})

    host, spans = _run_verdict_monitor(
        tmp_path,
        workdir=wd,
        developer=_FakeDeveloper(client),
        kill=True,
        kill_confidence=0.0,
    )

    # A zero action threshold proves invalid confidence is rejected by validity, not merely mapped below
    # the usual 0.8 boundary. The diagnostic remains durable and explicitly marks the sanitization.
    assert host.kill_signal.get("kill") is None
    assert not host.cancel.is_set()
    alerts = [data for event_type, data in host.store.events
              if event_type == EV_TRAIN_MONITOR_ALERT]
    assert alerts and alerts[0]["confidence"] == 0.0
    assert alerts[0]["confidence_valid"] is False
    monitor_spans = [span for span in spans if span.get("name") == "train_monitor"]
    assert monitor_spans and monitor_spans[0]["attributes"]["confidence"] == 0.0
    assert monitor_spans[0]["attributes"]["confidence_valid"] is False


def test_reason_is_redacted_before_storage(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: nan\n")
    client = _FakeClient({"status": "broken", "reason": "crashed near SECRET-TOKEN in the log",
                          "confidence": 0.9})
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        redact=lambda s: s.replace("SECRET-TOKEN", "[redacted]"))

    alerts = [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT]
    assert alerts and "SECRET-TOKEN" not in alerts[0]["reason"] and "[redacted]" in alerts[0]["reason"]
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert "SECRET-TOKEN" not in tm[0]["attributes"].get("reason", "")


class _RaisingClient:
    def __init__(self):
        self.calls = 0

    def complete_tool(self, messages, schema):
        self.calls += 1
        raise RuntimeError("endpoint is down")


def test_llm_error_skips_verdict_but_keeps_watching(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")
    client = _RaisingClient()
    host, spans = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), hold_s=0.3)

    assert client.calls >= 1                                # the LLM WAS attempted...
    assert [e for e in host.store.events if e[0] == EV_TRAIN_MONITOR_ALERT] == []   # ...and its failure
    tm = [s for s in spans if s.get("name") == "train_monitor"]                     # never crashed the task
    assert tm and "status" not in tm[0]["attributes"]      # observed (trace) but no verdict this tick


# --------------------------------------------------------------------------- Phase 2: adaptive cadence
def test_next_monitor_sleep_pacing():
    from looplab.engine.train_monitor import _HEALTHY_BACKOFF_K, next_monitor_sleep as nxt
    base = 100.0
    assert nxt(base) == base                               # nothing special -> base
    assert nxt(base, status="watch", healthy_streak=0) == base
    # agent self-pace: honored, but never faster than base (cost bound) and never slower than the cap
    assert nxt(base, recheck_after_s=250) == 250.0
    assert nxt(base, recheck_after_s=10) == base
    assert nxt(base, recheck_after_s=999999) == 3600.0
    assert nxt(base, recheck_after_s=0) == base            # non-positive ignored
    assert nxt(base, recheck_after_s=True) == base         # bool is not a seconds value
    # steadily-healthy -> geometric backoff after K consecutive; recheck still takes precedence
    assert nxt(base, status="healthy", healthy_streak=_HEALTHY_BACKOFF_K - 1) == base
    assert nxt(base, status="healthy", healthy_streak=_HEALTHY_BACKOFF_K) == base * 2
    assert nxt(base, status="healthy", healthy_streak=_HEALTHY_BACKOFF_K + 1) == base * 4
    assert nxt(base, status="healthy", healthy_streak=99, recheck_after_s=150) == 150.0


class _CadenceHost(TrainingMonitorMixin):
    def __init__(self, cfg, budget):
        self._train_monitor_interval_s = cfg
        self._budget = budget

    def _experiment_time_budget(self):
        return self._budget


# --------------------------------------------------------------------------- Phase 3: gated early kill
def test_should_monitor_kill_decision():
    from looplab.engine.train_monitor import TrainingVerdict, should_monitor_kill as kill
    broken = TrainingVerdict(status="broken", reason="loss nan", confidence=0.9)
    watch = TrainingVerdict(status="watch", reason="slow", confidence=0.99)
    healthy = TrainingVerdict(status="healthy", reason="ok", confidence=0.99)
    assert kill(broken, enabled=True, threshold=0.8) is True
    assert kill(TrainingVerdict(status="broken", reason="boundary", confidence=0.8),
                enabled=True, threshold=0.8) is True                 # inclusive configured boundary
    assert kill(TrainingVerdict(status="broken", reason="below", confidence=0.799999),
                enabled=True, threshold=0.8) is False
    assert kill(broken, enabled=False, threshold=0.8) is False        # opt-in: off by default
    assert kill(broken, enabled=True, threshold=0.95) is False        # below the confidence bar
    assert kill(watch, enabled=True, threshold=0.5) is False          # a plateau/'watch' is never killed
    assert kill(healthy, enabled=True, threshold=0.5) is False
    assert kill(None, enabled=True, threshold=0.5) is False


@pytest.mark.parametrize(
    "confidence",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_should_monitor_kill_rejects_non_finite_confidence_at_zero_threshold(confidence):
    from looplab.engine.train_monitor import TrainingVerdict, should_monitor_kill

    verdict = TrainingVerdict(status="broken", reason="invalid model confidence", confidence=confidence)
    assert should_monitor_kill(verdict, enabled=True, threshold=0.0) is False


def test_watchdog_kill_claim_is_first_writer_wins():
    signal: dict = {}
    cancel = threading.Event()

    assert claim_watchdog_kill(
        signal, cancel, reason="loss became NaN", terminal_reason="monitor_broken",
        confidence=0.97) is True
    assert claim_watchdog_kill(
        signal, cancel, reason="below sibling bar", terminal_reason="asha_underperforming") is False

    assert signal == {
        "kill": True,
        "reason": "loss became NaN",
        "terminal_reason": "monitor_broken",
        "confidence": 0.97,
    }
    assert cancel.is_set()


def test_broken_verdict_fires_kill_when_enabled(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("Using device: cpu\nRuntimeError in dataloader\nloss: nan\n")
    client = _FakeClient({"status": "broken", "reason": "silent CPU fallback + nan loss",
                          "confidence": 0.95})
    host, _ = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=True)

    assert host.kill_signal.get("kill") is True                       # kill decision recorded for _evaluate
    assert "CPU" in host.kill_signal.get("reason", "")
    assert host.kill_signal.get("terminal_reason") == "monitor_broken"
    assert host.cancel.is_set()                                       # the eval's tree-kill was triggered
    # the advisory alert was still recorded before the kill
    assert any(t == EV_TRAIN_MONITOR_ALERT for (t, _d) in host.store.events)


def test_broken_verdict_does_not_kill_when_disabled(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: nan\n")
    client = _FakeClient({"status": "broken", "reason": "nan", "confidence": 0.99})
    host, _ = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=False)

    assert host.kill_signal.get("kill") is None and not host.cancel.is_set()   # observe-only default
    assert any(t == EV_TRAIN_MONITOR_ALERT for (t, _d) in host.store.events)    # but still flagged


def test_llm_call_cap_stops_spending_but_keeps_observing(tmp_path, monkeypatch):
    # Past the per-node LLM-call backstop the monitor keeps OBSERVING (trace) but stops calling the LLM,
    # and marks the span `llm_capped` so the cap is never silent. Drive it by mutating the log each call.
    import looplab.engine.train_monitor as tm
    monkeypatch.setattr(tm, "_MAX_MONITOR_LLM_CALLS", 2)
    wd = tmp_path / "node_0"
    wd.mkdir()
    logf = wd / "train.log"
    logf.write_text("step 0 loss: 1.0\n")

    class _AppendingClient:
        def __init__(self):
            self.calls = 0

        def complete_tool(self, messages, schema):
            self.calls += 1
            with open(logf, "a", encoding="utf-8") as fh:   # change the log so the NEXT tick has a fresh digest
                fh.write(f"step {self.calls} loss: {1.0 / (self.calls + 1):.3f}\n")
            return {"status": "healthy", "reason": "ok", "confidence": 0.6}

    client = _AppendingClient()
    _host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), hold_s=0.5)
    assert client.calls <= 2, f"LLM must never EXCEED the cap (fired {client.calls})"   # `<=` = robust
    tm_spans = [s for s in spans if s.get("name") == "train_monitor"]
    assert any(s["attributes"].get("llm_capped") for s in tm_spans)   # cap was REACHED + surfaced (not silent)


def test_monitor_cadence_derives_from_budget():
    # ~10% of the per-experiment budget, clamped [30s, 30min], then capped by the config interval.
    assert _CadenceHost(600.0, 300.0)._monitor_cadence() == 30.0      # 10% of 300 = 30 (< config)
    assert _CadenceHost(600.0, 1800.0)._monitor_cadence() == 180.0    # 10% of 1800 = 180
    assert _CadenceHost(600.0, 18000.0)._monitor_cadence() == 600.0   # 10% = 1800 but config 600 caps it
    assert _CadenceHost(600.0, 60.0)._monitor_cadence() == 30.0       # floored at 30s
    assert _CadenceHost(600.0, None)._monitor_cadence() == 600.0      # no budget -> config
    assert _CadenceHost(600.0, 0)._monitor_cadence() == 600.0         # non-positive budget -> config
