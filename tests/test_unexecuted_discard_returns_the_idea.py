"""A speculative build that never ran is not evidence, so its IDEA comes back — exactly once.

The Layer-5 refund already returns the node SLOT of a prefetch the Card freshness gate superseded
before dispatch (`tests/test_card_budget_refund.py`). It never returned the HYPOTHESIS: the discarded
node stayed in its Card's `evidence`, and three readers key on that list, so one never-executed build
retired the question three ways over — out of the Card election, out of the claimable untested-belief
feed, and INTO the proposal prompt's "do NOT propose one of these again as if it were new" block, under
a promise ("a failed experiment is re-attempted by the engine itself") that nothing keeps for a
superseded prefetch. Measured on `runs/rubertlite-dr-unified-v7`, that permanently lost five research
directions including "hard-negative mining" and "label smoothing", both from deep research.

These tests drive the real engine through a real supersede and then read the real board, and they pin
the bound that keeps the return from becoming a loop: at most ONE return per card.
"""
from __future__ import annotations

import anyio
import pytest

import looplab.engine.speculation as speculation_module
from looplab.agents import roles as roles_mod
from looplab.core.models import NodeStatus, is_unevaluated_speculative_discard
from looplab.events import card_ledger
from looplab.events.replay import fold
from looplab.search import card_selection as cs

from tests.test_card_budget_refund import _spec_node, _state
from tests.test_card_speculation_engine import (  # noqa: F401 — the receipt fixture is autouse
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _commit_speculative_node,
    _engine,
    _start,
)


def _one_card_engine(tmp_path, name: str):
    """One ready Card, so the prefetch election head is deterministic across repeated commits."""
    engine, _producer = _engine(tmp_path / name)
    engine._base_max_nodes = 8
    engine.policy.max_nodes = 8
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    return engine


def _supersede_next_prefetch(engine, monkeypatch) -> int:
    """Commit one speculative build and let the freshness gate discard it before dispatch."""
    node_id = _commit_speculative_node(engine)
    monkeypatch.setattr(
        speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False)
    assert anyio.run(engine._drop_stale_speculation) is True
    monkeypatch.undo()
    return node_id


def _board(state):
    return (
        [c.id for c in state.open_research_beliefs()],
        [c.id for c in roles_mod.attempted_board_prompt_cards(state)],
    )


# ------------------------------------------------------------------ (1) the idea comes back

def test_a_superseded_prefetch_returns_its_idea_to_selection_and_to_the_board(
    tmp_path, monkeypatch,
):
    engine = _one_card_engine(tmp_path, "returns-once")
    dropped = _supersede_next_prefetch(engine, monkeypatch)

    state = fold(engine.store.read_all())
    node = state.nodes[dropped]
    # The premise, from the same durable facts the budget refund is proven from.
    assert node.status is NodeStatus.failed
    assert node.error_reason == "superseded"
    assert node.never_evaluated is True
    assert is_unevaluated_speculative_discard(state, node) is True

    card = state.cards["card-1"]
    # The node is RECORDED — nothing is un-written — but it is not called evidence.
    assert card.discarded_nodes == [dropped]
    assert card.evidence == []
    assert card.status == "proposed"
    assert card.status_nodes == []
    assert card.verdict == "open"
    # …so the fold's own readiness pass no longer derives a terminal owner, and the blocker it used
    # to stamp for work that never happened is gone.
    assert card.selection_provenance.owner_state == "none"
    assert card.selection_blockers == []
    assert card.selection_ready is True

    # Reader 1: the Card election.
    assert cs._strictly_selection_ready(card) is True
    assert "card-1" in [c.id for c in cs.eligible_cards(state, engine.policy)]

    # Readers 2 and 3: the proposal board. It is claimable again, and the two false sentences about
    # it are gone from the prompt the Researcher actually reads.
    claimable, attempted = _board(state)
    assert claimable == ["card-1"]
    assert attempted == []
    prompt = "\n".join(roles_mod.board_prompt_lines(state))
    assert "CARD_ID=card-1" in prompt
    assert "do NOT propose one of these again as if it were new" not in prompt
    assert "re-attempted by the engine itself" not in prompt

    # THE LOSS STAYS VISIBLE ON THE WIRE. Without this the operator reads a `proposed` card with no
    # evidence — indistinguishable from one that was never built — and the record of a Developer call
    # the run paid for and threw away exists nowhere they can see it.
    from looplab.serve.public_cards import public_cards
    published = public_cards(state.cards)["card-1"]
    assert published["discarded_nodes"] == [dropped]
    assert published["evidence"] == []
    assert published["status"] == "proposed"


