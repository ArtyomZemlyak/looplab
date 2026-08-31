"""Training-log monitor contracts: bounded digest/observation, diagnostic verdicts, adaptive cadence,
and the separately opt-in early-kill path. Diagnostic events remain fold-ignored; only an enabled,
confident `broken` intervention changes the node lifecycle through the ordinary terminal contract."""
from __future__ import annotations

import json
import threading
import time

import anyio
import pytest

from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.core.models import Event
from looplab.engine import train_monitor as _tm
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

        # This stub stands in for the real `_training_verdict`, so its parameters ARE that
        # contract: the loop calls it POSITIONALLY through `to_thread.run_sync`, and a stub one
        # parameter short raises `TypeError` inside the loop's own per-tick `except Exception:
        # continue` — the monitor then spins forever, `started` is never set, and this test hangs
        # rather than failing. That is what adding `trajectory_text` did, and then `tools` did it
        # AGAIN — the guard in `test_train_monitor_trajectory.py` bound the loop to the real
        # signature and never looked at the stubs, so it stayed green through both. It now derives
        # this stub too. Keep it in step with the signature.
        def _blocking_verdict(digest, context, stage_context="", trajectory_text="", tools=None,
                              contract_text=""):
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


def test_building_the_log_tools_costs_one_source_derivation_and_not_two(tmp_path):
    """`_log_query_tools` ran `monitor_log_sources` once just to decide whether there was anything
    to hand back, and then handed the SAME derivation in as a callable that ran it again on the
    provider's first read. That derivation globs the workdir and opens + fstats every stage log,
    with `attempt_byte_floor` probe-READING each one — so on the geesefs/S3 mounts this repo
    documents it is the most expensive thing a judge tick does, and it was being done twice before
    the model had asked a single question.

    The probe's own answer serves the FIRST read and nothing else: a later tool call must still
    re-derive, because a new stage log appearing mid-eval is exactly what the callable is for."""
    import looplab.engine.train_monitor as _tm_mod

    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("\r1/2 [0:00:01<0:00:01] loss: 1.0\n")
    plan = _tm_mod.EvalLogPlan(roles={_tm_mod._log_name_key("train.log"):
                                      ("train", _tm_mod.LOG_ROLE_TRAINING)})
    derivations = []
    real = _tm_mod.monitor_log_sources

    def counting(workdir, plan=None, snapshot=None):
        derivations.append(1)
        return real(workdir, plan, snapshot)

    _tm_mod.monitor_log_sources = counting
    try:
        tools = _tm_mod._log_query_tools(wd, plan, None)
        assert tools is not None
        assert len(derivations) == 1                       # the existence probe
        assert "loss: 1.0" in tools.execute("read_log", {"mode": "tail"})
        assert len(derivations) == 1, "the probe's own answer was thrown away"
        # ...and the NEXT call sees the filesystem again, which is the whole point of the callable.
        (wd / "score.log").write_text("scoring\n")
        tools.execute("read_log", {"mode": "tail"})
        assert len(derivations) == 2
    finally:
        _tm_mod.monitor_log_sources = real


