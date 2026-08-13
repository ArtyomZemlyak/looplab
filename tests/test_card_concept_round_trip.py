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

THE SECOND HALF (this change). `2acdb825` carried a `full` membership only. A `delta` proposal —
`concept_mode="delta"` plus `concepts_added`/`concepts_removed`, which is what the Researcher emits
for every non-root node once a parent membership exists — still reached `card_added` with nothing,
because those three keys were not in `card_ledger.py::_CARD_ADDED_ACTION_FIELDS` and adding them made
replay read the whole action as a lossy future schema, costing the Card its `selection_ready`.
Measured live on `runs/rubertlite-dr-unified-v6`: node 0 (full) carries two ids; nodes 1-4 carry no
concept envelope AT ALL, and seven of that run's proposals emitted `"concept_mode": "delta"`.

A delta is a delta against an inheritance base — the run base (`EV_RUN_CONCEPTS`) at a root, else the
union of the node's parents' effective memberships — and that base exists only in FOLDED state, so it
is resolved by `replay.py::_materialize_concept_deltas` over the whole DAG and never at mint time.
The Card therefore carries the delta UNRESOLVED and the claim rebuilds it onto the executed Idea, so
a Card-built node folds through exactly the same resolver as an unmediated proposal. The allow-list
is now one shared tuple (`core/cards.py::CARD_IDEA_CONCEPT_FIELDS`) that the writer emits and the
ledger admits, so the writer/reader disagreement that caused this cannot recur silently.
"""
from __future__ import annotations

import anyio
import pytest

from tests.factories import make_engine
from looplab.agents.roles import ToyResearcher
from looplab.core.models import Event, Idea
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED
from looplab.search.card_selection import card_action as projected_card_action

AUTHORED = ["loss/contrastive", "regularization/r-drop"]
# The seed membership the delta run authors at its root, and the change every later proposal states
# against it. `BASE - REMOVED + ADDED` is the membership a Card-built child must end up with.
BASE = ["loss/contrastive", "data/augmentation"]
ADDED = ["regularization/r-drop"]
REMOVED = ["data/augmentation"]
INHERITED = sorted(set(BASE) - set(REMOVED) | set(ADDED))


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


def test_a_delta_proposal_rides_the_card_lane_and_the_card_stays_selectable(tmp_path):
    """The half `2acdb825` left behind, and the constraint it stopped at, in one test.

    The delta travels UNRESOLVED (there is no base at mint time — see `_authored_card_concepts`), so
    what must survive the round trip is the operand pair and the mode, including the mode on its own:
    an explicit zero delta ("inherit unchanged") is a real claim and is indistinguishable from an
    absent envelope without it.

    `selection_ready` is the assertion that used to be False. It flipped because the three delta keys
    were missing from `_CARD_ADDED_ACTION_FIELDS`, so replay read the action as a lossy future schema
    — which is exactly why the writer and the ledger now share ONE tuple.
    """
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(concept_mode="delta", concepts=[],
                               concepts_added=list(ADDED), concepts_removed=list(REMOVED)))

    row = next(event for event in engine.store.read_all() if event.type == EV_CARD_ADDED)
    assert row.data["idea"]["concept_mode"] == "delta"
    assert row.data["idea"]["concepts_added"] == ADDED
    assert row.data["idea"]["concepts_removed"] == REMOVED
    assert "concepts" not in row.data["idea"], "a delta is not a membership and must not pose as one"

    executed, card = _claim(engine, plan.card_id)
    # The defect verbatim: all three of these used to come back empty/None.
    assert executed.concept_mode == "delta"
    assert executed.concepts_added == ADDED
    assert executed.concepts_removed == REMOVED
    assert executed.concepts == []
    # ...and the Card is still claimable at all, which is the property the earlier review stopped on.
    assert card.selection_ready is True
    # The board makes no membership claim for a delta: there is nothing to resolve it against yet.
    assert card.concept_source is not None
    assert card.concept_source.kind == "card_added"
    assert card.concept_source.membership_present is False
    assert card.concept_tags == []


def test_an_explicit_zero_delta_and_an_authored_empty_set_are_both_carried(tmp_path):
    """The two envelopes a truthiness test collapses. `delta` with both lists empty inherits its
    parents unchanged; `full` with an empty list is an authoritative KNOWN-EMPTY membership. Both
    used to record nothing at all, which replay reads as ABSENT — a third, different fact."""
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    zero = _mint(engine, _idea(concept_mode="delta", concepts=[]))
    executed, card = _claim(engine, zero.card_id)
    assert executed.concept_mode == "delta"
    assert executed.concepts_added == [] and executed.concepts_removed == []
    assert card.selection_ready is True

    other = make_engine(tmp_path / "run2", n_seeds=0, max_nodes=4, card_driven_selection=True)
    known_empty = _mint(other, _idea(concept_mode="full", concepts=[]))
    row = next(event for event in other.store.read_all() if event.type == EV_CARD_ADDED)
    assert row.data["idea"] == {**row.data["idea"], "concept_mode": "full", "concepts": []}
    executed, card = _claim(other, known_empty.card_id)
    assert executed.concept_mode == "full" and executed.concepts == []
    # An exact empty membership, not a missing one.
    assert card.concept_source.membership_present is True
    assert card.concept_source.complete is True
    assert card.selection_ready is True


def test_an_unknown_future_idea_member_still_costs_the_card_its_selectability(tmp_path):
    """The narrowness of the exemption, driven rather than asserted about.

    The predecessor of this test poisoned a real log with a DELTA envelope and watched
    `selection_ready` flip; that behaviour is now the bug being fixed, but the rule underneath it is
    not. Concept membership is exempt because it is metadata with its own receipt
    (`Card.concept_source`) and no digested action field. Anything else in the idea block may gain
    EXECUTION meaning in a later schema, so an old reader must refuse to call the action complete.
    """
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    plan = _mint(engine, _idea(concept_mode="delta", concepts=[], concepts_added=list(ADDED)))
    assert fold(engine.store.read_all()).cards[plan.card_id].selection_ready is True

    poisoned = []
    for index, event in enumerate(engine.store.read_all()):
        data = event.data
        if event.type == EV_CARD_ADDED:
            data = {**data, "idea": {**data["idea"], "beam_width": 3}}
        poisoned.append(Event(seq=index, type=event.type, data=data))
    assert fold(poisoned).cards[plan.card_id].selection_ready is False


def test_the_writer_and_the_ledger_agree_on_the_envelope_they_exchange(tmp_path):
    """The drift this whole class of bug is made of, closed at both ends.

    A key the writer emits and the ledger has never heard of is read as a lossy future ACTION
    member, and the Card silently stops being claimable — no error, no red test, just a lane that
    stops producing nodes. So the vocabulary is ONE tuple: the digests must not cover it (identity
    stays frozen), the ledger must admit all of it, and the writer must not exceed it. The last part
    is DRIVEN over the real mint in every mode rather than read off the source.
    """
    from looplab.core.cards import (CARD_ACTION_DIGEST_V1_FIELDS, CARD_ACTION_DIGEST_V2_FIELDS,
                                    CARD_IDEA_CONCEPT_FIELDS)
    from looplab.events.card_ledger import _CARD_ADDED_ACTION_FIELDS

    envelope = set(CARD_IDEA_CONCEPT_FIELDS)
    assert not envelope & set(CARD_ACTION_DIGEST_V2_FIELDS)
    assert not envelope & set(CARD_ACTION_DIGEST_V1_FIELDS)
    assert envelope <= _CARD_ADDED_ACTION_FIELDS

    action_keys = {"operator", "params", "space", "eval_profile", "eval_timeout"}
    modes = [
        _idea(),                                                        # full, non-empty
        _idea(concept_mode="full", concepts=[]),                        # full, known-empty
        _idea(concept_mode="delta", concepts=[], concepts_added=list(ADDED)),
        _idea(concept_mode="delta", concepts=[]),                       # explicit zero delta
        _idea(concept_mode=None, concepts=[]),                          # no envelope at all
    ]
    for index, idea in enumerate(modes):
        engine = make_engine(tmp_path / f"run{index}", n_seeds=0, max_nodes=4,
                             card_driven_selection=True)
        block = _mint(engine, idea).payload["idea"]
        assert set(block) - action_keys <= envelope, "the mint emitted a key no reader admits"
        assert fold(engine.store.read_all()).cards["card-0"].selection_ready is True


def test_the_delta_envelope_rides_outside_the_ownership_digest_too(tmp_path):
    """The identity constraint, over the members the earlier review could not admit: two Cards that
    differ only in what the proposal tagged are the same executable action and mint the same
    receipt. `CARD_IDEA_CONCEPT_FIELDS` is the complement of `CARD_ACTION_DIGEST_V2_FIELDS`, so no
    already-minted Card is invalidated by any of this."""
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    delta = _mint(engine, _idea(concept_mode="delta", concepts=[],
                                concepts_added=list(ADDED), concepts_removed=list(REMOVED))).payload

    other = make_engine(tmp_path / "run2", n_seeds=0, max_nodes=4, card_driven_selection=True)
    bare = _mint(other, _idea(concept_mode=None, concepts=[])).payload

    assert delta["ownership_receipt"] == bare["ownership_receipt"]
    assert {key: value for key, value in delta["idea"].items()
            if key not in ("concept_mode", "concepts_added", "concepts_removed")} == bare["idea"]


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


class _DeltaResearcher(ToyResearcher):
    """A real Researcher that authors a FULL membership at the root and a DELTA at every child.

    That is the shape `engine/proposal_cues.py` asks for once a parent membership exists, and the
    shape the live v6 Researcher emitted seven times while the Card lane dropped every one of them.
    `model_copy(update=...)` reaches the Idea the way the live producers do (see
    `_fixed_point_idea`), so the mint's fixed-point proof is exercised rather than side-stepped.
    """

    def propose(self, state, parent):
        idea = super().propose(state, parent)
        if parent is None:
            return idea.model_copy(update={"concept_mode": "full", "concepts": list(BASE)})
        return idea.model_copy(update={"concept_mode": "delta", "concepts": [],
                                       "concepts_added": list(ADDED),
                                       "concepts_removed": list(REMOVED)})


@pytest.fixture(scope="module")
def delta_run(tmp_path_factory):
    """ONE real Card-driven run whose children all propose deltas — the whole property, end to end."""
    run_dir = tmp_path_factory.mktemp("delta-run") / "run"
    engine = make_engine(run_dir, researcher=_DeltaResearcher({"x": (-5.0, 5.0), "y": (-5.0, 5.0)}),
                         n_seeds=1, max_nodes=3, card_driven_selection=True, speculation_depth=0)
    anyio.run(engine.run)
    return run_dir


def test_a_card_built_node_carries_the_delta_its_researcher_authored(delta_run):
    """THE defect, in the mode that was still broken: a real engine, a real delta, a real fold.

    The membership is resolved by `replay.py::_materialize_concept_deltas` against the node's own
    lineage — the run base at a root, the parents' effective sets below — which is why nothing needed
    resolving at mint time. Every non-root node lands on the same set here whatever its topology
    (`ADDED` is already inherited and `REMOVED` is already gone one level down), so the assertion is
    exact rather than a shape check.
    """
    events = EventStore(delta_run / "events.jsonl").read_all()
    state = fold(events)
    children = [node for node in state.nodes.values()
                if node.idea.card_id is not None and node.parent_ids]
    assert children, "the run took no Card-driven child build, so it proves nothing"
    # The durable row first: `node_created` is what every later reader and every replay starts from,
    # and for a Card-built node it is written from the rebuilt claim Idea and nothing else.
    created = {event.data["node_id"]: event.data["idea"]
               for event in events if event.type == "node_created"}
    for node in children:
        assert created[node.id]["concept_mode"] == "delta"
        assert created[node.id]["concepts_added"] == ADDED
        assert created[node.id]["concepts_removed"] == REMOVED
    for node in children:
        # Before this change every one of these was None/[]: the claim rebuilt the Idea from a
        # `card_added` idea block that had dropped the whole envelope.
        assert node.idea.concept_mode == "delta", f"node {node.id} lost its authored mode"
        assert node.idea.concepts_added == ADDED
        assert node.idea.concepts_removed == REMOVED
        assert state.node_concepts.get(node.id) == INHERITED, (
            f"node {node.id} inherits {state.node_concepts.get(node.id)}, not {INHERITED}")
        assert state.node_concept_provenance[node.id] == "researcher-authored"
        assert state.node_concept_materialization_receipts.get(node.id) is None
    # ...and the root's full membership still rides, so the base the deltas resolve against is real.
    assert state.node_concepts.get(0) == BASE


def test_the_delta_reaches_the_board_through_the_node_that_resolved_it(delta_run):
    """`Card.concept_tags` is what the belief board, the memory shelf and the run-base seed read. A
    delta Card claims no membership at mint time and inherits the exact one from its own node."""
    state = fold(EventStore(delta_run / "events.jsonl").read_all())
    for node in state.nodes.values():
        card = state.cards.get((node.idea.card_id or "").strip())
        if card is None or not node.parent_ids:
            continue
        assert card.concept_source is not None and card.concept_source.kind == "node"
        assert card.concept_source.node_id == node.id
        assert card.concept_tags == INHERITED
        assert card.provenance_tier == "researcher-authored"


def test_the_same_log_folds_to_the_same_cards_and_the_same_memberships(delta_run):
    """The replay contract. Nothing about the membership is recomputed from live state at claim time
    — the delta is durable and the resolver is a pure post-pass over the folded DAG — so a re-read of
    the same bytes must produce the same Cards AND the same node concepts. A mint-time resolution
    would have made this depend on when the log was read."""
    first = fold(EventStore(delta_run / "events.jsonl").read_all())
    second = fold(EventStore(delta_run / "events.jsonl").read_all())
    assert second.node_concepts == first.node_concepts
    assert second.node_concept_provenance == first.node_concept_provenance
    assert (second.node_concept_materialization_receipts
            == first.node_concept_materialization_receipts)
    assert second.run_base_concepts == first.run_base_concepts
    assert sorted(second.cards) == sorted(first.cards)
    for card_id, card in first.cards.items():
        replayed = second.cards[card_id]
        assert replayed.concept_tags == card.concept_tags
        assert replayed.concept_source == card.concept_source
        assert replayed.selection_ready == card.selection_ready
        assert replayed.identity == card.identity
