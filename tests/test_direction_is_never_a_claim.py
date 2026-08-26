"""A direction is a question. Nothing may claim it as work — and filing under one must survive.

THE DEFECT, driven against the tree at 7d406cc2. `bind_idea_to_board_card` resolves two independent
edges against the same visible board: `card_id` (a CLAIM — "this experiment IS that row") and
`parent_card_id` (a FILING — "this experiment answers that question"). Its own docstring says a
direction "is exactly the row a `card_id` claim must NOT resolve to", and a review note in the body
spelled out the fix. Neither was enforced: visibility was the only test, on BOTH resolution paths.

Two ways in, and the second fires on a WELL-BEHAVED proposal:

  A) the model puts a DIRECTION_ID in `card_id` — the prompt forbids it, but nothing stopped it.
     The direction bound, and the claim path overwrote the proposal's own `hypothesis` with the
     direction's broad seed statement, so the experiment's actual claim was destroyed too.

  B) the SEED FALLBACK. The direction block asks the model to "propose ONE concrete minimal-change
     experiment that would move it forward and return its DIRECTION_ID in `parent_card_id`". A model
     that does precisely that, and echoes the direction's wording as its `hypothesis`, matched the
     direction as `chosen` — and the self-edge guard, seeing `parent.id == chosen.id`, then NULLED
     THE PARENT. Measured: `parent_card_id="card-7"` in, `card_id='card-7' parent_card_id=None` out.
     The filing became a claim on the question, and the direction->experiment edge (#66, confirmed
     live on v7) died on the one path the prompt actively invites.

THE FIX IS ORDER-SENSITIVE, which is why both halves of B are asserted here: nulling `chosen` has to
happen after both resolution paths and BEFORE the self-edge comparison. Null it afterwards and the
parent is already gone.

MUTATION for every assertion below: delete the `if chosen is not None and card_is_direction(chosen)`
guard in `agents/roles.py::bind_idea_to_board_card`. A, B and the ordering assertion go red; the two
regressions stay green, which is what makes them worth keeping.
"""
from __future__ import annotations

from looplab.agents.roles import bind_idea_to_board_card
from looplab.core.cards import Card, CardSelectionProvenance
from looplab.core.models import Idea

_QUESTION = "Does contrastive temperature drive the plateau?"


def _direction(cid: str = "card-7") -> Card:
    """A row that owns NO action — `card_is_direction` reads exactly this."""
    return Card(id=cid, statement=_QUESTION, seed_statement=_QUESTION,
                selection_provenance=CardSelectionProvenance(
                    action_owner_count=0, action_source="none"))


def _work_item(cid: str = "card-9") -> Card:
    statement = "temperature 0.01 beats 0.07"
    return Card(id=cid, statement=statement, seed_statement=statement,
                selection_provenance=CardSelectionProvenance(
                    action_owner_count=1, action_source="card_added"))


def test_a_DIRECTION_ID_in_card_id_is_refused():
    """Path A: the id lookup must not hand back an action-less card."""
    idea = Idea(operator="tune", card_id="card-7", hypothesis="lower the temperature to 0.01")
    out = bind_idea_to_board_card(idea, [_direction()])

    assert out.card_id is None, (
        "MUTATION: drop the card_is_direction guard and this binds card-7 — the engine is handed a "
        "work item that owns no action and can never be built")
    assert out.hypothesis == "lower the temperature to 0.01", (
        "the claim path overwrites `hypothesis` with the card's seed statement; refusing the claim "
        "must leave the proposal's OWN hypothesis intact, not the direction's broad question")


def test_filing_under_a_direction_SURVIVES_the_seed_fallback():
    """Path B, the compliant proposal — and the reason the guard sits before the self-edge test."""
    idea = Idea(operator="tune", hypothesis=_QUESTION, parent_card_id="card-7")
    out = bind_idea_to_board_card(idea, [_direction()])

    assert out.parent_card_id == "card-7", (
        "MUTATION: drop the guard and the seed fallback selects the direction as `chosen`, the "
        "self-edge test then nulls the parent, and the direction->experiment edge is destroyed on a "
        "proposal that did exactly what the prompt asked")
    assert out.card_id is None


def test_the_guard_runs_BEFORE_the_self_edge_test():
    """Ordering, stated as behaviour: the same input cannot yield both a null parent and a claim."""
    idea = Idea(operator="tune", hypothesis=_QUESTION, parent_card_id="card-7")
    out = bind_idea_to_board_card(idea, [_direction()])

    assert not (out.card_id == "card-7" and out.parent_card_id is None), (
        "this is the exact measured output of the unfixed function; a guard placed AFTER the "
        "self-edge comparison would null the claim but leave the parent already gone")
    assert (out.card_id, out.parent_card_id) == (None, "card-7")


def test_a_real_work_item_still_binds_and_still_restores_its_seed():
    """REGRESSION: the guard must key on kind, never on visibility."""
    idea = Idea(operator="tune", card_id="card-9", hypothesis="a paraphrase of the row")
    out = bind_idea_to_board_card(idea, [_direction(), _work_item()])

    assert out.card_id == "card-9"
    assert out.hypothesis == "temperature 0.01 beats 0.07", (
        "MUTATION: widen the guard to null every `chosen` and this goes red — the immutable seed is "
        "no longer restored and paraphrase becomes semantic identity")


def test_a_self_edge_on_a_work_item_is_still_nulled():
    """REGRESSION: the self-edge guard is not what was wrong, and must keep firing."""
    idea = Idea(operator="tune", card_id="card-9", parent_card_id="card-9", hypothesis="x")
    out = bind_idea_to_board_card(idea, [_work_item()])

    assert out.card_id == "card-9"
    assert out.parent_card_id is None, (
        "MUTATION: remove the `parent.id == chosen.id` test and this goes red — a card would name "
        "itself as its own parent and the fold would drop the edge silently")


def test_an_unknown_id_is_still_nulled_on_both_edges():
    """REGRESSION: the pre-existing visibility rule is untouched by the kind rule."""
    idea = Idea(operator="tune", card_id="card-404", parent_card_id="card-405", hypothesis="x")
    out = bind_idea_to_board_card(idea, [_direction(), _work_item()])

    assert (out.card_id, out.parent_card_id) == (None, None)
