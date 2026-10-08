"""The upstream board (doc 73 §2.3, track 3): what the run's verified base carries, for the Developer
and for the operator.

The upstream lane (doc 72) promotes a champion's general capability into the base behind a named
flag. The Researcher's brief named the promotion; the role that writes the code — and repairs a
failure a promoted fix already cured — was told nothing. `core/upstream_board.py` is the one reading
of the folded history; under `Settings.upstream_board_brief` every repo build turn that states the
wall-clock budget also states the promotions. OFF, and on every run that never promoted anything,
the turns are the historical text byte for byte.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.adapters.repo_developer import upstream_board_enabled
from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.core.models import Idea, RunState
from looplab.core.upstream_board import (board_lines, developer_base_note, upstream_board)
from looplab.events.eventstore import Event
from looplab.events.replay import fold

_IDEA = Idea(operator="draft", params={"lr": 0.1}, rationale="a change")
_FLAG = {"name": "fast_attn", "default": "0", "enabled": "1"}


def _history(*rows):
    st = RunState(direction="min", goal="g")
    st.upstream_history = [{"seq": i, **row} for i, row in enumerate(rows)]
    return st


def _status(board, pid):
    return next(p["status"] for p in board["proposals"] if p["proposal_id"] == pid)


# ------------------------------------------------------------------------------- the projection
def test_each_proposal_reads_its_last_row():
    st = _history(
        {"type": "upstream_proposal_started", "proposal_id": "a"},
        {"type": "upstream_proposal_started", "proposal_id": "b"},
        {"type": "upstream_proposed", "proposal_id": "b", "source_node_id": 3, "summary": "s",
         "flag": _FLAG},
        {"type": "upstream_proposal_started", "proposal_id": "c"},
        {"type": "upstream_proposal_failed", "proposal_id": "c", "code": "upstream_scorer_changed"},
        {"type": "upstream_proposed", "proposal_id": "d", "source_node_id": 5},
        {"type": "upstream_gate_started", "proposal_id": "d"},
        {"type": "upstream_proposed", "proposal_id": "e", "source_node_id": 6},
        {"type": "upstream_gate_finished", "proposal_id": "e", "result": {"passed": True}},
        {"type": "upstream_proposed", "proposal_id": "f"},
        {"type": "upstream_gate_finished", "proposal_id": "f",
         "result": {"passed": False, "code": "upstream_regression"}},
        {"type": "upstream_gate_abandoned", "proposal_id": "g"},
    )
    board = upstream_board(st)
    assert [(p["proposal_id"], p["status"]) for p in board["proposals"]] == [
        ("a", "proposing"), ("b", "proposed"), ("c", "proposal_failed"), ("d", "checking"),
        ("e", "passed"), ("f", "check_failed"), ("g", "abandoned")]
    assert [p["proposal_id"] for p in board["in_flight"]] == ["a", "b", "d", "e"]
    assert next(p for p in board["proposals"] if p["proposal_id"] == "c")["code"] == \
        "upstream_scorer_changed"
    assert board["base"] is None and board["advanced"] == []


def test_a_promotion_supersedes_what_was_proposed_against_the_old_base():
    st = _history(
        {"type": "upstream_proposed", "proposal_id": "old", "source_node_id": 1},
        {"type": "upstream_proposed", "proposal_id": "won", "source_node_id": 2},
        {"type": "upstream_gate_finished", "proposal_id": "won", "result": {"passed": True}},
        {"type": "base_advanced", "proposal_id": "won", "source_node_id": 2, "summary": "kernel",
         "flag": _FLAG},
        {"type": "upstream_proposal_started", "proposal_id": "next"},
    )
    board = upstream_board(st)
    assert _status(board, "old") == "superseded" and _status(board, "won") == "advanced"
    assert [p["proposal_id"] for p in board["in_flight"]] == ["next"]
    assert board["advanced"][0]["flag"] == _FLAG and board["advanced"][0]["source_node_id"] == 2


def test_junk_rows_are_skipped_never_raised_on():
    st = _history({"type": "base_advanced"}, {"type": "upstream_proposed", "proposal_id": 7},
                  {"type": "upstream_gate_finished", "proposal_id": "x", "result": "garbage"},
                  {"type": "base_advanced", "proposal_id": "y", "flag": {"name": "not an id"},
                   "source_node_id": True, "summary": ["x"]})
    st.upstream_history.append("not a row")
    board = upstream_board(st)
    assert _status(board, "x") == "check_failed"
    assert board["advanced"] == [{"proposal_id": "y", "seq": 3, "source_node_id": None,
                                  "summary": "", "flag": None}]


def test_the_projection_reads_the_real_fold():
    rows = [Event(seq=0, ts=0.0, type="run_started", data={"run_id": "r", "direction": "min"}),
            Event(seq=1, ts=0.0, type="base_advanced",
                  data={"proposal_id": "p", "source_node_id": 4, "summary": "bf16 kernel",
                        "flag": _FLAG, "selector": {"digest": "ab" * 32}})]
    st = fold(rows)
    board = upstream_board(st)
    assert board["base"]["digest"] == "ab" * 8 and board["base"]["source_node_id"] == 4
    assert board_lines(st)[0].startswith("upstream base: abababababababab from #4")


# ------------------------------------------------------------------------------- the Developer note
def test_no_promotion_is_no_note_and_a_promotion_names_its_flag():
    assert developer_base_note(None) == ""
    assert developer_base_note(_history({"type": "upstream_proposed", "proposal_id": "a"})) == ""
    note = developer_base_note(_history(
        {"type": "base_advanced", "proposal_id": "a", "source_node_id": 4, "summary": "bf16 kernel",
         "flag": _FLAG}))
    assert "flag `fast_attn` (default `0`, enabled `1`) from experiment #4: bf16 kernel" in note
    assert "switch its flag on instead of re-implementing it" in note


def test_the_note_keeps_the_latest_promotions_only():
    rows = [{"type": "base_advanced", "proposal_id": f"p{i}", "summary": f"cap {i}",
             "flag": {"name": f"f{i}", "default": "0", "enabled": "1"}} for i in range(8)]
    note = developer_base_note(_history(*rows))
    assert "`f7`" in note and "`f3`" in note and "`f2`" not in note


def test_the_switch_has_one_reader_and_a_legacy_row():
    assert upstream_board_enabled(Settings()) is True
    assert upstream_board_enabled(Settings(upstream_board_brief=False)) is False
    assert upstream_board_enabled(object()) is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["upstream_board_brief"] is False


# ------------------------------------------------------------------------------- driven prompts
def _repo(tmp_path: Path) -> RepoTask:
    (tmp_path / "train.py").write_text("print('x')\n")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(tmp_path),
                    edit_surface=["**/*"], protect=[],
                    eval=EvalSpec(command=["python", "train.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))


def _turns(monkeypatch, task, state, **kw) -> dict:
    """Every phase's user turn, driven through `implement` with the model replaced."""
    import looplab.agents.agent as agent_mod
    seen: dict = {}

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        seen[emit_spec["function"]["name"]] = messages[1]["content"]
        if emit_spec["function"]["name"] == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev = LLMRepoDeveloper(object(), task, plan_decompose=False, **kw)
    dev.bind_state(state)
    dev.implement(_IDEA)
    return seen


