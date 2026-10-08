"""The operator's hold on the upstream automation (doc 73 §4.2 G2–G4): a kill switch on the live run,
a cap on automatic advances per hour, and a money cap on the author — each an audit row.

Real CPU SGD through the doc-72 fixture with only the author's two model calls stubbed (no paid call).
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.engine import upstream_serve
from looplab.engine.upstream_author import author_spent_usd, author_usd_cap
from looplab.engine.upstream_serve import advances_per_hour, auto_advances_in_window, upstream_live_view
from looplab.events.replay import fold
from tests.test_upstream_author import _champion_draft, _live_engine, _model
from tests.test_upstream_lane import fixture
from tests.test_upstream_live_lane import _engine, _live, _serve


def _settings(lane, **update):
    snapshot = lane.rd / "config.snapshot.json"
    snapshot.write_text(Settings.model_validate_json(snapshot.read_bytes()).model_copy(
        update=update).model_dump_json(), encoding="utf8")


def _types(store):
    return [e.type for e in store.read_all()]


# ------------------------------------------------------------------------------- the readers
def test_the_caps_have_one_reader_each_and_sane_defaults():
    assert advances_per_hour(Settings()) == 2 and advances_per_hour(object()) == 0
    assert advances_per_hour(Settings(upstream_advances_per_hour=0)) == 0
    assert author_usd_cap(Settings()) == 2.0 and author_usd_cap(object()) == 0.0
    assert author_usd_cap(Settings(upstream_author_usd=0)) == 0.0


def test_only_the_engines_own_recent_advances_count():
    from looplab.core.models import Event
    rows = [Event(seq=1, ts=500.0, type="base_advanced", data={"in_engine": True, "action_id": "auto-advance-up_a"}),
            Event(seq=2, ts=4000.0, type="base_advanced", data={"in_engine": True, "action_id": "auto-advance-up_b"}),
            Event(seq=3, ts=4100.0, type="base_advanced", data={"in_engine": True, "action_id": "operator-1"}),
            Event(seq=4, ts=4200.0, type="base_advanced", data={"action_id": "auto-advance-up_c"})]
    assert auto_advances_in_window(rows, now=4300.0) == 1


# ------------------------------------------------------------------------------- the kill switch
def test_the_kill_switch_stops_the_author_and_the_automatic_steps(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    engine = _live_engine(lane, store)
    store.append("upstream_auto_set", {"enabled": False, "reason": "operator review"})
    assert fold(store.read_all()).upstream_auto_paused is True
    _serve(engine)
    assert calls == [] and "lane_authored" not in _types(store), "no paid call while stopped"
    view = upstream_live_view(lane.rd, store.read_all())
    assert view["auto_paused"] is True
    store.append("upstream_auto_set", {"enabled": True})
    assert fold(store.read_all()).upstream_auto_paused is False
    _serve(engine)
    assert "base_advanced" in _types(store), "resumed: drafted, checked and advanced"
    history = [r for r in fold(store.read_all()).upstream_history if r["type"] == "upstream_auto_set"]
    assert [r["enabled"] for r in history] == [False, True], "the switch is audit history"


def test_the_kill_switch_still_serves_what_the_operator_queued(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    store.append("upstream_auto_set", {"enabled": False})
    store.append("lane_op_requested", {"op": "check", "action_id": "operator-check", "request_hash": "x",
                                       "body": {"expected_generation": generation,
                                                "action_id": "operator-check",
                                                "proposal_id": made["proposal_id"]}})
    _serve(_engine(lane, store))
    events = store.read_all()
    done, = [e.data for e in events if e.type == "lane_op_done"]
    assert done["action_id"] == "operator-check" and done["outcome"] == "succeeded"
    assert "base_advanced" not in _types(store), "the automatic advance stays stopped"


def test_the_control_intake_accepts_only_a_bool():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from looplab.serve.control_validation import _ControlIntake, _normalize_upstream_auto_set
    ok = _normalize_upstream_auto_set(_ControlIntake(None, None, "upstream_auto_set",
                                                     {"enabled": False, "reason": " review "}))
    assert ok["enabled"] is False and ok["reason"].strip() == "review"
    with pytest.raises(HTTPException):
        _normalize_upstream_auto_set(_ControlIntake(None, None, "upstream_auto_set", {"enabled": "no"}))


# ------------------------------------------------------------------------------- the hourly cap
def test_a_passed_gate_past_the_hourly_cap_waits_and_says_so_once(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    monkeypatch.setattr(upstream_serve, "auto_advances_in_window", lambda events, now, window_s=3600.0: 2)
    _serve(_engine(lane, store))
    _serve(_engine(lane, store))
    events = store.read_all()
    assert any(e.type == "upstream_gate_finished" for e in events), "the check still runs"
    assert "base_advanced" not in [e.type for e in events]
    held = [e.data for e in events if e.type == "lane_held"]
    assert held == [{"op": "advance", "reason": "rate_cap:2/h", "proposal_id": made["proposal_id"]}]
    monkeypatch.setattr(upstream_serve, "auto_advances_in_window", lambda events, now, window_s=3600.0: 0)
    _serve(_engine(lane, store))
    assert "base_advanced" in _types(store), "the hour freed: it advances"


def test_no_hourly_cap_when_set_to_zero(tmp_path, monkeypatch):
    lane, store, generation, body, made = _live(tmp_path)
    _settings(lane, upstream_advances_per_hour=0)
    monkeypatch.setattr(upstream_serve, "auto_advances_in_window", lambda events, now, window_s=3600.0: 99)
    _serve(_engine(lane, store))
    assert "base_advanced" in _types(store) and "lane_held" not in _types(store)


# ------------------------------------------------------------------------------- the money cap
def test_the_authors_spend_is_recorded_on_its_row(tmp_path, monkeypatch):
    import looplab.core.parse as parse
    from looplab.core.llm_budget import note_committed_cost
    from looplab.agents.maintainer import MaintainerCritique, MaintainerDraft
    lane, store, generation, body = fixture(tmp_path)

    def fake(client, messages, model, parser="tool_call"):
        note_committed_cost(0.25)                   # what a priced provider call commits on this thread
        if model is MaintainerDraft:
            return MaintainerDraft.model_validate(_champion_draft())
        return MaintainerCritique(verdict="pass", reason="generalized behind a flag")
    monkeypatch.setattr(parse, "parse_structured", fake)
    _serve(_live_engine(lane, store))
    authored, = [e.data for e in store.read_all() if e.type == "lane_authored"]
    assert authored["cost_usd"] == pytest.approx(0.5)
    assert author_spent_usd(store.read_all()) == pytest.approx(0.5)
    assert upstream_live_view(lane.rd, store.read_all())["author_spent_usd"] == pytest.approx(0.5)


def test_the_author_stops_paying_at_its_budget(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    store.append("lane_authored", {"action_id": "auto-author-earlier", "track": "repair",
                                   "source_node_id": 0, "outcome": "declined", "cost_usd": 2.5})
    engine = _live_engine(lane, store)
    _serve(engine)
    _serve(engine)
    assert calls == []
    held = [e.data for e in store.read_all() if e.type == "lane_held"]
    assert held == [{"op": "author", "reason": "cost_cap:2usd"}]


def test_no_money_cap_when_set_to_zero(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    _settings(lane, upstream_author_usd=0.0)
    calls = []
    _model(monkeypatch, _champion_draft(), calls=calls)
    store.append("lane_authored", {"action_id": "auto-author-earlier", "track": "repair",
                                   "source_node_id": 0, "outcome": "declined", "cost_usd": 50.0})
    _serve(_live_engine(lane, store))
    assert len(calls) == 2 and "lane_held" not in _types(store)
