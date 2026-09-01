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
    """The exact expression `_admissible_beliefs` evaluates, driven without an engine.

    IT STOPPED BEING THAT EXPRESSION and the docstring kept the claim. This helper applied the
    `c.id not in taken_up` narrowing to `open_statements` and then passed no `counted` at all, i.e.
    it was the ONE-POPULATION form — precisely the defect the two-population fix below it exists to
    prevent, frozen into the harness that every test above it runs through. It coincided with
    production only while no fixture combined a taken-up direction with a restatement of it; the
    tests that DO cover that call `admit_research_beliefs` directly, which is why nothing was red.
    A helper claiming to mirror production has to be re-derived from production, not from memory.
    """
    taken_up = {c.parent_card_id for c in st.cards.values() if c.parent_card_id}
    beliefs = [c for c in st.open_research_beliefs() if is_pure_belief(c)]
    return admit_research_beliefs(
        [c.seed_statement for c in beliefs], directions,
        counted=[c.seed_statement for c in beliefs if c.id not in taken_up])


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


# --------------------------------------------------------------------------------------------
# The two populations. `open_statements` and `counted` answer different questions, and sharing one
# list made the cap's (correct) narrowing silently break the duplicate rule.
# --------------------------------------------------------------------------------------------

_DIRECTION = "Distil from a stronger teacher"
_RESTATED = "distil from a  stronger   TEACHER"   # case/whitespace variant, same normalized key


def test_a_TAKEN_UP_question_is_still_deduplicated_against():
    """The defect. Freeing a cap slot must not also forget the question exists.

    A direction with children no longer competes for board room — that is the fix `counted` carries.
    But it is still registered, and a later memo restating it must not mint a SECOND card for the
    same question: `hypothesis_id` differs on a re-worded statement, so the fold would create one,
    and because a direction never accrues evidence the open population would then grow without
    bound past the five-row prompt window.
    """
    assert admit_research_beliefs([_DIRECTION], [_RESTATED], counted=[]) == [], (
        "MUTATION: pass the narrowed list as `open_statements` too and this admits the restatement, "
        "putting a duplicate of an already-answered question on the board")


def test_the_room_a_taken_up_question_frees_goes_to_a_NEW_question():
    """The counter-assertion — the dedup fix must not cost the cap fix. This is the whole point of
    narrowing `counted`: `runs/e5small-dr-unified-v5` paid for three think-hard reviews and could
    register nothing from any of them, because five childless beliefs met a cap of five."""
    full = [f"question {i}" for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    assert admit_research_beliefs(full, ["a genuinely new question"], counted=full) == [], (
        "five UNANSWERED questions still fill the board")
    assert admit_research_beliefs(full, ["a genuinely new question"], counted=[]) == [
        "a genuinely new question"], "…and questions somebody is working on free their room"


def test_an_admitted_direction_immediately_occupies_a_slot():
    """Otherwise one memo could fill an empty board past the cap in a single pass."""
    admitted = admit_research_beliefs(
        [], [f"q{i}" for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP + 3)], counted=[])
    assert len(admitted) == DEEP_RESEARCH_OPEN_BELIEF_CAP


def test_counted_defaults_to_the_open_list_byte_for_byte():
    """`counted=None` is the historical rule, so every existing caller and every replayed log is
    unchanged by the parameter existing."""
    board = [f"q{i}" for i in range(3)]
    proposed = ["q1", "a new one", "another"]
    assert (admit_research_beliefs(board, proposed)
            == admit_research_beliefs(board, proposed, counted=board))
