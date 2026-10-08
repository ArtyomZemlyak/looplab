"""`LOOPLAB_UI_AUTO_RESUME=1`: a server restart resumes the runs a dead engine left in progress.

Incident 2026-10-06: a container restart killed every engine, and each run waited — engine stopped,
GPUs idle — until a human pressed resume. Off by default; a paused run (an operator's pause, or the
engine's own `infra_unavailable` over a box the operator must fix) is never auto-resumed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.engine import run_lifecycle  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve import engine_proc as ep  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _run(root, name="run", *extra):
    rd = root / name
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "direction": "min"})
    for type_, data in extra:
        store.append(type_, data)
    return store


def _start(tmp_path, monkeypatch, *, enabled):
    spawns: list = []
    if enabled:
        monkeypatch.setenv(ep.AUTO_RESUME_ENV, "1")
    else:
        monkeypatch.delenv(ep.AUTO_RESUME_ENV, raising=False)
    monkeypatch.setattr(run_lifecycle, "RESUME_RECONCILE_GRACE_S", 0.0)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))
    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    return spawns


def test_an_in_progress_run_is_resumed_and_says_why(tmp_path, monkeypatch):
    store = _run(tmp_path)
    spawns = _start(tmp_path, monkeypatch, enabled=True)
    assert len(spawns) == 1 and spawns[0][0][0][0] == "resume"
    asked = [e.data for e in store.read_all() if e.type == "resume_requested"]
    assert asked[0] == {"mode": "resume", "auto_resume": True}, "the durable reason it came back"


def test_off_by_default(tmp_path, monkeypatch):
    store = _run(tmp_path)
    assert _start(tmp_path, monkeypatch, enabled=False) == []
    assert not [e for e in store.read_all() if e.type == "resume_requested"]


@pytest.mark.parametrize("extra", [
    [("pause", {"reason": "operator"})],
    [("pause", {"reason": "infra_unavailable", "detail": "mount gone"})],
    [("run_finished", {"reason": "budget"})],
])
def test_a_paused_or_finished_run_is_left_alone(tmp_path, monkeypatch, extra):
    store = _run(tmp_path, "run", *extra)
    assert _start(tmp_path, monkeypatch, enabled=True) == []
    assert not [e for e in store.read_all() if e.type == "resume_requested"]
    assert not fold(store.read_all()).resume_pending()


def test_a_stale_run_is_left_for_a_human(tmp_path, monkeypatch):
    """critic 2026-10-08: a Ctrl-C'd CLI run writes no `run_finished`, so without an age bound every
    such run under the root — months old — came back on each server start."""
    store = _run(tmp_path)
    events = store.read_all()
    assert ep._request_auto_resume(tmp_path / "run", store, events, fold(events),
                                   now=events[-1].ts + 25 * 3600) is False
    assert not [e for e in store.read_all() if e.type == "resume_requested"]
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_AGE_ENV, "48")
    monkeypatch.setattr(ep, "_spawn_liveness", lambda _rd: False)
    assert ep._request_auto_resume(tmp_path / "run", store, events, fold(events),
                                   now=events[-1].ts + 25 * 3600) is True


def test_at_most_max_runs_are_resumed_per_scan(tmp_path, monkeypatch):
    stores = [_run(tmp_path, f"run{i}") for i in range(5)]
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_RUNS_ENV, "2")
    spawns = _start(tmp_path, monkeypatch, enabled=True)
    resumed = [s for s in stores if any(e.type == "resume_requested" and e.data.get("auto_resume")
                                        for e in s.read_all())]
    assert len(resumed) == 2, [[e.data for e in s.read_all() if e.type == "resume_requested"]
                               for s in stores]


def test_a_long_stage_s_fresh_log_keeps_the_run_resumable(tmp_path, monkeypatch):
    """critic 2026-10-08, second round: a training stage writes no event while it runs, so the
    last event can be a day old on exactly the run auto-resume exists for."""
    import os
    store = _run(tmp_path, "run", ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                                    "idea": {"operator": "draft"}, "code": "x"}))
    events = store.read_all()
    monkeypatch.setattr(ep, "_spawn_liveness", lambda _rd: False)
    later = events[-1].ts + 30 * 3600
    assert ep._request_auto_resume(tmp_path / "run", store, events, fold(events), now=later) is False
    log = tmp_path / "run" / "nodes" / "node_0" / "train.log"
    log.parent.mkdir(parents=True)
    log.write_text("epoch 9/10\n")
    os.utime(log, (later - 3600, later - 3600))
    assert ep._request_auto_resume(tmp_path / "run", store, events, fold(events), now=later) is True


# ---------------------------------------------------------------- review 2026-10-08


def _boot(tmp_path, monkeypatch, *, enabled=True, grace=0.0, liveness=None):
    """`_start` with the knobs these regressions need: the reconcile grace left at a real value,
    and a liveness probe that can land an operator's row at an exact point of the scan."""
    spawns: list = []
    if enabled:
        monkeypatch.setenv(ep.AUTO_RESUME_ENV, "1")
    else:
        monkeypatch.delenv(ep.AUTO_RESUME_ENV, raising=False)
    if grace is not None:
        monkeypatch.setattr(run_lifecycle, "RESUME_RECONCILE_GRACE_S", grace)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append(a[0]))
    if liveness is not None:
        monkeypatch.setattr(ep, "_spawn_liveness", liveness)
    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    return spawns


