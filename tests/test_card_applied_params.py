"""A card publishes the coordinates its experiment RAN at, beside the ones it proposed.

`Card.params` is receipt-bound: it is inside the action digest, so correcting it would unmake the
card's identity, and that is right — the receipt records what was PROPOSED. What was wrong is that
nothing beside it recorded what the experiment then ran at. Under `params_style: "none"` the engine
applies nothing and the Developer realises the idea by editing the repo, so a repair that fits a
training into memory moves the numbers while the proposal stays frozen.

Measured over the corpus: 457 comparisons, 41 diverged, 18 of them on nodes that produced a metric.
Re-measured on `runs/e5small-dr-unified-v4` through this very fold: NINE cards carry an applied
record and SIX of them disagree with their own proposal — including the run's CHAMPION, card-132 /
node 13 (0.793411), whose card says batch 4096 / lr 0.001 / 3 epochs against a node that ran
2048 / 0.0005 / ONE epoch.

The node-level readers were fixed in August. This is the card level, which is the row an operator
and the Researcher both reason about.
"""
from __future__ import annotations

from looplab.core.models import Card, Idea, Node, NodeStatus, RunState
from looplab.events.card_ledger import _apply_card_applied_params, _CardLedger


def _node(node_id: int, applied: dict | None, *, status=NodeStatus.evaluated) -> Node:
    provenance = {"applied_params": {"applied": dict(applied)}} if applied is not None else {}
    return Node(id=node_id, operator="draft", idea=Idea(operator="draft"), status=status,
                metric_provenance=provenance)


def _fold(card: Card, nodes: list[Node]) -> Card:
    st = RunState(goal="g", direction="max", nodes={n.id: n for n in nodes})
    ledger = _CardLedger(cards={card.id: card})
    _apply_card_applied_params(st, ledger)
    return ledger.cards[card.id]


def _card(**kw) -> Card:
    return Card(id="card-1", statement="s", seed_statement="s", **kw)


def test_the_card_publishes_what_ran_and_names_the_node_it_ran_on():
    card = _fold(_card(evidence=[4], params={"lr": 0.001}), [_node(4, {"lr": 0.0005})])
    assert card.applied_params == {"lr": 0.0005}
    assert card.applied_params_node == 4, (
        "a claim about coordinates must name the experiment it is a claim about")
    assert card.params == {"lr": 0.001}, "the receipt-bound proposal is never rewritten"


def test_the_LATEST_evaluated_node_wins_and_nothing_is_merged():
    """Merging two evidence nodes' maps would mint a coordinate set no single run ever occupied —
    the same fabrication as reading the proposal, one step subtler. The mutation this refuses:
    accumulating across evidence instead of stopping at the first usable node."""
    card = _fold(_card(evidence=[2, 7]),
                 [_node(2, {"lr": 0.001, "epochs": 3.0}), _node(7, {"lr": 0.0005})])
    assert card.applied_params == {"lr": 0.0005}
    assert card.applied_params_node == 7
    assert "epochs" not in card.applied_params, "no composite of two runs may be published"


def test_a_pending_node_is_not_evidence_of_coordinates():
    """A node that has not finished has not run at anything. The mutation this refuses: dropping the
    status check, which publishes a half-built node's numbers as what the card ran."""
    card = _fold(_card(evidence=[9]), [_node(9, {"lr": 0.1}, status=NodeStatus.pending)])
    assert card.applied_params == {} and card.applied_params_node is None


def test_it_skips_past_an_evaluated_node_that_recorded_nothing():
    """A node whose metric was never bound has no applied record; the card should still be able to
    speak for an OLDER node that does. It stops at the first node with a usable record, newest
    first, so a run whose latest attempt lost its record is not silenced entirely."""
    card = _fold(_card(evidence=[1, 5]), [_node(1, {"lr": 0.002}), _node(5, None)])
    assert card.applied_params == {"lr": 0.002}
    assert card.applied_params_node == 1


def test_an_empty_map_means_NOT_RECORDED_and_never_agreement():
    """A pre-2026-08-20 node, or one whose metric was never bound, publishes nothing. The field's
    whole purpose is to be distinguishable from the declaration, so falling back to the declaration
    — which `effective_params` deliberately does for its own readers — would make every legacy card
    silently assert that it ran exactly as proposed."""
    card = _fold(_card(evidence=[3], params={"lr": 0.001}), [_node(3, None)])
    assert card.applied_params == {}
    assert card.applied_params_node is None


def test_a_card_with_no_evidence_publishes_nothing():
    assert _fold(_card(params={"lr": 1.0}), []).applied_params == {}


def test_the_derivation_is_reset_before_it_is_rewritten():
    """These are derived overlays. A card that lost its usable evidence this fold must not keep the
    numbers it published last fold — a stale coordinate claim is invisible once written."""
    stale = _card(evidence=[3], applied_params={"lr": 9.9}, applied_params_node=99)
    card = _fold(stale, [_node(3, None)])
    assert card.applied_params == {} and card.applied_params_node is None
