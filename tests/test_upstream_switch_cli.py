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
    # The row a live engine writes since the caps rode on it (review 2026-10-09): the view reads
    # the caps that engine ENFORCES here, never off a snapshot edited since.
    store.append("lane_armed", {"mode": "auto", "reason": "", "author": True,
                                "author_usd_cap": 2.0, "advances_per_hour": 2})
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
