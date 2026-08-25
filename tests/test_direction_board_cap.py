"""A direction somebody is already working on stops competing for room on the board.

FOUND LIVE on `runs/e5small-dr-unified-v5`, 2026-08-25, two hours into the run: FOUR deep-research
memos had completed and only the FIRST one's directions were ever registered — five of them, event
seqs 35-39. Memos 2, 3 and 4 produced concrete directions (a `dcl_threshold ∈ {0.02, 0.05, 0.1}`
sweep among them, visible in their own `hint` rows) and contributed ZERO to the board. The run paid
for three think-hard reviews and could not act on any of them.

THE MECHANISM, and my own change made it permanent. `DEEP_RESEARCH_OPEN_BELIEF_CAP` is 5 and
`_admissible_beliefs` counts `open_research_beliefs()` — open cards carrying no EVIDENCE. A
direction never carries any: since the `parent_card_id` edge shipped, the experiments answering a
direction are CHILD cards with evidence of their own, so the direction stays evidence-free for the
whole run BY DESIGN. Five childless beliefs meet a cap of five, and nothing ever frees a slot.

The fix narrows what the cap COUNTS, never what the model sees: the feed still shows a direction
with children, because one child and twelve experiments left to run is exactly the case that must
stay visible.
"""
from __future__ import annotations

from looplab.engine.research_cadence import (DEEP_RESEARCH_OPEN_BELIEF_CAP,
                                             admit_research_beliefs, is_pure_belief)
from looplab.core.models import Card, CardSelectionProvenance, RunState


def _direction(cid: str, statement: str, **kw) -> Card:
    return Card(id=cid, statement=statement, seed_statement=statement,
                selection_provenance=CardSelectionProvenance(), **kw)


def _board(cards) -> RunState:
    st = RunState(goal="g", direction="max")
    st.cards = {c.id: c for c in cards}
    return st


def _admissible(st: RunState, directions):
    """The exact expression `_admissible_beliefs` evaluates, driven without an engine."""
    taken_up = {c.parent_card_id for c in st.cards.values() if c.parent_card_id}
    open_statements = [c.seed_statement for c in st.open_research_beliefs()
                       if is_pure_belief(c) and c.id not in taken_up]
    return admit_research_beliefs(open_statements, directions)


def test_a_full_board_of_UNANSWERED_directions_still_refuses_a_new_one():
    """The cap's real job is unchanged: it bounds unanswered questions, and it must still bind."""
    st = _board([_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)])
    assert _admissible(st, ["a sixth question"]) == []


def test_a_direction_WITH_A_CHILD_frees_its_slot():
    """The live defect: four memos, one board's worth of directions, three reviews discarded."""
    cards = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    child = Card(id="card-9", statement="a concrete experiment", seed_statement="a concrete experiment",
                 parent_card_id="d0",
                 selection_provenance=CardSelectionProvenance(
                     action_source="card_added", action_owner_count=1))
    assert _admissible(_board(cards + [child]), ["a sixth question"]) == ["a sixth question"]


def test_only_the_TAKEN_UP_direction_frees_room_and_the_rest_still_count():
    cards = [_direction(f"d{i}", f"direction {i}") for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    child = Card(id="card-9", statement="e", seed_statement="e", parent_card_id="d0",
                 selection_provenance=CardSelectionProvenance(
                     action_source="card_added", action_owner_count=1))
    admitted = _admissible(_board(cards + [child]), ["q1", "q2", "q3"])
    assert admitted == ["q1"], "exactly ONE slot was freed, so exactly one question is admitted"


def test_a_duplicate_is_still_refused_before_the_cap_is_consulted():
    """The other half of `admit_research_beliefs` is untouched: an exact re-statement never lands."""
    st = _board([_direction("d0", "direction 0")])
    assert _admissible(st, ["direction 0"]) == []
    assert _admissible(st, ["Direction 0  "]) == [], "the belief key normalises case and whitespace"


def test_the_feed_still_shows_a_direction_that_has_children():
    """What was narrowed is the CAP, not the model's view. A direction with one child and a dozen
    experiments left to run is precisely the row the proposer must keep seeing."""
    child = Card(id="card-9", statement="e", seed_statement="e", parent_card_id="d0",
                 selection_provenance=CardSelectionProvenance(
                     action_source="card_added", action_owner_count=1))
    st = _board([_direction("d0", "direction 0"), child])
    assert "d0" in {c.id for c in st.open_research_beliefs()}
