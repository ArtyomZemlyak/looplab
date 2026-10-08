"""The auto-resume bounds are TOTAL, and a candidate-written log stamp cannot keep a run alive.

Review 2026-10-08. (a) `LOOPLAB_UI_AUTO_RESUME_MAX_RUNS=1e999` raised `OverflowError` in the startup
scan — outside per-run containment and even with auto-resume OFF — and the UI server did not start;
`0`, `0.5` and `0` hours meant three different things. (b) `_last_alive_ts` took a pending node's
`*.log` mtime as proof of life, and the candidate owns that directory: an `os.utime` into the future
made the run look alive forever and defeated `MAX_AGE_H`.
"""
from __future__ import annotations

import logging
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.config import Settings  # noqa: E402
from looplab.engine import run_lifecycle  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve import engine_proc as ep  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _run(root, name="run", *, node=False):
    rd = root / name
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "direction": "min"})
    if node:
        store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft"}, "code": "x"})
    return store


@pytest.mark.parametrize("raw", ["1e999", "inf", "-inf", "nan", "0", "-3", "2.5", "lots", "1e3"])
def test_a_max_runs_value_that_is_not_a_positive_whole_number_is_the_default(raw, monkeypatch, caplog):
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_RUNS_ENV, raw)
    with caplog.at_level(logging.WARNING, logger=ep._log.name):
        value = ep._auto_resume_bound(ep.AUTO_RESUME_MAX_RUNS_ENV, ep._AUTO_RESUME_MAX_RUNS, integer=True)
    assert value == ep._AUTO_RESUME_MAX_RUNS
    assert ep.AUTO_RESUME_MAX_RUNS_ENV in caplog.text, "an ignored value is said, not swallowed"


@pytest.mark.parametrize("raw", ["1e999", "inf", "nan", "0", "-1", "soon"])
def test_a_max_age_value_that_is_not_a_finite_positive_number_is_the_default(raw, monkeypatch):
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_AGE_ENV, raw)
    assert ep._auto_resume_bound(ep.AUTO_RESUME_MAX_AGE_ENV, ep._AUTO_RESUME_MAX_AGE_H) == 24.0


def test_valid_values_and_an_unset_variable(monkeypatch, caplog):
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_RUNS_ENV, " 3 ")
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_AGE_ENV, "0.5")
    with caplog.at_level(logging.WARNING, logger=ep._log.name):
        assert ep._auto_resume_bound(ep.AUTO_RESUME_MAX_RUNS_ENV, 4, integer=True) == 3
        assert ep._auto_resume_bound(ep.AUTO_RESUME_MAX_AGE_ENV, 24.0) == 0.5
        monkeypatch.delenv(ep.AUTO_RESUME_MAX_RUNS_ENV)
        assert ep._auto_resume_bound(ep.AUTO_RESUME_MAX_RUNS_ENV, 4, integer=True) == 4
    assert not caplog.text


@pytest.mark.parametrize("enabled", [True, False])
def test_an_overflowing_max_runs_never_costs_the_server_its_startup(tmp_path, monkeypatch, enabled):
    store = _run(tmp_path)
    monkeypatch.setenv(ep.AUTO_RESUME_MAX_RUNS_ENV, "1e999")
    if enabled:
        monkeypatch.setenv(ep.AUTO_RESUME_ENV, "1")
    else:
        monkeypatch.delenv(ep.AUTO_RESUME_ENV, raising=False)
    spawns: list = []
    monkeypatch.setattr(run_lifecycle, "RESUME_RECONCILE_GRACE_S", 0.0)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append(a))
    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
    asked = [e.data for e in store.read_all()
             if e.type == "resume_requested" and e.data.get("auto_resume") is True]
    assert (len(spawns), len(asked)) == ((1, 1) if enabled else (0, 0))


def _future_log(rd, when):
    log = rd / "nodes" / "node_0" / "train.log"
    log.parent.mkdir(parents=True)
    log.write_text("epoch 1\n")
    os.utime(log, (when, when))
    return log


def test_a_future_log_stamp_cannot_keep_a_stale_run_alive(tmp_path, monkeypatch):
    store = _run(tmp_path, node=True)
    rd = tmp_path / "run"
    events = store.read_all()
    monkeypatch.setattr(ep, "_spawn_liveness", lambda _rd: False)
    # Ten days after the last event, a log the candidate stamped a year into the future.
    later = events[-1].ts + 10 * 24 * 3600
    _future_log(rd, later + 365 * 24 * 3600)
    alive = ep._last_alive_ts(rd, events, fold(events), now=later)
    assert alive <= later
    assert ep._request_auto_resume(rd, store, events, fold(events), now=later) is False
    assert not [e for e in store.read_all() if e.type == "resume_requested"]


def test_a_log_extends_the_last_event_by_at_most_one_launch(tmp_path, monkeypatch):
    store = _run(tmp_path, node=True)
    rd = tmp_path / "run"
    (rd / "config.snapshot.json").write_text(
        Settings(max_launch_timeout_s=2 * 24 * 3600.0).model_dump_json(), encoding="utf-8")
    events = store.read_all()
    last = events[-1].ts
    _future_log(rd, last + 30 * 24 * 3600)
    assert ep._last_alive_ts(rd, events, fold(events), now=last + 3 * 24 * 3600) == last + 2 * 24 * 3600
    # Unreadable snapshot: the absolute 7-day launch limit is the bound.
    (rd / "config.snapshot.json").write_text("{not json", encoding="utf-8")
    assert ep._last_alive_ts(rd, events, fold(events), now=last + 20 * 24 * 3600) == last + 7 * 24 * 3600
    # And never past `now`.
    assert ep._last_alive_ts(rd, events, fold(events), now=last + 3600) == last + 3600


def test_an_honest_long_stage_log_still_counts(tmp_path):
    """The behaviour the stamp exists for is kept: a stage writing its log hours after the last
    event, inside one launch, is proof of life."""
    store = _run(tmp_path, node=True)
    rd = tmp_path / "run"
    events = store.read_all()
    last = events[-1].ts
    _future_log(rd, last + 20 * 3600)
    assert ep._last_alive_ts(rd, events, fold(events), now=last + 21 * 3600) == pytest.approx(last + 20 * 3600)