def _requests(store):
    return [e.data for e in store.read_all() if e.type == "resume_requested"]


def _reset_fence(rd):
    import json
    (rd / ".looplab-resetting.json").write_text(json.dumps({
        "version": 1, "operation_id": "12345678-1234-4234-8234-123456789abc",
        "expected_generation": "a" * 64, "receipt_name": "x"}), encoding="utf-8")


def _deletion_fence(rd):
    from looplab.core.run_deletion import publish_run_deletion_fence
    publish_run_deletion_fence(rd, operation_id="12345678-1234-4234-8234-123456789abd",
                               expected_generation="b" * 64, expected_seq=0, receipt_name="x")


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fence", [_reset_fence, _deletion_fence])
def test_a_fenced_run_neither_blocks_startup_nor_other_runs_recovery(
        tmp_path, monkeypatch, enabled, fence):
    """Finding 1: a run mid-Replay (or mid-deletion) refuses every append without its operation id,
    and the auto-resume append raised that refusal out of the startup scan — the UI server did not
    start, and no run after it was recovered. The fenced run is now skipped before deciding, and the
    run beside it (an operator-pressed resume owed) is still served."""
    bad = _run(tmp_path, "mid_replay")
    fence(tmp_path / "mid_replay")
    good = _run(tmp_path, "zz_good", ("resume_requested", {"mode": "resume"}))
    spawns = _boot(tmp_path, monkeypatch, enabled=enabled)
    assert [args[1] for args in spawns] == [str(tmp_path / "zz_good")]
    assert _requests(bad) == [], "the fenced run's own operation decides what it becomes"
    assert len(_requests(good)) == 2          # the operator's request + the server's launch claim


def test_one_runs_failing_recovery_is_contained_per_run(tmp_path, monkeypatch):
    """Finding 1, the containment half: whatever one run raises out of its recovery step is that
    run's failure, logged, never the server's startup — the next run is still recovered."""
    _run(tmp_path, "a_broken")
    _run(tmp_path, "b_fine")
    real = ep._request_auto_resume

    def flaky(rd, *args, **kwargs):
        if rd.name == "a_broken":
            raise RuntimeError("an unexpected refusal from the event store")
        return real(rd, *args, **kwargs)

    monkeypatch.setattr(ep, "_request_auto_resume", flaky)
    spawns = _boot(tmp_path, monkeypatch)
    assert [args[1] for args in spawns] == [str(tmp_path / "b_fine")]


