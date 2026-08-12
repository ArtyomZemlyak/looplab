"""The Researcher's authored concepts must survive the Card lane.

A node built from a native Card is born from `_rebuilt_claim_idea`, which reconstructs the executed
`Idea` from the durable `card_added` idea block and NOTHING else. That block carried five keys and
`concepts` was not one of them, so every Card-built node reached `node_created` with no concept
membership at all — and with it went `RunState.node_concepts`, the run's concept tree, the board's
`Card.concept_tags`, the memory shelf's per-record tags and `concept_run_base`'s seed.

The corpus split was total (measured 2026-08-12 over `runs/`): the one flagship run that predates
Card-driven selection (`rubertlite-dr-unified-v4`) had 11 of 11 nodes carrying authored concepts and
zero Cards; the three that use it (`rubert-dr-0807`, `-v2`, `-v5`) had 37 Card-built nodes between
them and zero carrying concepts. The live v5 Researcher demonstrably emitted three ids.

These tests drive the real mint -> real fold -> real claim, and then a real offline run, because the
whole defect was a writer and a reader that each looked correct in isolation: `card_ledger.py` has
decoded `card_added.idea["concepts"]` into `Card.concept_tags` since it shipped, and nothing wrote it.
"""
from __future__ import annotations

import anyio

from tests.factories import make_engine
from looplab.agents.roles import ToyResearcher
from looplab.core.models import Event, Idea
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED
from looplab.search.card_selection import card_action as projected_card_action

AUTHORED = ["loss/contrastive", "regularization/r-drop"]


def _idea(**overrides) -> Idea:
    base = dict(operator="draft", params={"x": 1.0}, rationale="because",
                hypothesis="R-Drop lifts recall", concept_mode="full", concepts=list(AUTHORED))
    base.update(overrides)
    return Idea(**base)


def _mint(engine, idea):
    """Mint one native Card through the real planner and append its real payload."""
    events = engine.store.read_all()
    plan = engine._plan_native_card(
        events, fold(events), idea, parents=[], parent_generations={},
        scored_against=None, source="researcher", at_node=1)
    assert plan.disposition == "mint", plan.disposition
    engine.store.append(EV_CARD_ADDED, plan.payload)
    return plan


def _claim(engine, card_id):
    """Claim that Card exactly as the build lane does, returning the Idea it will execute."""
    events = engine.store.read_all()
    state = fold(events)
    card = state.cards[card_id]
    action = projected_card_action(card)
    assert action is not None
    engine._card_claim_refusal = None
    reservation = engine._prepare_existing_card_claim(events, state, action, card, node_id=1)
    assert reservation is not None, engine._card_claim_refusal or "anonymous refusal"
    return reservation.idea, card


def test_a_claimed_card_executes_the_idea_the_researcher_authored_concepts_included(tmp_path):
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea())

    # The durable row is the only thing the claim may read, so the membership has to BE there.
    row = next(event for event in engine.store.read_all() if event.type == EV_CARD_ADDED)
    assert row.data["idea"]["concepts"] == AUTHORED

    executed, card = _claim(engine, plan.card_id)
    # The defect verbatim: this used to be `[]`, so the built node authored nothing.
    assert executed.concepts == AUTHORED
    assert executed.concept_mode == "full"
    # Everything the ownership digest DOES cover still rebuilds identically — the claim is only
    # reachable at all because `_prepare_existing_card_claim` re-proves that fixed point.
    assert executed.operator == "draft"
    assert executed.params == {"x": 1.0}
    assert executed.card_id == plan.card_id

    # And the board sees an exact proposal-time membership rather than an absent one.
    assert card.concept_tags == AUTHORED
    assert card.concept_source is not None
    assert card.concept_source.kind == "card_added"
    assert card.concept_source.membership_present is True
    assert card.concept_source.complete is True
    assert card.selection_ready is True


def test_the_membership_rides_outside_the_ownership_digest(tmp_path):
    """Concepts must not move the action identity: every already-minted Card keeps its receipt.

    `CARD_ACTION_DIGEST_V2_FIELDS` excludes concept membership, and this is what makes that a
    property of the WRITER rather than a fact about a constant: two Cards differing only in what the
    Researcher tagged are the same executable action and must mint the same digest.
    """
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    tagged = _mint(engine, _idea()).payload

    other = make_engine(tmp_path / "run2", n_seeds=0, max_nodes=4, card_driven_selection=True)
    untagged = _mint(other, _idea(concept_mode=None, concepts=[])).payload

    assert tagged["ownership_receipt"] == untagged["ownership_receipt"]
    assert "concepts" not in untagged["idea"]
    # The two idea blocks differ in exactly the one member the digest does not cover.
    assert {key: value for key, value in tagged["idea"].items() if key != "concepts"} \
        == untagged["idea"]


def test_a_delta_proposal_writes_no_membership_and_stays_selectable(tmp_path):
    """A delta is not a membership, and claiming to be one would cost the Card its selectability.

    `concept_mode`/`concepts_added`/`concepts_removed` are absent from
    `card_ledger.py::_CARD_ADDED_ACTION_FIELDS`, so putting a delta envelope in the idea block makes
    replay read the whole action as a lossy future schema. This drives that consequence directly, so
    a later "just carry the delta too" cannot land quietly: it would turn `selection_ready` False and
    stall the Card lane, which is strictly worse than the missing tags this change fixes.
    """
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(concept_mode="delta", concepts=[], concepts_added=["axis/new"]))

    row = next(event for event in engine.store.read_all() if event.type == EV_CARD_ADDED)
    assert "concepts" not in row.data["idea"]
    assert "concept_mode" not in row.data["idea"]

    executed, card = _claim(engine, plan.card_id)
    assert executed.concepts == []
    assert card.selection_ready is True

    # The guard the paragraph above describes, driven rather than asserted about: a delta envelope in
    # that block really does cost the Card its selectability.
    poisoned = []
    for index, event in enumerate(engine.store.read_all()):
        data = event.data
        if event.type == EV_CARD_ADDED:
            data = {**data, "idea": {**data["idea"], "concept_mode": "delta",
                                     "concepts_added": ["axis/new"]}}
        poisoned.append(Event(seq=index, type=event.type, data=data))
    assert fold(poisoned).cards[plan.card_id].selection_ready is False


def test_a_real_card_driven_run_gives_its_nodes_the_concepts_they_authored(tmp_path):
    """End to end, on the surface the complaint was about: `RunState.node_concepts` for node 0.

    A real engine, a real Researcher that authors a full membership, Card-driven selection on. The
    assertion is deliberately over the FOLDED read model rather than over any event, because that is
    what the UI, the concept tree and the memory shelf all read, and it is what was empty.
    """
    run_dir = tmp_path / "run"
    task_researcher = ToyResearcher({"x": (-5.0, 5.0), "y": (-5.0, 5.0)},
                                    calibration_concepts=True)
    engine = make_engine(run_dir, researcher=task_researcher, n_seeds=1, max_nodes=3,
                         card_driven_selection=True, speculation_depth=0)
    anyio.run(engine.run)

    state = fold(engine.store.read_all())
    built_from_a_card = [node for node in state.nodes.values() if node.idea.card_id is not None]
    assert built_from_a_card, "the run took no Card-driven build, so it proves nothing"
    for node in built_from_a_card:
        assert state.node_concepts.get(node.id), (
            f"node {node.id} was built from {node.idea.card_id} and authored no concepts")
        assert state.node_concept_provenance[node.id] == "researcher-authored"
    assert state.node_concepts.get(0)
