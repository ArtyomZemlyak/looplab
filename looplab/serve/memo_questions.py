"""Research-memo open questions projected onto the question board — a DERIVED read model.

Review 2026-10-14 section 4.2 / doc 80 §7 UI 4.2. An external agent's memos carried 212 open questions (69
unique) and the board registered none: the step that turns `open_questions` into cards
(`engine/research_cadence.py::_record_research_steering`) runs only for LoopLab's own Researcher.

WHY THIS IS NOT A FOLD CHANGE. The board is `RunState.cards`, a fold of `hypothesis_added` /
`card_*` events. Teaching the fold to mint a card from every `research_completed` would change the
folded state of EVERY recorded run — old external runs would replay to a board their operator never
saw, and the card ids experiments were filed under would move. So the projection lives here, at
read time: a pure function of the folded memos and the folded board, attached to the state payload
as `memo_questions` beside the board, never folded and never read by the engine. Filing stays an
EXPLICIT event — the agent's `hypothesis_added`, or the Research banner's "File on board", which
sends the same command — so a run's log changes only when somebody chose to change it.

THE SAME QUESTION IDENTITY THE BOARD ADMITS BY. A question is "on the board" when a card's
statement (or its seed statement) has the same `normalized_belief_key` — the append-site duplicate
rule the internal Researcher's admission uses — and the concept / parent joins are the shared
positional ones (`question_concept_rows`, `question_parent_rows`), so a row here files exactly as the
internal path would have filed it.
"""
from __future__ import annotations

from typing import Any

from looplab.core.cards import CARD_STATEMENT_MAX_CHARS, hypothesis_id
from looplab.engine.research_cadence import (
    normalized_belief_key, question_concept_rows, question_parent_rows)

# Bounds of the published projection. The board itself is capped at 256 cards on the wire; the
# banner is a to-do list, so its rows are the most recently asked first and the count is exact.
MEMO_QUESTION_ROWS_MAX = 64
# A row's `statement` is what the banner FILES (`hypothesis_added`), so it is never cut below the
# filing intake's own bound: a cut statement files a card whose belief key is not the question's,
# and the question then reads "not on the board" forever (review 2026-10-15 — the cut was 500
# while a memo question may be 1 200 chars, `core/advisory_payloads.py`). The row `id` is derived
# from the same uncut text. `MEMO_QUESTION_STATEMENT_CHARS` bounds DISPLAY-only text (the parent).
MEMO_QUESTION_FILED_CHARS = CARD_STATEMENT_MAX_CHARS
MEMO_QUESTION_STATEMENT_CHARS = 500
MEMO_QUESTION_MEMO_IDS_MAX = 8
MEMO_QUESTION_CONCEPTS_MAX = 16


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _board_keys(state) -> tuple[set[str], dict[str, str]]:
    """Every board statement's admission key, and card id -> statement for parent names."""
    keys: set[str] = set()
    statements: dict[str, str] = {}
    for card_id, card in (getattr(state, "cards", None) or {}).items():
        for attr in ("statement", "seed_statement"):
            text = _text(getattr(card, attr, None))
            if text:
                keys.add(normalized_belief_key(text))
        statements[str(card_id)] = _text(getattr(card, "statement", None))
    return keys, statements


def memo_question_projection(state) -> dict:
    """`{rows, asked, on_board, not_on_board, truncated}` over the run's folded memos.

    `rows` are the questions NOT on the board, most recently asked first, each with the concepts
    and parent its memo authored, the memos that asked it and when. A question every memo dropped
    later is still listed: a memo is a snapshot, and only filing (or the board) retires a question.
    Total over junk: a memo that is not a dict, a non-string question, a ragged concept row.
    """
    board_keys, board_statements = _board_keys(state)
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for index, memo in enumerate(getattr(state, "research", None) or []):
        if not isinstance(memo, dict):
            continue
        questions = memo.get("open_questions")
        if not isinstance(questions, list) or not questions:
            continue
        concepts_by = question_concept_rows(questions, memo.get("question_concepts") or [])
        parents_by = question_parent_rows(questions, memo.get("question_parents") or [],
                                          known_ids=list(board_statements))
        same_memo = {hypothesis_id(_text(q)): _text(q) for q in questions if _text(q)}
        memo_id = _text(memo.get("memo_id")) or f"memo-{index}"
        at_node = memo.get("at_node") if isinstance(memo.get("at_node"), int) else None
        for question in questions:
            statement = _text(question)
            if not statement:
                continue
            key = normalized_belief_key(statement)
            row = by_key.get(key)
            if row is None:
                row = by_key[key] = {
                    "statement": statement[:MEMO_QUESTION_FILED_CHARS],
                    "on_board": key in board_keys, "memo_ids": [], "times_asked": 0,
                    "first_at_node": at_node, "last_at_node": at_node,
                    "_last": index}
                order.append(key)
            row["times_asked"] += 1
            row["_last"] = index
            if at_node is not None:
                row["last_at_node"] = at_node
            if memo_id not in row["memo_ids"]:
                row["memo_ids"] = (row["memo_ids"] + [memo_id])[-MEMO_QUESTION_MEMO_IDS_MAX:]
            concepts = [c for c in concepts_by.get(statement) or [] if isinstance(c, str) and c]
            if concepts:            # the LATEST memo that authored concepts for it wins
                row["concepts"] = concepts[:MEMO_QUESTION_CONCEPTS_MAX]
            parent = parents_by.get(statement)
            if parent:
                row["parent_id"] = parent
                parent_text = same_memo.get(parent) or board_statements.get(parent) or ""
                if parent_text:
                    row["parent_statement"] = parent_text[:MEMO_QUESTION_STATEMENT_CHARS]
                    row["parent_on_board"] = (parent in board_statements
                                              or normalized_belief_key(parent_text) in board_keys)
    missing = [by_key[key] for key in order if not by_key[key]["on_board"]]
    missing.sort(key=lambda row: row["_last"], reverse=True)
    rows = []
    for row in missing[:MEMO_QUESTION_ROWS_MAX]:
        out = {k: v for k, v in row.items() if not k.startswith("_") and k != "on_board"}
        out["id"] = hypothesis_id(row["statement"])
        rows.append(out)
    return {
        "rows": rows,
        "asked": len(order),
        "on_board": len(order) - len(missing),
        "not_on_board": len(missing),
        "truncated": len(missing) > MEMO_QUESTION_ROWS_MAX,
    }


def questions_not_on_board(memo: dict, state) -> list[str]:
    """The intake receipt's half: which of ONE incoming memo's questions the board lacks."""
    board_keys, _ = _board_keys(state)
    return [q for q in (memo.get("open_questions") or [])
            if isinstance(q, str) and q.strip() and normalized_belief_key(q) not in board_keys]
