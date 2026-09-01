"""A question may sit under a broader question, and the join that says so must not invent edges.

MEASURED on e5small-dr-unified-v12 before this shipped: all 11 `hypothesis_added` rows carry
exactly `['at_node', 'concepts', 'source', 'statement']`. There is no parent field, and the only
edge any prompt asks for is experiment -> direction ("return its DIRECTION_ID in `parent_card_id`").
`Card` has carried `parent_card_id` and `child_card_ids` all along, so the model permitted a tree
the carrier could not express.

`question_parent_rows` is deliberately the TWIN of `question_concept_rows` — same positional rule,
same "checked, not trusted" contract — because a positional join spelled twice is a join that will
disagree with itself, which is the lesson `question_concept_rows`' own docstring records.
"""
from __future__ import annotations

import pytest

from looplab.core.cards import hypothesis_id
from looplab.engine.research_cadence import question_parent_rows

BROAD = "Does hard-negative mining help?"
NARROW = "Does a stronger teacher help mining?"
OTHER = "Is the 0.77 ceiling real?"


def test_a_question_names_a_sibling_of_the_same_memo_as_its_parent():
    # The common case: a memo mints a broad question and a narrower one under it. Neither exists on
    # the board yet, so the only name available is the statement itself.
    edges = question_parent_rows([BROAD, NARROW], ["", BROAD])
    assert edges == {NARROW: hypothesis_id(BROAD)}


def test_the_parent_is_resolved_to_the_boards_own_content_address():
    # Not the raw text: the board keys on `hypothesis_id`, so an edge stored as prose would join
    # against nothing. This is the assertion that would fail if the resolver stored `raw`.
    edges = question_parent_rows([BROAD, NARROW], ["", BROAD])
    assert list(edges.values()) == [hypothesis_id(BROAD)]
    assert BROAD not in edges.values()


def test_an_id_already_on_the_board_is_used_as_given():
    edges = question_parent_rows([BROAD], ["card-3"], known_ids=["card-3"])
    assert edges == {BROAD: "card-3"}


def test_a_name_that_matches_nothing_yields_no_edge():
    # Resolve, never fabricate. A wrong edge is not recoverable; an absent one is exactly the
    # behaviour every question had before this shipped.
    assert question_parent_rows([BROAD, NARROW], ["", "a question nobody asked"]) == {}
    assert question_parent_rows([BROAD], ["card-9"], known_ids=["card-3"]) == {}


def test_a_question_cannot_be_its_own_parent():
    # A row that is its own ancestor makes `card_child_rollup` recurse and renders as a cycle of one.
    assert question_parent_rows([BROAD, NARROW], [BROAD, ""]) == {}


def test_a_cycle_closed_inside_one_memo_is_dropped_whole():
    # a -> b -> a. Which member is "the wrong one" is not knowable here, so neither is kept.
    assert question_parent_rows([BROAD, NARROW], [NARROW, BROAD]) == {}


def test_a_three_step_cycle_is_dropped_too():
    assert question_parent_rows([BROAD, NARROW, OTHER], [OTHER, BROAD, NARROW]) == {}


def test_a_chain_that_does_not_close_survives():
    # The non-vacuity partner of the cycle tests: the same shape MINUS the closing edge must keep
    # both edges, or the cycle check is really just "drop every chain".
    edges = question_parent_rows([BROAD, NARROW, OTHER], ["", BROAD, NARROW])
    assert edges == {NARROW: hypothesis_id(BROAD), OTHER: hypothesis_id(NARROW)}


def test_a_blank_question_does_not_shift_the_parents_that_follow_it():
    # THE ORDER RULE, and the case that discriminates it. Blanks are skipped AFTER the index is
    # read. Filter them out first and the third question reads position 1 ("") instead of its own
    # position 2, so the edge silently disappears — the same defect `question_concept_rows` was
    # fixed for, where a question after a blank took its predecessor's concept row.
    edges = question_parent_rows(["", BROAD, NARROW], ["", "", BROAD])
    assert edges == {NARROW: hypothesis_id(BROAD)}


def test_a_short_or_missing_parents_list_is_not_an_error():
    # Checked, not trusted: the model may send fewer parents than questions, or none.
    assert question_parent_rows([BROAD, NARROW], [""]) == {}
    assert question_parent_rows([BROAD, NARROW], None) == {}
    assert question_parent_rows([BROAD, NARROW], [None, 0]) == {}


def test_no_questions_is_no_edges():
    assert question_parent_rows([], [BROAD]) == {}
    assert question_parent_rows(None, None) == {}


@pytest.mark.parametrize("parents", [["", BROAD], ["", " " + BROAD + " "]])
def test_the_parent_name_is_matched_on_the_statement_as_written_or_stripped(parents):
    # `hypothesis_id` normalizes whitespace itself, so a padded name must reach the same parent.
    assert question_parent_rows([BROAD, NARROW], parents) == {NARROW: hypothesis_id(BROAD)}
