"""The end-to-end half of #69: a board of QUESTIONS must not shut the prefetch lane.

`test_prefetch_inventory_counts_work.py` pins the COUNT — that `unconsumed_card_inventory` skips
research directions. This file pins the CONSEQUENCE, which is the thing that was actually broken and
the thing no unit test of the counter can see: with an evaluation in flight and questions on the
board, `_occupancy_paced_creates` must return work.

WHY IT NEEDS ITS OWN FILE RATHER THAN A LIVE RUN. The obvious verification is "watch the next run and
see whether GPU 1 gets work", and that is exactly what could not be relied on: `runs/e5small-dr-
unified-v7`'s opening memo came back with ZERO directions, so its board carries no questions and its
supply is 0 for a reason that has nothing to do with the fix. A green result there would be
indistinguishable from the pre-fix engine. The discriminating condition has to be constructed, and
once constructed it belongs in the suite permanently rather than in one run's log.

THE STATE BEING RECONSTRUCTED is the live v6 board that idled a two-H200 box for seven hours:
    ceiling  = min(speculation_depth 2, card_lane_width(greedy) 1) = 1
    supply   = _speculation_depth_used(consumed) 0  +  unconsumed_card_inventory 6  = 6
    guard    = 6 < 1  ->  False, on every turn from t+12.6m onward
Six of those cards were the opening memo's questions and none of them could ever become a Node.
"""
from __future__ import annotations

from looplab.core.cards import Card, CardSelectionProvenance
from looplab.core.models import RunState
from looplab.engine.speculation import SpeculationMixin
from looplab.search.card_selection import (CardResourceEnvelope, card_lane_width,
                                           unconsumed_card_inventory)
from looplab.search.policy import GreedyTree


def _question(cid: str) -> Card:
    """A deep-research direction: `action_source` "none" means no action owner, so it is unbuildable."""
    return Card(id=cid, statement=cid, seed_statement=cid, status="proposed", verdict="open",
                selection_provenance=CardSelectionProvenance(action_source="none"))


def _staged_work_item(cid: str) -> Card:
    return Card(id=cid, statement=cid, seed_statement=cid, status="proposed", verdict="open",
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))


def _board(*cards: Card) -> RunState:
    state = RunState(run_id="r", task_id="t", direction="max")
    state.cards = {card.id: card for card in cards}
    return state


def test_six_questions_no_longer_fill_a_ceiling_of_one():
    """The exact arithmetic that idled the box, asserted as arithmetic.

    `_prefetch_supply_used` is the producing gate's own number: the unconsumed speculative depth plus
    the Card inventory the board holds. With the six questions counted it read 6 against a ceiling of
    1; the fix must make the questions contribute nothing.
    """
    board = _board(*[_question(f"q{i}") for i in range(6)])
    ceiling = min(2, card_lane_width(GreedyTree()))
    supply = SpeculationMixin._prefetch_supply_used(board)

    assert ceiling == 1, "greedy holds one prefetch; if this ever widens, re-measure v7's supersedes"
    assert supply == 0, (
        "MUTATION: drop the kind clause in `unconsumed_card_inventory` and this reads 6 — the live "
        "v6 number that refused every prefetch for seven hours")
    assert supply < ceiling, (
        "and the GATE must open: this is the `_prefetch_supply_used` conjunct that decides whether "
        "the raw lane may mint work for a second evaluation lane")


def test_a_STAGED_WORK_ITEM_still_shuts_the_same_gate():
    """The other direction, and the reason the clause is by kind and not a blanket skip.

    One unbuilt work item is exactly the inventory the ceiling exists to bound: buying a second
    prefetch on top of it is what produced 3 builds and 3 supersedes on `rubertlite-dr-unified-v7`,
    roughly an hour of Developer wall-clock each.
    """
    board = _board(_staged_work_item("w1"), *[_question(f"q{i}") for i in range(6)])
    ceiling = min(2, card_lane_width(GreedyTree()))

    supply = SpeculationMixin._prefetch_supply_used(board)
    assert supply == 1, "the work item counts; the six questions do not"
    assert not supply < ceiling, (
        "MUTATION: make the clause skip every card and this goes red — the gate would then buy a "
        "prefetch while one already sits unbuilt, which is the defect the ceiling exists for")


def test_a_QUESTION_is_not_made_buildable_by_the_narrowing():
    """Excluding a question from the COUNT must not make it selectable.

    The narrowing is about what bounds the buying, not about what may be bought. A direction carries
    no action owner, so nothing downstream may turn it into a Node — and if that ever changes, this
    test is where the two ideas were kept apart.
    """
    question = _question("q0")
    assert question.selection_provenance.action_source == "none"
    assert unconsumed_card_inventory(_board(question)) == 0
    # The lane's own resource envelope is irrelevant to this: measured on the live board, the raw
    # lane returned the same actions at gpu_count 0, 1, 2 and None alike.
    assert CardResourceEnvelope(gpu_count=0, gpu_memory_mib=()) is not None


def test_the_gate_reopens_as_soon_as_the_work_item_is_consumed():
    """A freed lane must admit the next prefetch — the ceiling's own promise, re-asserted.

    Its docstring says "a freed eval lane admits the held prefetch, which drops it out of
    `_speculation_depth_used`'s unconsumed count, and the very next turn elects again". With
    questions no longer inflating the count, that promise is now reachable.
    """
    board = _board(_staged_work_item("w1"), *[_question(f"q{i}") for i in range(3)])
    ceiling = min(2, card_lane_width(GreedyTree()))
    assert not SpeculationMixin._prefetch_supply_used(board) < ceiling

    # The work item is built and is no longer staged inventory.
    board.cards["w1"].status = "running"
    assert SpeculationMixin._prefetch_supply_used(board) == 0
    assert SpeculationMixin._prefetch_supply_used(board) < ceiling