@pytest.fixture()
def promoted():
    return _history({"type": "base_advanced", "proposal_id": "a", "source_node_id": 4,
                     "summary": "bf16 kernel", "flag": _FLAG})


def test_off_and_no_promotion_are_the_historical_turns(tmp_path, monkeypatch, promoted):
    task = _repo(tmp_path)
    baseline = _turns(monkeypatch, task, _history())
    assert baseline and all("VERIFIED CAPABILITIES" not in t for t in baseline.values())
    assert _turns(monkeypatch, task, promoted) == baseline, "OFF is byte for byte"
    assert _turns(monkeypatch, task, _history(), upstream_board=True) == baseline, (
        "a run that never promoted anything is byte for byte")


def test_on_every_build_turn_states_the_promotion(tmp_path, monkeypatch, promoted):
    turns = _turns(monkeypatch, _repo(tmp_path), promoted, upstream_board=True)
    assert set(turns) >= {"declare_stages", "done"}, turns.keys()
    for name in ("declare_stages", "done"):
        assert "flag `fast_attn`" in turns[name], name


def test_a_promotion_out_of_the_history_window_is_still_read_from_the_base():
    """critic 2026-10-08: the fold keeps the last 200 upstream rows, executions included; the base
    is never trimmed, so the Developer keeps being told what it carries."""
    st = _history(*[{"type": "upstream_execution", "proposal_id": "z"} for _ in range(3)],
                  {"type": "upstream_proposed", "proposal_id": "old", "seq_hint": 1})
    st.upstream_history[-1]["seq"] = 50
    st.upstream_base = {"seq": 40, "type": "base_advanced", "proposal_id": "won",
                        "source_node_id": 4, "summary": "bf16 kernel", "flag": _FLAG}
    board = upstream_board(st)
    assert [a["proposal_id"] for a in board["advanced"]] == ["won"]
    assert "flag `fast_attn`" in developer_base_note(st)
    assert _status(board, "old") == "proposed", "proposed AFTER the promotion: still in flight"