def test_the_log_tools_derivation_happens_off_the_event_loop_thread(tmp_path):
    """`monitor_log_tools` is FILESYSTEM work — it globs the workdir for `*.log`, opens + fstats
    every stage log the plan names, and `attempt_byte_floor` probe-READS each one. It was passed as
    an ARGUMENT to `anyio.to_thread.run_sync`, which means Python evaluated it on the EVENT-LOOP
    thread before the hand-off ever happened, once per tick per running eval. Every other filesystem
    touch in this loop is deliberately handed to a worker (`_observe_log`), and on the geesefs/S3
    mounts this repo documents a single stat of an absent file costs 105-950 ms — so this one
    blocked the whole engine loop while looking like a plain argument.

    Driven by IDENTITY, not by source shape: record the thread the derivation runs on and the thread
    the loop itself runs on, and assert they differ.
    """
    import looplab.engine.train_monitor as _tm_mod

    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\n")
    derived_on: list = []
    real = _tm_mod.monitor_log_tools

    def _recording(engine, workdir, log_plan=None, log_snapshot=None):
        derived_on.append(threading.get_ident())
        return real(engine, workdir, log_plan, log_snapshot)

    async def drive():
        tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
        host = _MonitorStub(tracer, interval=0.02)
        host._monitor_cadence = lambda: 0.0
        host._train_monitor_tools = True
        judged = threading.Event()

        def _verdict(digest, context, stage_context="", trajectory_text="", tools=None,
                     contract_text=""):
            judged.set()
            return None

        host._training_verdict = _verdict
        cancel = threading.Event()
        loop_thread = threading.get_ident()
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_training, 0, 0, str(wd), cancel)
            await anyio.to_thread.run_sync(judged.wait, abandon_on_cancel=True)
            tg.cancel_scope.cancel()
        return loop_thread

    original = _tm_mod.monitor_log_tools
    _tm_mod.monitor_log_tools = _recording
    try:
        loop_thread = anyio.run(drive)
    finally:
        _tm_mod.monitor_log_tools = original
    assert derived_on, "the judge never got its tools built at all"
    assert loop_thread not in derived_on, (
        "the log-source derivation ran on the event-loop thread — it is an argument evaluated "
        "before the worker hand-off again")


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


# Wall-clock ceiling for "the monitor produced what this test is waiting for". It bounds only the
# FAILURE case: with `until` the loop cancels the instant the predicate holds, so a passing test never
# waits it out. `hold_s` alone is a FIXED window (0.22s at a 0.05s cadence), which a loaded full-suite
# host can miss entirely — the monitor then emitted nothing and the test read an empty list while
# passing in isolation. Tests asserting an artifact is ABSENT must still wait the fixed window; only a
# test waiting FOR something can poll.
_MONITOR_SETTLE_TIMEOUT_S = 15.0


# The stage plan these workdirs correspond to: they all write `train.log`. Kill authority requires a
# log the plan can PROVE is the run's own training, which `eval_log_plan` grants only to a log that is
# the WHOLE eval — the single-command `eval.log`, or (as here) a ONE-stage pipeline. So any test
# asserting about the kill must say which pipeline it is running: a bare `*.log` could be a scorer's,
# a pip install's, or some intermediate `data_prep` stage's.
_TRAIN_PLAN = _tm.eval_log_plan([{"name": "train", "command": ["python", "train.py"]}])
# The MULTI-stage shape (the Developer manifest plus the engine's always-appended protected scorer).
# `train.log` here is `LOG_ROLE_WORK`: read and judged exactly as before, but ADVISORY — nothing in the
# resolved pipeline proves which of its work stages is the training step. See `eval_log_plan`.
_PIPELINE_PLAN = _tm.eval_log_plan([{"name": "data_prep", "command": ["python", "prep.py"]},
                                    {"name": "train", "command": ["python", "train.py"]},
                                    {"name": "score", "command": ["python", "score.py"]}])


def _run_verdict_monitor(tmp_path, *, workdir, developer, hold_s=0.22, redact=None,
                         kill=False, kill_confidence=0.8, prior_events=(), until=None,
                         after_until_s=0.0, plan=None):
    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))
    host = _VerdictHost(tracer, developer, interval=0.05, redact=redact,
                        kill=kill, kill_confidence=kill_confidence)
    host.store.events.extend((event_type, dict(data)) for event_type, data in prior_events)

    async def drive():
        async with anyio.create_task_group() as tg:
            tg.start_soon(host._monitor_training, 0, 0, str(workdir), host.cancel, "ctx",
                          host.kill_signal, None, plan)
            if until is None:
                await anyio.sleep(hold_s)
            else:
                deadline = time.monotonic() + _MONITOR_SETTLE_TIMEOUT_S
                while not until(host) and time.monotonic() < deadline:
                    await anyio.sleep(0.005)
                if until(host) and after_until_s > 0:
                    await anyio.sleep(after_until_s)
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
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events))

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
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda _h: (tmp_path / "spans.jsonl").exists())

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

    # Two ticks are needed (broken, then the recovery). A FIXED 0.3s window at a 0.05s cadence is
    # not enough on a loaded host — wait for the pair instead of for the clock.
    host, _spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(_RecoveringClient()), hold_s=0.3,
        until=lambda h: sum(1 for (t, _d) in h.store.events if t == EV_TRAIN_MONITOR_ALERT) >= 2)
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
        until=lambda h: sum(
            1 for event_type, _data in h.store.events
            if event_type == EV_TRAIN_MONITOR_ALERT) >= 2,
    )
    alerts = [data for event_type, data in host.store.events
              if event_type == EV_TRAIN_MONITOR_ALERT]
    assert [event["status"] for event in alerts] == ["broken", "healthy"]