# ------------------------------------------------------------------ (2) the bound: it cannot loop

def test_a_second_supersede_of_the_same_card_retires_it_and_nothing_loops(
    tmp_path, monkeypatch,
):
    """The bound is ONCE per card, and this drives both sides of it through the real engine.

    One supersede is a statement about freshness at a MOMENT and says nothing about the idea, so the
    first is returned. A second is a durable, twice-repeated fact that this card cannot be built
    inside this board's rate of change — that IS information about the card, and the run stops paying
    for it. Worst case is exactly two Developer builds per card, so a returned idea cannot spin.
    """
    engine = _one_card_engine(tmp_path, "retires-on-two")

    first = _supersede_next_prefetch(engine, monkeypatch)
    returned = fold(engine.store.read_all()).cards["card-1"]
    assert returned.selection_ready is True          # the return really happened…

    second = _supersede_next_prefetch(engine, monkeypatch)   # …and it was spent here
    assert second != first

    state = fold(engine.store.read_all())
    card = state.cards["card-1"]
    assert card.discarded_nodes == sorted([first, second])
    # At two, NONE is forgiven — the count decides, never a choice of which one, which is what keeps
    # the phase order-tolerant.
    assert card.evidence == sorted([first, second])
    assert card.status == "failed"
    assert card.status_nodes == sorted([first, second])
    assert card.selection_ready is False
    assert "work_terminal" in card.selection_blockers
    assert cs._strictly_selection_ready(card) is False
    assert [c.id for c in cs.eligible_cards(state, engine.policy)] == []

    # It is off the claimable feed too, so the Researcher cannot re-mint it through the other door.
    claimable, _attempted = _board(state)
    assert claimable == []

    # A third turn buys nothing: with the only card retired the election has no head at all.
    assert engine._request_card_build() is False


# ------------------------------------------------------------------ (3) negative control

def test_an_idea_whose_node_actually_ran_is_never_returned(tmp_path, monkeypatch):
    """A prefetch whose sandbox STARTED is still stale and is still terminalized — and it keeps both
    its budget slot and its place in `evidence`. `Node.eval_started` is the durable half that survives
    the crash an in-memory in-flight set cannot, so this is the same fact, read the same way."""
    engine = _one_card_engine(tmp_path, "ran-stays-retired")
    node_id = _commit_speculative_node(engine)
    # The durable eval-START boundary: this node reached `_evaluate`.
    assert engine._record_eval_start_boundary(fold(engine.store.read_all()).nodes[node_id]) is True
    monkeypatch.setattr(
        speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False)
    assert anyio.run(engine._drop_stale_speculation) is True

    state = fold(engine.store.read_all())
    node = state.nodes[node_id]
    assert node.error_reason == "superseded"
    assert node.eval_started is True
    assert getattr(node, "never_evaluated", False) is not True
    assert is_unevaluated_speculative_discard(state, node) is False

    card = state.cards["card-1"]
    assert card.discarded_nodes == []
    assert card.evidence == [node_id]
    assert card.status == "failed"
    assert card.selection_ready is False
    assert cs._strictly_selection_ready(card) is False
    claimable, attempted = _board(state)
    assert claimable == []
    assert attempted == ["card-1"]


def test_an_ordinary_card_with_no_discard_is_untouched(tmp_path, monkeypatch):
    engine = _one_card_engine(tmp_path, "untouched")
    state = fold(engine.store.read_all())
    card = state.cards["card-1"]
    assert card.discarded_nodes == []
    assert card.evidence == []
    assert card.status == "proposed"
    assert card.selection_ready is True


