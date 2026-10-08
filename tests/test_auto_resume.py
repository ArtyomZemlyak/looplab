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
    assert ep._request_auto_resume(tmp_path / "run", store, fold(events),
                                   now=events[-1].ts + 25 * 3600) is False
    assert not [e for e in store.read_all() if e.type == "resume_requested"]
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_AGE_ENV, "48")
    monkeypatch.setattr(ep, "_spawn_liveness", lambda _rd: False)
    assert ep._request_auto_resume(tmp_path / "run", store, fold(events),
                                   now=events[-1].ts + 25 * 3600) is True


def test_at_most_max_runs_are_resumed_per_scan(tmp_path, monkeypatch):
    stores = [_run(tmp_path, f"run{i}") for i in range(5)]
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_RUNS_ENV, "2")
    spawns = _start(tmp_path, monkeypatch, enabled=True)
    resumed = [s for s in stores if any(e.type == "resume_requested" and e.data.get("auto_resume")
                                        for e in s.read_all())]
    assert len(resumed) == 2, [[e.data for e in s.read_all() if e.type == "resume_requested"]
                               for s in stores]