def test_unchanged_digest_does_not_re_call_the_llm(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")     # static log across every tick
    client = _FakeClient({"status": "healthy", "reason": "ok", "confidence": 0.7})
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda _h: (tmp_path / "spans.jsonl").exists(), after_until_s=0.3)
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
    _host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events),
    )
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm and any(s["attributes"].get("next_check_s") == 0.2 for s in tm), \
        "the loop must honor the verdict's recheck_after_s and stamp it on the span"


def test_no_client_degrades_to_trace_only_observation(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\n")
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(None),
        until=lambda _h: (tmp_path / "spans.jsonl").exists())

    assert [e for e in host.store.events if e[0] == EV_TRAIN_MONITOR_ALERT] == []
    tm = [s for s in spans if s.get("name") == "train_monitor"]
    assert tm, "still observes (Phase 0 trace) without an LLM client"
    assert "status" not in tm[0]["attributes"]             # no verdict without a client


def test_watch_verdict_alerts_and_confidence_is_clamped(tmp_path):
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("epoch 3 val_loss rising\n")
    client = _FakeClient({"status": "watch", "reason": "val loss ticking up", "confidence": 1.7})
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events))

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
        plan=_TRAIN_PLAN,          # an identified training stage: nothing but the confidence blocks it
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events),
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
        redact=lambda s: s.replace("SECRET-TOKEN", "[redacted]"),
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events))

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
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda _h: (tmp_path / "spans.jsonl").exists())

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
    from looplab.engine.train_monitor import (
        LOG_ROLE_SCORE, LOG_ROLE_SETUP, LOG_ROLE_TRAINING, LOG_ROLE_UNKNOWN,
        TrainingVerdict, should_monitor_kill)

    def kill(verdict, **kw):
        # The verdict-quality half of the gate. The stage-scope and confirmation halves are held at
        # their ACTING values here and exercised on their own below, so each conjunct has its own case.
        kw.setdefault("log_role", LOG_ROLE_TRAINING)
        kw.setdefault("broken_streak", 2)
        return should_monitor_kill(verdict, **kw)

    broken = TrainingVerdict(status="broken", reason="loss nan", confidence=0.9)
    watch = TrainingVerdict(status="watch", reason="slow", confidence=0.99)
    healthy = TrainingVerdict(status="healthy", reason="ok", confidence=0.99)
    assert kill(broken, enabled=True, threshold=0.8) is True
    assert kill(TrainingVerdict(status="broken", reason="boundary", confidence=0.8),
                enabled=True, threshold=0.8) is True                 # inclusive configured boundary
    assert kill(TrainingVerdict(status="broken", reason="below", confidence=0.799999),
                enabled=True, threshold=0.8) is False
    assert kill(broken, enabled=False, threshold=0.8) is False        # opt-in
    assert kill(broken, enabled=True, threshold=0.95) is False        # below the confidence bar
    assert kill(watch, enabled=True, threshold=0.5) is False          # a plateau/'watch' is never killed
    assert kill(healthy, enabled=True, threshold=0.5) is False
    assert kill(None, enabled=True, threshold=0.5) is False

    # STAGE SCOPE: the same confident verdict may only act about an identified TRAINING stage. The
    # monitor lives across setup -> stages -> the always-appended `score` stage, so a verdict formed
    # about any other phase — or about a log nothing could attribute — is evidence, never authority.
    for role in (LOG_ROLE_UNKNOWN, LOG_ROLE_SETUP, LOG_ROLE_SCORE):
        assert should_monitor_kill(broken, enabled=True, threshold=0.8,
                                   log_role=role, broken_streak=9) is False
    # ... and the default is the fail-closed one, so a caller that cannot say cannot kill.
    assert should_monitor_kill(broken, enabled=True, threshold=0.8, broken_streak=9) is False

    # CONFIRMATION: one confident tick arms, the second acts.
    assert should_monitor_kill(broken, enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_TRAINING, broken_streak=1) is False
    assert should_monitor_kill(broken, enabled=True, threshold=0.8,
                               log_role=LOG_ROLE_TRAINING, broken_streak=2) is True
    # A caller cannot disable the requirement by asking for zero/negative confirmations.
    for degenerate in (0, -1, "two", None):
        assert should_monitor_kill(broken, enabled=True, threshold=0.8, log_role=LOG_ROLE_TRAINING,
                                   broken_streak=0, confirm_ticks=degenerate) is False


