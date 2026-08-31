"""`card_kind_of` and `is_pure_belief` apply the same TEST and are not the same CALL.

Both ask action ownership — `selection_provenance.action_source != "none"`. `card_kind_of`'s
docstring once claimed they were one call, retracted it, and named the cost of the claim: "a reader
trusts the claim and stops checking". The RETRACTION never reached the `card_kind` FIELD comment,
which is the wire contract readers actually open and therefore the copy most likely to be trusted.

WHERE THEY DIVERGE, and it is not hypothetical: with `selection_provenance` MISSING,
`is_pure_belief` reads `getattr(None, "action_source", "none") == "none"` and answers "a belief" —
a direction — while `card_kind_of` answers `experiment`. That asymmetry is deliberate and each side
is right for its own job: mislabelling work as a direction HIDES it from the work accounting, while
the reverse merely renders a question in the wrong column.

These tests exist so the equivalence cannot be re-asserted without a red, and so that unifying the
two later is a deliberate change with a failing test to update rather than a silent one.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from looplab.core.cards import CARD_KIND_DIRECTION, CARD_KIND_EXPERIMENT, card_kind_of
from looplab.engine.research_cadence import is_pure_belief


def _card(action_source):
    """`selection_provenance=None` is the divergent case; a string is the ordinary one."""
    if action_source is _MISSING:
        return SimpleNamespace(selection_provenance=None)
    return SimpleNamespace(selection_provenance=SimpleNamespace(action_source=action_source))


_MISSING = object()


def test_they_AGREE_when_the_card_owns_an_action():
    """Mutation: make `card_kind_of` read readiness instead of ownership and a blocked experiment
    is re-labelled a direction — the confusion the function exists to end."""
    card = _card("card")
    assert card_kind_of(card) == CARD_KIND_EXPERIMENT
    assert is_pure_belief(card) is False


def test_they_AGREE_when_the_card_owns_none():
    """Mutation: treat the literal "none" as ownership and every research question becomes a work
    item the board must schedule."""
    card = _card("none")
    assert card_kind_of(card) == CARD_KIND_DIRECTION
    assert is_pure_belief(card) is True


def test_they_DISAGREE_on_a_MISSING_provenance_and_that_is_the_point():
    """THE RETRACTED EQUIVALENCE, driven. Each side is right for its own job, and a reader told they
    are one call would never check this case.

    Mutation: make `card_kind_of` answer `direction` for a missing provenance — 'unifying' them by
    moving the conservative side — and a work item with no recorded provenance silently drops out of
    the work accounting, which is the direction of harm the docstring rules out."""
    card = _card(_MISSING)
    assert card_kind_of(card) == CARD_KIND_EXPERIMENT, (
        "the wire side must stay conservative: mislabelling work as a direction hides it")
    assert is_pure_belief(card) is True, (
        "the append-site gate reads `action_source` off a missing object and gets 'none'")


def test_the_field_comment_no_longer_asserts_ONE_call():
    """A NEGATIVE source pin, which CLAUDE.md keeps as substrings on purpose: what must not come back
    is the TEXT. The comment is the wire contract readers open, so the retraction has to live there
    and not only in the docstring one screen away.

    Mutation: restore "This is the SAME predicate ... read ONE spelling"."""
    from looplab.core import cards

    src = inspect.getsource(cards)
    assert "read ONE\n    # spelling" not in src, "the retracted equivalence must not return"
    assert "THEY ARE NOT ONE\n    # CALL" in src, (
        "and the field comment must SAY so — deleting the false claim without stating the truth "
        "leaves the next reader to rediscover the divergence")
