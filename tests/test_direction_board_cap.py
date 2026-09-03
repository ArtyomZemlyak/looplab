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

import types

from looplab.engine.research_cadence import (classify_research_beliefs,
                                            DEEP_RESEARCH_OPEN_BELIEF_CAP,
                                             admit_research_beliefs, is_pure_belief)
from looplab.engine import research_cadence as rc
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
    IT STOPPED MIRRORING A SECOND TIME, the same way — the purity filter moved INSIDE the collapse
    (a comprehension after it DELETES a belief group rather than narrowing it; see
    `RunState.open_research_beliefs`) — and a mutation pass proved the drift is not fixable by
    re-deriving: reverting production left every test in this file green, because they run through
    a copy of it. So this no longer mirrors production, it CALLS it. `_admissible_beliefs` reads
    exactly one thing off `self` (`self.store.read_all()`) and hands it to the module-level `fold`,
    so a stub engine plus the documented `research_cadence.fold` seam is the whole harness — the
    same one `test_dropped_directions_reach_the_operator.py` drives the log line through. A helper
    that IS the production expression cannot drift from it.
    """
    # The double carries the REAL `_record_belief_admission`, bound to a store that records rather
    # than a stub that swallows: `_admissible_beliefs` calls it on every pass, and a double that
    # stubbed it would let this file keep passing while the row it appends stopped being written.
    # That is what stranded these five tests when the method landed — the double had no such
    # attribute and the call raised before the cap was ever consulted.
    appended: list[tuple] = []
    engine = types.SimpleNamespace(store=types.SimpleNamespace(
        read_all=lambda: [], append=lambda etype, data, **kw: appended.append((etype, data))))
    engine._record_belief_admission = types.MethodType(
        rc.ResearchCadenceMixin._record_belief_admission, engine)
    original = rc.fold
    rc.fold = lambda _events: st
    try:
        out = rc.ResearchCadenceMixin._admissible_beliefs(engine, directions)
    finally:
        rc.fold = original
    assert [etype for etype, _ in appended] == ["belief_admission"], (
        "every pass records what the board did with the memo's directions")
    return out


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


# --------------------------------------------------------------------------------------------
# Filtering BEFORE the collapse, and WHY each direction was dropped. Two defects one call apart:
# the first deleted beliefs the board still held, the second blamed the board for the memo's noise.
# --------------------------------------------------------------------------------------------

def _work_item(cid: str, statement: str, parent: str | None = None) -> Card:
    """An open card that OWNS an action — a work item, not a pure belief."""
    return Card(id=cid, statement=statement, seed_statement=statement, parent_card_id=parent,
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))


def test_a_belief_whose_FIRST_card_owns_an_action_is_narrowed_and_not_DELETED():
    """`open_research_beliefs()` elects the FIRST no-evidence card of each belief and includes work
    items, so filtering for purity afterwards drops the elected card and never reaches the pure
    sibling that shares its wording — the belief leaves the dedup universe entirely.

    NON-VACUITY: the work item is inserted FIRST, which is the only ordering under which the two
    spellings differ; `_admissible` is the production expression, and the assertion below is that a
    restatement of a question the board still holds is still refused.
    """
    statement = "distil from a teacher"
    st = _board([_work_item("w0", statement), _direction("d0", statement)])
    # The pre-fix spelling: filter after the collapse. The belief vanishes …
    assert [c.id for c in st.open_research_beliefs() if is_pure_belief(c)] == []
    # … and the fixed one narrows the group to its pure member instead.
    assert [c.id for c in st.open_research_beliefs(only=is_pure_belief)] == ["d0"]
    assert _admissible(st, [statement.upper() + "  "]) == [], (
        "a question the board is already carrying was admitted a SECOND time, which is how a "
        "direction that never accrues evidence grows the open population without bound")


def test_the_default_collapse_is_UNCHANGED_for_every_other_consumer():
    """`only=None` is byte-for-byte the historical behaviour — the proposal feed and the foresight
    ranking consume this and legitimately want work items in it."""
    st = _board([_work_item("w0", "a"), _direction("d1", "b")])
    assert [c.id for c in st.open_research_beliefs()] == ["w0", "d1"]


def test_the_memos_OWN_repeat_is_not_charged_to_the_board():
    """`dropped` was `len(directions) - len(admitted)`, so four unrelated causes arrived as one
    number the caller then explained with the board and the cap. Two of the four are facts about
    the MEMO and the board refused nothing."""
    verdict = classify_research_beliefs([], ["mine harder negatives", "MINE HARDER NEGATIVES",
                                             "", None], counted=[])
    assert verdict.admitted == ["mine harder negatives"]
    assert (verdict.repeated, verdict.blank) == (1, 2)   # `""` and `None` are both blank
    assert (verdict.restated, verdict.capped) == (0, 0), (
        "an empty board and an unbound cap refused nothing; charging them is the alarm this fixes")
    assert verdict.refused == 0 and verdict.dropped == 3, (
        "`dropped` stays the historical total; `refused` is the half a board/cap account explains")
    assert verdict.reasons() == "1 the memo repeated itself, 2 blank"


def test_the_board_and_the_cap_are_still_named_when_they_ARE_the_reason():
    """The complement: the two causes that ARE the board's, reported as such."""
    full = [f"direction {i}" for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    verdict = classify_research_beliefs(full, [full[0], "a genuinely new question"], counted=full)
    assert (verdict.restated, verdict.capped) == (1, 1)
    assert verdict.reasons() == "1 already on the board, 1 no room"
    assert verdict.admitted == []


def test_the_tail_after_the_cap_binds_is_CLASSIFIED_and_not_charged_to_the_cap():
    """The rule used to `break`, so everything after the cap bound went unexamined and was charged
    to it by subtraction. Admission is identical either way — `occupied` only grows — so the only
    thing the `break` bought was a wrong account of the tail."""
    full = [f"direction {i}" for i in range(DEEP_RESEARCH_OPEN_BELIEF_CAP)]
    verdict = classify_research_beliefs(full, ["new one", "new one", "", full[0]], counted=full)
    assert verdict.admitted == []
    assert (verdict.capped, verdict.repeated, verdict.blank, verdict.restated) == (1, 1, 1, 1)


def test_reasons_names_only_what_FIRED():
    """A line that always lists four numbers, three of them zero, is the wall of text every bounded
    output rule in this repo refuses."""
    assert classify_research_beliefs([], ["a"], counted=[]).reasons() == ""