@pytest.mark.parametrize(
    "confidence",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_should_monitor_kill_rejects_non_finite_confidence_at_zero_threshold(confidence):
    from looplab.engine.train_monitor import (
        LOG_ROLE_TRAINING, TrainingVerdict, should_monitor_kill)

    verdict = TrainingVerdict(status="broken", reason="invalid model confidence", confidence=confidence)
    # Every OTHER conjunct is held open (identified training stage, confirmed streak, zero bar) so the
    # False can only come from the confidence being INVALID — the property this test guards.
    assert should_monitor_kill(verdict, enabled=True, threshold=0.0,
                               log_role=LOG_ROLE_TRAINING, broken_streak=2) is False


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
    # `plan` names the pipeline this workdir belongs to, so `train.log` is an identified TRAINING stage
    # (without it the verdict is advisory — see test_watchdog_stage_scope.py). The kill needs the
    # verdict CONFIRMED, so it lands on the second consecutive broken tick, not the first.
    host, _ = _run_verdict_monitor(tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=True,
                                   plan=_TRAIN_PLAN,
                                   until=lambda h: h.kill_signal.get("kill"))

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
    host, _ = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=False,
        plan=_TRAIN_PLAN,  # only the opt-in is missing
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events))

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
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda _h: (
            (tmp_path / "spans.jsonl").exists()
            and '"llm_capped":true' in (tmp_path / "spans.jsonl").read_text()))
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


# --- one lifecycle scan for both watchdogs (doc 25 EC-04) ---------------------------------------
#
# Three sites hand-rolled "the newest row of this diagnostic type for exactly this (node, generation)",
# with the bool-guarded field validation copied verbatim. These pin the guard, and the property the
# tick-loop scaffold would have enforced had it been extractable.

def _row(event_type, **data):
    return Event(seq=0, ts=0.0, type=event_type, data=data)


def test_the_lifecycle_scan_rejects_a_bool_node_id_masquerading_as_an_int():
    """`isinstance(True, int)` is True in Python, so an untrusted row carrying `node_id: true` matches
    a plain `== node_id` test against node 1 — and hands a watchdog another lifecycle's history as if
    it were its own. Same trap on `generation`."""
    from looplab.engine.train_monitor import last_lifecycle_row

    rows = [
        _row("t", node_id=True, generation=1, status="broken"),
        _row("t", node_id=1, generation=True, status="broken"),
    ]
    assert last_lifecycle_row(rows, "t", 1, 1) is None
    rows.append(_row("t", node_id=1, generation=1, status="watch"))
    assert last_lifecycle_row(rows, "t", 1, 1) == {
        "node_id": 1, "generation": 1, "status": "watch"}


def test_the_lifecycle_scan_returns_the_newest_match_even_when_unusable():
    """It must not keep scanning backwards past a bad newest row into an OLDER one: that would answer
    a resuming watchdog with a stale verdict it has already moved past. Callers turn an unreadable
    payload into "no history"; the scan's job is only to find the right ROW."""
    from looplab.engine.train_monitor import last_lifecycle_row

    rows = [
        _row("t", node_id=1, generation=0, status="broken"),
        _row("t", node_id=1, generation=0, status="nonsense"),
    ]
    assert last_lifecycle_row(rows, "t", 1, 0) == {
        "node_id": 1, "generation": 0, "status": "nonsense"}


