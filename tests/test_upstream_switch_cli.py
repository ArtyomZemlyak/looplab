"""The upstream automation's kill switch and its operator view OUTSIDE `/commands` (doc 73 §4.3).

`looplab upstream-auto RUN off|on [--reason]` appends the SAME control intent `/commands` appends
(`upstream_auto_set`) through the SAME payload rule (`engine/upstream_switch.py::
normalize_upstream_auto_set`, which the server's control normalizer now calls), and `looplab inspect`
prints the switch, the steps a cap held back, the author's spend against its cap and the automatic
advances of the last hour.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.engine.upstream_switch import (UpstreamSwitchRefusal, normalize_upstream_auto_set,
                                            upstream_operator_lines)
from looplab.events.replay import fold
from tests.test_upstream_lane import fixture


def test_the_cli_appends_the_same_control_intent_and_the_engine_reads_it(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    out = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "off", "--reason", "  GPU budget review "])
    assert out.exit_code == 0, out.output
    assert "upstream automation OFF" in out.output
    row = store.read_all()[-1]
    assert row.type == "upstream_auto_set" and row.data == {"enabled": False, "reason": "GPU budget review"}
    assert fold(store.read_all()).upstream_auto_paused is True
    out = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "ON"])
    assert out.exit_code == 0 and "upstream automation ON" in out.output
    assert store.read_all()[-1].data == {"enabled": True}
    assert fold(store.read_all()).upstream_auto_paused is False


def test_the_cli_refuses_what_the_api_refuses(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    before = store.path.read_bytes()
    bad_state = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "maybe"])
    long_reason = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "off", "--reason", "x" * 301])
    assert bad_state.exit_code == 2 and long_reason.exit_code == 2
    assert "at most 300 characters" in long_reason.output
    assert store.path.read_bytes() == before, "a refusal appends nothing"
    missing = CliRunner().invoke(app, ["upstream-auto", str(tmp_path / "nowhere"), "off"])
    assert missing.exit_code != 0


@pytest.mark.parametrize("data", [{"enabled": False}, {"enabled": True, "reason": "  ok  "},
                                  {"enabled": False, "reason": "   "}, {"enabled": "no"},
                                  {"enabled": True, "reason": 3}, {"enabled": True, "reason": "x" * 301}])
def test_one_rule_answers_both_doors(data):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from looplab.serve.control_validation import _normalize_upstream_auto_set
    try:
        expected = normalize_upstream_auto_set(dict(data))
    except UpstreamSwitchRefusal as exc:
        with pytest.raises(HTTPException) as refused:
            _normalize_upstream_auto_set(SimpleNamespace(data=dict(data)))
        assert refused.value.status_code == 400 and refused.value.detail == str(exc)
    else:
        assert _normalize_upstream_auto_set(SimpleNamespace(data=dict(data))) == expected


def test_inspect_prints_the_switch_the_held_steps_the_spend_and_the_hourly_advances(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    store.append("lane_armed", {"mode": "auto", "reason": "", "author": True})
    store.append("lane_authored", {"action_id": "auto-author-a", "track": "champion", "source_node_id": 0,
                                   "outcome": "declined", "cost_usd": 0.25})
    store.append("base_advanced", {"action_id": "auto-advance-up_1", "proposal_id": "up_1", "in_engine": True,
                                   "selector": {}, "source_node_id": 0, "summary": "s", "flag": {}})
    store.append("lane_held", {"op": "advance", "reason": "rate_cap:2/h", "proposal_id": "up_2"})
    store.append("upstream_auto_set", {"enabled": False, "reason": "review"})
    lines = upstream_operator_lines(lane.rd, store.read_all(), now=time.time())
    text = "\n".join(lines)
    assert "upstream automation: mode auto" in text
    assert "switch: OFF" in text and "review" in text
    assert "author spend: $0.2500 of $2 cap; drafts recorded: 1" in text
    assert "automatic advances in the last hour: 1 of 2/h cap" in text
    assert "held: advance up_2 rate_cap:2/h" in text and "waiting" in text
    assert "1 of 2/h" not in "\n".join(upstream_operator_lines(lane.rd, store.read_all(),
                                                               now=time.time() + 7200)), "a rolling hour"
    out = CliRunner().invoke(app, ["inspect", str(lane.rd)])
    assert out.exit_code == 0, out.output
    assert "switch: OFF" in out.output and "held: advance up_2" in out.output
    store.append("base_advanced", {"action_id": "advance-up_2", "proposal_id": "up_2", "selector": {},
                                   "source_node_id": 0, "summary": "s", "flag": {}})
    assert "released" in "\n".join(upstream_operator_lines(lane.rd, store.read_all()))


def test_a_run_without_the_lane_prints_nothing(tmp_path):
    from looplab.events.eventstore import EventStore
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "p", "task_id": "t", "goal": "g", "direction": "min"})
    assert upstream_operator_lines(tmp_path, store.read_all()) == []


def test_a_switch_already_in_place_appends_nothing_and_says_so(tmp_path):
    """review 2026-10-09: the merge with master dropped the branch's no-op — `off` twice wrote two
    identical rows into the upstream history every reader pages through."""
    lane, store, generation, body = fixture(tmp_path)
    before = store.path.read_bytes()
    never = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "on"])
    assert never.exit_code == 0 and "already ON (the switch was never set)" in never.output
    assert store.path.read_bytes() == before, "a never-set switch is already on"
    assert CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "off"]).exit_code == 0
    seq = store.read_all()[-1].seq
    again = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "OFF", "--reason", "still"])
    assert again.exit_code == 0, again.output
    assert f"already OFF (seq {seq}) — nothing appended; the reason was not recorded" in again.output
    assert [e.seq for e in store.read_all() if e.type == "upstream_auto_set"] == [seq]


class _RacingStore:
    """The run's store, with ANOTHER writer's row landing between the switch's read and its append —
    the window the compare-and-swap closes."""

    def __init__(self, store, rival):
        self._store, self._rival = store, rival

    def read_all(self):
        events = self._store.read_all()
        self._rival()
        return events

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_a_write_between_the_read_and_the_append_refuses_the_switch(tmp_path):
    """review 2026-10-09: the merge dropped the branch's `expected_last_seq` — a UI switch that landed
    between the CLI's read and its append was silently overwritten by an intent raised on a view that
    no longer held. Now the append refuses (a `ConfigRefusal`: one line, exit 2) and writes nothing."""
    from looplab.core.errors import ConfigRefusal
    from looplab.engine.upstream_switch import set_upstream_auto
    from looplab.events.eventstore import EventStore
    lane, store, generation, body = fixture(tmp_path)
    rival = EventStore(store.path)
    racing = _RacingStore(store, lambda: rival.append("upstream_auto_set", {"enabled": False,
                                                                            "reason": "from the UI"}))
    with pytest.raises(ConfigRefusal, match="changed while the switch was being set"):
        set_upstream_auto(lane.rd, False, "from the CLI", store=racing)
    rows = [e.data for e in store.read_all() if e.type == "upstream_auto_set"]
    assert rows == [{"enabled": False, "reason": "from the UI"}], "only the rival's row landed"


def test_the_switch_refusal_is_an_operator_refusal_the_cli_prints_as_one_line(tmp_path, monkeypatch):
    """review 2026-10-09: `UpstreamSwitchRefusal` was a bare `ValueError`, so one that escaped the
    CLI's wrap printed the whole traceback at exit 1. It is a `ConfigRefusal` now — still the
    `ValueError` the server answers 400 (`test_one_rule_answers_both_doors`)."""
    from looplab.core.errors import ConfigRefusal, OperatorRefusal
    from looplab.engine import upstream_switch
    assert issubclass(UpstreamSwitchRefusal, ConfigRefusal)
    assert issubclass(UpstreamSwitchRefusal, OperatorRefusal) and issubclass(UpstreamSwitchRefusal, ValueError)
    lane, store, generation, body = fixture(tmp_path)

    def _escapes(*_a, **_k):
        raise UpstreamSwitchRefusal("enabled must be true or false")
    monkeypatch.setattr(upstream_switch, "set_upstream_auto", _escapes)
    out = CliRunner().invoke(app, ["upstream-auto", str(lane.rd), "off"])
    assert out.exit_code == 2, out.output
    assert "enabled must be true or false" in out.output and "Traceback" not in out.output


def test_inspect_prints_the_queue_and_the_author_outcomes(tmp_path):
    """review 2026-10-09: the merge dropped the branch's queue line and its per-outcome author count —
    `inspect` showed the switch but not that operations still waited, nor what the drafts became."""
    from looplab.engine.upstream_switch import upstream_inspect_lines
    lane, store, generation, body = fixture(tmp_path)
    store.append("lane_armed", {"mode": "auto", "reason": "", "author": True})
    for n, outcome in enumerate(("declined", "proposed", "declined")):
        store.append("lane_authored", {"action_id": f"auto-author-{n}", "track": "champion",
                                       "source_node_id": 0, "outcome": outcome, "cost_usd": 0.1})
    for n in range(3):
        store.append("lane_op_requested", {"action_id": f"op-{n}", "op": "check",
                                           "request_hash": "a" * 64, "proposal_id": f"up_{n:024x}"})
    store.append("lane_op_done", {"idx": 0, "op": "check", "outcome": "succeeded", "action_id": "op-0"})
    events = store.read_all()
    text = "\n".join(upstream_operator_lines(lane.rd, events))
    assert "queue: 2 waiting of 3" in text
    assert "author outcomes: declined 2, proposed 1" in text
    assert "drafts recorded: 3" in text
    state = fold(events)
    assert state.lane_ops_done == 1
    # `inspect`'s path hands the FOLD's cursor through — the reader the queue's waiting count is.
    assert "queue: 2 waiting of 3" in "\n".join(upstream_inspect_lines(lane.rd, state, events))
    assert "queue: 0 waiting of 3" in "\n".join(upstream_operator_lines(lane.rd, events, cursor=3))
    shown = CliRunner().invoke(app, ["inspect", str(lane.rd)])
    assert shown.exit_code == 0, shown.output
    assert "queue: 2 waiting of 3" in shown.output and "author outcomes: declined 2" in shown.output
