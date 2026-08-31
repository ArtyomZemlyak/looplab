"""A rendered direction says WHICH of the memo's two lists it came from.

The prompt asks the model to split what it would try next into `open_questions` (a family of
experiments) and `next_experiments` (ONE concrete change each), and then to "also fill
`recommended_directions` with the union of both, UNCHANGED". Measured on the first memo of
`e5small-dr-unified-v11` (2026-08-29): `recommended_directions == next_experiments + open_questions`
exactly, in order, 6/6 and 4/4 verbatim. The model complied — and complying is what made the
rendered list unreadable, because a concrete one-change experiment was presented identically to an
open family.

IT COST A REAL MISREADING. v11's card-0 matched `next_experiments[1]`, which looked like the first
live evidence that the concrete half of the split reaches a proposal — and it matched
`recommended_directions[1]` byte-for-byte, because d1 IS n1. No observation of the card could name
the carrier (#120).

Membership recovers it precisely BECAUSE the union is verbatim. An entry in BOTH lists or in
NEITHER is left unlabelled — a refusal that records itself rather than a guess.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.tools.run_tools import memo_direction_kind, memo_direction_row

_Q = ["Does training the e5-small backbone substantially longer help?"]
_X = ["Sweep n_epochs in {6, 10} on the applied champion config"]


def test_an_entry_in_only_the_experiments_list_is_an_experiment():
    """The defect's own case: v11's d1 IS n1, and the render could not say so. Mutation: return
    `None` for a one-list hit and every row goes back to being indistinguishable."""
    assert memo_direction_kind(_X[0], _Q, _X) == "experiment"
    assert memo_direction_row(_X[0], _Q, _X) == f"  - [experiment] {_X[0]}"


def test_an_entry_in_only_the_questions_list_is_a_question():
    assert memo_direction_kind(_Q[0], _Q, _X) == "question"
    assert memo_direction_row(_Q[0], _Q, _X) == f"  - [question] {_Q[0]}"


def test_an_entry_in_BOTH_lists_is_left_UNLABELLED():
    """A model that repeated itself across the split has said nothing about which list owns the
    entry. Mutation: check `in_x` first and return `experiment`, and the render asserts a
    provenance the memo does not support — the guess this whole helper exists to refuse."""
    both = "Sweep n_epochs in {6, 10} on the applied champion config"
    assert memo_direction_kind(both, [both], [both]) is None
    assert memo_direction_row(both, [both], [both]) == f"  - {both}"


def test_an_entry_in_NEITHER_list_is_left_UNLABELLED():
    """A legacy compat-only memo, or a model that edited the text on its way into the union.
    Mutation: default the marker to `[question]` and every preserved memo on this box gains a
    provenance claim its payload never made."""
    stray = "Something only the compatibility field carries"
    assert memo_direction_kind(stray, _Q, _X) is None
    assert memo_direction_row(stray, _Q, _X) == f"  - {stray}"


def test_matching_is_EXACT_not_a_prefix_or_substring_ON_BOTH_SIDES():
    """The union is verbatim, so equality is the whole rule. Mutation: match with `in`/startswith on
    EITHER side and an entry that merely quotes a list member's opening words is filed as that
    member — precisely the prose pattern-match that produced the misreading in the first place.

    BOTH sides are driven deliberately: an earlier cut of this test probed only the experiments
    list, and a mutant that loosened the QUESTIONS comparison survived it."""
    assert memo_direction_kind(_X[0][:20], _Q, _X) is None, (
        "a PREFIX of an experiment is not that experiment; only a verbatim member is")
    assert memo_direction_kind(_Q[0][:20], _Q, _X) is None, (
        "and the same on the questions side — the comparison is loosened one list at a time")
    # The containment can point the other way too: a LONGER entry that CONTAINS a member.
    assert memo_direction_kind(_X[0] + " and also re-mine", _Q, _X) is None
    assert memo_direction_kind(_Q[0] + " under distillation", _Q, _X) is None


def test_whitespace_is_normalized_on_both_sides():
    """The memo's own lists and the compat field are separately stripped by the caller, so a stray
    newline must not split one entry into two identities. Mutation: drop the `.strip()` and a
    trailing space makes a real experiment unlabelled."""
    assert memo_direction_kind("  " + _X[0] + "\n", _Q, _X) == "experiment"
    assert memo_direction_kind(_X[0], _Q, ["  " + _X[0] + "  "]) == "experiment"


def test_a_blank_entry_is_never_labelled():
    """Mutation: drop the empty guard and a blank row matches a blank list member, printing a
    marker attached to nothing.

    THE ONE-SIDED BLANK IS THE CASE THAT MATTERS, and an earlier cut of this test missed it: with
    a blank in BOTH lists the both-lists rule already answers `None`, so the mutant survived. It is
    a blank in exactly ONE list that reaches the label — a memo carrying `open_questions: [""]` is
    real (the model emitted an empty slot), and without the guard the blank row renders
    `- [question]` attached to nothing."""
    assert memo_direction_kind("", _Q, _X) is None
    assert memo_direction_kind("   ", [""], [""]) is None
    assert memo_direction_kind("", [""], _X) is None, (
        "a blank matching a blank QUESTION must still be unlabelled")
    assert memo_direction_kind("   ", _Q, ["  "]) is None, (
        "and the same for a blank EXPERIMENT slot")
    assert memo_direction_row("", [""], _X) == "  - ", (
        "and the row carries no marker either")