def test_the_lifecycle_scan_ignores_other_types_generations_and_empty_logs():
    from looplab.engine.train_monitor import last_lifecycle_row

    rows = [
        _row("other", node_id=1, generation=0, status="broken"),
        _row("t", node_id=2, generation=0, status="broken"),
        _row("t", node_id=1, generation=9, status="broken"),
        _row("t", generation=0, status="broken"),               # no node_id at all
    ]
    assert last_lifecycle_row(rows, "t", 1, 0) is None
    assert last_lifecycle_row(None, "t", 1, 0) is None
    assert last_lifecycle_row([], "t", 1, 0) is None


def _lifecycle_scan_sites():
    """The three sites that must not own a lifecycle scan: both resume recoveries and the judge's
    health lookup."""
    from looplab.engine import asha_monitor as asha
    from looplab.engine import train_monitor as train

    return {
        "train resume": train.TrainingMonitorMixin._monitor_training,
        "asha resume": asha.AshaMonitorMixin._monitor_asha,
        "latest_train_verdict": asha.latest_train_verdict,
    }


def test_all_three_lifecycle_scans_go_through_the_shared_helper():
    """Both resume recoveries and `asha_monitor.latest_train_verdict`. A site that re-grows the
    reversed scan gets its own copy of the bool guard, which is the half that drifts.

    Asserted on the AST, not on substrings: a mention of `last_lifecycle_row(` in a COMMENT satisfies
    a substring test, and `[::-1]` re-grows the reverse scan without ever spelling `reversed(`. Both
    together are a site that hand-rolls the scan while every textual check stays green.
    """
    import ast
    import inspect
    import textwrap

    for name, fn in _lifecycle_scan_sites().items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "last_lifecycle_row"]
        assert calls, f"{name} no longer CALLS the shared scan (a mention is not a call)"

        for node in ast.walk(tree):
            # `reversed(rows)` / `rows.reverse()` / `sorted(..., reverse=True)`
            if isinstance(node, ast.Call):
                callee = (node.func.id if isinstance(node.func, ast.Name)
                          else getattr(node.func, "attr", ""))
                assert callee not in ("reversed", "reverse"), (
                    f"{name} re-grew its own reverse scan (`{callee}(...)`)")
                assert not any(kw.arg == "reverse" for kw in node.keywords), (
                    f"{name} re-grew its own reverse scan (`reverse=` sort)")
            # `rows[::-1]`
            if isinstance(node, ast.Slice) and isinstance(node.step, ast.UnaryOp) \
                    and isinstance(node.step.op, ast.USub):
                assert False, f"{name} re-grew its own reverse scan (`[::-1]`)"


def test_the_train_resume_recovery_really_calls_the_shared_scan(tmp_path, monkeypatch):
    """And behaviourally, so the structural test above is not the only thing holding it: the running
    monitor's resume recovery goes through the helper for exactly its own `(type, node, generation)`.

    A re-inlined scan keeps the module import and every source mention while never invoking it.
    """
    import looplab.engine.train_monitor as train

    calls: list[tuple] = []
    real = train.last_lifecycle_row

    def spy(rows, event_type, node_id, generation):
        calls.append((event_type, node_id, generation))
        return real(rows, event_type, node_id, generation)

    monkeypatch.setattr(train, "last_lifecycle_row", spy)

    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\n")
    client = _FakeClient({"status": "healthy", "reason": "fine", "confidence": 0.9})
    _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        prior_events=[(EV_TRAIN_MONITOR_ALERT,
                       {"node_id": 0, "generation": 0, "status": "watch"})],
        until=lambda _h: bool(calls))

    assert (EV_TRAIN_MONITOR_ALERT, 0, 0) in calls, (
        f"the train resume recovery never asked the shared scan: {calls}")


