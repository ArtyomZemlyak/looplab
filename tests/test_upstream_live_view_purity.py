"""The live upstream lane's projection (`engine/upstream_serve.py::upstream_live_body`) is a PURE
function of the log, because the server caches it with the log's identity (`/state`, the SSE
`state_delta` frames) — review 2026-10-09.

Two defects, one shape. `advances_last_hour` was counted against the wall clock INTO the cached body,
so a quiet run kept answering the count of the moment the body was built ("1 of 2" an hour after the
advance aged out); and the caps were read off `config.snapshot.json`, which a `PUT /config` may edit
while the engine keeps enforcing what it ARMED with. Now the body carries the advances' timestamps and
the caps off the `lane_armed` row, and the hour is counted per serve (`live_view_at`), as
`engine_running` is stamped. A held advance the base moved past, or one the lane refused since, waits
for nothing — `auto_next_op` never asks it again — so it no longer reads `waiting`.
"""
from __future__ import annotations

import pytest

from looplab.core.models import Event
from looplab.engine import upstream_serve
from looplab.engine.upstream_serve import (LIVE_ADVANCE_TS_KEY, live_view_at, upstream_live_body,
                                           upstream_live_view)

_ARMED = {"mode": "auto", "reason": "", "author": True, "author_usd_cap": 2.0, "advances_per_hour": 2}


def _log(*rows):
    events = [Event(seq=1, ts=100.0, type="run_started", data={"upstream": {"tests": ["t"]}})]
    for seq, (ts, kind, data) in enumerate(rows, start=2):
        events.append(Event(seq=seq, ts=ts, type=kind, data=data))
    return events


def _advance(pid, *, auto=True):
    return {"proposal_id": pid, "in_engine": True,
            "action_id": f"auto-advance-{pid}" if auto else f"operator-{pid}"}


def _no_snapshot(monkeypatch):
    def refuse(run_dir):
        raise AssertionError("an armed lane's projection read the config snapshot")
    monkeypatch.setattr(upstream_serve, "run_settings", refuse)


def _no_clock(monkeypatch):
    def refuse():
        raise AssertionError("the cached body read the wall clock")
    monkeypatch.setattr(upstream_serve.time, "time", refuse)


# ------------------------------------------------------------------------------- purity
def test_the_body_reads_neither_the_clock_nor_the_snapshot(tmp_path, monkeypatch):
    """MUTATION: count `advances_last_hour` in the body again -> the clock is read; read the caps off
    `run_settings` again -> the snapshot is read."""
    events = _log((200.0, "lane_armed", _ARMED), (300.0, "base_advanced", _advance("up_a")))
    _no_snapshot(monkeypatch)
    _no_clock(monkeypatch)
    body = upstream_live_body(tmp_path, events)
    assert "advances_last_hour" not in body
    assert body[LIVE_ADVANCE_TS_KEY] == [300.0]
    assert body["author_usd_cap"] == 2.0 and body["advances_per_hour"] == 2
    assert upstream_live_body(tmp_path, events) == body, "a function of the log alone"


def test_the_caps_are_the_ones_the_engine_armed_with(tmp_path, monkeypatch):
    """A row written before the caps rode on it answers None — unknown, never a 0 that reads "no cap"."""
    _no_snapshot(monkeypatch)
    armed = {**_ARMED, "author_usd_cap": 0.5, "advances_per_hour": 7}
    body = upstream_live_body(tmp_path, _log((200.0, "lane_armed", armed)))
    assert (body["author_usd_cap"], body["advances_per_hour"]) == (0.5, 7)
    older = upstream_live_body(tmp_path, _log((200.0, "lane_armed",
                                               {"mode": "auto", "reason": "", "author": True})))
    assert older["author_usd_cap"] is None and older["advances_per_hour"] is None
    junk = upstream_live_body(tmp_path, _log((200.0, "lane_armed", {**_ARMED, "author_usd_cap": True,
                                                                   "advances_per_hour": -1})))
    assert junk["author_usd_cap"] is None and junk["advances_per_hour"] is None