# ------------------------------------------------------------------ (4) `gated` stays unreachable

def _ledger_with(*cards):
    ledger = card_ledger._CardLedger()
    for card in cards:
        ledger.cards[card.id] = card
    return ledger


def test_the_filter_never_moves_a_card_that_also_holds_real_evidence():
    """`gated` is the one lane that EXCLUDES, and nothing new may reach it.

    `_apply_card_status`'s `gated` branch fires when EVERY evidence node is trust-gated /
    breed-excluded / infeasible, so removing a proven-never-run node from a MIXED evidence set is the
    one way this phase could mint one. Requiring the discards to be the card's WHOLE evidence set
    makes the only reachable transition `failed` -> `proposed` (the empty-`ev_nodes` branch), which is
    a property rather than an argument. Driven here on the phase itself.
    """
    discard = _spec_node(0, card_id="card-a")
    real = _spec_node(1, card_id="card-a", never_evaluated=False, eval_started=True,
                      status=NodeStatus.evaluated, reason=None, error="")
    state = _state(discard, real)
    assert is_unevaluated_speculative_discard(state, discard) is True
    assert is_unevaluated_speculative_discard(state, real) is False

    card = card_ledger.Card(id="card-a", statement="s", seed_statement="s",
                            evidence=[0, 1], status="evaluated")
    card_ledger._apply_unexecuted_discards(state, _ledger_with(card))

    assert card.discarded_nodes == [0]      # the loss is still VISIBLE…
    assert card.evidence == [0, 1]          # …and the mixed set is left exactly as it was
    assert card.status == "evaluated"


def test_the_phase_is_a_count_not_a_choice_and_is_order_tolerant():
    """Two discards on one card forgive NEITHER, whichever order the nodes are seen in."""
    for evidence in ([0, 1], [1, 0]):
        state = _state(_spec_node(0, card_id="card-a"), _spec_node(1, card_id="card-a"))
        card = card_ledger.Card(id="card-a", statement="s", seed_statement="s",
                                evidence=list(evidence))
        card_ledger._apply_unexecuted_discards(state, _ledger_with(card))
        assert card.discarded_nodes == [0, 1]
        assert card.evidence == list(evidence)


# ------------------------------------------------------------------ (5) attribution is not evidence

def test_a_returned_cards_research_origin_and_footprint_survive_the_return(
    tmp_path, monkeypatch,
):
    """Taking the node out of `evidence` must not un-home the signals its BUILD produced.

    The enrichment pass homes a node's footprint and research memo onto the card by walking
    `Card.evidence`; a return that dropped the id there would silently strip the returned card of the
    deep-research memo it was proposed from — which on `runs/rubertlite-dr-unified-v7` is card-3's
    `memo:sha256:10f4085b…`, the "hard-negative mining" direction. Attribution is not evidence, so
    both readers walk `evidence + discarded_nodes`.
    """
    engine = _one_card_engine(tmp_path, "origin-survives")
    node_id = _commit_speculative_node(engine)
    node = fold(engine.store.read_all()).nodes[node_id]
    node.idea.footprint = {"gpus": 1}
    node.research_origin = {"memo_id": "memo:sha256:" + "c" * 64}
    monkeypatch.setattr(
        speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False)
    assert anyio.run(engine._drop_stale_speculation) is True

    state = fold(engine.store.read_all())
    # Re-attach the two sidecar facts to the FOLDED node and re-derive, which is what a real log with
    # a footprint/memo on `node_created` produces.
    state.nodes[node_id].idea.footprint = {"gpus": 1}
    state.nodes[node_id].research_origin = {"memo_id": "memo:sha256:" + "c" * 64}
    card_ledger.derive_cards(state)

    card = state.cards["card-1"]
    assert card.evidence == []
    assert card.discarded_nodes == [node_id]
    assert card.footprint == {"gpus": 1, "proposed_by": "researcher"}
    assert card.research_origin == "memo:sha256:" + "c" * 64