def test_the_judges_health_lookup_really_calls_the_shared_scan(monkeypatch):
    """`latest_train_verdict` reads UNTRUSTED diagnostic rows; the lifecycle half of that read is the
    shared scan, and a bool `node_id` must not hand the judge another lifecycle's verdict."""
    import looplab.engine.asha_monitor as asha
    import looplab.engine.train_monitor as train

    calls: list[tuple] = []
    real = train.last_lifecycle_row

    def spy(rows, event_type, node_id, generation):
        calls.append((event_type, node_id, generation))
        return real(rows, event_type, node_id, generation)

    monkeypatch.setattr(train, "last_lifecycle_row", spy)

    rows = [_row(EV_TRAIN_MONITOR_ALERT, node_id=True, generation=1,
                 status="broken", reason="another lifecycle's crash")]
    assert asha.latest_train_verdict(rows, 1, 1) is None
    assert calls == [(EV_TRAIN_MONITOR_ALERT, 1, 1)], calls


def test_every_watchdog_tick_loop_reraises_cancellation_before_swallowing():
    """The scaffold both loops share is 7 lines around 60-160 lines of unrelated body, so they stay
    separate — but the ONE line that must not be dropped is guarded here.

    Each loop ends in a blanket `except Exception: continue`, so a transient disk/LLM/tracer hiccup
    skips a tick instead of disabling the watcher for the rest of a long eval. anyio's cancellation
    is delivered as an exception, so without an earlier `except anyio.get_cancelled_exc_class(): raise`
    that blanket clause SWALLOWS the cancel: the eval finishes and the watchdog keeps looping against
    a dead node, holding the task group open."""
    import ast
    import inspect
    import textwrap

    from looplab.engine import asha_monitor as asha
    from looplab.engine import train_monitor as train

    for name, fn in {"train": train.TrainingMonitorMixin._monitor_training,
                     "asha": asha.AshaMonitorMixin._monitor_asha}.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        loops = [node for node in ast.walk(tree) if isinstance(node, ast.While)]
        assert loops, f"{name} has no tick loop"
        guarded = False
        for handler in (h for loop in loops for node in ast.walk(loop)
                        if isinstance(node, ast.Try) for h in node.handlers):
            names = ast.dump(handler.type) if handler.type is not None else ""
            if "get_cancelled_exc_class" in names:
                assert any(isinstance(stmt, ast.Raise) for stmt in handler.body), (
                    f"{name} catches cancellation but does not re-raise it")
                guarded = True
                break
            # A blanket clause reached FIRST would already have swallowed the cancel.
            assert "Exception" not in names, (
                f"{name}'s tick loop swallows Exception before re-raising cancellation")
        assert guarded, f"{name}'s tick loop never re-raises cancellation"


# ------------------------------- the CONFIRMATION ARM asks the gates, instead of re-listing them

def _armed(spans):
    return [s for s in spans if s.get("name") == "train_monitor"
            and s["attributes"].get("kill_armed")]


def test_a_work_stage_repair_stop_gets_its_prompt_confirmation_look(tmp_path):
    """The arm is what makes the two-tick requirement cheap, and until 2026-08-20 it was gated on
    `_KILL_ELIGIBLE_ROLES` — a list written when a KILL was the only thing a confirmation could
    reach. `should_monitor_repair` then opened the repair-stop to every judged role, so on exactly
    the roles it was opened for the second look cost a full cadence instead of
    `_MONITOR_CONFIRM_DELAY_S`, and on a log that diverged and then went silent it never arrived at
    all (`unchanged and armed_at is None` continues, and only an arm bypasses that gate).

    Driven through the real loop over a WORK-role stage, which is what every multi-stage pipeline
    this engine runs resolves to.
    """
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: 8.8534\nloss: 8.8534\nloss: 8.8534\n")
    client = _FakeClient({"status": "broken", "fault": "implementation", "confidence": 0.95,
                          "reason": "the objective as written cannot descend"})
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=True,
        plan=_PIPELINE_PLAN,                 # train.log is LOG_ROLE_WORK here, not TRAINING
        until=lambda h: h.kill_signal.get("kill"))

    ticks = [t for t in spans if t.get("name") == "train_monitor"]
    assert ticks and ticks[0]["attributes"].get("status") == "broken"   # the denominator, first
    assert _armed(spans), "a repair-stop one confirmation away must arm the prompt re-look"
    assert host.kill_signal.get("terminal_reason") == _tm.MONITOR_REPAIR_REASON
    # ...and the role is still what keeps the TERMINAL kill off this stage.
    assert host.kill_signal.get("terminal_reason") != "monitor_broken"


