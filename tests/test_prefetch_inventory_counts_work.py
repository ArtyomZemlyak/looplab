"""A research QUESTION is not prefetch inventory, and counting it as such idles half the box.

MEASURED end to end on the live `runs/e5small-dr-unified-v6` (greedy, `eval_parallel` settled to 2,
seven hours in, GPU 0 at 100% and GPU 1 at 0% for all of it):

    card_lane_width(GreedyTree())                            = 1
    _speculative_prefetch_ceiling() = min(depth 2, lane 1)   = 1
    _speculation_depth_used(state, consumed_inflight={(0,0)}) = 0   <- admission works
    unconsumed_card_inventory(state, exclude=…)               = 6   <- the six QUESTIONS
    _prefetch_supply_used                                     = 6
    guard `speculation.py::_prefetch_supply_used`:  6 < 1     -> False

So the raw lane — the only producer that can fill a second evaluation lane — is refused on every
turn from the moment the opening memo registers its questions, which is t+12.6m on that run. The
board's ENTIRE supply count was six cards that no build could ever consume.

WHY THEY PASS THE COUNTER: `unconsumed_card_inventory` keys on the board's terminal vocabulary —
`status == "proposed"`, `verdict == "open"`, no `dropped_reason`, no `merged_into`. That describes a
research question perfectly and says nothing about whether anything can BUILD it. All six carried
`action_owner_missing`.

THE PREMISE THAT WENT FALSE, in that function's own docstring: *"Over-counting only makes the run
prefetch LESS, which is the safe direction; under-counting is the bug being fixed."* True when
written — before directions became first-class board rows — and false now: over-counting by six does
not prefetch less, it prefetches never, and the count grows with every later memo.

BY KIND, NEVER BY READINESS. The same docstring is explicit that this count is "deliberately coarser
than `_strictly_selection_ready`" and that the coarseness is load-bearing: a work item failing a
freshness fence this turn is still inventory the board holds. Narrowing on readiness would
reintroduce the v4 defect the counter exists for; narrowing on KIND removes only rows that are not
work at all.
"""
from __future__ import annotations

from looplab.core.cards import Card, CardSelectionProvenance
from looplab.core.models import RunState
from looplab.search.card_selection import unconsumed_card_inventory


def _question(cid: str) -> Card:
    """A deep-research direction: no action owner, so nothing can ever build it.

    `card_kind_of` reads the kind from ACTION OWNERSHIP — an absent/`none` `action_source` is a
    direction — which is the same rule the board, the fold and the UI use.
    """
    return Card(id=cid, statement=cid, seed_statement=cid, status="proposed", verdict="open",
                selection_provenance=CardSelectionProvenance(action_source="none"))


def _work_item(cid: str, *, status: str = "proposed") -> Card:
    return Card(id=cid, statement=cid, seed_statement=cid, status=status, verdict="open",
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))


def _board(*cards: Card) -> RunState:
    state = RunState(run_id="r", task_id="t", direction="max")
    state.cards = {card.id: card for card in cards}
    return state


def test_the_live_v6_board_counts_ZERO_and_not_six():
    """The exact shape that idled a two-GPU box for seven hours.

    One experiment already running (so it is not `proposed` and never counted) plus the six
    questions the memos registered. Supply must be 0 so that `0 < ceiling 1` holds and the raw lane
    may produce; at 6 the gate is shut for the life of the run.
    """
    board = _board(
        _work_item("card-0", status="running"),
        *[_question(f"q{i}") for i in range(6)],
    )
    assert unconsumed_card_inventory(board) == 0, (
        "MUTATION: drop the kind clause and this reads 6 — which is the state that refused every "
        "prefetch on runs/e5small-dr-unified-v6 from t+12.6m onward")


def test_a_WORK_ITEM_waiting_to_be_built_still_counts():
    """The v4 defect the counter exists for must not come back.

    Measured then: `_speculation_depth_used` returned 1 against a ceiling of 2 while a large backlog
    of staged cards sat unbuilt, so minting stayed legal forever. A staged work item is exactly the
    inventory that has to stop the buying.
    """
    board = _board(_work_item("w1"), _work_item("w2"), _question("q1"))
    assert unconsumed_card_inventory(board) == 2, (
        "MUTATION: exclude everything, or key the clause on readiness, and this goes red")


def test_the_narrowing_is_by_KIND_and_not_by_READINESS():
    """A work item that could not be claimed THIS turn is still inventory the board holds.

    The counter is documented as deliberately coarser than `_strictly_selection_ready`; a blocker or
    a stale generation must not remove a row from the count, or the ceiling stops bounding the thing
    it exists to bound.
    """
    unready = _work_item("w1")
    unready.selection_ready = False
    unready.selection_blockers = ["freshness_unknown"]
    assert unconsumed_card_inventory(_board(unready)) == 1, (
        "MUTATION: swap the kind test for `_strictly_selection_ready` and this goes red")


def test_the_caller_s_own_half_is_still_excluded():
    """`exclude` is what the caller already counted; the two halves must not double-count."""
    board = _board(_work_item("w1"), _work_item("w2"))
    assert unconsumed_card_inventory(board, exclude={"w1"}) == 1


def test_terminal_rows_are_still_skipped_for_their_own_reasons():
    """Each terminal clause is separate from the kind clause and must survive it."""
    built = _work_item("built", status="building")
    dropped = _work_item("dropped")
    dropped.dropped_reason = "operator dropped"
    merged = _work_item("merged")
    merged.merged_into = "w1"
    decided = _work_item("decided")
    decided.verdict = "supported"
    assert unconsumed_card_inventory(_board(built, dropped, merged, decided)) == 0


def test_a_board_of_questions_ALONE_is_empty_inventory():
    """The opening minutes of every run: five or six questions and no experiment yet.

    Across the box the question count per run is 3-7 (v2 5, v3 4, v4 5, v6 6, rl-v6 7, rl-v7 3,
    rl-v8 5, rl-v9 5), so before this change every run began by filling its own prefetch ceiling
    with rows nothing could build.
    """
    assert unconsumed_card_inventory(_board(*[_question(f"q{i}") for i in range(7)])) == 0
