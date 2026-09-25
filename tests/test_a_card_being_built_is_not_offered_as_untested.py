"""A Card whose build is already running is shown as work in flight, never as an untested claimable one.

Measured 2026-09-25 on MiniOneRec inf12: card-17 was requested at 08:25 and built only at 10:51; in
between, node 18's proposal read it under "Untested hypotheses … return its CARD_ID", claimed it, and
the novelty gate rejected the proposal as a duplicate of the very build that was running.
"""
from __future__ import annotations

from looplab.agents.state_brief import (attempted_board_prompt_cards, board_prompt_lines,
                                        next_board_prompt_cards)
from looplab.events.replay import fold
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft, _admit_unit_speculation_receipt, _engine, _start,
)


def _board(tmp_path):
    engine, _ = _engine(tmp_path / "board", depth=1)
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.3)
    return engine


def test_an_untested_card_is_offered_until_its_build_is_requested(tmp_path):
    engine = _board(tmp_path)
    state = fold(engine.store.read_all())
    assert {c.id for c in next_board_prompt_cards(state)} == {"card-1", "card-2"}
    assert engine._request_card_build() is True
    state = fold(engine.store.read_all())
    requested = engine._head_request(state)["card_id"]
    assert state.cards_being_built() == {requested}
    offered = {c.id for c in next_board_prompt_cards(state)}
    assert requested not in offered and offered, offered
    assert requested in {c.id for c in attempted_board_prompt_cards(state)}


def test_the_in_flight_card_reads_as_building_in_the_proposal_brief(tmp_path):
    engine = _board(tmp_path)
    assert engine._request_card_build() is True
    state = fold(engine.store.read_all())
    requested = engine._head_request(state)["card_id"]
    text = "\n".join(board_prompt_lines(state))
    untested, _, already = text.partition("ALREADY on the board")
    assert f"CARD_ID={requested} " not in untested
    assert f"CARD_ID={requested} " in already and "STATUS=building" in already