def test_a_verdict_the_gate_will_refuse_on_confidence_does_not_arm(tmp_path):
    """The other direction, and it is a tightening the old hand-written list did not have: it
    checked the role and the trajectory and never the CONFIDENCE bar, so a `broken` at 0.3 on a
    training stage armed, bought a re-look at 30 s and bypassed the changed-digest gate for a kill
    `should_monitor_kill` refuses anyway. Asking the real predicates cannot drift from them."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("loss: nan\n")
    client = _FakeClient({"status": "broken", "reason": "possibly nan", "confidence": 0.3})
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=True, plan=_TRAIN_PLAN,
        until=lambda _h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in _h.store.events),
        after_until_s=0.15)

    # The DENOMINATOR first: `not _armed([])` is true for a monitor that never ran at all, which is
    # exactly the vacuous shape this suite has been bitten by three times in one day. Prove the tick
    # happened and produced the `broken` verdict before reading anything off its absence.
    ticks = [t for t in spans if t.get("name") == "train_monitor"]
    assert ticks and ticks[0]["attributes"].get("status") == "broken"
    assert not _armed(spans), "a confirmation the gate will refuse is a billable re-ask for nothing"
    assert host.kill_signal.get("kill") is None


def test_the_arming_question_is_the_two_gates_with_the_streak_satisfied():
    """`_confirmation_would_act` grants no authority of its own — it is the same counterfactual
    shape as the `role_withheld` / `trajectory_veto` receipts beside it, and its truth table is
    exactly the disjunction of the two predicates it asks."""
    from looplab.engine.train_monitor import (
        LOG_ROLE_TRAINING, LOG_ROLE_WORK, TrainingVerdict, _confirmation_would_act)

    bug = TrainingVerdict(status="broken", fault="implementation", reason="r", confidence=0.9)
    idea = TrainingVerdict(status="broken", fault="hypothesis", reason="r", confidence=0.9)
    ask = dict(enabled=True, threshold=0.8)
    # a named bug is repairable on ANY judged role, so it arms on a work stage...
    assert _confirmation_would_act(bug, log_role=LOG_ROLE_WORK, **ask) is True
    # ...while a verdict about the IDEA can only ever reach the terminal kill, which needs the role.
    assert _confirmation_would_act(idea, log_role=LOG_ROLE_WORK, **ask) is False
    assert _confirmation_would_act(idea, log_role=LOG_ROLE_TRAINING, **ask) is True
    # every fail-closed conjunct of the two gates still binds through it
    assert _confirmation_would_act(bug, log_role=LOG_ROLE_TRAINING, enabled=False,
                                   threshold=0.8) is False
    assert _confirmation_would_act(bug, log_role=LOG_ROLE_TRAINING, enabled=True,
                                   threshold=0.95) is False
    assert _confirmation_would_act(None, log_role=LOG_ROLE_TRAINING, **ask) is False


def test_a_HEALTHY_stage_that_cannot_MEET_ITS_WALL_still_records(tmp_path, monkeypatch):
    """THE CLOCK IS NOT A HEALTH VERDICT — v11 node 2, 10.0 GPU-hours.

    `stamp_projected_overrun` used to be called INSIDE the write branch, so a stage whose measured
    ETA cannot fit its own declared wall recorded that only when the judge ALSO had a health concern.
    A run training perfectly is exactly the case where it does not, and that is the case that costs:
    `e5small-dr-unified-v11` node 2 was judged healthy on every tick that emitted (correctly — loss
    41.46 -> 23.62, descending), so every row was suppressed, and its train stage was SIGKILLed by
    its own 36000 s wall at step 1764/2109 (84 %, 9h56m21s), then charged a full retrain. Node 3 of
    the same run drew a `watch` early, so its gate was open and all 12 of its rows carry
    `projected_overrun_s` — the signal was never missing, it was conditional on an unrelated fact.

    The projection is patched rather than provoked: what changed is the GATE, and building a real
    trajectory whose ETA overruns a real wall would test the tracker instead. `trajectory_row` is
    patched for the same reason — the gate's precondition must hold deterministically, not depend on
    what a three-line log yields.

    Mutation: drop `_wall_unreachable` from the gate (or move the stamp back inside the branch) and
    this row disappears while every other monitor test stays green — which is exactly how it shipped.
    """
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\nstep 3 loss: 0.3\n")

    def _doomed(alert, trajectory, resolved, log_plan, *, grace_cap=None):
        alert["projected_overrun_s"] = 4700.0
        alert["stage_wall_s"] = 36000.0
        alert["overrun_beyond_grace_s"] = 2900.0
        alert["stage_grace_s"] = 1800.0

    monkeypatch.setattr(_tm, "stamp_projected_overrun", _doomed)
    monkeypatch.setattr(_tm, "trajectory_row", lambda _t: {"direction": "descending"})

    client = _FakeClient({"status": "healthy", "reason": "loss steadily decreasing",
                          "confidence": 0.8})
    host, _spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client),
        until=lambda h: any(t == EV_TRAIN_MONITOR_ALERT for t, _d in h.store.events))

    alerts = [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT]
    assert alerts, ("a stage that cannot finish inside its own declared wall must record that even "
                    "when the training itself is healthy — this is the row whose absence let 10 "
                    "GPU-hours run to a SIGKILL at 84 %")
    assert alerts[0]["status"] == "healthy", (
        "and it must say the training was HEALTHY: the row is about the CLOCK, and relabelling it "
        "as a health concern would be a false claim about the loss")
    assert alerts[0]["overrun_beyond_grace_s"] == 2900.0, (
        "the projection must ride the row it opened — mutation: open the gate but write the alert "
        "without absorbing the fields, and the operator gets a bare healthy row with no reason")


def test_a_healthy_stage_that_FITS_its_wall_still_writes_nothing(tmp_path, monkeypatch):
    """The complement, and the thing the fix must not break: only an overrun BEYOND THE GRACE opens
    the gate. A 40-second overrun on a ten-hour stage is the noise the grace bar exists to swallow.

    Mutation: gate on `projected_overrun_s` instead of `overrun_beyond_grace_s` and every healthy
    tick of every long stage starts writing rows — a second, louder spelling of the noise the bar
    was built to stop."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    (wd / "train.log").write_text("step 1 loss: 0.5\nstep 2 loss: 0.4\nstep 3 loss: 0.3\n")

    def _absorbed(alert, trajectory, resolved, log_plan, *, grace_cap=None):
        alert["projected_overrun_s"] = 40.0        # real, and inside the grace
        alert["stage_wall_s"] = 36000.0            # no `overrun_beyond_grace_s`

    monkeypatch.setattr(_tm, "stamp_projected_overrun", _absorbed)
    monkeypatch.setattr(_tm, "trajectory_row", lambda _t: {"direction": "descending"})

    client = _FakeClient({"status": "healthy", "reason": "loss steadily decreasing",
                          "confidence": 0.8})
    # A FIXED WINDOW, and a PRECONDITION that a verdict really was processed. The first cut of this
    # test used `until=spans.jsonl exists` and was VACUOUS: the file appears as soon as any span
    # closes, so the loop could be cancelled before a single verdict was judged, and "no alert rows"
    # was true because nothing had happened. The mutation run said so — gating on the raw
    # `projected_overrun_s` instead of the beyond-grace key SURVIVED. An absence assertion has to
    # wait the window out (see `_MONITOR_SETTLE_TIMEOUT_S`'s note) and prove the tick occurred.
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), hold_s=0.6)

    judged = [s for s in spans
              if s.get("name") == "train_monitor" and s.get("attributes", {}).get("status")]
    assert judged, "precondition: the monitor actually produced a verdict to be gated"
    assert judged[0]["attributes"]["status"] == "healthy"
    assert [d for (t, d) in host.store.events if t == EV_TRAIN_MONITOR_ALERT] == [], (
        "an overrun the grace absorbs must stay trace-only, exactly as before")