def test_a_pause_landing_after_the_scan_read_the_log_is_not_overridden(tmp_path, monkeypatch):
    """Finding 2: the decision was made on the scan's fold but the append CASed on a FRESH re-read,
    so a pause landing in between was accepted by the swap and the spawned resume lifted it. The
    append now CASes on the rows the decision was folded from."""
    store = _run(tmp_path)
    real = ep._spawn_liveness
    calls = []

    def liveness(rd):
        calls.append(rd)
        if len(calls) == 1:          # inside `_request_auto_resume`, after the scan's fold
            EventStore(rd / "events.jsonl").append("pause", {"reason": "operator stop"})
        return real(rd)

    assert _boot(tmp_path, monkeypatch, liveness=liveness) == []
    assert _requests(store) == []
    state = fold(store.read_all())
    assert state.paused and not state.resume_pending()


@pytest.mark.parametrize("pause_on_call", [2, 3])
def test_a_pause_landing_after_the_auto_request_is_not_lifted_by_its_claim(
        tmp_path, monkeypatch, pause_on_call):
    """Finding 2, the claim half: a pause between the request and the claim (call 2 is the scan's
    own probe, call 3 the claim's, after its halted check and before its CAS) is the operator's
    newer word, and a request the SERVER minted never lifts it."""
    store = _run(tmp_path)
    real = ep._spawn_liveness
    calls = []

    def liveness(rd):
        calls.append(rd)
        if len(calls) == pause_on_call:
            EventStore(rd / "events.jsonl").append("pause", {"reason": "operator stop"})
        return real(rd)

    assert _boot(tmp_path, monkeypatch, liveness=liveness) == []
    assert _requests(store) == [{"mode": "resume", "auto_resume": True}], "no launch claim"
    assert fold(store.read_all()).paused


def test_an_operator_pressed_resume_over_a_pause_is_still_served(tmp_path, monkeypatch):
    """The halted check is narrow: a resume the OPERATOR asked for lifts their pause, as it always
    did — only a request the server minted defers to a later halt."""
    store = _run(tmp_path, "run", ("pause", {"reason": "operator"}),
                 ("resume_requested", {"mode": "resume"}))
    spawns = _boot(tmp_path, monkeypatch, enabled=True)
    assert len(spawns) == 1 and spawns[0][0] == "resume"
    assert _requests(store)[-1].get("launch_claim") is True


def test_the_auto_resume_claims_at_once_rather_than_after_the_grace(tmp_path, monkeypatch):
    """The server minted the request over a provably free lock, so no spawn of it is in flight, and
    it claims at once with the real 30 s grace in force — by decision, not because the scan's clock
    reading happened to predate the request's timestamp (which is how it used to come out so)."""
    _run(tmp_path)
    assert run_lifecycle.RESUME_RECONCILE_GRACE_S > 1.0
    spawns = _boot(tmp_path, monkeypatch, grace=None)
    assert len(spawns) == 1 and spawns[0][0] == "resume"


def test_a_resume_the_child_would_refuse_mints_no_request(tmp_path, monkeypatch):
    """Finding 3: a run whose config snapshot this build cannot read used to get a durable request
    anyway, so it sat `resume_pending()` forever (queued commands waiting, the UI saying "resume
    pending"). The child's own admission read is asked FIRST now."""
    import json
    store = _run(tmp_path)
    (tmp_path / "run" / "config.snapshot.json").write_text(
        json.dumps({"no_such_setting_from_a_newer_build": 1}), encoding="utf-8")
    assert ep.spawn_snapshot_refusal(tmp_path / "run") is not None
    assert _boot(tmp_path, monkeypatch) == []
    assert _requests(store) == []
    assert not fold(store.read_all()).resume_pending()


def test_a_resume_whose_launch_environment_is_refused_mints_no_request(tmp_path, monkeypatch):
    """Finding 3, the launch half: the credential/launch context the claim enters before Popen is
    probed before the durable append too."""
    from contextlib import contextmanager
    from looplab.serve.settings_store import SettingsStore

    @contextmanager
    def refused(self, rd):
        raise ValueError("run snapshot has no loadable task for launch credential validation")
        yield {}  # pragma: no cover — a generator, so the refusal raises on ENTER

    monkeypatch.setattr(SettingsStore, "launch_env_for_run", refused)
    store = _run(tmp_path)
    assert _boot(tmp_path, monkeypatch) == []
    assert _requests(store) == []
