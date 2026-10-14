"""Review 2026-10-14 §4.2: research-memo open questions on the question board, as a DERIVED read model.

An external agent's memos carried 212 open questions and the board registered none. The fix is a
read-time projection (`serve/memo_questions.py`) published beside the cards as `memo_questions`,
never a fold change: a recorded run's board — and the card ids experiments were filed under — must
replay exactly as before. Filing stays an explicit `hypothesis_added`, which retires the row.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from looplab.core.advisory_payloads import stamp_research_memo  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.serve.memo_questions import (  # noqa: E402
    MEMO_QUESTION_ROWS_MAX, memo_question_projection, questions_not_on_board)
from looplab.serve.server import make_app  # noqa: E402

OWNER = {"X-LoopLab-Token": "owner-secret"}
_MEMO_IDS: dict = {}   # label -> the stamped memo id of the last `_run`


def _memo(memo_id, questions, concepts=None, parents=None, at_node=0):
    memo = {"summary": "s", "memo_id": memo_id, "at_node": at_node, "trigger": "external",
            "open_questions": questions}
    if concepts is not None:
        memo["question_concepts"] = concepts
    if parents is not None:
        memo["question_parents"] = parents
    return memo


def _run(tmp_path, memos, *, filed=()):
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(json.dumps(
        {"backend": "toy", "external_harness": True}))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g",
                                 "direction": "max"})
    for statement in filed:
        store.append("hypothesis_added", {"statement": statement, "source": "external_research"})
    ids = {}
    for memo in memos:
        # The server's own write boundary: the memo id is the persisted version's digest.
        memo = dict(memo)
        label = memo.pop("memo_id")
        stamped, ids[label] = stamp_research_memo(memo)
        store.append("research_completed", {"memo": stamped, "memo_id": ids[label]})
    _MEMO_IDS.clear()
    _MEMO_IDS.update(ids)
    return rd


def _state(rd):
    return fold(EventStore(rd / "events.jsonl").read_all())


def test_unfiled_questions_are_projected_with_their_authored_concepts_and_parent(tmp_path):
    rd = _run(tmp_path, [
        _memo("m1", ["Does replay help?", "Does replay help on cold users?"],
              concepts=[["replay"], ["replay", "cold-users"]],
              parents=["", "Does replay help?"], at_node=1),
        _memo("m2", ["does   REPLAY help?", "Is week 7 worse?"], at_node=4),
    ], filed=["Is week 7 worse?"])
    projection = memo_question_projection(_state(rd))
    assert (projection["asked"], projection["on_board"], projection["not_on_board"]) == (3, 1, 2)
    rows = {row["statement"]: row for row in projection["rows"]}
    broad = rows["Does replay help?"]
    # Case and spacing are not semantics: the second memo restates the first question.
    assert broad["times_asked"] == 2
    assert broad["memo_ids"] == [_MEMO_IDS["m1"], _MEMO_IDS["m2"]]
    assert (broad["first_at_node"], broad["last_at_node"]) == (1, 4)
    assert broad["concepts"] == ["replay"]
    narrow = rows["Does replay help on cold users?"]
    assert narrow["concepts"] == ["replay", "cold-users"]
    assert narrow["parent_statement"] == "Does replay help?" and narrow["parent_on_board"] is False
    # Most recently asked first.
    assert projection["rows"][0]["statement"] == "Does replay help?"


def test_the_projection_is_not_folded_and_filing_retires_the_row(tmp_path):
    rd = _run(tmp_path, [_memo("m1", ["Does replay help?"], concepts=[["replay"]])])
    before = _state(rd)
    assert before.cards == {} and before.hypotheses_added == []
    assert [row["statement"] for row in memo_question_projection(before)["rows"]] == [
        "Does replay help?"]
    # Filing is the agent's (or the banner's) explicit hypothesis_added — the same command.
    EventStore(rd / "events.jsonl").append("hypothesis_added", {
        "statement": "Does replay help?", "source": "research_memo", "concepts": ["replay"]})
    after = _state(rd)
    assert len(after.cards) == 1
    assert memo_question_projection(after) == {
        "rows": [], "asked": 1, "on_board": 1, "not_on_board": 0, "truncated": False}


def test_the_receipt_notice_and_the_board_share_one_identity(tmp_path):
    rd = _run(tmp_path, [], filed=["Does replay help?"])
    memo = {"open_questions": ["does replay   help?", "New one?", "", 7]}
    assert questions_not_on_board(memo, _state(rd)) == ["New one?"]


def test_the_projection_is_bounded_and_total_over_junk(tmp_path):
    # A memo keeps at most 16 questions (the sanitizer's own cap), so several memos overflow the bound.
    total = MEMO_QUESTION_ROWS_MAX + 5
    memos = [_memo(f"m{start}", [f"Question number {i}?" for i in range(start, min(start + 16, total))],
                   concepts="not-a-list", parents=[7, None])
             for start in range(0, total, 16)]
    rd = _run(tmp_path, memos)
    state = _state(rd)
    state.research.append("not a memo")
    state.research.append({"open_questions": "not a list"})
    projection = memo_question_projection(state)
    assert len(projection["rows"]) == MEMO_QUESTION_ROWS_MAX and projection["truncated"] is True
    assert projection["not_on_board"] == total
    assert all("concepts" not in row and "parent_id" not in row for row in projection["rows"])


def test_the_state_payload_carries_the_projection_beside_the_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    _run(tmp_path, [_memo("m1", ["Does replay help?"], concepts=[["replay"]])])
    client = TestClient(make_app(tmp_path))
    state = client.get("/api/runs/demo/state", headers=OWNER).json()["state"]
    assert state["memo_questions"]["not_on_board"] == 1
    assert state["memo_questions"]["rows"][0]["concepts"] == ["replay"]
    assert state["cards"] == {}