def test_the_hour_is_counted_when_served_not_when_built(tmp_path):
    events = _log((200.0, "lane_armed", _ARMED), (1000.0, "base_advanced", _advance("up_a")),
                  (4000.0, "base_advanced", _advance("up_b")),
                  (4100.0, "base_advanced", _advance("up_c", auto=False)),
                  (4200.0, "base_advanced", {"proposal_id": "up_d", "action_id": "auto-advance-up_d"}))
    body = upstream_live_body(tmp_path, events)
    frozen = dict(body)
    assert live_view_at(body, 4300.0)["advances_last_hour"] == 2
    assert live_view_at(body, 4700.0)["advances_last_hour"] == 1, "up_a aged out with nothing appended"
    assert live_view_at(body, 9000.0)["advances_last_hour"] == 0
    assert LIVE_ADVANCE_TS_KEY not in live_view_at(body, 4300.0), "the wire shape is unchanged"
    assert live_view_at(body, None)["advances_last_hour"] is None, "a historical read is not now"
    assert body == frozen, "the cached body is never stamped in place"
    assert live_view_at(None, 1.0) is None
    assert upstream_live_view(tmp_path, events, now=4300.0)["advances_last_hour"] == 2


# ------------------------------------------------------------------------------- what still waits
def test_a_held_advance_the_base_moved_past_or_the_lane_refused_waits_for_nothing(tmp_path):
    held = lambda pid, reason="rate_cap:2/h": {"op": "advance", "reason": reason, "proposal_id": pid}  # noqa: E731
    events = _log((200.0, "lane_armed", _ARMED),
                  (300.0, "lane_held", held("up_a")),                     # superseded by up_b's advance
                  (310.0, "lane_held", held("up_r")),                     # refused since
                  (320.0, "lane_held", held("up_r", "refused:upstream_base_conflict")),
                  (330.0, "base_advanced", _advance("up_b", auto=False)),
                  (340.0, "lane_held", held("up_c")),                     # held on the CURRENT base
                  (350.0, "lane_held", {"op": "author", "reason": "cost_cap:2usd"}))
    rows = {(row["proposal_id"] if "proposal_id" in row else row["op"], row["reason"]): row["waiting"]
            for row in upstream_live_body(tmp_path, events)["held"]}
    assert rows == {("up_a", "rate_cap:2/h"): False,
                    ("up_r", "rate_cap:2/h"): False,
                    ("up_r", "refused:upstream_base_conflict"): False,
                    ("up_c", "rate_cap:2/h"): True,
                    ("author", "cost_cap:2usd"): False}


def test_the_author_waits_while_its_spend_is_at_the_armed_cap(tmp_path):
    events = _log((200.0, "lane_armed", _ARMED),
                  (300.0, "lane_authored", {"action_id": "a", "outcome": "drafted", "track": "champion",
                                            "source_node_id": 1, "cost_usd": 2.5}),
                  (310.0, "lane_held", {"op": "author", "reason": "cost_cap:2usd"}))
    assert upstream_live_body(tmp_path, events)["held"][0]["waiting"] is True
    rearmed = events + [Event(seq=9, ts=400.0, type="lane_armed", data={**_ARMED, "author_usd_cap": 5.0})]
    assert upstream_live_body(tmp_path, rearmed)["held"][0]["waiting"] is False, "the new engine's cap"


# ------------------------------------------------------------------------------- the served payload
def test_the_state_payload_re_stamps_the_hour_on_every_serve(tmp_path, monkeypatch):
    """MUTATION: drop the stamp in `AppState.state_payload` -> the cached body's timestamps leak and
    the count never moves; stamp the cached dict in place -> the body loses its timestamps."""
    pytest.importorskip("fastapi")
    from looplab.events.eventstore import EventStore
    from looplab.serve import appstate
    from looplab.serve.server import make_app
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min",
                                 "upstream": {"tests": ["t"]}})
    store.append("lane_armed", _ARMED)
    store.append("base_advanced", _advance("up_a"))
    advanced_at = next(e.ts for e in store.read_all() if e.type == "base_advanced")
    srv = make_app(tmp_path).state.looplab
    folds = []
    real_fold = appstate.fold
    monkeypatch.setattr(appstate, "fold", lambda events: folds.append(1) or real_fold(events))
    monkeypatch.setattr(appstate.time, "time", lambda: advanced_at + 60.0)
    first = srv.state_payload(rd)["state"]["upstream_live"]
    assert first["advances_last_hour"] == 1 and LIVE_ADVANCE_TS_KEY not in first
    assert (first["author_usd_cap"], first["advances_per_hour"]) == (2.0, 2)
    built = len(folds)
    monkeypatch.setattr(appstate.time, "time", lambda: advanced_at + 3700.0)
    later = srv.state_payload(rd)["state"]["upstream_live"]
    assert len(folds) == built, "served from the cache: nothing was appended"
    assert later["advances_last_hour"] == 0, "the advance aged out of the hour"
    historical = srv.state_payload(rd, upto_seq=3)["state"]["upstream_live"]
    assert historical["advances_last_hour"] is None
