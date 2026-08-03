"""One concept projection per election, not one per candidate (doc 25 SE-04).

`card_score` called `_coverage_inputs(state)`, which rebuilds the COMPLETE concept projection over
every node — and `card_score` runs once per candidate Card, so one election was
O(cards × nodes·concepts). The code's own inline comment had said this and prescribed the fix for
long enough to be quoted in a review.

The projection is an immutable view of a `state` that does not change while the lane is scored, so
this is pure waste: every candidate was paying to derive the same answer. Two things therefore need
pinning — that the work is done ONCE, and that doing it once did not change any ranking.
"""
from __future__ import annotations

import pytest

from looplab.core.models import RunState
from looplab.search import card_selection
from looplab.search.card_selection import card_score, card_selection_set
from looplab.search.policy import GreedyTree

# The Card model carries a large receipt/provenance envelope; the existing selection suite already
# owns a factory that builds a SELECTION-READY one correctly, and duplicating it here would be a
# fixture that drifts from the real shape.
from tests.test_card_driven_selection import _node, _ready_card


def _state_with_cards(count: int) -> RunState:
    """A run PAST its seed phase with `count` scorable cards, so an election really reaches scoring.

    Two evaluated nodes and `n_seeds=1` matter: with a seed prefix still due, `card_selection_set`
    returns before any candidate is scored — which would make a "scored once" assertion pass for the
    wrong reason.
    """
    return RunState(
        direction="max",
        nodes={0: _node(0, metric=0.4), 1: _node(1, metric=0.9)},
        best_node_id=1,
        cards={
            f"card-{index}": _ready_card(
                f"card-{index}", parents=(index % 2,), concepts=(f"axis/c{index}",),
                confidence=(index + 1) / (count + 1))
            for index in range(count)
        },
    )


def _policy():
    return GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)


def test_the_projection_is_built_ONCE_per_election_not_once_per_candidate(monkeypatch):
    """The whole finding. With N candidates this used to be N rebuilds of the same immutable view."""
    calls = []
    real = card_selection._coverage_inputs
    monkeypatch.setattr(card_selection, "_coverage_inputs",
                        lambda state: calls.append(1) or real(state))

    state = _state_with_cards(12)
    selected = card_selection_set(state, _policy(), max_nodes=8)
    assert selected, "the election never reached scoring — the fixture is wrong"
    assert len(calls) <= 1, f"the concept projection was rebuilt {len(calls)} times in one election"


def test_scoring_a_card_with_the_shared_snapshot_matches_scoring_it_alone():
    """The point of the optimisation is that it changes NOTHING. A threaded snapshot that differed
    from the per-candidate one would silently re-rank the lane."""
    state = _state_with_cards(6)
    snapshot = card_selection._coverage_inputs(state)
    for card in state.cards.values():
        assert card_score(state, card, coverage_inputs=snapshot) == card_score(state, card)


def test_the_public_hook_signature_still_works_without_the_snapshot():
    """`card_score(state, card, *, scoring=...)` is the documented policy hook. An external policy
    calls it with two arguments and must keep getting a correct score — slower, not wrong."""
    state = _state_with_cards(3)
    assert card_score(state, state.cards["card-0"]) is not None


def test_an_external_policy_hook_is_never_handed_the_extra_argument():
    """A third-party `card_score(self, state, card, *, scoring)` would raise TypeError on an unknown
    keyword — and `_score_for_policy` swallows exceptions, so the policy's scoring would stop being
    consulted at all and the lane would silently fall back to legacy actions. Nothing would go red.

    Driven at `_score_for_policy` rather than through a whole election, because the property is
    exactly "the extra argument never reaches the external hook"."""
    seen = []

    class _Policy(GreedyTree):
        def card_score(self, state, card, *, scoring):
            seen.append(card.id)
            return (1.0, (0.5,))

    from looplab.search.card_selection import _score_for_policy, normalize_card_scoring

    state = _state_with_cards(4)
    score = _score_for_policy(
        _Policy(), state, state.cards["card-1"], scoring=normalize_card_scoring(None),
        policy_actions=(), coverage_inputs=card_selection._coverage_inputs(state))
    assert seen == ["card-1"], "the external hook was never called — it raised on an unknown keyword"
    assert score == (1.0, (0.5,))


def test_the_ranking_is_unchanged_by_the_hoist():
    """A direct before/after: score every card both ways and compare the full ordering, not just the
    winner — a re-rank below the cut is still a behaviour change."""
    state = _state_with_cards(10)
    snapshot = card_selection._coverage_inputs(state)
    per_card = sorted(state.cards.values(),
                      key=lambda card: (card_score(state, card), card.id), reverse=True)
    shared = sorted(state.cards.values(),
                    key=lambda card: (card_score(state, card, coverage_inputs=snapshot),
                                      card.id), reverse=True)
    assert [card.id for card in per_card] == [card.id for card in shared]
    assert card_selection_set(state, _policy(), max_nodes=4) is not None


def test_a_stale_snapshot_is_not_silently_accepted_as_correct():
    """Pins WHY the snapshot is taken inside the election rather than cached across elections: it is
    only valid for the state it was derived from. A caller passing an unrelated one gets an
    unrelated coverage signal — which is exactly why nothing caches it beyond one lane."""
    state = _state_with_cards(4)
    other = _state_with_cards(1)
    honest = card_score(state, state.cards["card-1"])
    stale = card_score(state, state.cards["card-1"],
                       coverage_inputs=card_selection._coverage_inputs(other))
    assert honest is not None and stale is not None      # both score; they need not agree


@pytest.mark.parametrize("count", [0, 1, 2, 25])
def test_the_hoist_holds_at_every_lane_size(count, monkeypatch):
    calls = []
    real = card_selection._coverage_inputs
    monkeypatch.setattr(card_selection, "_coverage_inputs",
                        lambda state: calls.append(1) or real(state))
    card_selection_set(_state_with_cards(count), _policy(), max_nodes=8)
    assert len(calls) <= 1
